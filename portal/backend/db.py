"""数据库访问层：连接 MySQL，启动时幂等建库建表，提供查询/执行/事务助手。

全部使用 pymysql 参数化（%s 占位符），杜绝 SQL 注入。
"""
from __future__ import annotations

import hashlib
import logging
import os
import random
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import pymysql
import pymysql.cursors
from dbutils.pooled_db import PooledDB

from . import config

logger = logging.getLogger("portal.db")

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

_pool: PooledDB | None = None
_pool_lock = threading.RLock()

# 事务与连接池可观测（进阶）：把「重试吸收了多少次锁冲突、池子借出去多少条」
# 从日志里的散文变成可拉取的计数器。只记累计值与峰值，不存样本，开销 O(1)。
_tx_stats = {
    "tx_total": 0,            # run_tx 调用总次数（含重试前的口径）
    "retry_attempts": 0,      # 实际发生过的重试次数合计
    "retry_absorbed": 0,      # 重试后最终成功的次数（一次事务可能重试多轮计 1）
    "retry_exhausted": 0,     # 重试用尽仍抛锁冲突的次数（这类才是重试机制木够不着的）
    "by_code": {},            # 按错误码分布：1213 / 1205
    "pool_in_use_peak": 0,    # 借出连接数历史峰值
    "pool_wait_peak": 0,      # （近似）借出数触顶 maxconnections 的命中次数
}
_stats_lock = threading.Lock()

# InnoDB 并发下可自愈的瞬态锁冲突：1213 死锁（引擎已回滚整个事务）、
# 1205 锁等待超时。只有这两个码会被 run_tx 的有界重试吞掉；断连（20xx）、
# 主键冲突（1062）、权限（1142）等都不在此列，依旧原样抛出。
_RETRYABLE_MYSQL_ERRORS = (1213, 1205)


def assert_test_database() -> None:
    """在任何测试 SQL 之前拒绝应用库、管理员数据库账号与未显式启用的连接。"""
    if not config.TESTING or os.getenv("PORTAL_RUN_DB_TESTS") != "1":
        raise RuntimeError("数据库测试未启用，请使用隔离 MySQL 测试入口")
    if not re.fullmatch(r"ops_portal_test_[a-z0-9_]{8,40}", config.MYSQL_DB):
        raise RuntimeError("拒绝连接非隔离测试库")
    if config.MYSQL_USER != "portal_test":
        raise RuntimeError("测试只允许使用 portal_test 专用账号")


def _connection_guard() -> None:
    if os.getenv("PORTAL_TESTING") == "1" or config.TESTING:
        assert_test_database()
    if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", config.MYSQL_DB):
        raise RuntimeError("数据库名称不合法")


def _get_pool() -> PooledDB:
    """懒加载连接池（单例），复用连接避免每次请求都重新握手。"""
    global _pool
    _connection_guard()
    with _pool_lock:
        if _pool is not None:
            return _pool
        _pool = PooledDB(
            creator=pymysql,
            maxconnections=config.MYSQL_POOL_MAX,
            mincached=config.MYSQL_POOL_MIN_CACHED,
            maxcached=config.MYSQL_POOL_MAX_CACHED,
            blocking=True,   # 池满时等待而非报错
            ping=1,          # 取出前 ping，自动重连被 wait_timeout 断开的连接
            host=config.MYSQL_HOST,
            port=config.MYSQL_PORT,
            user=config.MYSQL_USER,
            password=config.MYSQL_PASSWORD,
            database=config.MYSQL_DB,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
            connect_timeout=config.MYSQL_CONNECT_TIMEOUT,
            read_timeout=15,
            write_timeout=15,
        )
    return _pool


def _connect(with_db: bool = True):
    _connection_guard()
    if with_db:
        return _get_pool().connection()
    # 建库阶段：不指定库名直接连（不走池，因为池要求库已存在）
    return pymysql.connect(
        host=config.MYSQL_HOST,
        port=config.MYSQL_PORT,
        user=config.MYSQL_USER,
        password=config.MYSQL_PASSWORD,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
        connect_timeout=config.MYSQL_CONNECT_TIMEOUT,
        read_timeout=15,
        write_timeout=15,
    )


