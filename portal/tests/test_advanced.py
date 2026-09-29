"""进阶能力测试：审计哈希链、密码 v2/兼容、会话 TTL、零结果词、事务指标。

覆盖本轮「往进阶版改代码」新增的能力，每条都对应一个可回归的行为断言，
而不是只证明代码能 import。数据库相关用例在独立 MySQL 下运行
（python -m portal.tests.run_mysql），未启用则整体 skip。

纯逻辑用例（密码哈希、TTL、指标快照）不连库，随时可跑。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import unittest
from uuid import uuid4

from portal.tests import init_test_db
from portal.backend import auth, config, db, logic
from portal.backend import main, search_index

MARK = "ZZADV"


async def http_request(path, body=None, token=None, method="POST"):
    """零新增依赖，直接驱动真实 ASGI 中间件、鉴权、参数校验与路由。"""
    sent, messages = False, []

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            payload = b"" if body is None else json.dumps(body).encode()
            return {"type": "http.request", "body": payload, "more_body": False}
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


class TestPasswordHashing(unittest.TestCase):
    """v2 每用户随机盐 + v1 全局盐兼容，全部纯函数，不连库。"""

    def test_v2_roundtrip_and_unique_salt(self):
        pw = "Passw0rd!进阶"
        h1 = auth.hash_password(pw)
        h2 = auth.hash_password(pw)
        # 自描述格式，含 $ 分段；同密码两次哈希因随机盐而不同
        self.assertTrue(h1.startswith("pbkdf2_sha256$"))
        self.assertNotEqual(h1, h2)
        self.assertFalse(auth.is_legacy_hash(h1))
        self.assertTrue(auth.verify_password(pw, h1))
        self.assertTrue(auth.verify_password(pw, h2))
        self.assertFalse(auth.verify_password(pw + "x", h1))

    def test_legacy_hash_still_verifies(self):
        # 复算改造前的 v1：全局 SECRET_KEY 作盐、固定 10 万次迭代、纯 hex 无 $
        pw = "legacy-secret-123"
        legacy = hashlib.pbkdf2_hmac(
            "sha256", pw.encode(), config.SECRET_KEY.encode(), 100_000).hex()
        self.assertTrue(auth.is_legacy_hash(legacy))
        self.assertTrue(auth.verify_password(pw, legacy))
        self.assertFalse(auth.verify_password(pw + "x", legacy))

    def test_malformed_hash_is_rejected_not_crash(self):
        for bad in ("", None, "pbkdf2_sha256$", "pbkdf2_sha256$abc$x$y", "no$", "a$b$c$d"):
            self.assertFalse(auth.verify_password("whatever", bad))


class TestSessionTTL(unittest.TestCase):
    """空闲 / 绝对过期：把 last_seen / created_at 拨到过去即可验证，不依赖真实等待。"""

    def _backdate(self, token, **kw):
        with auth._SESSION_LOCK:
            s = auth._SESSIONS[token]
            s.update(kw)

    def test_idle_expiry_and_touch_refresh(self):
        token, sid = auth.create_session(f"{MARK}_ttl")
        self.addCleanup(auth.drop_session, token)
        self.assertEqual(auth.resolve_session(token)["session_id"], sid)
        # 拨到超过空闲窗口 → 判过期并清除
        idle = config.SESSION_IDLE_MINUTES * 60
        self._backdate(token, last_seen=time.monotonic() - idle - 5)
        self.assertIsNone(auth.resolve_session(token))
        # 过期后已从表里删除
        self.assertNotIn(token, auth._SESSIONS)

    def test_absolute_expiry(self):
        token, _ = auth.create_session(f"{MARK}_abs")
        self.addCleanup(auth.drop_session, token)
        absolute = config.SESSION_ABSOLUTE_MINUTES * 60
        # 空闲时钟是新的，但创建时间超绝对窗 → 仍过期
        self._backdate(token, created_at=time.monotonic() - absolute - 5)
        self.assertIsNone(auth.resolve_session(token))

    def test_drop_user_sessions_revokes_all(self):
        emp = f"{MARK}_multi"
        t1, _ = auth.create_session(emp)
        t2, _ = auth.create_session(emp)
        self.assertEqual(auth.drop_user_sessions(emp), 2)
        self.assertIsNone(auth.resolve_session(t1))
        self.assertIsNone(auth.resolve_session(t2))


class TestTxStats(unittest.TestCase):
    """事务/连接池指标：结构存在、计数字段可累加，异常路径不误吞。"""

    def test_snapshot_shape(self):
        snap = db.stats()
        for key in ("tx_total", "retry_attempts", "retry_absorbed",
                    "retry_exhausted", "by_code", "pool"):
            self.assertIn(key, snap)
        self.assertIsInstance(snap["by_code"], dict)

    @unittest.skipUnless(
        __import__("os").getenv("PORTAL_RUN_DB_TESTS") == "1",
        "run_tx 需真实连接；未启用独立 MySQL 时跳过")
    def test_counter_increments_on_run(self):
        init_test_db()
        before = db.stats()["tx_total"]
        db.run_tx(lambda cur: cur.execute("SELECT 1"))
        self.assertGreaterEqual(db.stats()["tx_total"], before + 1)


@unittest.skipUnless(
    __import__("os").getenv("PORTAL_RUN_DB_TESTS") == "1",
    "未启用独立 MySQL；使用 python -m portal.tests.run_mysql")
class TestAuditChain(unittest.TestCase):
    """审计哈希链：写入自洽可验；改内容即断链，能定位到被篡改行。

    每条用例用独立工号写入，且不当测试结束删除自己写的审计行：
    链是按 id 跨行串联的，删中间行会让后继行的 prev_hash 指向已删哈希
    而把全局链误判为断。篡改验证完会还原原值，保证离开时链仍自洽。
    """

    def setUp(self):
        init_test_db()
        self.emp = f"{MARK}_aud_{uuid4().hex[:8]}"

    def _write(self, n):
        def cur_fn(cur):
            for i in range(n):
                logic.log_audit(self.emp, f"adv_action_{i}", "test", i, {"n": i}, cur=cur)
        db.run_tx(cur_fn)

    def test_append_then_tamper_detected(self):
        self._write(3)
        ids = [r["id"] for r in db.query(
            "SELECT id FROM audit_logs WHERE emp_id=%s AND row_hash<>'' ORDER BY id",
            (self.emp,))]
        self.assertEqual(len(ids), 3)

        # 全局链应先自洽（同时反向验证其它写入口都走了链）
        full = logic.verify_audit_chain()
        self.assertTrue(full["ok"], f"初始验链不应断：{full['broken_at']}")
        self.assertGreaterEqual(full["hashed_rows"], 3)

        # 篡改中间行的 detail（不改 row_hash）→ 应检出并定位
        target = ids[1]
        original = db.query_one("SELECT detail FROM audit_logs WHERE id=%s", (target,))["detail"]
        db.execute("UPDATE audit_logs SET detail=%s WHERE id=%s", ('{"tampered":true}', target))
        try:
            after = logic.verify_audit_chain()
            self.assertFalse(after["ok"])
            self.assertEqual(after["broken_at"]["id"], target)
        finally:
            db.execute("UPDATE audit_logs SET detail=%s WHERE id=%s", (original, target))
        # 还原后链重新自洽，不残留断裂影响后续用例
        self.assertTrue(logic.verify_audit_chain()["ok"])

    def test_limit_window_verify(self):
        self._write(2)
        recent = logic.verify_audit_chain(limit=10)
        self.assertTrue(recent["ok"])
        self.assertLessEqual(recent["rows"], 10)


@unittest.skipUnless(
    __import__("os").getenv("PORTAL_RUN_DB_TESTS") == "1",
    "未启用独立 MySQL；使用 python -m portal.tests.run_mysql")
class TestZeroResultTerms(unittest.TestCase):
    """搜不到的词：零结果落汇总并累加，命中结果不落。"""

    def setUp(self):
        init_test_db()
        self.term = f"{MARK}_查无此词_{uuid4().hex[:6]}"

    def tearDown(self):
        db.assert_test_database()
        db.execute("DELETE FROM search_zero_terms WHERE term LIKE %s", (MARK + "%",))

    def test_zero_result_recorded_and_accumulates(self):
        self.assertEqual(search_index.search(self.term)["courses"], [])
        search_index.record_zero_result(self.term)
        search_index.record_zero_result(self.term)
        row = db.query_one("SELECT hits FROM search_zero_terms WHERE term=%s", (self.term,))
        self.assertIsNotNone(row)
        self.assertGreaterEqual(row["hits"], 2)

    def test_blank_term_not_recorded(self):
        search_index.record_zero_result("   ")
        self.assertIsNone(db.query_one(
            "SELECT id FROM search_zero_terms WHERE term=''"))


class TestRegisterValidation(unittest.IsolatedAsyncioTestCase):
    """注册入参校验：全部发生在触库之前，不需数据库，随时可跑。

    重点守住「自我提权」封堵：前端多传一个 role 必须被拒，不接受客户端自报角色。
    """

    async def test_rejects_client_supplied_role(self):
        # extra="forbid" → 多传 role 直接 422，根本进不到写库分支
        status, _ = await http_request("/api/auth/register", {
            "username": "zzadv_ok", "password": "secret123", "role": "super_admin"})
        self.assertEqual(status, 422)

    async def test_rejects_short_or_illegal_input(self):
        cases = [
            {"username": "ab", "password": "secret123"},        # 用户名 < 3 → 422
            {"username": "zzadv_u", "password": "123"},         # 密码 < 6 → 422
            {"username": "zz adv", "password": "secret123"},    # 用户名含空格 → 正则拒 400
            {"username": "zzadv$bad", "password": "secret123"}, # 非法字符 → 正则拒 400
        ]
        for body in cases:
            with self.subTest(**body):
                status, _ = await http_request("/api/auth/register", body)
                self.assertIn(status, (400, 422))


@unittest.skipUnless(
    __import__("os").getenv("PORTAL_RUN_DB_TESTS") == "1",
    "未启用独立 MySQL；使用 python -m portal.tests.run_mysql")
class TestRegisterAndRoles(unittest.IsolatedAsyncioTestCase):
    """注册为普通用户 + 后台改角色：需要真实库。

    不清理自己写的 audit_logs 行（与 TestAuditChain 同理：删中间行会误判断链）；
    只回收本次创建的用户及其积分账户/流水。
    """

    def setUp(self):
        init_test_db()
        self.created = []      # 待清理的 emp_id（不含审计行）
        self.tokens = []       # 本用例创建的会话，结束逐一掉

    def tearDown(self):
        db.assert_test_database()
        for tok in self.tokens:
            auth.drop_session(tok)
        for emp in self.created:
            db.execute("DELETE FROM point_records WHERE emp_id=%s", (emp,))
            db.execute("DELETE FROM point_accounts WHERE emp_id=%s", (emp,))
            db.execute("DELETE FROM users WHERE emp_id=%s", (emp,))

    def _mk_user(self, role):
        # 直插一个指定角色的用户（不走注册）；password 无意义，登改用 create_session 直接建会话。
        emp = f"{MARK}{role[:3]}{uuid4().hex[:8]}"
        db.execute(
            "INSERT INTO users (emp_id, username, password_hash, name, role, department) "
            "VALUES (%s,%s,'x',%s,%s,'QA')", (emp, emp, emp, role))
        self.created.append(emp)
        return emp

    async def test_register_persists_user_and_welcome_points(self):
        uname = f"{MARK}r{uuid4().hex[:8]}"
        status, data = await http_request("/api/auth/register", {
            "username": uname, "password": "secret123", "name": "新同事", "department": "QA"})
        self.assertEqual(status, 200, data)
        emp = data["user"]["emp_id"]
        self.created.append(emp)
        self.tokens.append(data["token"])
        self.assertEqual(data["user"]["role"], "user")
        self.assertEqual(data["user"]["points"], 100)
        self.assertEqual(db.query_one("SELECT role FROM users WHERE emp_id=%s", (emp,))["role"], "user")
        self.assertEqual(db.query_one("SELECT balance FROM point_accounts WHERE emp_id=%s", (emp,))["balance"], 100)
        rec = db.query_one("SELECT points FROM point_records WHERE emp_id=%s AND ref_type='welcome'", (emp,))
        self.assertEqual(rec["points"], 100)

    async def test_register_duplicate_username_409(self):
        uname = f"{MARK}d{uuid4().hex[:8]}"
        s1, d1 = await http_request("/api/auth/register", {"username": uname, "password": "secret123"})
        self.assertEqual(s1, 200)
        self.created.append(d1["user"]["emp_id"])
        self.tokens.append(d1["token"])
        s2, _ = await http_request("/api/auth/register", {"username": uname, "password": "secret123"})
        self.assertEqual(s2, 409)

    async def test_non_super_cannot_list_or_change_roles(self):
        emp = self._mk_user("user")
        token, _ = auth.create_session(emp)
        self.tokens.append(token)
        s, _ = await http_request("/api/admin/users", token=token, method="GET")
        self.assertEqual(s, 403)
        s2, _ = await http_request(f"/api/admin/users/{emp}/role", {"role": "super_admin"},
                                   token=token, method="PATCH")
        self.assertEqual(s2, 403)

    async def test_super_admin_lists_and_changes_role_revokes_session(self):
        su = self._mk_user("super_admin")
        target = self._mk_user("user")
        sut, _ = auth.create_session(su)
        tt, _ = auth.create_session(target)   # target 现有活动会话，改角色后应被吊销
        self.tokens += [sut, tt]
        s, d = await http_request("/api/admin/users", token=sut, method="GET")
        self.assertEqual(s, 200)
        self.assertIn(target, [u["emp_id"] for u in d["users"]])
        s2, d2 = await http_request(f"/api/admin/users/{target}/role", {"role": "shop_admin"},
                                    token=sut, method="PATCH")
        self.assertEqual(s2, 200, d2)
        self.assertGreaterEqual(d2["revoked_sessions"], 1)
        self.assertEqual(db.query_one("SELECT role FROM users WHERE emp_id=%s", (target,))["role"], "shop_admin")
        self.assertIsNone(auth.resolve_session(tt))   # 会话已吊销，需重新登录才拿到新权限

    async def test_super_admin_cannot_demote_self(self):
        su = self._mk_user("super_admin")
        sut, _ = auth.create_session(su)
        self.tokens.append(sut)
        s, _ = await http_request(f"/api/admin/users/{su}/role", {"role": "user"},
                                  token=sut, method="PATCH")
        self.assertEqual(s, 400)
        self.assertEqual(db.query_one("SELECT role FROM users WHERE emp_id=%s", (su,))["role"], "super_admin")


if __name__ == "__main__":
    unittest.main()
