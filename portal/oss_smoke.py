"""OSS 真实直传冒烟脚本：用 backend/oss 签出的凭证，真打一次 PostObject 到阿里云。

用法（仓库根目录下，.env 已填好 AK/SK 且 OSS_ENABLED=1）：
    python -m portal.oss_smoke [文件名]

不带参数时内置一张 1x1 PNG 合成图，验证「签名被 OSS 接受 + 对象写入成功」这条闭不闭；
带参数则把你指定的真实图片传上去，并打印可访问 URL（需桶/对象为 public-read 才对外可见）。

这条路径刻意只用标准库（urllib + 手写 multipart），与 backend/oss.py 保持一致——
证明零 oss2 依赖也能真实落桶。失败时原样打印 OSS 返回的 XML <Code>，便于按错误码定位
（如 SignatureDoesNotMatch / AccessDenied / NoSuchBucket / InvalidAccessKeyId）。
"""
from __future__ import annotations

import base64
import sys
import urllib.error
import urllib.request
import uuid

from portal.backend import config, oss

# 一张合法的 1x1 透明 PNG，作为无参自测的最小文件体
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M8PAAAFGUdvALGWAAAAAElFTkSuQmCC"
)


def _build_multipart(host_fields: dict, file_field: str, filename: str,
                     content_type: str, file_bytes: bytes) -> tuple[bytes, str]:
    """按 OSS PostObject 要求组装 multipart/form-data：普通字段在前，file 放最后。"""
    boundary = f"----opsPortal{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in host_fields.items():
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
        parts.append(f"{value}\r\n".encode())
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode())
    parts.append(file_bytes + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), boundary


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows 控制台默认 GBK，打不出表情
    except Exception:
        pass
    print(f"bucket={config.OSS_BUCKET}  endpoint={config.OSS_ENDPOINT}  "
          f"enabled={config.OSS_ENABLED}  configured={oss.is_configured()}")
    if not oss.is_configured():
        print("→ OSS 未启用或 AK/SK 未填齐（is_configured()=False）。请检查 portal/.env。")
        return 2

    if len(sys.argv) > 1:
        path = sys.argv[1]
        with open(path, "rb") as fh:
            file_bytes = fh.read()
        filename = path.split("/")[-1].split("\\")[-1]
    else:
        file_bytes = _TINY_PNG
        filename = "smoke.png"
    print(f"→ 拟上传：{filename}（{len(file_bytes)} 字节）")

    cred = oss.make_upload_credential("smoketest", filename)
    print(f"   object key = {cred['key']}")
    print(f"   过期时刻    = {cred['expires_at']}")

    fields = {
        "key": cred["key"],
        "OSSAccessKeyId": cred["OSSAccessKeyId"],
        "policy": cred["policy"],
        "signature": cred["signature"],
        "success_action_status": cred["success_action_status"],
        "Content-Type": "image/png",
    }
    body, boundary = _build_multipart(fields, "file", filename, "image/png", file_bytes)
    req = urllib.request.Request(
        cred["host"], data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "Content-Length": str(len(body))})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"✅ 直传成功：HTTP {resp.status}")
    except urllib.error.HTTPError as e:
        print(f"❌ OSS 返回 HTTP {e.code}")
        print(e.read().decode("utf-8", "replace"))
        return 1
    except urllib.error.URLError as e:
        print(f"❌ 网络不可达（出网/endpoint/DNS）：{e.reason}")
        return 1

    url = oss.object_url(cred["key"])
    print(f"   object_url = {url or '（未配 OSS_PUBLIC_BASE_URL，无法拼公开 URL）'}")
    if url:
        with urllib.request.urlopen(url, timeout=30) as probe:
            print(f"   公开读取探活：HTTP {probe.status}，{len(probe.read())} 字节")
    print("下一步：把这张 key 通过后台礼品表单绑定到 image_key 即完成真实链路。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
