"""定时秒杀链路验证（演进 D 定时化）：排期→到点自动开抢→开抢前提醒预约者→抢购落库。

与 seckill_load.py（并发削峰）互补，本脚本只验「时间驱动的编排」这一段：
  1. 管理员给某上架礼品排一个未来时刻的秒杀场次（create_session）。
  2. 员工能列出该场次并「预约」（预约=订阅提醒，不锁名额）。
  3. 开抢前 5 分钟窗口内，调度线程给预约者入队并投递一条提醒（直连 DB 核对）。
  4. 到点后调度线程自动 warm 并把场次转 live（无需任何手动预热）。
  5. live 后员工 grab→异步落单成功；直连 DB 核对订单/桶库存一致。

需先起带秒杀的后端（SECKILL_ENABLED=1）并与本脚本用同一 MySQL/Redis。
环境变量：BASE(默认 127.0.0.1:8010) / LEAD(开抢倒计时秒,默认 35) / STOCK(默认 5)
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

BASE = os.getenv("BASE", "http://127.0.0.1:8010").rstrip("/")
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PW = os.getenv("ADMIN_PW", "admin123")
PW = "Seckill@12345"
LEAD = int(os.getenv("LEAD", "35"))     # 场次开抢时间 = 现在 + LEAD 秒
STOCK = int(os.getenv("STOCK", "5"))
COST = 1
POINTS = 1000

_failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    print(("  [OK] " if cond else "  [XX] ") + msg)
    if not cond:
        _failures.append(msg)


def req(method: str, path: str, body=None, token: str = ""):
    r = urllib.request.Request(BASE + path,
                               data=None if body is None else json.dumps(body).encode("utf-8"),
                               method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"detail": raw[:200]}


def main() -> None:
    from portal.backend import db, redis_client

    # ---- 0. 前置：登录管理员 ----
    st, r = req("POST", "/api/auth/login", {"username": ADMIN_USER, "password": ADMIN_PW})
    if st != 200:
        raise SystemExit(f"[x] 管理员登录失败 {st}: {r}")
    at = r["token"]

    # ---- 1. 建礼品 + 员工备分 ----
    st, r = req("POST", "/api/admin/gifts",
                {"name": "定时秒杀·验证(勿动)", "category": "秒杀", "points_cost": COST,
                 "stock": STOCK, "icon": "⏰", "description": "场次链路验证专用"}, token=at)
    if st != 200:
        raise SystemExit(f"[x] 建礼品失败 {st}: {r}")
    gid = r["id"]
    print(f"[+] 礼品 id={gid} 库存={STOCK}")

    uname = f"skchk{int(time.time()) % 1000000}"
    st, r = req("POST", "/api/auth/register",
                {"username": uname, "password": PW, "name": "预约验证号", "department": "SECKILL"})
    if st != 200:
        raise SystemExit(f"[x] 注册员工失败 {st}: {r}")
    et, emp = r["token"], r["user"]["emp_id"]
    req("POST", "/api/admin/points/adjust",
        {"emp_id": emp, "points": POINTS, "reason": "秒杀备分", "request_id": f"chk{emp}"}, token=at)

    # ---- 2. 排一个 LEAD 秒后开抢的场次 ----
    start = (datetime.now() + timedelta(seconds=LEAD)).strftime("%Y-%m-%dT%H:%M:%S")
    st, r = req("POST", "/api/admin/seckill/sessions",
                {"gift_id": gid, "start_at": start, "stock": STOCK}, token=at)
    if st != 200:
        raise SystemExit(f"[x] 排期失败 {st}: {r}")
    sid = r["id"]
    print(f"[+] 场次 id={sid} 开抢={start}")
    start_ts = time.time() + LEAD   # 本场开抢的墙钟时刻，后续两个等待窗口都以它为准

    # 额度超库存 / 未来时间 两条护栏
    st, _ = req("POST", "/api/admin/seckill/sessions",
                {"gift_id": gid, "start_at": (datetime.now() + timedelta(seconds=LEAD)).strftime("%Y-%m-%dT%H:%M:%S"),
                 "stock": STOCK + 1}, token=at)
    check(st == 400, "超库存排期被拒（400）")
    st, _ = req("POST", "/api/admin/seckill/sessions",
                {"gift_id": gid, "start_at": (datetime.now() - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S"),
                 "stock": 1}, token=at)
    check(st == 400, "过去时间排期被拒（400）")

    # ---- 3. 员工列出场次并预约 ----
    st, r = req("GET", "/api/seckill/sessions", token=et)
    mine = [s for s in r.get("sessions", []) if s["id"] == sid]
    check(st == 200 and mine and mine[0]["status"] == "scheduled", "员工能列出该 scheduled 场次")

    # 开抢前抢：应被判「未开始」（409，Redis 无额度键）
    st, _ = req("POST", f"/api/seckill/{gid}/grab", {}, token=et)
    check(st == 409, "开抢前抢被拒（409 活动未开始）")

    st, _ = req("POST", f"/api/seckill/sessions/{sid}/reserve", {}, token=et)
    check(st == 200, "预约成功")
    st, _ = req("POST", f"/api/seckill/sessions/{sid}/reserve", {}, token=et)
    check(st == 200, "重复预约幂等（仍 200）")

    # ---- 4. 等提醒在开抢前投递（调度 tick 内、start 之前）----
    remind_deadline = start_ts - 2
    out_cnt = nt_cnt = 0
    while time.time() < remind_deadline:
        out_cnt = db.query_one(
            "SELECT COUNT(*) c FROM notification_outbox WHERE event_key LIKE %s AND status='sent'",
            (f"seckill_remind:{sid}:%",))["c"]
        nt_cnt = db.query_one(
            "SELECT COUNT(*) c FROM notifications WHERE emp_id=%s AND title LIKE %s",
            (emp, "%秒杀即将开始%"))["c"]
        if nt_cnt > 0:
            break
        time.sleep(1)
    check(out_cnt > 0, f"提醒已入发件箱并投递（sent={out_cnt}）")
    check(nt_cnt > 0, f"预约者站内信已收到提醒（notifications={nt_cnt}）")
    nb = db.query_one("SELECT notified_at FROM seckill_sessions WHERE id=%s", (sid,))
    check(nb and nb["notified_at"] is not None, "notified_at 已标记（只发一次）")

    # ---- 5. 等自动开抢（无需手动 warm）----
    live_deadline = start_ts + 15
    status = ""
    while time.time() < live_deadline:
        status = db.query_one("SELECT status FROM seckill_sessions WHERE id=%s", (sid,))["status"]
        if status == "live":
            break
        time.sleep(1)
    check(status == "live", f"到点自动转 live（status={status}，全程无手动预热）")
    redis_key = f"{{portal}}:seckill:stock:{gid}"
    rl = redis_client.client().get(redis_key)
    check(rl is not None and int(rl) == STOCK - 0, f"调度器已把额度预热进 Redis（={rl}）")

    # ---- 6. 抢购 -> 异步落单 ----
    st, r = req("POST", f"/api/seckill/{gid}/grab", {}, token=et)
    check(st == 200 and r.get("state") == "queued", f"live 后抢到名额并入队（state={r.get('state')}）")
    key = r.get("key", "")
    final = ""
    for _ in range(30):
        final = req("GET", f"/api/seckill/res/{key}", token=et)[1].get("state", "")
        if final in ("success", "failed"):
            break
        time.sleep(0.5)
    check(final == "success", f"异步落单成功（state={final}）")

    # ---- 7. 直连 DB 核对账实 ----
    orders = db.query_one("SELECT COUNT(*) c FROM redemptions WHERE gift_id=%s", (gid,))["c"]
    bucket = db.query_one(
        "SELECT COALESCE(SUM(stock),0) t FROM gift_stock_bucket WHERE gift_id=%s", (gid,))["t"]
    rl_after = int(redis_client.client().get(redis_key) or -1)
    check(orders == 1, f"该礼品订单数=1（实际 {orders}）")
    check(int(bucket) == STOCK - 1, f"桶库存扣减正确（剩 {int(bucket)} / 应 {STOCK-1}）")
    check(rl_after == STOCK - 1, f"Redis 额度同步扣减（剩 {rl_after}）")

    # 收尾：结束场次 + 下架测试礼品，避免残留
    req("POST", f"/api/admin/seckill/sessions/{sid}/end", {}, token=at)
    req("POST", f"/api/admin/gifts/{gid}/status", {"status": "offline"}, token=at)

    print("\n[结论]", "PASS 定时秒杀全链路（预约→提醒→自动开抢→抢购落库）通过"
          if not _failures else f"FAIL {len(_failures)} 项：{_failures}")
    sys.exit(1 if _failures else 0)


if __name__ == "__main__":
    main()
