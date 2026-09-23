"""用户侧业务接口：首页 / 课程 / 礼品 / 活动公告 / 积分 / 订单 / 搜索 / 通知 / 答题。"""
from __future__ import annotations

from typing import List

import pymysql
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .. import db, logic, redemption, search_index
from ..auth import get_current_user

router = APIRouter()


@router.get("/api/user/home")
def home(user: dict = Depends(get_current_user)):
    carousel = db.query(
        "SELECT id, title, subtitle, emoji FROM activities "
        "WHERE status = 'active' AND is_carousel = 1 ORDER BY carousel_order ASC"
    )
    announcements = db.query(
        "SELECT id, title, category, summary, points, created_at FROM announcements "
        "WHERE status = 'active' ORDER BY id DESC LIMIT 5"
    )
    hot_courses = db.query(
        "SELECT c.id, c.title, c.category, c.level, c.points, c.emoji, COUNT(tp.id) AS enrolls "
        "FROM courses c LEFT JOIN training_progress tp ON tp.course_id = c.id "
        "WHERE c.status = 'active' GROUP BY c.id ORDER BY enrolls DESC, c.id DESC LIMIT 5"
    )
    return {"carousel": carousel, "announcements": announcements, "hot_courses": hot_courses}


@router.get("/api/user/courses")
def courses(user: dict = Depends(get_current_user)):
    rows = db.query(
        "SELECT c.id, c.title, c.category, c.level, c.duration, c.instructor, c.points, "
        "c.description, c.emoji, "
        "COALESCE(tp.enrolled, 0) AS my_enrolled, COALESCE(tp.completed, 0) AS my_completed, "
        "COALESCE(tp.progress, 0) AS my_progress "
        "FROM courses c LEFT JOIN training_progress tp ON tp.course_id = c.id AND tp.emp_id = %s "
        "WHERE c.status = 'active' ORDER BY c.id DESC",
        (user["emp_id"],),
    )
    return {"courses": rows}


@router.get("/api/user/gifts")
def gifts(user: dict = Depends(get_current_user)):
    rows = db.query(
        "SELECT g.id, g.name, g.category, g.points_cost, g.stock, g.icon, "
        "(SELECT COUNT(*) FROM redemptions r WHERE r.gift_id = g.id) AS redeemed "
        "FROM gifts g WHERE g.status = 'active' ORDER BY g.id ASC"
    )
    return {"gifts": rows}


@router.get("/api/user/activities")
def activities(user: dict = Depends(get_current_user)):
    acts = db.query(
        "SELECT a.id, a.title, a.subtitle, a.event_time, a.location, a.points, "
        "a.description, a.emoji, (ap.id IS NOT NULL) AS my_participated "
        "FROM activities a LEFT JOIN activity_participants ap "
        "ON ap.activity_id = a.id AND ap.emp_id = %s "
        "WHERE a.status = 'active' ORDER BY a.id DESC",
        (user["emp_id"],),
    )
    anns = db.query(
        "SELECT a.id, a.title, a.category, a.summary, a.content, a.points, a.created_at, "
        "(ar.id IS NOT NULL) AS my_read "
        "FROM announcements a LEFT JOIN announcement_reads ar "
        "ON ar.announcement_id = a.id AND ar.emp_id = %s "
        "WHERE a.status = 'active' ORDER BY a.id DESC",
        (user["emp_id"],),
    )
    return {"activities": acts, "announcements": anns}


@router.get("/api/user/points")
def points(user: dict = Depends(get_current_user)):
    logic.log_event_for(user, "points_view")
    bal = db.query_one(
        "SELECT balance FROM point_accounts WHERE emp_id = %s", (user["emp_id"],)
    )
    records = db.query(
        "SELECT id, points, note, ref_type, ref_id, created_at FROM point_records "
        "WHERE emp_id = %s ORDER BY id DESC LIMIT 100",
        (user["emp_id"],),
    )
    return {"points": bal["balance"] if bal else 0, "records": records}


@router.get("/api/user/orders")
def my_orders(user: dict = Depends(get_current_user)):
    rows = db.query(
        "SELECT r.id, r.gift_id, r.points_cost, r.status, r.express, r.created_at, r.shipped_at, "
        "g.name AS gift_name, g.icon AS gift_icon "
        "FROM redemptions r JOIN gifts g ON g.id = r.gift_id "
        "WHERE r.emp_id = %s ORDER BY r.id DESC",
        (user["emp_id"],),
    )
    return {"orders": rows}


