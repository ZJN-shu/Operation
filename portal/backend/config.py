"""集中配置：读取 .env 环境变量，缺省给本地默认值。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent  # portal/


def _load_dotenv() -> None:
    """极简 .env 加载器（不依赖 python-dotenv）。"""
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


TESTING = os.getenv("PORTAL_TESTING") == "1"
if not TESTING:
    _load_dotenv()

# 测试连接只消费专用变量，绝不回退到应用数据库凭据。
_PREFIX = "PORTAL_TEST_" if TESTING else ""
MYSQL_HOST = os.getenv(_PREFIX + "MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv(_PREFIX + "MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv(_PREFIX + "MYSQL_USER", "portal_test" if TESTING else "root")
MYSQL_PASSWORD = os.getenv(_PREFIX + "MYSQL_PASSWORD", "")
MYSQL_DB = os.getenv(_PREFIX + "MYSQL_DB", "ops_portal_test_unconfigured" if TESTING else "ops_portal")
DEMO_MODE = not TESTING and os.getenv("PORTAL_DEMO_MODE") == "1"
MYSQL_CONNECT_TIMEOUT = 5
# 连接池参数
MYSQL_POOL_MAX = int(os.getenv("MYSQL_POOL_MAX", "20"))
MYSQL_POOL_MIN_CACHED = int(os.getenv("MYSQL_POOL_MIN_CACHED", "2"))
MYSQL_POOL_MAX_CACHED = int(os.getenv("MYSQL_POOL_MAX_CACHED", "10"))
SECRET_KEY = "isolated-test-secret" if TESTING else os.getenv("SECRET_KEY", "dev-secret-change-me")

# 会话 TTL（进阶）：空闲超时 + 绝对超时，分钟。0 = 不限（兼容旧行为/测试）。
# 空闲 30 分钟、绝对 8 小时是设计起点，不是压测结论；部署时按业务体验调整。
SESSION_IDLE_MINUTES = int(os.getenv("SESSION_IDLE_MINUTES", "30"))
SESSION_ABSOLUTE_MINUTES = int(os.getenv("SESSION_ABSOLUTE_MINUTES", "480"))

# 写路径去串行化（纯代码层演进）：
# 审计锚点分片 —— 全局单条哈希链把「一切写事务」在提交前的最后一步串成队，
# 是危害最大的串行点。按 emp_id 稳定哈希分 N 条独立链，不同用户走不同锚点行，
# 提交尾不再互斥；同一用户恒定落同一分链，链内仍严格可验。用 hashlib 而非内建
# hash()：后者带跨进程 salt，多 worker 下同 emp 会漂到不同分链，链直接分叉。
AUDIT_CHAIN_SHARDS = int(os.getenv("AUDIT_CHAIN_SHARDS", "8"))
# 库存分桶 —— 热点礼品的单行 `gifts.stock` 被 SELECT ... FOR UPDATE 全程持锁到提交，
# 同礼品并发兑换在这一行上排成串行队。把库存拆到 K 个桶行，用户按 emp 哈希落各自桶，
# 各自锁各自的桶行，并行度 ~ min(桶数, 分链数)。gifts.stock 退为展示缓存，真相以 SUM(桶) 为准。
STOCK_BUCKETS = int(os.getenv("STOCK_BUCKETS", "8"))

# 会话后端（演进 B：Redis 共享会话 → 解锁多 worker）：
# 进程内会话表是「单 uvicorn worker」的根因——多 worker 下 A worker 建的 token，
# B worker 的 _SESSIONS 里没有，直接 401。把会话外置到 Redis，token 跨进程可读，
# 才能把 worker 数抬到核数、抬高 CPU 天花板（演进 A 拆掉的 DB 锁此时才承接并行）。
# 默认 process：测试与单实例行为不变、零新依赖路径；redis：走共享会话。
SESSION_BACKEND = os.getenv("SESSION_BACKEND", "process")
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")

# 异步化（演进 C：非关键写移出同步兑换事务）：
# 演进 B 把瓶颈精确定位到「同步 redeem 事务的 DB 端到端时延」。这一层用 Redis Streams
# 当轻量 MQ，把 redeem 事务里的审计哈希链追加、通知发件箱登记，以及全局埋点写入，
# 从「请求同步等它落库」改成「提交后 XADD 入流 + 消费者组异步落库」——缩短单请求关键路径。
# 关键写（扣桶 / 扣分 / 建订单 / 幂等重放）绝不出事务：不超卖、账实一致的边界一寸不让。
# 默认 sync：测试与单实例行为不变（这些写仍同步、仍在事务内、仍随回滚消失）。
# redis_streams：启用出流；消费者线程随 worker 一起在 lifespan 起，消费组自动按 worker 数横向扩。
MQ_BACKEND = os.getenv("MQ_BACKEND", "sync")
MQ_STREAM = os.getenv("MQ_STREAM", "{portal}:async_writes")
MQ_GROUP = os.getenv("MQ_GROUP", "writers")
MQ_BLOCK_MS = int(os.getenv("MQ_BLOCK_MS", "1000"))

# 秒杀（演进 D：紧俏商品高并发抢购）：分层漏斗把「售罄风暴」挡在 MySQL 之外。
# Redis 只做**前置削峰计数器**（Lua 原子预扣 + 限购），扣不到直接判售罄、零 DB 触达；
# 预扣成功后入 Redis Stream 队列，消费者以有限并发复用既有 redemption.redeem 事务落单
# （扣桶/扣分/建订单/幂等/对账边界一寸不动，Redis 绝不改 DB 库存真相）。
# 落单失败（如积分不足）回补 Redis 预扣名额，让别的请求能抢——「抢到≠买到」。
# 默认关：不影响既有兑换链路与测试；SECKILL_ENABLED=1 且备有可达 Redis 才启用。
SECKILL_ENABLED = os.getenv("SECKILL_ENABLED") == "1"
SECKILL_STREAM = os.getenv("SECKILL_STREAM", "{portal}:seckill:queue")
SECKILL_GROUP = os.getenv("SECKILL_GROUP", "seckill-writer")
# 消费并发度：秒杀落单仍受单实例 MySQL 提交时延约束，这里刻意用小并发串行消化队列，
# 靠 Redis 预扣把「队首长度」压到 ≤ 库存，DB 承接的是「有效抢购数」而非「请求洪峰数」。
SECKILL_CONSUMERS = int(os.getenv("SECKILL_CONSUMERS", "2"))
SECKILL_BLOCK_MS = int(os.getenv("SECKILL_BLOCK_MS", "1000"))
# 预热键的活动 TTL：兜底防止活动结束还残留名额占着 Redis；一次秒杀通常几分钟内打完。
SECKILL_TTL_SECONDS = int(os.getenv("SECKILL_TTL_SECONDS", "86400"))
# 定时场次调度：提前多少分钟给预约者发提醒、开抢后多久自动收摊、调度轮询间隔。
SECKILL_REMIND_MINUTES = int(os.getenv("SECKILL_REMIND_MINUTES", "5"))
SECKILL_LIVE_MINUTES = int(os.getenv("SECKILL_LIVE_MINUTES", "30"))
SECKILL_TICK_SECONDS = int(os.getenv("SECKILL_TICK_SECONDS", "5"))

# 密码哈希：v2 = 每用户随机盐（自描述格式），v1 = 全局 SECRET_KEY 盐（历史存量）。
# 登录校验通过后透明升级到 v2，不需要强制改密。
PBKDF2_ITERATIONS = int(os.getenv("PBKDF2_ITERATIONS", "100_000"))

# 迁移与启动分离（进阶部署）：默认 1 保持既有「启动即建表迁移」行为；
# 容器部署时应用账号只给 DML 权限，把 RUN_MIGRATIONS 置 0，
# 结构变更交给一次性 migrate 服务用迁移账号跑 python -m portal.migrate。
RUN_MIGRATIONS = os.getenv("RUN_MIGRATIONS", "1") == "1"

# 阿里云 OSS 图片直传（真实落地：对象存储）。默认全关，不配 AK/SK 时行为与现在一致。
# 只走「服务端签受限直传凭证 + 前端直传私有桶」，应用服务器不经手文件体。
# 没引入 oss2 SDK：签名用标准库 hmac/hashlib 手推（见 backend/oss.py），守住零新增依赖。
OSS_ENABLED = (not TESTING) and os.getenv("OSS_ENABLED") == "1"
OSS_ENDPOINT = os.getenv("OSS_ENDPOINT", "")            # 如 oss-cn-hangzhou.aliyuncs.com
OSS_BUCKET = os.getenv("OSS_BUCKET", "")
OSS_ACCESS_KEY_ID = os.getenv("OSS_ACCESS_KEY_ID", "")
OSS_ACCESS_KEY_SECRET = os.getenv("OSS_ACCESS_KEY_SECRET", "")
# 读图用的公开/CDN 基址（不配则不对外展示图片 URL）；绝不用带密钥的签名 URL 存库。
OSS_PUBLIC_BASE_URL = os.getenv("OSS_PUBLIC_BASE_URL", "")
OSS_KEY_PREFIX = os.getenv("OSS_KEY_PREFIX", "gifts/")
OSS_MAX_UPLOAD_BYTES = int(os.getenv("OSS_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))  # 10MB
OSS_UPLOAD_EXPIRE_SECONDS = int(os.getenv("OSS_UPLOAD_EXPIRE_SECONDS", "300"))        # 5min
