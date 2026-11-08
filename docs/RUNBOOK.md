# 运行手册

## 1. 环境要求

| 组件 | 版本 | 用途 |
|---|---|---|
| Docker + Compose | 24+ / v2+ | 一键起 MySQL、MongoDB、Redis、八个业务服务与前端 |
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

`down -v` 会删除 `mysqldata` 与 `mongodata`，属于破坏性操作，执行前确认没有需要保留的数据。

已有 `mysqldata` 卷时 `infra/mysql/init.sql` 不会重跑；新增库或补授权需要手动执行（`payment_db` 已写进 init.sql，老卷升级时按下面补齐）：

```bash
docker compose exec mysql mysql -uroot -p -e \
  'CREATE DATABASE IF NOT EXISTS payment_db CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;'
docker compose exec mysql mysql -uroot -p -e \
  "GRANT ALL PRIVILEGES ON payment_db.* TO 'shop'@'%';"
```

MongoDB 的集合与索引由 `infra/mongo/init.js` 建好；服务首次写入时也会幂等补建，不需要迁移脚本。

## 3. 本地验证（不依赖 Docker）

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
.venv/Scripts/python -m pytest -q
```

当前结果：`68 passed`。

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
# 下单响应里会带 payment_id / pay_url / payment_expires_at；mock 收银台确认支付
curl -s -X POST localhost:8080/api/storefront/payments/$PAYMENT_ID/pay \
  -H "Authorization: Bearer $TOKEN"
```

运营侧：

```bash
curl -s localhost:8080/api/fulfillment/shipments -H "Authorization: Bearer $OPS_TOKEN"
# 整单发货：不带 items
curl -s -X POST localhost:8080/api/fulfillment/shipments/$SHIPMENT_ID/ship \
  -H "Authorization: Bearer $OPS_TOKEN" -H 'Content-Type: application/json' -d '{"carrier":"SF"}'
# 部分发货：只发 item-a 1 件，剩余数量自动拆到新的 pending 履约单
curl -s -X POST localhost:8080/api/fulfillment/shipments/$SHIPMENT_ID/ship \
  -H "Authorization: Bearer $OPS_TOKEN" -H 'Content-Type: application/json' \
  -d '{"carrier":"SF","items":[{"order_item_id":"'"$ITEM_ID"'","qty":1}]}'
curl -s -X POST localhost:8080/api/fulfillment/shipments/$SHIPMENT_ID/deliver \
  -H "Authorization: Bearer $OPS_TOKEN"
```

验证点：部分发货时订单保持 `PAID`；全部行发完才 `PAID → SHIPPED`；所有履约单签收后才 `→ COMPLETED`，`/api/orders/$ORDER_ID/timeline` 可见轨迹。

### 4.4 售后

```bash
# 用户申请（整单退款金额必须等于剩余可退额；部分退款请传 order_item_id + quantity）
curl -s -X POST localhost:8080/api/aftersales/tickets \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"order_id":"'$ORDER_ID'","type":"refund_only","reason":"商品破损"}'

# 运营审批（通过后由 payment 服务同步出款，工单状态随 payment.refunded 事件回填）
curl -s -X POST localhost:8080/api/aftersales/tickets/$TICKET_ID/approve \
  -H "Authorization: Bearer $OPS_TOKEN" -H 'Content-Type: application/json' -d '{"note":"人工审核通过"}'

# 日终对账（默认昨天，可传 biz_date）；差异只记录，不自动改资金
curl -s -X POST "localhost:8080/api/payments/reconciliation?biz_date=2026-11-05" \
  -H "Authorization: Bearer $OPS_TOKEN"
```

小额低风险工单会被政策引擎 `auto_approve`；大额、高频、欺诈词会升级 `manual`。全额退款完成后订单进入 `REFUNDED`，部分退款回退到退款前状态，订单行保留已退数量与金额。

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

沙箱执行由独立 `sandbox-runner` 承载：只有它挂载 Docker socket 并创建一次性容器，harness 只经内网 HTTP（携带签名身份头）调用。runner 不映射宿主端口、不经 gateway 暴露，任何把它暴露到公网的改动都视为配置事故。

**生产必须是 rootless dockerd**：runner 被攻破时若连的是默认 `/var/run/docker.sock`（root 守护进程），攻击者等于拿到宿主 root，容器加固全部失效。因此生产 overlay 把 rootless socket 挂到容器内独立路径，并由 runner 启动守卫强制校验：

- `SANDBOX_DOCKER_HOST` 生产必填，且不允许是默认 root socket，否则 runner 拒绝启动；
- `SANDBOX_RUNTIME` 生产强制 `runsc`（gVisor），共享宿主内核的 runc 被启动守卫拒绝；
- `/healthz` 在不可达、隔离退化或 runsc 未注册时返回 **HTTP 503**，并暴露 `docker.rootless` / `docker.insecure` / `docker.runtime_ok` 供定位。

