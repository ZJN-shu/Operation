"""共享 Redis 客户端（演进 B：会话外置到 Redis，解锁多 worker）。

守两条边界：
  1. 惰性导入：只有真正配了 SESSION_BACKEND=redis 才 import redis / 建连接，
     测试与单实例默认路径（process 后端）根本不碰这个包，零新依赖副作用。
  2. 单连接复用：redis-py 客户端自带连接池，进程内一个懒单例即可；decode_responses
     让命令直接回 str，省得每处 decode。多 worker 下每个进程各连一份，指向同一个 Redis。
"""
from __future__ import annotations

import threading

from . import config

_lock = threading.Lock()
_client = None


def client():
    """返回进程内共享的 Redis 客户端（首次调用按 REDIS_URL 建连）。"""
    global _client
    if _client is None:
        with _lock:
            if _client is None:
                import redis  # 惰性：仅 redis 后端才需要该包
                _client = redis.Redis.from_url(
                    config.REDIS_URL, decode_responses=True,
                    socket_timeout=5, socket_connect_timeout=5, health_check_interval=0)
    return _client
