"""鉴权：密码哈希、会话 token、FastAPI 依赖。"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import config
from . import db
from . import redis_client

# 进程内会话表：token -> {emp_id, session_id, created_at, last_seen}
# （单进程演示够用；生产可换 Redis/JWT）
_SESSIONS: dict[str, dict] = {}
# 会话表会被 HTTP 线程和 SSE 生成器并发读写；TTL 下的「检查 + 刷新」必须原子，
# 否则两个请求能同时看到同一个过期会话并各自删除，留下半更新的痕迹。
_SESSION_LOCK = threading.Lock()

_bearer = HTTPBearer(auto_error=False)


# ---------- 密码 ----------
# 两代格式并存：
#   v1（历史存量）：纯 hex，盐 = 全局 SECRET_KEY。同密码同哈希，拖库可跨用户比对。
#   v2（进阶）：`pbkdf2_sha256$<迭代数>$<盐hex>$<哈希hex>`，每用户独立随机盐，
#         自描述——改迭代数不需要全量重灌，旧行照旧能验。
# 登录验密成功后就地升级到 v2（见 upgrade_password），用户无感，不需要强制改密。

_V2_PREFIX = "pbkdf2_sha256"


def _pbkdf2(password: str, salt: bytes, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations).hex()


def hash_password(password: str) -> str:
    """新密码一律 v2：每用户随机盐。"""
    salt = secrets.token_hex(16)
    return f"{_V2_PREFIX}${config.PBKDF2_ITERATIONS}${salt}${_pbkdf2(password, salt.encode(), config.PBKDF2_ITERATIONS)}"


def is_legacy_hash(hashed: str) -> bool:
    """v1 哈希没有 `$`，靠这个区分两代格式。"""
    return "$" not in (hashed or "")


def verify_password(password: str, hashed: str) -> bool:
    hashed = hashed or ""
    if is_legacy_hash(hashed):
        # v1 兼容路径：维持原全局盐口径，否则存量账号全部验不过。
        legacy = _pbkdf2(password, config.SECRET_KEY.encode(), 100_000)
        return secrets.compare_digest(legacy, hashed)
    try:
        _, iters, salt, digest = hashed.split("$", 3)
        iters = int(iters)
    except ValueError:
        return False
    return secrets.compare_digest(_pbkdf2(password, salt.encode(), iters), digest)


def upgrade_password(emp_id: str, password: str) -> None:
    """v1 账号登录成功后透明升级到每用户随机盐。升级失败不影响本次登录：
    下次登录还会再试，不能把旁路改写做成阻断主流程。"""
    try:
        db.execute("UPDATE users SET password_hash = %s WHERE emp_id = %s AND password_hash NOT LIKE %s",
                   (hash_password(password), emp_id, f"{_V2_PREFIX}$%"))
    except Exception:
        import logging
        logging.getLogger("portal.auth").warning("密码哈希升级失败 emp_id=%s", emp_id, exc_info=True)


# ---------- 会话 ----------

def create_session(emp_id: str) -> tuple[str, str]:
    """建会话，返回 (token, session_id)。

    session_id 是埋点的会话维度：一串页面访问能被串成同一次会话，用来算
    访问深度 / 单会话漏斗。它不复用 token——token 会轮换，session 不该跟着变。

    建会话时顺手回收过期项：会话表没有独立清理线程，lazy 回收在登录频
    率下足够；TTL 配 0 表示不限（兼容测试与旧行为）。
    """
    token = secrets.token_hex(32)
    session_id = secrets.token_hex(16)
    if _use_redis():
        _redis_create(token, emp_id, session_id)
        return token, session_id
    now = time.monotonic()
    with _SESSION_LOCK:
        _evict_expired_locked(now)
        _SESSIONS[token] = {
            "emp_id": emp_id, "session_id": session_id,
            "created_at": now, "last_seen": now,
        }
    return token, session_id


def _expired_locked(s: dict, now: float) -> bool:
    idle = config.SESSION_IDLE_MINUTES * 60
    absolute = config.SESSION_ABSOLUTE_MINUTES * 60
    if idle and now - s["last_seen"] > idle:
        return True
    if absolute and now - s["created_at"] > absolute:
        return True
    return False


def _evict_expired_locked(now: float) -> None:
    dead = [t for t, s in _SESSIONS.items() if _expired_locked(s, now)]
    for t in dead:
        _SESSIONS.pop(t, None)


def resolve_session(token: str) -> Optional[dict]:
    """按 token 取会话记录；无效 / 已过期 token 返回 None，命中则刷新空闲时钟。"""
    if _use_redis():
        return _redis_resolve(token)
    now = time.monotonic()
    with _SESSION_LOCK:
        s = _SESSIONS.get(token or "")
        if not s:
            return None
        if _expired_locked(s, now):
            _SESSIONS.pop(token, None)
            return None
        s["last_seen"] = now
        return dict(s)


def drop_session(token: str) -> bool:
    if _use_redis():
        return _redis_drop(token)
    with _SESSION_LOCK:
        return _SESSIONS.pop(token or "", None) is not None


def drop_user_sessions(emp_id: str) -> int:
    """吊销某用户全部会话（改密 / 离职禁用时用），返回吊销数。"""
    if _use_redis():
        return _redis_drop_user(emp_id)
    with _SESSION_LOCK:
        dead = [t for t, s in _SESSIONS.items() if s["emp_id"] == emp_id]
        for t in dead:
            _SESSIONS.pop(t, None)
        return len(dead)


def session_count() -> int:
    if _use_redis():
        return _redis_count()
    with _SESSION_LOCK:
        return len(_SESSIONS)


# ---------- Redis 共享会话后端（演进 B） ----------
# 进程内 _SESSIONS 是「单 worker」的根因：多 worker 下 A 建的 token，B 查不到 → 401。
# 外置到 Redis 后 token 跨进程可读，才能开多 worker。语义与内存版对齐：
#   空闲 TTL 用 Redis key 过期承载（命中即 EXPIRE 刷新）；绝对 TTL 存 created_at 手动判。
# 时间基准从 monotonic 换 time.time()：monotonic 跨进程不可比，Redis 两侧必须用墙上时钟。

_SID = "{portal}:sess:"      # 花括号内为 Redis Cluster hash tag，保证多键命令同槽；与 str.format 无关
_UI = "{portal}:user_sess:"


def _use_redis() -> bool:
    return config.SESSION_BACKEND == "redis"


def _sid(token: str) -> str:
    return _SID + token


def _uid(emp_id: str) -> str:
    return _UI + emp_id


def _idle_seconds() -> int:
    return config.SESSION_IDLE_MINUTES * 60


def _redis_create(token: str, emp_id: str, session_id: str) -> None:
    now = time.time()
    r = redis_client.client()
    # 存成 Hash：resolve 侧用 HGETALL 读；created_at/last_seen 落成字符串，读时再转 float。
    mapping = {"emp_id": emp_id, "session_id": session_id,
               "created_at": repr(now), "last_seen": repr(now)}
    idle = _idle_seconds()
    pipe = r.pipeline()
    pipe.hset(_sid(token), mapping=mapping)
    if idle:  # 空闲 TTL 交给 key 过期；绝对 TTL 存 created_at 手动判
        pipe.expire(_sid(token), idle)
    pipe.sadd(_uid(emp_id), token)
    pipe.execute()


def _redis_resolve(token: str) -> Optional[dict]:
    if not token:
        return None
    r = redis_client.client()
    s = r.hgetall(_sid(token))
    if not s:
        return None
    absolute = config.SESSION_ABSOLUTE_MINUTES * 60
    now = time.time()
    if absolute and now - float(s["created_at"]) > absolute:
        r.delete(_sid(token))
        r.srem(_uid(s["emp_id"]), token)
        return None
    s["last_seen"] = now
    idle = _idle_seconds()
    if idle:  # 空闲时钟刷新：key TTL 即空闲窗口
        r.expire(_sid(token), idle)
    return {"emp_id": s["emp_id"], "session_id": s["session_id"],
            "created_at": float(s["created_at"]), "last_seen": now}


def _redis_drop(token: str) -> bool:
    if not token:
        return False
    r = redis_client.client()
    s = r.hgetall(_sid(token))
    deleted = r.delete(_sid(token))
    if s.get("emp_id"):
        r.srem(_uid(s["emp_id"]), token)
    return bool(deleted)


def _redis_drop_user(emp_id: str) -> int:
    r = redis_client.client()
    tokens = r.smembers(_uid(emp_id))
    if not tokens:
        return 0
    pipe = r.pipeline()
    for t in tokens:
        pipe.delete(_sid(t))
    pipe.delete(_uid(emp_id))
    pipe.execute()
    return len(tokens)


def _redis_count() -> int:
    r = redis_client.client()
    return sum(1 for _ in r.scan_iter(match=_SID + "*", count=200))


def _lookup(emp_id: str) -> Optional[dict]:
    row = db.query_one("SELECT * FROM users WHERE emp_id = %s", (emp_id,))
    return row


def serialize_user(emp_id: str, session_id: str = "") -> dict:
    user = _lookup(emp_id)
    if not user:
        raise HTTPException(401, "用户不存在")
    bal = db.query_one(
        "SELECT balance FROM point_accounts WHERE emp_id = %s", (emp_id,)
    )
    return {
        "emp_id": user["emp_id"],
        "username": user["username"],
        "name": user["name"],
        "role": user["role"],
        "department": user["department"],
        "points": bal["balance"] if bal else 0,
        "session_id": session_id,
    }


# ---------- FastAPI 依赖 ----------

def get_current_user(request: Request) -> dict:
    """依赖：读取全局鉴权中间件注入的 emp_id / session_id，还原当前用户。

    把 session_id 一并放进用户上下文，路由写埋点时不用再各自去 Request 里捞。
    """
    emp_id = getattr(request.state, "emp_id", None)
    if not emp_id:
        raise HTTPException(401, "未登录或会话已过期")
    return serialize_user(emp_id, getattr(request.state, "session_id", ""))


ADMIN_ROLES = ("super_admin", "content_admin", "shop_admin", "viewer")


def require_roles(*roles: str):
    """返回一个依赖，要求当前用户 role 在给定角色集合内。"""
    def checker(user: dict = Depends(get_current_user)) -> dict:
        if user["role"] not in roles:
            raise HTTPException(403, "无权限执行该操作")
        return user
    return checker


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """任意管理角色（含只读运营）即可。"""
    if user["role"] not in ADMIN_ROLES:
        raise HTTPException(403, "需要管理员权限")
    return user
