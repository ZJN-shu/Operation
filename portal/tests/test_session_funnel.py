"""会话漏斗口径测试：会话归因、空会话排除、写操作透传。

分两类：
  - 口径层：metrics.session_* 的聚合是否按会话去重、是否排除空会话；
  - 透传层：报名 / 兑换写操作是否把 session_id 落到业务表里 —— 没有这一层，
    漏斗的最后一环根本归因不到会话。

独立 MySQL 下运行（python -m portal.tests.run_mysql），未启用则整体 skip。

断言一律用「插入前后增量」而不是绝对值：metrics 是全局聚合，同一轮里
其它测试类也会写 user_access_logs，直接断言绝对值会互相干扰。
"""
from __future__ import annotations

import unittest
from uuid import uuid4

from portal.tests import init_test_db
from portal.backend import db, metrics
from portal.backend import redemption as domain
from portal.backend.routers import user as user_api
from portal.tests.test_redemption import evidence

MARK = "ZZSESS"


class TestSessionFunnel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_test_db()
        cls.emp = f"{MARK}_U1"
        cls.emp2 = f"{MARK}_U2"
        db.execute(
            "INSERT INTO users (emp_id, username, password_hash, name) VALUES (%s,%s,'x','会话测试一')",
            (cls.emp, f"{MARK}_u1"))
        db.execute(
            "INSERT INTO users (emp_id, username, password_hash, name) VALUES (%s,%s,'x','会话测试二')",
            (cls.emp2, f"{MARK}_u2"))
        db.execute("INSERT INTO point_accounts (emp_id, balance) VALUES (%s, 1000)", (cls.emp,))
        db.execute("INSERT INTO point_accounts (emp_id, balance) VALUES (%s, 1000)", (cls.emp2,))
        cls.gift = db.insert(
            "INSERT INTO gifts (name, points_cost, stock) VALUES (%s, 10, 50)",
            (f"{MARK}_礼品",))
        cls.course = db.insert(
            "INSERT INTO courses (title, points) VALUES (%s, 5)", (f"{MARK}_课程",))
        cls.addClassCleanup(cls.cleanup)

    @classmethod
    def cleanup(cls):
        db.assert_test_database()
        for sql, arg in (
            ("DELETE FROM user_access_logs WHERE emp_id LIKE %s", MARK + "%"),
            ("DELETE FROM redemptions WHERE emp_id LIKE %s", MARK + "%"),
            ("DELETE FROM training_progress WHERE emp_id LIKE %s", MARK + "%"),
            ("DELETE FROM point_records WHERE emp_id LIKE %s", MARK + "%"),
            ("DELETE FROM point_accounts WHERE emp_id LIKE %s", MARK + "%"),
            ("DELETE FROM notifications WHERE emp_id LIKE %s", MARK + "%"),
            ("DELETE FROM audit_logs WHERE emp_id LIKE %s", MARK + "%"),
            ("DELETE FROM gifts WHERE name LIKE %s", MARK + "%"),
            ("DELETE FROM courses WHERE title LIKE %s", MARK + "%"),
            ("DELETE FROM search_keywords WHERE doc_id IN (%s, %s)", None),
        ):
            if arg is None:
                db.execute(sql, (cls.gift, cls.course))
            else:
                db.execute(sql, (arg,))
        db.execute("DELETE FROM users WHERE emp_id LIKE %s", (MARK + "%",))

    def setUp(self):
        # 每个用例都从干净状态起步，增量断言才有意义
        db.execute("DELETE FROM user_access_logs WHERE emp_id LIKE %s", (MARK + "%",))
        db.execute("DELETE FROM redemptions WHERE emp_id LIKE %s", (MARK + "%",))
        db.execute("DELETE FROM training_progress WHERE emp_id LIKE %s", (MARK + "%",))

    def _view(self, session: str, emp: str | None = None, event: str = "gift_view",
              ref_type: str = "gift", days_ago: int = 0):
        ref_id = self.gift if ref_type == "gift" else self.course
        db.execute(
            "INSERT INTO user_access_logs "
            "(event_id, session_id, emp_id, event_type, ref_type, ref_id, accessed_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,DATE_SUB(NOW(), INTERVAL %s DAY))",
            (uuid4().hex, session, emp or self.emp, event, ref_type, ref_id, days_ago))

    def _order(self, session: str, emp: str | None = None):
        return db.insert(
            "INSERT INTO redemptions (gift_id, emp_id, session_id, points_cost) "
            "VALUES (%s,%s,%s,10)", (self.gift, emp or self.emp, session))

    # ---------- 口径层 ----------

    def test_同会话内浏览并兑换才计转化(self):
        before = metrics.session_gift_funnel(7)
        self._view(f"{MARK}_S1")
        self._view(f"{MARK}_S2")            # 只看不买
        self._order(f"{MARK}_S1")           # 与第一次浏览同会话
        after = metrics.session_gift_funnel(7)
        self.assertEqual((after[0] - before[0], after[1] - before[1]), (2, 1))
        self.assertEqual(after[2], metrics.pct(after[1], after[0]))
        evidence("会话兑换漏斗", 浏览会话增量=after[0] - before[0],
                 兑换会话增量=after[1] - before[1], 转化率=after[2])

    def test_空会话的历史数据不进口径(self):
        """空串不是「一个会话」，聚起来会变成一个横跨所有人的超级会话。"""
        before = metrics.session_gift_funnel(7)
        self._view("", emp=self.emp)
        self._view("", emp=self.emp2)
        self._order("")
        after = metrics.session_gift_funnel(7)
        self.assertEqual((after[0] - before[0], after[1] - before[1]), (0, 0))
        evidence("空会话排除", 浏览会话增量=after[0] - before[0], 兑换会话增量=after[1] - before[1],
                 说明="两个不同人的空会话未被合并成一个")

    def test_窗口外的浏览不计入分母(self):
        before = metrics.session_gift_funnel(7)
        self._view(f"{MARK}_OLD", days_ago=20)
        self._order(f"{MARK}_OLD")
        after = metrics.session_gift_funnel(7)
        self.assertEqual((after[0] - before[0], after[1] - before[1]), (0, 0))
        evidence("漏斗时间窗", 窗口=7, 浏览发生在=20, 计入分母增量=after[0] - before[0])

    def test_访问深度按会话平均(self):
        before = metrics.session_depth(7)
        self._view(f"{MARK}_D1")
        self._view(f"{MARK}_D1")
        self._view(f"{MARK}_D1")
        self._view(f"{MARK}_D2")
        after = metrics.session_depth(7)
        self.assertEqual(after[1] - before[1], 2)
        self.assertGreaterEqual(after[0], 1.0)
        evidence("访问深度", 新增会话=after[1] - before[1], 会话内事件数="3 / 1",
                 全局平均深度=after[0])

    # ---------- 透传层 ----------

    def test_报名写入会话并计入课程漏斗(self):
        session = f"{MARK}_C1"
        before = metrics.session_course_funnel(7)
        self._view(session, event="course_view", ref_type="course")
        result = user_api.enroll(self.course, {"emp_id": self.emp, "session_id": session})
        self.assertEqual(result, {"ok": True})
        row = db.query_one("SELECT session_id FROM training_progress WHERE course_id=%s AND emp_id=%s",
                           (self.course, self.emp))
        self.assertEqual(row["session_id"], session)
        after = metrics.session_course_funnel(7)
        self.assertEqual((after[0] - before[0], after[1] - before[1]), (1, 1))
        evidence("课程会话漏斗", 浏览会话增量=after[0] - before[0],
                 报名会话增量=after[1] - before[1], 转化率=after[2])

    def test_兑换写入会话(self):
        session = f"{MARK}_R1"
        result = domain.redeem(self.gift, domain.RequestIn(request_id=uuid4().hex),
                               self.emp, session)
        self.assertTrue(result["ok"])
        row = db.query_one("SELECT session_id FROM redemptions WHERE id=%s", (result["redemption_id"],))
        self.assertEqual(row["session_id"], session)
        evidence("兑换会话透传", 订单=result["redemption_id"], session_id=row["session_id"])


if __name__ == "__main__":
    unittest.main()
