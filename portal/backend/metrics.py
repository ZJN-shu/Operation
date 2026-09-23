"""运营指标口径 —— 单一事实来源（Single Source of Truth）。

为什么单独抽一个文件：口径散在各个 router 里各写各的 SELECT，早晚会漂移出两套
算法（同一张看板，一处按次数算、一处按人数算），数字对不上还查不出原因。
所以口径全部收在这里，看板只允许调本模块的函数，不允许自己写聚合 SQL。

三条全局口径约定
----------------
1. 时间字段：统一用 user_access_logs.accessed_at（服务端落库时间）。
   客户端的 client_time 只用于诊断时钟偏移，绝不参与指标计算 ——
   否则用户把本机时间往前调就能刷指标。
2. 去重维度：人数类指标统一 COUNT(DISTINCT emp_id)，不按 session_id 去重。
   session_id 只用于会话内指标（访问深度、单会话漏斗）。
3. 空分母：一律返回 0.0，不返回 None / NaN —— 前端不做二次判空。
"""
from __future__ import annotations

from . import db

# 口径说明书：前端看板可直接取用做 tooltip，避免"数字有了但没人说得清怎么算的"
METRIC_DEFS: tuple[dict, ...] = (
    {
        "key": "dau", "name": "DAU",
        "definition": "当日产生过 page_visit 事件的去重工号数",
        "formula": "COUNT(DISTINCT emp_id) WHERE event_type='page_visit' AND DATE(accessed_at)=当日",
        "denominator": "—", "dedup": "emp_id", "window": "自然日",
        "decision": "观测活跃留存、活动拉新效果",
    },
    {
        "key": "wau", "name": "WAU",
        "definition": "最近 7 个自然日（含今日）产生过 page_visit 的去重工号数",
        "formula": "COUNT(DISTINCT emp_id) WHERE event_type='page_visit' AND accessed_at>=今日-6天",
        "denominator": "—", "dedup": "emp_id", "window": "7 日滚动",
        "decision": "区分「只是今天没来」和「已经流失」",
    },
    {
        "key": "mall_uv", "name": "商城浏览人数",
        "definition": "浏览过任一礼品详情页的去重工号数，是兑换转化率的分母",
        "formula": "COUNT(DISTINCT emp_id) WHERE event_type='gift_view'",
        "denominator": "—", "dedup": "emp_id", "window": "自然日 / 可选区间",
        "decision": "衡量商城曝光是否足够",
    },
    {
        "key": "redeem_users", "name": "兑换人数",
        "definition": "当日存在兑换记录的去重工号数（以 redemptions 业务表为准，不是埋点）",
        "formula": "COUNT(DISTINCT emp_id) FROM redemptions",
        "denominator": "—", "dedup": "emp_id", "window": "自然日 / 可选区间",
        "decision": "兑换漏斗的分子",
    },
    {
        "key": "redeem_conversion", "name": "兑换转化率",
        "definition": "兑换人数 ÷ 商城浏览人数",
        "formula": "redeem_users / mall_uv × 100%",
        "denominator": "商城浏览人数", "dedup": "分子分母都按 emp_id 去重", "window": "自然日 / 可选区间",
        "decision": "低转化=曝光够但兑换意愿低，指向定价/库存/品类问题",
    },
    {
        "key": "gift_click_to_redeem", "name": "礼品点击→兑换转化",
        "definition": "礼品维度：被点击次数 vs 被兑换次数，用来对比点击热度与兑换热度差",
        "formula": "redeem_count / view_count × 100%（按礼品）",
        "denominator": "该礼品 gift_view 次数", "dedup": "单礼品内按次数，不做人数去重",
        "window": "全周期",
        "decision": "点击高兑换低 → 定价或库存有问题",
    },
    {
        "key": "course_enroll_rate", "name": "课程报名率",
        "definition": "报名人数 ÷ 课程详情浏览人数",
        "formula": "course_enroll_users / course_view_users × 100%",
        "denominator": "课程详情浏览人数", "dedup": "emp_id", "window": "全周期",
        "decision": "衡量课程详情页的说服力",
    },
    {
        "key": "course_complete_rate", "name": "课程完成率",
        "definition": "完成人数 ÷ 报名人数",
        "formula": "course_complete_users / course_enroll_users × 100%",
        "denominator": "报名人数", "dedup": "emp_id", "window": "全周期",
        "decision": "衡量课程内容质量与激励力度",
    },
    {
        "key": "points_spend_rate", "name": "积分消耗率",
        "definition": "净消耗积分 ÷ 已发放积分；排除回滚及原笔，退款不算发放，已退款兑换不算消耗",
        "formula": "未回滚且未退款的消耗积分 / 排除退款与回滚后的发放积分 × 100%",
        "denominator": "已发放积分（不含回滚与被回滚的原笔）", "dedup": "按积分笔数求和，不涉及人数",
        "window": "全周期 / 近 7 天",
        "decision": "过低=积分发得出花不掉（礼品吸引力不足）；过高=积分贬值快、库存承压。"
                    "回滚是纠偏不是业务消耗：算进去会让回滚凭空抬高消耗率，"
                    "被回滚的原笔不排除则会让「收回的积分」仍留在发放总额里",
    },
    {
        "key": "shipping_avg_hours", "name": "平均发货时效",
        "definition": "已发货订单从下单到发货的平均小时数",
        "formula": "AVG(TIMESTAMPDIFF(HOUR, created_at, shipped_at))",
        "denominator": "已发货且 shipped_at 非空的订单数", "dedup": "按订单，不涉及人数",
        "window": "全周期",
        "decision": "履约体验的硬指标",
    },
)


