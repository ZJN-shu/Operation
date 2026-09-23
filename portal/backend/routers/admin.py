"""管理后台接口：课程/活动/礼品管理、轮播图顺序、订单发货、审计日志。

权限模型（RBAC）：
  super_admin    全部
  content_admin  课程 + 活动
  shop_admin     礼品 + 订单
  viewer         只看数据看板
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import List, Literal, Optional

import pymysql
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .. import auth, db, events, logic, metrics, redemption, search_index
from ..auth import require_admin, require_roles

router = APIRouter()

logger = logging.getLogger("portal.admin")

# 角色组合
COURSE_ADMINS = ("super_admin", "content_admin")
ACTIVITY_ADMINS = ("super_admin", "content_admin")
GIFT_ADMINS = ("super_admin", "shop_admin")
ORDER_ADMINS = ("super_admin", "shop_admin")
SUPER = ("super_admin",)

# SSE 心跳间隔（秒）。既是保活，也是断线探测 —— 写这一下能真正发现死连接。
# 顺带复查会话是否还有效（见 stream_points 里的说明）。
HEARTBEAT_SECONDS = 15


# ---------- 请求模型 ----------

class CourseIn(BaseModel):
    title: str
    category: str = "通用"
    level: str = "入门"
    duration: str = ""
    instructor: str = ""
    points: int = Field(0, strict=True, ge=0, le=redemption.MAX_POINTS)
    description: str = ""
    emoji: str = "📚"


class CourseUpdate(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None
    level: Optional[str] = None
    duration: Optional[str] = None
    instructor: Optional[str] = None
    points: Optional[int] = Field(None, strict=True, ge=0, le=redemption.MAX_POINTS)
    description: Optional[str] = None
    emoji: Optional[str] = None
    status: Optional[str] = None


class ActivityIn(BaseModel):
    title: str
    subtitle: str = ""
    event_time: str = ""
    location: str = ""
    points: int = Field(0, strict=True, ge=0, le=redemption.MAX_POINTS)
    description: str = ""
    emoji: str = "🎪"
    is_carousel: int = 0
    carousel_order: int = 0


class ActivityUpdate(BaseModel):
    title: Optional[str] = None
    subtitle: Optional[str] = None
    event_time: Optional[str] = None
    location: Optional[str] = None
    points: Optional[int] = Field(None, strict=True, ge=0, le=redemption.MAX_POINTS)
    description: Optional[str] = None
    emoji: Optional[str] = None
    status: Optional[str] = None


class GiftIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(..., min_length=1, max_length=255)
    category: str = Field("周边", max_length=64)
    points_cost: int = Field(0, strict=True, ge=0, le=redemption.MAX_POINTS)
    stock: int = Field(0, strict=True, ge=0, le=redemption.MAX_STOCK)
    icon: str = Field("🎁", max_length=64)
    description: str = Field("", max_length=10000)


class GiftUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    category: Optional[str] = Field(None, max_length=64)
    points_cost: Optional[int] = Field(None, strict=True, ge=0, le=redemption.MAX_POINTS)
    icon: Optional[str] = Field(None, max_length=64)
    description: Optional[str] = Field(None, max_length=10000)
    status: Optional[Literal["active", "offline"]] = None

    @model_validator(mode="after")
    def reject_null(self):
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("已提供字段不得为 null")
        return self


class StatusIn(BaseModel):
    status: str  # active | offline


class CarouselItem(BaseModel):
    activity_id: int
    order: int


class CarouselUpdate(BaseModel):
    items: List[CarouselItem] = []


class ShipIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    express: str = Field(..., min_length=1, max_length=128)


def _apply_update(table: str, rid: int, data: dict, allowed: set) -> None:
    """用白名单列构建动态 UPDATE，值参数化，防注入。"""
    keys = [k for k in data if k in allowed]
    if not keys:
        return
    sets = ", ".join(f"{k} = %s" for k in keys)
    vals = [data[k] for k in keys]
    db.execute(f"UPDATE {table} SET {sets} WHERE id = %s", (*vals, rid))


def _check_status(status: str) -> None:
    if status not in ("active", "offline"):
        raise HTTPException(400, "status 必须是 active 或 offline")


def _reindex(doc_type: str, doc_id: int) -> None:
    """业务写操作后同步搜索索引（课程 / 礼品）。

    索引跟着写操作走，而不是等下次重启全量重建 —— 否则管理员新加的课程
    在下一次重启前一直搜不到。索引失败不应该让业务写回滚，所以吞掉异常只记日志。
    """
    try:
        search_index.reindex_doc(doc_type, doc_id)
    except Exception:
        logger.exception("搜索索引更新失败 doc_type=%s id=%s", doc_type, doc_id)


# ---------- 课程管理 ----------

@router.get("/api/admin/courses")
def list_courses(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    _: dict = Depends(require_roles(*COURSE_ADMINS)),
):
    return db.paginate(
        "SELECT COUNT(*) AS c FROM courses",
        "SELECT c.*, (SELECT COUNT(*) FROM training_progress tp WHERE tp.course_id = c.id) AS enrolls "
        "FROM courses c ORDER BY c.id DESC",
        (), page, size,
    )


@router.post("/api/admin/courses")
def create_course(payload: CourseIn, user: dict = Depends(require_roles(*COURSE_ADMINS))):
    cid = db.insert(
        "INSERT INTO courses (title, category, level, duration, instructor, points, description, emoji) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (payload.title, payload.category, payload.level, payload.duration, payload.instructor,
         payload.points, payload.description, payload.emoji),
    )
    logic.log_audit(user["emp_id"], "create_course", "course", cid, payload.model_dump())
    _reindex("course", cid)
    return {"ok": True, "id": cid}


@router.put("/api/admin/courses/{cid}")
def update_course(cid: int, payload: CourseUpdate, user: dict = Depends(require_roles(*COURSE_ADMINS))):
    data = payload.model_dump(exclude_unset=True)
    _apply_update("courses", cid, data, {
        "title", "category", "level", "duration", "instructor", "points",
        "description", "emoji", "status",
    })
    logic.log_audit(user["emp_id"], "update_course", "course", cid, data)
    _reindex("course", cid)
    return {"ok": True}


@router.post("/api/admin/courses/{cid}/status")
def toggle_course(cid: int, payload: StatusIn, user: dict = Depends(require_roles(*COURSE_ADMINS))):
    _check_status(payload.status)
    db.execute("UPDATE courses SET status = %s WHERE id = %s", (payload.status, cid))
    logic.log_audit(user["emp_id"], "toggle_course", "course", cid, {"status": payload.status})
    _reindex("course", cid)   # 下架即从索引摘掉，搜不到
    return {"ok": True}


# ---------- 活动管理 ----------

@router.get("/api/admin/activities")
def list_activities(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    _: dict = Depends(require_roles(*ACTIVITY_ADMINS)),
):
    return db.paginate(
        "SELECT COUNT(*) AS c FROM activities",
        "SELECT a.*, (SELECT COUNT(*) FROM activity_participants ap WHERE ap.activity_id = a.id) AS parts "
        "FROM activities a ORDER BY a.id DESC",
        (), page, size,
    )


@router.post("/api/admin/activities")
def create_activity(payload: ActivityIn, user: dict = Depends(require_roles(*ACTIVITY_ADMINS))):
    aid = db.insert(
        "INSERT INTO activities (title, subtitle, event_time, location, points, description, emoji, is_carousel, carousel_order) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (payload.title, payload.subtitle, payload.event_time, payload.location, payload.points,
         payload.description, payload.emoji, payload.is_carousel, payload.carousel_order),
    )
    logic.log_audit(user["emp_id"], "create_activity", "activity", aid, payload.model_dump())
    return {"ok": True, "id": aid}


@router.put("/api/admin/activities/{aid}")
def update_activity(aid: int, payload: ActivityUpdate, user: dict = Depends(require_roles(*ACTIVITY_ADMINS))):
    data = payload.model_dump(exclude_unset=True)
    _apply_update("activities", aid, data, {
        "title", "subtitle", "event_time", "location", "points",
        "description", "emoji", "status",
    })
    logic.log_audit(user["emp_id"], "update_activity", "activity", aid, data)
    return {"ok": True}


@router.post("/api/admin/activities/{aid}/status")
def toggle_activity(aid: int, payload: StatusIn, user: dict = Depends(require_roles(*ACTIVITY_ADMINS))):
    _check_status(payload.status)
    db.execute("UPDATE activities SET status = %s WHERE id = %s", (payload.status, aid))
    logic.log_audit(user["emp_id"], "toggle_activity", "activity", aid, {"status": payload.status})
    return {"ok": True}


@router.put("/api/admin/carousel")
def update_carousel(payload: CarouselUpdate, user: dict = Depends(require_roles(*ACTIVITY_ADMINS))):
    db.execute("UPDATE activities SET is_carousel = 0, carousel_order = 0")
    for item in payload.items:
        db.execute(
            "UPDATE activities SET is_carousel = 1, carousel_order = %s WHERE id = %s",
            (item.order, item.activity_id),
        )
    logic.log_audit(user["emp_id"], "update_carousel", "carousel", None,
                    {"items": [i.model_dump() for i in payload.items]})
    return {"ok": True}


# ---------- 礼品管理 ----------

@router.get("/api/admin/gifts")
def list_gifts(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    _: dict = Depends(require_roles(*GIFT_ADMINS)),
):
    return db.paginate(
        "SELECT COUNT(*) AS c FROM gifts",
        "SELECT g.*, (SELECT COUNT(*) FROM redemptions r WHERE r.gift_id = g.id) AS redeemed "
        "FROM gifts g ORDER BY g.id ASC",
        (), page, size,
    )


@router.post("/api/admin/gifts")
def create_gift(payload: GiftIn, user: dict = Depends(require_roles(*GIFT_ADMINS))):
    def fn(cur):
        cur.execute(
            "INSERT INTO gifts (name, category, points_cost, stock, icon, description) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (payload.name, payload.category, payload.points_cost, payload.stock, payload.icon,
             payload.description))
        gid = cur.lastrowid
        redemption.stock_baseline(cur, {"id": gid, "stock": payload.stock}, user["emp_id"])
        logic.log_audit(user["emp_id"], "create_gift", "gift", gid, payload.model_dump(), cur=cur)
        return gid
    gid = db.run_tx(fn)
    _reindex("gift", gid)
    return {"ok": True, "id": gid}


@router.put("/api/admin/gifts/{gid}")
def update_gift(gid: int, payload: GiftUpdate, user: dict = Depends(require_roles(*GIFT_ADMINS))):
    data = payload.model_dump(exclude_unset=True)
    def fn(cur):
        cur.execute("SELECT id FROM gifts WHERE id = %s FOR UPDATE", (gid,))
        if not cur.fetchone():
            raise HTTPException(404, "礼品不存在")
        if data:
            keys = list(data)
            cur.execute("UPDATE gifts SET " + ", ".join(f"{key} = %s" for key in keys) + " WHERE id = %s",
                        (*[data[key] for key in keys], gid))
            logic.log_audit(user["emp_id"], "update_gift", "gift", gid, data, cur=cur)
    db.run_tx(fn)
    _reindex("gift", gid)
    return {"ok": True}


@router.post("/api/admin/gifts/{gid}/status")
def toggle_gift(gid: int, payload: StatusIn, user: dict = Depends(require_roles(*GIFT_ADMINS))):
    _check_status(payload.status)
    return update_gift(gid, GiftUpdate(status=payload.status), user)


@router.post("/api/admin/gifts/{gid}/stock")
def adjust_gift_stock(gid: int, payload: redemption.StockIn,
                      user: dict = Depends(require_roles(*GIFT_ADMINS))):
    return redemption.adjust_stock(gid, payload, user["emp_id"])


@router.post("/api/admin/search/reindex")
def reindex_search(user: dict = Depends(require_roles(*SUPER))):
    """手工全量重建搜索索引（超级管理员）。索引怀疑脏了就用这个兜底。"""
    counts = search_index.rebuild_all()
    logic.log_audit(user["emp_id"], "reindex_search", "search_keywords", None, counts)
    return {"ok": True, "counts": counts}


# ---------- 订单发货 ----------

@router.get("/api/admin/orders")
def list_orders(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    _: dict = Depends(require_roles(*ORDER_ADMINS)),
):
    return db.paginate(
        "SELECT COUNT(*) AS c FROM redemptions",
        "SELECT r.id, r.gift_id, r.emp_id, r.points_cost, r.status, r.express, r.created_at, r.shipped_at, "
        "g.name AS gift_name, g.icon AS gift_icon, u.name AS user_name, u.username "
        "FROM redemptions r JOIN gifts g ON g.id = r.gift_id JOIN users u ON u.emp_id = r.emp_id "
        "ORDER BY r.id DESC",
        (), page, size,
    )


@router.post("/api/admin/orders/{oid}/ship")
def ship_order(oid: int, payload: ShipIn, user: dict = Depends(require_roles(*ORDER_ADMINS))):
    return redemption.ship(oid, payload.express, user["emp_id"])


@router.post("/api/admin/orders/{oid}/cancel")
def cancel_order(oid: int, payload: redemption.CancelIn,
                 user: dict = Depends(require_roles(*ORDER_ADMINS))):
    return redemption.refund(oid, payload.reason, user["emp_id"])


@router.post("/api/admin/orders/{oid}/refund")
def refund_order(oid: int, payload: redemption.RefundIn,
                 user: dict = Depends(require_roles(*ORDER_ADMINS))):
    return redemption.refund(oid, payload.reason, user["emp_id"], returned=True)


@router.get("/api/admin/orders/reconciliation")
def reconcile_orders(_: dict = Depends(require_roles(*ORDER_ADMINS))):
    return redemption.reconciliation()


# ---------- 审计日志（仅超级管理员）----------

@router.get("/api/admin/audit-logs")
def list_audit_logs(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    _: dict = Depends(require_roles(*SUPER)),
):
    return db.paginate(
        "SELECT COUNT(*) AS c FROM audit_logs",
        "SELECT a.id, a.emp_id, a.action, a.target_type, a.target_id, a.detail, a.created_at, "
        "u.name AS user_name FROM audit_logs a LEFT JOIN users u ON u.emp_id = a.emp_id "
        "ORDER BY a.id DESC",
        (), page, size,
    )


# ---------- 积分明细（全员流水 + 实时推送 + 人工操作）----------
#
# 读写分权与全站惯例一致：读 = 全部管理角色（含只读 viewer，即「有数据看板权限」的角色），
# 写 = 仅 super_admin。
#
# ref_type / ref_id 的约定（见 schema.sql）：
#   系统流水 welcome/announcement/course/activity/redeem —— ref_id 是业务对象 id
#   人工流水 grant/deduct/revert/reconcile           —— ref_id 是 point_ops.id
# 所以下面每一处 join point_ops 都带 ref_type 限定，否则课程流水的 ref_id 会撞上
# point_ops.id，串出一个不相干的管理员当「操作人」。

# 与 ref_type 对应的中文标签（前端只认 ref_type，标签在服务端出，避免两边各写一份）。
# 注意这里没有 reconcile：校平只重建余额缓存、不产生流水行（见 reconcile_points）。
POINT_REF_LABELS = {
    "welcome": "注册奖励", "announcement": "阅读公告", "course": "完成课程",
    "activity": "参与活动", "redeem": "兑换礼品", "refund": "订单退款",
    "grant": "人工发放", "deduct": "人工扣减", "revert": "回滚",
}

_POINT_FROM = (
    "FROM point_records pr "
    "LEFT JOIN users u ON u.emp_id = pr.emp_id "
    "LEFT JOIN point_ops po ON po.id = pr.ref_id AND pr.ref_type IN %s "
    "LEFT JOIN users ou ON ou.emp_id = po.operator_emp_id "
)
_POINT_COLS = (
    "SELECT pr.id, pr.emp_id, u.username, u.name AS user_name, u.department, "
    "pr.points, pr.note, pr.ref_type, pr.ref_id, pr.created_at, "
    "po.op_type, po.operator_emp_id, ou.name AS operator_name "
)


def _point_where(emp_id: str, ref_type: str, direction: str, days: int):
    """拼筛选条件。列名来自固定集合，值一律参数化（同 _apply_update 的思路）。"""
    clauses, args = [], []
    if emp_id:
        clauses.append("pr.emp_id = %s")
        args.append(emp_id)
    if ref_type:
        clauses.append("pr.ref_type = %s")
        args.append(ref_type)
    if direction == "in":
        clauses.append("pr.points > 0")
    elif direction == "out":
        clauses.append("pr.points < 0")
    if days > 0:
        clauses.append("pr.created_at >= DATE_SUB(NOW(), INTERVAL %s DAY)")
        args.append(days)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, tuple(args)


@router.get("/api/admin/points")
def list_points(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    emp_id: str = Query("", max_length=32),
    ref_type: str = Query("", max_length=32),
    direction: str = Query("", max_length=8),
    days: int = Query(0, ge=0, le=3650),
    _: dict = Depends(require_admin),
):
    """全员积分流水。所有管理角色可读（含只读 viewer）。"""
    if ref_type and ref_type not in POINT_REF_LABELS:
        raise HTTPException(400, "未知的流水类型")
    if direction not in ("", "in", "out"):
        raise HTTPException(400, "direction 只能是 in / out / 空")

    where, args = _point_where(emp_id, ref_type, direction, days)
    # count 与 data 必须 join 同一批表：这里全是 LEFT JOIN，不会丢行；
    # 换成 INNER JOIN users 就可能「共 21 条却只显示 20 行」、末页空白。
    d = db.paginate(
        "SELECT COUNT(*) AS c " + _POINT_FROM + where,
        _POINT_COLS + _POINT_FROM + where + " ORDER BY pr.id DESC",
        (logic.ADMIN_REF_TYPES,) + args,
        page, size,
    )
    for it in d["items"]:
        it["ref_label"] = POINT_REF_LABELS.get(it["ref_type"], it["ref_type"])
        # 操作人：人工流水显示管理员工号，其余是用户自己触发的
        if not it.get("operator_emp_id"):
            it["operator_label"] = "本人"
        else:
            it["operator_label"] = f'{it["operator_emp_id"]} {it.get("operator_name") or ""}'.strip()
        # 能不能回滚由服务端说了算：规则就是 revert_points 里那条判断，
        # 前端只负责按这个布尔值渲染按钮。让前端自己维护一份「哪些 ref_type 是人工操作」
        # 的清单，就是在两个地方各写一遍同一条规则，早晚对不上。
        it["revertible"] = it["ref_type"] not in logic.NON_REVERTIBLE_REF_TYPES
        if it.get("created_at"):
            it["created_at"] = it["created_at"].strftime("%Y-%m-%d %H:%M:%S")
    return d


@router.get("/api/admin/points/ref-types")
def point_ref_types(_: dict = Depends(require_admin)):
    """筛选下拉用的类型清单（服务端单一来源，避免前后端各写一份枚举）。"""
    return {"ref_types": [{"value": k, "label": v} for k, v in POINT_REF_LABELS.items()]}


@router.get("/api/admin/points/trend")
def points_trend(days: int = Query(7, ge=1, le=90), _: dict = Depends(require_admin)):
    """积分趋势的**单独**入口，只为实时刷新用。

    为什么要单开一条而不是复用 /api/admin/dashboard：明细表收到一条推送帧就得刷一次
    趋势图，而看板载荷要把用户数/漏斗/热搜/订单全查一遍 —— 一张图挪动一格，
    不值当把整个看板重算一遍。

    口径必须走 metrics（见 metrics.py 头部约定）：客户端**不许自己拿帧里的 points
    往当日柱子上加**。加了就和服务端对不上 —— 回滚行（ref_type='revert'）不进口径，
    被回滚掉的原笔也要一起排除，这两条规则只有 metrics 里那一份。前端自己算，
    一次回滚之后图就和接口的数字不一样了。
    """
    return {"daily_points": metrics.daily_points(days)}


@router.get("/api/admin/points/drift")
def points_drift(_: dict = Depends(require_admin)):
    """盘点：账户余额与流水合计对不上的工号。

    这不是演示功能。seed._earn 用两条各自自动提交的语句写流水和余额，天生非原子，
    所以库上本来就有账户不平（实测 6 个）。这个接口是校平的前置查询。
    """
    rows = db.query(
        "SELECT pa.emp_id, u.name AS user_name, pa.balance, "
        "COALESCE(t.s, 0) AS records_sum, pa.balance - COALESCE(t.s, 0) AS drift "
        "FROM point_accounts pa "
        "LEFT JOIN users u ON u.emp_id = pa.emp_id "
        "LEFT JOIN (SELECT emp_id, SUM(points) AS s FROM point_records GROUP BY emp_id) t "
        "ON t.emp_id = pa.emp_id "
        "WHERE pa.balance <> COALESCE(t.s, 0) "
        "ORDER BY ABS(pa.balance - COALESCE(t.s, 0)) DESC"
    )
    return {"items": rows, "total": len(rows)}


class AdjustIn(BaseModel):
    emp_id: str = Field(..., min_length=1, max_length=32)
    # 正值=发放，负值=扣减。一个接口带符号入参，避免「本该发放却发到了扣减接口」
    points: int = Field(..., ge=-100000, le=100000)
    # note 是 VARCHAR(128)，strict 模式溢出报 1406 会把整个事务滚掉变成 500，
    # 所以这里卡住长度（还要留出「人工发放：」前缀的余量）
    reason: str = Field(..., min_length=1, max_length=100)
    request_id: str = Field(..., min_length=8, max_length=64)


class RevertIn(BaseModel):
    reason: str = Field("", max_length=100)
    request_id: str = Field(..., min_length=8, max_length=64)


class ReconcileIn(BaseModel):
    emp_id: str = Field(..., min_length=1, max_length=32)
    reason: str = Field("", max_length=100)
    request_id: str = Field(..., min_length=8, max_length=64)


def _require_user(emp_id: str) -> dict:
    """动积分前先确认工号真实存在。

    不校验的话 logic.ensure_account 的 INSERT IGNORE 会给不存在的工号
    凭空造一个 point_accounts 行，账本里多出一批查不到姓名的流水。
    """
    target = db.query_one("SELECT emp_id, name FROM users WHERE emp_id = %s", (emp_id,))
    if not target:
        raise HTTPException(404, "工号不存在")
    return target


@router.post("/api/admin/points/adjust")
def adjust_points(payload: AdjustIn, user: dict = Depends(require_roles(*SUPER))):
    """人工发放（points>0）或扣减（points<0）。仅超级管理员。"""
    if payload.points == 0:
        # 0 分流水既不属于发放也不属于消耗，在按方向筛选的列表里会凭空消失
        raise HTTPException(400, "变动值不能为 0")
    _require_user(payload.emp_id)

    op_type = "grant" if payload.points > 0 else "deduct"
    note = f"人工发放：{payload.reason}" if payload.points > 0 else f"人工扣减：{payload.reason}"

    def fn(cur):
        # 先锁账户行，再改余额。顺序与 redeem 一致（先账户后礼品），不会形成死锁环。
        cur.execute("SELECT balance FROM point_accounts WHERE emp_id = %s FOR UPDATE",
                    (payload.emp_id,))
        row = cur.fetchone()
        balance = row["balance"] if row else 0
        if balance + payload.points < 0:
            raise HTTPException(400, f"扣减后余额将为负（当前 {balance}）")
        # 先插 point_ops 拿自增 id 当 ref_id：point_records 的 uk_ref 是
        # (emp_id, ref_type, ref_id)，ref_id 写死 0 的话同一用户第二笔发放就撞唯一键。
        cur.execute(
            "INSERT INTO point_ops (op_type, target_emp_id, points, reason, "
            "operator_emp_id, idem_key) VALUES (%s, %s, %s, %s, %s, %s)",
            (op_type, payload.emp_id, payload.points, payload.reason,
             user["emp_id"], payload.request_id),
        )
        op_id = cur.lastrowid
        record_id = logic.add_points(cur, payload.emp_id, payload.points, note, op_type, op_id)
        logic.log_audit(user["emp_id"], f"points_{op_type}", "point_records", record_id,
                        {"target": payload.emp_id, "points": payload.points,
                         "reason": payload.reason}, cur=cur)
        return {"op_id": op_id, "record_id": record_id,
                "balance_before": balance, "balance_after": balance + payload.points,
                "op_type": op_type}

    try:
        return db.run_tx(fn)
    except pymysql.err.IntegrityError as exc:
        if logic.is_duplicate(exc):
            # 命中 point_ops.uk_idem：同一次请求被重投了。这里必须是 409 而不是静默成功，
            # 否则前端以为又发了一笔，会和实际入账对不上。
            raise HTTPException(409, "请勿重复提交（该请求已处理）")
        raise


@router.post("/api/admin/points/{rid}/revert")
def revert_points(rid: int, payload: RevertIn, user: dict = Depends(require_roles(*SUPER))):
    """回滚指定流水（按原样反向记一笔）。仅超级管理员。"""
    rec = db.query_one(
        "SELECT id, emp_id, points, note, ref_type FROM point_records WHERE id = %s", (rid,))
    if not rec:
        raise HTTPException(404, "流水不存在")
    if rec["ref_type"] in ("redeem", "refund"):
        raise HTTPException(400, "兑换相关流水必须通过订单取消或退货退款处理")
    if rec["ref_type"] in logic.ADMIN_REF_TYPES:
        # 否则可以「回滚一条回滚」，账本变成镜厅，谁也说不清最终余额是怎么来的
        raise HTTPException(400, "人工操作产生的流水不支持回滚")
    note = f"回滚：{rec['note']}" + (f"（{payload.reason}）" if payload.reason else "")

    def fn(cur):
        cur.execute("SELECT balance FROM point_accounts WHERE emp_id = %s FOR UPDATE",
                    (rec["emp_id"],))
        row = cur.fetchone()
        balance = row["balance"] if row else 0
        delta = -rec["points"]
        # 先插 point_ops，再判余额 —— 顺序不能反。
        # 「双击回滚」的第二次会同时踩到两个拒绝条件：已回滚过（uk_reverted）和
        # 余额将变负。让数据库先开口，报的是「这笔流水已经回滚过了」（409）——
        # 报「回滚后余额将为负」是答非所问，会把人往资金问题上带。
        cur.execute(
            "INSERT INTO point_ops (op_type, target_emp_id, points, reason, "
            "operator_emp_id, idem_key, reverted_record_id) "
            "VALUES ('revert', %s, %s, %s, %s, %s, %s)",
            (rec["emp_id"], delta, payload.reason, user["emp_id"],
             payload.request_id, rid),
        )
        op_id = cur.lastrowid
        if balance + delta < 0:
            # 全站假设余额非负（user.redeem 就按这个前提判够不够扣）。
            # 抛出去会让整个事务回滚，上面那行 point_ops 不会留下。
            raise HTTPException(400, f"回滚后余额将为负（当前 {balance}）")
        # ref_id 一律用 point_ops.id（不是被回滚的流水 id）——
        # 否则查不出来是谁点的回滚。防重复回滚靠 point_ops.uk_reverted 唯一键。
        record_id = logic.add_points(cur, rec["emp_id"], delta, note, "revert", op_id)
        logic.log_audit(user["emp_id"], "points_revert", "point_records", record_id,
                        {"reverted": rid, "points": delta}, cur=cur)
        return {"op_id": op_id, "record_id": record_id, "reverted": rid,
                "points": delta, "balance_after": balance + delta}

    try:
        return db.run_tx(fn)
    except pymysql.err.IntegrityError as exc:
        if logic.is_duplicate(exc):
            raise HTTPException(409, "这笔流水已经回滚过了")
        raise


@router.post("/api/admin/points/reconcile")
def reconcile_points(payload: ReconcileIn, user: dict = Depends(require_roles(*SUPER))):
    """把某个用户的账户余额**重算**为「流水合计」（单用户，仅超级管理员）。

    口径：point_records 是事实来源（每一笔增减的原始记录），point_accounts.balance
    只是它的一层物化缓存（add_points 负责同步维护）。所以校平 = 用事实来源重建缓存。

    为什么不是「插一笔差额流水把余额补上」：**那样根本修不好**。add_points 同时改
    流水和余额，插 x 分得到 balance+x 和 sum+x —— 差额是个不变量，插多少笔都纹丝不动。
    要真的改掉差额，只能直接改缓存这一侧（要么改 balance，要么绕过 add_points 裸插
    流水）。裸插流水会让看板的「累计发放积分」凭空多出一批伪造的历史，所以选改缓存。
    代价是这里不产生流水行：校平不是一次业务上的加分/减分，它只让缓存和账本对上，
    留痕在 point_ops 和审计日志里（action=points_reconcile）。
    """
    _require_user(payload.emp_id)

    def fn(cur):
        # 加锁顺序不能反：先锁账户行，再读流水合计。
        # 发分方对同一行做 UPDATE，会争同一把行锁，所以持锁读合计才有原子性；
        # 先读合计再加锁，会拿一个已被并发提交作废的快照去「修正」余额。
        cur.execute("SELECT balance FROM point_accounts WHERE emp_id = %s FOR UPDATE",
                    (payload.emp_id,))
        row = cur.fetchone()
        if not row:
            raise HTTPException(404, "该工号暂无积分账户")
        balance = row["balance"]
        cur.execute("SELECT COALESCE(SUM(points), 0) AS s FROM point_records WHERE emp_id = %s",
                    (payload.emp_id,))
        records_sum = int(cur.fetchone()["s"])
        delta = records_sum - balance
        if delta:
            cur.execute("UPDATE point_accounts SET balance = %s WHERE emp_id = %s",
                        (records_sum, payload.emp_id))
        cur.execute(
            "INSERT INTO point_ops (op_type, target_emp_id, points, reason, "
            "operator_emp_id, idem_key) VALUES ('reconcile', %s, %s, %s, %s, %s)",
            (payload.emp_id, delta, payload.reason, user["emp_id"], payload.request_id),
        )
        op_id = cur.lastrowid
        logic.log_audit(user["emp_id"], "points_reconcile", "point_accounts", None,
                        {"target": payload.emp_id, "balance": balance,
                         "records_sum": records_sum, "delta": delta}, cur=cur)
        return {"op_id": op_id, "balance_before": balance, "balance_after": records_sum,
                "records_sum": records_sum, "delta": delta}

    try:
        return db.run_tx(fn)
    except pymysql.err.IntegrityError as exc:
        if logic.is_duplicate(exc):
            raise HTTPException(409, "请勿重复提交（该请求已处理）")
        raise


@router.get("/api/admin/points/stream")
async def stream_points(request: Request, user: dict = Depends(require_admin)):
    """SSE 实时流水。所有管理角色可连（含只读 viewer）。

    用 fetch + getReader 消费而不是 EventSource —— EventSource 不能带
    Authorization 头，本项目当初否掉 sendBeacon 也是同一个原因。

    这个生成器里**不查库**：首屏数据走 GET /api/admin/points。
    所以一条开着的流不占连接池里的连接（池子只有 20 条）。
    """
    q = events.subscribe()
    token = request.headers.get("Authorization", "")
    token = token[7:].strip() if token.startswith("Bearer ") else ""

    async def gen():
        try:
            yield ": connected\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    # 心跳兼作鉴权：_SESSIONS 没有 TTL，drop_session 只在登出时调。
                    # 建连那一刻校验过一次之后就没人再管了 —— 不在这里复查的话，
                    # 已登出的 token 还能继续收到全量流水，直到标签页关掉。
                    if not auth.resolve_session(token):
                        yield "event: bye\ndata: {}\n\n"
                        break
                    yield ": ping\n\n"
                    continue
                # 只在心跳时复查鉴权不够：来帧持续不断时 wait_for 永远不超时，
                # 已撤销的会话就能靠一条不停的流水一直推送下去。每帧外发前再查一次。
                if not auth.resolve_session(token):
                    yield "event: bye\ndata: {}\n\n"
                    break
                yield "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
                if payload.get("type") == "dropped":
                    # 队列满说明客户端读得比生产慢，中间已经丢帧了。
                    # 让前端自己重载当前页补齐，而不是假装数据是连续的。
                    continue
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # 响应头此时已经发出去了，异常逃出去只会让 Starlette 重置连接并打日志。
            logger.exception("积分实时流异常，已断开")
        finally:
            # 必须同步收尾：这里加了 await 会在已取消的作用域里挂住。
            events.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",   # 有反代时禁掉缓冲，否则流会被攒着不发
    })


# ---------- 题目管理 ----------

class QuestionIn(BaseModel):
    question: str
    option_a: str = ""
    option_b: str = ""
    option_c: str = ""
    option_d: str = ""
    answer: str  # A/B/C/D


class QuestionUpdate(BaseModel):
    question: Optional[str] = None
    option_a: Optional[str] = None
    option_b: Optional[str] = None
    option_c: Optional[str] = None
    option_d: Optional[str] = None
    answer: Optional[str] = None


@router.get("/api/admin/courses/{cid}/questions")
def list_questions(cid: int, _: dict = Depends(require_roles(*COURSE_ADMINS))):
    rows = db.query(
        "SELECT * FROM quiz_questions WHERE course_id = %s ORDER BY id ASC", (cid,)
    )
    return {"questions": rows}


@router.post("/api/admin/courses/{cid}/questions")
def create_question(cid: int, payload: QuestionIn, user: dict = Depends(require_roles(*COURSE_ADMINS))):
    qid = db.insert(
        "INSERT INTO quiz_questions (course_id, question, option_a, option_b, option_c, option_d, answer) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (cid, payload.question, payload.option_a, payload.option_b, payload.option_c,
         payload.option_d, payload.answer),
    )
    logic.log_audit(user["emp_id"], "create_question", "question", qid, {"course_id": cid})
    return {"ok": True, "id": qid}


@router.put("/api/admin/questions/{qid}")
def update_question(qid: int, payload: QuestionUpdate, user: dict = Depends(require_roles(*COURSE_ADMINS))):
    data = payload.model_dump(exclude_unset=True)
    _apply_update("quiz_questions", qid, data,
                  {"question", "option_a", "option_b", "option_c", "option_d", "answer"})
    logic.log_audit(user["emp_id"], "update_question", "question", qid, data)
    return {"ok": True}


@router.delete("/api/admin/questions/{qid}")
def delete_question(qid: int, user: dict = Depends(require_roles(*COURSE_ADMINS))):
    db.execute("DELETE FROM quiz_questions WHERE id = %s", (qid,))
    logic.log_audit(user["emp_id"], "delete_question", "question", qid)
    return {"ok": True}
