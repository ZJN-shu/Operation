"""压测后正确性核对：不超卖 + 账实一致。全部走 HTTP API。

断言三件事：
  1) 礼品库存消耗 == 成功兑换行数（初始 1,000,000 减去现库存）；
  2) 库存非负、礼品存在；
  3) /api/admin/points/drift 积分为空（账户余额与流水合计无漂移）。
用法：python portal/perf/verify_loadtest.py [GIFT_ID] [INIT_STOCK]
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"
GIFT_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 7
INIT_STOCK = int(sys.argv[2]) if len(sys.argv) > 2 else 1_000_000


def req(method, path, body=None, token=""):
    data = None if body is None else json.dumps(body).encode()
    r = urllib.request.Request(BASE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if token:
        r.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(r, timeout=30) as x:
            return x.status, json.loads(x.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def main():
    _, lr = req("POST", "/api/auth/login", {"username": "admin", "password": "admin123"})
    tok = lr["token"]
    _, g = req("GET", "/api/user/gifts", token=tok)
    gifts = [x for x in g["gifts"] if x["id"] == GIFT_ID]
    if not gifts:
        print(f"[FAIL] 礼品 {GIFT_ID} 不在了")
        return 1
    gift = gifts[0]
    consumed = INIT_STOCK - gift["stock"]
    ok = (consumed == gift["redeemed"]) and (gift["stock"] >= 0)
    print(f"礼品{GIFT_ID}: 现库存={gift['stock']} 已兑换={gift['redeemed']} "
          f"库存消耗={consumed} cost={gift['points_cost']}")
    print(f"[{'PASS' if ok else 'FAIL'}] 不超卖核对：库存消耗({consumed}) == 兑换行数({gift['redeemed']}) "
          f"且库存>=0 -> {gift['stock'] >= 0}")

    _, dr = req("GET", "/api/admin/points/drift", token=tok)
    drift_rows = dr.get("items") or dr.get("drift") or dr.get("rows") or []
    print(f"[{'PASS' if not drift_rows else 'FAIL'}] 积分账实一致：drift={dr}")
    return 0 if ok and not drift_rows else 2


if __name__ == "__main__":
    sys.exit(main())
