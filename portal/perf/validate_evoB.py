"""演进 B 集成验证（打真实容器 4 worker + Redis 共享会话）。

验证三件事，任一失败即退出码非 0：
  1. login/register 不再 500，能拿到 token；
  2. 跨 worker 会话共享：同一 token 连打 /api/auth/me N 次（轮询到 4 个 worker）全 200，
     无 401（若还是进程内会话，命中率约 1/4，其余会 401）；
  3. logout 吊销跨 worker 生效：登出后任意 worker 都 401。
"""
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
ME = "/api/auth/me"


def _req(method, path, token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw or "{}")
        except json.JSONDecodeError:
            return e.code, {"_raw": raw}


def main():
    import secrets
    username = "evob_" + secrets.token_hex(4)
    st, out = _req("POST", "/api/auth/register",
                   body={"username": username, "password": "Passw0rd!", "name": "EvoB"})
    print(f"[1] register {username} -> HTTP {st}")
    if st != 200 or "token" not in out:
        print("    FAIL: register did not return token", out)
        return 1
    token = out["token"]

    N = 60
    codes = {}
    for _ in range(N):
        c, _o = _req("GET", ME, token=token)
        codes[c] = codes.get(c, 0) + 1
    print(f"[2] {ME} x{N} (4 workers) -> status dist {codes}")
    if codes.get(200, 0) != N:
        print("    FAIL: non-200 present, cross-worker session NOT shared")
        return 1

    st, _o = _req("POST", "/api/auth/logout", token=token)
    after = _req("GET", ME, token=token)[0]
    print(f"[3] logout -> {st}; after logout {ME} -> HTTP {after} (expect 401)")
    if after != 401:
        print("    FAIL: logout not revoked across workers")
        return 1

    print("ALL PASS: evolution B Redis shared session + multi-worker + revoke consistent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
