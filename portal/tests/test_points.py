"""积分明细：人工发放/扣减、回滚、校平，以及实时推送的边界。

跑法（在 运营门户 目录下，portal 的上一级）：
    python -m unittest discover -s portal/tests -v

未启用隔离 MySQL 时跳过；显式启用后的连接、迁移错误必须失败。

为什么直接调路由函数而不是走 HTTP：项目没有装 httpx，starlette.testclient
导入即抛 RuntimeError；装它又违反「零新增依赖」的约定。所以这里直接调
admin.adjust_points(...) 这类函数，用 assertRaises 断言 HTTPException 的状态码 ——
路由函数拿到的 user 字典就是权限依赖的返回值，权限判定本身在 auth.require_roles
里，这里只验证「路由在自己的分支上有没有正确拒绝」。
"""
from __future__ import annotations

import asyncio
from collections import Counter, deque
import inspect
import time
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi import HTTPException, Request

from portal.tests import init_test_db
from portal.backend import auth, db, events, logic, metrics, redemption
from portal.backend.routers import admin, user as user_api
from portal.tests.test_redemption import evidence, http_request, writes, InjectedFailure

TARGET = "ZZTP0001"      # 测试用户工号
OPERATOR = "ZZTP_OPERATOR"  # 仅供本组测试使用的操作人
USERNAME = "zztp_user01"


def _list(**kw):
    """直接调路由函数时，Query(...) 的默认值不会被解析成真实值，
    传下去的是一个 Query 对象 —— 所有查询参数都得显式给，一个都不能漏。
    漏掉 ref_type 时会拿到 Query 对象而不是 ""，接口直接 400「未知的流水类型」，
    报错信息看着像参数传错了，其实只是绕过 FastAPI 的必然结果。

    不注入 user：读接口把权限参数写成 `_`（它只是给 Depends 挂载点用的），
    传 user= 会直接 TypeError。
    """
    for k, v in (("page", 1), ("size", 20), ("emp_id", ""), ("ref_type", ""),
                 ("direction", ""), ("days", 0)):
        kw.setdefault(k, v)
    return admin.list_points(**kw)


# 直接调路由函数时，Depends(...) 的默认值同样不会被解析（传下去的是 Depends 对象），
# 所以 user 必须显式注入。传进去的字典就是权限依赖本来会返回的东西 ——
# 也就是说这几行等于在声明「以这个身份调用」。权限判定本身在 auth 里，
# 由 test_读写分权 单独验证。
SUPER_USER = {"emp_id": OPERATOR, "role": "super_admin", "name": "超级管理员"}
VIEWER_USER = {"emp_id": "1006", "role": "viewer", "name": "只读运营"}


def _adjust(payload, **kw):
    kw.setdefault("user", SUPER_USER)
    return admin.adjust_points(payload, **kw)


def _revert(rid, payload, **kw):
    kw.setdefault("user", SUPER_USER)
    return admin.revert_points(rid, payload, **kw)


def _reconcile(payload, **kw):
    kw.setdefault("user", SUPER_USER)
    return admin.reconcile_points(payload, **kw)


def _drift(**kw):
    """同上，读接口的参数名是 `_`，不能传 user=。"""
    return admin.points_drift(**kw)


def _cleanup():
    db.assert_test_database()
    db.execute("DELETE FROM point_records WHERE emp_id IN (%s, %s)", (TARGET, OPERATOR))
    db.execute("DELETE FROM point_accounts WHERE emp_id IN (%s, %s)", (TARGET, OPERATOR))
    db.execute("DELETE FROM point_ops WHERE target_emp_id = %s", (TARGET,))
    db.execute("DELETE FROM audit_logs WHERE emp_id = %s AND action LIKE 'points_%%'", (OPERATOR,))
    db.execute("DELETE FROM users WHERE emp_id IN (%s, %s)", (TARGET, OPERATOR))


def _balance() -> int:
    row = db.query_one("SELECT balance FROM point_accounts WHERE emp_id = %s", (TARGET,))
    return int(row["balance"]) if row else 0


def _sum_records() -> int:
    row = db.query_one("SELECT COALESCE(SUM(points), 0) AS s FROM point_records WHERE emp_id = %s",
                       (TARGET,))
    return int(row["s"])


def _rid(n: int = 8) -> str:
    return f"zzreq-{n}-{time.time_ns()}"


def _system_record(points: int, ref_type: str = "course") -> int:
    """造一笔「系统产生的」流水（完成课程这类），返回 point_records.id。

    为什么需要它：回滚只允许针对系统流水，人工发放产生的流水被显式拒绝 ——
    所以用 _adjust() 造出来的回滚靶子一定是 400，测不下去。

    这里不走 add_points，而是手写两条语句，效果等价（写流水 + 加余额）：
    直接 INSERT 让 ref_id 保持业务对象 id 的语义，也正是要覆盖的那个语境。
    """
    db.execute("INSERT IGNORE INTO point_accounts (emp_id, balance) VALUES (%s, 0)", (TARGET,))
    ref_id = 900000 + (time.time_ns() % 100000)
    rid = db.insert(
        "INSERT INTO point_records (emp_id, points, note, ref_type, ref_id) "
        "VALUES (%s, %s, %s, %s, %s)",
        (TARGET, points, "完成课程", ref_type, ref_id))
    db.execute("UPDATE point_accounts SET balance = balance + %s WHERE emp_id = %s",
               (points, TARGET))
    return rid


