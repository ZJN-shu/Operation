"""通知发件箱测试：事务内只入队、事务外投递、投递失败可观测。

覆盖的是「把通知从业务事务里摘出去」之后真正会出问题的地方：

  - 事务侧：入队是否和业务写入同生共死（回滚了就不能留待投递）；
  - 投递侧：至少一次投递 + 幂等落库、角色受众投递时展开、外部渠道在事务外调用；
  - 失败侧：退避重试、重试耗尽转死信而不是静默消失。

每个用例开始都把本类造的发件箱行清干净，断言一律针对本用例自己的行 ——
投递器是全局扫描，断言全局条数会被其它用例的残留干扰。

独立 MySQL 下运行（python -m portal.tests.run_mysql），未启用则整体 skip。
"""
from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch
from uuid import uuid4

from portal.tests import init_test_db
from portal.backend import db, notifier
from portal.backend import redemption as domain
from portal.tests.test_redemption import InjectedFailure, evidence, request_key, writes

MARK = "ZZNOT"

# 本类造的行：受众含工号、事件键带前缀，或 ref 指向本类的订单/礼品。
_SCOPE_SQL = ("audience LIKE %s OR event_key LIKE %s OR "
              "(ref_type='order' AND ref_id IN (SELECT id FROM redemptions WHERE emp_id LIKE %s)) OR "
              "(ref_type='gift' AND ref_id=%s)")
_SCOPE_ARGS = (f"%{MARK}%", f"{MARK}%", f"{MARK}%", None)   # 末位在类初始化后填 gift


