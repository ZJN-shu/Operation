"""运行独立的临时 MySQL 实例：python -m portal.tests.run_mysql [测试模块...]

仅使用 PATH 或 PORTAL_TEST_MYSQLD 指定的 mysqld；不读取应用 .env / my.ini，
不连接现有服务。临时数据目录在工作区内，结束后关闭自身实例并清理。
"""
from __future__ import annotations

import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import pymysql

ROOT = Path(__file__).resolve().parents[2]


def main():
    executable = os.getenv("PORTAL_TEST_MYSQLD") or shutil.which("mysqld")
    if not executable:
        raise RuntimeError("未找到 mysqld，请设置 PORTAL_TEST_MYSQLD 为服务端可执行文件路径")
    executable = str(Path(executable).resolve())
    database = "ops_portal_test_" + secrets.token_hex(8)
    password = secrets.token_urlsafe(32)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix=".mysql-test-", dir=ROOT) as temp:
        # Windows MySQL 对非 ASCII 绝对路径兼容性有限，使用同一工作区目录的短路径。
        runtime_dir = temp
        if os.name == "nt":
            import ctypes
            buffer = ctypes.create_unicode_buffer(32768)
            if ctypes.windll.kernel32.GetShortPathNameW(temp, buffer, len(buffer)):
                runtime_dir = buffer.value
        common = [executable, "--no-defaults", "--datadir=./data", "--console",
                  "--mysqlx=0", "--innodb-buffer-pool-size=32M"]
        initialized = subprocess.run(common + ["--initialize-insecure"],
                                     cwd=runtime_dir, capture_output=True, timeout=120)
        if initialized.returncode:
            raise RuntimeError("临时 MySQL 初始化失败：" + initialized.stderr.decode(errors="replace"))
        server = subprocess.Popen(common + [f"--port={port}", "--bind-address=127.0.0.1",
                                            "--skip-log-bin", "--max-connections=40"],
                                  cwd=runtime_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        root = None
        try:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    output = server.communicate(timeout=2)[0].decode(errors="replace")
                    log = Path(temp) / "mysql.err"
                    if log.exists():
                        output += log.read_text(encoding="utf-8", errors="replace")
                    raise RuntimeError("临时 MySQL 启动退出；未执行数据库测试：" + output)
                try:
                    root = pymysql.connect(host="127.0.0.1", port=port, user="root",
                                           connect_timeout=1, autocommit=True)
                    break
                except pymysql.err.OperationalError:
                    time.sleep(0.2)
            if root is None:
                raise RuntimeError("临时 MySQL 启动超时")
            with root.cursor() as cur:
                cur.execute("ALTER USER 'root'@'localhost' IDENTIFIED BY %s", (secrets.token_urlsafe(32),))
                cur.execute(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
                cur.execute("CREATE USER 'portal_test'@'127.0.0.1' IDENTIFIED BY %s", (password,))
                # 转义授权库名中的下划线：MySQL 库级授权中的 _ 是通配符。
                grant_db = database.replace("_", r"\_")
                cur.execute(f"GRANT ALL PRIVILEGES ON `{grant_db}`.* TO 'portal_test'@'127.0.0.1'")
            env = dict(os.environ, PORTAL_TESTING="1", PORTAL_RUN_DB_TESTS="1",
                       PORTAL_TEST_MYSQL_HOST="127.0.0.1", PORTAL_TEST_MYSQL_PORT=str(port),
                       PORTAL_TEST_MYSQL_USER="portal_test", PORTAL_TEST_MYSQL_PASSWORD=password,
                       PORTAL_TEST_MYSQL_DB=database, PYTHONIOENCODING="utf-8")
            targets = sys.argv[1:] or ["discover", "-s", "portal/tests", "-v"]
            print(f"独立 MySQL：127.0.0.1:{port} / {database}；专用限权账号，未读取应用配置", flush=True)
            result = subprocess.run([sys.executable, "-m", "unittest", *targets],
                                    cwd=ROOT, env=env, timeout=300, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, encoding="utf-8")
            print(result.stdout, end="", flush=True)
            return result.returncode
        finally:
            if root is not None:
                try:
                    with root.cursor() as cur:
                        cur.execute("SHUTDOWN")
                except pymysql.Error:
                    pass
                root.close()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.terminate()
                server.wait(timeout=15)
            if server.stdout:
                server.stdout.close()


if __name__ == "__main__":
    sys.exit(main())