class TestPointsAdmin(unittest.TestCase):
    """人工积分操作，只在显式启用的隔离数据库执行。"""

    @classmethod
    def setUpClass(cls):
        init_test_db()
        _cleanup()
        db.execute(
            "INSERT INTO users (emp_id, username, password_hash, name, role, department) "
            "VALUES (%s, %s, %s, %s, 'user', '测试部')",
            (TARGET, USERNAME, "x", "积分测试用户"),
        )
        db.execute(
            "INSERT INTO users (emp_id, username, password_hash, name, role) "
            "VALUES (%s, %s, 'x', '测试管理员', 'super_admin')", (OPERATOR, OPERATOR))

    @classmethod
    def tearDownClass(cls):
        _cleanup()

    def setUp(self):
        _cleanup_points_only()

    def make_course(self):
        cid = db.insert("INSERT INTO courses (title,points) VALUES ('奖励资格验收',30)")
        def cleanup():
            db.assert_test_database()
            for table in ("quiz_attempts", "quiz_questions", "training_progress"):
                db.execute(f"DELETE FROM {table} WHERE course_id=%s", (cid,))
            db.execute("DELETE FROM courses WHERE id=%s", (cid,))
        self.addCleanup(cleanup)
        return cid

    def test_未报名未学习未答题不得领取课程积分(self):
        """按此前建议的奖励资格验收；缺口保留真实失败，不用预期失败掩盖。"""
        cid = self.make_course()
        token, _ = auth.create_session(TARGET)
        try:
            status, result = asyncio.run(http_request(f"/api/user/courses/{cid}/complete", {}, token))
        finally:
            auth.drop_session(token)
        progress = db.query_one("SELECT enrolled,progress,completed FROM training_progress "
                                "WHERE course_id=%s AND emp_id=%s", (cid, TARGET))
        evidence("未报名未答题直接完成", HTTP状态=status, 响应=result, 学习记录=progress,
                 实际余额=_balance(), 应发积分=0)
        self.assertEqual(_balance(), 0, "没有报名和学习结果，调用 complete 仍获得积分")

    def test_答题零分不得保留课程奖励(self):
        cid = self.make_course()
        qid = db.insert("INSERT INTO quiz_questions (course_id,question,answer) VALUES (%s,'资格测试','A')", (cid,))
        actor = {"emp_id": TARGET}
        user_api.enroll(cid, actor)
        user_api.complete(cid, actor)
        result = user_api.submit_quiz(cid, user_api.QuizSubmit(answers=[{"qid": qid, "answer": "B"}]), actor)
        self.assertEqual((result["score"], result["total"]), (0, 1))
        evidence("课程答题全部错误", 得分=result["score"], 总题数=result["total"],
                 实际余额=_balance(), 应发积分=0)
        self.assertEqual(_balance(), 0, "答题结果没有约束已发放的课程积分")

    def test_课程重复完成只发一次奖励(self):
        cid = self.make_course()
        results = [user_api.complete(cid, {"emp_id": TARGET}) for _ in range(10)]
        self.assertEqual(sum(r["ok"] for r in results), 1)
        self.assertEqual((_balance(), _sum_records()), (30, 30))
        evidence("课程重复领取", 请求数=len(results), 成功数=sum(r["ok"] for r in results), 实际余额=_balance())

    def test_HTTP人工积分权限与会话撤销(self):
        token, _ = auth.create_session(OPERATOR)
        results = []
        try:
            for role in ("user", "viewer", "content_admin", "shop_admin", "super_admin"):
                db.execute("UPDATE users SET role=%s WHERE emp_id=%s", (role, OPERATOR))
                read_status, _ = asyncio.run(http_request("/api/admin/points", token=token, method="GET"))
                body = {"emp_id": TARGET, "points": 1, "reason": "权限验收", "request_id": _rid()}
                write_status, _ = asyncio.run(http_request("/api/admin/points/adjust", body, token))
                results.append({"role": role, "read": read_status, "write": write_status})
                self.assertEqual(read_status, 403 if role == "user" else 200)
                self.assertEqual(write_status, 200 if role == "super_admin" else 403)
            auth.drop_session(token)
            revoked, _ = asyncio.run(http_request("/api/admin/points/adjust", body, token))
            self.assertEqual(revoked, 401)
            self.assertEqual(_balance(), 1)
            evidence("人工积分真实HTTP权限", 角色结果=results, 撤销后状态=revoked, 实际余额=_balance())
        finally:
            auth.drop_session(token)
            db.execute("UPDATE users SET role='super_admin' WHERE emp_id=%s", (OPERATOR,))

    def test_人工积分每个写步骤异常均回滚(self):
        def snapshot():
            return {t: db.query(f"SELECT * FROM {t} ORDER BY {key}") for t, key in
                    (("point_accounts", "emp_id"), ("point_records", "id"), ("point_ops", "id"), ("audit_logs", "id"))}
        for kind in ("grant", "deduct", "revert", "reconcile"):
            def prepare():
                _cleanup_points_only()
                _adjust(admin.AdjustIn(emp_id=TARGET, points=1000, reason="准备余额", request_id=_rid()))
                if kind in ("grant", "deduct"):
                    payload = admin.AdjustIn(emp_id=TARGET, points=30 if kind == "grant" else -30,
                                             reason="故障验收", request_id=_rid())
                    return lambda: _adjust(payload)
                if kind == "revert":
                    rid = _system_record(80)
                    payload = admin.RevertIn(request_id=_rid())
                    return lambda: _revert(rid, payload)
                db.execute("UPDATE point_accounts SET balance=1030 WHERE emp_id=%s", (TARGET,))
                payload = admin.ReconcileIn(emp_id=TARGET, request_id=_rid())
                return lambda: _reconcile(payload)
            operation = prepare()
            with writes() as trace:
                operation()
            for step in range(1, len(trace) + 1):
                with self.subTest(kind=kind, step=step):
                    operation = prepare()
                    before = snapshot()
                    with self.assertRaises(InjectedFailure), writes(fail_at=step):
                        operation()
                    self.assertEqual(snapshot(), before)
                    operation()
                    self.assertEqual(_balance(), _sum_records())
            evidence("人工积分逐步故障", 业务=kind, 注入次数=len(trace), 完整回滚数=len(trace), 重试成功数=len(trace))

    def test_SSE晚提交超过尾窗也不应漏流水(self):
        for ahead in (2, events._TRAILING_WINDOW + 1):
            with self.subTest(先提交条数=ahead):
                watermark = db.query_one("SELECT COALESCE(MAX(id),0) m FROM point_records")["m"]
                emitted = []
                conn = db._connect()
                try:
                    conn.begin()
                    with conn.cursor() as cur:
                        late = logic.add_points(cur, TARGET, 7, "延迟提交", "course", 800000 + ahead)
                    def advance(cur):
                        for index in range(ahead):
                            logic.add_points(cur, OPERATOR, 1, "先提交", "activity", 800000 + ahead * 100 + index)
                    db.run_tx(advance)
                    with patch.object(events, "_published", deque(maxlen=events._DEDUP_SIZE)), \
                         patch.object(events, "_emit", side_effect=emitted.append):
                        watermark = events._poll_once(watermark)
                        conn.commit()
                        for _ in range(3):
                            watermark = events._poll_once(watermark)
                    late_frames = sum(frame["id"] == late for frame in emitted)
                    self.assertIsNotNone(db.query_one("SELECT id FROM point_records WHERE id=%s", (late,)))
                    evidence("SSE提交乱序", 先提交条数=ahead, 尾窗=events._TRAILING_WINDOW,
                             提交后轮询次数=3, 延迟流水已落库=True, 延迟流水推送数=late_frames)
                    self.assertEqual(late_frames, 1, "已提交流水超出尾窗后没有被推送")
                finally:
                    conn.rollback()
                    conn.close()

    # ---- 发放 / 扣减 ----

    def test_发放加技能真实入账(self):
        r = _adjust(admin.AdjustIn(
            emp_id=TARGET, points=100, reason="项目奖励", request_id=_rid()))
        self.assertEqual(r["op_type"], "grant")
        self.assertEqual(r["balance_before"], 0)
        self.assertEqual(r["balance_after"], 100)
        self.assertEqual(_balance(), 100)
        self.assertEqual(_sum_records(), 100)
        # 流水文案要照顾用户端：用户端只渲染 note，裸的 reason 在用户眼里没头没尾
        rec = db.query_one("SELECT note, ref_type, ref_id FROM point_records WHERE id = %s",
                           (r["record_id"],))
        self.assertEqual(rec["note"], "人工发放：项目奖励")
        self.assertEqual(rec["ref_type"], "grant")
        self.assertEqual(rec["ref_id"], r["op_id"])

    def test_扣减按负数入账(self):
        _adjust(admin.AdjustIn(emp_id=TARGET, points=200, reason="先给",
                                           request_id=_rid()))
        r = _adjust(admin.AdjustIn(emp_id=TARGET, points=-50, reason="收回",
                                               request_id=_rid()))
        self.assertEqual(r["op_type"], "deduct")
        self.assertEqual(_balance(), 150)
        self.assertEqual(_sum_records(), 150)

    def test_同一用户可以连续发放多笔(self):
        """回归：point_records 的 uk_ref 是 (emp_id, ref_type, ref_id)，
        如果人工发放照搬 ref_id=0，第二笔就会撞唯一键发不出去。
        靠先插 point_ops 取自增 id 当 ref_id 解决。"""
        for i in range(3):
            _adjust(admin.AdjustIn(emp_id=TARGET, points=10, reason=f"第{i}笔",
                                               request_id=_rid(i)))
        rows = db.query("SELECT id FROM point_records WHERE emp_id = %s", (TARGET,))
        self.assertEqual(len(rows), 3)
        self.assertEqual(_balance(), 30)

    def test_同一request_id重放只入账一次(self):
        rid = _rid()
        _adjust(admin.AdjustIn(emp_id=TARGET, points=100, reason="幂等",
                                           request_id=rid))
        # 重投命中 point_ops.uk_idem → 1062 → 409
        with self.assertRaises(HTTPException) as ctx:
            _adjust(admin.AdjustIn(emp_id=TARGET, points=100, reason="幂等",
                                               request_id=rid))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(_balance(), 100)       # 只动了一次
        self.assertEqual(_sum_records(), 100)

    def test_变动为零被拒(self):
        with self.assertRaises(HTTPException) as ctx:
            _adjust(admin.AdjustIn(emp_id=TARGET, points=0, reason="空",
                                               request_id=_rid()))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(_sum_records(), 0)

    def test_工号不存在时拒绝且不产生孤立账户(self):
        """ensure_account 是 INSERT IGNORE —— 不先校验 users，
        给不存在的工号发放会凭空造出一个查不到姓名的账户和流水。"""
        ghost = "ZZTPNOPE"
        db.execute("DELETE FROM point_accounts WHERE emp_id = %s", (ghost,))
        with self.assertRaises(HTTPException) as ctx:
            _adjust(admin.AdjustIn(emp_id=ghost, points=10, reason="幽灵",
                                               request_id=_rid()))
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIsNone(db.query_one("SELECT emp_id FROM point_accounts WHERE emp_id = %s",
                                       (ghost,)))
        self.assertIsNone(db.query_one("SELECT id FROM point_records WHERE emp_id = %s", (ghost,)))

    def test_扣成负数被拒且什么都不写(self):
        _adjust(admin.AdjustIn(emp_id=TARGET, points=30, reason="底金",
                                           request_id=_rid()))
        with self.assertRaises(HTTPException) as ctx:
            _adjust(admin.AdjustIn(emp_id=TARGET, points=-100, reason="超扣",
                                               request_id=_rid()))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(_balance(), 30)
        self.assertEqual(_sum_records(), 30)
        # 事务整体回滚，连 point_ops 都不该留下
        self.assertEqual(
            db.query_one("SELECT COUNT(*) AS c FROM point_ops WHERE target_emp_id = %s",
                         (TARGET,))["c"], 1)

    # ---- 回滚 ----

    def test_回滚按原样反向记一笔(self):
        rec_id = _system_record(100)
        r2 = _revert(rec_id, admin.RevertIn(reason="发错了", request_id=_rid()))
        self.assertEqual(r2["points"], -100)
        self.assertEqual(_balance(), 0)
        self.assertEqual(_sum_records(), 0)
        ops = db.query_one("SELECT op_type, reverted_record_id, operator_emp_id "
                           "FROM point_ops WHERE id = %s", (r2["op_id"],))
        self.assertEqual(ops["op_type"], "revert")
        self.assertEqual(ops["reverted_record_id"], rec_id)
        self.assertEqual(ops["operator_emp_id"], OPERATOR)
        # 回滚行自己的 ref_id 指向 point_ops（不是被回滚的流水 id），
        # 否则列表里查不出是谁点的回滚
        rev = db.query_one("SELECT ref_type, ref_id FROM point_records WHERE id = %s",
                           (r2["record_id"],))
        self.assertEqual(rev["ref_type"], "revert")
        self.assertEqual(rev["ref_id"], r2["op_id"])

    def test_回滚行的操作人查得出来(self):
        """回归：join point_ops 时回滚行也要能接上。
        如果回滚行的 ref_id 存成「被回滚的流水 id」，这里会显示成「本人」。"""
        rec_id = _system_record(60)
        _revert(rec_id, admin.RevertIn(request_id=_rid()))
        rows = _list(emp_id=TARGET, ref_type="revert")["items"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["operator_label"].startswith(OPERATOR + " "),
                        rows[0]["operator_label"])

    def test_同一笔流水不能回滚两次(self):
        rec_id = _system_record(100)
        _revert(rec_id, admin.RevertIn(request_id=_rid()))
        with self.assertRaises(HTTPException) as ctx:
            _revert(rec_id, admin.RevertIn(request_id=_rid()))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(_balance(), 0)

    def test_不能回滚人工操作(self):
        """否则可以「回滚一条回滚」，账本变成镜厅。"""
        r = _adjust(admin.AdjustIn(emp_id=TARGET, points=100, reason="人工",
                                   request_id=_rid()))
        with self.assertRaises(HTTPException) as ctx:
            _revert(r["record_id"], admin.RevertIn(request_id=_rid()))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("人工操作", ctx.exception.detail)

    def test_回滚不存在或回滚后为负都被拒(self):
        with self.assertRaises(HTTPException) as ctx:
            _revert(99999999, admin.RevertIn(request_id=_rid()))
        self.assertEqual(ctx.exception.status_code, 404)

        # 攒了 100 又花掉 100（余额 0），此时回滚那笔 +100 会让余额变负 → 必须拒绝
        earned = _system_record(100)
        _system_record(-100)
        self.assertEqual(_balance(), 0)
        with self.assertRaises(HTTPException) as ctx:
            _revert(earned, admin.RevertIn(request_id=_rid()))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertEqual(_balance(), 0)
        self.assertEqual(_sum_records(), 0)

    # ---- 校平 ----

    def test_校平把歪掉的余额拉回流水合计(self):
        _adjust(admin.AdjustIn(emp_id=TARGET, points=100, reason="底金",
                                           request_id=_rid()))
        # 模拟 seed._earn 那种非原子写入造成的账实不符
        db.execute("UPDATE point_accounts SET balance = 130 WHERE emp_id = %s", (TARGET,))
        self.assertNotEqual(_balance(), _sum_records())

        r = _reconcile(admin.ReconcileIn(emp_id=TARGET, reason="对账",
                                                     request_id=_rid()))
        self.assertEqual(r["balance_before"], 130)
        self.assertEqual(r["records_sum"], 100)
        self.assertEqual(r["delta"], -30)
        self.assertEqual(_balance(), _sum_records())
        self.assertEqual(_balance(), 100)
        # 关键：流水合计**一字未动**。校平不能靠「插入一笔 -30 的流水」实现 ——
        # add_points 会同时动余额和流水，balance - SUM(ledger) 这个差额是不变量，
        # 插流水永远抹不平差额（这正是第一版实现被这条测试抓出来的地方）。
        self.assertEqual(_sum_records(), 100)
        self.assertEqual(
            db.query_one("SELECT COUNT(*) AS c FROM point_records WHERE emp_id = %s "
                         "AND ref_type = 'reconcile'", (TARGET,))["c"], 0)

    def test_校平已平的账户不写流水(self):
        _adjust(admin.AdjustIn(emp_id=TARGET, points=100, reason="底金",
                                           request_id=_rid()))
        r = _reconcile(admin.ReconcileIn(emp_id=TARGET, request_id=_rid()))
        self.assertEqual(r["delta"], 0)
        self.assertNotIn("record_id", r)        # 不写 point_records，就没有流水 id 可返回
        self.assertEqual(r["balance_after"], 100)
        self.assertEqual(
            db.query_one("SELECT COUNT(*) AS c FROM point_records WHERE emp_id = %s "
                         "AND ref_type = 'reconcile'", (TARGET,))["c"], 0)
        # 但仍留一条 point_ops：「查过，无需修正」
        self.assertEqual(
            db.query_one("SELECT COUNT(*) AS c FROM point_ops WHERE target_emp_id = %s "
                         "AND op_type = 'reconcile'", (TARGET,))["c"], 1)

    def test_校平不存在的账户被拒(self):
        with self.assertRaises(HTTPException) as ctx:
            _reconcile(admin.ReconcileIn(emp_id=TARGET, request_id=_rid()))
        self.assertEqual(ctx.exception.status_code, 404)

    # ---- 口径（metrics.py 的单一事实来源）----

    def test_口径不把回滚算成消耗(self):
        """回滚是纠偏不是业务：原笔和被回滚的那笔都不该进「已发放/已消耗」。

        两个错误各有各的样子：
          ① 只把 revert 行排除、不排除原笔 → 回滚一笔 +80 之后，发放总额纹丝不动、
             消耗也纹丝不动，账面上那笔钱像从没被收回过；
          ② 两行都算 → 回滚凭空多出 80 的「消耗」，消耗率直接翻倍。
        这里用「加一笔再回滚，口径必须回到起点」把两种情况同时钉住。
        """
        before_issued = metrics.points_issued()
        before_spent = metrics.points_spent()
        today = datetime.now().strftime("%Y-%m-%d")

        def today_of(field):
            return next((d[field] for d in metrics.daily_points(7)
                         if str(d["d"]) == today), 0)

        before_today = (today_of("issued"), today_of("spent"))

        rec = _system_record(80)
        self.assertEqual(metrics.points_issued(), before_issued + 80,
                         "新增的原始发放应当计入已发放")

        _revert(rec, admin.RevertIn(request_id=_rid()))
        self.assertEqual(metrics.points_issued(), before_issued,
                         "被回滚的原笔不该再算已发放")
        # 朴素写法 SUM(-points WHERE points<0) 在这里会是 before_spent + 80
        self.assertEqual(metrics.points_spent(), before_spent,
                         "回滚行本身不该算已消耗")
        # 每日趋势走的是同一套排除规则，必须一起改：看板顶上两个大数字对了、
        # 下面那张趋势图还歪着，是更难发现的那种错。
        self.assertEqual((today_of("issued"), today_of("spent")), before_today,
                         "趋势图的当日数字也该回到起点")

    # ---- 列表与盘点 ----

    def test_列表带出姓名标签与操作人(self):
        _adjust(admin.AdjustIn(emp_id=TARGET, points=100, reason="列表用",
                                           request_id=_rid()))
        d = _list(emp_id=TARGET)
        self.assertEqual(d["total"], 1)
        row = d["items"][0]
        self.assertEqual(row["user_name"], "积分测试用户")
        self.assertEqual(row["ref_label"], "人工发放")
        # 操作人带专用测试工号 + 姓名，不借用真实或种子管理员。
        self.assertTrue(row["operator_label"].startswith(OPERATOR + " "), row["operator_label"])
        self.assertNotEqual(row["operator_label"], "本人")
        self.assertIsInstance(row["created_at"], str)   # 已格式化成字符串，能进 JSON

    def test_列表的方向与类型筛选(self):
        _adjust(admin.AdjustIn(emp_id=TARGET, points=100, reason="收",
                                           request_id=_rid()))
        _adjust(admin.AdjustIn(emp_id=TARGET, points=-40, reason="支",
                                           request_id=_rid()))
        self.assertEqual(_list(emp_id=TARGET, direction="in")["total"], 1)
        self.assertEqual(_list(emp_id=TARGET, direction="out")["total"], 1)
        self.assertEqual(_list(emp_id=TARGET, ref_type="grant")["total"], 1)
        with self.assertRaises(HTTPException) as ctx:
            _list(emp_id=TARGET, ref_type="不存在的类型")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_分页的count与data必须一致(self):
        """db.paginate 的 count 与 data 是两条独立 SQL，join 不一致就会
        「共 21 条却只有 20 行」、末页空白。这里用多笔数据把它钉住。"""
        for i in range(7):
            _adjust(admin.AdjustIn(emp_id=TARGET, points=10, reason=f"第{i}笔",
                                               request_id=_rid(i)))
        d = _list(emp_id=TARGET, page=2, size=3)
        self.assertEqual(d["total"], 7)
        self.assertEqual(len(d["items"]), 3)      # 第 2 页仍是满的
        self.assertEqual(d["page"], 2)

    def test_列表不会把系统流水的ref_id串成操作人(self):
        """回归：join point_ops 必须带 ref_type 限定。
        否则 course 流水的 ref_id=N 会撞上 point_ops.id=N，
        把一个不相干的管理员显示成这笔课程积分的操作人 —— 看着合理、实际全错。"""
        r = _adjust(admin.AdjustIn(emp_id=TARGET, points=100, reason="占位",
                                               request_id=_rid()))
        # 关键：用真实的 point_ops.id 当课程流水的 ref_id。
        # 如果 join 少了 ref_type 限定，这一行必然会错接到上面那笔人工发放上。
        db.execute("INSERT INTO point_records (emp_id, points, note, ref_type, ref_id) "
                   "VALUES (%s, 5, '完成课程', 'course', %s)", (TARGET, r["op_id"]))
        rows = _list(emp_id=TARGET, ref_type="course")["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["operator_label"], "本人",
                         "课程流水被错误地接上了 point_ops 里的操作人")
        self.assertIsNone(rows[0]["op_type"])

    def test_盘点能查出账实不符(self):
        _adjust(admin.AdjustIn(emp_id=TARGET, points=100, reason="底金",
                                           request_id=_rid()))
        db.execute("UPDATE point_accounts SET balance = 175 WHERE emp_id = %s", (TARGET,))
        items = _drift()["items"]
        mine = [i for i in items if i["emp_id"] == TARGET]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["drift"], 75)
        db.execute("UPDATE point_accounts SET balance = 100 WHERE emp_id = %s", (TARGET,))


