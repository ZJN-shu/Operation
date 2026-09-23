"""鉴权：密码哈希、会话 token、FastAPI 依赖。"""
from __future__ import annotations

import hashlib
import secrets
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import config
from . import db

# 进程内会话表：token -> {emp_id, session_id}（单进程演示够用；生产可换 Redis/JWT）
_SESSIONS: dict[str, dict] = {}

_bearer = HTTPBearer(auto_error=False)


# ---------- 密码 ----------

def hash_password(password: str) -> str:
    salt = config.SECRET_KEY.encode()
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000).hex()


def verify_password(password: str, hashed: str) -> bool:
    return secrets.compare_digest(hash_password(password), hashed)


# ---------- 会话 ----------

def create_session(emp_id: str) -> tuple[str, str]:
    """建会话，返回 (token, session_id)。

    session_id 是埋点的会话维度：一串页面访问能被串成同一次会话，用来算
    访问深度 / 单会话漏斗。它不复用 token——token 会轮换，session 不该跟着变。
    """
    token = secrets.token_hex(32)
    session_id = secrets.token_hex(16)
    _SESSIONS[token] = {"emp_id": emp_id, "session_id": session_id}
    return token, session_id


def resolve_session(token: str) -> Optional[dict]:
    """按 token 取会话记录；无效 token 返回 None。"""
    return _SESSIONS.get(token or "")


def drop_session(token: str) -> bool:
    return _SESSIONS.pop(token or "", None) is not None


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
