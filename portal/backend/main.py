"""IT 运营门户 · FastAPI 入口。

启动时幂等建库建表，仅显式演示模式灌入演示数据，随后托管 API 与单页前端。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import asyncio
import logging
import secrets
import sys
import time

import pymysql
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, config, db, events, logic, search_index, seed
from .routers import admin, analytics, user

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

logger = logging.getLogger("portal")
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.INFO)
    logger.propagate = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    if config.DEMO_MODE:
        seed.seed()
    # 搜索倒排索引在启动时全量重建，保证索引和业务表对齐
    # （运行时的新增/修改由上架接口增量维护，见 admin.reindex_* 调用）
    search_index.rebuild_all()
    # 积分实时推送：绑定事件循环（供轮询线程回投），再启动轮询线程。
    # 必须在 seed 之后 —— 水位要对齐到「已灌完演示数据」的那一刻，
    # 否则启动瞬间会把演示流水全推给第一个连上来的管理端。
    events.bind_loop(asyncio.get_running_loop())
    events.start_poller()
    yield
    events.stop_poller()


app = FastAPI(title="IT 运营门户", lifespan=lifespan)

app.include_router(user.router)
app.include_router(admin.router)
app.include_router(analytics.router)


# ---------- 中间件 ----------

_AUTH_WHITELIST = {"/api/auth/login", "/api/health"}
if config.DEMO_MODE:
    _AUTH_WHITELIST.add("/api/auth/dev-login")


@app.middleware("http")
async def global_auth(request: Request, call_next):
    """全局鉴权：所有 /api/* 默认要登录，白名单（登录/健康检查）放行。

    这样任何一个新接口天然受保护，不会再出现「漏挂鉴权依赖」导致越权的问题。
    """
    path = request.url.path
    if path.rstrip("/") == "/api/auth/dev-login" and not config.DEMO_MODE:
        return JSONResponse({"detail": "接口不存在"}, status_code=404)
    if path.startswith("/api/") and path not in _AUTH_WHITELIST:
        header = request.headers.get("Authorization", "")
        token = header[7:].strip() if header.startswith("Bearer ") else ""
        session = auth.resolve_session(token)
        if not session:
            return JSONResponse({"detail": "未登录或会话已过期"}, status_code=401)
        request.state.emp_id = session["emp_id"]
        request.state.session_id = session["session_id"]
    return await call_next(request)


@app.middleware("http")
async def trace_logging(request: Request, call_next):
    """请求日志 + Trace ID：每个 API 请求打一行结构化日志，响应头回 X-Request-Id。"""
    trace_id = secrets.token_hex(8)
    request.state.trace_id = trace_id
    start = time.perf_counter()
    response = await call_next(request)
    cost_ms = round((time.perf_counter() - start) * 1000, 1)
    if request.url.path.startswith("/api/"):
        emp = getattr(request.state, "emp_id", "-")
        logger.info("%s %s -> %s (%.1fms) user=%s trace=%s",
                    request.method, request.url.path, response.status_code, cost_ms, emp, trace_id)
    response.headers["X-Request-Id"] = trace_id
    return response


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    """全局异常处理：统一返回 500 JSON，记录堆栈，不泄露内部细节。"""
    trace_id = getattr(request.state, "trace_id", "-")
    logger.exception("未捕获异常 path=%s trace=%s", request.url.path, trace_id)
    return JSONResponse(
        {"detail": "服务器内部错误，请稍后重试", "code": "internal_error", "trace_id": trace_id},
        status_code=500,
    )


# ---------- 登录 / 会话 ----------

class LoginIn(BaseModel):
    username: str
    password: str


class DevLoginIn(BaseModel):
    role: str  # admin | user


def dev_login(payload: DevLoginIn):
    """本地模拟登录：无集团 BUC，直接按角色选一个预置账号进入（对应原项目本地 fallback）。"""
    if not config.DEMO_MODE:
        raise HTTPException(404, "接口不存在")
    if payload.role == "admin":
        row = db.query_one("SELECT * FROM users WHERE role = 'super_admin' ORDER BY emp_id ASC LIMIT 1")
    elif payload.role == "user":
        row = db.query_one("SELECT * FROM users WHERE role = 'user' ORDER BY emp_id ASC LIMIT 1")
    else:
        raise HTTPException(400, "role 必须是 admin 或 user")
    if not row:
        raise HTTPException(404, "该角色暂无账号")

    def grant(cur):
        logic.add_points(cur, row["emp_id"], 100, "新用户注册奖励", "welcome", 0)

    try:
        db.run_tx(grant)
    except pymysql.err.IntegrityError:
        pass  # 已发放过

    token, session_id = auth.create_session(row["emp_id"])
    return {"token": token, "session_id": session_id,
            "user": auth.serialize_user(row["emp_id"], session_id)}


if config.DEMO_MODE:
    app.post("/api/auth/dev-login")(dev_login)


@app.post("/api/auth/login")
def login(payload: LoginIn):
    row = db.query_one("SELECT * FROM users WHERE username = %s", (payload.username,))
    if not row or not auth.verify_password(payload.password, row["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")

    # 幂等赠送新用户欢迎积分
    def grant(cur):
        logic.add_points(cur, row["emp_id"], 100, "新用户注册奖励", "welcome", 0)

    try:
        db.run_tx(grant)
    except pymysql.err.IntegrityError:
        pass  # 已发放过

    token, session_id = auth.create_session(row["emp_id"])
    return {"token": token, "session_id": session_id,
            "user": auth.serialize_user(row["emp_id"], session_id)}


@app.post("/api/auth/logout")
def logout(creds=Depends(auth._bearer)):
    if creds:
        auth.drop_session(creds.credentials)
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: dict = Depends(auth.get_current_user)):
    return {"user": user}


@app.get("/api/health")
def health():
    return {"ok": True}


# 静态资源 + SPA 兜底（必须最后挂载）
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
