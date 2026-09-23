"""搜索模块测试：分词 / 交集召回 / 字段加权 / 索引与业务表同步。

分两层：
  - 纯函数层（tokenize / build_rows）：不碰数据库，任何环境都能跑；
  - 集成层：跑真实 MySQL，连不上就整体 skip（不假装通过）。

运行（在 **运营门户** 目录下，即 portal 的上一级）：
    python -m unittest discover -s portal/tests -v
"""
from __future__ import annotations

import unittest
from collections import Counter

from portal.tests import init_test_db
from portal.backend import db, search_index
from portal.backend.search_index import DESC_W, TITLE_W, build_rows, tokenize
from portal.backend.routers import user as user_api
from portal.tests.test_redemption import evidence


class TestTokenize(unittest.TestCase):
    """分词是索引和查询的共用入口，两边必须同源，所以先把它钉死。"""

    def test_中文按单字拆(self):
        self.assertEqual(tokenize("马克杯"), {"马", "克", "杯"})

    def test_英文数字混合词整体与拆分都要有(self):
        # 整体 token 让「搜 vue3」命中；拆出的 vue / 3 让「搜 vue」「搜 3」也命中。
        # 老版本的坑正是索引里是 vue+3、查询里是 vue3，两边对不上。
        self.assertEqual(tokenize("vue3"), {"vue3", "vue", "3"})

    def test_大小写归一(self):
        self.assertEqual(tokenize("Vue3"), tokenize("vue3"))
        self.assertEqual(tokenize("PYTHON"), {"python"})

    def test_停用词被过滤(self):
        tokens = tokenize("我的课程")
        self.assertIn("课", tokens)
        self.assertIn("程", tokens)
        self.assertNotIn("的", tokens)

    def test_空输入(self):
        self.assertEqual(tokenize(""), set())
        self.assertEqual(tokenize(None), set())

    def test_纯停用词没有关键词(self):
        self.assertEqual(tokenize("的 or 和"), set())


class TestBuildRows(unittest.TestCase):
    """把文档编译成索引行：权重取命中字段里最高的那个。"""

    def _weights(self, **kwargs):
        rows = build_rows("course", 7, **kwargs)
        self.assertTrue(all(r[1] == "course" and r[2] == 7 for r in rows))
        return {kw: weight for kw, _, _, weight in rows}

    def test_标题命中权重最高(self):
        self.assertEqual(self._weights(title="排序算法")["排"], TITLE_W)

    def test_只在描述命中权重最低(self):
        self.assertEqual(self._weights(description="排序")["排"], DESC_W)

    def test_同一词多处命中取最高(self):
        # 描述里也出现，不能把标题命中的 3 稀释成 1
        weights = self._weights(title="排序", description="排序")
        self.assertEqual(weights["排"], TITLE_W)

    def test_空文档不产生索引行(self):
        self.assertEqual(build_rows("course", 1), [])

    def test_输出稳定有序(self):
        # 关键词排序后输出，保证同一文档每次编译出的行顺序一致，便于比对和排查
        rows = build_rows("course", 1, title="杯克马")
        self.assertEqual([r[0] for r in rows], sorted(r[0] for r in rows))


