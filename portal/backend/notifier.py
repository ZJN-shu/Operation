"""通知投递：业务事务只写发件箱，投递在事务提交之后异步完成。

为什么拆成两步（这是问题被逼出来的，不是照抄模式）：

原本 `logic.notify(..., cur=cur)` 直接在业务事务里 INSERT notifications。它有两个
站不住的地方：

1. **投递失败会连带回滚业务。** 通知列是 VARCHAR(500)，内容超长、表被锁、连接抖动
   都会让整笔兑换白做。用户看到的是"兑换失败"，而账上其实一分没扣 —— 商品也还在。
   这个耦合在生产上是不可接受的。
2. **外部渠道根本放不进来。** 邮件 / 短信 / 企微是慢速网络调用，放进事务等于让
   礼品行锁多握几百毫秒甚至几秒，整条兑换链路的并发度被一个通知拖垮。

拆开之后的保证反而更强：**只有提交成功才发通知**，这件事由 InnoDB 的提交可见性
保证 —— outbox 行写在业务事务里，回滚时跟着一起消失，不需要任何"已发送"记账
代码（和 events.py 把 point_records 当 outbox 是同一个道理）。

投递侧是**至少一次**：投递成功前先把 attempts 加一，进程崩了重跑也不会凭空多发；
重复投递由 notifications 上的 uk_outbox(outbox_id, emp_id) 兜住，撞键即空操作，
不靠内存标记去重。失败走指数退避（带抖动，避免一批失败通知齐步重试），重试耗尽
转成 dead 状态留在表里 —— **失败必须可观测，不能静默消失**，管理员接口能查到。
"""
from __future__ import annotations

import logging
import random
import threading
from typing import Callable, Optional

from . import db

logger = logging.getLogger("portal.notifier")

PENDING = "pending"
SENT = "sent"
DEAD = "dead"

# 投递线程的节奏：0.5s 一轮，人的感知上仍是实时；批量上限防止单轮拖太久。
_POLL_INTERVAL = 0.5
_BATCH = 50

# 重试策略：最多 5 次，2→4→8→16s 指数退避，上限 60s。
_MAX_ATTEMPTS = 5
_BACKOFF_BASE = 2.0
_BACKOFF_MAX = 60.0

# 已投递行的保留期。outbox 是流水表，不回收会一直涨；
# dead 行**不回收** —— 那是死信队列，留着给人工处理。
_RETENTION_DAYS = 30
_REAP_EVERY = 120        # 每多少个周期回收一次
_REAP_LIMIT = 1000       # 单次回收上限，避免长事务持锁

# 商城运营通知的受众：只发给管库存的人，不含 content_admin（管内容）和 viewer（只读）。
SHOP_ADMIN_ROLES = ("super_admin", "shop_admin")

# 外部渠道（邮件 / 短信 / 企微机器人）注册到这里，签名 (row, emp_id) -> None。
# 它们一律在事务之外被调用，失败走同一套退避重试；同一条 outbox 行重复调用时
# 必须自身幂等（下游按 event_key 去重），因为投递保证的是「至少一次」。
CHANNELS: list[Callable[[dict, str], None]] = []


# ---------- 事务侧：入队 ----------

def user_audience(emp_id: str) -> str:
    return f"user:{emp_id}"


def role_audience(*roles: str) -> str:
    """角色受众。**投递时才展开成具体人** —— 在事务里展开会多一次 SELECT users
    加 N 次 INSERT，白占着礼品行锁；而且"当时有几个管理员"不该被历史订单钉死。"""
    return "role:" + ",".join(roles)


def enqueue(cur, *, event_key: str, audience: str, title: str, content: str,
            ntype: str = "system", ref_type: str = "", ref_id: Optional[int] = None) -> None:
    """在调用方事务里登记一条待投递通知。必须传 cur，不传就不是原子的了。

    event_key 由调用方给出业务语义键（`redeem:<订单号>`、`low_stock:<礼品>:<订单号>`
    这类），唯一约束保证同一业务事件重放、1213 死锁重试都只入队一次；
    ON DUPLICATE KEY 空更新让它安静地变成 no-op，而不是抛 1062 打断业务。
    """
    cur.execute(
        "INSERT INTO notification_outbox "
        "(event_key, audience, ntype, title, content, ref_type, ref_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE id = id",
        (event_key, audience, ntype, title[:255], content[:500], ref_type, ref_id))


# ---------- 投递侧：事务之外 ----------

def _recipients(audience: str) -> list[str]:
    kind, _, value = audience.partition(":")
    if kind == "user":
        return [value] if value else []
    if kind == "role":
        roles = tuple(r for r in value.split(",") if r)
        if not roles:
            return []
        return [r["emp_id"] for r in db.query("SELECT emp_id FROM users WHERE role IN %s", (roles,))]
    logger.warning("未知通知受众，已跳过 audience=%s", audience)
    return []


