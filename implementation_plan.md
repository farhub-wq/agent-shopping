# M1 账号与权限实施计划

> **给执行者：** 按任务顺序逐项执行，每个任务先写失败测试再实现；每个任务结束跑一次验证。提交前征得用户同意。

**目标：** 把“客户端自选角色”替换为真实账号体系，使顾客 / 运营 / 管理员身份由服务端校验，并让前端完成登录、刷新与退出。

**架构：** 新增独立 `identity` 服务（`identity_db`），负责注册、登录、refresh 轮换与角色授予；网关改为调用 identity 并转发 Cookie，服务侧鉴权改为“JWT 优先 + 内网签名头兜底”。业务服务与订单状态机不改。

**技术栈：** FastAPI · SQLAlchemy 2.0 async · MySQL 8（生产）/ SQLite（本地测试）· Redis · PyJWT · Argon2id（`argon2-cffi`）· React + TypeScript。

**规格：** [system-design.md](docs/design/system-design.md) 第 4.1、5.1、8 节；需求 [requirements-shop-agent.md](docs/requirements/requirements-shop-agent.md) FR-01/02、NFR-05。

> **生产基线说明**：本计划只覆盖 M1。M1 的验证仍在本地单机 Compose 完成；双主机 + LB + 托管 MySQL/Redis、沙箱独立 runner、事件 DLQ、审计服务与灾备属于 M3，不在 M1 改造范围。设计基线（SLO、总账、部分履约）见设计文档第 1、11、12 节。

## 全局约束

- 代码注释用简体中文，说明“做什么、为什么”，不写复述语法的注释。
- 所有写接口必须显式校验权限；错误统一 `ApiError`，不得吞异常。
- 角色只能来自 `identity_db`，任何请求体、Header 都不能指定角色。
- 密码用 Argon2id；refresh token 只存哈希；日志不得打印密码、完整 token、完整手机号。
- 提交信息 `feat(identity): ...` / `fix(gateway): ...`，简体中文描述；不自动 push。
- 验证命令：`.venv/Scripts/python -m pytest -q`、`.venv/Scripts/python -m ruff check packages services tests`、`cd web && npm run build`。

## Review Focus

1. 同一手机号并发注册 → 应返回 409，不能抛 500。
2. 旧 refresh token 被重放 → 必须 401，且新 token 不受影响。
3. 客户端伪造 `X-User-Role: admin` → 生产模式下必须被忽略或拒绝。
4. 第 5 次登录失败后的锁定边界 → 锁定期间正确密码也返回 429，窗口过后恢复。
5. 前端 access token 过期 → 只自动刷新一次，刷新失败必须清空会话且不进入死循环。

---

### Task 1: identity 服务骨架与注册

**Files:**
- Create: `services/identity/__init__.py`
- Create: `services/identity/models.py`
- Create: `services/identity/security.py`
- Create: `services/identity/app.py`
- Create: `tests/test_identity_register.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `shop_common.db.Base/get_session_factory/init_models`、`shop_common.errors.ApiError/install_error_handlers`、`shop_common.logging.configure_logging`
- Produces:
  - `security.hash_password(password: str) -> str`、`security.verify_password(password: str, password_hash: str) -> bool`
  - `models.User(id, phone, email, password_hash, status, created_at, updated_at)`
  - `models.UserRole(user_id, role, tenant, granted_by, granted_at)`
  - `POST /auth/register` → `201 {"id": str, "phone": str, "roles": ["customer"]}`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_identity_register.py
def test_register_creates_customer(client):
    resp = client.post("/auth/register", json={"phone": "13800000001", "password": "Passw0rd!"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["phone"] == "13800000001"
    assert body["roles"] == ["customer"]
    assert "password" not in resp.text

def test_register_duplicate_phone_conflicts(client):
    payload = {"phone": "13800000002", "password": "Passw0rd!"}
    assert client.post("/auth/register", json=payload).status_code == 201
    assert client.post("/auth/register", json=payload).status_code == 409

def test_register_rejects_short_password(client):
    resp = client.post("/auth/register", json={"phone": "13800000003", "password": "short"})
    assert resp.status_code == 400
```

同文件顶部定义 fixture（app 的 lifespan 会建表）：

```python
@pytest.fixture(scope="module")
def client():
    from identity.app import app
    with TestClient(app) as test_client:
        yield test_client
```

- [ ] **Step 2: 运行并确认失败**

运行：`.venv/Scripts/python -m pytest tests/test_identity_register.py -q`
预期：`ModuleNotFoundError: No module named 'identity'`