def init_db() -> None:
    """启动时调用：建库（若不存在）+ 建表（幂等）。"""
    # 1) 不指定库名连接，建库
    conn = _connect(with_db=False)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE DATABASE IF NOT EXISTS `%s` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                % config.MYSQL_DB
            )
        conn.commit()
    finally:
        conn.close()

    # 2) 连接目标库，按分号拆分 schema.sql 逐条建表
    schema = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn = _connect(with_db=True)
    try:
        with conn.cursor() as cur:
            for stmt in _split_statements(schema):
                cur.execute(stmt)
            _migrate(cur)
        conn.commit()
    finally:
        conn.close()


# 幂等迁移：老库的 user_access_logs 没有 event_id / session_id，需要补列。
# MySQL 8 没有 ADD COLUMN IF NOT EXISTS，所以靠忽略「列/索引已存在」错误达成幂等。
# 顺序敏感：先加列 → 回填历史行 → 再加唯一键。反过来历史行的空 event_id 会互相冲突。
_MIGRATIONS: tuple[str, ...] = (
    "ALTER TABLE user_access_logs ADD COLUMN event_id VARCHAR(96) NOT NULL DEFAULT '' AFTER id",
    "ALTER TABLE user_access_logs ADD COLUMN session_id VARCHAR(64) DEFAULT '' AFTER event_id",
    "ALTER TABLE user_access_logs ADD COLUMN client_time DATETIME DEFAULT NULL AFTER properties",
    "UPDATE user_access_logs SET event_id = CONCAT('legacy:', id) WHERE event_id = ''",
    "ALTER TABLE user_access_logs ADD UNIQUE KEY uk_event (event_id)",
    "ALTER TABLE user_access_logs ADD KEY idx_session (session_id)",
    "ALTER TABLE redemptions ADD COLUMN request_id VARBINARY(64) DEFAULT NULL",
    "ALTER TABLE redemptions ADD COLUMN response_json TEXT",
    "ALTER TABLE redemptions ADD COLUMN integrity_version TINYINT NOT NULL DEFAULT 0",
    "ALTER TABLE redemptions ALTER COLUMN integrity_version SET DEFAULT 1",
    "ALTER TABLE redemptions ADD COLUMN refunded_at DATETIME DEFAULT NULL",
    "ALTER TABLE redemptions ADD COLUMN refund_reason VARCHAR(100) DEFAULT ''",
    "ALTER TABLE redemptions ADD COLUMN refund_operator VARCHAR(32) DEFAULT ''",
    "ALTER TABLE redemptions ADD UNIQUE KEY uk_redeem_request (emp_id, request_id)",
    # 会话归因：老订单/老进度补列即可，默认空串代表「无从归因」，口径里一律排除，
    # 不需要回填 —— 空串若被当成同一个会话，会把漏斗聚成一个超级会话算错。
    "ALTER TABLE redemptions ADD COLUMN session_id VARCHAR(64) DEFAULT '' AFTER emp_id",
    "ALTER TABLE training_progress ADD COLUMN session_id VARCHAR(64) DEFAULT '' AFTER emp_id",
    "ALTER TABLE redemptions ADD KEY idx_session (session_id)",
    "ALTER TABLE training_progress ADD KEY idx_session (session_id)",
    # 通知改走发件箱后，投递结果要能反查来源并保证重复投递不落库。
    # notification_outbox 本身由 schema.sql 的 CREATE IF NOT EXISTS 建，老库启动即补齐。
    "ALTER TABLE notifications ADD COLUMN outbox_id INT DEFAULT NULL AFTER ref_id",
    "ALTER TABLE notifications ADD UNIQUE KEY uk_outbox (outbox_id, emp_id)",
    # 审计哈希链（进阶）：补列后由 _backfill_audit_chain 逐行回填历史行哈希，
    # 链从第一行审计就算得起；新行在 logic.log_audit 里接着链尾追加。
    # 锚点表也在迁移里兼容一份：老库在 schema.sql 不重跑新建语句的极端情况下也能补齐。
    "CREATE TABLE IF NOT EXISTS audit_chain (id TINYINT NOT NULL, last_hash VARCHAR(64) NOT NULL DEFAULT '', seq INT NOT NULL DEFAULT 0, PRIMARY KEY (id)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4",
    # 预置单行锚点：log_audit 靠 SELECT ... FOR UPDATE 锁这一行串行化追加。若不预插，
    # 表空时第一个并发事务读不到锁行会各自 prev='' 开链 → 链分叉。INSERT IGNORE 幂等。
    "INSERT IGNORE INTO audit_chain (id, last_hash, seq) VALUES (1, '', 0)",
    "ALTER TABLE audit_logs ADD COLUMN prev_hash VARCHAR(64) NOT NULL DEFAULT '' AFTER detail",
    "ALTER TABLE audit_logs ADD COLUMN row_hash VARCHAR(64) NOT NULL DEFAULT '' AFTER prev_hash",
    # 图片直传 OSS（真实落地）：存受控 object key，非签名 URL；老库补列默认空串代表未接图。
    "ALTER TABLE gifts ADD COLUMN image_key VARCHAR(255) DEFAULT '' AFTER icon",
)

