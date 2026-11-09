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

当前结果：`318 passed`。

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

**runner 只持有专用密钥与受限 Redis 凭据**（SBX-15/16）：runner 不接受 Bearer JWT，`Authorization` 头不参与鉴权；harness 用 `RUNNER_SIGNING_SECRET` 签身份头，runner 用同一把专用密钥验签，二者都不得复用 `INTERNAL_SIGNING_SECRET`。runner 的 Redis 连接走 `SANDBOX_REDIS_URL`（受限 ACL 用户 `sandbox-runner`），只允许 `sandbox:exec:*` 与 `shop:events:audit`，不能读业务键。Redis 的 `default` 用户已改为密码认证，业务服务通过 `REDIS_PASSWORD` 注入凭据；runner 容器拿不到该密码，因此无法匿名绕过 ACL。非 dev 下缺任一项 runner 直接拒绝启动。

**隔离档位（`SANDBOX_ISOLATION_MODE`）**：生产有互斥的两种隔离形态，默认 `rootless`。

| 档位 | Docker 端点 | 守护进程 | runner 运行身份 | 节点要求 | 适用场景 |
|---|---|---|---|---|---|
| `rootless`（默认） | 必须显式，禁止默认 root socket | rootless dockerd | 沙箱用户 uid/gid（socket 属主） | 可与业务同机 | 单机小规模 |
| `dedicated` | 必须显式本地 `unix://` socket | rootful dockerd | 容器内 root | **只跑沙箱栈**，不承载业务容器/业务库/业务密钥 | 生产推荐 |

**rootless 档位必须是 rootless dockerd**：runner 被攻破时若连的是默认 `/var/run/docker.sock`（root 守护进程），攻击者等于拿到宿主 root，容器加固全部失效。生产 overlay 把 rootless socket 挂到容器内独立路径，并由 runner 启动守卫强制校验：

- `SANDBOX_DOCKER_HOST` 生产必填：`rootless` 档位不允许默认 root socket；`dedicated` 档位不允许空值（会回落 SDK 默认）或 `tcp://`（远端 root 守护进程），只接受本地 `unix://`；
- `SANDBOX_RUNTIME` 生产强制 `runsc`（gVisor），共享宿主内核的 runc 被启动守卫拒绝；
- `/healthz` 在不可达、隔离退化、档位与守护进程不符、节点混部或 runsc 未注册时返回 **HTTP 503**，并暴露 `docker.rootless` / `docker.insecure` / `docker.runtime_ok` / `isolation.mode` / `isolation.node_exclusive` / `isolation.foreign_containers` 供定位。

**dedicated 档位为什么可以接受 rootful**：`rootless + runsc` 在部分内核/发行版上不可用（systemd cgroup manager 需要系统总线授权，改 cgroupfs 驱动又会撞 `cgroup.subtree_control` 权限），实测 rootful + runsc 正常。于是把风险从「进程权限」转移到「节点边界」：Docker 守护进程只服务沙箱，节点不承载任何业务容器与数据，失陷上限是可整机重建的节点本身；gVisor 继续收敛内核 CVE 面。**这条前提不可协商**——`dedicated` 节点上出现任何非沙箱容器时，runner 在每次执行前拒绝（返回 `unavailable`）并让 healthz 503。

节点独占按两条依据判定容器是否属于沙箱栈：① 容器标签 `shop.sandbox.role`——`runner`（runner 自身）、`execution`（沙箱执行容器，runner 自动打标）、`infra`（节点上的沙箱配套容器）；② 容器属于 `SANDBOX_NODE_COMPOSE_PROJECTS` 配置的 compose 项目（默认 `shop-agent-sandbox`，由 compose 自动注入 `com.docker.compose.project`，配套容器无需手工打标）。两条都不满足的容器一律视为混部。

宿主侧初始化已脚本化（Debian/Ubuntu，root 执行，幂等）：