- [ ] **Step 3: 实现模型与注册接口**

- `pyproject.toml`：dependencies 增加 `argon2-cffi>=23.1.0`；packages include 增加 `"identity*"`。
- `models.py` 三个模型，`User.phone` 唯一索引、`status` 默认 `active`、`created_at/updated_at` 用 `datetime.now(UTC)`；`UserRole` 复合主键 `(user_id, role, tenant)`。
- `security.py` 用 `argon2.PasswordHasher()`，参数 `time_cost=3, memory_cost=65536, parallelism=4`。
- `app.py`：`lifespan` 里 `configure_logging("identity")` + `init_models()`；请求体 `phone` 用 `pattern=r"^1[3-9]\d{9}$"`，`password` 长度 8–72；同一手机号捕获 `IntegrityError` 转 `ApiError("CONFLICT", "手机号已注册")`；注册时同事务写 `UserRole(role="customer")`。
- 响应只挑 `id/phone/roles`，禁止返回 ORM 对象。

- [ ] **Step 4: 运行测试**

运行：`.venv/Scripts/python -m pytest tests/test_identity_register.py -q`
预期：3 passed

- [ ] **Step 5: 提交**

```bash
git add services/identity tests/test_identity_register.py pyproject.toml
git commit -m "feat(identity): 新增账号注册与用户模型"
```

---

### Task 2: 登录、refresh 轮换、登出与失败锁定

**Files:**
- Create: `services/identity/tokens.py`
- Create: `tests/test_identity_login.py`
- Modify: `services/identity/models.py`（新增 `RefreshToken`、`LoginAttempt`）
- Modify: `services/identity/app.py`

**Interfaces:**
- Consumes: Task 1 的 `User`/`UserRole`/`security`
- Produces:
  - `tokens.new_refresh_token() -> str`、`tokens.hash_refresh_token(raw: str) -> str`
  - `POST /auth/login {phone, password}` → `200 {"access_token": str, "token_type": "bearer", "expires_in": int, "user": {"id","phone","roles"}}` + `Set-Cookie: shop_refresh`（HttpOnly）与 `shop_csrf`
  - `POST /auth/refresh`（Cookie `shop_refresh` + Header `X-CSRF-Token`）→ 同上结构，轮换 refresh
  - `POST /auth/logout` → `204`
  - `GET /me` → `{"id","phone","roles"}`

- [ ] **Step 1: 写失败测试**

```python
def test_login_returns_roles_from_db(client):
    client.post("/auth/register", json={"phone": "13800000010", "password": "Passw0rd!"})
    resp = client.post("/auth/login", json={"phone": "13800000010", "password": "Passw0rd!"})
    assert resp.status_code == 200
    assert resp.json()["user"]["roles"] == ["customer"]
    assert resp.json()["expires_in"] == 900
    assert "HttpOnly" in resp.headers["set-cookie"]

def test_refresh_replay_is_rejected(client):
    ...  # 登录取 cookie → refresh 成功 → 用旧 cookie 再 refresh，断言 401

def test_logout_revokes_refresh(client):
    ...  # 登录 → logout → 用原 cookie refresh，断言 401

def test_fifth_failure_locks_account(client):
    ...  # 连续 5 次错误密码 → 第 6 次正确密码返回 429
```

- [ ] **Step 2: 运行并确认失败**

运行：`.venv/Scripts/python -m pytest tests/test_identity_login.py -q`
预期：404（路由不存在）

- [ ] **Step 3: 实现登录与令牌轮换**

- `jwt_ttl_seconds` 由 86400 改为 900（`shop_common/config.py`）；新增 `env: str = "dev"`、`refresh_ttl_seconds=604800`、`login_max_failures=5`、`login_lock_minutes=15`。
- access token 增加 `jti`（`uuid4`）、`iss="shop-agent"`、`aud="shop-web"`；`decode_token` 校验 `audience="shop-web"`。
- refresh token：`secrets.token_urlsafe(48)`，存 `sha256` 十六进制；轮换时写 `rotated_from` 并立即把旧行 `revoked_at` 置为当前时间；重放旧 token 返回 `UNAUTHORIZED`。
- Cookie：`shop_refresh` 参数 `httponly=True, samesite="lax", secure=(settings.env == "prod"), max_age=refresh_ttl, path="/api/auth"`；`shop_csrf` 非 HttpOnly，前端读取后放 `X-CSRF-Token`。
- 登录失败按 `phone + client_ip` 写 `LoginAttempt`；达到 5 次设置 `locked_until=now+15min`；锁定期间统一返回 429，不区分账号是否存在。
- 启动引导：`BOOTSTRAP_ADMIN_PHONE` / `BOOTSTRAP_ADMIN_PASSWORD` 存在时，空库创建 admin 角色用户；密码不落日志。
- 所有响应显式挑字段；手机号在日志中脱敏为 `138****0010`。

