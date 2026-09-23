"""管理端积分流水的实时推送通道（进程内扇出）。

两条设计决定，都是被具体问题逼出来的：

1. **不自己维护「待推送事件」缓冲区，而是轮询 point_records。**
   point_records 本身就是一张 transactional outbox —— 未提交的行在 InnoDB 隔离下
   对其他连接**根本不可见**。所以「只有提交成功才推送」这件事由数据库保证，而不是
   由我自己的记账代码保证：回滚的事务天然推不出去，且这个结论是关于 MySQL 可见性的
   真实性断言，不是关于我记账逻辑的断言（自己维护缓冲区的话，缓冲区漏清一次就会把
   回滚掉的分推给管理端 —— 比不推送更糟）。代价是 ≤400ms 延迟，换来的是
   db.run_tx / logic.add_points 这两个撑着全项目正确性的文件一行都不用动。

2. **轮询线程而不是 asyncio task。** 它要跑阻塞的 pymysql；放进事件循环会把
   整个 loop 卡住，所有请求（包括每条 SSE 流）跟着一起卡。

已知边界（被问到要主动说）：订阅者注册表在进程内存里，只能触达**同一个 uvicorn
worker** 上的客户端。多 worker / 多实例要换成 Redis pub/sub 这类跨进程通道，
和 auth._SESSIONS 是同一类限制。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from typing import Any, Optional

from . import db, logic

logger = logging.getLogger("portal.events")

# 每个连接一个队列，队列长度封顶：客户端连上了却不读（页面卡死、断网但没发 FIN）
# 时不能让它无限涨。满了就丢一条并补一个 dropped 标记，让前端重载当前页 ——
# 沿用埋点队列那条原则：丢数必须能被观测到，不能静默消失。
_QUEUE_SIZE = 100

_POLL_INTERVAL = 0.4      # 秒。人的感知上仍是实时，换来零改动核心事务代码
# 每周期重扫的 id 尾部窗口。为什么需要它：**提交顺序不等于 id 顺序** ——
# 事务 A 拿 101、B 拿 102，B 先提交；此刻读到的最大 id 是 102，水位推到 102，
# 等 101 提交时它已经在水位之下了，**永远收不到**。所以每周期回退 50 个 id 重扫。
# 边界：如果某个长事务攥着 id 迟迟不提交，期间提交并扫过的 id 超过 50 个，
# 它仍会被漏掉（400ms 一轮 × 50 的余量）。要彻底解决得用 binlog / CDC。
_TRAILING_WINDOW = 50
_BATCH = 200              # 单周期最多取多少行
_DEDUP_SIZE = 500         # 去重环长度，必须 >= 尾窗，否则重扫会把自己去重掉

_subscribers: set["asyncio.Queue[dict]"] = set()
_loop: Optional[asyncio.AbstractEventLoop] = None
_published: deque[int] = deque(maxlen=_DEDUP_SIZE)

_stop = threading.Event()
_thread: Optional[threading.Thread] = None

_SELECT = (
    "SELECT pr.id, pr.emp_id, u.username, u.name AS user_name, pr.points, pr.note, "
    "pr.ref_type, pr.ref_id, pr.created_at, "
    "po.op_type, po.operator_emp_id, ou.name AS operator_name "
    "FROM point_records pr "
    "LEFT JOIN users u ON u.emp_id = pr.emp_id "
    # 这个 ref_type 限定不能省：ref_id 只在 ref_type 语境里有意义，
    # course 流水的 ref_id=3 会撞上 point_ops.id=3，把不相干的管理员显示成「操作人」。
    "LEFT JOIN point_ops po ON po.id = pr.ref_id AND pr.ref_type IN %s "
    "LEFT JOIN users ou ON ou.emp_id = po.operator_emp_id "
    "WHERE pr.id > %s ORDER BY pr.id ASC LIMIT %s"
)


def bind_loop(loop: asyncio.AbstractEventLoop) -> None:
    """在 lifespan 里绑定事件循环，幂等。轮询线程要靠它把数据投回循环。"""
    global _loop
    _loop = loop


def subscribe() -> "asyncio.Queue[dict]":
    q: asyncio.Queue[dict] = asyncio.Queue(maxsize=_QUEUE_SIZE)
    _subscribers.add(q)
    return q


def unsubscribe(q: "asyncio.Queue[dict]") -> None:
    """必须可重入：SSE 生成器的 finally 可能跑两次（正常结束 + 取消）。"""
    _subscribers.discard(q)


def subscriber_count() -> int:
    return len(_subscribers)


def _deliver(q: "asyncio.Queue[dict]", payload: dict) -> None:
    """往单个队列投递。只在事件循环线程里调用。"""
    try:
        q.put_nowait(payload)
    except asyncio.QueueFull:
        # 丢最旧的一条，换一个 dropped 标记进去，让客户端知道「中间缺了，重载」。
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            q.put_nowait({"type": "dropped"})
        except asyncio.QueueFull:
            pass


def _publish(payload: dict) -> None:
    """扇出给所有订阅者。可能是在轮询线程里被调用。"""
    for q in list(_subscribers):
        try:
            _deliver(q, payload)
        except Exception:
            logger.exception("推送失败，已跳过该订阅者")


def _row_payload(row: dict) -> dict:
    created = row.get("created_at")
    return {
        "type": "point",
        "id": row["id"],
        "emp_id": row["emp_id"],
        "username": row.get("username") or "",
        "user_name": row.get("user_name") or "",
        "points": row["points"],
        "note": row.get("note") or "",
        "ref_type": row["ref_type"],
        "ref_id": row["ref_id"],
        # 直接格式化成字符串：datetime 不能进 JSON
        "created_at": created.strftime("%Y-%m-%d %H:%M:%S") if created else "",
        "op_type": row.get("op_type"),
        "operator_emp_id": row.get("operator_emp_id") or "",
        "operator_name": row.get("operator_name") or "",
        # 与 list_points 同一个判断，前端拿它决定要不要渲染「回滚」按钮
        "revertible": row["ref_type"] not in logic.NON_REVERTIBLE_REF_TYPES,
    }


def _emit(payload: dict) -> None:
    """把 payload 投到事件循环。失败只记日志 —— 绝不把异常抛进业务流程。"""
    if _loop is None:
        logger.debug("事件循环未绑定，丢弃一条推送")
        return
    try:
        _loop.call_soon_threadsafe(_publish, payload)
    except RuntimeError:
        # 关停过程中 loop 已经关了，属正常路径
        logger.debug("事件循环已关闭，丢弃一条推送")


def _poll_once(watermark: int) -> int:
    """扫一次增量，返回新的水位。"""
    rows = db.query(_SELECT, (logic.ADMIN_REF_TYPES, watermark - _TRAILING_WINDOW, _BATCH))
    if not rows:
        return watermark
    newest = watermark
    for row in rows:
        rid = row["id"]
        if rid > newest:
            newest = rid
        if rid in _published:
            continue
        _published.append(rid)
        _emit(_row_payload(row))
    return newest


def _mark_window_published(watermark: int) -> None:
    """把水位附近这一窗 id 标记成「已推送」。

    启动时和「无订阅者」期间必须调：这两个时刻**不该有任何推送**，
    但尾窗里的 id 会被后续扫描重新读到 —— 不预先标记，第二次有人订阅时
    就会把几十条历史流水当成新流水推出去。
    """
    rows = db.query("SELECT id FROM point_records WHERE id > %s", (watermark - _TRAILING_WINDOW,))
    for r in rows:
        if r["id"] not in _published:
            _published.append(r["id"])


def _run() -> None:
    watermark = 0
    # 启动水位对齐到当前最大 id，并把尾窗标记掉：
    # 否则第一个连上来的客户端会被灌进一批历史流水。
    try:
        row = db.query_one("SELECT COALESCE(MAX(id), 0) AS m FROM point_records")
        watermark = int(row["m"]) if row else 0
        _mark_window_published(watermark)
    except Exception:
        logger.exception("初始化积分流水水位失败，从 0 开始（首轮会全量推送一遍）")

    while not _stop.is_set():
        _stop.wait(_POLL_INTERVAL)
        if _stop.is_set():
            break
        if not _subscribers:
            # 没人订阅就不查流水。但水位不能停着不动，否则订阅者一来就会收到
            # 这段空窗期的全部历史 —— 重新对齐并标记一次。
            try:
                row = db.query_one("SELECT COALESCE(MAX(id), 0) AS m FROM point_records")
                if row:
                    watermark = max(watermark, int(row["m"]))
                    _mark_window_published(watermark)
            except Exception:
                logger.exception("对齐水位失败")
            continue
        try:
            watermark = _poll_once(watermark)
        except Exception:
            # 轮询线程不能因为一次查询失败就退出，否则整个实时通道静默死掉。
            logger.exception("积分流水轮询失败，下个周期重试")


def start_poller() -> None:
    """启动轮询线程。幂等。"""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, name="points-poller", daemon=True)
    _thread.start()
    logger.info("积分流水轮询已启动，间隔 %.1fs", _POLL_INTERVAL)


def stop_poller() -> None:
    _stop.set()
    if _thread and _thread.is_alive():
        _thread.join(timeout=2)
