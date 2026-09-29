-- IT 运营门户 · MySQL 8 schema（utf8mb4，InnoDB）
-- 由 db.init_db() 在启动时幂等执行（全部 IF NOT EXISTS）

CREATE TABLE IF NOT EXISTS users (
    emp_id        VARCHAR(32)  NOT NULL,
    username      VARCHAR(64)  NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    name          VARCHAR(64)  DEFAULT '',
    role          VARCHAR(16)  DEFAULT 'user',
    department    VARCHAR(64)  DEFAULT '',
    created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (emp_id),
    UNIQUE KEY uk_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS point_accounts (
    emp_id     VARCHAR(32) NOT NULL,
    balance    INT         NOT NULL DEFAULT 0,
    updated_at DATETIME    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (emp_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS point_records (
    id         INT          NOT NULL AUTO_INCREMENT,
    emp_id     VARCHAR(32)  NOT NULL,
    points     INT          NOT NULL,           -- 正=获得，负=消耗
    note       VARCHAR(128) DEFAULT '',
    ref_type   VARCHAR(32)  NOT NULL,           -- 见下方「ref_type 与 ref_id 的约定」
    ref_id     INT          NOT NULL DEFAULT 0,
    created_at DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_ref (emp_id, ref_type, ref_id),
    KEY idx_emp (emp_id),
    KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- ref_type 与 ref_id 的约定（ref_id 只在 ref_type 语境里有意义，禁止跨语境 join）：
--   系统流水 welcome/announcement/course/activity/redeem/refund —— ref_id 是业务对象 id
--   人工流水 grant/deduct/revert/reconcile           —— ref_id 是 point_ops.id
-- 违反这条约定最典型的后果：course 流水的 ref_id=3 会撞上 point_ops.id=3，
-- 把某个不相干的管理员显示成「操作人」。所以查询里 join point_ops 必须带 ref_type 限定。

-- 管理端人工积分操作：为发放/扣减/回滚/校平提供唯一 ref_id。
-- point_records 的 uk_ref 是 (emp_id, ref_type, ref_id)，如果人工发放照搬 ref_id=0，
-- 同一个用户的第二笔发放就会撞唯一键、发不出去。先插本表拿自增 id 当 ref_id 才唯一。
CREATE TABLE IF NOT EXISTS point_ops (
    id                 INT          NOT NULL AUTO_INCREMENT,
    op_type            VARCHAR(16)  NOT NULL,           -- grant/deduct/revert/reconcile
    target_emp_id      VARCHAR(32)  NOT NULL,
    points             INT          NOT NULL,           -- 与对应 point_records.points 同值同号
    reason             VARCHAR(128) NOT NULL DEFAULT '',
    operator_emp_id    VARCHAR(32)  NOT NULL,           -- 谁操作的
    idem_key           VARCHAR(64)  NOT NULL DEFAULT '',-- 客户端 request_id，防重复提交
    reverted_record_id INT          DEFAULT NULL,       -- 仅 revert：被回滚的 point_records.id
    created_at         DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_idem (idem_key),
    UNIQUE KEY uk_reverted (reverted_record_id),
    KEY idx_target (target_emp_id, id),
    KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- uk_reverted 建在 reverted_record_id 上：MySQL 唯一索引允许多个 NULL，
-- 所以非 revert 的操作（NULL）互不冲突，而同一笔流水最多只能被回滚一次 ——
-- 靠数据库 1062 保证，不是「先查再插」的竞态。

CREATE TABLE IF NOT EXISTS announcements (
    id         INT          NOT NULL AUTO_INCREMENT,
    title      VARCHAR(255) NOT NULL,
    category   VARCHAR(64)  DEFAULT '通知',
    summary    VARCHAR(500) DEFAULT '',
    content    TEXT,
    points     INT          NOT NULL DEFAULT 0, -- 阅读奖励积分
    status     VARCHAR(16)  NOT NULL DEFAULT 'active',  -- active=上架 offline=下架
    created_at DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS courses (
    id          INT          NOT NULL AUTO_INCREMENT,
    title       VARCHAR(255) NOT NULL,
    category    VARCHAR(64)  DEFAULT '通用',
    level       VARCHAR(32)  DEFAULT '入门',
    duration    VARCHAR(32)  DEFAULT '',
    instructor  VARCHAR(64)  DEFAULT '',
    points      INT          NOT NULL DEFAULT 0, -- 完成奖励积分
    description TEXT,
    emoji       VARCHAR(16)  DEFAULT '📚',
    status      VARCHAR(16)  NOT NULL DEFAULT 'active',  -- active=上架 offline=下架
    created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS activities (
    id            INT          NOT NULL AUTO_INCREMENT,
    title         VARCHAR(255) NOT NULL,
    subtitle      VARCHAR(255) DEFAULT '',
    event_time    VARCHAR(128) DEFAULT '',
    location      VARCHAR(128) DEFAULT '',
    points        INT          NOT NULL DEFAULT 0, -- 参与奖励积分
    description   TEXT,
    emoji         VARCHAR(16)  DEFAULT '🎪',
    is_carousel   TINYINT      NOT NULL DEFAULT 0, -- 1=上轮播图
    carousel_order INT         NOT NULL DEFAULT 0, -- 轮播图排序
    status        VARCHAR(16)  NOT NULL DEFAULT 'active',  -- active=上架 offline=下架
    created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_status (status),
    KEY idx_carousel (is_carousel, carousel_order)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS gifts (
    id          INT          NOT NULL AUTO_INCREMENT,
    name        VARCHAR(255) NOT NULL,
    category    VARCHAR(64)  DEFAULT '周边',
    points_cost INT          NOT NULL DEFAULT 0,
    stock       INT          NOT NULL DEFAULT 0,
    icon        VARCHAR(64)  DEFAULT '🎁',       -- emoji 或图片路径
    image_key   VARCHAR(255) DEFAULT '',          -- OSS 对象 key（非 URL）；读取时按公开基址拼 URL
    description TEXT,
    status      VARCHAR(16)  NOT NULL DEFAULT 'active',  -- active=上架 offline=下架
    created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 库存分桶（写路径去串行化）：把热点礼品的单行 gifts.stock 拆成 K 个桶行，
-- 并发兑换按 emp 哈希落各自桶、各锁各的桶行，把「同礼品抢最后一件」的串行队
-- 拆成并行。桶存量之和才是库存真相：gifts.stock 退为展示/缓存值，读侧一律 SUM(bucket)。
-- 无超卖：每桶 UPDATE ... WHERE stock>0 的 rowcount 保证只从非空桶扣；无假售罄：
-- home 桶空时轮转下一桶；无死锁：一个兑换事务只锁一个桶行，冷路径按 bucket_no 升序锁。
CREATE TABLE IF NOT EXISTS gift_stock_bucket (
    gift_id   INT NOT NULL,
    bucket_no INT NOT NULL,
    stock     INT NOT NULL DEFAULT 0,
    PRIMARY KEY (gift_id, bucket_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS redemptions (
    id          INT         NOT NULL AUTO_INCREMENT,
    gift_id     INT         NOT NULL,
    emp_id      VARCHAR(32) NOT NULL,
    -- 下单时的会话：把「浏览了哪个礼品」和「最终兑换」串到同一次访问上，会话漏斗的最后一环靠它归因。
    -- 允许为空串：老订单没有会话，口径里一律排除（见 metrics.session_funnel）。
    session_id  VARCHAR(64) DEFAULT '',
    points_cost INT         NOT NULL,
    status      VARCHAR(16) NOT NULL DEFAULT 'pending',  -- pending=待发货 shipped=已发货 cancelled=已取消 refunded=已退货退款
    express     VARCHAR(128) DEFAULT '',
    created_at  DATETIME    DEFAULT CURRENT_TIMESTAMP,
    shipped_at  DATETIME    DEFAULT NULL,
    request_id  VARBINARY(64) DEFAULT NULL,
    response_json TEXT,
    integrity_version TINYINT NOT NULL DEFAULT 1,
    refunded_at DATETIME DEFAULT NULL,
    refund_reason VARCHAR(100) DEFAULT '',
    refund_operator VARCHAR(32) DEFAULT '',
    PRIMARY KEY (id),
    UNIQUE KEY uk_redeem_request (emp_id, request_id),
    KEY idx_emp (emp_id),
    KEY idx_gift (gift_id),
    KEY idx_status (status),
    KEY idx_session (session_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 库存流水：baseline 为接入时的库存快照，不伪造接入前的历史出入库。
CREATE TABLE IF NOT EXISTS gift_stock_records (
    id BIGINT NOT NULL AUTO_INCREMENT,
    gift_id INT NOT NULL,
    delta INT NOT NULL,
    stock_after INT NOT NULL,
    kind VARCHAR(16) NOT NULL,
    ref_id INT DEFAULT NULL,
    operator_emp_id VARCHAR(32) NOT NULL,
    request_id VARBINARY(64) DEFAULT NULL,
    reason VARCHAR(100) NOT NULL DEFAULT '',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_stock_ref (gift_id, kind, ref_id),
    UNIQUE KEY uk_stock_request (operator_emp_id, request_id),
    KEY idx_stock_gift (gift_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS announcement_reads (
    id             INT         NOT NULL AUTO_INCREMENT,
    announcement_id INT        NOT NULL,
    emp_id         VARCHAR(32) NOT NULL,
    read_at        DATETIME    DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk (announcement_id, emp_id),
    KEY idx_emp (emp_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS training_progress (
    id        INT         NOT NULL AUTO_INCREMENT,
    course_id INT         NOT NULL,
    emp_id    VARCHAR(32) NOT NULL,
    -- 报名时的会话（同 redemptions.session_id）：课程浏览→报名的会话归因靠它。
    -- 只在 INSERT 时写入，完成课程不改 —— 语义是「这条进度首次产生于哪个会话」。
    session_id VARCHAR(64) DEFAULT '',
    enrolled  TINYINT     NOT NULL DEFAULT 0,
    progress  INT         NOT NULL DEFAULT 0,
    completed TINYINT     NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE KEY uk (course_id, emp_id),
    KEY idx_emp (emp_id),
    KEY idx_session (session_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS activity_participants (
    id             INT         NOT NULL AUTO_INCREMENT,
    activity_id    INT         NOT NULL,
    emp_id         VARCHAR(32) NOT NULL,
    participated_at DATETIME   DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk (activity_id, emp_id),
    KEY idx_emp (emp_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS feedbacks (
    id         INT          NOT NULL AUTO_INCREMENT,
    emp_id     VARCHAR(32)  NOT NULL,
    category   VARCHAR(64)  DEFAULT '其他',
    content    TEXT,
    rating     INT          DEFAULT 5,
    status     VARCHAR(16)  DEFAULT 'open',
    created_at DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS user_access_logs (
    id          INT         NOT NULL AUTO_INCREMENT,
    event_id    VARCHAR(96) NOT NULL,           -- 幂等键：重试/双击/网络重发的同一次事件只落一条
    session_id  VARCHAR(64) DEFAULT '',         -- 会话：串单会话漏斗、算访问深度
    emp_id      VARCHAR(32) NOT NULL,
    event_type  VARCHAR(32) NOT NULL,           -- 白名单见 logic.ALLOWED_EVENTS
    ref_type    VARCHAR(32) DEFAULT '',
    ref_id      INT         DEFAULT NULL,
    properties  TEXT,
    client_time DATETIME    DEFAULT NULL,       -- 客户端事件时间，仅诊断时钟偏移，不参与口径
    accessed_at DATETIME    DEFAULT CURRENT_TIMESTAMP,  -- 服务端落库时间 = 唯一指标时间口径
    PRIMARY KEY (id),
    UNIQUE KEY uk_event (event_id),
    KEY idx_event_time (event_type, accessed_at),
    KEY idx_session (session_id),
    KEY idx_ref (ref_type, ref_id),
    KEY idx_emp (emp_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 搜索倒排索引：一行一个「关键词 → 文档」，weight 是该词命中的最高字段权重
-- （3=标题 2=分类 1=描述）。建索引和查询共用 search_index.tokenize()。
CREATE TABLE IF NOT EXISTS search_keywords (
    id       INT         NOT NULL AUTO_INCREMENT,
    keyword  VARCHAR(64) NOT NULL,
    doc_type VARCHAR(16) NOT NULL,            -- course | gift
    doc_id   INT         NOT NULL,
    weight   TINYINT     NOT NULL DEFAULT 1,
    PRIMARY KEY (id),
    UNIQUE KEY uk_kw_doc (keyword, doc_type, doc_id),
    KEY idx_kw (keyword, doc_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 搜不到的词（进阶）：零结果查询落一张汇总表，运营看板按次数排序反哺内容。
-- 存 tokenize 后的原始查询（非 token）：运营要看的是「用户搜了什么」。
CREATE TABLE IF NOT EXISTS search_zero_terms (
    id         INT         NOT NULL AUTO_INCREMENT,
    term       VARCHAR(64) NOT NULL,
    hits       INT         NOT NULL DEFAULT 1,
    last_seen  DATETIME    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_term (term),
    KEY idx_hits (hits)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS app_settings (
    name  VARCHAR(64) NOT NULL,
    value TEXT,
    PRIMARY KEY (name)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS audit_logs (
    id          INT         NOT NULL AUTO_INCREMENT,
    emp_id      VARCHAR(32) NOT NULL,
    action      VARCHAR(64) NOT NULL,
    target_type VARCHAR(32) DEFAULT '',
    target_id   INT         DEFAULT NULL,
    detail      TEXT,
    -- 哈希链（进阶）：row_hash = SHA256(前一行哈希 | 本行业务字段)，prev_hash 指向链尾。
    -- 空串 = 接入前的历史行（v1 基线），不参与校验；篡改任意 v2 行会让 verify 断链。
    prev_hash   VARCHAR(64) NOT NULL DEFAULT '',
    row_hash    VARCHAR(64) NOT NULL DEFAULT '',
    -- 审计锚点分片：本行属于第几条链（= stable_slot(emp_id)）。单行锚点表已拆成
    -- 多条独立链，每行必须记住自己在哪条链上，verify/backfill 才能按链重接。
    chain_id    TINYINT     NOT NULL DEFAULT 0,
    created_at  DATETIME    DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_emp (emp_id),
    KEY idx_action (action),
    KEY idx_created (created_at),
    KEY idx_chain (chain_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 链尾指针：单行表，写审计时 FOR UPDATE 锁这一行把并发追加串行化。
-- 为什么不用「读最后一行 FOR UPDATE」：表空时无行可锁，间隙锁会把整表锁成
-- 串行域还容易和业务写入对撞死锁；锚点行是确定的主键行锁，代价可预测。
CREATE TABLE IF NOT EXISTS audit_chain (
    id       TINYINT     NOT NULL,
    last_hash VARCHAR(64) NOT NULL DEFAULT '',
    seq      INT         NOT NULL DEFAULT 0,
    PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS notifications (
    id         INT          NOT NULL AUTO_INCREMENT,
    emp_id     VARCHAR(32)  NOT NULL,
    title      VARCHAR(255) NOT NULL,
    content    VARCHAR(500) DEFAULT '',
    ntype      VARCHAR(32)  DEFAULT 'system',  -- redeem / ship / refund / low_stock / system
    ref_id     INT          DEFAULT NULL,
    -- 来源发件箱行号。与 uk_outbox 一起构成投递幂等：投递器是「至少一次」，
    -- 重复投递撞这条唯一键变成空操作，而不是靠内存标记去重。
    -- 可空是为了兼容历史行：MySQL 唯一索引允许多个 NULL，老数据不受影响。
    outbox_id  INT          DEFAULT NULL,
    is_read    TINYINT      NOT NULL DEFAULT 0,
    created_at DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_emp (emp_id, is_read),
    KEY idx_created (created_at),
    UNIQUE KEY uk_outbox (outbox_id, emp_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 通知发件箱（transactional outbox）：业务事务只往这里插一行，投递在事务提交后
-- 由后台线程完成。拆成两步解决一个矛盾 —— 通知是这次业务的结果凭证，不能丢；
-- 但投递过程（站内信落库、邮件、短信、企微）又绝不能拖长业务事务，尤其是外部
-- 渠道的网络调用。
-- 「只有提交成功才发通知」不需要任何记账代码：行写在业务事务里，回滚时跟着一起
-- 消失，由 InnoDB 的提交可见性保证（和 events.py 把 point_records 当 outbox 同源）。
CREATE TABLE IF NOT EXISTS notification_outbox (
    id              INT          NOT NULL AUTO_INCREMENT,
    event_key       VARCHAR(128) NOT NULL,  -- 业务事件幂等键，重放/重试不会重复入队
    audience        VARCHAR(64)  NOT NULL,  -- 'user:<emp_id>' / 'role:super_admin,shop_admin'
    ntype           VARCHAR(32)  NOT NULL DEFAULT 'system',
    title           VARCHAR(255) NOT NULL,
    content         VARCHAR(500) NOT NULL DEFAULT '',
    ref_type        VARCHAR(32)  DEFAULT '',
    ref_id          INT          DEFAULT NULL,
    status          VARCHAR(16)  NOT NULL DEFAULT 'pending',  -- pending=待投递 sent=已投递 dead=重试耗尽
    attempts        TINYINT      NOT NULL DEFAULT 0,
    next_attempt_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,  -- 退避到期时间
    last_error      VARCHAR(255) NOT NULL DEFAULT '',
    created_at      DATETIME     DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    sent_at         DATETIME     DEFAULT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_event (event_key),
    KEY idx_due (status, next_attempt_at),
    KEY idx_reap (status, sent_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS quiz_questions (
    id         INT          NOT NULL AUTO_INCREMENT,
    course_id  INT          NOT NULL,
    question   VARCHAR(500) NOT NULL,
    option_a   VARCHAR(255) DEFAULT '',
    option_b   VARCHAR(255) DEFAULT '',
    option_c   VARCHAR(255) DEFAULT '',
    option_d   VARCHAR(255) DEFAULT '',
    answer     VARCHAR(1)   NOT NULL,   -- A/B/C/D
    created_at DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_course (course_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS quiz_attempts (
    id         INT         NOT NULL AUTO_INCREMENT,
    emp_id     VARCHAR(32) NOT NULL,
    course_id  INT         NOT NULL,
    score      INT         NOT NULL,
    total      INT         NOT NULL,
    created_at DATETIME    DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_emp (emp_id),
    KEY idx_course (course_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 秒杀场次（演进 D 定时化）：管理员上架某礼品时排一个「几点开抢」的场次。
-- 真相仍在 DB：status 由调度线程按时间推进 scheduled→live→ended；live 时把额度预热进 Redis。
-- stock 是本场秒杀额度（Redis 预扣上界），与礼品真实库存对齐由预热时的 COALESCE 口径保证。
CREATE TABLE IF NOT EXISTS seckill_sessions (
    id          INT          NOT NULL AUTO_INCREMENT,
    gift_id     INT          NOT NULL,
    start_at    DATETIME     NOT NULL,             -- 开抢时刻
    end_at      DATETIME     DEFAULT NULL,          -- 收摊时刻（转 live 时按窗口算出）
    stock       INT          NOT NULL DEFAULT 0,    -- 本场额度
    status      VARCHAR(16)  NOT NULL DEFAULT 'scheduled',  -- scheduled/live/ended/cancelled
    notified_at DATETIME     DEFAULT NULL,          -- 提前 N 分钟提醒已发的时间戳（判重，只发一次）
    created_by  VARCHAR(32)  NOT NULL,
    created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_gift_status (gift_id, status),
    KEY idx_status_start (status, start_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- 秒杀预约名单：预约=订阅开抢提醒（不锁名额，名额仍到点先到先得）。
-- uk(session_id, emp_id) 让重复预约幂等，也是提醒受众的唯一来源。
CREATE TABLE IF NOT EXISTS seckill_reservations (
    id         INT         NOT NULL AUTO_INCREMENT,
    session_id INT         NOT NULL,
    emp_id     VARCHAR(32) NOT NULL,
    created_at DATETIME    DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_sess_emp (session_id, emp_id),
    KEY idx_emp (emp_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
