"""兑换验收：真实 InnoDB 并发、逐写入故障注入、丢响应重试及退款对账。

普通发现只跑安全/模型测试；完整执行：python -m portal.tests.run_mysql
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import inspect
import json
import os
from pathlib import Path
import runpy
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import pymysql
from fastapi import HTTPException
from pydantic import ValidationError

from portal.tests import init_test_db
from portal.backend import auth, config, db, logic, main, metrics, redemption as domain
from portal.backend.routers import admin


def request_key():
    return uuid4().hex


def evidence(case, **data):
    """输出数据库实测值；不把预期值或测试通过数量伪装成业务数据。"""
    print("\n验收数据 " + json.dumps({"场景": case, **data}, ensure_ascii=False, default=str))


async def http_request(path, body=None, token=None, method="POST"):
    """零新增依赖，直接驱动真实 ASGI 中间件、鉴权、参数校验与路由。"""
    sent, messages = False, []
    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
        await asyncio.Event().wait()
    async def send(message):
        messages.append(message)
    headers = [(b"content-type", b"application/json")]
    if token:
        headers.append((b"authorization", ("Bearer " + token).encode()))
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
             "http_version": "1.1", "scheme": "http", "method": method,
             "path": path, "raw_path": path.encode(), "root_path": "", "query_string": b"",
             "headers": headers, "server": ("test", 80), "client": ("127.0.0.1", 1)}
    await main.app(scope, receive, send)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    data = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return status, json.loads(data) if data else {}


class TestSafety(unittest.TestCase):
    def test_默认免密入口404且不注册不访问数据库(self):
        self.assertFalse(config.DEMO_MODE)
        self.assertNotIn("/api/auth/dev-login", [getattr(r, "path", "") for r in main.app.routes])
        with patch.object(db, "_connect", side_effect=AssertionError("不应连接数据库")):
            self.assertEqual(asyncio.run(http_request("/api/auth/dev-login", {"role": "admin"}))[0], 404)
            self.assertEqual(asyncio.run(http_request("/api/user/gifts/1/redeem", {}))[0], 401)

    def test_非演示启动不灌默认用户(self):
        async def startup():
            async with main.lifespan(main.app):
                pass
        with patch.object(db, "init_db"), patch.object(main.seed, "seed") as seed, \
             patch.object(main.search_index, "rebuild_all"), patch.object(main.events, "bind_loop"), \
             patch.object(main.events, "start_poller"), patch.object(main.events, "stop_poller"):
            asyncio.run(startup())
            seed.assert_not_called()

    def test_数据库门禁在连接前失败(self):
        for name, user, enabled in (("ops_portal", "portal_test", "1"),
                                    ("ops_portal_test_12345678", "root", "1"),
                                    ("ops_portal_test_12345678", "portal_test", "0")):
            with self.subTest(name=name, user=user, enabled=enabled), \
                 patch.object(config, "MYSQL_DB", name), patch.object(config, "MYSQL_USER", user), \
                 patch.dict(os.environ, {"PORTAL_RUN_DB_TESTS": enabled}), \
                 patch.object(pymysql, "connect") as connect:
                with self.assertRaises(RuntimeError):
                    db._connect(False)
                connect.assert_not_called()

    def test_请求键与输入边界(self):
        cases = [(domain.RequestIn, {}), (domain.RequestIn, {"request_id": "short"}),
                 (domain.RequestIn, {"request_id": "非法字符0000"}),
                 (domain.RequestIn, {"request_id": "x" * 65}),
                 (admin.GiftIn, {"name": " "}), (admin.GiftIn, {"name": "礼品", "stock": -1}),
                 (admin.GiftIn, {"name": "礼品", "points_cost": -1}),
                 (admin.GiftIn, {"name": "礼品", "points_cost": 1.5}),
                 (admin.GiftIn, {"name": "礼品", "stock": True}),
                 (admin.GiftUpdate, {"stock": 10}), (admin.GiftUpdate, {"points_cost": None}),
                 (admin.GiftUpdate, {"status": "wrong"}),
                 (domain.StockIn, {"request_id": request_key(), "delta": "1", "reason": "补货"}),
                 (domain.CancelIn, {"reason": " "}),
                 (domain.RefundIn, {"reason": "退货", "returned": False}),
                 (domain.RefundIn, {"reason": "退货", "returned": 1}),
                 (admin.ShipIn, {"express": " "})]
        for model, data in cases:
            with self.subTest(model=model.__name__, data=data), self.assertRaises(ValidationError):
                model(**data)
        self.assertEqual(domain.RequestIn(request_id="A" * 64).request_id, "A" * 64)
        self.assertTrue(domain.RefundIn(reason="已入库", returned=True).returned)

    def test_测试配置不读取环境文件且不回退应用凭据(self):
        with patch.dict(os.environ, {"PORTAL_TESTING": "1", "MYSQL_DB": "application_sentinel",
                                    "MYSQL_USER": "application_sentinel"}, clear=True), \
             patch.object(Path, "read_text", side_effect=AssertionError("不得读取 .env")) as read:
            isolated = runpy.run_path(config.__file__)
        read.assert_not_called()
        self.assertEqual(isolated["MYSQL_USER"], "portal_test")
        self.assertEqual(isolated["MYSQL_DB"], "ops_portal_test_unconfigured")
        self.assertFalse(isolated["DEMO_MODE"])
        evidence("配置隔离", 环境文件读取次数=read.call_count, 应用凭据回退=False)

    def test_课程活动积分上下界与严格类型(self):
        rejected = 0
        for model in (admin.CourseIn, admin.CourseUpdate, admin.ActivityIn, admin.ActivityUpdate):
            for value in (-1, domain.MAX_POINTS + 1, 1.5, True, "10"):
                with self.subTest(model=model.__name__, value=value), self.assertRaises(ValidationError):
                    model(title="边界测试", points=value)
                rejected += 1
            for value in (0, domain.MAX_POINTS):
                self.assertEqual(model(title="边界测试", points=value).points, value)
        evidence("奖励模型边界", 非法参数拒绝数=rejected, 合法上下界通过数=8)

    def test_商城路由真实挂载权限依赖(self):
        for fn in (admin.adjust_gift_stock, admin.ship_order, admin.cancel_order, admin.refund_order,
                   admin.reconcile_orders):
            params = inspect.signature(fn).parameters
            dependency = params["user" if "user" in params else "_"].default.dependency
            for role in ("viewer", "content_admin", "user"):
                with self.subTest(fn=fn.__name__, role=role), self.assertRaises(HTTPException) as ctx:
                    dependency({"role": role})
                self.assertEqual(ctx.exception.status_code, 403)
            for role in ("super_admin", "shop_admin"):
                self.assertEqual(dependency({"role": role})["role"], role)


class InjectedFailure(RuntimeError):
    pass


@contextmanager
def writes(fail_at=None, disconnect=False):
    """在真正执行 SQL 后注入异常，走真实连接池及 run_tx 回滚，不模拟数据库。"""
    original = pymysql.cursors.Cursor.execute
    statements = []
    def execute(cursor, sql, args=None):
        result = original(cursor, sql, args)
        if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            statements.append(sql)
            if len(statements) == fail_at:
                if disconnect:
                    cursor.connection.close()
                else:
                    raise InjectedFailure(f"第 {fail_at} 个写步骤后失败")
        return result
    with patch.object(pymysql.cursors.Cursor, "execute", execute):
        yield statements


class TestRedemption(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_test_db()

    def setUp(self):
        self.emps, self.gifts = [], []
        self.operator = self.make_user(balance=0, role="shop_admin")
        self.actor = {"emp_id": self.operator, "role": "shop_admin"}

    def tearDown(self):
        db.assert_test_database()
        for emp in self.emps:
            for table, field in (("notifications", "emp_id"), ("audit_logs", "emp_id"),
                                 ("point_records", "emp_id"), ("point_ops", "target_emp_id"),
                                 ("point_accounts", "emp_id"), ("redemptions", "emp_id"), ("users", "emp_id")):
                db.execute(f"DELETE FROM {table} WHERE {field}=%s", (emp,))
        for gid in self.gifts:
            db.execute("DELETE FROM gift_stock_records WHERE gift_id=%s", (gid,))
            db.execute("DELETE FROM search_keywords WHERE doc_type='gift' AND doc_id=%s", (gid,))
            db.execute("DELETE FROM notifications WHERE ntype='low_stock' AND ref_id=%s", (gid,))
            db.execute("DELETE FROM gifts WHERE id=%s", (gid,))

    def make_user(self, balance=100, role="user"):
        emp = "ZZRD" + uuid4().hex[:20]
        self.emps.append(emp)
        db.execute("INSERT INTO users (emp_id,username,password_hash,name,role) VALUES (%s,%s,'x','验收用户',%s)",
                   (emp, emp, role))
        db.run_tx(lambda cur: logic.add_points(cur, emp, balance, "测试余额", "welcome", 0))
        return emp

    def make_gift(self, stock=4, cost=60, baseline=True):
        gid = db.insert("INSERT INTO gifts (name,points_cost,stock) VALUES ('验收礼品',%s,%s)", (cost, stock))
        self.gifts.append(gid)
        if baseline:
            db.run_tx(lambda cur: domain.stock_baseline(cur, {"id": gid, "stock": stock}, self.operator))
        return gid

    def redeem(self, gid, emp, key=None):
        return domain.redeem(gid, domain.RequestIn(request_id=key or request_key()), emp)

    def stock(self, gid):
        return db.query_one("SELECT stock FROM gifts WHERE id=%s", (gid,))["stock"]

    def balance(self, emp):
        return db.query_one("SELECT balance FROM point_accounts WHERE emp_id=%s", (emp,))["balance"]

    def assert_clean(self):
        report = domain.reconciliation()
        self.assertTrue(report["ok"], report)

    def business_data(self, emp, gid):
        debit = db.query_one("SELECT COUNT(*) n,COALESCE(SUM(points),0) total FROM point_records "
                             "WHERE emp_id=%s AND ref_type='redeem'", (emp,))
        refund = db.query_one("SELECT COUNT(*) n,COALESCE(SUM(points),0) total FROM point_records "
                              "WHERE emp_id=%s AND ref_type='refund'", (emp,))
        return {"余额": self.balance(emp), "库存": self.stock(gid),
                "订单数": db.query_one("SELECT COUNT(*) n FROM redemptions WHERE emp_id=%s", (emp,))["n"],
                "扣分流水数": debit["n"], "扣分合计": -int(debit["total"]),
                "退款流水数": refund["n"], "返分合计": int(refund["total"])}

    def parallel(self, functions):
        barrier = threading.Barrier(len(functions), timeout=10)
        def invoke(fn):
            barrier.wait()
            return fn()
        with ThreadPoolExecutor(max_workers=len(functions)) as pool:
            futures = [pool.submit(invoke, fn) for fn in functions]
            return [f.result(timeout=30) for f in futures]

    def snapshot(self):
        return {t: db.query(f"SELECT * FROM {t} ORDER BY {key}") for t, key in (
            ("users", "emp_id"), ("point_accounts", "emp_id"), ("point_records", "id"),
            ("redemptions", "id"), ("gifts", "id"), ("gift_stock_records", "id"),
            ("audit_logs", "id"), ("notifications", "id"), ("point_ops", "id"))}

    def test_多人抢最后一件仅一单且库存非负(self):
        gid = self.make_gift(stock=1, baseline=False)
        users = [self.make_user() for _ in range(12)]
        results = self.parallel([lambda emp=emp: self.redeem(gid, emp) for emp in users])
        self.assertEqual(sum(r["ok"] for r in results), 1)
        self.assertEqual(self.stock(gid), 0)
        self.assertEqual(sum(100 - self.balance(emp) for emp in users), 60)
        self.assertEqual(len(db.query("SELECT id FROM redemptions WHERE gift_id=%s", (gid,))), 1)
        self.assert_clean()
        evidence("12人抢最后1件", 请求数=len(results), 成功数=sum(r["ok"] for r in results),
                 拒绝数=sum(not r["ok"] for r in results), 库存=self.stock(gid),
                 总扣分=sum(100 - self.balance(emp) for emp in users),
                 扣分流水数=db.query_one("SELECT COUNT(*) n FROM point_records p JOIN redemptions r "
                                     "ON p.ref_id=r.id AND p.ref_type='redeem' WHERE r.gift_id=%s", (gid,))["n"])

    def test_同一用户并发不同礼品不超扣(self):
        emp = self.make_user()
        gifts = [self.make_gift(stock=2) for _ in range(10)]
        results = self.parallel([lambda gid=gid: self.redeem(gid, emp) for gid in gifts])
        self.assertEqual(sum(r["ok"] for r in results), 1)
        self.assertEqual(self.balance(emp), 40)
        self.assert_clean()
        evidence("同用户并发10种礼品", 初始余额=100, 请求数=len(results), 成功数=sum(r["ok"] for r in results),
                 总剩余库存=sum(self.stock(gid) for gid in gifts), **self.business_data(emp, gifts[0]))

    def test_同请求键并发和串行重放仅一单一次扣分(self):
        emp, gid, key = self.make_user(balance=1000), self.make_gift(stock=20), request_key()
        results = self.parallel([lambda: self.redeem(gid, emp, key) for _ in range(12)])
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(self.redeem(gid, emp, key), results[0])
        self.assertEqual(self.balance(emp), 940)
        self.assertEqual(self.stock(gid), 19)
        self.assertEqual(len(db.query("SELECT id FROM redemptions WHERE emp_id=%s", (emp,))), 1)
        self.assert_clean()
        evidence("同请求键重放", 并发数=len(results), 后续重试数=1,
                 不同订单响应数=len({r["redemption_id"] for r in results}), **self.business_data(emp, gid))

    def test_丢响应后改价下架重试取得原结果(self):
        emp, gid, key = self.make_user(), self.make_gift(), request_key()
        committed = []
        def lost_response():
            committed.append(self.redeem(gid, emp, key))
            raise ConnectionError("事务已提交，但响应未到客户端")
        with self.assertRaises(ConnectionError):
            lost_response()
        admin.update_gift(gid, admin.GiftUpdate(name="新名", points_cost=999, status="offline"), self.actor)
        before = self.snapshot()
        replay = self.redeem(gid, emp, key)
        self.assertEqual(replay, committed[0])
        self.assertEqual(self.snapshot(), before)
        self.assert_clean()
        evidence("丢响应且改价下架后重试", 原响应一致=replay == committed[0],
                 现价=999, 实际订单价格=replay["points_cost"], 重试数据变更=False, **self.business_data(emp, gid))

    def test_请求键作用域及异参冲突(self):
        gid, other, emp = self.make_gift(), self.make_gift(), self.make_user(balance=1000)
        self.redeem(gid, emp, "CaseKey123")
        with self.assertRaises(HTTPException) as ctx:
            self.redeem(other, emp, "CaseKey123")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertTrue(self.redeem(gid, emp, "casekey123")["ok"])
        self.assertTrue(self.redeem(gid, self.make_user(), "CaseKey123")["ok"])
        self.assert_clean()

    def test_管理员旧表单交错不覆盖库存(self):
        emp, gid = self.make_user(), self.make_gift(stock=1)
        stale = {"name": "旧表单改名", "stock": 1}
        self.redeem(gid, emp)
        token, _ = auth.create_session(self.operator)
        try:
            status, _ = asyncio.run(http_request(f"/api/admin/gifts/{gid}", stale, token, "PUT"))
            self.assertEqual(status, 422)
        finally:
            auth.drop_session(token)
        admin.update_gift(gid, admin.GiftUpdate(name=stale["name"]), self.actor)
        self.assertEqual(self.stock(gid), 0)
        self.assert_clean()
        evidence("管理员旧表单", 旧库存=stale["stock"], HTTP状态=status, **self.business_data(emp, gid))

    def test_库存调整与兑换交错且重试只调整一次(self):
        gid, emp = self.make_gift(stock=4), self.make_user()
        payload = domain.StockIn(request_id=request_key(), delta=5, reason="到货入库")
        results = self.parallel([lambda: self.redeem(gid, emp),
                                 lambda: domain.adjust_stock(gid, payload, self.operator)])
        self.assertEqual(self.stock(gid), 8)
        self.assertEqual(domain.adjust_stock(gid, payload, self.operator), results[1])
        changed = payload.model_copy(update={"delta": 6})
        with self.assertRaises(HTTPException):
            domain.adjust_stock(gid, changed, self.operator)
        self.assertEqual(self.stock(gid), 8)
        self.assert_clean()
        evidence("增量补货与兑换交错", 初始库存=4, 调整量=payload.delta, 调整请求含重试=2,
                 调整流水数=db.query_one("SELECT COUNT(*) n FROM gift_stock_records WHERE gift_id=%s AND kind='adjust'", (gid,))["n"],
                 **self.business_data(emp, gid))

    def test_不同礼品同库存请求键竞争只有一个成功(self):
        gifts = [self.make_gift(), self.make_gift()]
        payload = domain.StockIn(request_id=request_key(), delta=2, reason="入库")
        def adjust(gid):
            try:
                return domain.adjust_stock(gid, payload, self.operator)
            except HTTPException as exc:
                return {"ok": False, "status": exc.status_code}
        results = self.parallel([lambda gid=gid: adjust(gid) for gid in gifts])
        self.assertEqual(sum(r["ok"] for r in results), 1)
        self.assertEqual(sum(self.stock(gid) for gid in gifts), 10)
        self.assert_clean()

    def test_重复取消与退货退款仅返一次(self):
        for returned in (False, True):
            with self.subTest(returned=returned):
                emp, gid = self.make_user(), self.make_gift()
                oid = self.redeem(gid, emp)["redemption_id"]
                if returned:
                    domain.ship(oid, "SF-test", self.operator)
                results = self.parallel([lambda: domain.refund(oid, "已核实", self.operator, returned=returned)
                                         for _ in range(8)])
                self.assertTrue(all(r == results[0] for r in results))
                self.assertEqual(self.balance(emp), 100)
                self.assertEqual(self.stock(gid), 4)
                self.assertEqual(len(db.query("SELECT id FROM point_records WHERE ref_type='refund' AND ref_id=%s", (oid,))), 1)
                self.assertEqual(len(db.query("SELECT id FROM gift_stock_records WHERE kind='refund' AND ref_id=%s", (oid,))), 1)
                self.assert_clean()
                evidence("重复退货退款" if returned else "重复取消", 并发数=len(results),
                         状态=results[0]["status"], **self.business_data(emp, gid))

    def test_状态机与本人权限(self):
        emp, gid = self.make_user(), self.make_gift()
        oid = self.redeem(gid, emp)["redemption_id"]
        with self.assertRaises(HTTPException) as ctx:
            domain.refund(oid, "越权", self.operator, owner_only=True)
        self.assertEqual(ctx.exception.status_code, 404)
        with self.assertRaises(HTTPException):
            domain.refund(oid, "未发货不能退货", self.operator, returned=True)
        domain.refund(oid, "本人取消", emp, owner_only=True)
        with self.assertRaises(HTTPException):
            domain.ship(oid, "SF-test", self.operator)
        self.assert_clean()

    def test_发货取消竞争只进入合法状态(self):
        emp, gid = self.make_user(), self.make_gift()
        oid = self.redeem(gid, emp)["redemption_id"]
        def attempt(fn):
            try:
                return fn()
            except HTTPException as exc:
                return {"ok": False, "status": exc.status_code}
        results = self.parallel([lambda: attempt(lambda: domain.ship(oid, "SF-test", self.operator)),
                                 lambda: attempt(lambda: domain.refund(oid, "取消", emp, owner_only=True))])
        self.assertEqual(sum(r["ok"] for r in results), 1)
        self.assert_clean()
        evidence("发货与取消竞争", 成功数=sum(r["ok"] for r in results),
                 状态=db.query_one("SELECT status FROM redemptions WHERE id=%s", (oid,))["status"],
                 **self.business_data(emp, gid))

    def test_兑换退款禁止通用冲正且退款不算发放(self):
        emp, gid = self.make_user(), self.make_gift()
        issued, spent = metrics.points_issued(), metrics.points_spent()
        oid = self.redeem(gid, emp)["redemption_id"]
        self.assertEqual(metrics.points_spent(), spent + 60)
        domain.refund(oid, "取消", emp, owner_only=True)
        self.assertEqual((metrics.points_issued(), metrics.points_spent()), (issued, spent))
        evidence("退款指标口径", 退款后发放增量=metrics.points_issued() - issued,
                 退款后净消耗增量=metrics.points_spent() - spent, **self.business_data(emp, gid))
        for row in db.query("SELECT id FROM point_records WHERE emp_id=%s AND ref_type IN ('redeem','refund')", (emp,)):
            with self.assertRaises(HTTPException) as ctx:
                admin.revert_points(row["id"], admin.RevertIn(request_id=request_key()),
                                    {"emp_id": self.operator, "role": "super_admin"})
            self.assertEqual(ctx.exception.status_code, 400)
        self.assert_clean()

    def test_每个事务写入后异常均完整回滚并可重试(self):
        for kind in ("redeem", "cancel", "refund", "ship", "stock"):
            def prepare():
                emp, gid, key = self.make_user(), self.make_gift(baseline=False), request_key()
                if kind == "redeem":
                    return lambda: self.redeem(gid, emp, key)
                if kind == "stock":
                    payload = domain.StockIn(request_id=key, delta=2, reason="入库")
                    return lambda: domain.adjust_stock(gid, payload, self.operator)
                oid = self.redeem(gid, emp)["redemption_id"]
                if kind == "ship":
                    return lambda: domain.ship(oid, "SF-test", self.operator)
                if kind == "refund":
                    domain.ship(oid, "SF-test", self.operator)
                return lambda: domain.refund(oid, "取消或退货", self.operator, returned=kind == "refund")
            operation = prepare()
            with writes() as trace:
                operation()
            self.assertGreater(len(trace), 2)
            for step in range(1, len(trace) + 1):
                with self.subTest(kind=kind, step=step, sql=trace[step - 1]):
                    operation = prepare()
                    before = self.snapshot()
                    with self.assertRaises(InjectedFailure), writes(fail_at=step):
                        operation()
                    self.assertEqual(self.snapshot(), before)
                    self.assertTrue(operation()["ok"])
                    self.assert_clean()
            evidence("逐写步骤故障", 业务=kind, 注入次数=len(trace), 快照检查表数=len(before),
                     完整回滚数=len(trace), 重试成功数=len(trace))

    def test_事务断线不自动重放后半段(self):
        emp, gid, key = self.make_user(), self.make_gift(), request_key()
        before = self.snapshot()
        with self.assertRaises(pymysql.Error), writes(fail_at=4, disconnect=True):
            self.redeem(gid, emp, key)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.redeem(gid, emp, key)["ok"])
        self.assert_clean()
        evidence("事务中底层断线", 注入写步骤=4, 回滚后快照一致=True, **self.business_data(emp, gid))

    def test_对账发现账单库存及孤立流水且不改账(self):
        emp, gid = self.make_user(), self.make_gift()
        oid = self.redeem(gid, emp)["redemption_id"]
        self.assert_clean()
        db.execute("UPDATE point_accounts SET balance=balance+2 WHERE emp_id=%s", (emp,))
        db.execute("UPDATE gifts SET stock=stock+1 WHERE id=%s", (gid,))
        db.execute("UPDATE redemptions SET points_cost=points_cost+1 WHERE id=%s", (oid,))
        db.execute("INSERT INTO point_records (emp_id,points,ref_type,ref_id) VALUES (%s,1,'refund',2147483647)", (emp,))
        before = self.snapshot()
        report = domain.reconciliation()
        self.assertFalse(report["ok"])
        for field in ("accounts", "orders", "stocks", "orphan_records"):
            self.assertTrue(report[field], field)
        self.assertEqual(self.snapshot(), before)
        evidence("四维对账", **{field: len(report[field]) for field in ("accounts", "orders", "stocks", "orphan_records")},
                 检查后数据变更=False)

    def test_数据库唯一约束直接挡重复订单(self):
        emp, gid, key = self.make_user(), self.make_gift(), request_key()
        self.redeem(gid, emp, key)
        with self.assertRaises(pymysql.err.IntegrityError):
            db.execute("INSERT INTO redemptions (emp_id,gift_id,points_cost,request_id) VALUES (%s,%s,60,%s)",
                       (emp, gid, key))
        self.assert_clean()

    def test_提交确认丢失重试仍只有原订单(self):
        emp, gid, key = self.make_user(), self.make_gift(), request_key()
        original = pymysql.connections.Connection.commit
        def uncertain_commit(conn):
            original(conn)
            raise ConnectionError("COMMIT 已生效，但确认丢失")
        with patch.object(pymysql.connections.Connection, "commit", uncertain_commit), self.assertRaises(ConnectionError):
            self.redeem(gid, emp, key)
        before = self.snapshot()
        result = self.redeem(gid, emp, key)
        self.assertTrue(result["ok"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.balance(emp), 40)
        self.assert_clean()
        evidence("提交成功确认丢失", 重试数据变更=False, **self.business_data(emp, gid))

    def test_库存调整范围拒绝且无半笔业务(self):
        gid = self.make_gift(stock=1)
        for delta in (0, -2, domain.MAX_STOCK):
            before = self.snapshot()
            with self.subTest(delta=delta), self.assertRaises(HTTPException):
                domain.adjust_stock(gid, domain.StockIn(request_id=request_key(), delta=delta, reason="测试"), self.operator)
            self.assertEqual(self.snapshot(), before)

    def test_历史已冲正订单拒绝重复返还(self):
        emp, gid = self.make_user(), self.make_gift()
        oid = self.redeem(gid, emp)["redemption_id"]
        debit = db.query_one("SELECT id FROM point_records WHERE ref_type='redeem' AND ref_id=%s", (oid,))["id"]
        db.execute("INSERT INTO point_ops (op_type,target_emp_id,points,operator_emp_id,idem_key,reverted_record_id) "
                   "VALUES ('revert',%s,60,%s,%s,%s)", (emp, self.operator, request_key(), debit))
        before = self.snapshot()
        with self.assertRaises(HTTPException) as ctx:
            domain.refund(oid, "不能二次返还", self.operator)
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(domain.reconciliation()["orders"])

    def test_HTTP兑换重放取消退款与权限(self):
        emp, gid = self.make_user(), self.make_gift()
        token, _ = auth.create_session(emp)
        admin_token, _ = auth.create_session(self.operator)
        try:
            path, body = f"/api/user/gifts/{gid}/redeem", {"request_id": request_key()}
            self.assertEqual(asyncio.run(http_request(path, {}, token))[0], 422)
            status, result = asyncio.run(http_request(path, body, token))
            self.assertEqual(status, 200)
            self.assertTrue(result["ok"])
            self.assertEqual(asyncio.run(http_request(path, body, token))[1], result)
            oid = result["redemption_id"]
            refund_path = f"/api/admin/orders/{oid}/refund"
            self.assertEqual(asyncio.run(http_request(refund_path, {"reason": "退货", "returned": True}, token))[0], 403)
            self.assertEqual(asyncio.run(http_request(refund_path, {"reason": "退货", "returned": False}, admin_token))[0], 422)
            cancel_path = f"/api/user/orders/{oid}/cancel"
            self.assertEqual(asyncio.run(http_request(cancel_path, {"reason": "本人取消"}, admin_token))[0], 404)
            status, cancelled = asyncio.run(http_request(cancel_path, {"reason": "本人取消"}, token))
            self.assertEqual((status, cancelled["status"]), (200, "cancelled"))
            self.assertEqual(asyncio.run(http_request(cancel_path, {"reason": "重试"}, token))[1], cancelled)
            status, report = asyncio.run(http_request("/api/admin/orders/reconciliation", token=admin_token, method="GET"))
            self.assertEqual(status, 200)
            self.assertTrue(report["ok"])
        finally:
            auth.drop_session(token)
            auth.drop_session(admin_token)
        self.assert_clean()

    def test_账本余额相等仍能发现整笔订单未记账(self):
        emp, gid = self.make_user(), self.make_gift()
        oid = db.insert("INSERT INTO redemptions (emp_id,gift_id,points_cost) VALUES (%s,%s,60)", (emp, gid))
        before = self.snapshot()
        report = domain.reconciliation()
        self.assertFalse(report["accounts"])
        self.assertEqual([r["id"] for r in report["orders"]], [oid])
        self.assertEqual(self.snapshot(), before)
        evidence("整笔订单漏记账", 账户差异数=len(report["accounts"]), 订单差异数=len(report["orders"]),
                 扣分流水数=report["orders"][0]["debit_count"], 出库流水数=report["orders"][0]["stock_out"])

    def test_历史非法礼品价格不会反向加分(self):
        emp = self.make_user()
        for cost in (-60, domain.MAX_POINTS + 1):
            gid = self.make_gift(cost=cost)
            before = self.snapshot()
            with self.subTest(cost=cost), self.assertRaises(HTTPException) as ctx:
                self.redeem(gid, emp)
            self.assertEqual(ctx.exception.status_code, 409)
            self.assertEqual(self.snapshot(), before)
            evidence("历史非法价格", 价格=cost, 拒绝状态=ctx.exception.status_code, **self.business_data(emp, gid))

    def test_测试账号没有系统用户表权限(self):
        identity = db.query_one("SELECT DATABASE() db, CURRENT_USER() account, VERSION() version")
        self.assertEqual(identity["db"], config.MYSQL_DB)
        self.assertEqual(identity["account"], "portal_test@127.0.0.1")
        with self.assertRaises(pymysql.err.OperationalError) as ctx:
            db.query("SELECT User FROM mysql.user LIMIT 1")
        self.assertIn(ctx.exception.args[0], (1142, 1143))
        evidence("真实数据库隔离", **identity, 系统表访问拒绝码=ctx.exception.args[0])

    def test_旧表迁移保留历史版本且新订单使用新版本(self):
        conn = db._connect()
        try:
            with conn.cursor() as cur:
                # 临时表只遮蔽当前连接，绝不改动正常订单表。
                cur.execute("CREATE TEMPORARY TABLE redemptions (id INT PRIMARY KEY, gift_id INT, "
                            "emp_id VARCHAR(32), points_cost INT) ENGINE=InnoDB")
                try:
                    cur.execute("INSERT INTO redemptions VALUES (1,1,'legacy',60)")
                    db._migrate(cur)
                    db._migrate(cur)
                    cur.execute("INSERT INTO redemptions (id,gift_id,emp_id,points_cost) VALUES (2,1,'new',60)")
                    cur.execute("SELECT id,integrity_version,request_id FROM redemptions ORDER BY id")
                    rows = cur.fetchall()
                    self.assertEqual([r["integrity_version"] for r in rows], [0, 1])
                    self.assertTrue(all(r["request_id"] is None for r in rows))
                finally:
                    cur.execute("DROP TEMPORARY TABLE redemptions")
        finally:
            conn.rollback()
            conn.close()

    def test_迁移可重复执行(self):
        emp, gid, key = self.make_user(), self.make_gift(), request_key()
        result = self.redeem(gid, emp, key)
        db.init_db()
        db.init_db()
        self.assertEqual(self.redeem(gid, emp, key), result)
        self.assert_clean()


class TestSeedIntegrity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_test_db()

    def test_演示兑换同样具有完整事务流水(self):
        self.assertEqual(db.query_one("SELECT COUNT(*) c FROM users")["c"], 0)
        tables = ("users", "point_accounts", "point_records", "point_ops", "announcements", "courses",
                  "activities", "gifts", "redemptions", "gift_stock_records", "announcement_reads",
                  "training_progress", "activity_participants", "user_access_logs", "audit_logs",
                  "notifications", "quiz_questions", "quiz_attempts")
        keys = {t: "emp_id" if t in ("users", "point_accounts") else "id" for t in tables}
        before = {t: {r[keys[t]] for r in db.query(f"SELECT {keys[t]} FROM {t}")} for t in tables}
        try:
            self.assertTrue(main.seed.seed())
            self.assertFalse(main.seed.seed())
            self.assertEqual(db.query_one("SELECT COUNT(*) c FROM redemptions")["c"], 3)
            report = domain.reconciliation()
            self.assertTrue(report["ok"], report)
        finally:
            db.assert_test_database()
            # 仅清理本测试新产生的主键；不借用任何原有用户或管理员审计。
            for t in reversed(tables):
                added = {r[keys[t]] for r in db.query(f"SELECT {keys[t]} FROM {t}")} - before[t]
                for key in added:
                    db.execute(f"DELETE FROM {t} WHERE {keys[t]}=%s", (key,))


if __name__ == "__main__":
    unittest.main()
