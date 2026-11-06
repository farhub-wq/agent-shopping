-- 每个微服务独立数据库，避免共享表导致的隐式耦合。
-- 字符集统一 utf8mb4 + 0900_ai_ci：中文、emoji 都能存，排序规则与 MySQL 8 默认一致。
CREATE DATABASE IF NOT EXISTS catalog_db CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE IF NOT EXISTS inventory_db CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE IF NOT EXISTS order_db CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE IF NOT EXISTS fulfillment_db CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE IF NOT EXISTS aftersales_db CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE IF NOT EXISTS identity_db CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE IF NOT EXISTS payment_db CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE IF NOT EXISTS audit_db CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;

-- 应用账号名与 compose 的 MYSQL_USER 默认值保持一致；
-- 若覆盖 MYSQL_USER，需要同步修改这里的账号名，否则服务连库会被拒绝。
GRANT ALL PRIVILEGES ON catalog_db.* TO 'shop'@'%';
GRANT ALL PRIVILEGES ON inventory_db.* TO 'shop'@'%';
GRANT ALL PRIVILEGES ON order_db.* TO 'shop'@'%';
GRANT ALL PRIVILEGES ON fulfillment_db.* TO 'shop'@'%';
GRANT ALL PRIVILEGES ON aftersales_db.* TO 'shop'@'%';
GRANT ALL PRIVILEGES ON identity_db.* TO 'shop'@'%';
GRANT ALL PRIVILEGES ON payment_db.* TO 'shop'@'%';
GRANT ALL PRIVILEGES ON audit_db.* TO 'shop'@'%';
FLUSH PRIVILEGES;