# 1060 = Duplicate column name，1061 = Duplicate key name
_IGNORED_DDL_ERRORS = {1060, 1061}


def _migrate(cur) -> None:
    """把老库结构对齐到 schema.sql 当前版本（幂等，启动时跑）。"""
    for stmt in _MIGRATIONS:
        try:
            cur.execute(stmt)
        except pymysql.err.OperationalError as exc:
            if exc.args[0] in _IGNORED_DDL_ERRORS:
                continue
            raise
    # 审计锚点分片：chain_id 列若本次才补上，说明是从单链老库迁移过来，
    # 需要一次性把历史行按分链重算哈希（老单链跨行 prev 与新分链不兼容）。
    reshard = _ensure_audit_shards(cur)
    _backfill_audit_chain(cur, full=reshard)
    _seed_stock_buckets(cur)


def split_stock(total: int, k: int) -> list[tuple[int, int]]:
    """把一个总库存平摊到 k 个桶：尽量均匀，余数进 0 号桶。

    不一次性堆在 0 号桶：否则用户哈希到 1..K-1 的桶都空，兑换总要轮转到 0 号桶，
    串行回到单行，分桶白做。平摊后并发兑换能真正并行到不同桶行。
    """
    if k <= 1:
        return [(0, total)]
    base, rem = divmod(total, k)
    return [(b, base + (rem if b == 0 else 0)) for b in range(k)]


def seed_gift_buckets(cur, gid: int, total: int, k: int | None = None) -> None:
    """为礼品初始化 K 个库存桶（幂等：INSERT IGNORE）。已存在的桶不重分、不叠加。

    并发首次触碰时两个事务都看到空桶、都用同一 total 跑本函数：第二个的
    INSERT IGNORE 全被忽略，不会重复插量 —— 库存不会被分桶本身翻倍。
    """
    k = k or config.STOCK_BUCKETS
    for b, amt in split_stock(total, k):
        cur.execute(
            "INSERT IGNORE INTO gift_stock_bucket (gift_id, bucket_no, stock) VALUES (%s,%s,%s)",
            (gid, b, amt))


def _seed_stock_buckets(cur) -> None:
    """启动时给还没建桶的存量礼品按 gifts.stock 快照分桶（老库升级的一次性补齐）。"""
    cur.execute(
        "SELECT g.id, g.stock FROM gifts g "
        "WHERE NOT EXISTS (SELECT 1 FROM gift_stock_bucket b WHERE b.gift_id = g.id)")
    for r in cur.fetchall() or []:
        seed_gift_buckets(cur, r["id"], r["stock"])


def stable_slot(key: str, n: int) -> int:
    """把字符串稳定映射到 0..n-1 的分片号（跨进程一致）。

    绝不能用内建 hash()：CPython 对 str 的 hash 带 PYTHONHASHSEED 随机盐，
    多进程 / 重启后同一 emp_id 会漂到不同分片，审计链直接分叉、库存桶归属也会乱。
    用 sha256 取前 8 hex 转整数再取模，确定性且不依赖运行时盐。
    """
    if n <= 1:
        return 0
    digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % n


def _audit_digest(prev_hash: str, row: tuple) -> str:
    """链式哈希的规范序列化：参入字段顺序固定，分隔符用 \x00 避免字段内容仿冒接缝。

    建链（回填）与验链（verify）必须共用本函数，否则口径漂移会把正常链判成篡改。
    """
    payload = "\x00".join([prev_hash, *row]).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _ensure_audit_shards(cur) -> bool:
    """补 chain_id 列 + 播 N 条分链锚点行。返回本次是否刚刚新增列（需全量重算历史）。"""
    just_added = False
    try:
        cur.execute("ALTER TABLE audit_logs ADD COLUMN chain_id TINYINT NOT NULL DEFAULT 0 AFTER row_hash")
        just_added = True
    except pymysql.err.OperationalError as exc:
        if exc.args[0] != 1060:
            raise
    try:
        cur.execute("ALTER TABLE audit_logs ADD KEY idx_chain (chain_id, id)")
    except pymysql.err.OperationalError as exc:
        if exc.args[0] != 1061:
            raise
    for i in range(config.AUDIT_CHAIN_SHARDS):
        cur.execute("INSERT IGNORE INTO audit_chain (id, last_hash, seq) VALUES (%s, '', 0)", (i,))
    return just_added


