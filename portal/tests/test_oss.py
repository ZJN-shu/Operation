"""OSS 直传：签名、对象 key 治理、开关行为的离线测试。

这些用例不打真实阿里云、不连库、不出网——它们验证「服务端这层逻辑正确」：
签名公式、policy 结构、key 白名单、体积/过期约束、未配置时的行为。
真实桶的端到端上传要用自己的 AK/SK 跑，不在自动化断言里谎称已连通。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from portal.backend import config, oss


class TestPolicySigning(unittest.TestCase):
    """纯签名原语：固定输入 → 可复现输出。"""

    def test_sign_policy_is_hmac_sha1_base64(self):
        secret = "testsecret"
        policy_b64 = base64.b64encode(b"{}").decode()
        expected = base64.b64encode(
            hmac.new(secret.encode(), policy_b64.encode(), hashlib.sha1).digest()
        ).decode()
        self.assertEqual(oss.sign_policy(secret, policy_b64), expected)

    def test_policy_doc_binds_key_and_size(self):
        exp = "2026-09-27T08:36:09.000Z"
        doc = oss.build_policy("mybucket", "gifts/202609/u_1.png", 1024, exp)
        self.assertEqual(doc["expiration"], exp)
        # key 用精确值作 starts-with 前缀 → 客户端不能改传目标对象
        self.assertIn(["starts-with", "$key", "gifts/202609/u_1.png"], doc["conditions"])
        self.assertIn(["content-length-range", 0, 1024], doc["conditions"])
        self.assertIn({"bucket": "mybucket"}, doc["conditions"])

    def test_policy_b64_compact_and_decodes(self):
        doc = {"expiration": "x", "conditions": []}
        b = oss.policy_to_b64(doc)
        self.assertNotIn(" ", base64.b64decode(b).decode())  # separators 去空格，签名可复现
        self.assertEqual(json.loads(base64.b64decode(b)), doc)

    def test_iso_expiration_format(self):
        base = datetime(2026, 9, 27, 8, 36, 9, tzinfo=timezone.utc)
        self.assertEqual(oss.iso_expiration(base, 300), "2026-09-27T08:41:09.000Z")


class TestObjectKey(unittest.TestCase):
    fixed_now = datetime(2026, 9, 27, 1, 2, 3, tzinfo=timezone.utc)

    def test_key_shape_and_prefix(self):
        key = oss.new_object_key("E-1001/x", "照片.PNG", now=self.fixed_now)
        self.assertTrue(key.startswith("gifts/202609/"))
        self.assertTrue(key.endswith(".png"))         # 扩展名归一小写
        self.assertIn("E-1001x_", key)                # 工号里的斜杠被清洗，连字符保留
        self.assertTrue(oss.validate_object_key(key))  # 自己生成的必然通过核验

    def test_bad_extension_rejected(self):
        for name in ("evil.svg", "page.html", "run.exe", "noext"):
            with self.assertRaises(ValueError):
                oss.new_object_key("E1", name, now=self.fixed_now)

    def test_validate_rejects_foreign_or_traversal(self):
        for bad in ("", "https://evil.com/a.png", "../secret.png",
                    "other/202609/x.png", "gifts/202609/x.svg", "gifts/a.png?x=1",
                    "x" * 300):
            self.assertFalse(oss.validate_object_key(bad), bad)

    def test_object_url_only_when_public_base(self):
        key = "gifts/202609/E1_abcd.png"
        with patch.object(config, "OSS_PUBLIC_BASE_URL", ""):
            self.assertIsNone(oss.object_url(key))
        with patch.object(config, "OSS_PUBLIC_BASE_URL", "https://cdn.example.com/"):
            self.assertEqual(oss.object_url(key), "https://cdn.example.com/gifts/202609/E1_abcd.png")
            # 非法 key 不拼 URL
            self.assertIsNone(oss.object_url("https://evil/x.png"))


class TestGatingAndCredential(unittest.TestCase):
    def test_not_configured_by_default_in_test(self):
        # 测试环境 OSS_ENABLED 强制 False
        self.assertFalse(oss.is_configured())
        self.assertFalse(oss.capability()["enabled"])
        with self.assertRaises(RuntimeError):
            oss.make_upload_credential("E1", "a.png")

    def test_credential_when_configured(self):
        with patch.object(config, "OSS_ENABLED", True), \
             patch.object(config, "OSS_ENDPOINT", "oss-cn-hangzhou.aliyuncs.com"), \
             patch.object(config, "OSS_BUCKET", "mybucket"), \
             patch.object(config, "OSS_ACCESS_KEY_ID", "testak"), \
             patch.object(config, "OSS_ACCESS_KEY_SECRET", "testsk"), \
             patch.object(config, "OSS_MAX_UPLOAD_BYTES", 1024):
            cred = oss.make_upload_credential("E-1", "pic.png")
        # host 由 bucket+endpoint 拼出
        self.assertEqual(cred["host"], "https://mybucket.oss-cn-hangzhou.aliyuncs.com")
        self.assertEqual(cred["OSSAccessKeyId"], "testak")
        self.assertTrue(oss.validate_object_key(cred["key"]))
        # 签名可用同一 secret 复算验证（凭证自洽）
        self.assertEqual(cred["signature"], oss.sign_policy("testsk", cred["policy"]))
        # policy 解码后确实绑了这个 key 和体积上限
        doc = json.loads(base64.b64decode(cred["policy"]))
        self.assertIn(["starts-with", "$key", cred["key"]], doc["conditions"])
        self.assertIn(["content-length-range", 0, 1024], doc["conditions"])
        # 凭证里绝不回传密钥
        self.assertNotIn("testsk", json.dumps(cred))

    def test_capability_shape_configured(self):
        with patch.object(config, "OSS_ENABLED", True), \
             patch.object(config, "OSS_ENDPOINT", "ep"), \
             patch.object(config, "OSS_BUCKET", "bk"), \
             patch.object(config, "OSS_ACCESS_KEY_ID", "ak"), \
             patch.object(config, "OSS_ACCESS_KEY_SECRET", "sk"), \
             patch.object(config, "OSS_PUBLIC_BASE_URL", "https://cdn/x"):
            cap = oss.capability()
        self.assertTrue(cap["enabled"])
        self.assertEqual(cap["upload_host"], "https://bk.ep")
        self.assertEqual(cap["image_base_url"], "https://cdn/x")
        self.assertIn("png", cap["accept"])


if __name__ == "__main__":
    unittest.main()
