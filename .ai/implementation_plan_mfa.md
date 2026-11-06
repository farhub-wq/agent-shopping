# 实现计划：管理员 MFA（TOTP）

> 状态：待确认。PRD 见 `.ai/PRD_MFA.md`。

## 1. 改动文件树

```
packages/shop_common/config.py        (改) 新增 mfa_encryption_key 配置
packages/shop_common/errors.py        (改) ApiError.status_by_code 增加 MFA_REQUIRED→401
services/identity/totp.py             (新) RFC 6238 TOTP 生成/校验/step 匹配
services/identity/security.py         (改) 新增 Fernet encrypt_secret/decrypt_secret
services/identity/models.py           (改) User 表加 totp_secret/totp_enabled/last_totp_step
services/identity/app.py              (改) login 加 otp 参数；新增 /auth/mfa/{enroll,confirm,disable}
migrations/identity/versions/0003_add_mfa_columns.py  (新) 三列迁移
tests/test_mfa.py                     (新) MFA 绑定/登录/重放/关闭测试
web/src/lib/api.ts                    (改) login 加 otp；新增 enrollMfa/confirmMfa/disableMfa
web/src/pages/Mfa.tsx                 (新) 管理员 MFA 设置页
web/src/App.tsx                       (改) 登录面板两步验证 + /mfa 路由与导航
docker-compose.yml                    (改) MFA_ENCRYPTION_KEY 注入
.env.example                          (改) 补 MFA_ENCRYPTION_KEY
```

## 2. 技术方案

### 2.1 TOTP 自研（不引入 pyotp）

`services/identity/totp.py` 实现 RFC 6238（约 40 行，全部可单测）：

- `new_secret()` → `base64.b32encode(os.urandom(20))`。
- `otpauth_url(secret, account)` → `otpauth://totp/shop-agent:{account}?secret=...&issuer=shop-agent&algorithm=SHA1&digits=6&period=30`。
- `verify(secret, code, now)` → HMAC-SHA1 动态截断，窗口 ±1 步（共 3 步）；返回匹配的 `step` 或 `None`。

理由：算法是公开标准、代码量小、可完整单测、避免新增依赖及审查成本；自研能直接拿到匹配 step 支撑重放防护。

### 2.2 secret 加密存储

`security.py` 用 `cryptography.fernet.Fernet`（cryptography 已在依赖中）：`encrypt_secret/decrypt_secret`，密钥来自 `settings.mfa_encryption_key`。enroll 生成的明文 secret 只在该次响应返回，落库与后续校验均用密文。

### 2.3 登录两步流程

`login` 密码验证通过后：

1. `totp_enabled=False` → 直接签发（行为不变）。
2. `totp_enabled=True` 且未提供 `otp` → `ApiError("MFA_REQUIRED", "需要动态验证码")`，不写 refresh、不计失败。
3. 提供 `otp` → 解密 secret 后 `verify`；失败或 step 与 `last_totp_step` 相同 → `_record_failure` 计失败 + 401；成功 → 更新 `last_totp_step`，照常签发。

### 2.4 管理接口

- `enroll`：`require_admin()`；已启用 → 409；生成 secret → 加密写 `totp_secret`（`totp_enabled=False`）→ 审计 `identity.mfa_enroll` → 返回 otpauth_url + 明文 secret。
- `confirm`：`require_admin()`；无 secret → 409；`verify` 通过 → `totp_enabled=True` + `last_totp_step=step` → 审计 `identity.mfa_enabled`；失败 401。
- `disable`：`require_admin()`；重新 `verify_password` 校验密码，错误 401；通过 → 清三列 → 审计 `identity.mfa_disabled`。

### 2.5 前端

- `api.ts`：`login(phone, password, otp?)`；`readError` 升级为可携带 `code`，新增 `ApiRequestError`（含 code 字段）供登录面板判断 `MFA_REQUIRED`；新增三个 MFA 函数走统一 `request()`。
- `App.tsx`：登录面板 state 增加 `mfaPending`，收到 `MFA_REQUIRED` 时切到验证码输入，重试 `login(phone, password, otp)`。
- `Mfa.tsx`：enroll 后展示 `secret`（手动录入）与 otpauth 链接；confirm/disable 表单；导航仅 admin 可见。

## 3. 风险点与对策

| 风险 | 对策 |
|---|---|
| 自研 TOTP 的边界/截断 bug | 用 RFC 6238 官方测试向量（`totp.py` 单测固定 secret 与时间，比对 6 位码） |
| 管理员丢验证器被锁死 | 提供 `disable`（需密码）+ 线下重置路径（运维直接改库清三列，RUNBOOK 补一条）；不强制启用 |
| Fernet 密钥丢失/轮换 | dev 默认、生产强制注入；密钥丢失则已绑定用户需运维重置（明文 secret 无法恢复，符合安全预期） |
| 时钟偏移 | 窗口 ±1 步（±30s），覆盖常见 NTP 偏差 |
| 验证码暴力破解 | 复用 `LoginAttempt`（phone+ip）限流；错误码也计入失败次数 |
| 已登录会话被劫持后关 MFA | `disable` 强制重新校验密码 |

## 4. 验证

1. `tests/test_mfa.py`：未启用不要求码；启用后仅密码 401/MFA_REQUIRED；密码+正确码成功；错误码失败并计锁定；同 step 重放拒绝；enroll/confirm/disable 权限与密码校验；secret 密文落库。
2. `tests/test_totp.py`（或并入 test_mfa.py）：RFC 6238 测试向量。
3. 全量：pytest + ruff + web build 全绿。
4. 手工冒烟（dev 起 identity + 前端）：bootstrap admin 登录 → 绑定 → 退出重登需验证码 → 关闭恢复单因素。
