"""测试入口先隔离配置；普通发现只跑无数据库测试，不读取应用 .env。"""
import os
import unittest

os.environ["PORTAL_TESTING"] = "1"


def init_test_db():
    from portal.backend import db
    if os.getenv("PORTAL_RUN_DB_TESTS") != "1":
        raise unittest.SkipTest("未启用独立 MySQL；使用 python -m portal.tests.run_mysql")
    # 显式启用后，权限、迁移和连接错误都是失败，不能包装成跳过。
    db.assert_test_database()
    db.init_db()
