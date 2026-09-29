# 运营门户后端镜像（进阶部署：容器化，不改运行逻辑）
# 单阶段即可——纯 Python 无编译产物，多阶段反而增加维护成本。
FROM python:3.12-slim

# 固定 UID 便于宿主挂载卷授权；非 root 运行是硬要求。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app \
    PORTAL_HOST=0.0.0.0 \
    PORTAL_PORT=8000

WORKDIR /app

# 先只拷依赖清单，让 pip 安装层在源码变动时仍能命中缓存。
COPY portal/requirements.txt /app/portal/requirements.txt
RUN pip install --no-cache-dir -r /app/portal/requirements.txt

# 再拷后端代码（放在上一行之后，源码改动不会击穿 pip 缓存层）。
COPY portal/ /app/portal/

# 语法自检：构建期就能挡住手滑的语法错误，不必等容器起不来才发现。
RUN python -m compileall -q /app/portal/backend /app/portal/migrate.py

# 用非 root 用户运行，降低容器逃逸面。
RUN useradd --create-home --uid 10001 portal
USER portal

EXPOSE 8000

# 健康检查打 /api/health（无需鉴权，在白名单内）。
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"

# worker 数由 PORTAL_WORKERS 控制，默认 1（与进程内会话兼容）。
# 演进 B：会话外置到 Redis（SESSION_BACKEND=redis）后，compose 把 PORTAL_WORKERS 抬到核数，
# 才能横向并行、抬高单实例 CPU 天花板；未外置会话时盲加 worker 会让跨进程 token 401。
# exec 形式（shell CMD + exec）让 uvicorn 接管 PID 1 的信号，保证 docker stop 优雅退出。
ENV PORTAL_WORKERS=1
CMD exec python -m uvicorn portal.backend.main:app --host 0.0.0.0 --port 8000 --workers "${PORTAL_WORKERS}" --proxy-headers --forwarded-allow-ips "*"
