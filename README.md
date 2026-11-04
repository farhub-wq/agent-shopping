# shop-agent

一个把 **电商业务数据面** 与 **Agent 控制面** 放在同一套可运行骨架里的参考实现。

它不追求再写一个商城 Demo，而是集中回答三个工程问题：

1. **库存一致性**：如何在高并发下不超卖，且失败可回滚；
2. **跨服务订单状态**：如何让订单、履约、售后在事件驱动下不丢状态、不重复推进；
3. **Agent 的权限与可验证性**：如何让 Agent 只能做被授权的事，并且每一步都能被审计和回放。

---

## 架构

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

| 服务 | 端口 | 职责 | 数据所有权 |
|---|---|---|---|
| gateway | 8080 | 鉴权、限流、BFF、代理、SSE、审计 | 无业务主数据 |
| catalog | 8001 | 商品、类目属性、违规词与合规检查 | catalog_db |
| inventory | 8002 | 库存、预占/提交/释放、秒杀 | inventory_db |
| order | 8003 | 订单状态机、支付、退款、Saga 补偿 | order_db |
| fulfillment | 8004 | 履约单、SLA 超时扫描、发货/签收 | fulfillment_db |
| aftersales | 8005 | 售后单、政策引擎、风险评分、审批 | aftersales_db |
| harness | 8006 | 技能准入、工具权限、沙箱、MCP、记忆、Trace | harness_db |
| web | 5173 | 商城前台 / 订单售后 / 运营后台 / Agent 对话 | — |

技术栈：Python 3.11 · FastAPI · SQLAlchemy 2.0（async）· Pydantic v2 · PostgreSQL 16 · Redis 7（Lua + Streams）· React 18 · Vite · TypeScript · Docker Compose。

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

| 难点 | 做法 | 代码 |
|---|---|---|
| 超卖 | Redis + Lua 原子多 SKU 预占；DB 落库兜底；失败回滚；30s 超时预占自动回补 | [scripts.py](services/inventory/scripts.py) |
| 订单状态不一致 | 显式状态机集中校验 + 事务性 Outbox + Redis Streams 至少一次投递 + 幂等键 | [state_machine.py](services/order/state_machine.py) · [outbox.py](packages/shop_common/outbox.py) |
| Agent 越权与不可验证 | SKILL 准入 → 信任等级封顶 → 工具权限 → 人工审批 → DLP 脱敏 → 证据卡 | [policy.py](services/harness/core/policy.py) · [tools.py](services/harness/core/tools.py) · [trace.py](services/harness/core/trace.py) |

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
- 库存扣减全部在 Lua 中完成，DB 只负责落库与对账。

---

## Agent 控制面

- **技能**：`services/harness/skills/*/SKILL.md`，兼容 front matter。加载时做六类准入检查：`preprocessing-command`、`undeclared-tool`、`unlisted-script`、`path-escape`、`network-egress`、`dependency-risk`。
- **信任**：`community(T1) < reviewed(T2) < vendor(T3) < certified(T4)`。manifest 只能声明，不能提权；T1/T2 永远没有脚本执行权。
- **审批**：不可逆动作返回 `needs_approval`，落 Redis `shop:harness:pending:{run_id}`，只由运营人员通过 `/runs/{id}/approve` 重放。
- **MCP**：自研 JSON-RPC 2.0 协议层，支持 `initialize / ping / tools/list / tools/call`，HTTP 与 stdio 两种传输。
- **记忆**：工作记忆（Redis 列表）、情节记忆（哈希 n-gram 向量）、语义事实（正则抽取）；召回分 = `0.6*相似度 + 0.25*重要性 + 0.15*时间衰减`。
- **证据**：每次运行写 `.harness/traces/{run_id}.jsonl`，证据卡带 `sha256` 摘要，可回放、可核对。

---

## 端到端验收

```bash
# 1. 登录
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

# 4. 支付
curl -s -X POST localhost:8080/api/storefront/orders/$ORDER_ID/pay \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"method":"mock"}'

# 5. 与 Agent 对话（SSE，加 ?stream=false 可拿 JSON）
curl -s -X POST localhost:8080/api/agents/customer-service/chat \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"message":"SKU-1001 还有库存吗？"}'

# 6. 查看本次运行的证据
curl -s localhost:8080/api/agents/runs/$RUN_ID/evidence -H "Authorization: Bearer $TOKEN"
```

完整验收（含履约、售后审批、角色权限）见 [docs/RUNBOOK.md](docs/RUNBOOK.md)。

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
web/                      React 18 + Vite：商城 / 订单售后 / 运营后台 / Agent
tests/                    状态机、售后政策、准入、策略、记忆、MCP、沙箱、事件过滤
```

---

## 文档

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)：分层、数据所有权、事件契约、一致性设计
- [docs/PUBLIC-SOLUTION.md](docs/PUBLIC-SOLUTION.md)：如何把这套骨架抽象成通用电商解决方案
- [docs/RUNBOOK.md](docs/RUNBOOK.md)：启动、验收流程、故障排查

---

## 已知边界

1. 单镜像多入口（`SERVICE_NAME` 选择应用）是为了本地一键启动；生产应拆成独立镜像与独立部署单元。
2. Redis Streams 适合垂直切片与中小流量；需要跨机房/长期回放时替换 Kafka，只需保持 `publish/poll` 契约。
3. 本地子进程沙箱不是安全边界；Docker 模式才具备 `network=none`、只读 FS、资源上限与禁止提权。
4. 哈希 n-gram 向量是离线兜底，不是语义模型；接入真实 embedding 后应重建索引。
5. 证据数据来自本地运行记录，不等价于生产评测结论。
