"""秒杀（演进 D）：紧俏商品高并发抢购的分层漏斗落地。

一句话设计：**把「售罄风暴」挡在 MySQL 之外**。Redis 只做前置削峰计数器，
库存不足时绝大多数请求应在碰到任何 DB 行锁/连接池之前就被 Redis 原子预扣挡回——
这是本方案对既有 redeem 无锁预检（那层仍要打一次 DB 查询）的进一步前移。

三条不可越界的边界（与 mq.py / redemption.py 同源）：
  1. Redis 绝不改 DB 库存真相。预扣只是「排队名额计数器」，权威扣减/扣分/建订单/幂等/
     baseline/对账全部由 redemption.redeem 在同一事务里完成——账实一致的分寸一寸不动。
     因此 reconciliation 天然保持绿：秒杀没有引入第二套库存口径。
  2. 抢到≠买到。预扣成功只表示「拿到一次落单资格并入队」，最终是否成交取决于 DB 事务
     （积分够不够、货还在不在）。落单失败（积分不足/下架）回补预扣名额让给别人抢；
     DB 真售罄（out_of_stock）不回补（回填了也没货，纯噪声）。
  3. 至少一次 + 落库幂等：消费崩溃/重投由 redeem 的 request_id 唯一约束（uk_redeem_request）
     兜底——同一条预约重复消费只会命中重放分支返回原结果，绝不二次扣款/二次建单。

默认关（SECKILL_ENABLED != 1）：本模块所有入口在未启用时抛 409，消费者不启动，
既有链路与离线测试完全不受影响，也不碰 redis。
"""
from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import datetime
from typing import Optional

from fastapi import HTTPException

from . import config, db, notifier, redis_client, redemption

logger = logging.getLogger("portal.seckill")

_stop = threading.Event()
_threads: list[threading.Thread] = []

# ---------- Redis 键（带 {portal} hash tag，未来迁集群同槽） ----------


def _stock_key(gid: int) -> str:
    return f"{{portal}}:seckill:stock:{gid}"


def _bought_key(gid: int) -> str:
    return f"{{portal}}:seckill:bought:{gid}"


def _res_key(key: str) -> str:
    return f"{{portal}}:seckill:res:{key}"


# ---------- Lua：原子预扣（削峰的关键，单命令内完成判库存+判限购+扣减） ----------
# 返回码：1=拿到名额；0=已售罄；-1=该用户已抢过（限购）；-2=活动未预热/已结束。
_PRE_DEDUCT_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then return -2 end
if redis.call('SISMEMBER', KEYS[2], ARGV[1]) == 1 then return -1 end
if tonumber(redis.call('GET', KEYS[1])) <= 0 then return 0 end
redis.call('DECR', KEYS[1])
redis.call('SADD', KEYS[2], ARGV[1])
return 1
"""

# 回补：落单失败但货仍在（积分不足/临时下架）时把名额放回池子，并撤销该用户限购标记。
_COMPENSATE_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then redis.call('INCR', KEYS[1]) end
redis.call('SREM', KEYS[2], ARGV[1])
return 1
"""

_lua_cache: dict = {}


def _script(name: str, body: str):
    """惰性登记并复用 Script 对象（redis-py 内部按 SHA 缓存，命中走 EVALSHA）。"""
    r = redis_client.client()
    s = _lua_cache.get(name)
    if s is None:
        s = r.register_script(body)
        _lua_cache[name] = s
    return s


def enabled() -> bool:
    return config.SECKILL_ENABLED


def _require_enabled() -> None:
    if not enabled():
        raise HTTPException(409, "秒杀活动未启用（SECKILL_ENABLED）")


# ---------- 活动预热 / 收摊 ----------

def warm(gid: int, override: Optional[int] = None) -> dict:
    """把 DB 当前库存真相（各桶之和）快照进 Redis 作预扣额度；override 可强制指定。

    以 DB 真相为准：Redis 额度 = 真实可售，保证「预扣成功数 ≤ DB 可成交数」的上界，
    配合 redeem 里 `WHERE stock>0` 的每桶原子扣减，超卖在两层都无路可超。
    """
    _require_enabled()
    if override is None:
        # 与 redemption.redeem 预检同口径：尚无桶行的新礼品回退到 gifts.stock，
        # 否则 SUM(桶) 的空值会把预热额度误算成 0，全场秒判售罄。
        row = db.query_one(
            "SELECT COALESCE((SELECT SUM(stock) FROM gift_stock_bucket WHERE gift_id = %s), "
            "(SELECT stock FROM gifts WHERE id = %s)) AS t", (gid, gid))
        total = int(row["t"]) if row and row["t"] is not None else 0
    else:
        total = int(override)
    if total < 0:
        raise HTTPException(400, "预热额度不能为负")
    r = redis_client.client()
    pipe = r.pipeline()
    pipe.set(_stock_key(gid), total, ex=config.SECKILL_TTL_SECONDS)
    pipe.delete(_bought_key(gid))       # 收摊后重新开抢清限购记录
    pipe.execute()
    return {"gift_id": gid, "warmed_stock": total}


