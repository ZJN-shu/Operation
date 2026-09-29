"""轻量 MQ（演进 C）：Redis Streams + 消费者组，把非关键写移出同步兑换事务。

设计取向（承演进 B 的瓶颈定位）：redeem 单请求的墙钟里，审计哈希链追加与通知发件箱
登记是「旁路写」——它们不参与「绝不超卖 / 账实一致」的裁决。把它们从同步事务里摘出来、
提交后 XADD 入流、由消费者组异步落库，缩短关键路径时延。

三条边界：
  1. 只旁路「审计 / 通知 / 埋点」。扣桶、扣分、建订单、幂等重放读、response_json 更新
     全部留在 redeem 事务内——钱和货的一致性不动。
  2. 默认 sync（MQ_BACKEND != redis_streams）：本模块所有 publish 是空操作，
     调用方走原同步路径，测试与单实例行为完全不变、不碰 redis。
  3. 消费是「至少一次」+ 落库幂等：
       - 通知：notifier.enqueue 靠 event_key 唯一约束 ODKU，重投即空操作；
       - 埋点：log_event 靠 uk_event 唯一约束，重复上报 rowcount=0；
       - 审计：哈希链按锚点行 FOR UPDATE 串行追加，重复消费只会多一条自洽的链上行
         （链仍 ok:true），丢失则该行缺席（链不断、可验）——审计不进对账，可容忍。
     故不做去重表；换取实现极简与「消费者随 worker 数横向扩」。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Optional

from . import config, db, redis_client

logger = logging.getLogger("portal.mq")

_stop = threading.Event()
_thread: Optional[threading.Thread] = None


def enabled() -> bool:
    return config.MQ_BACKEND == "redis_streams"


def _encode(kind: str, fields: dict) -> dict:
    """Redis Stream 字段值必须是 str/number；dict/list 序列化成 JSON 串。"""
    body = {"kind": kind}
    for k, v in fields.items():
        if isinstance(v, (dict, list)):
            body[k] = json.dumps(v, ensure_ascii=False)
        elif v is None:
            body[k] = ""
        else:
            body[k] = str(v)
    return body


def publish(kind: str, **fields) -> None:
    """入流一条异步写请求。未启用时为空操作（调用方已自行走同步路径）。"""
    if not enabled():
        return
    redis_client.client().xadd(config.MQ_STREAM, _encode(kind, fields))


def publish_many(items) -> None:
    """一次 pipeline 批量入流：redeem 提交后把审计 + 通知（可能低库存）一并推出，
    只一趟往返，避免把省下来的事务时延又耗在多次 Redis RTT 上。"""
    if not enabled() or not items:
        return
    r = redis_client.client()
    pipe = r.pipeline()
    for kind, fields in items:
        pipe.xadd(config.MQ_STREAM, _encode(kind, fields))
    pipe.execute()


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# 一批多条共享一个提交：把审计/通知/埋点的提交次数从「每条一次」压回「每批一次」。
# 否则异步反而把 1 次提交放大成 3 次（演进 C 首轮实测因此回退），单 MySQL 被拖垮。
_BATCH = 256


def _shard_of(emp_id: str) -> int:
    return db.stable_slot(emp_id, config.AUDIT_CHAIN_SHARDS)


def _apply_batch(bodies: list) -> None:
    """把一个 XREADGROUP 批次里的多条事件写进**同一个事务/提交**。

    审计先按分链排序再上锁：多个消费者并发批量时，锚点行加锁顺序一致→避免自相死锁。
    任一失败整批回滚（run_tx 有界重试）：notify/event 靠唯一约束幂等，audit 重跑只会多
    一条自洽链上行——均不破一致性。"""
    from . import logic, notifier

    def fn(cur):
        for body in bodies:
            kind = body.get("kind")
            if kind == "audit":
                detail_raw = body.get("detail")
                logic.log_audit(body["emp_id"], body["action"], body.get("target_type", ""),
                                _int(body.get("target_id")),
                                json.loads(detail_raw) if detail_raw else None, cur=cur)
            elif kind == "notify":
                notifier.enqueue(cur, event_key=body["event_key"], audience=body["audience"],
                                 ntype=body.get("ntype", "system"),
                                 ref_type=body.get("ref_type", ""), ref_id=_int(body.get("ref_id")),
                                 title=body.get("title", ""), content=body.get("content", ""))
            elif kind == "event":
                props_raw = body.get("properties")
                logic.log_event(body["emp_id"], body["event_type"], body.get("ref_type", ""),
                                _int(body.get("ref_id")),
                                json.loads(props_raw) if props_raw else None,
                                session_id=body.get("session_id", ""),
                                event_id=body.get("event_id") or None,
                                client_time=body.get("client_time") or None, cur=cur)
            else:
                logger.warning("未知异步写类型，已丢弃 kind=%s", kind)

    # 审计按分链排序（稳定）：同一批内锁锚点顺序确定，降低跨消费者死锁。
    bodies.sort(key=lambda b: (b.get("kind") != "audit",
                               _shard_of(b["emp_id"]) if b.get("kind") == "audit" else 0))
    db.run_tx(fn, retries=3)


def _consume() -> None:
    r = redis_client.client()
    key, grp = config.MQ_STREAM, config.MQ_GROUP
    # 消费组幂等建组：BUSYGROUP=已存在。id="$" 只消费建组之后的新消息（历史不外溢重放）。
    try:
        r.xgroup_create(key, grp, id="$", mkstream=True)
    except Exception as exc:  # noqa: BLE001
        if "BUSYGROUP" not in str(exc):
            logger.exception("消费组建组失败")
    name = f"c-{os.getpid()}"
    logger.info("异步写消费者启动 stream=%s group=%s consumer=%s", key, grp, name)
    while not _stop.is_set():
        try:
            resp = r.xreadgroup(grp, name, {key: ">"}, count=_BATCH, block=config.MQ_BLOCK_MS)
        except Exception:  # noqa: BLE001
            logger.exception("xreadgroup 失败，退避后重试")
            _stop.wait(1)
            continue
        if not resp:
            continue
        msgs = [m for _s, ms in resp for m in ms]
        if not msgs:
            continue
        bodies = [b for _mid, b in msgs]
        try:
            _apply_batch(bodies)          # 整批一个提交
        except Exception:  # noqa: BLE001
            # 整批未落：不 ACK，留在 PEL 靠幂等重投兜。绝不因一批失败停线程。
            logger.exception("异步写批量落库失败，保留 pending 条数=%d", len(msgs))
            continue
        r.xack(key, grp, *[mid for mid, _b in msgs])


def start_consumer() -> None:
    """启动消费者线程（阻塞式 pymysql，用线程不用 asyncio task）。幂等。"""
    global _thread
    if not enabled():
        return
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_consume, name="mq-consumer", daemon=True)
    _thread.start()


def stop_consumer() -> None:
    _stop.set()
    if _thread and _thread.is_alive():
        _thread.join(timeout=2)


def drain(max_msgs: int = 100000) -> int:
    """把当前流里所有待处理消息同步消费掉（压测核对/一次性脚本用），返回处理条数。
    用与后台消费者同一 group 的一个临时 consumer 名，读完即止。"""
    if not enabled():
        return 0
    r = redis_client.client()
    key, grp = config.MQ_STREAM, config.MQ_GROUP
    try:
        r.xgroup_create(key, grp, id="0", mkstream=True)
    except Exception:  # noqa: BLE001
        pass
    name = f"drain-{os.getpid()}"
    done = 0
    while done < max_msgs:
        resp = r.xreadgroup(grp, name, {key: ">"}, count=_BATCH, block=200)
        if not resp:
            break
        msgs = [m for _s, ms in resp for m in ms]
        if not msgs:
            break
        _apply_batch([b for _mid, b in msgs])
        r.xack(key, grp, *[mid for mid, _b in msgs])
        done += len(msgs)
    return done