def _backfill_audit_chain(cur, full: bool = False) -> None:
    """按分链给还没有哈希的审计行补算 row_hash（幂等，启动时跑）。

    每条分链各自从 audit_chain[id] 的链尾往后接：同一分链内严格串接、跨分链互不相关。
    full=True 时是老→新分片的一次性迁移：先把所有行 prev/row_hash 清空、按 emp_id 重分链、
    重置所有锚点，再逐链重建（老单链的跨行 prev 与新分链不兼容，必须整体重算）。
    full=False（常规启动）只接 row_hash='' 的新行，绝不重算已有哈希 ——
    否则重启会把入库后发生的篡改“抹平”成自洽，丢防篡改能力。
    """
    n = config.AUDIT_CHAIN_SHARDS
    if full:
        cur.execute("SELECT id, emp_id FROM audit_logs")
        for r in cur.fetchall() or []:
            cur.execute("UPDATE audit_logs SET prev_hash='', row_hash='', chain_id=%s WHERE id=%s",
                        (stable_slot(r["emp_id"], n), r["id"]))
        cur.execute("UPDATE audit_chain SET last_hash='', seq=0")
    for shard in range(n):
        cur.execute(
            "SELECT id, emp_id, action, target_type, IFNULL(target_id, -1) AS tid, "
            "IFNULL(detail, '') AS d, created_at FROM audit_logs "
            "WHERE chain_id = %s AND row_hash = '' ORDER BY id", (shard,))
        rows = cur.fetchall() or []
        if not rows:
            continue
        cur.execute("SELECT last_hash, seq FROM audit_chain WHERE id = %s FOR UPDATE", (shard,))
        tail = cur.fetchone()
        prev = tail["last_hash"] if tail else ""
        seq = (tail["seq"] if tail else 0)
        last = None
        for r in rows:
            fields = (str(r["emp_id"]), str(r["action"]), str(r["target_type"]),
                      str(r["tid"]), str(r["d"]), str(r["created_at"]))
            last = _audit_digest(prev, fields)
            cur.execute("UPDATE audit_logs SET prev_hash = %s, row_hash = %s WHERE id = %s",
                        (prev, last, r["id"]))
            prev = last
            seq += 1
        cur.execute(
            "INSERT INTO audit_chain (id, last_hash, seq) VALUES (%s, %s, %s) "
            "ON DUPLICATE KEY UPDATE last_hash = VALUES(last_hash), seq = VALUES(seq)",
            (shard, last, seq))


def _split_statements(sql: str) -> list[str]:
    """先去掉整行注释，再按分号拆分（DDL 里没有字符串字面量分号）。"""
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    cleaned = "\n".join(lines)
    return [p.strip() for p in cleaned.split(";") if p.strip()]