宿主侧初始化已脚本化（Debian/Ubuntu，root 执行，幂等）：

```bash
sudo make setup-sandbox-host
# 等价于：sudo bash infra/rootless-docker/setup.sh
```

脚本会创建专用用户、开启 linger、安装 rootless dockerd 与 gVisor（runsc）、把 runsc 写入该用户的 `daemon.json`，最后打印：

```
SANDBOX_ROOTLESS_SOCKET=/run/user/<uid>/docker.sock
SANDBOX_RUNTIME=runsc
```

把这两行写入部署机 `.env`。之后 runner 只接受 rootless dockerd + runsc 的组合。

gVisor 兼容性：Python 标准库与镜像内预装依赖需在部署机跑一遍真实脚本回归。runsc 不支持个别 syscall，表现为沙箱执行失败（fail-closed，不会静默回落 runc）；这也是生产强制它的代价。

`docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d` 后，runner 容器内的端点为 `unix:///run/rootless/docker.sock`，与宿主 socket 路径解耦。

**生产密钥是启动硬前置**：`ENV=prod` 下，`JWT_SECRET`、`INTERNAL_SIGNING_SECRET` 必须 ≥32 字符且不得使用仓库里的开发默认值，否则所有服务在启动阶段直接抛错；identity 额外要求 `MFA_ENCRYPTION_KEY`，payment 额外要求 `PAYMENT_WEBHOOK_SECRET`。`TRUST_IDENTITY_HEADERS` 保持开启（gateway 签名转发身份头需要），但签名密钥不合格时信任头不再提供任何安全性。runner `/healthz` 中 `security.secrets_configured=false` 或 `security.issues` 非空，即表示密钥没有真正替换。

**隔离参数生产锁死**：`SANDBOX_NETWORK` 只能为 `none`，`SANDBOX_CPU` ≤ 2.0，`SANDBOX_MEMORY_MB` ≤ 2048；超限 runner 拒绝启动。需要出网时新增 allowlist egress 代理，不要改 `SANDBOX_NETWORK`。

**容器加固**：每次执行都显式 `cap_drop=ALL`、`no-new-privileges`、只读根 + `/tmp` 32MB、`shm_size=16MB`、`nofile=1024`、`pids_limit=64`、uid 65534；`/proc` 敏感路径由 Docker 默认 masked/readonly 列表覆盖，丢弃全部 capability 后无法 remount。

可用性设计：

- harness 的 `SANDBOX_RUNNER_URLS` 支持逗号分隔的多台 runner，按请求轮询；连接失败、429/502/503/504、或 runner 返回「繁忙」时自动换下一台，故障实例进入 30s 冷却。
- 单台 runner 通过 `SANDBOX_MAX_CONCURRENCY` 限制并发容器数，超出先排队（`SANDBOX_QUEUE_TIMEOUT_SECONDS`），排队超时返回「繁忙」并触发 harness 转移。
- **跨 runner 重复执行防护**：harness 每次沙箱调用生成一个 `X-Execution-Id`，整个故障转移过程复用；runner 用 Redis 做「执行锁 + 结果缓存」——同一 execution_id 只真正执行一次，转移后优先复用第一次的结果。去重存储不可用时 runner 直接拒绝执行（fail-closed），因此 `sandbox-runner` 依赖 Redis。
- 500 不转移：可能是容器已执行后响应序列化失败，换实例重试会造成重复执行。
- **真正抗主机故障要求每台沙箱主机各部署一个 runner**；`docker compose --scale sandbox-runner=2` 只能抗进程崩溃，同宿主副本仍共享同一个 Docker 守护进程，后者仍是单点。

本地演示/测试（`SANDBOX_ENABLED=false`）走本地子进程，不是安全边界，不能用于生产。

启用 Docker 沙箱：

```bash
# 构建并输出内容寻址的 SANDBOX_IMAGE（形如 sha256:...），写入部署机 .env
make build-sandbox-image
# .env 设置 SANDBOX_ENABLED=true 与上一步输出的 SANDBOX_IMAGE；
# compose 已给 harness 注入 SANDBOX_RUNNER_URLS=http://sandbox-runner:8000
docker compose up -d --build sandbox-runner harness
# 多主机：SANDBOX_RUNNER_URLS=http://runner-a:8000,http://runner-b:8000
```

**生产镜像必须 digest 固定**：`SANDBOX_IMAGE` 只接受 `repo@sha256:...`（registry 推送后获取）或 `sha256:...`（本地构建的镜像 ID）。`:latest` 等 tag 会在 runner 启动守卫处直接拒绝，避免执行环境被静默替换。换镜像时重新构建并更新 `.env` 后重启 runner。

本地裸进程调试可分别启动：