def unwarm(gid: int) -> dict:
    """结束活动：删除额度/限购键，后续预扣一律返回 -2（活动未开始/已结束）。"""
    _require_enabled()
    r = redis_client.client()
    removed = r.delete(_stock_key(gid), _bought_key(gid))
    return {"gift_id": gid, "keys_removed": removed}


# ---------- 预约（用户热路径：一次 Redis 往返，售罄请求在此终结，零 DB） ----------

def reserve(gid: int, emp_id: str) -> dict:
    """原子预扣 → 成功则生成预约并入队；失败态（售罄/限购/未开始）直接返回，不打 DB。"""
    _require_enabled()
    r = redis_client.client()
    code = int(_script("pre_deduct", _PRE_DEDUCT_LUA)(
        keys=[_stock_key(gid), _bought_key(gid)], args=[emp_id]))
    if code == -2:
        raise HTTPException(409, "秒杀活动未开始或已结束，请先预热库存")
    if code == -1:
        return {"state": "already"}          # 已抢过（限购 1 件）
    if code == 0:
        return {"state": "sold_out"}         # 售罄：整条链路到此为止，没有一次 DB 访问
    # code == 1：拿到名额。预约键即 redeem 的幂等 request_id（uuid hex，合法字符集内）。
    key = uuid.uuid4().hex
    r.hset(_res_key(key), mapping={"state": "queued", "gid": str(gid),
                                   "emp_id": emp_id, "request_id": key})
    r.expire(_res_key(key), config.SECKILL_TTL_SECONDS)
    r.xadd(config.SECKILL_STREAM, {"key": key, "gid": str(gid),
                                   "emp_id": emp_id, "request_id": key})
    return {"state": "queued", "key": key}


def status(key: str) -> dict:
    """查询一次预约的最终状态（success/failed/queued）。供用户端轮询或 SSE 补拉。"""
    _require_enabled()
    d = redis_client.client().hgetall(_res_key(key))
    if not d:
        return {"state": "unknown"}
    return d


# ---------- 异步下单消费者（复用 redemption.redeem 落库） ----------

def _finish(key: str, state: str, *, order_id=None, reason: str = "") -> None:
    r = redis_client.client()
    fields = {"state": state}
    if order_id is not None:
        fields["order_id"] = str(order_id)
    if reason:
        fields["reason"] = reason
    r.hset(_res_key(key), mapping=fields)


def _compensate(gid: int, emp_id: str) -> None:
    """回补预扣名额：放回额度 + 撤销限购标记，让其它请求有机会成交。"""
    redis_client.client().eval(
        _COMPENSATE_LUA, 2, _stock_key(gid), _bought_key(gid), emp_id)


def _process(body: dict) -> None:
    """处理一条预约：跑权威 DB 事务，按结果落状态；可回收的失败回补名额。"""
    key = body["key"]
    gid = int(body["gid"])
    emp = body["emp_id"]
    try:
        result = redemption.redeem(gid, redemption.RequestIn(request_id=body["request_id"]), emp)
    except HTTPException as exc:
        # 同键不同礼品(409)/礼品不存在(404) 等：视为该名额作废并回补（货通常仍在）。
        logger.warning("秒杀落单业务异常 key=%s gid=%s emp=%s status=%s", key, gid, emp, exc.status_code)
        _finish(key, "failed", reason=f"http_{exc.status_code}")
        _compensate(gid, emp)
        return
    except Exception:  # noqa: BLE001
        logger.exception("秒杀落单未预期异常 key=%s gid=%s emp=%s", key, gid, emp)
        _finish(key, "failed", reason="error")
        _compensate(gid, emp)
        return
    if result.get("ok"):
        _finish(key, "success", order_id=result.get("redemption_id"))
        return
    reason = result.get("reason", "failed")
    _finish(key, "failed", reason=reason)
    # out_of_stock 是 DB 真相售罄，不回补（补了也无货）；其余可回收失败把名额还给池子。
    if reason != "out_of_stock":
        _compensate(gid, emp)