```bash
# rootless（默认）：rootless dockerd + runsc
sudo make setup-sandbox-host

# dedicated：rootful dockerd + runsc（专用节点）
sudo -E SANDBOX_SETUP_MODE=dedicated make setup-sandbox-node
# 可选镜像加速（不会把第三方源固化进仓库）：DOCKER_REGISTRY_MIRRORS=https://mirror.example
```

rootless 分支最后打印：

```
SANDBOX_ROOTLESS_SOCKET=/run/user/<uid>/docker.sock
SANDBOX_RUNTIME=runsc
SANDBOX_UID=<uid>
SANDBOX_GID=<gid>
```

把这些写入部署机 `.env`，runner 只接受 rootless dockerd + runsc 的组合。dedicated 分支打印：

```
SANDBOX_ISOLATION_MODE=dedicated
SANDBOX_DOCKER_HOST=unix:///var/run/docker.sock
SANDBOX_RUNTIME=runsc
```

**专用节点部署**（与业务栈分开，节点只跑沙箱栈）：

```bash
# 1) 节点 .env 只放沙箱所需配置：SANDBOX_IMAGE(digest)、RUNNER_SIGNING_SECRET、
#    SANDBOX_REDIS_URL（受限 ACL）、SANDBOX_RUNNER_BIND=<节点私网 IP>
# 2) 在节点自身 rootful dockerd 上构建镜像并取 digest：make build-sandbox-image
# 3) 起 runner：8000 只绑 SANDBOX_RUNNER_BIND（默认 127.0.0.1），不发布其它端口
docker compose -f docker-compose.sandbox-node.yml up -d --build
# 4) 业务侧 harness 指向节点：.env 设 SANDBOX_RUNNER_URLS=http://<节点私网 IP>:8000
```

节点要求：不部署业务服务/业务库/业务密钥；防火墙只放行 harness 网段到 8000；节点可整机重建。`SANDBOX_IMAGE` 留空仍是 tag 时会被启动守卫拒绝（dedicated 不放宽 digest 校验）。

**跨节点去重依赖共享 Redis**：专用节点的 runner 仍需通过 `SANDBOX_REDIS_URL` 连接同一份受限 ACL Redis（执行锁 + 结果缓存 + 审计流）。若改在节点本地起 Redis，跨节点去重会退化为「每节点独立锁」，只在单 runner 部署时可接受，必须在 `.env` 注释中写明。

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
`docker-compose.prod.yml` 已强制 `SANDBOX_ENABLED=true`；harness 启动守卫在非 dev 下缺该开关会直接拒绝启动，避免请求落回与 harness 同权限的本地子进程。

启用 Docker 沙箱：

```bash
# 1) 构建并输出内容寻址的 SANDBOX_IMAGE（形如 sha256:...）。
#    镜像必须构建进 runner 实际连接的守护进程，否则 runner 会一直 ImageNotFound：
#    rootless 档位填 setup.sh 打印的宿主 socket（SANDBOX_ROOTLESS_SOCKET）
SANDBOX_DOCKER_HOST=unix:///run/user/<uid>/docker.sock make build-sandbox-image
#    dedicated 档位在专用节点上直接 make build-sandbox-image（走本机 rootful 默认端点）
#    本地开发直接 make build-sandbox-image 即可（走 Docker Desktop 默认端点）
# 2) 把上一步输出的 SANDBOX_IMAGE 写入部署机 .env（禁止保留 :latest）
# 3) 确认镜像在 runner 端点可见后再重启
docker -H unix:///run/user/<uid>/docker.sock image inspect <输出的 sha256:...> >/dev/null \
  && echo "镜像已就绪"
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build sandbox-runner harness
# 4) 端到端验证：/sandbox/health 的 runners 全 ok，且 POST /sandbox/run 返回 status=ok
# 多主机：SANDBOX_RUNNER_URLS=http://runner-a:8000,http://runner-b:8000
```

**生产镜像必须 digest 固定**：`SANDBOX_IMAGE` 只接受 `repo@sha256:...`（registry 推送后获取）或 `sha256:...`（本地构建的镜像 ID）。`:latest` 等 tag 会在 runner 启动守卫处直接拒绝，避免执行环境被静默替换。换镜像时重新构建、更新 `.env`、确认 runner 端点已有该 digest 后重启 runner，三步缺一不可。