# ---------- 详情 + 浏览埋点 ----------

@router.get("/api/user/courses/{cid}")
def course_detail(cid: int, user: dict = Depends(get_current_user)):
    row = db.query_one(
        "SELECT c.*, COALESCE(tp.enrolled,0) AS my_enrolled, COALESCE(tp.completed,0) AS my_completed, "
        "COALESCE(tp.progress,0) AS my_progress "
        "FROM courses c LEFT JOIN training_progress tp ON tp.course_id = c.id AND tp.emp_id = %s "
        "WHERE c.id = %s AND c.status = 'active'",
        (user["emp_id"], cid),
    )
    if not row:
        raise HTTPException(404, "课程不存在或已下架")
    logic.log_event_for(user, "course_view", "course", cid)
    return {"course": row}


@router.get("/api/user/gifts/{gid}")
def gift_detail(gid: int, user: dict = Depends(get_current_user)):
    row = db.query_one(
        "SELECT * FROM gifts WHERE id = %s AND status = 'active'", (gid,)
    )
    if not row:
        raise HTTPException(404, "礼品不存在或已下架")
    logic.log_event_for(user, "gift_view", "gift", gid)
    return {"gift": row}


@router.get("/api/user/announcements/{aid}")
def announcement_detail(aid: int, user: dict = Depends(get_current_user)):
    row = db.query_one(
        "SELECT a.*, (ar.id IS NOT NULL) AS my_read "
        "FROM announcements a LEFT JOIN announcement_reads ar "
        "ON ar.announcement_id = a.id AND ar.emp_id = %s "
        "WHERE a.id = %s AND a.status = 'active'",
        (user["emp_id"], aid),
    )
    if not row:
        raise HTTPException(404, "公告不存在或已下架")
    logic.log_event_for(user, "announcement_view", "announcement", aid)
    return {"announcement": row}


@router.get("/api/user/activities/{aid}")
def activity_detail(aid: int, user: dict = Depends(get_current_user)):
    row = db.query_one(
        "SELECT a.*, (ap.id IS NOT NULL) AS my_participated "
        "FROM activities a LEFT JOIN activity_participants ap "
        "ON ap.activity_id = a.id AND ap.emp_id = %s "
        "WHERE a.id = %s AND a.status = 'active'",
        (user["emp_id"], aid),
    )
    if not row:
        raise HTTPException(404, "活动不存在或已下架")
    logic.log_event_for(user, "activity_view", "activity", aid)
    return {"activity": row}


# ---------- 写操作 ----------

@router.post("/api/user/courses/{cid}/enroll")
def enroll(cid: int, user: dict = Depends(get_current_user)):
    emp = user["emp_id"]

    def fn(cur):
        cur.execute("SELECT id FROM courses WHERE id = %s AND status = 'active'", (cid,))
        if not cur.fetchone():
            return {"ok": False, "reason": "not_found"}
        cur.execute(
            "SELECT id FROM training_progress WHERE course_id = %s AND emp_id = %s", (cid, emp)
        )
        if cur.fetchone():
            return {"ok": False, "reason": "already_enrolled"}
        cur.execute(
            "INSERT INTO training_progress (course_id, emp_id, enrolled, progress, completed) "
            "VALUES (%s, %s, 1, 0, 0)",
            (cid, emp),
        )
        return {"ok": True}

    try:
        return db.run_tx(fn)
    except pymysql.err.IntegrityError:
        return {"ok": False, "reason": "already_enrolled"}


@router.post("/api/user/courses/{cid}/complete")
def complete_course(cid: int, user: dict = Depends(get_current_user)):
    """HTTP 入口：先过奖励资格门（必须已报名），再交给领域核心发奖。

    资格判定只放在路由这一层，不塞进 complete —— complete 要保证的是幂等
    （同一课程重复调用只发一次奖），而「没报名就不该白拿积分」是接口暴露
    策略。两层混在一个函数里，就没法单独验证幂等了。
    """
    emp = user["emp_id"]
    tp = db.query_one(
        "SELECT enrolled FROM training_progress WHERE course_id = %s AND emp_id = %s",
        (cid, emp),
    )
    if not tp or not tp["enrolled"]:
        return {"ok": False, "reason": "not_enrolled"}
    return complete(cid, user)


