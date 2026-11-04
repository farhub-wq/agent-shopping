# shop-agent

**Harness 控制面 + 电商业务数据面的生产级参考实现。**

目标不是再写一个商城 Demo，而是把电商最难的三类问题——**库存一致性、跨服务订单状态、Agent 权限与可验证性**——放进同一套可运行的骨架，并按生产标准逐步加固：真实资金、不丢单、不超卖、可回滚、可观测。

当前仓库已经是**可运行的业务骨架**（下单、预占、状态机、Outbox、履约、售后、Agent 控制面）；生产化缺口与里程碑见[生产基线](#生产基线已冻结分阶段实施)，设计细节见 [system-design.md](docs/design/system-design.md)。

- **业务面**：`catalog` / `inventory` / `order` / `fulfillment` / `aftersales` 五个微服务，各自持有独立数据库，跨服务只走 HTTP 与事件。
- **控制面**：`harness` 负责技能准入、信任分级、工具网关、沙箱、三层记忆与证据账本；它**不是**全系统架构，只治理 Agent。
- **入口**：`gateway` 做鉴权、限流、BFF 聚合、审计与 SSE 透传；`web` 是 React 前台、运营后台与 Agent 对话。

三个问题的答案：不超卖靠原子预占 + 事务对账；订单不乱靠显式状态机 + 事务性 Outbox + 幂等消费；Agent 不越权靠准入、信任封顶、权限校验、人工审批、脱敏与可校验证据链。

---

## 架构

当前已实现的数据面与控制面：

```
浏览器（React 18 + Vite）
    │  /api/*（同源代理）
    ▼
gateway (8080) ── 鉴权 / 限流 / BFF 聚合 / 反向代理 / SSE / 审计
    │                    │
    ├── catalog(8001)    ├── harness(8006)
    ├── inventory(8002)  │     ├─ SKILL 准入 + 信任分级
    ├── order(8003)      │     ├─ 工具网关 + 人工审批
    ├── fulfillment(8004)│     ├─ 沙箱 + MCP + 三层记忆
    └── aftersales(8005) │     └─ 证据账本 TraceLedger
                         │
              Redis Streams（订单 / 履约 / 售后事件）
              PostgreSQL 16（每服务独立 DB）
```

生产目标拓扑：两台应用主机跑 Compose 栈，LB 终止 TLS，PostgreSQL/Redis 托管；新增 `identity`（账号）、`payment`（支付/退款/总账/对账）、`audit`（审计）、`notify`（通知）四个服务，且只暴露 gateway 与 web。

| 服务 | 端口 | 职责 | 数据所有权 | 状态 |
|---|---|---|---|---|
| gateway | 8080 | 鉴权、限流、BFF、代理、SSE、审计 | 无业务主数据 | 已实现 |
| catalog | 8001 | 商品、类目属性、违规词与合规检查 | catalog_db | 已实现 |
| inventory | 8002 | 库存、预占/提交/释放、秒杀 | inventory_db | 已实现 |
| order | 8003 | 订单状态机、支付、退款、Saga 补偿 | order_db | 已实现（支付为 mock） |
| fulfillment | 8004 | 履约单、SLA 超时扫描、发货/签收 | fulfillment_db | 已实现 |
| aftersales | 8005 | 售后单、政策引擎、风险评分、审批 | aftersales_db | 已实现 |
| harness | 8006 | 技能准入、工具权限、沙箱、MCP、记忆、Trace | harness_db | 已实现 |
| identity | 8007 | 注册、登录、refresh 轮换、角色授予 | identity_db | 规划（M1） |
| payment | 8008 | 支付单、退款、拒付、复式总账、对账 | payment_db | 规划（M2） |
| audit | 8009 | 长期审计、查询、导出 | audit_db | 规划（M3） |
| notify | 8010 | 发货/退款/审批通知 | notify_db | 规划（Should） |
| web | 5173 | 商城前台 / 订单售后 / 运营后台 / Agent 对话 | — | 已实现 |

技术栈：Python 3.11 · FastAPI · SQLAlchemy 2.0（async）· Pydantic v2 · PostgreSQL 16 · Redis 7（Lua + Streams）· React 18 · Vite · TypeScript · Docker Compose。

### 与常见 Demo 的差别

| 维度 | 常见商城 Demo / Agent 框架 | 本项目（当前 → 生产目标） |
|---|---|---|
| 库存一致性 | 先查再减，或依赖单库事务 | 当前：Lua 原子预占 + DB 落库；目标：DB 为最终真相 + Redis 闸门 + 预占表 + 定时对账，fail closed |
| 资金正确性 | 只存支付状态，靠渠道后台 | 目标：独立 payment 服务 + 内部复式总账 + 日终对账，渠道是结算事实、内部账是业务事实 |
| 订单生命周期 | 整单发货 / 整单退款 | 当前：整单；目标：部分发货、部分退款、退货退款，订单状态与履约解耦 |
| 服务边界 | 单服务单库，或跨库直连 | 五个业务服务各自持库，跨服务只走 HTTP / 事件，共享能力收敛到 `shop_common` |
| 事件可靠 | 事务提交后再发消息，可能丢或重 | 业务写入与 outbox 同事务；目标补齐消费组、`XAUTOCLAIM`、DLQ、重放与 relay 分布式锁 |
| Agent 权限 | 直接把工具列表交给模型 | 准入 → 信任封顶 → 权限 → 审批 → 脱敏 → 证据；不可逆动作只返回 `needs_approval` |
| 沙箱 | 本地 subprocess 或直接给宿主权限 | 目标：独立 runner，API 不持 Docker socket，无网络、固定 digest、不可用即失败，禁止本地降级 |
| 可验证性 | 只打印日志 | TraceLedger 落 `.jsonl` 证据卡并带 `sha256` 摘要，可回放、可核对 |

---

## 生产基线（已冻结，分阶段实施）

| 项目 | 冻结值 |
|---|---|
| 规模 | 下单峰值 30 TPS、浏览 300 QPS、SKU 5 万、日订单 3 万 |
| SLO | 单区域可用性 99.5%，下单 P95 <800ms |
| 部署 | 双应用主机 + Compose + LB，托管 PostgreSQL/Redis，不引入 K8s |
| 租户 / 仓 | 单租户（全表预留并强制过滤 `tenant_id`）；单仓（预留 `warehouse_id`） |
| 资金 | 聚合支付托管收银台（微信/支付宝），退款原路退回，拒付人工处理 |
| 库存 | DB 为真相，Redis 为原子闸门；允许极短少卖，绝不超卖；故障 fail closed |
| 订单 | 部分发货、部分退款、退货退款；换货/拒收放 Should |
| 事件 | Outbox 同事务 + Redis Streams 消费组 + DLQ + 重放 + relay 锁 |
| 安全 | PCI SAQ A、管理员 MFA、PII 字段加密、RBAC、审计 180 天 |
| 数据 | 订单/资金 5 年、日志 30 天、回调摘要 90 天、备份 30 天 |
| 灾备 | RPO ≤15 分钟、RTO ≤4 小时，季度恢复演练并对账 |
| Agent | 面向顾客只读 + 提议；不可逆动作人工审批；主备模型 + 熔断降级；PII 脱敏 |
| 沙箱 | 独立 runner，`SANDBOX_ENABLED=true`，失败即拒绝，禁止本地兜底 |

**里程碑**

| 阶段 | 内容 |
|---|---|
| M1 账号与权限 | identity 服务、注册/登录/refresh、网关改造、前端登录（见 [implementation_plan.md](implementation_plan.md)） |
| M2 资金与订单 | payment 服务、复式总账、回调幂等、部分发货/退款、对账、超时关单 |
| M3 生产加固 | 事件 DLQ/重放、audit、可观测与告警、沙箱 runner、双主机/托管 DB、Alembic、灾备 |
| M4 上线准入 | 五场景压测与混沌、灰度、1 元真实支付验证、三方签字、范围冻结 |

**明确不做（V1）**：促销/优惠券引擎、换货/拒收、多仓与调拨、快递 API 自动取号、电子发票 API、第三方风控、社交登录、多租户 SaaS/RLS、K8s、Kafka、多地域多活、自建支付/清结算、存储卡数据、完整等保/ISO 认证、7×24 值班。

---

## 快速开始

### Docker（推荐）

```bash
cp .env.example .env
docker compose up --build -d
```

| 入口 | 地址 |
|---|---|
| 前台 | http://localhost:5173 |
| 网关健康检查 | http://localhost:8080/healthz |
| Harness 健康检查 | http://localhost:8006/healthz |

首次启动会建库并播种：catalog 8 个 SKU、inventory 120 件库存、harness 3 个技能。未配置模型也能运行——`LLM_BASE_URL` 留空时使用内置确定性规划器（关键词路由 + 观测后汇总），适合演示与回归。

> 当前登录接口 `/api/auth/token` 是**本地开发用**，按请求体里的 `role` 签发 token。M1 完成后会替换为 identity 的注册/登录，生产模式禁用该接口。

### 本地验证（不依赖 Docker）

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"   # Windows
.venv/bin/python -m pip install -e ".[dev]"       # macOS / Linux

.venv/Scripts/python -m pytest -q
cd web && npm install && npm run build
```

常用命令：`make test` · `make lint` · `make up` · `make down` · `make logs`。

---

## 三个难点怎么解

| 难点 | 当前做法 | 生产目标 | 代码 |
|---|---|---|---|
| 超卖 | Redis + Lua 原子多 SKU 预占；DB 落库；失败回滚；超时预占回补 | DB 为最终真相 + 预占表 + 定时对账；Redis 故障时 fail closed | [scripts.py](services/inventory/scripts.py) |
| 订单不一致 | 显式状态机 + 事务性 Outbox + Streams 至少一次 + 幂等键 | 补部分发货/退款聚合、DLQ/重放、relay 锁 | [state_machine.py](services/order/state_machine.py) · [outbox.py](packages/shop_common/outbox.py) |
| Agent 越权 | 准入 → 信任封顶 → 工具权限 → 人工审批 → 脱敏 → 证据 | 独立沙箱 runner、主备模型熔断、PII 全链路脱敏 | [policy.py](services/harness/core/policy.py) · [tools.py](services/harness/core/tools.py) · [trace.py](services/harness/core/trace.py) |

一次下单的完整链路：

```
创建订单 → 预占库存(Redis Lua) → 支付回调 → 提交库存 → outbox 发 order.paid
        → fulfillment 建履约单 → 发货/签收事件 → order 状态推进
        → 售后建单 → 政策引擎 + 风险评分 → 人工审批 → 退款 → 状态闭环
```

关键约束：

- 状态变更只能走 `resolve_target`，服务层不直接赋值 `status`；
- 业务写入与事件入 `outbox_events` 在同一事务提交，`OutboxRelay` 投递失败自动重试；
- 消费者按 `event["type"]` 过滤，用 `shop:dedupe:*` 保证幂等；
- 库存扣减全部在 Lua 中完成，DB 负责落库；生产目标改为 DB 真相 + Redis 闸门；
- 资金写入必须借贷平衡，已入账分录不可修改（M2）。

---

## 已实现的功能

- **商城前台**：商品列表与详情、购物车、幂等下单、mock 支付、订单列表、售后申请、Agent 客服对话（SSE 流式）。
- **订单与履约**：显式订单状态机（支付 / 取消 / 退款 / 完成）、Saga 补偿、履约单生成、SLA 超时扫描、发货与签收回写状态。
- **库存与商品**：多 SKU 原子预占 / 提交 / 释放、超时预占回补、秒杀限购、类目属性校验与违规词合规检查。
- **售后**：政策引擎自动 / 人工分流、风险评分、审批与退款闭环、不可逆动作审批留痕。
- **运营后台**：订单 / 履约 / 售后操作入口、Agent 运行记录与证据查看、人工审批。
- **Harness 控制面**：技能六类准入、信任四档封顶、工具网关、Docker / 本地沙箱、MCP（HTTP / stdio）、三层记忆、离线确定性规划器、Trace 证据账本。
- **工程能力**：JWT 鉴权（开发接口）、限流、BFF 聚合、代理白名单、审计、事务性 Outbox、幂等键、pytest / ruff / Docker Compose 一键启动。

### 生产化缺口（按优先级）

1. **账号越权**：`/api/auth/token` 允许请求体自选角色，生产必须移除（M1）。
2. **资金**：支付是 mock，没有回调验签、总账、退款与对账（M2）。
3. **订单**：只有整单发货/退款，缺部分履约与部分退款聚合（M2）。
4. **事件**：relay 随进程启动，多实例会重复投递；缺 DLQ 与重放（M3）。
5. **运维**：单机 Compose、无指标/告警/备份演练/SLO（M3）。
6. **沙箱**：默认关闭，开启后 API 持有 Docker socket，本地兜底不是安全边界（M3）。

---

## Agent 控制面

- **技能**：`services/harness/skills/*/SKILL.md`，兼容 front matter。加载时做六类准入检查：`preprocessing-command`、`undeclared-tool`、`unlisted-script`、`path-escape`、`network-egress`、`dependency-risk`。
- **信任**：`community(T1) < reviewed(T2) < vendor(T3) < certified(T4)`。manifest 只能声明，不能提权；T1/T2 永远没有脚本执行权。
- **审批**：不可逆动作返回 `needs_approval`，落 Redis `shop:harness:pending:{run_id}`，只由运营人员通过 `/runs/{id}/approve` 重放。
- **MCP**：自研 JSON-RPC 2.0 协议层，支持 `initialize / ping / tools/list / tools/call`，HTTP 与 stdio 两种传输。
- **记忆**：工作记忆（Redis 列表）、情节记忆（哈希 n-gram 向量）、语义事实（正则抽取）；召回分 = `0.6*相似度 + 0.25*重要性 + 0.15*时间衰减`。
- **证据**：每次运行写 `.harness/traces/{run_id}.jsonl`，证据卡带 `sha256` 摘要，可回放、可核对。
- **生产边界**：面向顾客只读 + 提议；主模型 + 降级模型（15s 超时、3 次失败熔断 5 分钟、熔断后固定话术 + 转人工）；单会话工具/token 预算；PII 脱敏；沙箱独立 runner。

---

## 端到端验收（本地）

```bash
# 1. 登录（仅本地开发接口，M1 后移除）
curl -s -X POST localhost:8080/api/auth/token \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"demo-customer","role":"customer"}'

# 2. 浏览商品
curl -s localhost:8080/api/storefront/feed -H "Authorization: Bearer $TOKEN"

# 3. 下单（Idempotency-Key 必须唯一）
curl -s -X POST localhost:8080/api/storefront/checkout \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-001' \
  -d '{"items":[{"sku":"SKU-1001","qty":1}],"address":{"receiver":"张三","phone":"13800001111","province":"上海","city":"上海","detail":"浦东新区世纪大道 100 号"}}'

# 4. 支付（当前为 mock）
curl -s -X POST localhost:8080/api/storefront/orders/$ORDER_ID/pay \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"method":"mock"}'

# 5. 与 Agent 对话（SSE，加 ?stream=false 可拿 JSON）
curl -s -X POST localhost:8080/api/agents/customer-service/chat \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"message":"SKU-1001 还有库存吗？"}'

# 6. 查看本次运行的证据
curl -s localhost:8080/api/agents/runs/$RUN_ID/evidence -H "Authorization: Bearer $TOKEN"
```

完整验收（含履约、售后审批、角色权限）见 [docs/RUNBOOK.md](docs/RUNBOOK.md)。生产验收按设计文档第 11 节的五场景压测与灰度门槛执行。

---

## 目录

```
packages/shop_common/     配置、DB、鉴权、错误、事件总线、Outbox、幂等、HTTP 客户端
services/catalog/         商品、类目属性校验、违规词校验、合规检查
services/inventory/       库存、预占/提交/释放、秒杀、Lua 脚本
services/order/           订单状态机、支付、退款、Saga 补偿、事件消费
services/fulfillment/     履约单、SLA 超时扫描、发货/签收
services/aftersales/      售后单、政策引擎、风险评分、审批
services/gateway/         鉴权、限流、BFF、反向代理、SSE、审计
services/harness/         技能准入、策略、工具、沙箱、MCP、记忆、Runtime、Trace
services/identity/        账号与权限（规划，M1）
services/payment/         支付、退款、总账、对账（规划，M2）
services/audit/           长期审计（规划，M3）
web/                      React 18 + Vite：商城 / 订单售后 / 运营后台 / Agent
tests/                    状态机、售后政策、准入、策略、记忆、MCP、沙箱、事件过滤
docs/design/              生产基线系统设计
docs/requirements/        需求与验收标准
```

---

## 文档

- [system-design.md](docs/design/system-design.md)：生产基线设计（部署、数据模型、资金、事件、沙箱、可观测、灾备、里程碑）
- [requirements-shop-agent.md](docs/requirements/requirements-shop-agent.md)：需求、验收标准与差距
- [implementation_plan.md](implementation_plan.md)：M1 账号与权限实施计划
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：分层、数据所有权、事件契约、一致性设计
- [docs/PUBLIC-SOLUTION.md](docs/PUBLIC-SOLUTION.md)：如何把这套骨架抽象成通用电商解决方案
- [docs/RUNBOOK.md](docs/RUNBOOK.md)：启动、验收流程、故障排查

---

## 已知边界

1. 单镜像多入口（`SERVICE_NAME` 选择应用）是为了本地一键启动；生产按设计拆成独立镜像与独立部署单元。
2. 当前库存是“Redis 先扣、DB 后写”；生产目标改为 DB 真相 + Redis 闸门 + 预占表 + 对账，见设计文档第 4.4、5.2 节。
3. 当前支付是 mock，没有渠道回调、总账与对账；M2 接入真实聚合支付。
4. 当前只支持整单发货/退款；部分履约与部分退款在 M2 落地。
5. Outbox relay 随服务进程启动，多实例会重复投递；M3 补 relay 锁、DLQ 与重放。
6. 本地子进程沙箱不是安全边界；生产必须使用独立 runner，Docker 不可用时失败而不是本地降级。
7. 哈希 n-gram 向量是离线兜底，不是语义模型；接入真实 embedding 后应重建索引。
8. 证据数据来自本地运行记录，不等价于生产评测结论。