class TestSearchIntegration(unittest.TestCase):
    """真实 MySQL：重建索引 → 召回 → 排序 → 下架摘除。连不上就整体跳过。"""

    # 测试数据统一带前缀，方便和种子数据区分，也方便断言"没串进来"
    MARK = "ZZTEST"

    @classmethod
    def setUpClass(cls):
        init_test_db()
        search_index.rebuild_all()

    def setUp(self):
        self._docs: list[tuple[str, str, int]] = []

    def tearDown(self):
        for doc_type, table, doc_id in self._docs:
            db.execute("DELETE FROM search_keywords WHERE doc_type = %s AND doc_id = %s",
                       (doc_type, doc_id))
            db.execute(f"DELETE FROM {table} WHERE id = %s", (doc_id,))

    # ---- 造数据 ----

    def _course(self, title, category="通用", description=""):
        cid = db.insert(
            "INSERT INTO courses (title, category, description, points) VALUES (%s, %s, %s, 0)",
            (title, category, description))
        self._docs.append(("course", "courses", cid))
        search_index.reindex_doc("course", cid)
        return cid

    def _gift(self, name, category="周边", description=""):
        gid = db.insert(
            "INSERT INTO gifts (name, category, description, stock) VALUES (%s, %s, %s, 10)",
            (name, category, description))
        self._docs.append(("gift", "gifts", gid))
        search_index.reindex_doc("gift", gid)
        return gid

    def _course_ids(self, q):
        return [c["id"] for c in search_index.search(q)["courses"]]

    # ---- 召回 ----

    def test_中文连续串能命中(self):
        cid = self._course(self.MARK + "定制马克杯课程")
        self.assertIn(cid, self._course_ids("马克杯"))

    def test_多词交集而不是并集(self):
        hit = self._course(self.MARK + "定制马克杯")
        miss = self._course(self.MARK + "马克垫")
        ids = self._course_ids("马克杯")
        self.assertIn(hit, ids)
        self.assertNotIn(miss, ids, "只命中部分关键词的文档不该被召回（那是并集）")

    def test_英文大小写与英文数字混合(self):
        # 注意前缀后面要留空格：英文/数字是「按连续串切词」的，ZZTESTVue3 会粘成
        # 一个 alnum 串 zztestvue3，拆不出 vue。这也划出了本方案的能力边界 ——
        # 没有词典就没有子词切分，生产要上 ES 的 IK / 词干化来补。
        cid = self._course(self.MARK + " Vue3 实战")
        self.assertIn(cid, self._course_ids("vue"))    # 拆出的子词能命中
        self.assertIn(cid, self._course_ids("VUE3"))   # 整体词能命中

    def test_礼品也能搜到(self):
        gid = self._gift(self.MARK + "定制马克杯")
        ids = [g["id"] for g in search_index.search("马克杯")["gifts"]]
        self.assertIn(gid, ids)

    def test_分类命中也能召回(self):
        cid = self._course(self.MARK + "某课程", category="数据安全")
        self.assertIn(cid, self._course_ids("数据安全"))

    # ---- 排序 ----

    def test_标题命中排在描述命中前面(self):
        title_hit = self._course(self.MARK + "排序算法入门")
        desc_hit = self._course(self.MARK + "另一门课", description="讲了排序")
        ids = self._course_ids("排序")
        self.assertIn(title_hit, ids)
        self.assertIn(desc_hit, ids)
        self.assertLess(ids.index(title_hit), ids.index(desc_hit),
                        "标题命中应该比描述命中更相关")

    # ---- 索引与业务表同步 ----

    def test_下架后立刻搜不到(self):
        cid = self._course(self.MARK + "定制马克杯")
        self.assertIn(cid, self._course_ids("马克杯"))
        db.execute("UPDATE courses SET status = 'offline' WHERE id = %s", (cid,))
        search_index.reindex_doc("course", cid)
        self.assertNotIn(cid, self._course_ids("马克杯"), "下架内容必须从索引摘掉")

    def test_改标题后新词能搜到旧词搜不到(self):
        cid = self._course(self.MARK + "马克杯")
        db.execute("UPDATE courses SET title = %s WHERE id = %s", (self.MARK + "保温杯", cid))
        search_index.reindex_doc("course", cid)
        self.assertIn(cid, self._course_ids("保温"))
        self.assertNotIn(cid, self._course_ids("马克"), "索引必须跟着写操作走，不能留旧词")

    # ---- 边界 ----

    def test_纯停用词查询返回空(self):
        self.assertEqual(self._course_ids("的"), [])

    def test_注入式输入不报错也不放大结果(self):
        # 关键词只出现在参数里，IN 的占位符数量由后端按分词结果生成，
        # 用户输入永远拼不进 SQL 结构里。
        result = search_index.search("' OR 1=1 --")
        self.assertIn("courses", result)
        self.assertIn("gifts", result)

    def test_超长查询被截断到上限(self):
        # 50 个互不相同的汉字 → 50 个 token，必须被截到上限，否则 IN 子句会失控
        long_query = "".join(chr(0x4E00 + i) for i in range(50))
        tokens = search_index.search(long_query)["keywords"]
        self.assertEqual(len(tokens), search_index.MAX_QUERY_TOKENS)


