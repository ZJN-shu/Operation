"""积分 / 幂等 / 审计 / 埋点 等业务公共逻辑。"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any, Optional

import pymysql

from . import db

logger = logging.getLogger("portal.logic")

# 会写进 point_records 的人工 ref_type。约定：只有这三类的 ref_id 解释为 point_ops.id，
# 其余（welcome/course/activity/announcement/redeem）的 ref_id 是业务对象 id。
# 查询里 join point_ops 必须拿它做限定，否则 course 流水的 ref_id=3 会撞上
# point_ops.id=3，把不相干的管理员显示成「操作人」。
#
# 注意没有 reconcile：校平是把余额缓存按流水重算，不产生流水行，
# 所以它不会出现在 point_records 里（留痕在 point_ops / 审计日志）。
ADMIN_REF_TYPES = ("grant", "deduct", "revert")
NON_REVERTIBLE_REF_TYPES = (*ADMIN_REF_TYPES, "redeem", "refund")


def ensure_account(cur: pymysql.cursors.Cursor, emp_id: str) -> None:
    cur.execute(
        "INSERT IGNORE INTO point_accounts (emp_id, balance) VALUES (%s, 0)", (emp_id,)
    )


def get_balance(cur: pymysql.cursors.Cursor, emp_id: str) -> int:
    cur.execute("SELECT balance FROM point_accounts WHERE emp_id = %s", (emp_id,))
    row = cur.fetchone()
    return row["balance"] if row else 0


def add_points(
    cur: pymysql.cursors.Cursor,
    emp_id: str,
    points: int,
    note: str,
    ref_type: str,
    ref_id: int = 0,
) -> int:
    """在同一事务内：写积分流水 + 原子更新账户余额。返回 point_records.id。

    依赖 point_records(emp_id, ref_type, ref_id) 唯一约束保证幂等；
    重复调用会抛 IntegrityError，由调用方捕获。

    注意返回的是本函数捕获的值：不要在调用之后再读 cur.lastrowid ——
    下面那条 UPDATE 会把 pymysql 的 insert_id 覆盖成 0，读出来恒为 0。
    """
    ensure_account(cur, emp_id)
    cur.execute(
        "INSERT INTO point_records (emp_id, points, note, ref_type, ref_id) "
        "VALUES (%s, %s, %s, %s, %s)",
        (emp_id, points, note, ref_type, ref_id),
    )
    record_id = cur.lastrowid
    # points 可为正（获得）或负（消耗），统一 balance + points
    cur.execute(
        "UPDATE point_accounts SET balance = balance + %s WHERE emp_id = %s",
        (points, emp_id),
    )
    return record_id


def is_duplicate(exc: Exception) -> bool:
    return isinstance(exc, pymysql.err.IntegrityError) and exc.args[0] == 1062


def log_audit(emp_id: str, action: str, target_type: str = "", target_id: int | None = None,
              detail: dict | None = None, cur: pymysql.cursors.Cursor | None = None) -> None:
    """写入操作审计日志。

    传 cur 时走调用方的事务（同一连接）；不传时独立写（best-effort）。

    为什么要在事务里传 cur：不传的话 db.execute 会从连接池**再借一条**连接，
    池子只有 MYSQL_POOL_MAX（默认 20）条且 blocking=True —— 在事务里调它就是
    握着一条连接等第二条，并发一上来就死锁。而且它独立提交，回滚掉的业务操作
    照样会留下一条「已成功」的审计，事后对不上账。
    """
    sql = ("INSERT INTO audit_logs (emp_id, action, target_type, target_id, detail) "
           "VALUES (%s, %s, %s, %s, %s)")
    args = (emp_id, action, target_type, target_id,
            json.dumps(detail, ensure_ascii=False) if detail else None)
    if cur is not None:
        cur.execute(sql, args)
    else:
        db.execute(sql, args)


# ---------- 埋点：事件白名单 + 幂等键 ----------

# 事件类型白名单：没登记的事件一律拒收，避免脏事件流进看板污染口径。
ALLOWED_EVENTS = frozenset({
    "page_visit", "search", "course_view", "gift_view", "announcement_view",
    "activity_view", "points_view", "track_dropped",
})

# 服务端内联埋点的时间分桶（秒）：同一用户对同一目标在同一秒内的重复上报算同一次。
_BUCKET_SECONDS = 1


def make_event_id(emp_id: str, event_type: str, ref_type: str = "",
                  ref_id: int | None = None, bucket_seconds: int = _BUCKET_SECONDS) -> str:
    """服务端兜底幂等键：emp + 事件 + 目标 + 时间分桶。

    只解决「服务端内联埋点」的重发（同一次请求被重试、双击），不做业务去重——
    同一用户下一秒再看一次同一课程，仍算两次点击，热度口径不受影响。
    """
    bucket = int(time.time() // bucket_seconds)
    return f"srv:{emp_id}:{event_type}:{ref_type}:{ref_id or 0}:{bucket}"


def parse_client_time(value: Optional[str]) -> Optional[datetime]:
    """客户端事件时间：解析失败就丢成 NULL，绝不让脏时间进库。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


