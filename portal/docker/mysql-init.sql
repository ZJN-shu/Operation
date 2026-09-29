-- 首次建库后由 MySQL 官方镜像的 docker-entrypoint-initdb.d 自动执行一次。
-- 目的：创建一个只握 DML 权限的运行账号 portal_app，与迁移账号（root）分离。
-- 应用容器以该账号 + RUN_MIGRATIONS=0 启动，即便被拖库也无法 ALTER/DROP/CREATE。
-- 注意：.sql 初始化脚本不做 compose 变量替换，这里的口令是固定开发值，
--       必须与 docker-compose.yml 中 app 服务的 APP_DB_PASSWORD 默认值一致；
--       上生产前请三处（此处、compose 环境变量、密钥管理）一起换成真实强口令。
CREATE USER IF NOT EXISTS 'portal_app'@'%' IDENTIFIED BY 'app-pw-change-me';

-- 只给业务读写，不给 DDL；GRANT 里显式列权限，避免 ALL PRIVILEGES 带上的
-- CREATE / ALTER / DROP / INDEX 悄悄放行结构变更。
GRANT SELECT, INSERT, UPDATE, DELETE ON `ops_portal`.* TO 'portal_app'@'%';

FLUSH PRIVILEGES;
