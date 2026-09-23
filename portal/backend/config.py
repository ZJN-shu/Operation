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
