# 运行手册

## 1. 环境要求

| 组件 | 版本 | 用途 |
|---|---|---|
| Docker + Compose | 24+ / v2+ | 一键起 PostgreSQL、Redis、七个服务与前端 |
| Python | 3.11+ | 本地单测与离线冒烟 |
| Node.js | 20+ | 前端构建 |

## 2. 启动

```bash
cp .env.example .env
docker compose up --build -d
docker compose ps
```

入口：

- 前台：<http://localhost:5173>
- 网关健康：<http://localhost:8080/healthz>
- Harness 健康：<http://localhost:8006/healthz>

只构建不启动时校验配置：

```bash
docker compose config --quiet
```

### 重建与清理

```bash
docker compose up --build -d catalog inventory        # 只重建部分服务
docker compose logs -f --tail=100 order               # 看日志
docker compose down -v                                # 连数据库卷一起清掉
```

`down -v` 会删除 `pgdata`，属于破坏性操作，执行前确认没有需要保留的数据。

## 3. 本地验证（不依赖 Docker）

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest -q
```

当前结果：`34 passed`。

只启动不依赖 Redis 的服务做冒烟：

```powershell
$env:SERVICE_NAME='catalog'; $env:DATABASE_URL='sqlite+aiosqlite:///./data/catalog.db'
.\.venv\Scripts\python.exe -m uvicorn catalog.app:app --port 8011
# GET http://127.0.0.1:8011/healthz
# GET http://127.0.0.1:8011/products
```

```powershell
$env:SERVICE_NAME='harness'; $env:DATABASE_URL='sqlite+aiosqlite:///./data/harness.db'
$env:SANDBOX_ENABLED='false'
.\.venv\Scripts\python.exe -m uvicorn harness.app:app --port 8016
# GET  /healthz   → skills=3 tools=13
# GET  /skills    → 3 个技能的准入结果与信任等级
# POST /sandbox/run {"code":"print(6*7)"}（需 X-User-Role: operator）
# POST /mcp       → JSON-RPC initialize
```

`inventory` 启动时必须连 Redis（播种热库存），因此不能脱离 Redis 单独启动。

## 4. 端到端验收流程

以下命令假设网关在 `http://localhost:8080`。

### 4.1 注册与登录

```bash
# 注册（默认授予 customer 角色；运营/管理员账号由 bootstrap 环境变量或后续管理接口发放）
curl -s -X POST localhost:8080/api/auth/register \
  -H 'Content-Type: application/json' \
  -d '{"phone":"13800000001","password":"Passw0rd!"}'

# 登录：返回 access_token，并把 refresh/CSRF Cookie 存到 cookies.txt
curl -s -c cookies.txt -X POST localhost:8080/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"phone":"13800000001","password":"Passw0rd!"}'

# 会话续期（access token 15 分钟过期后）：从 cookies.txt 里取 shop_csrf 的值填入 X-CSRF-Token
curl -s -b cookies.txt -c cookies.txt -X POST localhost:8080/api/auth/refresh \
  -H 'X-CSRF-Token: <shop_csrf 值>'

# 登出：撤销服务端 refresh 令牌
curl -s -b cookies.txt -X POST localhost:8080/api/auth/logout \
  -H 'X-CSRF-Token: <shop_csrf 值>'
```

保存登录返回的 `access_token`，后续用 `Authorization: Bearer $TOKEN`。运营/管理员账号：空库启动时设置 `BOOTSTRAP_ADMIN_PHONE` / `BOOTSTRAP_ADMIN_PASSWORD` 环境变量可引导第一个 admin；登录后按返回的 `role` 判断是否可进运营后台。

### 4.2 浏览与下单

```bash
curl -s localhost:8080/api/storefront/feed -H "Authorization: Bearer $TOKEN"
```

记下一个 `sku`，然后下单（`Idempotency-Key` 必须唯一）：

```bash
curl -s -X POST localhost:8080/api/storefront/checkout \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-001' \
  -d '{"items":[{"sku":"SKU-1001","qty":1}],"address":{"receiver":"张三","phone":"13800001111","province":"上海","city":"上海","detail":"浦东新区世纪大道 100 号"}}'
```

验证点：
- 库存不足返回 `409`，错误码不是泛化的 `INTERNAL`。
- 用同一个幂等键重复提交，返回同一订单，不重复扣库存。

### 4.3 支付与履约

```bash
curl -s -X POST localhost:8080/api/storefront/orders/$ORDER_ID/pay \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"method":"mock"}'
```

运营侧：

```bash
curl -s localhost:8080/api/fulfillment/shipments -H "Authorization: Bearer $OPS_TOKEN"
curl -s -X POST localhost:8080/api/fulfillment/shipments/$SHIPMENT_ID/ship \
  -H "Authorization: Bearer $OPS_TOKEN" -H 'Content-Type: application/json' -d '{"carrier":"SF"}'
curl -s -X POST localhost:8080/api/fulfillment/shipments/$SHIPMENT_ID/deliver \
  -H "Authorization: Bearer $OPS_TOKEN"
```

