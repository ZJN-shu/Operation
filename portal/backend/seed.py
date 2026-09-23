"""灌入演示数据（幂等：仅当 users 表为空时执行）。"""
from __future__ import annotations

from . import db, redemption
from .auth import hash_password


def _earn(emp_id: str, points: int, note: str, ref_type: str, ref_id: int = 0,
          days_ago: int = 0) -> int:
    """写一笔「系统产生的」流水并同步余额，返回 point_records.id。

    为什么必须在一个事务里改两张表：拆成两条各自自动提交的语句，就是历史上那几个
    「余额 ≠ 流水合计」账户的来源（见 admin.points_drift 与 README 的能力边界）。
    生产路径同样只有这一个原子写入方：logic.add_points。
    """
    def fn(cur):
        cur.execute(
            "INSERT INTO point_records (emp_id, points, note, ref_type, ref_id, created_at) "
            "VALUES (%s, %s, %s, %s, %s, DATE_SUB(NOW(), INTERVAL %s DAY))",
            (emp_id, points, note, ref_type, ref_id, days_ago),
        )
        rid = cur.lastrowid          # 必须在下面的 UPDATE 之前捕获，否则读到的是 0
        cur.execute(
            "UPDATE point_accounts SET balance = balance + %s WHERE emp_id = %s",
            (points, emp_id),
        )
        return rid
    return db.run_tx(fn)


def _manual(op_type: str, emp_id: str, points: int, note: str, reason: str,
            operator_emp_id: str, days_ago: int = 0,
            reverted_record_id: int = None) -> int:
    """灌一笔人工操作（发放 grant / 回滚 revert），返回 point_records.id。

    为什么不能只插流水：人工流水的 ref_id 指向 point_ops.id（见 schema.sql 里
    「ref_type 与 ref_id 的约定」）。少了那条 point_ops，列表里这笔的「操作人」
    查不出来、回滚按钮也不会出现 —— 演示数据就演示不了完整链路。
    """
    op_id = db.insert(
        "INSERT INTO point_ops (op_type, target_emp_id, points, reason, operator_emp_id, "
        "idem_key, reverted_record_id, created_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, DATE_SUB(NOW(), INTERVAL %s DAY))",
        (op_type, emp_id, points, reason, operator_emp_id,
         f"seed-{op_type}-{emp_id}-{abs(points)}-{days_ago}", reverted_record_id, days_ago),
    )
    return _earn(emp_id, points, note, op_type, op_id, days_ago)