def query(sql: str, args: Optional[tuple] = None) -> list[dict]:
    """查询多行，返回 list[dict]。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            return cur.fetchall()
    finally:
        conn.rollback()   # SELECT 无需提交；回滚清空事务状态，避免污染连接池
        conn.close()


def query_one(sql: str, args: Optional[tuple] = None) -> Optional[dict]:
    rows = query(sql, args)
    return rows[0] if rows else None


def execute(sql: str, args: Optional[tuple] = None) -> int:
    """执行单条写语句，返回影响行数（rowcount）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            conn.commit()
            return cur.rowcount
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def insert(sql: str, args: Optional[tuple] = None) -> int:
    """执行 INSERT，返回自增主键 lastrowid。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            conn.commit()
            return cur.lastrowid
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def run_tx(fn: Callable[[pymysql.cursors.Cursor], Any], *, read_snapshot: bool = False,
           retries: int = 0, retry_base_delay: float = 0.02,
           retry_max_delay: float = 0.2) -> Any:
    """在一个事务里执行 fn(cur)；异常整体回滚并抛出。

    retries>0 时，对 InnoDB 的**瞬态锁冲突**做有界重试：死锁（1213，InnoDB 已自己
    回滚了整个事务）与锁等待超时（1205）。这两类不是业务失败，而是“换个时机重跑
    一遍就能成”的并发碰撞，生产惯例就是有限次重试。

    重试的前提是 fn 可重入：每次重试都在一条**全新连接**上开**全新事务**（见 _run_once
    里的显式 begin），要求重跑不会二次生效。本项目的写事务正是靠“数据库唯一约束 +
    事务体开头的幂等重放读”兜住这一点，而不是靠内存标记。

    业务用的 HTTPException、语法/约束错误、断连（20xx）等一律不重试，原样抛出。

    重试过程同步计入模块级计数器（见 stats()）：面试/运维问「重试到底有没有发生、
    池子到底多紧张」时拿数字而不是拿印象。
    """
    attempt = 0
    with _stats_lock:
        _tx_stats["tx_total"] += 1
    while True:
        try:
            result = _run_once(fn, read_snapshot=read_snapshot)
            if attempt:
                with _stats_lock:
                    _tx_stats["retry_absorbed"] += 1
            return result
        except pymysql.err.OperationalError as exc:
            code = exc.args[0] if exc.args else None
            if code in _RETRYABLE_MYSQL_ERRORS:
                with _stats_lock:
                    _tx_stats["by_code"][str(code)] = _tx_stats["by_code"].get(str(code), 0) + 1
            if attempt >= retries or code not in _RETRYABLE_MYSQL_ERRORS:
                if code in _RETRYABLE_MYSQL_ERRORS and attempt >= retries:
                    with _stats_lock:
                        _tx_stats["retry_exhausted"] += 1
                raise
            attempt += 1
            with _stats_lock:
                _tx_stats["retry_attempts"] += 1
            logger.warning("事务撞 InnoDB 瞬态锁冲突(code=%s)，第 %d/%d 次重试",
                           code, attempt, retries)
            delay = min(retry_base_delay * (2 ** (attempt - 1)), retry_max_delay)
            # 加随机抖动，避免多个受害者同一时刻齐步重试再次对撞。
            time.sleep(delay + random.uniform(0, delay))


def stats() -> dict:
    """事务/连接池指标快照：累计计数器 + 池子即时占用（PooledDB  introspection）。

    PooledDB 的 thread_usage()/idle_connections() 在 DBUtils 3.x 提供；版本不符时
    降级为只返回计数器，pool 字段标 unavailable，不让观测代码把主链路带倒。
    """
    with _stats_lock:
        snap = dict(_tx_stats)
        snap["by_code"] = dict(_tx_stats["by_code"])
    pool = _pool
    if pool is None:
        snap["pool"] = {"status": "not_initialized"}
        return snap
    # DBUtils 不同版本把参数暴露成 maxconnections 或 _maxconnections，
    # 属性缺失/改名在维护期很常见：宁可降级成 unavailable，不让观测代码把主链路带倒。
    max_conn = getattr(pool, "maxconnections", None) or getattr(pool, "_maxconnections", None)
    idle_fn = getattr(pool, "idle_connections", None)
    if not max_conn or not callable(idle_fn):
        snap["pool"] = {"status": "unavailable"}
        return snap
    try:
        idle = idle_fn()
    except Exception:
        snap["pool"] = {"status": "unavailable"}
        return snap
    in_use = max_conn - idle
    snap["pool"] = {"maxconnections": max_conn, "idle": idle, "in_use": in_use}
    with _stats_lock:
        if in_use > _tx_stats["pool_in_use_peak"]:
            _tx_stats["pool_in_use_peak"] = in_use
        snap["pool_in_use_peak"] = _tx_stats["pool_in_use_peak"]
        if in_use >= max_conn:
            _tx_stats["pool_wait_peak"] += 1
        snap["pool_wait_peak"] = _tx_stats["pool_wait_peak"]
    return snap


def _run_once(fn: Callable[[pymysql.cursors.Cursor], Any], *, read_snapshot: bool) -> Any:
    """跑一次事务：整段成功提交，任何异常都回滚并抛出。run_tx 的重试循环靠它。"""
    conn = _connect()
    try:
        if read_snapshot:
            with conn.cursor() as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        # 必须通知 DBUtils 已进入事务，禁止断线后只重放失败的单条 SQL。
        conn.begin()
        with conn.cursor() as cur:
            result = fn(cur)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def paginate(count_sql: str, data_sql: str, args: tuple, page: int, size: int) -> dict:
    """通用分页：count 总数 + 查当前页数据。返回 {items,total,page,size}。"""
    total = query_one(count_sql, args)["c"]
    offset = (page - 1) * size
    items = query(f"{data_sql} LIMIT %s OFFSET %s", (*args, size, offset))
    return {"items": items, "total": total, "page": page, "size": size}