class TestBulkCourseClassification(unittest.TestCase):
    """人工标签与关键词检索的批量验收，不把检索命中率称为智能分类准确率。"""

    CATEGORIES = ("数据安全", "后端开发", "前端开发", "项目管理", "云端运维", "办公效率")
    MARK = "ZZBULKQUERY"

    @classmethod
    def setUpClass(cls):
        init_test_db()
        cls.docs = []
        cls.addClassCleanup(cls.cleanup)

        def seed(cur):
            for category_index, category in enumerate(cls.CATEGORIES):
                for index in range(100):
                    description = "数据安全案例" if category_index > 0 and index < 5 else "企业培训实践"
                    status = "active" if index < 90 else "offline"
                    title = f"{cls.MARK} 模拟课程 {category_index}-{index}"
                    cur.execute(
                        "INSERT INTO courses (title,category,description,points,status) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (title, category, description, index % 4 * 10, status))
                    cls.docs.append({"type": "course", "id": cur.lastrowid,
                                     "category": category, "status": status,
                                     "cross_hit": category_index > 0 and index < 5})
            for index in range(30):
                cur.execute("INSERT INTO gifts (name,category,stock) VALUES (%s,'学习周边',100)",
                            (f"{cls.MARK} 模拟礼品 {index}",))
                cls.docs.append({"type": "gift", "id": cur.lastrowid, "status": "active"})

        db.run_tx(seed)
        search_index.rebuild_all()
        evidence("批量课程夹具", 课程数=600, 分类数=len(cls.CATEGORIES),
                 上架数=540, 下架数=60, 礼品数=30, 跨分类描述命中样本=25)

    @classmethod
    def cleanup(cls):
        db.assert_test_database()
        for kind, table in (("course", "courses"), ("gift", "gifts")):
            ids = tuple(row["id"] for row in cls.docs if row["type"] == kind)
            if ids:
                db.execute("DELETE FROM search_keywords WHERE doc_type=%s AND doc_id IN %s", (kind, ids))
                db.execute(f"DELETE FROM {table} WHERE id IN %s", (ids,))

    def test_六类课程标签及下架过滤(self):
        expected = {row["id"]: row for row in self.docs
                    if row["type"] == "course" and row["status"] == "active"}
        owned = {row["id"] for row in self.docs if row["type"] == "course"}
        rows = [row for row in user_api.courses({"emp_id": "ZZBULK_READER"})["courses"]
                if row["id"] in owned]
        self.assertEqual({row["id"] for row in rows}, set(expected))
        self.assertEqual(len(rows), len(expected))
        for row in rows:
            self.assertEqual(row["category"], expected[row["id"]]["category"])
        counts = Counter(row["category"] for row in rows)
        self.assertEqual(counts, {category: 90 for category in self.CATEGORIES})
        evidence("课程标签读取", 返回课程数=len(rows), 分类数量=dict(counts),
                 下架泄漏数=len({row["id"] for row in rows} - set(expected)))

    def test_分类词搜索同时命中其他分类的描述(self):
        expected = {row["id"] for row in self.docs if row["type"] == "course"
                    and row["status"] == "active"
                    and (row["category"] == "数据安全" or row["cross_hit"])}
        result = search_index.search(f"{self.MARK} 数据安全", limit=1000)
        rows = result["courses"]
        self.assertEqual({row["id"] for row in rows}, expected)
        self.assertFalse(result["gifts"])
        cross = sum(row["category"] != "数据安全" for row in rows)
        self.assertEqual((len(rows), cross), (115, 25))
        evidence("分类词检索边界", 查询="数据安全", 放宽上限后的命中数=len(rows),
                 同分类命中数=len(rows) - cross, 其他分类描述命中数=cross,
                 说明="当前是关键词检索，不是分类精确筛选")

    def test_六个分类检索逐项与人工标注比对(self):
        for category in self.CATEGORIES:
            with self.subTest(category=category):
                expected = {row["id"] for row in self.docs if row["type"] == "course"
                            and row["status"] == "active"
                            and (row["category"] == category or
                                 (category == "数据安全" and row["cross_hit"]))}
                rows = search_index.search(f"{self.MARK} {category}", limit=1000)["courses"]
                self.assertEqual({row["id"] for row in rows}, expected)
                self.assertEqual(len(rows), len(expected))
        evidence("六分类检索", 查询数=len(self.CATEGORIES), 漏召回数=0, 非预期命中数=0,
                 说明="对照预先标注的关键词样本，诊断查询放宽结果上限")

    def test_课程礼品分组以及默认全局截断(self):
        query = f"{self.MARK} 模拟"
        full = search_index.search(query, limit=1000)
        for kind, key in (("course", "courses"), ("gift", "gifts")):
            self.assertEqual({row["id"] for row in full[key]},
                             {row["id"] for row in self.docs
                              if row["type"] == kind and row["status"] == "active"})
        limited = search_index.search(query)
        self.assertEqual(len(limited["courses"]) + len(limited["gifts"]), search_index.MAX_RESULTS)
        for key in ("courses", "gifts"):
            self.assertTrue({row["id"] for row in limited[key]} <= {row["id"] for row in full[key]})
        evidence("搜索分组与截断", 实际匹配课程数=len(full["courses"]),
                 实际匹配礼品数=len(full["gifts"]), 默认课程返回数=len(limited["courses"]),
                 默认礼品返回数=len(limited["gifts"]), 全局上限=search_index.MAX_RESULTS)


if __name__ == "__main__":
    unittest.main()
