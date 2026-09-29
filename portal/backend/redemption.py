"""兑换领域：账户 → 礼品 → 订单的统一加锁顺序，所有必要写入使用同一事务。

请求键由客户端为一次意图生成并持久化，重试复用；新兑换使用新键。
取消仅允许 pending；退货退款仅允许 shipped，且管理员须确认退货入库。
"""
from __future__ import annotations

import json
from typing import Literal

import pymysql
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import config, db, logic, mq, notifier

MAX_STOCK = 1_000_000_000
MAX_POINTS = 1_000_000

# 兑换/库存写事务的有界死锁重试次数。InnoDB 死锁时会回滚整个事务（1213），
# 而本模块每个事务体开头的幂等重放读 + 数据库唯一约束保证“重跑不会二次生效”，
# 所以撞锁后整段重跑是安全的；超过上限则当作真实故障抛出。
TX_RETRIES = 3


class RequestIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: str = Field(..., min_length=8, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class StockIn(RequestIn):
    delta: int = Field(..., strict=True, ge=-MAX_STOCK, le=MAX_STOCK)
    reason: str = Field(..., min_length=1, max_length=100)


class CancelIn(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    reason: str = Field(..., min_length=1, max_length=100)


class RefundIn(CancelIn):
    returned: Literal[True]

    @field_validator("returned", mode="before")
    @classmethod
    def confirm_return(cls, value):
        if value is not True:
            raise ValueError("必须明确确认退货已入库")
        return value


def _account(cur, emp_id):
    # 重复键空更新也取得排他锁，避免 INSERT IGNORE 的共享锁升级死锁。
    cur.execute("INSERT INTO point_accounts (emp_id, balance) VALUES (%s, 0) "
                "ON DUPLICATE KEY UPDATE balance = balance", (emp_id,))
    cur.execute("SELECT balance FROM point_accounts WHERE emp_id = %s FOR UPDATE", (emp_id,))
    return cur.fetchone()["balance"]


def _gift(cur, gid):
    cur.execute("SELECT * FROM gifts WHERE id = %s FOR UPDATE", (gid,))
    gift = cur.fetchone()
    if not gift:
        raise HTTPException(404, "礼品不存在")
    if not 0 <= gift["stock"] <= MAX_STOCK or not 0 <= gift["points_cost"] <= MAX_POINTS:
        raise HTTPException(409, "礼品历史价格或库存非法，请先核查")
    return gift


# ---------- 库存分桶（写路径去串行化） ----------
# gifts.stock 退为展示缓存：首触碰时按它快照分桶，之后不再写它；库存真相 = SUM(桶)。
# 读侧（展示/对账/预检）一律走桶总和，保证与流水账实一致。


def _stock_total(cur, gid) -> int:
    """当前库存真相 = 各桶存量之和（事务内一致性读，能看到本事务自己的未提交修改）。

    SUM() 回的是 Decimal，必须 int() 回正：否则 old_stock 会随 result 进 json.dumps 直接报错。
    """
    cur.execute("SELECT COALESCE(SUM(stock), 0) AS t FROM gift_stock_bucket WHERE gift_id = %s", (gid,))
    return int(cur.fetchone()["t"])


def ensure_buckets(cur, gid) -> None:
    """若该礼品尚无桶行，按 gifts.stock 快照初始化 K 个桶（幂等，并发不翻倍）。"""
    cur.execute("SELECT 1 FROM gift_stock_bucket WHERE gift_id = %s LIMIT 1", (gid,))
    if cur.fetchone():
        return
    cur.execute("SELECT stock FROM gifts WHERE id = %s", (gid,))
    row = cur.fetchone()
    db.seed_gift_buckets(cur, gid, row["stock"] if row else 0)


def _decrement_bucket(cur, gid, home, k) -> int | None:
    """从 home 桶起环形扫描，对首个非空桶原子扣 1；成功返回桶号，全空返回 None。

    健康库存下 home 桶非空，一枪命中 → 本事务只锁一个桶行（这是去串行的关键）。
    home 空时轮转后续桶：并发轮转可能偶发 1213 死锁，交由 run_tx 有界重试吞掉
    （重放读 + 唯一约束保证重跑不二次生效）；`WHERE stock>0` 保证绝不把桶扣成负、绝不超卖。
    """
    for off in range(k):
        b = (home + off) % k
        cur.execute("UPDATE gift_stock_bucket SET stock = stock - 1 "
                    "WHERE gift_id = %s AND bucket_no = %s AND stock > 0", (gid, b))
        if cur.rowcount == 1:
            return b
    return None


def _adjust_buckets(cur, gid, delta) -> None:
    """管理员改库存：增则全进 0 号桶（确定性、无超卖风险），减则按 bucket_no 升序逐桶扣到够。

    升序锁定使多个 adjust 之间不成环；与兑换轮转偶发死锁仍由 run_tx 有界重试兜底（冷路径）。
    """
    ensure_buckets(cur, gid)
    if delta >= 0:
        cur.execute("UPDATE gift_stock_bucket SET stock = stock + %s "
                    "WHERE gift_id = %s AND bucket_no = 0", (delta, gid))
        return
    need = -delta
    for b in range(config.STOCK_BUCKETS):
        if need == 0:
            break
        cur.execute("SELECT stock FROM gift_stock_bucket WHERE gift_id = %s AND bucket_no = %s FOR UPDATE",
                    (gid, b))
        row = cur.fetchone()
        have = row["stock"] if row else 0
        take = need if need < have else have
        if take > 0:
            cur.execute("UPDATE gift_stock_bucket SET stock = stock - %s "
                        "WHERE gift_id = %s AND bucket_no = %s", (take, gid, b))
            need -= take
    if need:
        raise HTTPException(400, "库存不足，无法完成调整")


def stock_baseline(cur, gift, operator):
    """只在持有礼品行锁时建立基线；接入前的历史不冒充完整流水。"""
    # 唯一键空更新不改旧基线，也不依赖等待礼品锁之前可能已经建立的读快照。
    cur.execute(
        "INSERT INTO gift_stock_records "
        "(gift_id, delta, stock_after, kind, ref_id, operator_emp_id, reason) "
        "VALUES (%s,%s,%s,'baseline',0,%s,'库存流水接入基线') "
        "ON DUPLICATE KEY UPDATE id = id",
        (gift["id"], gift["stock"], gift["stock"], operator))


def _stock_record(cur, gid, delta, after, kind, ref_id, operator, request_id=None, reason=""):
    cur.execute(
        "INSERT INTO gift_stock_records "
        "(gift_id, delta, stock_after, kind, ref_id, operator_emp_id, request_id, reason) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (gid, delta, after, kind, ref_id, operator, request_id, reason))
    return cur.lastrowid


def redeem(gid: int, payload: RequestIn, emp_id: str, session_id: str = ""):
    """兑换。session_id 记下单时的会话，供「浏览→兑换」会话漏斗归因。

    默认空串：老调用方（含测试）拿不到会话时写入空串，口径里一律排除，
    不会把历史订单误并进同一个会话。
    """
    # 售罄/下架无锁预检：库存不足时，绝大多数请求应在碰到热点礼品行锁之前就被挡回。
    # 否则每个失败请求仍会走 _gift 的 SELECT ... FOR UPDATE 去抢那把全局热点行锁，
    # 售罄后把整条写路径堆成串行队（这是大厂秒杀「99% 请求挡在库外」的最小落地版）。
    # 关键：预检合并成**单次查询**（库存/状态 + 用 EXISTS 子查询一并判幂等重放），
    # 全程只 checkout 一次连接、一次往返——否则拒绝风暴下多次短查询会反过来加重连接池压力。
    # 它只是不加锁的一致性读，权威判定仍在事务内，故不改变任何正确性边界；
    # 重放必须返回原结果，不能被预检误拦。
    pre = db.query_one(
        "SELECT g.status, "
        "COALESCE((SELECT SUM(b.stock) FROM gift_stock_bucket b WHERE b.gift_id = g.id), g.stock) AS stock, "
        "EXISTS(SELECT 1 FROM redemptions r "
        "       WHERE r.emp_id = %s AND r.request_id = %s) AS is_replay "
        "FROM gifts g WHERE g.id = %s",
        (emp_id, payload.request_id, gid))
    if pre is None or pre["status"] != "active" or pre["stock"] <= 0:
        if not pre or not pre["is_replay"]:
            if pre is None or pre["status"] != "active":
                return {"ok": False, "reason": "not_found"}
            return {"ok": False, "reason": "out_of_stock"}
        # 命中重放：落到事务内的权威重放分支返回原结果，不在此处臆断。

    # 演进 C：非关键写（审计哈希链 / 通知发件箱）可在启用 MQ 时移出同步事务。
    # 在 fn 内只收集到 deferred，run_tx 成功返回（=已提交）后一次性 XADD 入流；
    # 回滚或重放分支不追写 deferred → 不会为失败的兑换发通知/留审计。禁用时走原同步写。
    deferred: list = []

    def fn(cur):
        deferred.clear()  # 1213 死锁重跑时清空上一次尝试收集的事件，避免重复入流
        balance = _account(cur, emp_id)
        # 同用户同请求串行化后先读旧结果，库存、价格和上架状态变化不影响重放。
        cur.execute("SELECT gift_id, response_json FROM redemptions "
                    "WHERE emp_id = %s AND request_id = %s", (emp_id, payload.request_id))
        existing = cur.fetchone()
        if existing:
            if existing["gift_id"] != gid:
                raise HTTPException(409, "同一请求键不能用于不同礼品")
            return json.loads(existing["response_json"])
        # 不加锁读礼品元信息：兑换热路径不再 SELECT ... FOR UPDATE 锁 gifts 行，
        # 否则同礼品所有请求仍在那一行上排串行队，分桶就白拆了。
        cur.execute("SELECT id, name, status, points_cost, stock FROM gifts WHERE id = %s", (gid,))
        gift = cur.fetchone()
        if not gift:
            raise HTTPException(404, "礼品不存在")
        if gift["status"] != "active":
            return {"ok": False, "reason": "not_found"}
        cost = gift["points_cost"]
        if not 0 <= cost <= MAX_POINTS:
            raise HTTPException(409, "礼品历史价格或库存非法，请先核查")
        ensure_buckets(cur, gid)
        total_before = _stock_total(cur, gid)
        if total_before <= 0:
            return {"ok": False, "reason": "out_of_stock"}
        if balance < cost:
            return {"ok": False, "reason": "insufficient_points"}
        stock_baseline(cur, gift, emp_id)
        home = db.stable_slot(emp_id, config.STOCK_BUCKETS)
        if _decrement_bucket(cur, gid, home, config.STOCK_BUCKETS) is None:
            # 预检/事务内读到有货，但进事务后各桶已被并发抢空：无桶可扣 → 判售罄。
            # 不抛 409（那会作为异常冒到调用方）；与事务内 stock<=0 分支一致返回 out_of_stock。
            return {"ok": False, "reason": "out_of_stock"}
        cur.execute("INSERT INTO redemptions (gift_id, emp_id, session_id, points_cost, request_id) "
                    "VALUES (%s,%s,%s,%s,%s)", (gid, emp_id, session_id, cost, payload.request_id))
        oid = cur.lastrowid
        logic.add_points(cur, emp_id, -cost, "兑换礼品", "redeem", oid)
        _stock_record(cur, gid, -1, total_before - 1, "redeem", oid, emp_id)
        # —— 审计与通知：启用 MQ 则出流异步落，否则维持原同步写（随事务回滚）——
        audit_fields = {"emp_id": emp_id, "action": "redeem_order", "target_type": "order",
                        "target_id": oid, "detail": {"gift_id": gid, "points": cost,
                                                     "request_id": payload.request_id}}
        if mq.enabled():
            deferred.append(("audit", audit_fields))
        else:
            logic.log_audit(emp_id, "redeem_order", "order", oid,
                            {"gift_id": gid, "points": cost, "request_id": payload.request_id}, cur=cur)
        # 通知只登记到发件箱，真正投递在事务提交之后：投递失败不该回滚这笔兑换，
        # 外部渠道（邮件/短信）更不能拖着礼品行锁不放。事件键带上订单号，
        # 重放与 1213 重试都只有一条入队。
        notify_fields = {"event_key": f"redeem:{oid}", "audience": notifier.user_audience(emp_id),
                         "ntype": "redeem", "ref_type": "order", "ref_id": oid,
                         "title": "兑换成功",
                         "content": f"你已兑换「{gift['name']}」，消耗 {cost} 积分，等待发货"}
        if mq.enabled():
            deferred.append(("notify", notify_fields))
        else:
            notifier.enqueue(cur, event_key=notify_fields["event_key"], audience=notify_fields["audience"],
                             ntype="redeem", ref_type="order", ref_id=oid,
                             title=notify_fields["title"], content=notify_fields["content"])
        if total_before > logic.LOW_STOCK_THRESHOLD >= total_before - 1:
            # 受众写成角色，投递时才展开成具体管理员：事务里不必 SELECT users，
            # 也不必为每个管理员各插一行。事件键用订单号锚定「这一次跨阈值」，
            # 补货后再跌到同一水位仍能再告警一次。
            low_fields = {"event_key": f"low_stock:{gid}:{oid}",
                          "audience": notifier.role_audience(*notifier.SHOP_ADMIN_ROLES),
                          "ntype": "low_stock", "ref_type": "gift", "ref_id": gid,
                          "title": "库存告警",
                          "content": f"「{gift['name']}」库存仅剩 {total_before - 1} 件，请及时补货"}
            if mq.enabled():
                deferred.append(("notify", low_fields))
            else:
                notifier.enqueue(cur, event_key=low_fields["event_key"], audience=low_fields["audience"],
                                 ntype="low_stock", ref_type="gift", ref_id=gid,
                                 title=low_fields["title"], content=low_fields["content"])
        result = {"ok": True, "redemption_id": oid, "gift_name": gift["name"],
                  "points_cost": cost, "old_stock": total_before}
        cur.execute("UPDATE redemptions SET response_json = %s WHERE id = %s",
                    (json.dumps(result, ensure_ascii=False), oid))
        return result
    result = db.run_tx(fn, retries=TX_RETRIES)
    # 提交成功后才把旁路写推出（回滚不会走到这里）；未启用 MQ 时 deferred 为空。
    if deferred:
        mq.publish_many(deferred)
    return result


def adjust_stock(gid: int, payload: StockIn, operator: str):
    if payload.delta == 0:
        raise HTTPException(400, "库存变动不能为零")

    def replay(row):
        if (row["gift_id"], row["delta"], row["reason"]) != (gid, payload.delta, payload.reason):
            raise HTTPException(409, "同一库存请求键不能修改参数")
        return {"ok": True, "record_id": row["id"], "stock": row["stock_after"]}

    def fn(cur):
        cur.execute("SELECT id, stock FROM gifts WHERE id = %s", (gid,))
        gift = cur.fetchone()
        if not gift:
            raise HTTPException(404, "礼品不存在")
        cur.execute("SELECT * FROM gift_stock_records WHERE operator_emp_id = %s "
                    "AND request_id = %s", (operator, payload.request_id))
        row = cur.fetchone()
        if row:
            return replay(row)
        ensure_buckets(cur, gid)
        before = _stock_total(cur, gid)
        after = before + payload.delta
        if not 0 <= after <= MAX_STOCK:
            raise HTTPException(400, "调整后的库存超出合法范围")
        stock_baseline(cur, gift, operator)
        _adjust_buckets(cur, gid, payload.delta)
        rid = _stock_record(cur, gid, payload.delta, after, "adjust", None, operator,
                            payload.request_id, payload.reason)
        logic.log_audit(operator, "adjust_stock", "gift", gid,
                        {"delta": payload.delta, "before": before, "after": after,
                         "reason": payload.reason, "request_id": payload.request_id}, cur=cur)
        return {"ok": True, "record_id": rid, "stock": after}
    try:
        return db.run_tx(fn, retries=TX_RETRIES)
    except pymysql.err.IntegrityError as exc:
        if not logic.is_duplicate(exc):
            raise
        # 不同礼品使用同一请求键时由数据库唯一约束仲裁。
        row = db.query_one("SELECT * FROM gift_stock_records WHERE operator_emp_id = %s "
                           "AND request_id = %s", (operator, payload.request_id))
        if row:
            return replay(row)
        raise


def _order_tx(oid, actor, callback, owner_only=False):
    initial = db.query_one("SELECT emp_id, gift_id FROM redemptions WHERE id = %s", (oid,))
    if not initial or (owner_only and initial["emp_id"] != actor):
        raise HTTPException(404, "订单不存在")

    def fn(cur):
        _account(cur, initial["emp_id"])
        gift = _gift(cur, initial["gift_id"])
        cur.execute("SELECT * FROM redemptions WHERE id = %s FOR UPDATE", (oid,))
        order = cur.fetchone()
        if not order or (order["emp_id"], order["gift_id"]) != (initial["emp_id"], initial["gift_id"]):
            raise HTTPException(409, "订单已变化")
        return callback(cur, order, gift)
    return db.run_tx(fn, retries=TX_RETRIES)


def ship(oid: int, express: str, actor: str):
    def fn(cur, order, gift):
        if order["status"] == "shipped" and order["express"] == express:
            return {"ok": True}
        if order["status"] != "pending":
            raise HTTPException(409, "只有待发货订单可以发货")
        cur.execute("UPDATE redemptions SET status = 'shipped', express = %s, shipped_at = NOW() "
                    "WHERE id = %s AND status = 'pending'", (express, oid))
        logic.log_audit(actor, "ship_order", "order", oid, {"express": express}, cur=cur)
        notifier.enqueue(cur, event_key=f"ship:{oid}",
                         audience=notifier.user_audience(order["emp_id"]),
                         ntype="ship", ref_type="order", ref_id=oid,
                         title="订单已发货",
                         content=f"「{gift['name']}」已发货，物流单号：{express}")
        return {"ok": True}
    return _order_tx(oid, actor, fn)


def refund(oid: int, reason: str, actor: str, *, returned=False, owner_only=False):
    target = "refunded" if returned else "cancelled"
    source = "shipped" if returned else "pending"

    def fn(cur, order, gift):
        result = {"ok": True, "redemption_id": oid, "status": target,
                  "points_refunded": order["points_cost"]}
        if order["status"] == target:
            return result
        if order["status"] != source:
            raise HTTPException(409, "订单状态不允许此操作")
        # 旧订单若曾被通用冲正入口返分，不再二次退款，交给对账人工核查。
        cur.execute("SELECT pr.id, pr.points, po.id AS reverted FROM point_records pr "
                    "LEFT JOIN point_ops po ON po.reverted_record_id = pr.id "
                    "WHERE pr.emp_id = %s AND pr.ref_type = 'redeem' AND pr.ref_id = %s",
                    (order["emp_id"], oid))
        debit = cur.fetchone()
        if not debit or debit["points"] != -order["points_cost"] or debit["reverted"] is not None:
            raise HTTPException(409, "订单扣分流水异常，需人工核查")
        ensure_buckets(cur, gift["id"])
        before = _stock_total(cur, gift["id"])
        if before >= MAX_STOCK:
            raise HTTPException(409, "库存达到上限，需先核查")
        stock_baseline(cur, gift, actor)
        cur.execute("UPDATE redemptions SET status = %s, refunded_at = NOW(), "
                    "refund_reason = %s, refund_operator = %s WHERE id = %s AND status = %s",
                    (target, reason, actor, oid, source))
        logic.add_points(cur, order["emp_id"], order["points_cost"], "订单取消退款" if not returned else "退货退款",
                         "refund", oid)
        _adjust_buckets(cur, gift["id"], 1)
        _stock_record(cur, gift["id"], 1, before + 1, "refund", oid, actor, reason=reason)
        logic.log_audit(actor, "refund_order" if returned else "cancel_order", "order", oid,
                        {"reason": reason, "points": order["points_cost"], "returned": returned}, cur=cur)
        notifier.enqueue(cur, event_key=f"refund:{oid}",
                         audience=notifier.user_audience(order["emp_id"]),
                         ntype="refund", ref_type="order", ref_id=oid,
                         title="订单已退款",
                         content=f"订单 #{oid} 已返还 {order['points_cost']} 积分")
        return result
    return _order_tx(oid, actor, fn, owner_only)


def reconciliation():
    """同一快照核对账、单、库存；只报差异，不自动制造补偿流水。"""
    def fn(cur):
        cur.execute(
            "SELECT e.emp_id, pa.balance, COALESCE(p.total,0) AS records_sum "
            "FROM (SELECT emp_id FROM point_accounts UNION SELECT emp_id FROM point_records) e "
            "LEFT JOIN point_accounts pa ON pa.emp_id=e.emp_id "
            "LEFT JOIN (SELECT emp_id,SUM(points) total FROM point_records GROUP BY emp_id) p ON p.emp_id=e.emp_id "
            "WHERE pa.emp_id IS NULL OR pa.balance <> COALESCE(p.total,0)")
        accounts = cur.fetchall()
        cur.execute(
            "SELECT r.id, r.emp_id, r.gift_id, r.points_cost, r.status, r.integrity_version, "
            "(SELECT COUNT(*) FROM point_records p WHERE p.ref_type='redeem' AND p.ref_id=r.id) AS debit_count, "
            "(SELECT COUNT(*) FROM point_records p WHERE p.ref_type='redeem' AND p.ref_id=r.id "
            "AND p.emp_id=r.emp_id AND p.points=-r.points_cost) AS debit_valid, "
            "(SELECT COUNT(*) FROM point_ops po JOIN point_records p ON p.id=po.reverted_record_id "
            "WHERE p.ref_type IN ('redeem','refund') AND p.ref_id=r.id) AS reverted, "
            "(SELECT COUNT(*) FROM point_records p WHERE p.ref_type='refund' AND p.ref_id=r.id) AS refund_count, "
            "(SELECT COUNT(*) FROM point_records p WHERE p.ref_type='refund' AND p.ref_id=r.id "
            "AND p.emp_id=r.emp_id AND p.points=r.points_cost) AS refund_valid, "
            "(SELECT COUNT(*) FROM gift_stock_records s WHERE s.kind='redeem' AND s.ref_id=r.id "
            "AND s.gift_id=r.gift_id AND s.delta=-1) AS stock_out, "
            "(SELECT COUNT(*) FROM gift_stock_records s WHERE s.kind='refund' AND s.ref_id=r.id "
            "AND s.gift_id=r.gift_id AND s.delta=1) AS stock_in "
            "FROM redemptions r")
        orders, legacy = [], 0
        for row in cur.fetchall():
            closed = row["status"] in ("cancelled", "refunded")
            legacy += row["integrity_version"] == 0
            valid = (row["status"] in ("pending", "shipped", "cancelled", "refunded")
                     and row["debit_count"] == row["debit_valid"] == 1 and row["reverted"] == 0
                     and row["refund_count"] == row["refund_valid"] == int(closed)
                     and row["stock_in"] == int(closed)
                     and (row["integrity_version"] == 0 or row["stock_out"] == 1))
            if not valid:
                orders.append(row)
        cur.execute(
            "SELECT g.id AS gift_id,COALESCE(b.total,0) AS stock,COALESCE(s.total,0) AS ledger_stock,s.baselines "
            "FROM gifts g "
            "LEFT JOIN (SELECT gift_id,SUM(stock) total FROM gift_stock_bucket GROUP BY gift_id) b ON b.gift_id=g.id "
            "LEFT JOIN (SELECT gift_id,SUM(delta) total, "
            "SUM(kind='baseline') baselines FROM gift_stock_records GROUP BY gift_id) s ON s.gift_id=g.id "
            "WHERE s.gift_id IS NULL OR s.baselines<>1 OR COALESCE(b.total,0)<>s.total")
        stocks = cur.fetchall()
        cur.execute(
            "SELECT p.id,p.ref_type,p.ref_id FROM point_records p LEFT JOIN redemptions r ON r.id=p.ref_id "
            "WHERE p.ref_type IN ('redeem','refund') AND r.id IS NULL")
        orphans = cur.fetchall()
        return {"accounts": accounts, "orders": orders, "stocks": stocks, "orphan_records": orphans,
                "legacy_orders": legacy, "ok": not (accounts or orders or stocks or orphans)}
    return db.run_tx(fn, read_snapshot=True)