def _consume() -> None:
    r = redis_client.client()
    stream, grp = config.SECKILL_STREAM, config.SECKILL_GROUP
    try:
        r.xgroup_create(stream, grp, id="$", mkstream=True)
    except Exception as exc:  # noqa: BLE001
        if "BUSYGROUP" not in str(exc):
            logger.exception("秒杀消费组建组失败")
            return
    name = f"sk-{os.getpid()}-{threading.get_ident()}"
    logger.info("秒杀下单消费者启动 stream=%s group=%s consumer=%s", stream, grp, name)
    while not _stop.is_set():
        try:
            resp = r.xreadgroup(grp, name, {stream: ">"}, count=8, block=config.SECKILL_BLOCK_MS)
        except Exception:  # noqa: BLE001
            logger.exception("秒杀 xreadgroup 失败，退避后重试")
            _stop.wait(1)
            continue
        if not resp:
            continue
        for _s, msgs in resp:
            for mid, body in msgs:
                try:
                    _process(body)
                except Exception:  # noqa: BLE001
                    # _process 内部已兜底；这里只防御性保证单条毒消息不停线程。
                    logger.exception("秒杀消费循环漏网异常 mid=%s", mid)
                # 无论成交/售罄/失败都已落状态（幂等），ACK 掉；重投撞 request_id 只会重放。
                r.xack(stream, grp, mid)


def start_consumer() -> None:
    """随 worker 启动有限并发的下单消费者。幂等。未启用则不启动。"""
    global _threads
    if not enabled():
        return
    if any(t.is_alive() for t in _threads):
        return
    _stop.clear()
    _threads = []
    for _ in range(max(1, config.SECKILL_CONSUMERS)):
        t = threading.Thread(target=_consume, name="seckill-consumer", daemon=True)
        t.start()
        _threads.append(t)
    logger.info("秒杀消费者已启动，并发度=%d", len(_threads))


def stop_consumer() -> None:
    _stop.set()
    for t in _threads:
        if t.is_alive():
            t.join(timeout=2)


# ---------- 观测：统计 / 排空（对账兜底与压测收尾用） ----------

def stats(gid: int) -> dict:
    """并列展示 Redis 额度 与 DB 库存真相，用于确认「Redis 削峰没把货算错」。"""
    _require_enabled()
    r = redis_client.client()
    stock_left = r.get(_stock_key(gid))
    bought = r.scard(_bought_key(gid))
    db_row = db.query_one(
        "SELECT COALESCE((SELECT SUM(stock) FROM gift_stock_bucket WHERE gift_id = %s), "
        "(SELECT stock FROM gifts WHERE id = %s)) AS t", (gid, gid))
    db_left = int(db_row["t"]) if db_row and db_row["t"] is not None else 0
    try:
        queue_len = int(r.xlen(config.SECKILL_STREAM))
    except Exception:  # noqa: BLE001
        queue_len = -1
    return {"gift_id": gid, "redis_stock_left": int(stock_left) if stock_left is not None else None,
            "redis_bought": bought, "db_stock_left": db_left, "queue_len": queue_len}


def drain(max_msgs: int = 100000) -> int:
    """把当前队列里所有待处理预约同步消费掉（压测/一次性核对用），返回处理条数。"""
    if not enabled():
        return 0
    r = redis_client.client()
    stream, grp = config.SECKILL_STREAM, config.SECKILL_GROUP
    try:
        r.xgroup_create(stream, grp, id="0", mkstream=True)
    except Exception:  # noqa: BLE001
        pass
    name = f"sk-drain-{os.getpid()}"
    done = 0
    while done < max_msgs:
        resp = r.xreadgroup(grp, name, {stream: ">"}, count=8, block=200)
        if not resp:
            break
        got = 0
        for _s, msgs in resp:
            for mid, body in msgs:
                _process(body)
                r.xack(stream, grp, mid)
                got += 1
        if not got:
            break
        done += got
    return done


# ================= 定时场次 + 预约 + 调度器 =================
# 把「手动预热才能抢」升级成「排好钟点、到点自动开抢、开抢前 5 分钟提醒预约者」。
# 不变式不变：DB 的 seckill_sessions.status 是唯一真相，调度线程只是按时间推进它并
# 在转 live 那一刻调 warm() 把额度快照进 Redis；Redis 仍不参与库存真相，也不改 DB。