def _cleanup_points_only():
    db.assert_test_database()
    db.execute("DELETE FROM point_records WHERE emp_id = %s", (TARGET,))
    db.execute("DELETE FROM point_ops WHERE target_emp_id = %s", (TARGET,))
    db.execute("DELETE FROM point_accounts WHERE emp_id = %s", (TARGET,))


class TestNoPublishOnRollback(unittest.IsolatedAsyncioTestCase):
    """实时推送的边界：只有提交成功的流水才推得出去。

    这一条为什么必须测：事务回滚了但事件已经推给管理端的话，管理员会看到
    一笔**不存在的加分** —— 比不推送更糟。

    注意现有业务代码里没有「emit 之后回滚」的路径（六个 add_points 调用点都是
    事务体的最后一句），所以这里的失败路径是**构造**出来的。但这个场景在生产里
    是真实存在的：校平如果实现成 add_points(delta) 之后再做一个乐观 UPDATE
    （WHERE balance = 期望值）而影响 0 行，就必须抛异常回滚，那笔 delta 流水绝不能外泄。

    测的是「未提交的行对其他连接不可见」这个 MySQL 语义，而不是我方缓冲区的记账 ——
    这也是选择轮询 point_records 而不是自维护事件缓冲的原因。
    """

    async def asyncSetUp(self):
        init_test_db()
        _cleanup()
        db.execute(
            "INSERT INTO users (emp_id, username, password_hash, name, role, department) "
            "VALUES (%s, %s, %s, %s, 'user', '测试部')",
            (TARGET, USERNAME, "x", "积分测试用户"),
        )
        db.execute(
            "INSERT INTO users (emp_id, username, password_hash, name, role) "
            "VALUES (%s, %s, 'x', '测试管理员', 'super_admin')", (OPERATOR, OPERATOR))
        events.bind_loop(asyncio.get_running_loop())
        events.start_poller()
        self.q = events.subscribe()
        # IsolatedAsyncioTestCase 默认开着 loop 的 debug 模式，会把每个超过
        # 100ms 的任务当「慢回调」打一行警告。下面这次 sleep 和轮询等待必然超，
        # 不调阈值就会在测试输出里刷一堆看着像报错的东西。
        asyncio.get_running_loop().slow_callback_duration = 10
        # 等轮询线程完成水位对齐，免得把对齐瞬间的历史行算进来
        await asyncio.sleep(1.2)

    async def asyncTearDown(self):
        events.unsubscribe(self.q)
        events.stop_poller()
        _cleanup()

    async def _drain(self, timeout=1.5):
        """把等待期内收到的 point 帧收集起来（跳过 dropped）。"""
        got = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                frame = await asyncio.wait_for(self.q.get(), timeout=deadline - time.monotonic())
            except asyncio.TimeoutError:
                break
            if frame.get("type") == "point":
                got.append(frame)
        return got

    async def test_回滚的事务推不出去而已提交的推得出去(self):
        # ① 提交成功的那笔：应当被推出来（正向对照，证明订阅通道是活的）
        _adjust(admin.AdjustIn(emp_id=TARGET, points=100, reason="已提交",
                                           request_id=_rid()))
        got = await self._drain()
        self.assertEqual([f["points"] for f in got], [100],
                         f"已提交的流水应当推送，实际收到 {got}")
        self.assertEqual(got[0]["user_name"], "积分测试用户")

        # ② 同一个连接、同一个表，写一笔然后回滚：绝不能推出去
        def fn(cur):
            logic.add_points(cur, TARGET, 999, "回滚掉的分", "course", 424242)
            raise RuntimeError("模拟业务失败，整个事务回滚")

        with self.assertRaises(RuntimeError):
            db.run_tx(fn)

        got2 = await self._drain(timeout=1.6)     # 至少跨过 3 个轮询周期
        self.assertEqual(got2, [], f"回滚掉的流水不该推送，实际收到 {got2}")
        # 顺带确认那笔确实没落库（不然测试是空转）
        self.assertEqual(_sum_records(), 100)
        self.assertEqual(_balance(), 100)
        evidence("SSE提交与回滚", 已提交推送数=len(got), 回滚推送数=len(got2), 实际余额=_balance())


