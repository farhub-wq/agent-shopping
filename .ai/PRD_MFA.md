# PRD：管理员 MFA（TOTP）

> 状态：待确认。对应 system-design.md M3「管理员 MFA（TOTP）」。
> 范围：仅 admin 角色可启用/关闭；启用后登录必须通过二次验证。

## 1. 业务上下文

自营电商的 admin 账号拥有全部权限（退款审批、库存盘点、审计查询等），是资损与越权的最高价值目标。当前 admin 登录仅凭「手机号 + 密码」单因素，一旦密码泄露（撞库、钓鱼、口令复用）即可完整接管后台。M3 要求为 admin 增加 TOTP 双因素认证：启用后即使密码泄露，攻击者无动态验证码也无法登录。

约束与现状：

- admin 账号由 bootstrap 或运营手动授予 `admin` 角色，数量少（1–3 人），MFA 为**可选增强**而非强制（首个 admin 无任何已绑定验证器，无法自举强制）。
- 登录与令牌签发集中在 `services/identity`，其他服务只认 JWT / 签名身份头，因此 MFA 只改 identity 与前端登录面板，不动其他服务。
- 审计服务（模块一）已就绪，MFA 绑定/关闭属敏感操作，需埋审计点。

## 2. 数据模型

`users` 表新增三列（Alembic 迁移 `migrations/identity/versions/0003_*.py`）：

| 列 | 类型 | 说明 |
|---|---|---|
| `totp_secret` | String(512) nullable | Fernet 加密后的 TOTP 共享密钥；非空表示已发起绑定 |
| `totp_enabled` | Boolean default False | 是否已确认生效；登录仅当 `totp_enabled=true` 时要求二次验证 |
| `last_totp_step` | Integer nullable | 最近一次成功验证的时间步，防同一验证码在窗口内重放 |

设计要点：

- `totp_secret` 用 Fernet 对称加密落库（密钥来自环境变量 `MFA_ENCRYPTION_KEY`），满足「密钥不入库」的 NFR-05 精神；明文 secret 仅在 enroll 响应中一次性返回给用户展示二维码，之后不再出现。
- 绑定分两步（enroll 生成并暂存 → confirm 校验通过置 `totp_enabled=true`），避免用户未扫码成功就被锁在登录外。
- 重放防护：TOTP 验证码在 30s 窗口内有效，记录匹配的 `time_step`；同一用户用相同 step 的验证码二次登录被拒绝。

## 3. 接口契约

全部挂在 `services/identity`，错误沿用 `ApiError` 统一出口；新增错误码 `MFA_REQUIRED`（映射 HTTP 401）。

### 3.1 登录（改造 `POST /auth/login`）

请求体新增可选字段 `otp: str = Field(default="", pattern=r"^\d{6}$")`。

- 密码正确、用户未启用 MFA → 照常签发 session（行为不变）。
- 密码正确、用户已启用 MFA 且未提供 `otp` → 返回 401 `{"code":"MFA_REQUIRED","message":"需要动态验证码"}`，不签发 session、不写 refresh、不计失败次数。
- 密码正确、提供了 `otp` → 校验动态码：
  - 匹配且 step 不等于 `last_totp_step` → 签发 session，更新 `last_totp_step`。
  - 不匹配 / step 重复 → 计入登录失败计数（复用 `LoginAttempt` 限流），返回 401。

### 3.2 绑定与关闭（均需 admin 鉴权，`principal.require_admin()`）

| 接口 | 说明 |
|---|---|
| `POST /auth/mfa/enroll` | 已启用时返回 409；否则生成 20 字节 secret，加密落库（`totp_enabled=false`），返回 `{otpauth_url, secret}` |
| `POST /auth/mfa/confirm` | 请求体 `{code}`；用暂存的 secret 校验，通过置 `totp_enabled=true`，否则 401 |
| `POST /auth/mfa/disable` | 请求体 `{password}`；重新校验密码（防已登录会话被劫持后直接关 MFA），通过后清空 secret、置 `totp_enabled=false`、清 `last_totp_step` |

审计埋点：enroll 成功 → `identity.mfa_enroll`；confirm 成功 → `identity.mfa_enabled`；disable 成功 → `identity.mfa_disabled`（复用 `enqueue_audit`，target_type="user"、target_id=user.id、tenant=user.tenant_id）。

### 3.3 配置

`shop_common.config` 新增 `mfa_encryption_key: str`（Fernet 32 字节 urlsafe-base64；dev 有默认值，生产必须注入）。`docker-compose.yml` 与 `.env.example` 同步补 `MFA_ENCRYPTION_KEY`。

## 4. 前端要求

- `web/src/lib/api.ts`：`login` 增加可选 `otp` 参数；新增 `enrollMfa()` / `confirmMfa(code)` / `disableMfa(password)`；`login` 需能区分 `MFA_REQUIRED`（读响应 `code`，抛可识别的错误类型）。
- `web/src/App.tsx` 登录面板：登录返回 `MFA_REQUIRED` 时展示「动态验证码」输入框（6 位），用户输入后带 `otp` 重试登录。
- 新增 `web/src/pages/Mfa.tsx`（仅 admin 可见的路由 `/mfa` + 导航项）：展示绑定入口、二维码（otpauth 协议转内联二维码或提供 secret 手动输入）、确认/关闭表单。页面只渲染与调用 api.ts，禁止组件内 fetch、禁止硬编码 `/api` 路径。

## 5. 验收标准

1. 未启用 MFA 的 admin：登录行为与改造前完全一致（不要求验证码）。
2. 启用 MFA 后：仅密码登录返回 401 + `MFA_REQUIRED`，不签发 session；密码 + 正确验证码登录成功。
3. 错误验证码登录失败，连续失败计入登录锁定（复用现有限流）。
4. 同一验证码在时间窗口内二次使用被拒绝（重放防护）。
5. `totp_secret` 在数据库中以密文存储，明文 secret 只出现在 enroll 响应一次；任何日志/审计/`/me` 响应不含 secret。
6. 绑定/确认/关闭均要求 admin 身份，且关闭需重新校验密码；全程有审计留痕（`identity.mfa_*`）。
7. `enroll` 未扫码确认前（`totp_enabled=false`）不阻塞登录；重复 `enroll` 已启用时返回 409。
8. 全量验证绿：`.venv/Scripts/python -m pytest -q`、`.venv/Scripts/python -m ruff check packages services tests`、`cd web && npm run build`。