验证点：订单状态按 `PAID → SHIPPED → COMPLETED` 推进，`/api/orders/$ORDER_ID/timeline` 可见轨迹。

### 4.4 售后

```bash
# 用户申请
curl -s -X POST localhost:8080/api/aftersales/tickets \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"order_id":"'$ORDER_ID'","type":"refund_only","reason":"商品破损"}'

# 运营审批（body 里的 note 会写入 decision_note）
curl -s -X POST localhost:8080/api/aftersales/tickets/$TICKET_ID/approve \
  -H "Authorization: Bearer $OPS_TOKEN" -H 'Content-Type: application/json' -d '{"note":"人工审核通过"}'

# 退款完成
curl -s -X POST localhost:8080/api/aftersales/tickets/$TICKET_ID/refund-complete \
  -H "Authorization: Bearer $OPS_TOKEN"
```

小额低风险工单会被政策引擎 `auto_approve`；大额、高频、欺诈词会升级 `manual`。退款完成后订单进入 `REFUNDED`。

### 4.5 Agent 与证据

```bash
curl -s localhost:8080/api/agents -H "Authorization: Bearer $TOKEN"
curl -s -X POST localhost:8080/api/agents/customer-service/chat \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"message":"SKU-1001 还有库存吗？"}'
# SSE 流；加 ?stream=false 可直接拿 JSON
```

运行记录：

```bash
curl -s localhost:8080/api/agents/runs/$RUN_ID/evidence -H "Authorization: Bearer $TOKEN"
```

验证点：`tool_calls` 数量、`denied_calls`、trace 事件类型完整；不可逆动作应返回 `pending_approval` 而不是执行。

## 5. 沙箱

默认 `SANDBOX_ENABLED=false`，走本地子进程（仅防误操作）。启用 Docker 沙箱：

```bash
docker build -t shop-agent-sandbox:latest infra/sandbox
# .env 中设置 SANDBOX_ENABLED=true 后重启 harness
```

`POST /api/agents/sandbox/run` 仅供 staff，执行受 `SANDBOX_TIMEOUT_SECONDS`、CPU、内存、PID 与 `network=none` 限制。

## 6. 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| inventory 启动即退出 | Redis 不可达（启动要播种热库存） | `docker compose ps redis`，检查 `REDIS_URL` |
| 下单 401 | 未带 token 或 `JWT_SECRET` 不一致 | 重新登录取 token，确认所有服务同一 secret |
| 登录返回 429 | 同一手机号+IP 连续失败 5 次，锁定 15 分钟 | 等锁定期满，或清 `login_attempts` 表对应行 |
| 刷新会话返回 403 | 缺 `X-CSRF-Token` 或与 `shop_csrf` Cookie 不一致 | 从 Cookie 取值回填请求头 |
| 刷新会话返回 401 | refresh Cookie 缺失/过期/已被轮换撤销 | 重新登录；确认请求走的是网关 `/api/auth/*` 路径 |
| 业务接口 401 但 Cookie 还在 | access token 只有 15 分钟 | 前端会自动调一次 `/auth/refresh` 重放；仍失败则重新登录 |
| 下游服务把用户当访客 | 身份头未签名或服务 `TRUST_IDENTITY_HEADERS=false` | 确认所有服务同一 `INTERNAL_SIGNING_SECRET` 且开关为 true |
| 下单 409 `CONFLICT` | 库存不足 / 幂等键复用 | 查 `/api/inventory/stocks`；换幂等键 |
| 订单卡在 `PAYING` | 支付回调失败 | 查 order 日志与 outbox `last_error` |
| 履约单不出现 | fulfillment 未消费 `order.paid` | 查消费者组 `shop:events:order` 与 `shop:dedupe:*` |
| 售后审批 403 | 用了 customer 账号 | 改用 operator/admin 账号（bootstrap 引导或运营发放） |
| Agent 对话无输出 | SSE 被代理缓冲 | 确认 nginx `proxy_buffering off`，网关是流式路径 |
| 沙箱返回 `unavailable` | 镜像不存在或 Docker 不可达 | 构建 `infra/sandbox`，或接受本地降级 |
| 记忆检索结果差 | 无模型时用哈希 n-gram | 属预期；接真实 embedding 后重建索引 |

## 7. 当前验证状态

已执行：

- `pytest -q` → **49 passed**
- `ruff check packages services tests` → **All checks passed**
- `python -m compileall` → **通过**
- `cd web && npm install && npm run build` → **构建成功**
- `docker compose -f docker-compose.yml -f docker-compose.prod.yml config` → **通过**（生产配置仅 gateway:8080 / web:5173 对外）

未执行：

- `docker compose up` 端到端（当前环境 Docker daemon 未运行，且启动守护进程属于需用户确认的系统操作）