class TestStreamBoundaries(unittest.IsolatedAsyncioTestCase):
    async def collect_after_revoke(self, count):
        token, _ = auth.create_session("isolated_stream_fixture")
        q = events.subscribe()
        request = Request({"type": "http", "headers": [(b"authorization", ("Bearer " + token).encode())]})
        response = None
        frames = []
        try:
            with patch.object(events, "subscribe", return_value=q), patch.object(admin, "HEARTBEAT_SECONDS", 0.01):
                response = await admin.stream_points(request, {"role": "super_admin"})
                iterator = response.body_iterator
                self.assertIn("connected", await anext(iterator))
                auth.drop_session(token)
                for i in range(max(count, 1)):
                    if count:
                        q.put_nowait({"type": "point", "id": i + 1, "points": 10})
                    frame = await asyncio.wait_for(anext(iterator), 1)
                    frames.append(frame)
                    if "event: bye" in frame:
                        break
            return frames
        finally:
            if response:
                await response.body_iterator.aclose()
            events.unsubscribe(q)
            auth.drop_session(token)

    async def test_空闲期间撤销会话会断流(self):
        frames = await self.collect_after_revoke(0)
        self.assertEqual(len(frames), 1)
        self.assertIn("event: bye", frames[0])
        evidence("SSE空闲撤销", 终止帧数=len(frames), 积分帧数=0)

    async def test_持续来帧期间撤销会话必须停止积分推送(self):
        frames = await self.collect_after_revoke(3)
        leaked = sum(frame.startswith("data:") for frame in frames)
        evidence("SSE持续来帧撤销", 撤销后投递帧数=3, 实际外发积分帧数=leaked, 期望外发数=0)
        self.assertEqual(leaked, 0, "会话已撤销，队列持续有消息时仍输出积分流水")

    async def test_慢客户端队列有界并发出丢帧标记(self):
        q = events.subscribe()
        try:
            for i in range(events._QUEUE_SIZE + 1):
                events._deliver(q, {"type": "point", "id": i})
            size = q.qsize()
            frames = [q.get_nowait() for _ in range(size)]
            dropped = sum(f["type"] == "dropped" for f in frames)
            self.assertEqual(size, events._QUEUE_SIZE)
            self.assertEqual(dropped, 1)
            evidence("SSE慢客户端", 投递数=events._QUEUE_SIZE + 1, 队列长度=size,
                     保留积分帧数=size - dropped, 丢帧标记数=dropped)
        finally:
            events.unsubscribe(q)


