"""数据库访问层：连接 MySQL，启动时幂等建库建表，提供查询/执行/事务助手。

全部使用 pymysql 参数化（%s 占位符），杜绝 SQL 注入。
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any, Callable, Optional

import pymysql
import pymysql.cursors
from dbutils.pooled_db import PooledDB

from . import config

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

_pool: PooledDB | None = None
_pool_lock = threading.RLock()


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


def run_tx(fn: Callable[[pymysql.cursors.Cursor], Any], *, read_snapshot: bool = False) -> Any:
    """在一个事务里执行 fn(cur)；异常整体回滚并抛出。"""
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
