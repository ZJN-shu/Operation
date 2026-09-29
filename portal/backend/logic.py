"""积分 / 幂等 / 审计 / 埋点 等业务公共逻辑。"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any, Optional

import pymysql

from . import config, db, mq

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
    """写入操作审计日志（进阶：链式哈希，把「留痕」升级成「可验篡改」）。

    传 cur 时走调用方的事务（同一连接）；不传时独立成一个小事务（best-effort）。

    为什么要在事务里传 cur：不传的话 db.execute 会从连接池**再借一条**连接，
    池子只有 MYSQL_POOL_MAX（默认 20）条且 blocking=True —— 在事务里调它就是
    握着一条连接等第二条，并发一上来就死锁。而且它独立提交，回滚掉的业务操作
    照样会留下一条「已成功」的审计，事后对不上账。

    哈希链规则（与 db._backfill_audit_chain / verify_audit_chain 共用
    db._audit_digest，三处口径必须一致）：锁本行所属分链的 audit_chain 锚点 → 取链尾 →
    row_hash = SHA256(prev_hash | 本行字段) → 追加并推尾。锁锚点而不是 SELECT
    最后一行 FOR UPDATE：表空时后者无行可锁，间隙锁会和业务写入对撞。

    锚点分片：链不再是一条，而是 AUDIT_CHAIN_SHARDS 条独立链，按 emp_id 稳定哈希
    归链。不同用户走不同锚点行，提交尾不再全局互斥；同一用户恒定同链，链内仍严格
    可验。代价是「整段截尾」防护变成按链维度（每链各自一条），语义不变。
    """
    detail_json = json.dumps(detail, ensure_ascii=False) if detail else None
    shard = db.stable_slot(emp_id, config.AUDIT_CHAIN_SHARDS)

    def fn(c):
        c.execute("SELECT last_hash, seq FROM audit_chain WHERE id = %s FOR UPDATE", (shard,))
        tail = c.fetchone()
        prev = tail["last_hash"] if tail else ""
        seq = (tail["seq"] + 1) if tail else 1
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = (str(emp_id), str(action), str(target_type), str(target_id if target_id is not None else -1),
               str(detail_json if detail_json is not None else ""), str(created_at))
        row_hash = db._audit_digest(prev, row)
        c.execute(
            "INSERT INTO audit_logs (emp_id, action, target_type, target_id, detail, "
            "prev_hash, row_hash, chain_id, created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (emp_id, action, target_type, target_id, detail_json, prev, row_hash, shard, created_at))
        c.execute(
            "INSERT INTO audit_chain (id, last_hash, seq) VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE last_hash = VALUES(last_hash), seq = VALUES(seq)",
            (shard, row_hash, seq))

    if cur is not None:
        fn(cur)
    else:
        try:
            db.run_tx(fn)
        except Exception:
            logger.warning("审计写入失败 action=%s emp_id=%s", action, emp_id, exc_info=True)


def verify_audit_chain(limit: int = 0) -> dict:
    """验审计哈希链：从头扫到尾，返回 {rows, hashed_rows, baseline_rows, ok, broken_at}。

    能检出：改/删任意 v2 行的内容（重算哈希对不上）、断链（prev_hash 不指向前行）。
    检不出：整段截尾（没有外部锚点，需定期把 last_hash 归档到库外才能防），
    以及回填前就已被篡改的历史行（回填时按当时内容重算，天然自洽）。
    边界随结果外露，不宣称全量防篡改。

    链已按 emp_id 分片：按 (chain_id, id) 逐链独立验，prev 在链首重置为空串，
    跨链互不影响。limit>0 只验最近 N 行（看板周期校验）：窗口可能从某链中段切入，
    该链首行（窗口内）的前驱在窗口外，不校 prev_hash 链接、但仍用行内存的 prev_hash
    校内容摘要（改内容照样能检出）；链内后续行仍严格校 prev 指向。
    """
    sql = ("SELECT id, emp_id, action, target_type, IFNULL(target_id, -1) AS tid, "
           "IFNULL(detail, '') AS d, prev_hash, row_hash, created_at, chain_id "
           "FROM audit_logs")
    if limit:
        rows = db.query(sql + " ORDER BY id DESC LIMIT %s", (limit,)) or []
        rows.reverse()
        # 窗口可能截断某条链：按链分组后仅能验窗口内的相对链接，故按 (chain_id, id) 重排。
        rows.sort(key=lambda r: (r["chain_id"], r["id"]))
    else:
        rows = db.query(sql + " ORDER BY chain_id, id") or []
    ok, broken_at, hashed, baseline = True, None, 0, 0
    prev_by_chain: dict[int, str] = {}
    seen_chain: set[int] = set()
    for r in rows:
        cid = r["chain_id"]
        if not r["row_hash"]:   # 回填漏网的新格式行？正常不会出现，保守计为基线
            baseline += 1
            prev_by_chain[cid] = ""
            seen_chain.add(cid)
            continue
        field = (str(r["emp_id"]), str(r["action"]), str(r["target_type"]),
                 str(r["tid"]), str(r["d"]), str(r["created_at"]))
        first_in_window = bool(limit) and cid not in seen_chain
        seen_chain.add(cid)
        prev = prev_by_chain.get(cid, "")
        # 窗口切入的链首行：前驱在窗口外，不校链接，但仍用行内存 prev_hash 校内容。
        check_prev = prev if not first_in_window else r["prev_hash"]
        if r["prev_hash"] != check_prev or db._audit_digest(check_prev, field) != r["row_hash"]:
            if ok:
                ok = False
                broken_at = {"id": r["id"], "action": r["action"], "emp_id": r["emp_id"]}
        hashed += 1
        prev_by_chain[cid] = r["row_hash"]
    return {"ok": ok, "broken_at": broken_at, "rows": len(rows),
            "hashed_rows": hashed, "baseline_rows": baseline,
            "note": "可检内容篡改与断链；不可检整段截尾（需库外锚点）与上链前的历史改动"}


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


def _write_event(cur, eid, session_id, emp_id, event_type, ref_type, ref_id,
                 properties, client_time) -> bool:
    """把一条埋点写进给定事务游标。ON DUPLICATE KEY UPDATE id=id：命中 uk_event 时空更新、
    rowcount=0（重复上报），新事件 rowcount=1。消费者批量 flush 多条共用一个提交时调它。"""
    cur.execute(
        "INSERT INTO user_access_logs "
        "(event_id, session_id, emp_id, event_type, ref_type, ref_id, properties, client_time) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE id = id",
        (eid, session_id, emp_id, event_type, ref_type, ref_id,
         json.dumps(properties, ensure_ascii=False) if properties else None,
         parse_client_time(client_time)))
    return cur.rowcount == 1


def log_event(emp_id: str, event_type: str, ref_type: str = "", ref_id: int | None = None,
              properties: dict | None = None, session_id: str = "",
              event_id: Optional[str] = None, client_time: Optional[str] = None,
              cur: Optional[Any] = None) -> bool:
    """通用事件埋点：写入 user_access_logs。返回 True=新写入，False=重复或写入失败。

    幂等：event_id 上有唯一约束（uk_event），重复上报（客户端重试 / 用户双击 /
    网络重发）会被数据库直接拒掉，计数不会虚高。客户端自带 event_id 时优先用
    客户端的（重投才认得出是同一次）；服务端内联埋点用时间分桶生成兜底 ID。

    传 cur 时写进调用方事务（消费者批量 flush 共用一个提交）；不传且启用 MQ 则出流。
    """
    if event_type not in ALLOWED_EVENTS:
        logger.warning("未登记的事件类型，已丢弃 event_type=%s", event_type)
        return False

    eid = event_id or make_event_id(emp_id, event_type, ref_type, ref_id)
    if cur is not None:   # 消费者侧：写进共享事务，不再入流
        return _write_event(cur, eid, session_id, emp_id, event_type, ref_type, ref_id,
                            properties, client_time)
    # 演进 C：启用 MQ 时埋点出流异步落库（旁路，不占读请求同步时延）。
    # 幂等仍由消费侧的 uk_event 保证；入流失败即当作一次旁路丢弃，不阻断主流程。
    if mq.enabled():
        try:
            mq.publish("event", emp_id=emp_id, event_type=event_type, ref_type=ref_type,
                       ref_id=ref_id or 0, properties=properties, session_id=session_id,
                       event_id=eid, client_time=client_time or "")
            return True
        except Exception:
            logger.exception("埋点入流失败 event_type=%s emp=%s", event_type, emp_id)
            return False
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

# 通知相关的职责已迁到 notifier.py：事务内只写发件箱，投递在事务外异步完成。
# 这里保留阈值常量是因为它是业务规则（库存跨过 3 才告警一次），不是投递细节。