本地裸进程调试可分别启动：

```bash
make dev-sandbox-runner   # 需要本机 Docker 守护进程
SANDBOX_ENABLED=true SANDBOX_RUNNER_URLS=http://localhost:8011 make dev-harness
```

启用后为 fail-closed：全部 runner 不可达、Docker SDK、守护进程或镜像任一不可用，`sandbox_run` 直接返回 `unavailable`，**绝不回退本地子进程**。

排查顺序：

1. `curl localhost:8006/sandbox/health` 看 `backend`、`runner_urls` 与 `runners.endpoints` 每台状态；
2. `docker compose logs sandbox-runner`，并确认 runner 的 `SANDBOX_REDIS_URL`（受限 ACL 用户）可连通，且不是共享 `REDIS_URL`；
3. 确认 runner 容器内可访问 Docker 端点并确认是 rootless：`docker compose exec sandbox-runner python -c "import docker;print(docker.DockerClient(base_url='unix:///run/rootless/docker.sock').info()['SecurityOptions'])"`（本地开发用默认 socket 时可省略 `base_url`）。

`POST /api/agents/sandbox/run` 仅供 staff，runner 侧会再校验一次（非 staff 返回 403）；执行受 `SANDBOX_TIMEOUT_SECONDS`、CPU、内存、PID 与 `network=none` 限制。

### 5.1 同机 rootless 部署机待办与验证清单（待执行）

> 本地代码侧通过不等于部署机可用。以下每一项都必须在部署机执行并回填结果；全部完成前，沙箱不得标记为可上线。
> 专用节点（`dedicated` 档位）用 §5.2 清单，不要在本清单里混用 rootless 的 socket 与 uid/gid。
> 约定：`$ROOTLESS` 为 `setup.sh` 打印的宿主 socket（如 `/run/user/1000/docker.sock`），`$TOKEN` 为 §4.1 登录得到的 staff access token。

**环境前提（不满足则停止）**

- [ ] 内核为 cgroup v2：`stat -fc %T /sys/fs/cgroup` 输出 `cgroup2fs`。
- [ ] 支持非特权 user namespace（rootless Docker 硬前提）。
- [ ] 仅 Debian/Ubuntu，具备 root/sudo。

**A. 宿主初始化与 `.env`**

- [ ] `sudo make setup-sandbox-host`（等价 `sudo bash infra/rootless-docker/setup.sh`），记录输出的 `SANDBOX_ROOTLESS_SOCKET`、`SANDBOX_RUNTIME`、`SANDBOX_UID`、`SANDBOX_GID`。
- [ ] `.env` 写入 `SANDBOX_ROOTLESS_SOCKET=$ROOTLESS`、`SANDBOX_RUNTIME=runsc`、`SANDBOX_UID`、`SANDBOX_GID`、`ENV=prod`。
- [ ] runner 运行身份与 socket 属主一致：`stat -c '%u %g' $ROOTLESS` 输出的 uid/gid 必须等于 `.env` 中的 `SANDBOX_UID` / `SANDBOX_GID`；不一致时 runner 容器会因 EACCES 无法连接 rootless dockerd。
- [ ] 生产密钥：`JWT_SECRET`、`INTERNAL_SIGNING_SECRET` 均为 ≥32 字符随机值；identity 另配 `MFA_ENCRYPTION_KEY`，payment 另配 `PAYMENT_WEBHOOK_SECRET`。
- [ ] runner 专用密钥：`.env` 注入 ≥32 字符的 `RUNNER_SIGNING_SECRET`，且必须与 `INTERNAL_SIGNING_SECRET` 不同；harness 与 runner 都要读取。
- [ ] 业务 Redis 认证：`.env` 注入 ≥32 字符 `REDIS_PASSWORD`；`REDIS_URL` 可只写 host/db，服务启动时会把密码注入 default 用户。生产 Redis 不应暴露到公网，并定期轮换密码。
- [ ] runner Redis ACL：注入 `SANDBOX_REDIS_PASSWORD`，并确认 `SANDBOX_REDIS_URL` 使用非 `default` 的受限用户、带密码、不等于共享 `REDIS_URL`。runner 容器不得注入 `REDIS_PASSWORD`，否则它可绕过 ACL 走 default 用户。
- [ ] runner 容器环境不得包含 `JWT_SECRET` / `INTERNAL_SIGNING_SECRET` / `REDIS_PASSWORD`：`docker compose config --format json` 中 `services.sandbox-runner.environment` 只应有 `RUNNER_SIGNING_SECRET`、`SANDBOX_REDIS_URL` 与隔离类参数。
- [ ] 确认 harness 侧沙箱开关：`docker compose -f docker-compose.yml -f docker-compose.prod.yml config --format json` 中 `services.harness.environment.SANDBOX_ENABLED` 为 `"true"`。

