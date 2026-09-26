"""数据看板 + 埋点上报入口。

所有指标口径从 metrics.py 取，本文件不自己写聚合 SQL —— 口径散落各写各的，
早晚会漂移出两套算法。
"""
from __future__ import annotations

import json
from typing import List, Optional

import pymysql
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .. import db, logic, metrics
from ..auth import get_current_user, require_admin

router = APIRouter()

MAX_BATCH = 200


# ---------- 埋点上报：批量 + 幂等 ----------

class TrackEventIn(BaseModel):
    event_id: str = Field(..., min_length=8, max_length=96)  # 客户端生成的幂等键
    event_type: str = Field(..., max_length=32)
    ref_type: str = Field("", max_length=32)
    ref_id: Optional[int] = None
    properties: Optional[dict] = None
    client_time: Optional[str] = None


class TrackBatchIn(BaseModel):
    session_id: str = Field("", max_length=64)
    events: List[TrackEventIn] = Field(..., max_length=MAX_BATCH)


@router.post("/api/analytics/events")
def track_batch(payload: TrackBatchIn, user: dict = Depends(get_current_user)):
    """批量上报入口：客户端本地队列攒一批发过来，发失败下次重投。

    为什么批量而不是逐条：断网 / 关页面时逐条发的丢失面更大，批量 + 重投能把
    「丢数」收敛成「最多晚一个 flush 周期」。
    幂等靠 event_id 上的唯一约束 uk_event：重投同一条会命中唯一键，MySQL 抛
    1062，这里当「已收过」计数，不会重复计数。
    """
    session_id = payload.session_id or user.get("session_id", "")
    accepted = duplicated = rejected = 0

    def fn(cur):
        nonlocal accepted, duplicated, rejected
        for ev in payload.events:
            if ev.event_type not in logic.ALLOWED_EVENTS:
                rejected += 1  # 白名单外的事件直接丢，不让脏事件进库污染口径
                continue
            try:
                cur.execute(
                    "INSERT INTO user_access_logs "
                    "(event_id, session_id, emp_id, event_type, ref_type, ref_id, "
                    " properties, client_time) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE id = id",
                    (ev.event_id, session_id, user["emp_id"], ev.event_type, ev.ref_type,
                     ev.ref_id,
                     json.dumps(ev.properties, ensure_ascii=False) if ev.properties else None,
                     logic.parse_client_time(ev.client_time)),
                )
            except pymysql.err.IntegrityError as exc:
                if logic.is_duplicate(exc):
                    # MySQL 里唯一键冲突不会中止整个事务，可以继续处理后面的批次
                    duplicated += 1
                    continue
                raise
            if cur.rowcount == 1:
                accepted += 1
            else:
                duplicated += 1

    db.run_tx(fn)
    return {"ok": True, "accepted": accepted,
            "duplicated": duplicated, "rejected": rejected}


@router.get("/api/admin/metrics-defs")
def metrics_defs(_: dict = Depends(require_admin)):
    """口径说明书：看板上每个数字是怎么算出来的，前端取去做 tooltip。"""
    return {"defs": list(metrics.METRIC_DEFS)}


