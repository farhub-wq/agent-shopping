# M3 生产加固实施计划（代码项）

> 状态：**待人工审批**。审批前不动代码。
> 范围：audit 审计服务、管理员 MFA(TOTP)、拒付处理、可观测与告警（Prometheus+Grafana）四个可在本地实现并验证的模块。
> 排除：沙箱独立 runner、双主机+托管 DB HA、备份恢复演练三个部署/运维项，另立计划。
> 已完成不重复：事件 DLQ/重放、relay 锁、任务锁、Alembic。

## 0. 总则

- 按 AGENTS.md 分阶段推进：每个模块「后端完成 → pytest/ruff/web build 全绿 → 停下等确认」再进下一模块。
- 每模块独立 PRD（业务上下文 / 数据模型 / 接口契约 / 前端要求 / 验收标准）+ 独立提交单元。
- 提交信息 `feat(...)` + 简体中文描述；不自动 commit/push，提交前展示变更摘要。
- 验证命令：`.venv/Scripts/python -m pytest -q`、`.venv/Scripts/python -m ruff check packages services tests`、`cd web && npm run build`。
- Schema 变更一律走 Alembic：新增 `migrations/<svc>/versions/`，禁止只改 models 靠 create_all。

---

## 模块一：audit 审计服务（FR-11「运营动作全部留痕」）

### 1.1 业务上下文
当前运营写操作仅靠 Redis `shop:audit` 短保留（gateway/app.py:368），不满足「运营动作可追溯、保留 180 天、只增不改、查询仅 admin」（设计 §8、§4.5）。目标：业务服务在写操作事务内以 outbox 写 `audit.{action}` 事件，新建 audit 服务消费落库，提供 admin 查询接口。

### 1.2 数据模型
新服务 `audit`（`audit_db`），单表：

| audit_log 列 | 类型 | 说明 |
|---|---|---|
| id | String(36) PK | uuid |
| occurred_at | UTCDateTime index | 事件时间（取事件 created_at，非消费时间） |
| service | String(32) | 来源服务名 |
| actor_id / actor_role | String(36)/String(16) | 操作人 |
| tenant_id | String(36) default | 租户 |
| action | String(64) index | 如 `order.ship`、`payment.refund`、`inventory.adjust` |
| target_type / target_id | String(32)/String(64) index | 操作对象 |
| request_id | String(64) index | 贯穿链路 |
| before / after | JSON nullable | 变更前后关键字段（脱敏后） |
| ip_hash | String(64) | 客户端 IP 哈希 |

只增不改，无 update/delete 路径；保留 180 天由后台清理任务处理（复用 `run_task_loop`，M3 只建表+落库，清理可后续加）。

### 1.3 接口契约
- 事件：topic `audit`，type `audit.{action}`，payload `{actor_id, actor_role, action, target_type, target_id, before, after, request_id, ip_hash}`。
- `shop_common/audit.py` 新增 `enqueue_audit(session, *, actor, action, target_type, target_id, before=None, after=None, request_id="", ip_hash="", tenant="default")` —— 内部封装 `enqueue(session, "audit", f"audit.{action}", {...})`，调用方同事务 commit。
- audit 服务消费组 `audit-svc`，消费 topic `audit`，按 type 过滤 `audit.` 前缀，落 `audit_log`；重复消费靠 `(event_id)` 幂等（ProcessedEvent 已兜底）+ 落库唯一约束。
- 查询：`GET /audit/logs?action=&target_type=&target_id=&actor_id=&page=&size=` → `{items, page, size}`，`principal.require_admin()`；响应显式挑字段。

**本次埋点范围（敏感写路径，逐服务）**：
- order：发货推进（`order.ship`）、取消（`order.cancel`）、完成（`order.complete`）。
- payment：退款（`payment.refund`）、拒付（`payment.chargeback`，见模块三）、支付回调（`payment.callback`）。
- inventory：入库/盘点调整（`inventory.adjust`）。
- fulfillment：发货（`fulfillment.ship`）、签收（`fulfillment.deliver`）。
- aftersales：审批通过/驳回（`aftersales.approve/reject`）。
- identity：管理员登录（`identity.admin_login`，ip_hash 必填）。
- harness：Agent 审批通过后的执行（`agent.execute_approved`）。