- [ ] **Step 4: 运行测试**

运行：`.venv/Scripts/python -m pytest tests/test_identity_login.py -q`
预期：4 passed

- [ ] **Step 5: 提交**

```bash
git add services/identity tests/test_identity_login.py packages/shop_common/config.py
git commit -m "feat(identity): 新增登录、令牌轮换与登录锁定"
```

---

### Task 3: 共享鉴权加固（JWT 优先 + 内网签名）

**Files:**
- Create: `tests/test_auth_principal.py`
- Modify: `packages/shop_common/config.py`
- Modify: `packages/shop_common/auth.py`

**Interfaces:**
- Consumes: `settings.jwt_secret`、`settings.internal_signing_secret`、`settings.trust_identity_headers`、`settings.env`
- Produces:
  - `create_internal_signature(*, user_id: str, role: str, tenant: str, ts: str, request_id: str, secret: str) -> str`
  - `verify_internal_signature(signature: str, *, user_id: str, role: str, tenant: str, request_id: str, secret: str, max_skew_seconds: int = 30) -> bool`
  - `get_principal` 顺序：Bearer JWT → 已签名身份头 → guest

- [ ] **Step 1: 写失败测试**

```python
async def test_forged_identity_headers_are_ignored(monkeypatch):
    monkeypatch.setattr(auth.settings, "trust_identity_headers", False)
    p = await auth.get_principal(authorization=None, x_user_id="u1", x_user_role="admin", x_tenant="default")
    assert p.role == "customer"

async def test_signed_headers_are_trusted(monkeypatch):
    monkeypatch.setattr(auth.settings, "trust_identity_headers", True)
    ts = str(int(time.time()))
    sig = auth.create_internal_signature(user_id="u1", role="operator", tenant="default", ts=ts, request_id="r1", secret="s")
    p = await auth.get_principal(authorization=None, x_user_id="u1", x_user_role="operator",
                                 x_tenant="default", x_internal_signature=f"{ts}.{sig}", x_request_id="r1")
    assert p.role == "operator"

async def test_stale_signature_is_rejected(monkeypatch):
    ...  # ts 为 60 秒前，断言 ApiError("UNAUTHORIZED")
```

- [ ] **Step 2: 运行并确认失败**

运行：`.venv/Scripts/python -m pytest tests/test_auth_principal.py -q`
预期：`AttributeError`（函数不存在）

- [ ] **Step 3: 实现签名校验**

- 配置新增：`internal_signing_secret: str = ""`、`trust_identity_headers: bool = False`（`env` 已在 Task 2 添加）。
- 签名内容 `f"{user_id}|{role}|{tenant}|{ts}|{request_id}"`，`hmac.new(secret.encode(), msg.encode(), sha256).hexdigest()`；比较用 `hmac.compare_digest`。
- `get_principal` 增加 `x_internal_signature`、`x_request_id` 两个 Header；签名缺失或过期一律 `UNAUTHORIZED`，不回退到未签名头。
- 保持 `Principal.require_staff()` 行为不变。

- [ ] **Step 4: 运行测试**

运行：`.venv/Scripts/python -m pytest tests/test_auth_principal.py -q`
预期：3 passed

- [ ] **Step 5: 提交**

```bash
git add packages/shop_common tests/test_auth_principal.py
git commit -m "feat(common): 增加内网身份签名校验"
```

---

### Task 4: 网关接入 identity 并移除自选角色签发

**Files:**
- Create: `tests/test_gateway_auth.py`
- Modify: `services/gateway/app.py`
- Modify: `packages/shop_common/config.py`（`identity_url`）

**Interfaces:**
- Consumes: Task 1/2 的 `/auth/*`、Task 3 的签名函数
- Produces:
  - `POST /api/auth/register|login|refresh|logout` 代理到 identity，登录/刷新原样转发 `Set-Cookie`
  - `GET /api/me` 代理 identity
  - `_forward(principal, request_id) -> dict[str, str]` 返回 `X-User-*` + `X-Tenant` + `X-Internal-Signature` + `X-Request-Id`
  - `POST /api/auth/token` 仅在 `settings.env == "dev" and settings.auth_dev_mode` 时存在，其他情况 404