def create_session(gift_id: int, start_at: datetime, stock: int, operator: str) -> dict:
    """管理员为某上架礼品排一个定时秒杀场次。额度不得超过当前真实库存。"""
    _require_enabled()
    if stock <= 0:
        raise HTTPException(400, "秒杀额度必须大于 0")
    if start_at <= datetime.now():
        raise HTTPException(400, "开抢时间必须晚于当前时间")
    gift = db.query_one("SELECT id, name, status FROM gifts WHERE id = %s", (gift_id,))
    if not gift:
        raise HTTPException(404, "礼品不存在")
    if gift["status"] != "active":
        raise HTTPException(400, "礼品未上架，无法排秒杀")
    row = db.query_one(
        "SELECT COALESCE((SELECT SUM(stock) FROM gift_stock_bucket WHERE gift_id = %s), "
        "(SELECT stock FROM gifts WHERE id = %s)) AS t", (gift_id, gift_id))
    total = int(row["t"]) if row and row["t"] is not None else 0
    if stock > total:
        raise HTTPException(400, f"秒杀额度 {stock} 超过当前可售库存 {total}")
    clash = db.query_one(
        "SELECT id FROM seckill_sessions WHERE gift_id = %s AND status IN ('scheduled','live')",
        (gift_id,))
    if clash:
        raise HTTPException(409, f"该礼品已有未结束的秒杀场次（#{clash['id']}），请先结束")
    sid = db.insert(
        "INSERT INTO seckill_sessions (gift_id, start_at, stock, created_by) VALUES (%s,%s,%s,%s)",
        (gift_id, start_at, stock, operator))
    logger.info("秒杀排期 session=%s gift=%s 开抢=%s 额度=%s", sid, gift_id, start_at, stock)
    return get_session(sid)


def get_session(sid: int) -> Optional[dict]:
    return db.query_one(
        "SELECT ss.*, g.name AS gift_name, g.points_cost, g.icon "
        "FROM seckill_sessions ss JOIN gifts g ON g.id = ss.gift_id WHERE ss.id = %s", (sid,))


def list_sessions(emp_id: str) -> list[dict]:
    """用户端：未开始(scheduled) + 进行中(live) 的场次，附带本人是否已预约。"""
    return db.query(
        "SELECT ss.*, g.name AS gift_name, g.points_cost, g.icon, g.image_key, "
        "  EXISTS(SELECT 1 FROM seckill_reservations r WHERE r.session_id = ss.id AND r.emp_id = %s) AS reserved "
        "FROM seckill_sessions ss JOIN gifts g ON g.id = ss.gift_id "
        "WHERE ss.status IN ('scheduled','live') ORDER BY ss.start_at", (emp_id,))


def list_admin_sessions() -> list[dict]:
    return db.query(
        "SELECT ss.*, g.name AS gift_name, "
        "  (SELECT COUNT(*) FROM seckill_reservations r WHERE r.session_id = ss.id) AS reserve_count "
        "FROM seckill_sessions ss JOIN gifts g ON g.id = ss.gift_id "
        "ORDER BY ss.status = 'live' DESC, ss.start_at DESC LIMIT 50")


def reserve_session(sid: int, emp_id: str) -> dict:
    """预约=订阅提醒（不锁名额）。仅未开始时可约；uk 保证重复预约幂等。"""
    s = get_session(sid)
    if not s:
        raise HTTPException(404, "场次不存在")
    if s["status"] != "scheduled":
        raise HTTPException(409, "该秒杀已开抢或已结束，无法预约")
    db.execute(
        "INSERT INTO seckill_reservations (session_id, emp_id) VALUES (%s,%s) "
        "ON DUPLICATE KEY UPDATE id = id", (sid, emp_id))
    return {"session_id": sid, "reserved": True}


def cancel_reservation(sid: int, emp_id: str) -> dict:
    db.execute("DELETE FROM seckill_reservations WHERE session_id = %s AND emp_id = %s",
               (sid, emp_id))
    return {"session_id": sid, "reserved": False}


def end_session(sid: int) -> Optional[dict]:
    """管理员提前收摊：live 中的场次回收 Redis 额度，一律置 ended。"""
    s = get_session(sid)
    if not s:
        raise HTTPException(404, "场次不存在")
    if s["status"] == "live":
        try:
            unwarm(s["gift_id"])
        except HTTPException:
            pass   # 未启用/Redis 不可用时仍需把场次关掉，Redis 残留交给 TTL 兜底
    db.execute(
        "UPDATE seckill_sessions SET status = 'ended', end_at = NOW() "
        "WHERE id = %s AND status IN ('scheduled','live')", (sid,))
    return get_session(sid)


# ---------- 调度线程：按时间推进 scheduled→live→ended，并在窗口内提醒预约者 ----------

