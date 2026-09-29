"""压测遗留数据清理（第二层演进收尾）。

压测/售罄/手工兑换用例通过真实 HTTP API 造了一批一次性资产：20 个 perf 号、
一批 cost=1 的「压测礼品 / 售罄礼品」，以及它们派生的兑换单、积分流水/账户/操作、
库存流水/桶、通知与发件箱、审计链行。这些是**测试残留**，不属于演示数据集，
需清干净让库回到「只有 demo 礼品(1-6) + 真实用户」的状态。

安全边界（脚本自身守住）：
  - 清除集 = perf 用户（username LIKE 'perf%'） ∪ 压测礼品（name 前缀 压测/售罄）。
    真实用户从未兑过压测礼品（盘点已验证：collision=0），所以按这个并集删不会伤及 demo。
  - demo 礼品 1-6、非 perf 的真实用户一律保留。
  - 审计哈希链不能只删中间行（会让后继行 prev_hash 指向已删哈希→全局误判断链）：
    删完 perf 审计行后，对**存活的真实审计行按 (chain_id, id) 全量重算**，
    复用与后端完全一致的 db.stable_slot / db._audit_digest 口径，链重新自洽。

默认 dry-run：只统计将删多少、保留什么，不落任何删除。真正执行需 APPLY=1。
连的是容器 MySQL（宿主 3308），凭据走环境变量，绝不写死。

用法（项目根）：
    python -m portal.perf.cleanup_loadtest                   # 预览（不删）
    $env:APPLY="1"; python -m portal.perf.cleanup_loadtest   # 真删
可选覆盖：MYSQL_HOST/MYSQL_PORT/MYSQL_USER/MYSQL_PASSWORD/MYSQL_DB
"""
from __future__ import annotations

import os

# 必须在 import portal.backend.db 之前设好连接环境变量（config 在 import 时读取）。
os.environ.setdefault("MYSQL_HOST", "127.0.0.1")
os.environ.setdefault("MYSQL_PORT", "3308")
os.environ.setdefault("MYSQL_USER", "root")
os.environ.setdefault("MYSQL_PASSWORD", os.getenv("MYSQL_ROOT_PASSWORD", "rootpw-change-me"))
os.environ.setdefault("MYSQL_DB", "ops_portal")

from portal.backend import db  # noqa: E402

APPLY = os.getenv("APPLY") == "1"

# 压测/售罄礼品按首字匹配（name 前缀唯一标识测试礼品）。用 HEX(LEFT(name,1)) 比较单个
# 汉字的 utf-8 字节 hex，绕开命令行中文编码坑：压=E58E8B、售=E594AE（demo 礼品首字不撞）。
PERF_NAME_PREFIXES = ("压测", "售罄")
_PERF_FIRST_HEX = tuple(p[0].encode("utf-8").hex().upper() for p in PERF_NAME_PREFIXES)
_LIKE = " OR ".join(["HEX(LEFT(name, 1)) = %s"] * len(_PERF_FIRST_HEX))
_LIKE_ARGS = list(_PERF_FIRST_HEX)


def _ph(vals: list) -> str:
    """展开成 '(%s,...)' 括号片段（调用方自带 IN）。空列表返回 '(-1)'（永假，避免 'IN ()'）。"""
    if not vals:
        return "(-1)"
    return "(" + ",".join(["%s"] * len(vals)) + ")"


def scan_counts(cur, perf: list, gifts: list) -> dict:
    per, gph = _ph(perf), _ph(gifts)
    # (SQL, args)：字面通配符一律作参数传入，绝不拼进 SQL 串（避开 pymysql 的 % 格式解析）。
    queries = [
        ("perf用户", "SELECT COUNT(*) n FROM users WHERE username LIKE %s", ("perf%",)),
        ("压测礼品", f"SELECT COUNT(*) n FROM gifts WHERE id IN {gph}", gifts),
        ("兑换单", f"SELECT COUNT(*) n FROM redemptions WHERE emp_id IN {per} OR gift_id IN {gph}",
         perf + gifts),
        ("积分流水", f"SELECT COUNT(*) n FROM point_records WHERE emp_id IN {per}", perf),
        ("积分账户", f"SELECT COUNT(*) n FROM point_accounts WHERE emp_id IN {per}", perf),
        ("积分操作", f"SELECT COUNT(*) n FROM point_ops WHERE target_emp_id IN {per}", perf),
        ("库存流水", f"SELECT COUNT(*) n FROM gift_stock_records WHERE gift_id IN {gph}", gifts),
        ("库存桶", f"SELECT COUNT(*) n FROM gift_stock_bucket WHERE gift_id IN {gph}", gifts),
        ("通知", f"SELECT COUNT(*) n FROM notifications WHERE emp_id IN {per}", perf),
        ("发件箱", "SELECT COUNT(*) n FROM notification_outbox WHERE audience IN "
         f"(SELECT CONCAT('user:', emp_id) FROM users WHERE emp_id IN {per})", perf),
        ("审计行(perf)", f"SELECT COUNT(*) n FROM audit_logs WHERE emp_id IN {per}", perf),
        ("埋点(perf)", f"SELECT COUNT(*) n FROM user_access_logs WHERE emp_id IN {per}", perf),
        ("——审计行(总计)", "SELECT COUNT(*) n FROM audit_logs", ()),
        ("——埋点(总计)", "SELECT COUNT(*) n FROM user_access_logs", ()),
        ("——真实用户", "SELECT COUNT(*) n FROM users WHERE username NOT LIKE %s", ("perf%",)),
    ]
    out = {}
    for label, sql, args in queries:
        cur.execute(sql, args or None)
        out[label] = cur.fetchone()["n"]
    return out