- [ ] **Step 1: 写失败测试**

```python
def test_dev_token_disabled_outside_dev(monkeypatch):
    monkeypatch.setattr(gateway_app.settings, "env", "prod")
    assert gateway_app._dev_token_enabled() is False

def test_forward_carries_signature():
    p = Principal("u1", "operator", "default")
    headers = gateway_app._forward(p, request_id="r1")
    assert headers["X-Internal-Signature"].startswith(f"{int(time.time())}.")
    assert headers["X-User-Role"] == "operator"
```

- [ ] **Step 2: 运行并确认失败**

运行：`.venv/Scripts/python -m pytest tests/test_gateway_auth.py -q`
预期：`AttributeError`

- [ ] **Step 3: 实现网关改造**

- 删除硬编码的 `TokenRequest` 语义：改为 `register/login/refresh/logout/me` 五个代理函数，统一 `httpx.AsyncClient(timeout=10)`，把 `Set-Cookie` 头原样回传（只挑 `set-cookie`）。
- 代理失败映射沿用现有 `ApiError(payload["code"], payload["message"], details)` 模式。
- `_forward` 增加 `request_id` 参数（取 `X-Request-Id`，缺省 `uuid4`），签名密钥取 `settings.internal_signing_secret`；**所有既有 `_forward(principal)` 调用点都要改为 `_forward(principal, request_id)`**。
- refresh 代理必须原样转发 `Cookie` 与 `X-CSRF-Token` 头；登录/刷新响应只透传 `set-cookie`，不复制 content-length 等 hop-by-hop 头。
- 限流白名单从 `/api/auth/token` 改为 `/api/auth/login|register|refresh`：登录按 IP 更严格（`10/min`），避免暴力破解。
- 保留 dev 模式 token 接口用于本地演示，但必须在 `env=dev` 且显式 `AUTH_DEV_MODE=true`；否则 404。
- 配置新增 `identity_url: str = "http://localhost:8007"`、`auth_dev_mode: bool = False`。
- 禁止网关再读取 `order_db`（仅配置清理，见 Task 6）。

- [ ] **Step 4: 运行测试**

运行：`.venv/Scripts/python -m pytest tests/test_gateway_auth.py -q`
预期：2 passed

- [ ] **Step 5: 提交**

```bash
git add services/gateway packages/shop_common/config.py tests/test_gateway_auth.py
git commit -m "feat(gateway): 接入账号服务并关闭自选角色签发"
```

---

### Task 5: 前端登录、刷新与退出

**Files:**
- Modify: `web/src/lib/api.ts`
- Modify: `web/src/App.tsx`
- Verify: `cd web && npm run build`

**Interfaces:**
- Consumes: `/api/auth/register|login|refresh|logout|me`
- Produces:
  - `login(phone: string, password: string): Promise<Session>`
  - `register(phone: string, password: string): Promise<void>`
  - `refreshSession(): Promise<Session | null>`
  - `logout(): Promise<void>`
  - `request<T>(path, options)` 对 401 只重试一次刷新

- [ ] **Step 1: 改造 api.ts**

- `Session` 只保留 `{ user_id, role, phone }` 存 `localStorage`；access token 只放模块内存变量，刷新页面后调用 `refreshSession()` 重新获取。
- 所有请求 `credentials: "include"`；`login` 不再接收 role。
- `request` 遇 401：若未重试过则 `await refreshSession()` 后重放一次；刷新失败清空会话并抛原错误。
- 新增 `csrfToken(): string | null` 读取非 HttpOnly 的 `shop_csrf` Cookie；`refreshSession` 必须带 `X-CSRF-Token`，否则 identity 返回 403。
- `logout` 先调 `/auth/logout`，无论成功失败都清本地会话。

- [ ] **Step 2: 改造 App.tsx**

- 登录面板：手机号 + 密码，增加“注册”切换；删除角色下拉与“演示环境直接签发”提示。
- 登录成功后按服务端返回的 `role` 决定是否跳 `/ops`；前端不再传角色。
- 应用挂载时调用一次 `refreshSession()` 恢复会话（access token 只在内存，刷新页面后会丢失）；恢复失败不跳转、不弹错误，保持未登录状态。
- 退出调用异步 `logout()`。

- [ ] **Step 3: 构建验证**

运行：`cd web && npm run build`
预期：`vite build` 成功，无 TS 错误

- [ ] **Step 4: 提交**

```bash
git add web/src/lib/api.ts web/src/App.tsx
git commit -m "feat(web): 接入账号登录、注册与令牌刷新"
```

---