@router.get("/api/admin/dashboard")
def dashboard(_: dict = Depends(require_admin)):
    overview = {
        "total_users": db.query_one("SELECT COUNT(*) AS c FROM users")["c"],
        "today_visits": metrics.dau(),
        "wau": metrics.wau(),
        "active_courses": db.query_one(
            "SELECT COUNT(*) AS c FROM courses WHERE status = 'active'")["c"],
        "active_activities": db.query_one(
            "SELECT COUNT(*) AS c FROM activities WHERE status = 'active'")["c"],
        "active_gifts": db.query_one(
            "SELECT COUNT(*) AS c FROM gifts WHERE status = 'active'")["c"],
        # 口径一律走 metrics（见 metrics.py 头部约定）：这里曾经手写聚合，
        # 和 metrics.points_spend_rate() 是两套算法，加进回滚/校平后必然一起漂移。
        "points_issued": metrics.points_issued(),
        "points_spent": metrics.points_spent(),
        "total_orders": db.query_one("SELECT COUNT(*) AS c FROM redemptions")["c"],
        "pending_orders": db.query_one(
            "SELECT COUNT(*) AS c FROM redemptions WHERE status = 'pending'")["c"],
        "shipped_orders": db.query_one(
            "SELECT COUNT(*) AS c FROM redemptions WHERE status = 'shipped'")["c"],
        "course_enrolls": db.query_one(
            "SELECT COUNT(*) AS c FROM training_progress")["c"],
        "course_completions": db.query_one(
            "SELECT COUNT(*) AS c FROM training_progress WHERE completed = 1")["c"],
        "activity_parts": db.query_one(
            "SELECT COUNT(*) AS c FROM activity_participants")["c"],
        "announcement_reads": db.query_one(
            "SELECT COUNT(*) AS c FROM announcement_reads")["c"],
    }

    daily_points = metrics.daily_points(7)

    # 带出 stock / points_cost：积分 tab 要在同一张卡里讲「礼品是积分花掉去哪了」，
    # 而 /api/admin/gifts 挂在 GIFT_ADMINS 上（content_admin 和 viewer 都读不到），
    # 所以不能靠二次请求补库存 —— 走看板自己的载荷，四种管理角色都拿得到。
    top_gifts = db.query(
        "SELECT g.id, g.name, g.icon, g.stock, g.points_cost, COUNT(r.id) AS c "
        "FROM redemptions r JOIN gifts g ON g.id = r.gift_id "
        "GROUP BY g.id, g.name, g.icon, g.stock, g.points_cost ORDER BY c DESC LIMIT 5"
    )

    recent_orders = db.query(
        "SELECT r.id, r.status, r.points_cost, r.created_at, "
        "g.name AS gift_name, g.icon AS gift_icon, u.name AS user_name "
        "FROM redemptions r JOIN gifts g ON g.id = r.gift_id JOIN users u ON u.emp_id = r.emp_id "
        "ORDER BY r.id DESC LIMIT 8"
    )

    # 曝光量（次数）：看「有多少次机会」
    funnel = {
        "search": metrics.event_count("search"),
        "course_view": metrics.event_count("course_view", "course"),
        "course_enroll": db.query_one("SELECT COUNT(*) AS c FROM training_progress")["c"],
        "course_complete": db.query_one("SELECT COUNT(*) AS c FROM training_progress WHERE completed = 1")["c"],
        "gift_view": metrics.event_count("gift_view", "gift"),
        "redeem": db.query_one("SELECT COUNT(*) AS c FROM redemptions")["c"],
        "announcement_view": metrics.event_count("announcement_view", "announcement"),
        "announcement_read": db.query_one("SELECT COUNT(*) AS c FROM announcement_reads")["c"],
        "points_view": metrics.event_count("points_view"),
        "activity_participants": db.query_one("SELECT COUNT(*) AS c FROM activity_participants")["c"],
    }

    # 人数口径（去重工号）：转化率统一走这里，不混用次数，
    # 否则「1 个人点 10 次」会把转化率算成 10 倍。
    funnel_users = {
        "course_view": metrics.event_users("course_view", "course"),
        "course_enroll": metrics.distinct_users("training_progress"),
        "course_complete": metrics.distinct_users("training_progress", "completed = 1"),
        "gift_view": metrics.event_users("gift_view", "gift"),
        "redeem": metrics.distinct_users("redemptions"),
        "announcement_view": metrics.event_users("announcement_view", "announcement"),
        "announcement_read": metrics.distinct_users("announcement_reads"),
        "activity_participants": metrics.distinct_users("activity_participants"),
    }

    spent_points, issued_points, spend_rate = metrics.points_spend_rate()
    rates = {
        "course_view_to_enroll": metrics.pct(
            funnel_users["course_enroll"], funnel_users["course_view"]),
        "course_complete": metrics.pct(
            funnel_users["course_complete"], funnel_users["course_enroll"]),
        "gift_redeem": metrics.pct(funnel_users["redeem"], funnel_users["gift_view"]),
        "announcement_read": metrics.pct(
            funnel_users["announcement_read"], funnel_users["announcement_view"]),
        # 积分消耗率：判断积分「发得出、花得掉」，过低=礼品吸引力不足
        "points_spend": spend_rate,
    }

    # 搜索热词 TOP（解析埋点 properties 里的 keyword）
    search_keywords = db.query(
        "SELECT JSON_UNQUOTE(JSON_EXTRACT(properties, '$.keyword')) AS kw, COUNT(*) AS c "
        "FROM user_access_logs WHERE event_type = 'search' AND properties IS NOT NULL "
        "GROUP BY kw ORDER BY c DESC, kw ASC LIMIT 10"
    )

    # 课程答题正确率
    _acc = db.query(
        "SELECT c.id, c.title, c.emoji, AVG(qa.score / qa.total) AS acc, COUNT(qa.id) AS attempts "
        "FROM quiz_attempts qa JOIN courses c ON c.id = qa.course_id "
        "GROUP BY c.id, c.title, c.emoji ORDER BY attempts DESC, acc ASC LIMIT 10"
    )
    course_accuracy = [
        {"id": r["id"], "title": r["title"], "emoji": r["emoji"],
         "accuracy": round(float(r["acc"]) * 100, 1), "attempts": r["attempts"]}
        for r in _acc
    ]

    # 发货时效（下单 → 发货，小时）
    _ship = db.query_one(
        "SELECT AVG(TIMESTAMPDIFF(HOUR, created_at, shipped_at)) AS avg_hours, COUNT(*) AS c "
        "FROM redemptions WHERE status = 'shipped' AND shipped_at IS NOT NULL"
    )
    shipping = {
        "avg_hours": round(float(_ship["avg_hours"]), 1) if _ship["avg_hours"] is not None else 0.0,
        "c": _ship["c"],
    }

    # 课程 / 礼品点击热度 TOP（浏览埋点）
    top_course_views = db.query(
        "SELECT c.id, c.title, c.emoji, COUNT(*) AS views FROM user_access_logs ual "
        "JOIN courses c ON c.id = ual.ref_id "
        "WHERE ual.event_type = 'course_view' AND ual.ref_type = 'course' "
        "GROUP BY c.id, c.title, c.emoji ORDER BY views DESC LIMIT 5"
    )
    top_gift_views = db.query(
        "SELECT g.id, g.name, g.icon, COUNT(*) AS views FROM user_access_logs ual "
        "JOIN gifts g ON g.id = ual.ref_id "
        "WHERE ual.event_type = 'gift_view' AND ual.ref_type = 'gift' "
        "GROUP BY g.id, g.name, g.icon ORDER BY views DESC LIMIT 5"
    )

    # 会话漏斗：与上面的人数口径并列，回答「同一次访问里能不能走到最后一步」。
    # 空串会话的排除规则只在 metrics 里有一份，这里不自己写聚合 SQL。
    session_funnel = metrics.session_funnel(7)

    return {
        "overview": overview,
        "daily_points": daily_points,
        "top_gifts": top_gifts,
        "recent_orders": recent_orders,
        "funnel": funnel,
        "funnel_users": funnel_users,
        "rates": rates,
        "points_rate": {"spent": spent_points, "issued": issued_points, "rate": spend_rate},
        "search_keywords": search_keywords,
        "course_accuracy": course_accuracy,
        "shipping": shipping,
        "top_course_views": top_course_views,
        "top_gift_views": top_gift_views,
        "session_funnel": session_funnel,
    }