### 1.4 前端要求
- 新增 `web/src/pages/Audit.tsx`（仅 admin 可见）：筛选（action/target_type/target_id/actor_id）+ 分页列表。
- `web/src/lib/api.ts` 新增 `AuditLog` 类型与 `listAuditLogs()`；页面不出现 URL。
- `web/src/App.tsx` 路由与菜单挂载（admin 角色）。

### 1.5 验收标准
- Given 任一埋点写操作，When 完成，Then `audit_log` 出现一条记录，service/actor/action/target/request_id 正确，before/after 不泄漏密码/手机号明文。
- Given 重复投递同一 audit 事件，When 消费，Then 只落一条（幂等）。
- Given customer 身份，When 调 `GET /audit/logs`，Then 403；admin 可查。
- Given 写操作事务回滚，When 查询 audit，Then 无对应记录（outbox 同事务）。

### 1.6 改动文件树与提交
```
packages/shop_common/audit.py                 # 新增 enqueue_audit
services/audit/{__init__,models,app}.py        # 新增服务
migrations/audit/{alembic.ini,env.py,versions/0001_baseline.py}  # 新增
docker-compose.yml / .env.example / infra/mysql/init.sql  # 新增 audit 服务+库
services/{order,payment,inventory,fulfillment,aftersales,identity,harness}/app.py  # 埋点
gateway/app.py                                 # SERVICES 白名单 + 代理 /api/audit
web/src/lib/api.ts / web/src/pages/Audit.tsx / web/src/App.tsx  # 前端
tests/test_audit.py                            # 新增
pyproject.toml                                 # packages include 增加 audit*
```
提交：`feat(audit): 新增事件驱动审计服务与运营写操作埋点`（后端+迁移），`feat(web): 新增审计日志查询页`（前端）。

---

## 模块二：管理员 MFA（TOTP）（NFR-05 安全加固）

### 2.1 业务上下文
管理员账号是资金与权限的最高入口，需第二因子（TOTP，authenticator app 六位码）。顾客/运营不受影响。设计 §5.2 已定排 M3。

### 2.2 数据模型
`identity` 的 `User` 表新增两列（可空，MFA 未开启时不影响既有登录）：
- `totp_secret: String(32) nullable`（base32 种子，仅存密文种子本身；不落日志）
- `totp_enabled_at: UTCDateTime nullable`
- 迁移 `migrations/identity/versions/0002_add_totp.py`。

### 2.3 接口契约（identity 服务，均走网关代理）
- `POST /auth/mfa/setup`（登录态，staff 且非 admin 亦可自助开启）：生成 `pyotp.random_base32()`，返回 `{secret, otpauth_uri, expires_in}`；种子暂存需二次验证，不入库直到 verify 成功（V1 用短时签名 token 承载待激活种子，避免裸存）。
- `POST /auth/mfa/verify`：`{code}` 校验 TOTP 后把 `totp_secret/totp_enabled_at` 落库；失败 401，成功返回 `{enabled: true}`。
- `POST /auth/mfa/disable`：`{code, password}` 二次确认后清空两列。
- 登录改造：`POST /auth/login` 密码通过后，若 `user.totp_enabled_at` 非空 → 返回 `{mfa_required: true, mfa_token}`（`mfa_token` 为 scope=mfa、5 分钟短时 JWT，不含角色），不签发 access/refresh、不 Set-Cookie。
- 新增 `POST /auth/mfa/login`：`{mfa_token, code}` → 校验 TOTP + mfa_token → 走原 `_session_body` + Set-Cookie 完整登录。

**依赖**：`pyproject.toml` 增加 `pyotp>=2.9.0`。

### 2.4 前端要求
- 登录面板（`App.tsx`）：登录响应 `mfa_required` 时切换为 TOTP 输入步骤，提交 `/auth/mfa/login`。
- 新增管理员「安全设置」入口：展示 QR（用 `otpauth_uri` 生成）、输入 6 位码开启、关闭入口。
- `api.ts` 新增 `mfaSetup/mfaVerify/mfaLogin/mfaDisable` 类型与函数。

### 2.5 验收标准
- Given 已开 MFA 的 admin，When 密码正确登录，Then 返回 `mfa_required` 且不签发会话；TOTP 码正确后拿到会话。
- Given 错误 TOTP，When 连续尝试，Then 401；mfa_token 过期（>5 分钟）后 TOTP 正确也 401。
- Given 未开 MFA 的用户，When 登录，Then 行为与现在完全一致。
- Given customer，When 调 `/auth/mfa/*`，Then 拒绝或仅影响本人（禁用需 admin/本人）。
- 日志不出现 totp_secret；setup 未 verify 不落库（verify 前重启不产生残留种子）。