class TestNotificationOutbox(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_test_db()
        cls.admin = f"{MARK}_ADMIN"
        cls.emp = f"{MARK}_U1"
        for emp, role, name in ((cls.admin, "shop_admin", "通知管理员"), (cls.emp, "user", "通知用户")):
            db.execute("INSERT INTO users (emp_id, username, password_hash, name, role) "
                       "VALUES (%s,%s,'x',%s,%s)", (emp, emp, name, role))
        db.execute("INSERT INTO point_accounts (emp_id, balance) VALUES (%s, 1000)", (cls.emp,))
        cls.gift = db.insert("INSERT INTO gifts (name, points_cost, stock) VALUES (%s, 60, 4)",
                             (f"{MARK}_礼品",))
        cls.addClassCleanup(cls.cleanup)

    @classmethod
    def cleanup(cls):
        db.assert_test_database()
        cls.purge()
        for sql, arg in (
            ("DELETE FROM notifications WHERE emp_id LIKE %s", MARK + "%"),
            ("DELETE FROM redemptions WHERE emp_id LIKE %s", MARK + "%"),
            ("DELETE FROM point_records WHERE emp_id LIKE %s", MARK + "%"),
            ("DELETE FROM point_accounts WHERE emp_id LIKE %s", MARK + "%"),
            ("DELETE FROM gift_stock_records WHERE gift_id=%s", cls.gift),
            ("DELETE FROM gift_stock_bucket WHERE gift_id=%s", cls.gift),
            ("DELETE FROM audit_logs WHERE emp_id LIKE %s", MARK + "%"),
            ("DELETE FROM gifts WHERE id=%s", cls.gift),
            ("DELETE FROM users WHERE emp_id LIKE %s", MARK + "%"),
        ):
            db.execute(sql, (arg,))

    @classmethod
    def purge(cls):
        db.assert_test_database()
        db.execute(f"DELETE FROM notification_outbox WHERE {_SCOPE_SQL}",
                   (*_SCOPE_ARGS[:3], cls.gift))

    def setUp(self):
        self.purge()
        db.execute("DELETE FROM notifications WHERE emp_id LIKE %s", (MARK + "%",))
        # 每个用例都从同一份业务状态开始：库存与余额会被前面的用例消耗掉，
        # 不留神就会让后面的兑换变成「库存不足」而什么都没发生。
        # 库存真相已是「桶总和」，只改 gifts.stock 不会被动到扣减路径，故用 sync_bucket
        # 把桶重置到目标值（等价于「新礼品首次触碰按 gifts.stock 快照建桶」）。
        db.execute("UPDATE gifts SET stock = 100 WHERE id = %s", (self.gift,))
        self.sync_bucket(100)
        db.execute("UPDATE point_accounts SET balance = 1000 WHERE emp_id = %s", (self.emp,))

    def sync_bucket(self, value):
        """把该礼品的库存桶强制重置为总和 value（先删后建，幂等于快照）。"""
        def fn(cur):
            cur.execute("DELETE FROM gift_stock_bucket WHERE gift_id = %s", (self.gift,))
            db.seed_gift_buckets(cur, self.gift, value)
        db.run_tx(fn)

    # ---------- 工具 ----------

    def outbox_rows(self):
        return db.query(f"SELECT * FROM notification_outbox WHERE {_SCOPE_SQL} ORDER BY id",
                        (*_SCOPE_ARGS[:3], self.gift))

    def row(self, rid):
        return db.query_one("SELECT * FROM notification_outbox WHERE id=%s", (rid,))

    def inbox(self, emp_id):
        return db.query("SELECT * FROM notifications WHERE emp_id=%s ORDER BY id", (emp_id,))

    def redeem(self, key=None):
        return domain.redeem(self.gift, domain.RequestIn(request_id=key or request_key()), self.emp)

    def make_pending(self, *, event_key=None, audience=None, title="测试通知", content="内容"):
        """直接造一条待投递行，用来单独验证投递侧行为（不依赖业务链路）。"""
        key = event_key or f"{MARK}_manual_{uuid4().hex[:12]}"
        db.execute("INSERT INTO notification_outbox (event_key, audience, ntype, title, content) "
                   "VALUES (%s,%s,'system',%s,%s)",
                   (key, audience or notifier.user_audience(self.emp), title, content))
        return db.query_one("SELECT * FROM notification_outbox WHERE event_key=%s", (key,))

    # ---------- 事务侧 ----------

    def test_兑换只在发件箱登记不发站内信(self):
        """业务事务里不落站内信：投递不是业务的一部分，不该和兑换绑在一起提交。"""
        self.redeem()
        # 库存 100 → 99，没跨阈值，所以这里只有兑换通知一条（告警另有专门用例）
        rows = self.outbox_rows()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual(rows[0]["status"], notifier.PENDING)
        self.assertEqual(rows[0]["audience"], notifier.user_audience(self.emp))
        self.assertEqual(rows[0]["ntype"], "redeem")
        # 还没投递，站内信一条都没有
        self.assertEqual(len(self.inbox(self.emp)), 0)
        evidence("兑换只入队", 发件箱行数=len(rows), 状态=rows[0]["status"],
                 站内信数=len(self.inbox(self.emp)))

    def test_库存跨阈值只产生一条告警(self):
        """告警事件键用订单号锚定「这一次跨阈值」：继续卖到 2、1 件不再重复轰炸管理员。"""
        self.sync_bucket(4)   # 库存真相=SUM(桶)：直接重置桶到 4，而不是只改缓存
        alerts = lambda: [r for r in self.outbox_rows() if r["ntype"] == "low_stock"]
        self.redeem()      # 4 → 3，跨过阈值
        self.assertEqual(len(alerts()), 1)
        self.assertEqual(alerts()[0]["audience"], notifier.role_audience(*notifier.SHOP_ADMIN_ROLES))
        self.redeem()      # 3 → 2，已在阈值下方
        self.assertEqual(len(alerts()), 1)
        evidence("库存告警去重", 库存=2, 告警条数=len(alerts()))

    def test_事务回滚则待投递通知一起消失(self):
        """「只有提交成功才发通知」的真实性断言：注入故障让事务回滚，outbox 不能留行。"""
        with writes() as trace:
            self.redeem()
        self.assertGreater(len(trace), 2)
        before = len(self.outbox_rows())
        self.assertEqual(before, 1, "成功那笔应留下一条待投递")
        for step in range(1, len(trace) + 1):
            with self.subTest(step=step):
                with self.assertRaises(InjectedFailure), writes(fail_at=step):
                    self.redeem()
                self.assertEqual(len(self.outbox_rows()), before)
        evidence("逐写步骤回滚", 注入次数=len(trace), 回滚后发件箱残留=0,
                 完整回滚数=len(trace))

    def test_事件键相同只入队一次(self):
        """重放与 1213 死锁重试撞同一业务事件时，静默变 no-op 而不是抛 1062 打断业务。"""
        key = f"{MARK}_dup"

        def fn(cur):
            for _ in range(3):
                notifier.enqueue(cur, event_key=key, audience=notifier.user_audience(self.emp),
                                 title="重复入队", content="应只有一行")
        db.run_tx(fn)
        self.assertEqual(len(db.query("SELECT id FROM notification_outbox WHERE event_key=%s", (key,))), 1)

    # ---------- 投递侧 ----------

    def test_投递后落站内信且重复投递不重复(self):
        row = self.make_pending(content="投递测试")
        self.assertEqual(notifier.drain(), 1)
        inbox = self.inbox(self.emp)
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["content"], "投递测试")
        self.assertEqual(inbox[0]["outbox_id"], row["id"])
        self.assertEqual(self.row(row["id"])["status"], notifier.SENT)
        # 至少一次投递 + 幂等落库：再排空一次，站内信仍然是同一条
        notifier.drain()
        self.assertEqual(len(self.inbox(self.emp)), 1)
        evidence("投递幂等", 投递后站内信数=1,
                 二次排空后站内信数=len(self.inbox(self.emp)), 状态=self.row(row["id"])["status"])

    def test_角色受众在投递时才展开成具体管理员(self):
        """入队时不查 users，投递时才解析 —— 事务里不必多一次 SELECT 和 N 次 INSERT。"""
        row = self.make_pending(event_key=f"{MARK}_role",
                                audience=notifier.role_audience(*notifier.SHOP_ADMIN_ROLES),
                                title="库存告警", content="库存不足")
        self.assertTrue(row["audience"].startswith("role:"), row["audience"])
        self.assertEqual(len(self.inbox(self.admin)), 0)
        notifier.drain()
        targets = {r["emp_id"] for r in db.query(
            "SELECT emp_id FROM notifications WHERE outbox_id=%s", (row["id"],))}
        self.assertIn(self.admin, targets)
        evidence("角色受众展开", 受众=row["audience"], 命中管理员=self.admin in targets,
                 落库数=len(targets))

    def test_外部渠道只在事务外被调用(self):
        """邮件/短信这类慢调用绝不能出现在业务事务里 —— 整条链路会被它拖垮。"""
        calls: list[str] = []
        notifier.CHANNELS.append(lambda row, emp_id: calls.append(row["event_key"]))
        self.addCleanup(notifier.CHANNELS.clear)
        oid = self.redeem()["redemption_id"]
        self.assertEqual(calls, [], "兑换事务内不应触发外部渠道")
        notifier.drain()
        self.assertIn(f"redeem:{oid}", calls)
        evidence("外部渠道时机", 事务内调用次数=0, 投递后事件键=calls)

    # ---------- 失败侧 ----------

    def test_投递失败退避重试且耗尽后进死信(self):
        """投不出去必须能被看见：重试耗尽转 dead 留在表里，管理员接口查得到。"""
        row = self.make_pending(event_key=f"{MARK}_fail", title="注定失败", content="投递会抛异常")
        with patch.object(notifier, "_deliver", side_effect=RuntimeError("下游不可用")):
            notifier.drain()
        first = self.row(row["id"])
        self.assertEqual(first["status"], notifier.PENDING)
        self.assertEqual(first["attempts"], 1)
        self.assertGreater(first["next_attempt_at"], datetime.now())
        self.assertEqual(first["last_error"], "下游不可用")
        # 退避期内不会被重新拉出来
        notifier.dispatch_once()
        self.assertEqual(self.row(row["id"])["attempts"], 1)

        with patch.object(notifier, "_deliver", side_effect=RuntimeError("下游不可用")):
            for _ in range(notifier._MAX_ATTEMPTS - 1):
                db.execute("UPDATE notification_outbox SET next_attempt_at = NOW() WHERE id=%s",
                           (row["id"],))
                notifier.dispatch_once()
        dead = self.row(row["id"])
        self.assertEqual(dead["status"], notifier.DEAD)
        self.assertEqual(dead["attempts"], notifier._MAX_ATTEMPTS)
        self.assertIn(row["id"], [d["id"] for d in notifier.dead_letters()])
        self.assertEqual(len(self.inbox(self.emp)), 0)
        evidence("投递失败可观测", 重试次数=dead["attempts"], 终态=dead["status"],
                 死信可见=row["id"] in [d["id"] for d in notifier.dead_letters()],
                 站内信数=len(self.inbox(self.emp)))

    def test_已投递行可回收而死信保留(self):
        row = self.make_pending(event_key=f"{MARK}_reap", title="可回收", content="投递成功")
        notifier.drain()
        db.execute("UPDATE notification_outbox SET sent_at = NOW() - INTERVAL 60 DAY WHERE id=%s",
                   (row["id"],))
        self.assertEqual(notifier.reap_sent(), 1)
        self.assertIsNone(self.row(row["id"]))
        # 死信不回收：那是给人工处理用的
        db.execute("INSERT INTO notification_outbox (event_key, audience, title, status, sent_at) "
                   "VALUES (%s,%s,'死信','dead',NOW() - INTERVAL 60 DAY)",
                   (f"{MARK}_dead", notifier.user_audience(self.emp)))
        self.assertEqual(notifier.reap_sent(), 0)
        evidence("发件箱回收", 已投递回收=1, 死信保留=True)


if __name__ == "__main__":
    unittest.main()