def log_event(emp_id: str, event_type: str, ref_type: str = "", ref_id: int | None = None,
              properties: dict | None = None, session_id: str = "",
              event_id: Optional[str] = None, client_time: Optional[str] = None) -> bool:
    """通用事件埋点：写入 user_access_logs。返回 True=新写入，False=重复或写入失败。

    幂等：event_id 上有唯一约束（uk_event），重复上报（客户端重试 / 用户双击 /
    网络重发）会被数据库直接拒掉，计数不会虚高。客户端自带 event_id 时优先用
    客户端的（重投才认得出是同一次）；服务端内联埋点用时间分桶生成兜底 ID。
    """
    if event_type not in ALLOWED_EVENTS:
        logger.warning("未登记的事件类型，已丢弃 event_type=%s", event_type)
        return False

    eid = event_id or make_event_id(emp_id, event_type, ref_type, ref_id)
    try:
        # ON DUPLICATE KEY UPDATE id=id：命中 uk_event 时是空更新，
        # rowcount 返回 0，据此判断这次是重复上报而不是新事件。
        rows = db.execute(
            "INSERT INTO user_access_logs "
            "(event_id, session_id, emp_id, event_type, ref_type, ref_id, properties, client_time) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
            "ON DUPLICATE KEY UPDATE id = id",
            (eid, session_id, emp_id, event_type, ref_type, ref_id,
             json.dumps(properties, ensure_ascii=False) if properties else None,
             parse_client_time(client_time)),
        )
        return rows == 1
    except Exception:
        # 埋点是旁路，写不进去也不能拖垮主流程。这里丢的只是「单条内联事件」的
        # 一次投递，客户端上报通道（/api/analytics/events）由本地队列保证重投。
        logger.exception("埋点写入失败 event_type=%s emp=%s", event_type, emp_id)
        return False


def log_event_for(user: dict, event_type: str, ref_type: str = "", ref_id: int | None = None,
                  properties: dict | None = None) -> bool:
    """路由里用：emp_id / session_id 直接从当前用户上下文取。"""
    return log_event(user["emp_id"], event_type, ref_type, ref_id, properties,
                     session_id=user.get("session_id", ""))


LOW_STOCK_THRESHOLD = 3


def notify(emp_id: str, title: str, content: str, ntype: str = "system",
           ref_id: int | None = None, cur=None) -> None:
    """站内信通知：发给单个用户。"""
    sql = ("INSERT INTO notifications (emp_id, title, content, ntype, ref_id) "
           "VALUES (%s, %s, %s, %s, %s)")
    args = (emp_id, title, content[:500], ntype, ref_id)
    if cur is not None:
        cur.execute(sql, args)
    else:
        db.execute(sql, args)


def notify_admins(title: str, content: str, ntype: str = "system", ref_id: int | None = None,
                  cur=None) -> None:
    """站内信通知：发给商城运营 + 超级管理员（处理礼品/库存相关角色）。"""
    sql = "SELECT emp_id FROM users WHERE role IN ('super_admin', 'shop_admin')"
    if cur is not None:
        cur.execute(sql)
        admins = cur.fetchall()
    else:
        admins = db.query(sql)
    for a in admins:
        notify(a["emp_id"], title, content, ntype, ref_id, cur=cur)
