"""秒杀链路并发证明（演进 D）：紧俏商品到底能不能抗住秒杀？

打法：造一个库存很小（默认 20）的专用礼品，预热进 Redis；用一批已备分的账号
先打一轮「真抢购」，再用同一批 token 反复打若干轮洪峰。全程走 HTTP 打**正在运行
的后端**（默认 127.0.0.1:8000），结束后直接连 MySQL/Redis 核对账实。

要证明的两件事：
  1. 削峰有效：总请求数远大于库存，但真正进入 DB 落单的数量被 Redis 原子预扣压到
     ≤ 库存——售罄/限购的请求一次 DB 都不碰。
  2. 绝不超卖 + 账实一致：成功订单数 == 初始库存，桶库存归 0 且无负桶，
     该礼品在 reconciliation 里不出现在任何库存/订单差异中。

用法（需先起后端，且后端与本脚本用同一 Redis）：
  python portal/perf/seckill_load.py
环境变量：BASE / N / PW / STOCK / POINTS / COST / EXTRA_ROUNDS / WORKERS / ADMIN_USER / ADMIN_PW
仅标准库 + 复用后端模块做只读核对；对本地实例造数（注册/发分/建礼品/预热）属预期。
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

# 直接跑本文件时 sys.path[0] 是 perf/ 目录，注入仓库根让 `from portal.backend import ...` 可用。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

BASE = os.getenv("BASE", "http://127.0.0.1:8000").rstrip("/")
N = int(os.getenv("N", "60"))                 # 抢购账号数
PW = os.getenv("PW", "Seckill@12345")
STOCK = int(os.getenv("STOCK", "20"))         # 秒杀库存（真稀缺）
POINTS = int(os.getenv("POINTS", "1000"))     # 每号备分，保证积分不是瓶颈
COST = int(os.getenv("COST", "1"))            # 礼品单价
EXTRA_ROUNDS = int(os.getenv("EXTRA_ROUNDS", "9"))  # 首轮后再打几轮洪峰
WORKERS = int(os.getenv("WORKERS", "64"))
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PW = os.getenv("ADMIN_PW", "admin123")


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


def ensure_users(admin_token: str) -> list[str]:
    """注册/登录 N 个账号并发分，返回 token 列表（token 由服务端签发，进程内会话才认）。"""
    tokens = []
    for i in range(1, N + 1):
        uname = f"sk{i:03d}"
        st, r = req("POST", "/api/auth/register",
                    {"username": uname, "password": PW, "name": f"秒杀号{i}", "department": "SECKILL"})
        if st == 200:
            tok, emp = r["token"], r["user"]["emp_id"]
        elif st == 409:
            st2, r2 = req("POST", "/api/auth/login", {"username": uname, "password": PW})
            if st2 != 200:
                raise SystemExit(f"[x] {uname} 冲突且登录失败 {st2}: {r2}")
            tok, emp = r2["token"], r2["user"]["emp_id"]
        else:
            raise SystemExit(f"[x] 注册 {uname} 失败 {st}: {r}")
        req("POST", "/api/admin/points/adjust",
            {"emp_id": emp, "points": POINTS, "reason": "秒杀备分", "request_id": f"skg{i:03d}"},
            token=admin_token)
        tokens.append(tok)
    print(f"[+] 备妥 {len(tokens)} 个秒杀号（各 {POINTS} 分）")
    return tokens


def make_gift(admin_token: str) -> int:
    st, r = req("POST", "/api/admin/gifts",
                {"name": "限量秒杀·紧俏品(勿动)", "category": "秒杀", "points_cost": COST,
                 "stock": STOCK, "icon": "⚡", "description": "演进 D 秒杀并发证明专用"},
                token=admin_token)
    if st != 200:
        raise SystemExit(f"[x] 建礼品失败 {st}: {r}")
    print(f"[+] 秒杀礼品 id={r['id']} stock={STOCK} cost={COST}")
    return r["id"]


def burst(grab_tokens: list[str], gid: int) -> tuple[Counter, float]:
    """并发打一轮 grab，返回 (各状态计数, 本轮墙钟秒)。"""
    def one(tok):
        return req("POST", f"/api/seckill/{gid}/grab", {}, token=tok)[1].get("state", "?")
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        states = list(ex.map(one, grab_tokens))
    return Counter(states), time.perf_counter() - t0


def settle(keys: list[str], timeout: float = 60.0) -> Counter:
    """轮询给定预约键，直到不再有 queued（异步落单收敛），返回终态计数。"""
    deadline = time.time() + timeout
    final: Counter = Counter()
    pending = list(keys)
    while pending and time.time() < deadline:
        time.sleep(0.5)
        still = []
        for k in pending:
            st = req("GET", f"/api/seckill/res/{k}")[1].get("state", "?")
            if st == "queued":
                still.append(k)
            else:
                final[st] += 1
        pending = still
    if pending:
        final["timeout"] += len(pending)
    return final


def verify(gid: int, admin_token: str) -> None:
    """直连 DB/Redis + 复用领域对账，出最终结论。"""
    from portal.backend import config, db, redis_client, redemption

    orders = db.query_one("SELECT COUNT(*) AS c FROM redemptions WHERE gift_id = %s", (gid,))["c"]
    bucket_sum = int(db.query_one(
        "SELECT COALESCE(SUM(stock),0) AS t FROM gift_stock_bucket WHERE gift_id = %s", (gid,))["t"])
    neg = db.query_one(
        "SELECT COUNT(*) AS c FROM gift_stock_bucket WHERE gift_id = %s AND stock < 0", (gid,))["c"]
    redis_left = redis_client.client().get(f"{{portal}}:seckill:stock:{gid}")
    recon = redemption.reconciliation()
    gift_recon_bad = any(int(s["gift_id"]) == gid for s in recon["stocks"])
    gift_order_bad = any(int(o["gift_id"]) == gid for o in recon["orders"])

    print("\n===== 核对（DB 真相 vs Redis 削峰）=====")
    print(f"  成功订单数            : {orders}")
    print(f"  桶库存剩余 SUM        : {bucket_sum}")
    print(f"  负桶数量（应 0）      : {neg}")
    print(f"  Redis 剩余额度        : {redis_left}")
    print(f"  该礼品对账异常        : 库存={gift_recon_bad} 订单={gift_order_bad}")
    print(f"  （全局对账 ok={recon['ok']}，含历史噪声，仅供参考）")

    ok = (orders == STOCK and bucket_sum == 0 and neg == 0
          and not gift_recon_bad and not gift_order_bad)
    print("\n[结论]", "PASS 恰好卖出 %d 单、库存归 0、无超卖、该礼品对账无差异" % STOCK
          if ok else "FAIL 见上方不一致项")
    if not ok:
        raise SystemExit(1)


def main() -> None:
    st, r = req("POST", "/api/auth/login", {"username": ADMIN_USER, "password": ADMIN_PW})
    if st != 200:
        raise SystemExit(f"[x] 管理员登录失败 {st}: {r}（后端是否已启动并配好演示账号？）")
    admin_token = r["token"]

    gid = make_gift(admin_token)
    tokens = ensure_users(admin_token)

    st, r = req("POST", f"/api/admin/seckill/{gid}/warm", {}, token=admin_token)
    if st != 200:
        raise SystemExit(f"[x] 预热失败 {st}: {r}")
    print(f"[+] 已预热 Redis 额度 = {r['warmed_stock']}")

    total_reqs = 0
    agg: Counter = Counter()
    # 首轮：真抢购，拿到名额的会入队、由异步消费者落单。
    states, dt = burst(tokens, gid)
    total_reqs += len(tokens)
    agg.update(states)
    print(f"[~] 首轮：{dict(states)}  墙钟 {dt*1000:.0f}ms")

    # 多轮洪峰：同一批 token 反复打，赢家得 already、输家得 sold_out，全在 Redis 层终结、不触 DB。
    for rnd in range(EXTRA_ROUNDS):
        states, dt = burst(tokens, gid)
        total_reqs += len(tokens)
        agg.update(states)
        print(f"[~] 洪峰第{rnd+2}轮：{dict(states)}  墙钟 {dt*1000:.0f}ms")

    # 等异步消费者把首轮入队的落单跑完（以订单数收敛到库存为准）。
    deadline = time.time() + 60
    while time.time() < deadline:
        if req_db_orders(gid) >= STOCK:
            break
        time.sleep(0.5)

    db_writes = req_db_orders(gid)
    print("\n===== 削峰账本 =====")
    print(f"  总请求数              : {total_reqs}")
    print(f"  Redis 层终结(售罄/限购): {agg['sold_out'] + agg['already']}")
    print(f"  拿到名额入队(→触达DB) : {agg['queued']}")
    print(f"  实际进入 DB 落单成功  : {db_writes}")
    print(f"  被挡在 MySQL 之外比例 : {(total_reqs - agg['queued']) / total_reqs * 100:.1f}%")

    verify(gid, admin_token)


def req_db_orders(gid: int) -> int:
    from portal.backend import db
    return db.query_one("SELECT COUNT(*) AS c FROM redemptions WHERE gift_id = %s", (gid,))["c"]


if __name__ == "__main__":
    main()
