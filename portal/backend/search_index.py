"""站内搜索：倒排索引 + 单字分词 + 多词交集召回 + 字段加权排序。

为什么不用 `LIKE '%关键词%'`：
  ① 前置通配符走不了索引，数据一多就是全表扫；
  ② 用户输入里的 % / _ 是 LIKE 的通配符，不转义就是注入面，转义了又是个易漏的活。

索引表 search_keywords 是最简倒排：一行一个「关键词 → 文档」，
行上带该关键词命中的最高字段权重（标题 3 > 分类 2 > 描述 1）。

分词规则（tokenize）——建索引和查询必须用同一套，这点是硬的：
  1. 英文/数字连续串整体成一个 token：`vue3` → `vue3`
  2. 该串再按「字母段 / 数字段」边界拆一层：`vue3` → `vue`, `3`
     于是搜 `vue`、搜 `3`、搜 `vue3` 都能命中。老版本的问题正是
     「索引里存的是 vue + 3，查询时是 vue3」，两边对不上就搜不到；
     统一分词函数之后这个坑从根上没了。
  3. 中文按单字拆：`马克杯` → `马`, `克`, `杯`
     没有中文分词器和词典，单字拆是损失最小的做法 —— 索引变大，但
     「马克杯」这类连续串一定能命中。生产要换成 jieba / ES 的 IK 分词。

召回口径：**词的全交集**（AND）。文档必须命中查询里的每一个关键词，
靠 GROUP BY + HAVING COUNT(DISTINCT keyword) = 词数 实现，命中更多词的不算更相关，
因为大家命中的词数都一样 —— 相关性差异由字段权重（SUM(weight)）体现。
"""
from __future__ import annotations

import logging
import re

from . import db

logger = logging.getLogger("portal.search")

# 字段权重：同一个词出现在标题里，通常比出现在描述里更相关
TITLE_W, CATEGORY_W, DESC_W = 3, 2, 1

# 单个关键词最长长度，和 search_keywords.keyword VARCHAR(64) 对齐
MAX_KEYWORD_LEN = 64
# 一次查询最多用多少个关键词做交集（防止超长查询把 IN 子句撑爆）
MAX_QUERY_TOKENS = 10
MAX_RESULTS = 20

_WORD_RE = re.compile(r"[a-z0-9]+")
_SPLIT_RE = re.compile(r"[a-z]+|[0-9]+")
_CJK_RE = re.compile(r"[一-鿿]")

# 停用词：命中面太大的词留着只会让交集退化成「啥都匹配」
_STOPWORDS = frozenset({
    "的", "了", "和", "是", "在", "与", "个", "及", "或",
    "the", "a", "an", "of", "to", "and", "or", "for",
})

# doc_type -> (表名, 标题列, 分类列, 描述列)
DOC_SOURCES: dict[str, tuple[str, str, str, str]] = {
    "course": ("courses", "title", "category", "description"),
    "gift": ("gifts", "name", "category", "description"),
}


def tokenize(*texts: str) -> set[str]:
    """把文本切成索引/查询共用的关键词集合。纯函数，可直接单测。"""
    tokens: set[str] = set()
    for text in texts:
        if not text:
            continue
        text = str(text)
        for match in _WORD_RE.finditer(text.lower()):
            word = match.group()
            tokens.add(word)
            # 字母段/数字段再拆一层，让 vue3 能被 vue 或 3 命中
            tokens.update(_SPLIT_RE.findall(word))
        tokens.update(ch for ch in text if _CJK_RE.match(ch))
    return {t for t in tokens if t not in _STOPWORDS and 0 < len(t) <= MAX_KEYWORD_LEN}


def build_rows(doc_type: str, doc_id: int, title: str = "",
               category: str = "", description: str = "") -> list[tuple]:
    """把一篇文档编译成索引行 [(keyword, doc_type, doc_id, weight)]。纯函数。

    同一个关键词在多个字段命中时取**最高**权重：标题里有就以 3 计，
    不让「描述里也出现过」把标题命中的分量稀释掉。
    """
    best: dict[str, int] = {}
    for text, weight in ((title, TITLE_W), (category, CATEGORY_W), (description, DESC_W)):
        for keyword in tokenize(text):
            if weight > best.get(keyword, 0):
                best[keyword] = weight
    return [(kw, doc_type, doc_id, best[kw]) for kw in sorted(best)]