### 2.6 改动文件树与提交
```
pyproject.toml                                  # pyotp
services/identity/{app.py,models.py,security.py,tokens.py}  # TOTP + 登录分流
migrations/identity/versions/0002_add_totp.py
gateway/app.py                                  # /api/auth/mfa/* 代理 + 限流
web/src/lib/api.ts / web/src/App.tsx            # TOTP 登录步骤 + 安全设置
tests/test_identity_mfa.py
```
提交：`feat(identity): 管理员 TOTP 双因子登录`，`feat(web): 登录 TOTP 步骤与 MFA 设置页`。

---

## 模块三：拒付处理（chargeback）

### 3.1 业务上下文
渠道对已结算资金发起拒付时，需记录拒付单、走总账（借记 CHARGEBACK 损失 / 贷记 PROVIDER_RECEIVABLE 应收）、支付单进入拒付状态，并支持运营登记处置结果。分录模板已定义（design §5、ledger.py ACCOUNT_CODES 已有 `CHARGEBACK`）。

### 3.2 数据模型
`payment` 新增 `chargebacks` 表（design §3）：
| chargebacks 列 | 类型 | 说明 |
|---|---|---|
| id | String(36) PK | |
| payment_id | String(36) index | 关联支付单 |
| provider_case_id | String(64) UNIQUE | 渠道案件号，幂等键 |
| amount_cents | BigInteger | 拒付金额 |
| status | String(16) | `open` / `merchant_won` / `customer_won` |
| opened_at / closed_at | UTCDateTime | |
- `Payment.status` 枚举扩展 `chargeback`（收到拒付后置位；`merchant_won` 时回 `paid`）。
- 迁移 `migrations/payment/versions/0003_add_chargebacks.py`。

### 3.3 接口契约（payment 服务）
- `POST /payments/chargebacks`（staff）：`{payment_id, provider_case_id, amount_cents}` → 按 `provider_case_id` 幂等；写 Chargeback(open) + `post_transaction(txn_type="chargeback", entries=[("CHARGEBACK","debit",amt),("PROVIDER_RECEIVABLE","credit",amt)])` + `payment.status="chargeback"` + outbox `payment.chargeback_opened` + `enqueue_audit`。
- `POST /payments/chargebacks/{id}/resolve`（staff）：`{outcome}` → `merchant_won` 时反向分录 `[("PROVIDER_RECEIVABLE","debit",amt),("CHARGEBACK","credit",amt)]` + `payment.status="paid"`；`customer_won` 时不动账 + `payment.status="refunded"`；写 closed_at、outbox `payment.chargeback_resolved` + audit。
- `GET /payments/chargebacks?status=&page=&size=`（staff）→ 列表。
- 金额校验：拒付金额 ≤ 支付金额，超限拒绝。

### 3.4 前端要求
- 运营后台（`Ops.tsx` 或新 `Chargebacks.tsx`）新增「拒付单」列表：状态筛选、登记拒付、处置结果。
- `api.ts` 新增类型与 `listChargebacks/createChargeback/resolveChargeback`。

### 3.5 验收标准
- Given 渠道拒付通知，When 登记，Then chargeback 落库、总账借贷平衡、payment 进 chargeback。
- Given 重复 `provider_case_id`，When 登记，Then 幂等返回不重复出账。
- Given `merchant_won`，When resolve，Then 反向分录后总账仍平衡、payment 回 paid。
- Given 拒付金额 > 支付金额，When 登记，Then 409。
- Given 任一拒付写操作，When 完成，Then audit_log 有 `payment.chargeback*` 记录。

### 3.6 改动文件树与提交
```
services/payment/{app.py,models.py,ledger.py}   # chargebacks 表+流程+分录
migrations/payment/versions/0003_add_chargebacks.py
web/src/lib/api.ts / web/src/pages/Chargebacks.tsx / web/src/App.tsx
tests/test_chargeback.py
```
提交：`feat(payment): 拒付登记与总账处置流程`，`feat(web): 拒付单运营后台页`。

---

## 模块四：可观测与告警（Prometheus + Grafana）（NFR-06）