def _scalar(sql: str, args: tuple = ()) -> int:
    row = db.query_one(sql, args)
    return int(list(row.values())[0]) if row else 0


def pct(numerator: float, denominator: float) -> float:
    """口径约定 3：空分母返回 0.0，不返回 None/NaN。"""
    return round(numerator / denominator * 100, 1) if denominator else 0.0


# ---------- 人数类指标（口径约定 2：一律按 emp_id 去重） ----------

def event_users(event_type: str, ref_type: str | None = None) -> int:
    """某个事件类型的去重人数。"""
    if ref_type:
        return _scalar(
            "SELECT COUNT(DISTINCT emp_id) FROM user_access_logs "
            "WHERE event_type = %s AND ref_type = %s", (event_type, ref_type))
    return _scalar(
        "SELECT COUNT(DISTINCT emp_id) FROM user_access_logs WHERE event_type = %s",
        (event_type,))


def event_count(event_type: str, ref_type: str | None = None) -> int:
    """某个事件类型的发生次数（不去重，用于热度类指标）。"""
    if ref_type:
        return _scalar(
            "SELECT COUNT(*) FROM user_access_logs "
            "WHERE event_type = %s AND ref_type = %s", (event_type, ref_type))
    return _scalar("SELECT COUNT(*) FROM user_access_logs WHERE event_type = %s", (event_type,))


def dau() -> int:
    """口径约定 1：按 accessed_at（服务端落库时间）切自然日。"""
    return _scalar(
        "SELECT COUNT(DISTINCT emp_id) FROM user_access_logs "
        "WHERE event_type = 'page_visit' AND accessed_at >= CURDATE()")


def wau() -> int:
    """7 日滚动窗口，用 accessed_at >= 今日-6天，不是 O(天数×7) 的循环。"""
    return _scalar(
        "SELECT COUNT(DISTINCT emp_id) FROM user_access_logs "
        "WHERE event_type = 'page_visit' "
        "AND accessed_at >= DATE_SUB(CURDATE(), INTERVAL 6 DAY)")


def distinct_users(table: str, where: str = "1=1", args: tuple = ()) -> int:
    """业务表的去重人数（redemptions / training_progress 这类以业务表为准的指标）。"""
    return _scalar(f"SELECT COUNT(DISTINCT emp_id) FROM {table} WHERE {where}", args)


# ---------- 比率类指标 ----------