**B. 镜像（必须与 runner 端点一致）**

- [ ] 在 rootless 端点构建：`SANDBOX_DOCKER_HOST=unix://$ROOTLESS make build-sandbox-image`，记录输出的 `SANDBOX_IMAGE=sha256:...`。
- [ ] 写入 `.env` 的 `SANDBOX_IMAGE`，并确认镜像在 runner 端点可见：
  `docker -H unix://$ROOTLESS image inspect <digest>` 成功返回。
- [ ] 启动生产 overlay：`docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build sandbox-runner harness`。
- [ ] `docker compose ps sandbox-runner` 持续 `healthy`，无反复重启（重启通常表示镜像未固定或 runsc 未注册）。

**C. 健康与隔离档位**

- [ ] runner 健康检查：`docker compose exec sandbox-runner curl -fsS http://localhost:8000/healthz`，期望 `status=ok`、`docker.rootless=true`、`docker.runtime_ok=true`、`image_pinned=true`、`security.secrets_configured=true`。
- [ ] harness：`docker compose exec harness curl -fsS http://localhost:8000/sandbox/health`，期望 `enabled=true`、`backend=remote`、`runners.status=ok`。
- [ ] 实际运行时为 runsc：先提交一段 `import time; time.sleep(5)`，在另一终端执行
  `docker -H unix://$ROOTLESS ps -q | xargs -r docker -H unix://$ROOTLESS inspect --format '{{.HostConfig.Runtime}} {{.Config.Image}}'`
  期望输出 `runsc sha256:...`。

**D. 端到端功能与隔离探针**

- [ ] 基本执行：`POST localhost:8080/api/agents/sandbox/run`，body `{"code":"print(6*7)"}`，期望 `status=ok`、`stdout=42`、`backend=docker`。
- [ ] 隔离探针（同一次调用执行以下代码），期望 `UID=65534`、`NETWORK_BLOCKED`、`READONLY_OK`：

```python
import os, socket
print("UID", os.getuid())
try:
    socket.create_connection(("1.1.1.1", 80), timeout=2)
    print("NETWORK_OPEN")
except Exception as exc:
    print("NETWORK_BLOCKED", type(exc).__name__)
try:
    open("/etc/hostname", "a").write("x")
    print("READONLY_BROKEN")
except Exception as exc:
    print("READONLY_OK", type(exc).__name__)
```

- [ ] 超时：`{"code":"import time; time.sleep(30)","timeout":3}`，记录实际 `status`。
  **已知差异**：当前超时会被归到 `status=error`（P1 待修），此项先记录实际值，不算功能失败。
- [ ] 内存炸弹：`{"code":"b = bytearray(1024 * 1024 * 1024)"}`，期望容器被内存限制终止（exit 137 或 unavailable），宿主保持稳定。
- [ ] 输出刷屏：`{"code":"while True: print('x' * 1024)"}`，期望超时结束；检查单容器 json-file ≤1MB，`docker -H unix://$ROOTLESS ps -a` 无残留容器，宿主磁盘无明显增长。
- [ ] fail-closed：`docker compose stop sandbox-runner` 后再执行沙箱，期望 `status=unavailable`；`docker compose logs harness` 中不得出现「本地子进程执行」。
- [ ] （多 runner 时）停掉一台，请求仍成功，且同一 `X-Execution-Id` 只真正执行一次。