def build_deletes(perf: list, gifts: list) -> list[tuple[str, list]]:
    """返回按安全顺序排列的 (SQL, args) 删除语句。users 最后删（前面靠它的子查询已物化）。"""
    per, gph = _ph(perf), _ph(gifts)
    return [
        (f"DELETE FROM redemptions WHERE emp_id IN {per} OR gift_id IN {gph}", perf + gifts),
        (f"DELETE FROM notification_outbox WHERE audience IN "
         f"(SELECT CONCAT('user:', emp_id) FROM users WHERE emp_id IN {per})", perf),
        (f"DELETE FROM notifications WHERE emp_id IN {per}", perf),
        (f"DELETE FROM point_ops WHERE target_emp_id IN {per}", perf),
        (f"DELETE FROM point_records WHERE emp_id IN {per}", perf),
        (f"DELETE FROM point_accounts WHERE emp_id IN {per}", perf),
        (f"DELETE FROM gift_stock_records WHERE gift_id IN {gph}", gifts),
        (f"DELETE FROM gift_stock_bucket WHERE gift_id IN {gph}", gifts),
        (f"DELETE FROM user_access_logs WHERE emp_id IN {per}", perf),
        (f"DELETE FROM audit_logs WHERE emp_id IN {per}", perf),
        (f"DELETE FROM gifts WHERE id IN {gph}", gifts),
        (f"DELETE FROM users WHERE emp_id IN {per}", perf),
    ]


def rebuild_audit_chain(cur) -> int:
    """对存活审计行按 (chain_id, id) 全量重算哈希链，锚点重置。与 db._backfill_audit_chain
    的 full=True 分支同口径：先清空 prev/row_hash + 重分链 + 重置锚点，再逐链串接。"""
    n = db.config.AUDIT_CHAIN_SHARDS
    cur.execute("SELECT id, emp_id FROM audit_logs ORDER BY id")
    rows = cur.fetchall() or []
    for r in rows:
        cur.execute("UPDATE audit_logs SET prev_hash='', row_hash='', chain_id=%s WHERE id=%s",
                    (db.stable_slot(r["emp_id"], n), r["id"]))
    cur.execute("UPDATE audit_chain SET last_hash='', seq=0")
    for shard in range(n):
        cur.execute(
            "SELECT id, emp_id, action, target_type, IFNULL(target_id,-1) tid, "
            "IFNULL(detail,'') d, created_at FROM audit_logs "
            "WHERE chain_id=%s AND row_hash='' ORDER BY id", (shard,))
        srows = cur.fetchall() or []
        if not srows:
            continue
        prev, seq, last = "", 0, None
        for r in srows:
            fields = (str(r["emp_id"]), str(r["action"]), str(r["target_type"]),
                      str(r["tid"]), str(r["d"]), str(r["created_at"]))
            last = db._audit_digest(prev, fields)
            cur.execute("UPDATE audit_logs SET prev_hash=%s, row_hash=%s WHERE id=%s",
                        (prev, last, r["id"]))
            prev, seq = last, seq + 1
        cur.execute("INSERT INTO audit_chain (id, last_hash, seq) VALUES (%s,%s,%s) "
                    "ON DUPLICATE KEY UPDATE last_hash=VALUES(last_hash), seq=VALUES(seq)",
                    (shard, last, seq))
    return len(rows)


def main() -> int:
    perf = [r["emp_id"] for r in db.query(
        "SELECT emp_id FROM users WHERE username LIKE %s", ("perf%",))]
    gifts = [r["id"] for r in db.query(f"SELECT id FROM gifts WHERE {_LIKE}", _LIKE_ARGS)]
    demo = [r["id"] for r in db.query(f"SELECT id FROM gifts WHERE NOT ({_LIKE})", _LIKE_ARGS)]

    print("== 清理范围 ==")
    print(f"  perf 用户数    : {len(perf)}")
    print(f"  压测礼品 id    : {gifts}")
    print(f"  保留 demo 礼品 : {demo}")
    if not perf and not gifts:
        print("[=] 无压测遗留数据，无需清理。")
        return 0

    # 安全闸：待删礼品必须都是 cost<=1 的测试礼品，绝不能把高价 demo 礼品卷进来。
    if gifts:
        gph = _ph(gifts)
        bad = db.query(f"SELECT id, points_cost FROM gifts WHERE id IN {gph} AND points_cost > 1", gifts)
        if bad:
            print(f"[x] 中止：待删礼品含 cost>1 项 {bad}，请人工核对。")
            return 3

    with db._connect() as conn:
        with conn.cursor() as cur:
            print("\n== 待清理行数（前 / 后对照用） ==")
            before = scan_counts(cur, perf, gifts)
            for k, v in before.items():
                print(f"  {k:<16}: {v}")

            if not APPLY:
                conn.rollback()
                print("\n[dry-run] 未落删除。确认无误后：$env:APPLY=\"1\" 再跑一次。")
                return 0

            print("\n[APPLY] 执行删除 ...")
            for sql, args in build_deletes(perf, gifts):
                cur.execute(sql, args)
                print(f"  删 {cur.rowcount:>6} 行  <- {sql.split('WHERE')[0].strip()}")
            survived = rebuild_audit_chain(cur)
            conn.commit()
            print(f"\n审计链已对 {survived} 条存活行全量重算。")

            print("\n== 清理后核对 ==")
            after = scan_counts(cur, perf, gifts)
            for k, v in after.items():
                print(f"  {k:<16}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
