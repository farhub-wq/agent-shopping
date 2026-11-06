# 实施计划：引入 Alembic 迁移

## 1. 背景与目标

当前建表依赖 `shop_common.db.init_models()` 里的 `Base.metadata.create_all`，它**只补建缺失的表，不修改已有表结构**（`db.py:101`）。这导致 commit `1d3bfd8`（移除订单侧 `payment_status`）删了模型字段却没做 DDL 迁移，有数据的库升级后下单直接报 `1364 Field 'payment_status' doesn't have a default value`（本次本地验证已复现并手动 `DROP COLUMN` 兜底）。

目标：引入 Alembic，让 schema 变更可版本化、可追踪，容器启动时自动应用迁移，从根上消除「删列/改列无迁移」这类问题。

## 2. 现状事实（方案依据）

- 有数据库的服务共 7 个：`catalog`、`inventory`、`order`、`fulfillment`、`aftersales`、`identity`、`payment`。`gateway`（纯 BFF）、`harness`（Mongo）无关系库，跳过。
- 所有 ORM 模型共享同一个 `Base`（`packages/shop_common/db.py`）。每个服务的库 = 本服务表 + 4 张共享表（`outbox_events`、`processed_events`、`dead_events`、`task_runs`）。
- 每个服务一个进程一个库，URL 由 `DATABASE_URL` 环境变量注入（compose 中为 `mysql+aiomysql://...`），本地缺省退化为 `sqlite+aiosqlite:///./data/{service_name}.db`（`config.py:81`）。
- 测试用 SQLite + `init_models()`（`create_all`）建表，`tests/` 大量依赖此路径，**本次不动测试建表方式**。
- 项目未上线、无历史数据（此前设计已定「行级 DDL 直接重建」），因此**只需要一个基线迁移 = 当前 schema**，无需回填完整历史迁移。

## 3. 方案总览

每个有库服务一个独立 Alembic 迁移目录（独立版本序列，符合「服务只拥有自己的库」边界），共享一份迁移运行逻辑；容器启动时先 `alembic upgrade head` 再起 uvicorn；测试继续走 `create_all` 不受影响。

```
migrations/
  _common.py                    # 共享逻辑：async 引擎 + 动态 import 服务 models + run_migrations
  catalog/
    alembic.ini
    env.py                      # 薄封装：from migrations._common import run_migrations; run_migrations()
    versions/0001_baseline.py   # 基线 = 当前 schema
  inventory/  ...               # 同上
  order/      ...
  fulfillment/...
  aftersales/ ...
  identity/   ...
  payment/    ...
```

## 4. 改动文件树

```
pyproject.toml                                  # [project].dependencies 增加 alembic>=1.13.0
migrations/_common.py                           # 新增：共享迁移运行逻辑
migrations/<svc>/alembic.ini                    # 新增 x7：script_location 指向本服务 versions
migrations/<svc>/env.py                         # 新增 x7：薄封装
migrations/<svc>/versions/0001_baseline.py      # 新增 x7：基线迁移（autogenerate 生成 + 人工审查）
infra/Dockerfile                                # 修改：CMD 改为「有迁移目录则 upgrade head，再 uvicorn」
docs/                                          # 记录「新增服务需建迁移目录」约定（写进 AGENTS.md 或 RUNBOOK）
```

不改动：`packages/shop_common/db.py` 的 `init_models`（测试仍依赖）、`tests/` 全部用例、各服务 `models.py`、`app.py` 业务逻辑。

## 5. 关键技术点

1. **异步迁移**：项目 SQLAlchemy 是 async（`create_async_engine` + `aiomysql`）。Alembic 采用 async 模板做法——`create_async_engine(settings.resolved_database_url)` + `connection.run_sync(do_run_migrations)`，复用现有 `aiomysql`/`aiosqlite`，不新增同步驱动。

2. **动态定位服务**：`_common.py` 读 `settings.service_name`（compose 已注入 `SERVICE_NAME`），`importlib.import_module(f"{svc}.models")` 把该服务模型注册进 `Base.metadata`，`target_metadata = Base.metadata`。这样 7 份 `env.py` 内容一致，无复制粘贴漂移。

3. **Dockerfile 单镜像多入口**：`SERVICE_NAME` 决定服务。CMD 改为
   ```sh
   if [ -d "migrations/${SERVICE_NAME}" ]; then alembic -c migrations/${SERVICE_NAME}/alembic.ini upgrade head; fi && uvicorn ${SERVICE_NAME}.app:app --host 0.0.0.0 --port 8000
   ```
   gateway/harness 无迁移目录，条件判断直接跳过。

4. **create_all 去留**：`init_models` 保留不动。容器里 alembic 先建表，`create_all` 幂等 no-op；测试继续 `create_all`。不引入「生产关闭 create_all」的开关，避免过度设计。

5. **基线迁移**：对每个服务在**空库**上 `alembic revision --autogenerate -m baseline` 生成 `0001_baseline`（对比 metadata vs 空库 → 全量 `create_table`），`down_revision=None`。生成后人工审查，确保与现有表结构一致。

6. **已有本地库收口**：本地 7 个库已被 `create_all` 建好（schema 与 baseline 基本一致，仅 inventory 有行级化前遗留的孤儿表 `reservations`）。对每个库执行 `alembic -c migrations/<svc>/alembic.ini stamp 0001` 标记基线已应用（**不能 stamp head**：inventory 的 0002 是清孤儿表，stamp head 会跳过它），随后 `upgrade head` 对 inventory 执行 0002 清理孤儿表、对其余服务幂等无操作。

## 6. 验证方式

1. `pytest`：全绿（测试路径未动，应 87 passed）。
2. `ruff check packages services tests`：全绿（migrations 不在 lint 范围）。
3. 全新库验证：`docker compose down -v` 后 `up --build`，确认 7 个服务 `alembic upgrade head` 建表成功、healthz 正常、端到端冒烟（下单/支付/发货/入库/盘点）通过。
4. 存量库验证（可选）：保留现有 volume，直接 `up --build`，确认 `upgrade head` 对已 stamp 的库幂等、不报错。
5. 未来变更演练：给某服务加一列 + 生成新迁移 + `upgrade head`，确认自动应用（作为文档示例，不留在代码里）。

## 7. 风险与边界

- **autogenerate 基线需人工审查**：跨 MySQL 类型（`UTCDateTime`→`DATETIME(6)`、JSON）与 `create_all` 实际产出的 DDL 可能有细微差异，生成后逐服务核对表/索引/唯一约束。
- **孤儿表已清理**：inventory 库遗留的旧 `reservations` 表由 `0002_drop_legacy_reservations` 迁移条件删除（`sa.inspect` 判断存在才 DROP，兼容全新库），随 `upgrade head` 自动执行，无需手工 DROP。
- **Dockerfile 改动影响所有服务**：CMD 变更对 gateway/harness 也生效，靠目录判断跳过，需在重建后确认这两个服务不受影响。
- **新增服务的约定**：后续新增有库服务时，需同步建 `migrations/<svc>/` 并生成基线，此约定写进 AGENTS.md 第 4 节目录说明。

## 8. 提交单元

1. `feat(common): 引入 Alembic 迁移框架与 7 服务基线`
   - pyproject.toml、migrations/ 全部、infra/Dockerfile、AGENTS.md 约定说明。
   - 一个提交完成（框架 + 基线不可拆分验证），pytest/ruff/web build 全绿后提交。