def complete(cid: int, user: dict):
    """领域核心：幂等地发放课程奖励（不查报名资格，由调用方分层把关）。"""
    emp = user["emp_id"]

    def fn(cur):
        cur.execute("SELECT * FROM courses WHERE id = %s AND status = 'active'", (cid,))
        c = cur.fetchone()
        if not c:
            return {"ok": False, "reason": "not_found"}
        cur.execute(
            "SELECT * FROM training_progress WHERE course_id = %s AND emp_id = %s", (cid, emp)
        )
        tp = cur.fetchone()
        if tp and tp["completed"]:
            return {"ok": False, "reason": "already_completed"}
        if tp:
            cur.execute(
                "UPDATE training_progress SET progress = 100, completed = 1 "
                "WHERE course_id = %s AND emp_id = %s",
                (cid, emp),
            )
        else:
            cur.execute(
                "INSERT INTO training_progress (course_id, emp_id, enrolled, progress, completed) "
                "VALUES (%s, %s, 1, 100, 1)",
                (cid, emp),
            )
        logic.add_points(cur, emp, c["points"], "完成课程", "course", cid)
        return {"ok": True, "points": c["points"]}

    try:
        return db.run_tx(fn)
    except pymysql.err.IntegrityError:
        return {"ok": False, "reason": "already_completed"}


@router.post("/api/user/activities/{aid}/participate")
def participate(aid: int, user: dict = Depends(get_current_user)):
    emp = user["emp_id"]

    def fn(cur):
        cur.execute("SELECT * FROM activities WHERE id = %s AND status = 'active'", (aid,))
        a = cur.fetchone()
        if not a:
            return {"ok": False, "reason": "not_found"}
        cur.execute(
            "SELECT id FROM activity_participants WHERE activity_id = %s AND emp_id = %s",
            (aid, emp),
        )
        if cur.fetchone():
            return {"ok": False, "reason": "already_participated"}
        cur.execute(
            "INSERT INTO activity_participants (activity_id, emp_id) VALUES (%s, %s)", (aid, emp)
        )
        logic.add_points(cur, emp, a["points"], "参与活动", "activity", aid)
        return {"ok": True, "points": a["points"]}

    try:
        return db.run_tx(fn)
    except pymysql.err.IntegrityError:
        return {"ok": False, "reason": "already_participated"}


@router.post("/api/user/announcements/{aid}/read")
def read_announcement(aid: int, user: dict = Depends(get_current_user)):
    emp = user["emp_id"]

    def fn(cur):
        cur.execute("SELECT * FROM announcements WHERE id = %s AND status = 'active'", (aid,))
        a = cur.fetchone()
        if not a:
            return {"ok": False, "reason": "not_found"}
        cur.execute(
            "SELECT id FROM announcement_reads WHERE announcement_id = %s AND emp_id = %s",
            (aid, emp),
        )
        if cur.fetchone():
            return {"ok": False, "reason": "already_read"}
        cur.execute(
            "INSERT INTO announcement_reads (announcement_id, emp_id) VALUES (%s, %s)", (aid, emp)
        )
        logic.add_points(cur, emp, a["points"], "阅读公告", "announcement", aid)
        return {"ok": True, "points": a["points"]}

    try:
        return db.run_tx(fn)
    except pymysql.err.IntegrityError:
        return {"ok": False, "reason": "already_read"}


@router.post("/api/user/gifts/{gid}/redeem")
def redeem(gid: int, payload: redemption.RequestIn, user: dict = Depends(get_current_user)):
    return redemption.redeem(gid, payload, user["emp_id"])


@router.post("/api/user/orders/{oid}/cancel")
def cancel_order(oid: int, payload: redemption.CancelIn, user: dict = Depends(get_current_user)):
    return redemption.refund(oid, payload.reason, user["emp_id"], owner_only=True)


@router.get("/api/search")
def search(q: str = Query(..., min_length=1, max_length=50), user: dict = Depends(get_current_user)):
    """走倒排索引：多词交集召回 + 字段加权排序（见 search_index 模块头注释）。"""
    result = search_index.search(q)
    logic.log_event_for(user, "search", properties={"keyword": q})
    return {"query": q, **result}


@router.post("/api/analytics/visit")
def visit(user: dict = Depends(get_current_user)):
    logic.log_event_for(user, "page_visit")
    return {"ok": True}


# ---------- 站内信通知 ----------