```bash
make dev-sandbox-runner   # 需要本机 Docker 守护进程
SANDBOX_ENABLED=true SANDBOX_RUNNER_URLS=http://localhost:8011 make dev-harness
```

启用后为 fail-closed：全部 runner 不可达、Docker SDK、守护进程或镜像任一不可用，`sandbox_run` 直接返回 `unavailable`，**绝不回退本地子进程**。

排查顺序：

1. `curl localhost:8006/sandbox/health` 看 `backend`、`runner_urls` 与 `runners.endpoints` 每台状态；
2. `docker compose logs sandbox-runner`，并确认 runner 的 `REDIS_URL` 可连通；
3. 确认 runner 容器内可访问 Docker 端点并确认是 rootless：`docker compose exec sandbox-runner python -c "import docker;print(docker.DockerClient(base_url='unix:///run/rootless/docker.sock').info()['SecurityOptions'])"`（本地开发用默认 socket 时可省略 `base_url`）。

`POST /api/agents/sandbox/run` 仅供 staff，runner 侧会再校验一次（非 staff 返回 403）；执行受 `SANDBOX_TIMEOUT_SECONDS`、CPU、内存、PID 与 `network=none` 限制。

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
| 下单 409 `CONFLICT` | 库存不足 / 幂等键复用 / 支付服务不可用 | 查 `/api/inventory/stocks` 与 payment 日志；换幂等键 |
| 订单卡在 `PAYING` | 支付回调失败或 outbox 未投递 | 查 payment/order 日志与 outbox `last_error`，手动重放 `payment.succeeded` |
| 订单被自动取消 | 支付单超时（`PAYMENT_TTL_SECONDS`，默认 900s） | 重新下单；或调大 TTL |
| 支付回调 401 | 签名密钥不一致或时间戳超出 5 分钟 | 确认 `PAYMENT_WEBHOOK_SECRET` 一致与机器时钟 |
| 支付回调 409 金额不一致 | 渠道回调金额/币种与建单不一致 | 不落账；按日志排查建单与渠道对账 |
| 退款 409 | 累计退款超过支付金额或工单重复 | 查 `refunds` 表与订单行 `refunded_amount_cents` |
| 对账出现 diff | 渠道账单缺失/金额不一致/迟到回调 | 查 `reconciliation_diffs`，人工核实，不自动改资金 |
| 用户付款了但订单已取消 | 迟到支付：回调晚于超时关单到达 | 属预期：订单侧自动全额退款（`latepay-{order_id}` 幂等），时间线有 `late_payment` 事件 |
| 日志反复出现「库存提交失败」 | 支付后 commit 失败且预占已被 TTL 释放 | 订单 `inventory_committed=False`，补偿扫单持续重试；仍失败需人工核对库存与订单后手动 commit 或退款 |
| 退款后库存没回来 | 该订单行已发货数量不回补 / commit 未成功 | 只回补未发货数量；退货收货确认回补未实现，需人工调整 |
| 履约单不出现 | fulfillment 未消费 `order.paid` | 查消费者组 `shop:events:order` 与 `shop:dedupe:*` |
| 售后审批 403 | 用了 customer 账号 | 改用 operator/admin 账号（bootstrap 引导或运营发放） |
| Agent 对话无输出 | SSE 被代理缓冲 | 确认 nginx `proxy_buffering off`，网关是流式路径 |
| 服务启动即退出并提示「生产环境安全配置不合格」 | `ENV=prod` 但密钥仍是开发默认值/长度不足 | 按 `.env.example` 注入随机密钥，identity/payment 还要各自专属密钥 |
| sandbox-runner 启动报 `SANDBOX_NETWORK`/`SANDBOX_CPU`/`SANDBOX_MEMORY_MB` 不合格 | 生产隔离参数被改回 bridge/host 或资源上限过高 | 恢复 `none` 与 (0, 2.0] 核、(0, 2048] MB；扩容需改代码常量并扩容专用宿主机 |
| 沙箱返回 `unavailable` | 镜像不存在或 Docker 不可达 | 构建 `infra/sandbox`，或接受本地降级 |
| 记忆检索结果差 | 未配真实嵌入，或端点故障回退哈希 | 配 `EMBEDDING_BASE_URL` + `EMBEDDING_API_KEY` 启用真实向量；换模型/维度后旧向量被维度守卫跳过，需清理或重建 `memory_records` |

## 7. 当前验证状态

已执行：

- `pytest -q` → **268 passed**
- `ruff check packages services tests` → **All checks passed**
- `python -m compileall` → **通过**
- `cd web && npm install && npm run build` → **构建成功**
- `docker compose -f docker-compose.yml -f docker-compose.prod.yml config` → **通过**（生产配置仅 gateway:8080 / web:5173 对外）

未执行：

- `docker compose up` 端到端（当前环境 Docker daemon 未运行，且启动守护进程属于需用户确认的系统操作）