_INSERT = ("INSERT INTO search_keywords (keyword, doc_type, doc_id, weight) "
           "VALUES (%s, %s, %s, %s)")


def _rows_for(doc_type: str, row: dict) -> list[tuple]:
    """把一行业务数据编译成索引行；下架/删除的文档返回空（等于从索引里摘掉）。"""
    if not row or row.get("status") != "active":
        return []
    _, title_col, cat_col, desc_col = DOC_SOURCES[doc_type]
    return build_rows(doc_type, row["id"], row.get(title_col) or "",
                      row.get(cat_col) or "", row.get(desc_col) or "")


def reindex_doc(doc_type: str, doc_id: int) -> int:
    """单篇重建：先删旧行再按当前数据写新行。后台增删改后调用。

    这是「管理员新加了课程，但搜不到」的正解 —— 索引跟着业务写操作走，
    而不是靠定期全量重建（那样中间必有一段时间搜不到）。
    """
    table = DOC_SOURCES[doc_type][0]
    row = db.query_one(f"SELECT * FROM {table} WHERE id = %s", (doc_id,))
    rows = _rows_for(doc_type, row)

    def fn(cur):
        cur.execute("DELETE FROM search_keywords WHERE doc_type = %s AND doc_id = %s",
                    (doc_type, doc_id))
        if rows:
            cur.executemany(_INSERT, rows)

    db.run_tx(fn)
    return len(rows)


def rebuild_all() -> dict[str, int]:
    """全量重建（启动时调用，也可由管理员手工触发）。返回 {doc_type: 文档数}。"""
    counts: dict[str, int] = {}

    def fn(cur):
        cur.execute("DELETE FROM search_keywords")
        for doc_type, (table, title_col, cat_col, desc_col) in DOC_SOURCES.items():
            cur.execute(
                f"SELECT id, {title_col} AS t, {cat_col} AS c, {desc_col} AS d "
                f"FROM {table} WHERE status = 'active'")
            rows = []
            for r in cur.fetchall():
                rows.extend(build_rows(doc_type, r["id"], r["t"] or "", r["c"] or "", r["d"] or ""))
            if rows:
                cur.executemany(_INSERT, rows)
            counts[doc_type] = len({r[2] for r in rows})

    db.run_tx(fn)
    logger.info("搜索索引重建完成 %s", counts)
    return counts


def _fetch_by_ids(doc_type: str, ordered_ids: list[int]) -> list[dict]:
    """按相关性排序后的 id 取回文档行，保持传入顺序（SQL 的 IN 不保证顺序）。"""
    if not ordered_ids:
        return []
    placeholders = ", ".join(["%s"] * len(ordered_ids))
    if doc_type == "course":
        sql = (f"SELECT id, title, category, level, points, emoji FROM courses "
               f"WHERE status = 'active' AND id IN ({placeholders})")
    else:
        sql = (f"SELECT id, name, category, points_cost, stock, icon FROM gifts "
               f"WHERE status = 'active' AND id IN ({placeholders})")
    by_id = {r["id"]: r for r in db.query(sql, tuple(ordered_ids))}
    return [by_id[i] for i in ordered_ids if i in by_id]


def search(q: str, limit: int = MAX_RESULTS) -> dict:
    """多词交集召回 + 字段加权排序。返回 {courses, gifts, keywords}。"""
    tokens = sorted(tokenize(q))[:MAX_QUERY_TOKENS]
    if not tokens:
        return {"courses": [], "gifts": [], "keywords": []}

    placeholders = ", ".join(["%s"] * len(tokens))
    rows = db.query(
        f"SELECT doc_type, doc_id, SUM(weight) AS score "
        f"FROM search_keywords "
        f"WHERE keyword IN ({placeholders}) "
        f"GROUP BY doc_type, doc_id "
        f"HAVING COUNT(DISTINCT keyword) = %s "   # 交集：每个查询词都必须命中
        f"ORDER BY score DESC, doc_id DESC "
        f"LIMIT %s",
        (*tokens, len(tokens), limit),
    )

    ordered: dict[str, list[int]] = {t: [] for t in DOC_SOURCES}
    for r in rows:
        if r["doc_type"] in ordered:
            ordered[r["doc_type"]].append(r["doc_id"])

    return {
        "courses": _fetch_by_ids("course", ordered["course"]),
        "gifts": _fetch_by_ids("gift", ordered["gift"]),
        "keywords": tokens,   # 回传实际使用的分词结果，方便排查「为什么搜不到」
    }