class TestPointsPermissions(unittest.TestCase):
    """读写分权：读 = 任意管理角色（含只读 viewer），写 = 仅 super_admin。

    这组不连数据库（不需要 setUpClass 跳过）—— 判权是纯函数。

    为什么从函数签名里取依赖、而不是直接把 user 传进路由函数：路由函数体里没有
    判权代码，403 是 Depends 抛的。把 user=VIEWER_USER 传给 adjust_points 只能
    证明「函数体不判权」，证明不了「接口挂了判权」。所以这里取出签名里的
    Depends 对象，调它真正的检查函数。
    """

    def test_只读角色能读不能写(self):
        self.assertIs(auth.require_admin(VIEWER_USER), VIEWER_USER)
        with self.assertRaises(HTTPException) as ctx:
            auth.require_roles(*admin.SUPER)(VIEWER_USER)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_超管读写皆可(self):
        self.assertIs(auth.require_admin(SUPER_USER), SUPER_USER)
        self.assertIs(auth.require_roles(*admin.SUPER)(SUPER_USER), SUPER_USER)

    def test_普通用户读也不行(self):
        with self.assertRaises(HTTPException) as ctx:
            auth.require_admin({"emp_id": "2001", "role": "user"})
        self.assertEqual(ctx.exception.status_code, 403)

    def test_写路由挂的依赖拒绝只读角色(self):
        for fn in (admin.adjust_points, admin.revert_points, admin.reconcile_points):
            dep = inspect.signature(fn).parameters["user"].default
            with self.assertRaises(HTTPException) as ctx:
                dep.dependency(VIEWER_USER)
            self.assertEqual(ctx.exception.status_code, 403, fn.__name__)

    def test_读路由挂的依赖放行只读角色(self):
        # points_trend 是给实时刷新趋势图用的单开入口，也是只读，同样要放行 viewer
        for fn in (admin.list_points, admin.points_drift, admin.point_ref_types,
                   admin.points_trend):
            dep = inspect.signature(fn).parameters["_"].default
            self.assertIs(dep.dependency(VIEWER_USER), VIEWER_USER, fn.__name__)


