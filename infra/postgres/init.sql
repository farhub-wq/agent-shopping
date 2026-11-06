-- 每个微服务独立数据库，避免共享表导致的隐式耦合。
CREATE DATABASE catalog_db;
CREATE DATABASE inventory_db;
CREATE DATABASE order_db;
CREATE DATABASE fulfillment_db;
CREATE DATABASE aftersales_db;
CREATE DATABASE harness_db;
CREATE DATABASE identity_db;
CREATE DATABASE payment_db;

\connect catalog_db
CREATE EXTENSION IF NOT EXISTS pgcrypto;

\connect inventory_db
CREATE EXTENSION IF NOT EXISTS pgcrypto;

\connect order_db
CREATE EXTENSION IF NOT EXISTS pgcrypto;

\connect fulfillment_db
CREATE EXTENSION IF NOT EXISTS pgcrypto;

\connect aftersales_db
CREATE EXTENSION IF NOT EXISTS pgcrypto;

\connect harness_db
CREATE EXTENSION IF NOT EXISTS pgcrypto;

\connect identity_db
CREATE EXTENSION IF NOT EXISTS pgcrypto;

\connect payment_db
CREATE EXTENSION IF NOT EXISTS pgcrypto;