def _deliver_inbox(row: dict, emp_id: str) -> None:
    """站内信落库。uk_outbox(outbox_id, emp_id) 把重复投递变成空操作。"""
    db.execute(
        "INSERT IGNORE INTO notifications "
        "(emp_id, title, content, ntype, ref_id, outbox_id) VALUES (%s,%s,%s,%s,%s,%s)",
        (emp_id, row["title"], row["content"], row["ntype"], row["ref_id"], row["id"]))


def _deliver(row: dict) -> None:
    """一次投递尝试。抛异常即视为失败，交给 dispatch_once 记 attempts 并退避。"""
    recipients = _recipients(row["audience"])
    if not recipients:
        # 受众为空不是"失败"，重试也不会变出人来，记一条日志后按成功收尾。
        logger.warning("通知受众为空 outbox_id=%s audience=%s", row["id"], row["audience"])
        return
    for emp_id in recipients:
        _deliver_inbox(row, emp_id)
        for channel in CHANNELS:
            channel(row, emp_id)


def _backoff_seconds(attempts: int) -> float:
    return min(_BACKOFF_BASE * (2 ** max(0, attempts - 1)), _BACKOFF_MAX)


def dispatch_once() -> int:
    """投递一批到期通知，返回本轮处理条数。测试与一次性任务可直接调用。"""
    rows = db.query(
        "SELECT * FROM notification_outbox WHERE status = %s AND next_attempt_at <= NOW() "
        "ORDER BY id LIMIT %s", (PENDING, _BATCH))
    for row in rows:
        # 先落 attempts 再投递：即使投递过程中进程崩掉，这一轮也已被计数，
        # 重启后不会从 0 开始无限重试同一条。
        db.execute("UPDATE notification_outbox SET attempts = attempts + 1 WHERE id = %s",
                   (row["id"],))
        try:
            _deliver(row)
        except Exception as exc:
            attempts = int(row["attempts"]) + 1
            message = str(exc)[:250]
            if attempts >= _MAX_ATTEMPTS:
                logger.error("通知投递耗尽重试，转入死信 outbox_id=%s err=%s", row["id"], message)
                db.execute("UPDATE notification_outbox SET status = %s, last_error = %s WHERE id = %s",
                           (DEAD, message, row["id"]))
            else:
                # 加抖动：一批通知同时失败时不要齐步重试，否则下一轮又一起撞墙。
                delay = int(_backoff_seconds(attempts) * (1 + random.random() * 0.3))
                db.execute("UPDATE notification_outbox SET last_error = %s, "
                           "next_attempt_at = DATE_ADD(NOW(), INTERVAL %s SECOND) WHERE id = %s",
                           (message, delay, row["id"]))
        else:
            db.execute("UPDATE notification_outbox SET status = %s, sent_at = NOW(), "
                       "last_error = '' WHERE id = %s", (SENT, row["id"]))
    return len(rows)


def reap_sent() -> int:
    """回收已投递的历史行。dead 行保留 —— 那是给人工看的死信。"""
    return db.execute(
        "DELETE FROM notification_outbox WHERE status = %s "
        "AND sent_at < NOW() - INTERVAL %s DAY LIMIT %s", (SENT, _RETENTION_DAYS, _REAP_LIMIT))


# ---------- 后台线程 ----------

_stop = threading.Event()
_thread: Optional[threading.Thread] = None


def _run() -> None:
    cycles = 0
    while not _stop.is_set():
        _stop.wait(_POLL_INTERVAL)
        if _stop.is_set():
            break
        try:
            dispatch_once()
            cycles += 1
            if cycles % _REAP_EVERY == 0:
                reap_sent()
        except Exception:
            # 投递线程不能因为一次查询失败就退出，否则整条通知链路静默死掉。
            logger.exception("通知投递周期失败，下个周期重试")


def start_dispatcher() -> None:
    """启动投递线程。幂等。用线程而不是 asyncio task —— 它跑阻塞的 pymysql，
    放进事件循环会把整个 loop 卡住。"""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, name="notify-dispatcher", daemon=True)
    _thread.start()
    logger.info("通知投递线程已启动，间隔 %.1fs，最多重试 %d 次", _POLL_INTERVAL, _MAX_ATTEMPTS)


def stop_dispatcher() -> None:
    _stop.set()
    if _thread and _thread.is_alive():
        _thread.join(timeout=2)


def drain(max_rounds: int = 20) -> int:
    """同步排空：测试与一次性脚本用。退避中的行 next_attempt_at 未到，不会被拉出来。"""
    total = 0
    for _ in range(max_rounds):
        handled = dispatch_once()
        total += handled
        if handled == 0:
            break
    return total


def counts() -> dict:
    """各状态条数：投不出去必须能被看见。"""
    rows = db.query("SELECT status, COUNT(*) c FROM notification_outbox GROUP BY status")
    return {r["status"]: r["c"] for r in rows}


def dead_letters(limit: int = 50) -> list[dict]:
    return db.query("SELECT id, event_key, audience, ntype, title, ref_type, ref_id, attempts, "
                    "last_error, created_at, updated_at FROM notification_outbox "
                    "WHERE status = %s ORDER BY updated_at DESC LIMIT %s", (DEAD, limit))