### Task 6: 部署配置（Compose）

**Files:**
- Create: `docker-compose.prod.yml`
- Modify: `docker-compose.yml`
- Modify: `infra/postgres/init.sql`
- Modify: `.env.example`

**Interfaces:**
- Consumes: identity 镜像（复用 `infra/Dockerfile`，`SERVICE_NAME=identity`）
- Produces：`identity` 服务（内网 `8000`，宿主机 `8007` 仅本地映射）；`identity_db`；网关 `IDENTITY_URL=http://identity:8000`

- [ ] **Step 1: 更新本地编排**

- `init.sql` 增加 `CREATE DATABASE identity_db;` 并 `\connect identity_db` 建 `pgcrypto`。
- `docker-compose.yml` 新增 `identity` 服务，`DATABASE_URL=...@postgres:5432/identity_db`，依赖 postgres/redis 健康；本地映射 `8007:8000`。
- `gateway` 增加 `IDENTITY_URL: http://identity:8000`、`depends_on.identity: service_healthy`；删除 gateway 的 `DATABASE_URL`（不再直连 order_db）。
- 公共 env 增加 `INTERNAL_SIGNING_SECRET`（dev 用固定值，prod 必须注入）、`TRUST_IDENTITY_HEADERS`（dev 为 `true`）。

- [ ] **Step 2: 新增生产 overlay 骨架**

- `docker-compose.prod.yml` 只包含生产差异：`backend` internal 网络、`restart: unless-stopped`、资源上限、日志轮转、`TRUST_IDENTITY_HEADERS=false`、`AUTH_DEV_MODE=false`。
- 内网服务用 `ports: !reset []` 清空本地端口映射；`gateway` 与 `web` 保留对外端口。
- 文件顶部注明需 Docker Compose v2.24+（`!reset` 语法），并注明双主机部署、LB 与托管 DB/Redis 在 M3 接入；M1 只验证单机 overlay 可解析。

- [ ] **Step 3: 校验编排**

运行：`docker compose -f docker-compose.yml -f docker-compose.prod.yml config > $null`
预期：无报错；`identity` 无对外端口，`gateway` 仍暴露

- [ ] **Step 4: 提交**

```bash
git add docker-compose.yml docker-compose.prod.yml infra/postgres/init.sql .env.example
git commit -m "chore(infra): 增加 identity 服务与生产编排"
```

---

### Task 7: 文档、验收与回归

**Files:**
- Modify: `README.md`（快速开始改为注册/登录）
- Modify: `docs/RUNBOOK.md`（登录排障与账号锁定）
- Create: `tests/test_permissions.py`

**Interfaces:**
- Consumes: Task 1–6 全部接口
- Produces：M1 验收证据（测试输出 + 构建结果）

- [ ] **Step 1: 写越权回归测试**

```python
async def test_customer_cannot_use_staff_action():
    p = Principal("u1", "customer", "default")
    with pytest.raises(ApiError) as exc:
        p.require_staff()
    assert exc.value.code == "FORBIDDEN"
```

- [ ] **Step 2: 更新文档**

- README 的 curl 验收改为：注册 → 登录取 Cookie → 带 Bearer 下单；删除 `POST /api/auth/token {user_id, role}`。
- RUNBOOK 增加：登录 429 → 查锁定窗口；refresh 401 → 重新登录；`Set-Cookie` 丢失 → 检查网关是否透传。

- [ ] **Step 3: 全量验证**

运行：

```bash
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff check packages services tests
cd web && npm run build
```

预期：pytest 全绿（原 34 + 新增 ≥10）、ruff 无告警、前端构建成功。

- [ ] **Step 4: 提交**

```bash
git add README.md docs/RUNBOOK.md tests/test_permissions.py
git commit -m "docs: 更新账号登录流程与排障说明"
```

---

## 不做（M1 明确排除）

- 支付服务与支付回调（M2）。
- audit 服务、Alembic 全量迁移、Prometheus（M3）。
- 邮箱/短信通知、密码找回、第三方 OAuth、MFA。
- 多租户隔离与角色管理后台（仅保留启动引导管理员）。

## 验收清单（M1 退出条件）

- [ ] FR-01：注册/登录可用，无效凭证不签发 token
- [ ] FR-02：顾客调用运营接口返回 403，伪造角色头无效
- [ ] NFR-05：密码 Argon2id、refresh 仅存哈希、日志无明文敏感信息、生产禁用 dev 换 token
- [ ] 前端刷新页面后能通过 Cookie 恢复会话
- [ ] pytest / ruff / web build 全绿
