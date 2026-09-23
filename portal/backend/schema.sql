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
    description TEXT,
    status      VARCHAR(16)  NOT NULL DEFAULT 'active',  -- active=上架 offline=下架
    created_at  DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS redemptions (
    id          INT         NOT NULL AUTO_INCREMENT,
    gift_id     INT         NOT NULL,
    emp_id      VARCHAR(32) NOT NULL,
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
    KEY idx_status (status)
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
    enrolled  TINYINT     NOT NULL DEFAULT 0,
    progress  INT         NOT NULL DEFAULT 0,
    completed TINYINT     NOT NULL DEFAULT 0,
    PRIMARY KEY (id),
    UNIQUE KEY uk (course_id, emp_id),
    KEY idx_emp (emp_id)
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
    created_at  DATETIME    DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_emp (emp_id),
    KEY idx_action (action),
    KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS notifications (
    id         INT          NOT NULL AUTO_INCREMENT,
    emp_id     VARCHAR(32)  NOT NULL,
    title      VARCHAR(255) NOT NULL,
    content    VARCHAR(500) DEFAULT '',
    ntype      VARCHAR(32)  DEFAULT 'system',  -- redeem / ship / refund / low_stock / system
    ref_id     INT          DEFAULT NULL,
    is_read    TINYINT      NOT NULL DEFAULT 0,
    created_at DATETIME     DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_emp (emp_id, is_read),
    KEY idx_created (created_at)
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