### 4.1 业务上下文
各服务目前只有 `/healthz`，无指标、无告警。目标：各服务暴露 `/metrics`（Prometheus 文本格式），Prometheus 抓取 + 告警规则 + Grafana 面板，覆盖设计 §10 的指标表。

### 4.2 数据模型（无 DB 变更）
纯运行时指标，无表结构变更。

### 4.3 接口契约
- 依赖：`prometheus-client>=0.20.0`。
- `shop_common/metrics.py` 新增：单例 `Metrics` 封装（Counter/Gauge/Histogram 注册表），`http_requests_total{service,status}`、`http_request_duration_seconds{service}`；`MetricsMiddleware` 挂在各服务 app。
- 业务指标埋点（按设计 §10）：
  - `checkout_total{result}`：order 下单成功/失败。
  - `payment_callback_total{result}`：payment 回调验签成功/失败。
  - `order_stuck_total`：order 扫描（PAYING 超支付窗口 / RESERVED 超 5 分钟）gauge。
  - `outbox_lag_seconds`：relay 每轮计算 oldest pending created_at 距 now。
  - `db_pool_exhausted_total`：连接池满计数器（在 http client/engine 处接入，V1 以日志事件计数，若接入成本高则记录为后续项）。
  - `inventory_negative_total`：库存变负计数器（inventory Lua 脚本后置校验）。
  - `ledger_imbalance_total`：payment 对账 `ledger_balanced=false` 计数。
  - `reconciliation_diff_total`：对账差异条数 gauge。
  - `agent_tool_denied_total`：harness 工具拒绝计数。
- 各服务新增 `GET /metrics`（复用 `MetricsMiddleware` 的 registry，不鉴权内网；网关不代理 `/metrics`，Prometheus 直连各服务内网端口）。

### 4.4 前端要求
无（Grafana 面板由 provisioning 提供）。

### 4.5 验收标准
- Given 任一服务，When `GET /metrics`，Then 返回含 `http_requests_total`、`http_request_duration_seconds` 的文本。
- Given 一次 5xx 响应，When 抓取，Then `http_requests_total{status="5xx"}` 递增。
- Given 对账差异 >0，When 抓取 payment /metrics，Then `reconciliation_diff_total` 反映差异数。
- Given compose up，When 访问 Grafana，Then Prometheus 数据源可用、面板可渲染（本地验证）。
- 告警规则文件覆盖：5xx>1%、callback 验签失败>0、outbox_lag>10s、inventory_negative>0、ledger_imbalance>0。

### 4.6 改动文件树与提交
```
pyproject.toml                                  # prometheus-client
packages/shop_common/metrics.py                 # 新增 Metrics + Middleware
services/*/app.py                               # 挂中间件 + /metrics + 业务指标埋点
infra/prometheus/{prometheus.yml,alerts.yml}    # 新增
infra/grafana/{provisioning/...}                # 新增 datasource + dashboard
docker-compose.yml / .env.example               # prometheus + grafana 服务
tests/test_metrics.py                           # 新增
```
提交：`feat(common): Prometheus 指标与各服务 /metrics`，`chore(infra): Prometheus+Grafana 编排与告警规则`。

---

## 5. 不做（本次明确排除）

- 沙箱独立 runner、双主机+托管 DB HA、备份恢复演练（另立部署计划）。
- audit_log 180 天清理任务（先建表落库，清理后台任务后续加）。
- 通知/报表（FR-15/16，属 Should）。
- Prometheus 高可用/远端存储、Tracing（采样 10% 全链路 trace，属后续）。
- 拒付证据上传、自动争议应答（V1 只登记+处置+对账）。

## 6. 全局风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| audit 埋点跨 7 服务，改动面大 | 回归风险 | 埋点只加 enqueue 一行，不改业务逻辑；pytest 全量回归 |
| TOTP 登录分流破坏现有登录 | 登录不可用 | 未开 MFA 用户路径零改动；新增测试覆盖两分支 |
| 拒付分录方向错 | 账不平 | 复用 `post_transaction` 借贷平衡不变量 + 单测断言 |
| 指标埋点遗漏/命名漂移 | 告警失效 | 指标名集中在 `metrics.py` 常量，告警规则与埋点同源核对 |
| prometheus-client 全局 registry 冲突 | 指标错乱 | 每进程单例，测试用独立 registry fixture |
