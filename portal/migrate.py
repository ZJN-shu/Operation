"""一次性数据库迁移入口（进阶：迁移与启动分离）。

容器部署时应用账号只给 DML 权限，结构变更（建库建表 / ALTER 回填）由本命令用
**迁移账号**单独跑一次：

    python -m portal.migrate

应用进程则以 RUN_MIGRATIONS=0 启动，只做一条轻量 SELECT 确认可达，不再自带 DDL
副作用（见 backend/main.py 的 lifespan）。这样多个副本并行拉起不会各自抢着改表结构。

本地开发直接 `python run.py` 仍走启动即迁移的旧路径（RUN_MIGRATIONS 默认 1），
不需要单独跑本模块。
"""
from __future__ import annotations

import sys

# migrate 是运维命令而非测试，绝不允许连到隔离测试判定分支里去。
if __name__ == "__main__":
    from portal.backend import db

    db.init_db()
    print("数据库迁移完成", flush=True)
    sys.exit(0)
