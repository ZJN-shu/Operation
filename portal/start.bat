@echo off
chcp 65001 >nul
cd /d %~dp0

if not exist .env (
    echo [提示] 未找到 .env 文件，请先复制 .env.example 为 .env 并填写 MySQL 密码
    copy .env.example .env >nul
    echo 已自动生成 .env，请打开填写 MYSQL_PASSWORD 后重新运行本脚本
    pause
    exit /b
)

echo 正在启动 IT 运营门户... http://127.0.0.1:8000
python run.py
pause