def redeem_conversion() -> tuple[int, int, float]:
    """兑换转化率：返回 (兑换人数, 商城浏览人数, 转化率%)。"""
    redeem = distinct_users("redemptions")
    mall_uv = event_users("gift_view", "gift")
    return redeem, mall_uv, pct(redeem, mall_uv)


# 纠偏类流水：回滚（revert）是为了把账改对，不是业务上的发放/消耗。
# 把它算进口径会得到荒唐结论：回滚一笔「完成课程 +50」会插一条 -50，
# 于是「已消耗」+50、消耗率凭空翻倍。所以口径里排除掉。
# 用黑名单而不是白名单：白名单一旦漏掉某个既有的 ref_type（welcome/course/...），
# 看板上的历史数字会当场变掉；黑名单保证加这个功能前后数字一字不变。
# （校平不在此列 —— 它根本不写流水，只重建余额缓存。）
_CORRECTION_REF_TYPES = ("revert", "refund")

# 光排除回滚行还不够：被回滚掉的那笔原始发放还留在库里正数那侧，
# 于是「回滚一笔发放」之后，发放总额纹丝不动、消耗也纹丝不动 ——
# 账面上那笔钱像是从没被收回过。所以口径要连**原笔**一起排除，
# 让一笔回滚的效果是「这笔业务没发生过」，这才是业务方的预期。
#
# 为什么用 NOT EXISTS 而不是 NOT IN：NOT IN 遇到子查询里的 NULL 会让整个
# 比较变成 UNKNOWN，一行都匹配不上（发放额会静默变成 0）。这里显式过滤了
# NOT NULL，但 NOT EXISTS 从写法上就不会踩这个坑。
# 现有数据里没有 revert、point_ops 也是空的，子查询返回空集 ——
# 看板上的历史数字与加本功能之前一字不差。
_NOT_REVERTED = (
    " AND NOT EXISTS (SELECT 1 FROM point_ops po "
    "WHERE po.reverted_record_id = point_records.id)"
    " AND NOT (point_records.ref_type = 'redeem' AND EXISTS "
    "(SELECT 1 FROM point_records refund WHERE refund.ref_type = 'refund' "
    "AND refund.emp_id = point_records.emp_id AND refund.ref_id = point_records.ref_id))"
)


def points_issued() -> int:
    """已发放积分 = 正数流水之和，排除纠偏类与被回滚掉的原笔。"""
    return _scalar(
        "SELECT COALESCE(SUM(points), 0) FROM point_records "
        "WHERE points > 0 AND ref_type NOT IN %s" + _NOT_REVERTED,
        (_CORRECTION_REF_TYPES,))


def points_spent() -> int:
    """已消耗积分 = 负数流水绝对值之和，排除纠偏类与被回滚掉的原笔。"""
    return _scalar(
        "SELECT COALESCE(SUM(-points), 0) FROM point_records "
        "WHERE points < 0 AND ref_type NOT IN %s" + _NOT_REVERTED,
        (_CORRECTION_REF_TYPES,))


def points_spend_rate() -> tuple[int, int, float]:
    """积分消耗率：返回 (已消耗, 已发放, 消耗率%)。"""
    issued = points_issued()
    spent = points_spent()
    return spent, issued, pct(spent, issued)


def daily_points(days: int = 7) -> list[dict]:
    """近 N 个自然日（含今日）的每日发放/消耗，口径同上（排除纠偏类与被回滚原笔）。"""
    return db.query(
        "SELECT DATE(created_at) AS d, "
        "SUM(CASE WHEN points > 0 THEN points ELSE 0 END) AS issued, "
        "SUM(CASE WHEN points < 0 THEN -points ELSE 0 END) AS spent "
        "FROM point_records "
        "WHERE created_at >= DATE_SUB(CURDATE(), INTERVAL %s DAY) "
        "AND ref_type NOT IN %s" + _NOT_REVERTED + " "
        "GROUP BY DATE(created_at) ORDER BY d ASC",
        (days - 1, _CORRECTION_REF_TYPES),
    )