def seed() -> bool:
    if db.query_one("SELECT COUNT(*) AS c FROM users")["c"] > 0:
        return False

    # ---- 用户（多角色：超级管理员 / 内容运营 / 商城运营 / 只读运营 / 普通用户）----
    db.execute(
        "INSERT INTO users (emp_id, username, password_hash, name, role, department) VALUES "
        "(%s,%s,%s,%s,%s,%s),(%s,%s,%s,%s,%s,%s),(%s,%s,%s,%s,%s,%s),"
        "(%s,%s,%s,%s,%s,%s),(%s,%s,%s,%s,%s,%s),(%s,%s,%s,%s,%s,%s)",
        (
            "1001", "admin", hash_password("admin123"), "运营管理员", "super_admin", "信息技术部",
            "1002", "user01", hash_password("123456"), "张三", "user", "市场部",
            "1003", "user02", hash_password("123456"), "李四", "user", "产品部",
            "1004", "content", hash_password("content123"), "内容运营", "content_admin", "培训组",
            "1005", "shop", hash_password("shop123"), "商城运营", "shop_admin", "行政组",
            "1006", "viewer", hash_password("viewer123"), "只看运营", "viewer", "运营组",
        ),
    )
    db.execute(
        "INSERT INTO point_accounts (emp_id, balance) VALUES "
        "('1001', 0), ('1002', 0), ('1003', 0), ('1004', 0), ('1005', 0), ('1006', 0)"
    )

    # ---- 公告 ----
    a1 = db.insert(
        "INSERT INTO announcements (title, category, summary, content, points) VALUES (%s,%s,%s,%s,%s)",
        ("IT DAY 2026 正式开启", "通知", "一年一度的 IT DAY 来了，参与活动赢积分兑好礼！", "详情见活动中心，欢迎踊跃报名。", 10),
    )
    a2 = db.insert(
        "INSERT INTO announcements (title, category, summary, content, points) VALUES (%s,%s,%s,%s,%s)",
        ("办公网络升级通知", "系统", "本周六 0:00-4:00 办公网络升级，期间可能短暂断网。", "请提前保存工作内容。", 10),
    )
    a3 = db.insert(
        "INSERT INTO announcements (title, category, summary, content, points) VALUES (%s,%s,%s,%s,%s)",
        ("新员工入职指南", "指南", "入职必读：账号、工卡、报销、考勤一网打尽。", "详见指南全文。", 5),
    )

    # ---- 课程 ----
    c1 = db.insert(
        "INSERT INTO courses (title, category, level, duration, instructor, points, description, emoji) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        ("Windows 蓝屏排查实战", "运维", "进阶", "2 小时", "王工", 50, "从蓝屏代码定位到常见故障处理，一节课上手。", "🖥️"),
    )
    c2 = db.insert(
        "INSERT INTO courses (title, category, level, duration, instructor, points, description, emoji) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        ("Python 快速入门", "开发", "入门", "3 小时", "李老师", 40, "零基础学会 Python 语法与脚本。", "🐍"),
    )
    c3 = db.insert(
        "INSERT INTO courses (title, category, level, duration, instructor, points, description, emoji) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        ("Excel 高效办公技巧", "办公", "入门", "1.5 小时", "张老师", 30, "函数、透视表、图表一次讲透。", "📊"),
    )
    c4 = db.insert(
        "INSERT INTO courses (title, category, level, duration, instructor, points, description, emoji) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        ("网络安全意识", "安全", "入门", "1 小时", "赵工", 20, "钓鱼邮件识别与密码安全。", "🔐"),
    )
    c5 = db.insert(
        "INSERT INTO courses (title, category, level, duration, instructor, points, description, emoji) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        ("SQL 查询优化", "数据", "进阶", "2.5 小时", "王工", 60, "索引、执行计划与慢查询优化实战。", "🗄️"),
    )

    # ---- 活动（前三个上轮播图）----
    act1 = db.insert(
        "INSERT INTO activities (title, subtitle, event_time, location, points, description, emoji, is_carousel, carousel_order) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        ("IT DAY 2026 技术嘉年华", "年度科技盛会", "2026-09-25 09:00", "一楼展厅", 20, "技术展台、互动游戏、丰厚奖品。", "🎉", 1, 1),
    )
    act2 = db.insert(
        "INSERT INTO activities (title, subtitle, event_time, location, points, description, emoji, is_carousel, carousel_order) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        ("AI 办公应用分享会", "让 AI 帮你提效", "2026-09-28 14:00", "会议室 A", 15, "分享 AI 工具在日常办公中的落地。", "🤖", 1, 2),
    )
    act3 = db.insert(
        "INSERT INTO activities (title, subtitle, event_time, location, points, description, emoji, is_carousel, carousel_order) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        ("信息安全月·答题挑战", "答题赢积分", "2026-10-01 全天", "线上", 10, "参与安全知识答题，赢取积分奖励。", "🛡️", 1, 3),
    )
    act4 = db.insert(
        "INSERT INTO activities (title, subtitle, event_time, location, points, description, emoji, is_carousel, carousel_order) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        ("新人见面会", "认识新伙伴", "2026-10-10 16:00", "咖啡角", 5, "轻松交流，快速融入团队。", "👋", 0, 0),
    )

    # ---- 礼品 ----
    g1 = db.insert(
        "INSERT INTO gifts (name, category, points_cost, stock, icon, description) VALUES (%s,%s,%s,%s,%s,%s)",
        ("定制马克杯", "桌面周边", 60, 18, "☕", "公司定制陶瓷马克杯，容量 350ml，杯身印有 IT DAY 专属图案。"),
    )
    g2 = db.insert(
        "INSERT INTO gifts (name, category, points_cost, stock, icon, description) VALUES (%s,%s,%s,%s,%s,%s)",
        ("品牌 T 恤", "服饰", 150, 10, "👕", "纯棉圆领 T 恤，经典黑/白两色，胸口刺绣公司 Logo。"),
    )
    g3 = db.insert(
        "INSERT INTO gifts (name, category, points_cost, stock, icon, description) VALUES (%s,%s,%s,%s,%s,%s)",
        ("蓝牙耳机", "数码", 300, 5, "🎧", "真无线蓝牙耳机，续航 24 小时，支持降噪通话。"),
    )
    g4 = db.insert(
        "INSERT INTO gifts (name, category, points_cost, stock, icon, description) VALUES (%s,%s,%s,%s,%s,%s)",
        ("公司帆布袋", "周边", 80, 14, "👜", "加厚帆布环保袋，容量大、耐用，通勤买菜两相宜。"),
    )
    g5 = db.insert(
        "INSERT INTO gifts (name, category, points_cost, stock, icon, description) VALUES (%s,%s,%s,%s,%s,%s)",
        ("京东卡 50 元", "卡券", 500, 3, "💳", "京东电子礼品卡，面值 50 元，兑换后发放卡密。"),
    )
    g6 = db.insert(
        "INSERT INTO gifts (name, category, points_cost, stock, icon, description) VALUES (%s,%s,%s,%s,%s,%s)",
        ("公司贴纸", "周边", 20, 50, "🏷️", "公司吉祥物主题贴纸一套，积分门槛低，人人可兑。"),
    )

    def baselines(cur):
        cur.execute("SELECT id, stock FROM gifts ORDER BY id FOR UPDATE")
        for gift in cur.fetchall():
            redemption.stock_baseline(cur, gift, "1001")
    db.run_tx(baselines)

    # ---- 积分事件（欢迎 + 获得 + 消耗，与余额一致；按 7 天分散便于看趋势）----
    for i, emp in enumerate(("1001", "1002", "1003", "1004", "1005", "1006")):
        _earn(emp, 100, "新用户注册奖励", "welcome", days_ago=6 - i)

    _earn("1002", 40, "完成课程", "course", c2, days_ago=0)
    _earn("1002", 20, "参与活动", "activity", act1, days_ago=1)
    _earn("1002", 10, "阅读公告", "announcement", a1, days_ago=3)
    _earn("1003", 10, "阅读公告", "announcement", a1, days_ago=1)

    # ---- 业务参与/阅读记录（与积分流水对应，供漏斗统计）----
    db.execute(
        "INSERT INTO training_progress (course_id, emp_id, enrolled, progress, completed) "
        "VALUES (%s, %s, 1, 100, 1)",
        (c2, "1002"),
    )
    db.execute(
        "INSERT INTO announcement_reads (announcement_id, emp_id) VALUES (%s,%s),(%s,%s)",
        (a1, "1002", a1, "1003"),
    )
    db.execute(
        "INSERT INTO activity_participants (activity_id, emp_id) VALUES (%s,%s)",
        (act1, "1002"),
    )

    # ---- 课程单选题 ----
    db.execute(
        "INSERT INTO quiz_questions (course_id, question, option_a, option_b, option_c, option_d, answer) VALUES "
        "(%s,%s,%s,%s,%s,%s,%s),(%s,%s,%s,%s,%s,%s,%s),(%s,%s,%s,%s,%s,%s,%s)",
        (c2, "Python 中用于输出到控制台的函数是？", "print", "input", "echo", "console", "A",
         c2, "Python 中定义函数的关键字是？", "func", "def", "function", "define", "B",
         c2, "下面哪个写法是 Python 列表？", "[]", "{}", "()", "<>", "A"),
    )
    db.execute(
        "INSERT INTO quiz_questions (course_id, question, option_a, option_b, option_c, option_d, answer) VALUES "
        "(%s,%s,%s,%s,%s,%s,%s)",
        (c1, "Windows 蓝屏通常被称为什么？", "BSOD", "DOS", "BIOS", "CPU", "A"),
    )
    db.execute(
        "INSERT INTO quiz_attempts (emp_id, course_id, score, total) VALUES (%s,%s,%s,%s)",
        ("1002", c2, 2, 3),
    )

    # ---- 兑换订单（含发货演示）----
    for n, (gid, emp) in enumerate(((g1, "1002"), (g4, "1002"), (g1, "1003"))):
        result = redemption.redeem(gid, redemption.RequestIn(request_id=f"seed-order-{n}"), emp)
        if not result["ok"]:
            raise RuntimeError("演示兑换失败，停止初始化")
        if n == 0:
            redemption.ship(result["redemption_id"], "SF1234567890", "1005")

    # ---- 人工积分操作（让人一打开积分明细就能看到完整链路）----
    # 有这两笔，页面上才看得到「操作人」和「可回滚」；否则整页都是系统流水。
    _manual("grant", "1003", 50, "人工发放：IT DAY 现场抽奖补录",
            "IT DAY 现场抽奖补录", "1001", days_ago=1)
    # 回滚只能针对「系统产生的」流水 —— 人工发放产生的流水是明确不允许回滚的
    # （否则可以回滚一条回滚，账本变成镜厅）。所以先造一笔发错的，再把它收回。
    wrong = _earn("1003", 80, "参与活动", "activity", act1, days_ago=2)
    _manual("revert", "1003", -80, "回滚：参与活动（重复发放，已收回）",
            "重复发放，已收回", "1001", days_ago=2, reverted_record_id=wrong)

    # ---- 事件埋点（漏斗/指标数据源）----
    # 演示数据里每次调用都给独立 event_id：同一用户重复浏览同一课程仍是两次点击，
    # 幂等键只负责挡住「同一次事件的重投」，不做业务去重（见 logic.make_event_id）。
    _seq = [0]

    def ev(emp_id, event_type, ref_type="", ref_id=None, properties=None):
        _seq[0] += 1
        db.execute(
            "INSERT INTO user_access_logs "
            "(event_id, session_id, emp_id, event_type, ref_type, ref_id, properties) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (f"seed:{emp_id}:{_seq[0]}", f"seed-sess-{emp_id}", emp_id,
             event_type, ref_type, ref_id, properties),
        )

    for emp in ("1001", "1002", "1003"):
        ev(emp, "page_visit")
    for emp, kw in (("1002", "马克杯"), ("1003", "Python"), ("1002", "蓝屏")):
        ev(emp, "search", properties='{"keyword":"%s"}' % kw)
    ev("1002", "course_view", "course", c1)
    ev("1002", "course_view", "course", c2)
    ev("1002", "course_view", "course", c2)  # 二次浏览，体现点击
    ev("1003", "course_view", "course", c1)
    ev("1002", "activity_view", "activity", act1)
    ev("1003", "activity_view", "activity", act1)
    ev("1002", "gift_view", "gift", g1)
    ev("1002", "gift_view", "gift", g4)
    ev("1003", "gift_view", "gift", g1)
    ev("1002", "announcement_view", "announcement", a1)
    ev("1003", "announcement_view", "announcement", a1)
    ev("1002", "points_view")
    ev("1003", "points_view")

    # ---- 站内信（演示欢迎通知）----
    for emp in ("1001", "1002", "1003", "1004", "1005", "1006"):
        db.execute(
            "INSERT INTO notifications (emp_id, title, content, ntype) VALUES (%s,%s,%s,'system')",
            (emp, "欢迎使用 IT 运营门户", "欢迎！登录后逛课程、攒积分、兑好礼。"),
        )

    return True
