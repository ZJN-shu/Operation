"""秒杀接口（演进 D）。

用户热路径只两条：预约抢购（一次 Redis 往返）与查询预约结果。管理端负责活动
预热/收摊与观测。鉴权沿用全局中间件（/api/* 默认需登录）+ 角色依赖。
"""
from __future__ import annotations

from typing import Optional

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from .. import seckill
from ..auth import get_current_user, require_roles

router = APIRouter()

GIFT_ADMINS = ("super_admin", "shop_admin")


class WarmIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stock: Optional[int] = Field(None, strict=True, ge=0)


class SessionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gift_id: int = Field(..., strict=True, gt=0)
    start_at: datetime
    stock: int = Field(..., strict=True, gt=0)


# ---------- 用户侧 ----------

@router.get("/api/seckill/sessions")
def list_sessions(user: dict = Depends(get_current_user)):
    """未开始 + 进行中的秒杀场次（含倒计时所需的 start_at、本人是否已预约）。"""
    return {"sessions": seckill.list_sessions(user["emp_id"])}


@router.post("/api/seckill/sessions/{sid}/reserve")
def reserve(sid: int, user: dict = Depends(get_current_user)):
    """预约=订阅开抢前 5 分钟的提醒（不锁名额，名额仍到点先到先得）。"""
    return seckill.reserve_session(sid, user["emp_id"])


@router.delete("/api/seckill/sessions/{sid}/reserve")
def cancel_reserve(sid: int, user: dict = Depends(get_current_user)):
    return seckill.cancel_reservation(sid, user["emp_id"])


@router.post("/api/seckill/{gid}/grab")
def grab(gid: int, user: dict = Depends(get_current_user)):
    """原子预扣抢名额：售罄/限购直接返回，不打 DB；拿到名额则入队异步下单。"""
    return seckill.reserve(gid, user["emp_id"])


@router.get("/api/seckill/res/{key}")
def grab_status(key: str, user: dict = Depends(get_current_user)):
    """轮询一次预约的最终结果（queued→success/failed）。"""
    return seckill.status(key)


# ---------- 管理侧 ----------

@router.post("/api/admin/seckill/{gid}/warm")
def warm(gid: int, payload: WarmIn, user: dict = Depends(require_roles(*GIFT_ADMINS))):
    """把 DB 库存快照进 Redis 开抢；不传 stock 用当前真实库存，传则强制指定额度。"""
    return seckill.warm(gid, payload.stock)


@router.delete("/api/admin/seckill/{gid}/warm")
def unwarm(gid: int, user: dict = Depends(require_roles(*GIFT_ADMINS))):
    """收摊：删除额度/限购键，后续抢购一律判「活动未开始/已结束」。"""
    return seckill.unwarm(gid)


@router.get("/api/admin/seckill/{gid}/stats")
def stats(gid: int, user: dict = Depends(require_roles(*GIFT_ADMINS))):
    """并列展示 Redis 额度与 DB 库存真相 + 队列积压，确认削峰没把货算错。"""
    return seckill.stats(gid)


# ---------- 定时场次（排期 / 结束） ----------

@router.get("/api/admin/seckill/sessions")
def admin_sessions(user: dict = Depends(require_roles(*GIFT_ADMINS))):
    return {"sessions": seckill.list_admin_sessions()}


@router.post("/api/admin/seckill/sessions")
def create_session(payload: SessionIn, user: dict = Depends(require_roles(*GIFT_ADMINS))):
    """上架某礼品时排一个定时秒杀：到点自动开抢、开抢前提醒预约者。"""
    return seckill.create_session(payload.gift_id, payload.start_at, payload.stock,
                                  user["emp_id"])


@router.post("/api/admin/seckill/sessions/{sid}/end")
def end_session(sid: int, user: dict = Depends(require_roles(*GIFT_ADMINS))):
    return seckill.end_session(sid)