@router.get("/api/user/notifications")
def list_notifications(user: dict = Depends(get_current_user)):
    rows = db.query(
        "SELECT id, title, content, ntype, ref_id, is_read, created_at FROM notifications "
        "WHERE emp_id = %s ORDER BY id DESC LIMIT 50",
        (user["emp_id"],),
    )
    return {"notifications": rows}


@router.get("/api/user/notifications/unread")
def unread_count(user: dict = Depends(get_current_user)):
    c = db.query_one(
        "SELECT COUNT(*) AS c FROM notifications WHERE emp_id = %s AND is_read = 0",
        (user["emp_id"],),
    )["c"]
    return {"count": c}


@router.post("/api/user/notifications/read")
def mark_notifications_read(user: dict = Depends(get_current_user)):
    db.execute(
        "UPDATE notifications SET is_read = 1 WHERE emp_id = %s AND is_read = 0",
        (user["emp_id"],),
    )
    return {"ok": True}


# ---------- 课程答题 ----------

# 答题及格线（百分比）：完成课程先发的奖励，只有及格才保留，不及格则被撤销。
QUIZ_PASS_PERCENT = 60


class QuizAnswer(BaseModel):
    qid: int
    answer: str


class QuizSubmit(BaseModel):
    answers: List[QuizAnswer]


@router.get("/api/user/courses/{cid}/quiz")
def get_quiz(cid: int, user: dict = Depends(get_current_user)):
    tp = db.query_one(
        "SELECT completed FROM training_progress WHERE course_id = %s AND emp_id = %s",
        (cid, user["emp_id"]),
    )
    if not tp or not tp["completed"]:
        raise HTTPException(400, "请先完成课程再答题")
    questions = db.query(
        "SELECT id, question, option_a, option_b, option_c, option_d "
        "FROM quiz_questions WHERE course_id = %s ORDER BY id ASC",
        (cid,),
    )
    return {"questions": questions}


@router.post("/api/user/courses/{cid}/quiz")
def submit_quiz(cid: int, payload: QuizSubmit, user: dict = Depends(get_current_user)):
    tp = db.query_one(
        "SELECT completed FROM training_progress WHERE course_id = %s AND emp_id = %s",
        (cid, user["emp_id"]),
    )
    if not tp or not tp["completed"]:
        raise HTTPException(400, "请先完成课程再答题")
    questions = db.query(
        "SELECT id, answer FROM quiz_questions WHERE course_id = %s ORDER BY id ASC", (cid,)
    )
    if not questions:
        raise HTTPException(400, "该课程暂无题目")
    ans_map = {a.qid: a.answer.upper() for a in payload.answers}
    score = 0
    results = []
    for q in questions:
        chosen = ans_map.get(q["id"], "")
        correct = chosen == q["answer"]
        if correct:
            score += 1
        results.append({"qid": q["id"], "your": chosen, "answer": q["answer"], "correct": correct})
    db.insert(
        "INSERT INTO quiz_attempts (emp_id, course_id, score, total) VALUES (%s, %s, %s, %s)",
        (user["emp_id"], cid, score, len(questions)),
    )
    # 答题不及格 → 撤销之前“完成课程”已发放的奖励。
    # 奖励资格不能只看“点没点完成”，还得看学习结果；否则挂完课刷分就能白拿。
    total = len(questions)
    if not total or score * 100 < total * QUIZ_PASS_PERCENT:
        _revert_course_reward(cid, user["emp_id"])
    return {"score": score, "total": total, "results": results}


def _revert_course_reward(cid: int, emp_id: str) -> int:
    """删掉该用户该课程的“完成课程”流水并同额回扣余额（幂等，无流水时返回 0）。

    只改积分（流水 + 余额），不动 training_progress：completed 标记反映的是
    “学过”，撤销只针对“白拿的奖励”。两者分开才不会把用户的答题入口一并遮掉。
    """

    def fn(cur):
        cur.execute(
            "SELECT id, points FROM point_records "
            "WHERE emp_id = %s AND ref_type = 'course' AND ref_id = %s",
            (emp_id, cid),
        )
        row = cur.fetchone()
        if not row:
            return 0
        cur.execute("DELETE FROM point_records WHERE id = %s", (row["id"],))
        # 发放时是 balance + points，撤销就是反向同额减回（points 为正数时减）。
        cur.execute(
            "UPDATE point_accounts SET balance = balance - %s WHERE emp_id = %s",
            (row["points"], emp_id),
        )
        return row["points"]

    return db.run_tx(fn)
