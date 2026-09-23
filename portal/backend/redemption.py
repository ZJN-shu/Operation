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

from . import db, logic

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


def redeem(gid: int, payload: RequestIn, emp_id: str):
    def fn(cur):
        balance = _account(cur, emp_id)
        # 同用户同请求串行化后先读旧结果，库存、价格和上架状态变化不影响重放。
        cur.execute("SELECT gift_id, response_json FROM redemptions "
                    "WHERE emp_id = %s AND request_id = %s", (emp_id, payload.request_id))
        existing = cur.fetchone()
        if existing:
            if existing["gift_id"] != gid:
                raise HTTPException(409, "同一请求键不能用于不同礼品")
            return json.loads(existing["response_json"])
        gift = _gift(cur, gid)
        if gift["status"] != "active":
            return {"ok": False, "reason": "not_found"}
        if gift["stock"] <= 0:
            return {"ok": False, "reason": "out_of_stock"}
        cost = gift["points_cost"]
        if balance < cost:
            return {"ok": False, "reason": "insufficient_points"}
        stock_baseline(cur, gift, emp_id)
        cur.execute("UPDATE gifts SET stock = stock - 1 WHERE id = %s AND stock > 0", (gid,))
        if cur.rowcount != 1:
            raise HTTPException(409, "库存已变化，请重试")
        cur.execute("INSERT INTO redemptions (gift_id, emp_id, points_cost, request_id) "
                    "VALUES (%s,%s,%s,%s)", (gid, emp_id, cost, payload.request_id))
        oid = cur.lastrowid
        logic.add_points(cur, emp_id, -cost, "兑换礼品", "redeem", oid)
        _stock_record(cur, gid, -1, gift["stock"] - 1, "redeem", oid, emp_id)
        logic.log_audit(emp_id, "redeem_order", "order", oid,
                        {"gift_id": gid, "points": cost, "request_id": payload.request_id}, cur=cur)
        logic.notify(emp_id, "兑换成功", f"你已兑换「{gift['name']}」，消耗 {cost} 积分，等待发货",
                     "redeem", oid, cur=cur)
        if gift["stock"] > logic.LOW_STOCK_THRESHOLD >= gift["stock"] - 1:
            logic.notify_admins("库存告警", f"「{gift['name']}」库存仅剩 {gift['stock'] - 1} 件，请及时补货",
                                "low_stock", gid, cur=cur)
        result = {"ok": True, "redemption_id": oid, "gift_name": gift["name"],
                  "points_cost": cost, "old_stock": gift["stock"]}
        cur.execute("UPDATE redemptions SET response_json = %s WHERE id = %s",
                    (json.dumps(result, ensure_ascii=False), oid))
        return result
    return db.run_tx(fn, retries=TX_RETRIES)


def adjust_stock(gid: int, payload: StockIn, operator: str):
    if payload.delta == 0:
        raise HTTPException(400, "库存变动不能为零")

    def replay(row):
        if (row["gift_id"], row["delta"], row["reason"]) != (gid, payload.delta, payload.reason):
            raise HTTPException(409, "同一库存请求键不能修改参数")
        return {"ok": True, "record_id": row["id"], "stock": row["stock_after"]}

    def fn(cur):
        gift = _gift(cur, gid)
        cur.execute("SELECT * FROM gift_stock_records WHERE operator_emp_id = %s "
                    "AND request_id = %s", (operator, payload.request_id))
        row = cur.fetchone()
        if row:
            return replay(row)
        after = gift["stock"] + payload.delta
        if not 0 <= after <= MAX_STOCK:
            raise HTTPException(400, "调整后的库存超出合法范围")
        stock_baseline(cur, gift, operator)
        cur.execute("UPDATE gifts SET stock = stock + %s WHERE id = %s", (payload.delta, gid))
        rid = _stock_record(cur, gid, payload.delta, after, "adjust", None, operator,
                            payload.request_id, payload.reason)
        logic.log_audit(operator, "adjust_stock", "gift", gid,
                        {"delta": payload.delta, "before": gift["stock"], "after": after,
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
        logic.notify(order["emp_id"], "订单已发货", f"「{gift['name']}」已发货，物流单号：{express}",
                     "ship", oid, cur=cur)
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
        if gift["stock"] >= MAX_STOCK:
            raise HTTPException(409, "库存达到上限，需先核查")
        stock_baseline(cur, gift, actor)
        cur.execute("UPDATE redemptions SET status = %s, refunded_at = NOW(), "
                    "refund_reason = %s, refund_operator = %s WHERE id = %s AND status = %s",
                    (target, reason, actor, oid, source))
        logic.add_points(cur, order["emp_id"], order["points_cost"], "订单取消退款" if not returned else "退货退款",
                         "refund", oid)
        cur.execute("UPDATE gifts SET stock = stock + 1 WHERE id = %s", (gift["id"],))
        _stock_record(cur, gift["id"], 1, gift["stock"] + 1, "refund", oid, actor, reason=reason)
        logic.log_audit(actor, "refund_order" if returned else "cancel_order", "order", oid,
                        {"reason": reason, "points": order["points_cost"], "returned": returned}, cur=cur)
        logic.notify(order["emp_id"], "订单已退款", f"订单 #{oid} 已返还 {order['points_cost']} 积分",
                     "refund", oid, cur=cur)
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
            "SELECT g.id AS gift_id,g.stock,COALESCE(s.total,0) AS ledger_stock,s.baselines "
            "FROM gifts g LEFT JOIN (SELECT gift_id,SUM(delta) total, "
            "SUM(kind='baseline') baselines FROM gift_stock_records GROUP BY gift_id) s ON s.gift_id=g.id "
            "WHERE s.gift_id IS NULL OR s.baselines<>1 OR g.stock<>s.total")
        stocks = cur.fetchall()
        cur.execute(
            "SELECT p.id,p.ref_type,p.ref_id FROM point_records p LEFT JOIN redemptions r ON r.id=p.ref_id "
            "WHERE p.ref_type IN ('redeem','refund') AND r.id IS NULL")
        orphans = cur.fetchall()
        return {"accounts": accounts, "orders": orders, "stocks": stocks, "orphan_records": orphans,
                "legacy_orders": legacy, "ok": not (accounts or orders or stocks or orphans)}
    return db.run_tx(fn, read_snapshot=True)