**E. gVisor 兼容性回归（SBX-09）**

- [ ] 控制面执行：`SANDBOX_RUNNER_URLS=http://<runner>:8000 RUNNER_SIGNING_SECRET=<runner 专用密钥> make sandbox-gvisor-regression`。脚本先校验该 runner 的 healthz 为 `runtime=runsc`，再跑标准库、线程、子进程、文件+sqlite、浮点统计探针；任一失败退出码非 0。
- [ ] 业务计划预装依赖用 `SANDBOX_PROBE_MODULES=numpy,pandas` 追加。失败信息含 `ModuleNotFoundError` 时是「镜像没装依赖」，需增肥 [Dockerfile](file:///c:/Users/far/Desktop/git/shop-agent/infra/sandbox/Dockerfile)；其余多为 runsc syscall 不兼容，记录报错。
- [ ] 全部通过后，把 [sandbox-hardening.md](file:///c:/Users/far/Desktop/git/shop-agent/docs/design/sandbox-hardening.md) 的 SBX-09 勾选为 ☑ 并写明日期与镜像 digest。

**F. 观测与运维**

- [ ] runner `/healthz` 返回 503 时（可临时停 rootless dockerd 模拟），确认 Prometheus/告警能发现，且 harness 沙箱调用 fail-closed。
- [ ] 确认镜像清理任务排除当前 `SANDBOX_IMAGE` digest（`docker image prune` 不应删除该镜像）。
- [ ] `docker compose logs sandbox-runner` 无刷屏；宿主磁盘与内存无异常增长。

**结果记录**

| 日期 | 分组 | 命令/用例 | 实际结果 | 结论 |
|---|---|---|---|---|
|  | A | setup.sh + `.env` |  |  |
|  | B | 构建/镜像可见/healthy |  |  |
|  | C | healthz + runsc 探测 |  |  |
|  | D | 端到端 + 隔离探针 + fail-closed |  |  |
|  | E | gVisor 兼容性 |  |  |
|  | F | 告警与运维 |  |  |

### 5.2 专用节点（dedicated）部署与验证清单（2026-11-08 已执行节点侧；控制面项待补）

> 必须在**干净专用节点**执行：与本仓库业务栈不共机，节点上没有业务容器、业务库与业务密钥。混部节点（例如同时跑 erp-agent 的部署机）只能走 §5.1 的 rootless 清单，不能用本清单验收 dedicated。
> 约定：`$NODE` 为节点私网 IP，`$TOKEN` 为 §4.1 登录得到的 staff access token，控制面 `.env` 指业务侧 harness 所在机器。

**环境前提（不满足则停止）**

- [x] 节点仅运行沙箱栈：`docker ps --format '{{.Names}} {{.Label "shop.sandbox.role"}} {{.Label "com.docker.compose.project"}}'` 中的容器要么带 `runner` / `execution` / `infra` 角色标签，要么属于 `SANDBOX_NODE_COMPOSE_PROJECTS` 配置的沙箱 compose 项目。
- [x] 节点不承载业务数据：无 MySQL / Mongo / 业务 Redis 容器，无业务库挂载。
- [x] 节点私网可达 harness，且公网只放行运维 SSH；8000 不对公网开放。

**A. 宿主初始化与 `.env`**

- [x] `sudo -E SANDBOX_SETUP_MODE=dedicated make setup-sandbox-node`，脚本退出 0。
- [x] `docker info --format '{{.Runtimes}}'` 含 `runsc`；`docker info --format '{{.SecurityOptions}}'` **不含** `rootless`。
- [x] 节点 `.env` 写入 `SANDBOX_ISOLATION_MODE=dedicated`、`SANDBOX_DOCKER_HOST=unix:///var/run/docker.sock`、`SANDBOX_RUNTIME=runsc`。
- [x] 节点 `.env` 写入 `RUNNER_SIGNING_SECRET`（≥32 字符，与 `INTERNAL_SIGNING_SECRET` 不同）与受限 ACL 的 `SANDBOX_REDIS_URL`；**不得**出现 `JWT_SECRET` / `INTERNAL_SIGNING_SECRET` / `REDIS_PASSWORD`。
- [x] `docker compose -f docker-compose.sandbox-node.yml config --format json` 中 `services.sandbox-runner.environment` 只含上述沙箱参数与隔离类参数。
- [ ] 控制面 `.env` 写入 `SANDBOX_RUNNER_URLS=http://$NODE:8000`，`SANDBOX_ENABLED=true`。

**B. 镜像（必须与节点 rootful 守护进程一致）**

- [x] 在节点执行 `make build-sandbox-image`，记录 `SANDBOX_IMAGE=sha256:...` 并写入节点 `.env`。
- [x] `docker image inspect <digest>` 在节点成功返回（runner 与构建共用同一守护进程）。
- [x] `docker compose -f docker-compose.sandbox-node.yml up -d --build`，`docker compose ps` 持续 `healthy`，无反复重启。

**C. 健康与隔离档位**

- [x] `curl -fsS http://$NODE:8000/healthz` 期望 `status=ok`、`isolation.mode=dedicated`、`isolation.node_exclusive=true`、`docker.rootless=false`、`docker.runtime_ok=true`、`image_pinned=true`、`security.secrets_configured=true`。
- [ ] 业务侧 `/sandbox/health` 期望 `enabled=true`、`backend=remote`、对应 runner 状态 `ok`。

**D. 节点独占强制（核心验收项）**

- [x] 在节点临时启动一个非沙箱容器（如 `docker run -d --name mixed-in alpine:3.20 sleep 3600`），不带 `shop.sandbox.role` 标签。
- [x] `curl -fsS http://$NODE:8000/healthz` 返回 **503**，`isolation.foreign_containers` 列出 `mixed-in`。
- [x] 此时调用沙箱执行，期望 `status=unavailable`，`docker ps` 中没有新建的执行容器（执行边界已拒绝）。
- [x] 删除临时容器后 healthz 恢复 200，`isolation.node_exclusive=true`（健康检查按请求时探测，无需重启 runner）。

**E. 端到端功能与隔离探针**

- [x] 基本执行：`{"code":"print(6*7)"}` 期望 `status=ok`、`stdout=42`。
- [x] 隔离探针（同 §5.1 D 代码），期望 `UID=65534`、`NETWORK_BLOCKED`、`READONLY_OK`。
- [x] 实际运行时为 runsc：执行 sleep 脚本期间在节点运行
  `docker ps -q | xargs -r docker inspect --format '{{.HostConfig.Runtime}} {{.Config.Image}} {{.Config.Labels}}'`
  期望 `runsc sha256:... map[shop.sandbox.role:execution]`。
- [x] 端口暴露面：节点 `ss -lntp` 中 8000 只绑 `$NODE:8000`（或 127.0.0.1），无 3306/6379/27017。
- [ ] fail-closed：停掉 runner 后沙箱调用 `status=unavailable`，harness 日志无「本地子进程执行」。
- [x] 内存炸弹与输出刷屏按 §5.1 D 同法验证；节点磁盘与内存保持稳定。

**F. 观测与运维**

- [ ] healthz 503 能被告警发现；节点容器清单可作为「混部」巡检项。
- [x] `docker compose logs sandbox-runner` 无刷屏；节点可整机重建（重建后只需 `.env` + 镜像 digest + compose 文件）。
- [ ] 孤儿回收：在节点执行 `docker run -d --label shop.sandbox.role=execution --name orphan-probe alpine:3.20 sleep 3600`，等待 `SANDBOX_TIMEOUT_SECONDS + 30` 秒后最多再等一个回收周期（60s），确认容器被删除且 `docker ps -a --filter label=shop.sandbox.role=execution` 为空；同时确认执行中的容器（sleep 脚本跑着时）不会被回收。
- [x] gVisor 兼容性回归（SBX-09）在 dedicated 档位实测（2026-11-09）：runner 为 `runtime=runsc`、镜像 digest 固定时，`make sandbox-gvisor-regression` 的 5 项探针 stdlib / threads / subprocess / files / math 全部 PASS，审计流写入 5 条 `audit.sandbox.executed`，无执行容器残留。业务依赖（numpy/pandas）未预装，`ModuleNotFoundError` 属镜像缺依赖，需按实际脚本需求增肥镜像。

**结果记录**

| 日期 | 分组 | 命令/用例 | 实际结果 | 结论 |
|---|---|---|---|---|
| 2026-11-08 | A | setup-sandbox-node + `.env` | `runc+runsc`、无 rootless；环境只含沙箱参数（无 `JWT_SECRET`/`INTERNAL_SIGNING_SECRET`/`REDIS_PASSWORD`） | 通过 |
| 2026-11-08 | B | 节点构建/镜像可见/healthy | `SANDBOX_IMAGE=sha256:e73edb37…`；runner 容器 healthy，未重启 | 通过 |
| 2026-11-08 | C | healthz dedicated + harness | 200；`isolation.mode=dedicated`、`node_exclusive=true`、`docker.rootless=false`、`runtime_ok=true`、`image_pinned=true`、`secrets_configured=true`；harness 项未验证 | 节点侧通过 |
| 2026-11-08 | D | 混部容器 → 503 + unavailable | healthz→503 且 `foreign_containers=["mixed-probe"]`；`/run`→`unavailable`，无新建容器；删除后 healthz→200 | 通过 |
| 2026-11-08 | E | 端到端 + 探针 + 端口面 | `42`；`uid=65534`；`NETWORK_BLOCKED`；`ROOTFS_READONLY`；`runtime=runsc`、`network=none`、`capdrop=[ALL]`、`shm=16MB`；timeout 20s→`status=timeout`；同 execution_id 第二次耗时一致；20KB→`truncated=true`；400MB 写入→`exit_code=137` | 通过（修复 2 处缺陷） |
| 2026-11-08 | F | 告警、重建与 gVisor 回归 | 日志无刷屏；8000 仅 127.0.0.1；告警未验证（节点无 Prometheus）；SBX-09 兼容性未跑 | 部分通过 |
| 2026-11-09 | F | gVisor 回归（SBX-09） | 节点临时栈（`runtime=runsc`、digest 固定）跑 `gvisor_regression.py`：stdlib/threads/subprocess/files/math 全 PASS；审计流 5 条；无执行容器残留 | 通过（业务依赖缺失另行跟踪） |

**验收中发现并修复的缺陷（2026-11-08）**

1. `container.wait` 读超时被 urllib3 2.x 包装成 `ConnectionError`，未映射为 `timeout`，实测返回 `infra_error`。修复：仅对错误文本含 `read timed out` 的 `ConnectionError` 按 timeout 处理，其余连接故障仍走基础设施错误；补 `test_wait_wrapped_read_timeout_maps_to_timeout` 与 `test_wait_connection_error_maps_to_infra_error`。
2. 只设 `mem_limit` 时 Docker 默认放开同等大小 swap，256MB 限制下脚本仍可写入 400MB。修复：`memswap_limit` 与 `mem_limit` 取同值禁用 swap，实测 400MB 写入立即 `exit_code=137`，宿主内存稳定。
3. 受限网络构建：`infra/Dockerfile` 增加 `APT_MIRROR` / `PIP_INDEX_URL` / `PIP_TRUSTED_HOST` 构建参数（默认仍为官方源），节点构建传阿里云镜像，避免卡在 `deb.debian.org` 与 `pypi.org`。

## 6. 故障排查

| 现象 | 可能原因 | 处理 |
|---|---|---|
| inventory 启动即退出 | Redis 不可达（启动要播种热库存） | `docker compose ps redis`，检查 `REDIS_URL` / `REDIS_PASSWORD` |
| Redis 返回 NOAUTH / 服务启动报「禁止匿名 default 用户」 | `REDIS_PASSWORD` 未注入，或 `REDIS_URL` 仍指向无密码连接 | 按 `.env.example` 注入 `REDIS_PASSWORD`，或让 `REDIS_URL` 自带凭据；确认 redis 容器健康检查带认证 |
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
| sandbox-runner 启动报 `RUNNER_SIGNING_SECRET` / `SANDBOX_REDIS_URL` 不合格 | runner 密钥复用内网密钥，或 Redis 仍用共享/默认用户 | 注入独立的 `RUNNER_SIGNING_SECRET` 与非 `default`、带密码的 `SANDBOX_REDIS_URL` |
| sandbox-runner 启动报 `SANDBOX_NETWORK`/`SANDBOX_CPU`/`SANDBOX_MEMORY_MB` 不合格 | 生产隔离参数被改回 bridge/host 或资源上限过高 | 恢复 `none` 与 (0, 2.0] 核、(0, 2048] MB；扩容需改代码常量并扩容专用宿主机 |
| sandbox-runner 启动报 `SANDBOX_ISOLATION_MODE` 无法识别 | 档位值拼错（如 `dedicated-node`） | 只能是 `rootless` 或 `dedicated`；拼错不回落默认，避免隔离预期与部署不一致 |
| dedicated 档位启动报端点不合格 | `SANDBOX_DOCKER_HOST` 为空或写成 `tcp://` | 专用节点必须显式 `unix:///var/run/docker.sock`（本地 socket，不接受远端守护进程） |
| dedicated 档位 healthz 503 且 `isolation.foreign_containers` 非空 | 节点混部：容器既无 `shop.sandbox.role ∈ execution/runner/infra` 标签，也不属于 `SANDBOX_NODE_COMPOSE_PROJECTS` 中的 compose 项目 | 移走业务容器；临时维护容器加 `shop.sandbox.role=infra` 标签后重启 runner；用 `-p` 改过项目名时同步 `SANDBOX_NODE_COMPOSE_PROJECTS` |
| dedicated 档位 healthz 503 且 `isolation.mode_mismatch=true` | 声明 dedicated 却连到 rootless 守护进程 | 按专用节点重新执行 `setup-sandbox-node`；或改回 `rootless` 档位与 rootless socket |
| 沙箱返回 `unavailable` 且 stderr 提示「专用节点存在非沙箱容器」 | 执行边界独占检查拦截（节点混部） | 清理非沙箱容器；这是预期 fail-closed，不会回退 runc 或本地进程 |
| 沙箱返回 `unavailable` | 镜像不存在或 Docker 不可达 | 构建 `infra/sandbox`，或接受本地降级 |
| 记忆检索结果差 | 未配真实嵌入，或端点故障回退哈希 | 配 `EMBEDDING_BASE_URL` + `EMBEDDING_API_KEY` 启用真实向量；换模型/维度后旧向量被维度守卫跳过，需清理或重建 `memory_records` |

## 7. 当前验证状态

已执行：

- `pytest -q` → **318 passed**
- `ruff check packages services tests` → **All checks passed**
- `python -m compileall` → **通过**
- `cd web && npm install && npm run build` → **构建成功**
- `docker compose -f docker-compose.yml -f docker-compose.prod.yml config` → **通过**（生产配置仅 gateway:8080 / web:5173 对外）
- prod overlay 实际生效值 → `harness.SANDBOX_ENABLED=true`、`sandbox-runner.SANDBOX_RUNTIME=runsc`、`sandbox-runner.SANDBOX_DOCKER_HOST=unix:///run/rootless/docker.sock`

未执行：

- `docker compose up` 端到端（当前环境 Docker daemon 未运行，且启动守护进程属于需用户确认的系统操作）
- 部署机沙箱上线验证：按 §5.1 清单逐项执行（rootless 构建端点、镜像可见性、runsc 实际生效、隔离探针、gVisor 兼容性、fail-closed、告警）