def _notify_subscribers(sid: int, gid: int, start_at: datetime) -> None:
    """给预约者逐人入队一条开抢提醒，与 notified_at 同事务——保证只发一次。"""
    when = start_at.strftime("%H:%M") if isinstance(start_at, datetime) else str(start_at)

    def fn(cur):
        cur.execute("SELECT emp_id FROM seckill_reservations WHERE session_id = %s", (sid,))
        emps = [r["emp_id"] for r in cur.fetchall()]
        cur.execute("SELECT name FROM gifts WHERE id = %s", (gid,))
        g = cur.fetchone()
        name = g["name"] if g else "礼品"
        for emp in emps:
            notifier.enqueue(
                cur, event_key=f"seckill_remind:{sid}:{emp}",
                audience=notifier.user_audience(emp), ntype="seckill",
                ref_type="seckill", ref_id=sid, title="⚡ 秒杀即将开始",
                content=f"你预约的「{name}」将于 {when} 开抢，准备好手速！")
        cur.execute("UPDATE seckill_sessions SET notified_at = NOW() "
                    "WHERE id = %s AND notified_at IS NULL", (sid,))
        return len(emps)

    try:
        n = db.run_tx(fn)
        if n:
            logger.info("秒杀提醒 session=%s 已发给 %d 位预约者", sid, n)
    except Exception:  # noqa: BLE001
        logger.exception("秒杀提醒入队失败 session=%s", sid)


def _tick() -> None:
    # 1) 到点自动开抢：预热额度并转 live（先于提醒，使已过点的场次不再补发提醒）。
    for s in db.query(
        "SELECT * FROM seckill_sessions WHERE status = 'scheduled' AND start_at <= NOW() "
        "ORDER BY start_at"):
        try:
            warm(s["gift_id"], s["stock"])
        except Exception:  # noqa: BLE001
            logger.exception("秒杀场次自动预热失败 session=%s", s["id"])
            continue
        db.execute(
            "UPDATE seckill_sessions SET status = 'live', "
            "end_at = DATE_ADD(start_at, INTERVAL %s MINUTE) "
            "WHERE id = %s AND status = 'scheduled'",
            (config.SECKILL_LIVE_MINUTES, s["id"]))
        logger.info("秒杀开抢 session=%s gift=%s 额度=%s", s["id"], s["gift_id"], s["stock"])
    # 2) 开抢前 N 分钟提醒预约者（仅未提醒过的 scheduled）。
    for s in db.query(
        "SELECT * FROM seckill_sessions WHERE status = 'scheduled' AND notified_at IS NULL "
        "AND start_at <= DATE_ADD(NOW(), INTERVAL %s MINUTE) ORDER BY start_at",
        (config.SECKILL_REMIND_MINUTES,)):
        _notify_subscribers(s["id"], s["gift_id"], s["start_at"])
    # 3) 到点自动收摊：回收 Redis 额度并置 ended。
    for s in db.query(
        "SELECT * FROM seckill_sessions WHERE status = 'live' AND end_at IS NOT NULL "
        "AND end_at <= NOW()"):
        try:
            unwarm(s["gift_id"])
        except Exception:  # noqa: BLE001
            logger.exception("秒杀收摊回收失败 session=%s", s["id"])
        db.execute("UPDATE seckill_sessions SET status = 'ended' WHERE id = %s AND status = 'live'",
                   (s["id"],))
        logger.info("秒杀收摊 session=%s gift=%s", s["id"], s["gift_id"])


_sched_stop = threading.Event()
_sched_thread: Optional[threading.Thread] = None


def _scheduler_loop() -> None:
    while not _sched_stop.is_set():
        _sched_stop.wait(config.SECKILL_TICK_SECONDS)
        if _sched_stop.is_set():
            break
        try:
            _tick()
        except Exception:  # noqa: BLE001
            # 调度线程不能因一次查询失败就退出，否则定时开抢/提醒静默死掉。
            logger.exception("秒杀调度周期失败，下个周期重试")


def start_scheduler() -> None:
    global _sched_thread
    if not enabled():
        return
    if _sched_thread and _sched_thread.is_alive():
        return
    _sched_stop.clear()
    _sched_thread = threading.Thread(target=_scheduler_loop, name="seckill-scheduler", daemon=True)
    _sched_thread.start()
    logger.info("秒杀调度器已启动：提醒提前 %d 分钟，开抢窗口 %d 分钟，轮询 %ds",
                config.SECKILL_REMIND_MINUTES, config.SECKILL_LIVE_MINUTES,
                config.SECKILL_TICK_SECONDS)


def stop_scheduler() -> None:
    _sched_stop.set()
    if _sched_thread and _sched_thread.is_alive():
        _sched_thread.join(timeout=3)
