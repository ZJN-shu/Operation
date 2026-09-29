"""阿里云 OSS 图片直传：服务端签受限 Post 凭证 + 对象 key 治理。

真实落地对象存储的正规姿势是「浏览器直传 + 服务端只签名不经手文件体」：应用
服务器不去接收/转发图片字节，只下发一张有时效、有体积上限、key 固定的 PostPolicy，
前端拿它直接 POST 到 OSS。好处是大文件不过应用、应用无状态、密钥永不下发到浏览器。

刻意**不引入 oss2 SDK**：PostPolicy 的签名就是 `base64(HMAC-SHA1(AccessKeySecret,
base64(policy_json)))`，标准库 hmac/hashlib/base64 足以实现，守住本项目零新增
Python 依赖的约定，也让签名逻辑可离线单测（纯函数 + 固定测试向量）。

诚实边界：能否真的传进阿里云，取决于有没有配 AK/SK + 出网 + 桶已建好。本模块把
「签名正确性、key 治理、体积/类型约束、开关行为」做成可离线回归的部分；真实桶的
端到端上传要用自己的凭证跑（见 make_upload_credential 产出的字段），不谎称已连通。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timedelta, timezone

from . import config

# 允许的图片扩展名（存进 object key 的后缀，也是前端选择框的 accept）
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
# object key 只允许这些字符：字母数字、下划线、连字符、点、斜杠——杜绝 ../ 与查询串注入
_KEY_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


# ---------- 纯签名原语（可离线单测，不读 config） ----------

def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def iso_expiration(from_utc: datetime, seconds: int) -> str:
    """OSS 要求的过期时刻：UTC + 毫秒 + Z，如 2026-09-27T08:36:09.000Z。"""
    exp = from_utc + timedelta(seconds=seconds)
    return exp.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build_policy(bucket: str, exact_key: str, max_bytes: int, expiration: str) -> dict:
    """构造 PostPolicy 文档。key 用 starts-with=精确 key 绑死，客户端不能改传目标路径。"""
    return {
        "expiration": expiration,
        "conditions": [
            {"bucket": bucket},
            ["starts-with", "$key", exact_key],
            ["content-length-range", 0, max_bytes],
        ],
    }


def sign_policy(access_key_secret: str, policy_b64: str) -> str:
    """OSS V1 Post 签名：base64(HMAC-SHA1(secret, policy_b64))。"""
    digest = hmac.new(access_key_secret.encode("utf-8"),
                      policy_b64.encode("utf-8"), hashlib.sha1).digest()
    return _b64(digest)


def policy_to_b64(policy: dict) -> str:
    # separators 去空格，保证同输入同字节串，签名才可复现
    return _b64(json.dumps(policy, separators=(",", ":")).encode("utf-8"))


# ---------- key 治理 ----------

def _safe_emp(emp_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "", (emp_id or ""))[:32] or "anon"


def _ext(filename: str) -> str:
    m = re.search(r"(\.[A-Za-z0-9]{1,5})$", filename or "")
    return m.group(1).lower() if m else ""


def new_object_key(emp_id: str, filename: str, now: datetime | None = None,
                   prefix: str | None = None) -> str:
    """生成受控 object key：<prefix><YYYYMM>/<安全工号>_<随机>.<ext>。

    随机段防枚举与覆盖；扩展名白名单挡 .svg/.html/.exe 伪装上传；工号清洗防注入。
    非法扩展名直接抛错，由调用方转成 4xx。
    """
    prefix = (prefix if prefix is not None else config.OSS_KEY_PREFIX) or ""
    ext = _ext(filename)
    if ext not in ALLOWED_EXT:
        raise ValueError("不支持的图片类型，仅允许 " + "/".join(sorted(e.lstrip('.') for e in ALLOWED_EXT)))
    now = now or datetime.now(timezone.utc)
    key = f"{prefix}{now.strftime('%Y%m')}/{_safe_emp(emp_id)}_{secrets.token_hex(8)}{ext}"
    if not _KEY_RE.match(key):
        raise ValueError("生成的对象名不合法")
    return key


def validate_object_key(key: str, prefix: str | None = None) -> bool:
    """绑定业务记录前核验 key：必须命中我们自己的前缀、字符安全、扩展名合法。

    防的是前端绕过直传，把一个任意外链 URL 或越权对象名塞进 image_key。
    """
    if not key or len(key) > 255 or not _KEY_RE.match(key):
        return False
    prefix = (prefix if prefix is not None else config.OSS_KEY_PREFIX) or ""
    if prefix and not key.startswith(prefix):
        return False
    return _ext(key) in ALLOWED_EXT


# ---------- 面向配置的高层封装 ----------

def is_configured() -> bool:
    """开关打开且四要素齐全才算真能签发凭证（缺一律视为未启用，避免半配置崩在签名上）。"""
    return bool(config.OSS_ENABLED and config.OSS_ENDPOINT and config.OSS_BUCKET
                and config.OSS_ACCESS_KEY_ID and config.OSS_ACCESS_KEY_SECRET)


def upload_host() -> str:
    return f"https://{config.OSS_BUCKET}.{config.OSS_ENDPOINT}"


def object_url(key: str) -> str | None:
    """拼对外展示用的图片地址。没配公开基址就返回 None（不伪造可访问 URL）。"""
    base = config.OSS_PUBLIC_BASE_URL
    if not base or not validate_object_key(key):
        return None
    return base.rstrip("/") + "/" + key.lstrip("/")


def make_upload_credential(emp_id: str, filename: str) -> dict:
    """给前端下发一次直传凭证。未配置时抛错，由路由翻成明确的 409（功能未启用）。

    返回字段即浏览器构造 multipart/form-data 直传 OSS 所需：把 file 放最后，
    其余按 key 顺序 append 即可。
    """
    if not is_configured():
        raise RuntimeError("OSS 未配置或已禁用")
    key = new_object_key(emp_id, filename)
    now = datetime.now(timezone.utc)
    expiration = iso_expiration(now, config.OSS_UPLOAD_EXPIRE_SECONDS)
    policy = build_policy(config.OSS_BUCKET, key, config.OSS_MAX_UPLOAD_BYTES, expiration)
    policy_b64 = policy_to_b64(policy)
    return {
        "host": upload_host(),
        "bucket": config.OSS_BUCKET,
        "key": key,
        "policy": policy_b64,
        "signature": sign_policy(config.OSS_ACCESS_KEY_SECRET, policy_b64),
        "OSSAccessKeyId": config.OSS_ACCESS_KEY_ID,
        "success_action_status": "200",
        "expires_at": expiration,
        "max_bytes": config.OSS_MAX_UPLOAD_BYTES,
    }


def capability() -> dict:
    """给前端的只读能力探测：只暴露「能不能用 + 约束」，绝不回传 AK/SK。"""
    enabled = is_configured()
    return {
        "enabled": enabled,
        "upload_host": upload_host() if enabled else None,
        "max_bytes": config.OSS_MAX_UPLOAD_BYTES,
        "expire_seconds": config.OSS_UPLOAD_EXPIRE_SECONDS,
        "accept": sorted(e.lstrip(".") for e in ALLOWED_EXT),
        "image_base_url": (config.OSS_PUBLIC_BASE_URL or None) if enabled else None,
    }