class TestBulkPointClassification(unittest.TestCase):
    """导入带确定期望值的混合流水，验证读取归类；不代替真实奖励资格或写入链路测试。"""

    LABELS = {"welcome": "注册奖励", "course": "完成课程", "activity": "参与活动",
              "announcement": "阅读公告", "grant": "人工发放", "deduct": "人工扣减",
              "redeem": "兑换礼品", "refund": "订单退款", "revert": "回滚"}
    OPERATOR = "ZZBULK_OPERATOR"

    @classmethod
    def setUpClass(cls):
        init_test_db()
        if db.query_one("SELECT COUNT(*) n FROM point_records")["n"]:
            raise AssertionError("批量积分夹具要求独立空测试库，不与既有积分混用")
        cls.employees = tuple(f"ZZBULK{i:03d}" for i in range(50))
        cls.identities = (*cls.employees, cls.OPERATOR)
        if db.query_one("SELECT COUNT(*) n FROM users WHERE emp_id IN %s", (cls.identities,))["n"]:
            raise AssertionError("模拟身份已存在，拒绝覆盖")
        cls.documents = {table: [] for table in ("courses", "activities", "announcements", "gifts")}
        cls.records, cls.reverted, cls.refunded = [], set(), set()
        cls.balances, cls.stocks = Counter(), Counter()
        cls.day = db.query_one("SELECT CURDATE() d")["d"]
        cls.addClassCleanup(cls.cleanup)
        started = time.perf_counter()
        db.run_tx(cls.seed)
        evidence("批量积分夹具", 用户数=len(cls.employees), 管理员数=1,
                 流水数=len(cls.records), 类型数量=dict(Counter(r["ref_type"] for r in cls.records)),
                 订单数=500, 退款订单数=len(cls.refunded), 冲正数=len(cls.reverted),
                 造数耗时秒=round(time.perf_counter() - started, 3),
                 说明="同事务导入归类夹具，不冒充逐笔业务流程验收")

    @classmethod
    def seed(cls, cur):
        for emp in cls.identities:
            role = "super_admin" if emp == cls.OPERATOR else "user"
            cur.execute("INSERT INTO users (emp_id,username,password_hash,name,role) VALUES (%s,%s,'x',%s,%s)",
                        (emp, emp, "模拟管理员" if role == "super_admin" else "模拟员工", role))
        for table, count in (("courses", 120), ("activities", 20), ("announcements", 10)):
            for index in range(count):
                points = (0 if index == 119 else (index % 3 + 1) * 10) if table == "courses" else (15 if table == "activities" else 5)
                cur.execute(f"INSERT INTO {table} (title,points) VALUES (%s,%s)", (f"积分归类样本{index}", points))
                cls.documents[table].append((cur.lastrowid, points))
        for index in range(30):
            cur.execute("INSERT INTO gifts (name,points_cost,stock) VALUES (%s,60,200)", (f"积分归类礼品{index}",))
            gid = cur.lastrowid
            cls.documents["gifts"].append((gid, 60))
            cls.stocks[gid] = 200
            cur.execute("INSERT INTO gift_stock_records (gift_id,delta,stock_after,kind,ref_id,operator_emp_id) "
                        "VALUES (%s,200,200,'baseline',0,%s)", (gid, cls.OPERATOR))

        def record(emp, points, kind, ref):
            day = cls.day - timedelta(days=len(cls.records) % 7)
            created = datetime(day.year, day.month, day.day)
            cur.execute("INSERT INTO point_records (emp_id,points,note,ref_type,ref_id,created_at) "
                        "VALUES (%s,%s,'模拟归类流水',%s,%s,%s)", (emp, points, kind, ref, created))
            rid = cur.lastrowid
            cls.records.append({"id": rid, "emp_id": emp, "points": points,
                                "ref_type": kind, "ref_id": ref, "created_at": created})
            cls.balances[emp] += points
            return rid

        def operation(emp, points, kind, reverted=None):
            cur.execute("INSERT INTO point_ops (op_type,target_emp_id,points,reason,operator_emp_id,idem_key,reverted_record_id) "
                        "VALUES (%s,%s,%s,'模拟归类',%s,%s,%s)",
                        (kind, emp, points, cls.OPERATOR, f"bulk-{emp}-{len(cls.records)}", reverted))
            record(emp, points, kind, cur.lastrowid)

        def stock(gid, delta, kind, oid):
            cls.stocks[gid] += delta
            cur.execute("INSERT INTO gift_stock_records (gift_id,delta,stock_after,kind,ref_id,operator_emp_id) "
                        "VALUES (%s,%s,%s,%s,%s,%s)", (gid, delta, cls.stocks[gid], kind, oid, cls.OPERATOR))

        for user_index, emp in enumerate(cls.employees):
            record(emp, 10000, "welcome", 0)
            courses = []
            for table, kind in (("courses", "course"), ("activities", "activity"), ("announcements", "announcement")):
                for ref, points in cls.documents[table]:
                    rid = record(emp, points, kind, ref)
                    if kind == "course":
                        courses.append((rid, points))
            for _ in range(10):
                operation(emp, 25, "grant")
                operation(emp, -5, "deduct")
            for index in range(10):
                gid = cls.documents["gifts"][(user_index * 10 + index) % 30][0]
                status = "cancelled" if index < 3 else ("refunded" if index < 5 else "pending")
                cur.execute("INSERT INTO redemptions (gift_id,emp_id,points_cost,status) VALUES (%s,%s,60,%s)",
                            (gid, emp, status))
                oid = cur.lastrowid
                record(emp, -60, "redeem", oid)
                stock(gid, -1, "redeem", oid)
                if index < 5:
                    record(emp, 60, "refund", oid)
                    stock(gid, 1, "refund", oid)
                    cls.refunded.add((emp, oid))
            for rid, points in courses[:5]:
                operation(emp, -points, "revert", rid)
                cls.reverted.add(rid)
        cur.executemany("INSERT INTO point_accounts (emp_id,balance) VALUES (%s,%s)", list(cls.balances.items()))
        cur.executemany("UPDATE gifts SET stock=%s WHERE id=%s", [(value, gid) for gid, value in cls.stocks.items()])

    @classmethod
    def cleanup(cls):
        db.assert_test_database()
        def clean(cur):
            for table, column in (("user_access_logs", "emp_id"), ("point_records", "emp_id"),
                                  ("point_ops", "target_emp_id"), ("point_accounts", "emp_id"),
                                  ("redemptions", "emp_id")):
                cur.execute(f"DELETE FROM {table} WHERE {column} IN %s", (cls.identities,))
            for table, docs in cls.documents.items():
                ids = tuple(row[0] for row in docs)
                if ids:
                    if table == "gifts":
                        cur.execute("DELETE FROM gift_stock_records WHERE gift_id IN %s", (ids,))
                    cur.execute(f"DELETE FROM {table} WHERE id IN %s", (ids,))
            cur.execute("DELETE FROM users WHERE emp_id IN %s", (cls.identities,))
        db.run_tx(clean)

    def test_混合类型全量分页无重复漏项及操作人串类(self):
        rows = []
        for page in range(1, (len(self.records) + 99) // 100 + 1):
            result = _list(page=page, size=100)
            self.assertEqual(result["total"], len(self.records))
            rows.extend(result["items"])
        expected = {r["id"]: r for r in self.records}
        self.assertEqual([r["id"] for r in rows], sorted(expected, reverse=True))
        for row in rows:
            original = expected[row["id"]]
            self.assertEqual((row["emp_id"], row["points"], row["ref_type"], row["ref_id"]),
                             tuple(original[key] for key in ("emp_id", "points", "ref_type", "ref_id")))
            self.assertEqual(row["ref_label"], self.LABELS[original["ref_type"]])
            manual = original["ref_type"] in ("grant", "deduct", "revert")
            self.assertEqual(row["operator_emp_id"], self.OPERATOR if manual else None)
            self.assertEqual(row["operator_label"], f"{self.OPERATOR} 模拟管理员" if manual else "本人")
        evidence("批量积分分页归类", 流水数=len(rows), 分页数=(len(rows) + 99) // 100,
                 类型数量=dict(Counter(r["ref_type"] for r in rows)), 重复数=len(rows) - len(expected),
                 漏项数=len(expected) - len(rows), 标签或操作人错配数=0)

    def test_类型收支工号时间组合筛选(self):
        queries = 0
        def verify(expected, **filters):
            nonlocal queries
            queries += 1
            result = _list(size=100, **filters)
            self.assertEqual(result["total"], len(expected), filters)
            self.assertEqual([r["id"] for r in result["items"]], sorted((r["id"] for r in expected), reverse=True)[:100], filters)
        for kind in ("", *self.LABELS):
            for direction in ("", "in", "out"):
                expected = [r for r in self.records if (not kind or r["ref_type"] == kind)
                            and (not direction or (r["points"] > 0 if direction == "in" else r["points"] < 0))]
                verify(expected, ref_type=kind, direction=direction)
        for emp in self.employees:
            verify([r for r in self.records if r["emp_id"] == emp], emp_id=emp)
        for days in (1, 7, 30):
            cutoff = db.query_one("SELECT NOW() n")["n"] - timedelta(days=days)
            verify([r for r in self.records if r["created_at"] >= cutoff], days=days)
        positive = _list(direction="in")["total"]
        negative = _list(direction="out")["total"]
        zero = sum(r["points"] == 0 for r in self.records)
        self.assertEqual(positive + negative + zero, len(self.records))
        evidence("批量积分组合筛选", 筛选组合数=queries, 收入流水数=positive,
                 支出流水数=negative, 零分流水数=zero, 数量与首屏记录错配数=0)

    def test_退款冲正不混入发放消耗且按天统计一致(self):
        effective = [r for r in self.records if r["ref_type"] not in ("refund", "revert")
                     and r["id"] not in self.reverted
                     and not (r["ref_type"] == "redeem" and (r["emp_id"], r["ref_id"]) in self.refunded)]
        issued = sum(max(r["points"], 0) for r in effective)
        spent = sum(max(-r["points"], 0) for r in effective)
        self.assertEqual((metrics.points_issued(), metrics.points_spent()), (issued, spent))
        daily_expected = {}
        for row in effective:
            totals = daily_expected.setdefault(row["created_at"].date(), [0, 0])
            totals[0] += max(row["points"], 0)
            totals[1] += max(-row["points"], 0)
        daily_actual = {r["d"]: [int(r["issued"]), int(r["spent"])] for r in metrics.daily_points(7)}
        self.assertEqual(daily_actual, daily_expected)
        report = redemption.reconciliation()
        self.assertTrue(report["ok"], report)
        balance = db.query_one("SELECT SUM(balance) total FROM point_accounts WHERE emp_id IN %s", (self.employees,))["total"]
        self.assertEqual(balance, issued - spent)
        evidence("批量积分统计", 原始正分合计=sum(max(r["points"], 0) for r in self.records),
                 原始负分绝对值合计=sum(max(-r["points"], 0) for r in self.records),
                 有效发放=metrics.points_issued(), 净消耗=metrics.points_spent(), 总余额=int(balance),
                 日统计天数=len(daily_actual), 对账差异数=sum(len(report[k]) for k in ("accounts", "orders", "stocks", "orphan_records")))

    def test_真实HTTP返回混合流水及正确总数(self):
        token, _ = auth.create_session(self.OPERATOR)
        try:
            status, result = asyncio.run(http_request("/api/admin/points", token=token, method="GET"))
        finally:
            auth.drop_session(token)
        self.assertEqual(status, 200)
        self.assertEqual(result["total"], len(self.records))
        self.assertEqual([r["id"] for r in result["items"]], sorted((r["id"] for r in self.records), reverse=True)[:20])
        evidence("批量积分HTTP", HTTP状态=status, 总数=result["total"], 首屏条数=len(result["items"]))

    def test_用户侧只展示最近一百条的边界(self):
        emp = self.employees[0]
        expected = [r for r in self.records if r["emp_id"] == emp]
        token, _ = auth.create_session(emp)
        try:
            status, result = asyncio.run(http_request("/api/user/points", token=token, method="GET"))
        finally:
            auth.drop_session(token)
        self.assertEqual(status, 200)
        self.assertEqual([r["id"] for r in result["records"]], sorted((r["id"] for r in expected), reverse=True)[:100])
        self.assertEqual(result["points"], sum(r["points"] for r in expected))
        evidence("用户流水截断边界", 用户实际流水数=len(expected), 返回流水数=len(result["records"]),
                 当前响应未返回条数=len(expected) - len(result["records"]), 账户余额=result["points"])


if __name__ == "__main__":
    unittest.main()
