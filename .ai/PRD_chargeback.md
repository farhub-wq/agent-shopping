# PRD：拒付处理（Chargeback）

> 状态：待确认。对应 system-design.md M3「拒付处理」。
> 范围：payment 服务闭环——渠道拒付回调落库、复式总账冲正、运营查询与处置（won/lost）；不改订单宏状态。

## 1. 业务上下文

顾客通过发卡行对某笔已支付交易发起争议（chargeback），渠道将已结算资金从商家扣回。拒付与退款是两条独立资金通道：退款由商家主动发起，拒付由持卡人/渠道强制发起，商家只能记录并决定是否申诉。

现状与约束：

- payment 已有支付回调（`paid/failed`）、退款（`refunds`）、复式总账（`post_transaction`）、日终对账、审计埋点，全部走「渠道是结算事实、内部账是业务事实」的既有约定。
- system-design 4.2 已预留 `chargebacks` 表（`id, payment_id, provider_case_id UNIQUE, amount_cents, status, opened_at, closed_at`）与分录模板 `CHARGEBACK / PROVIDER_RECEIVABLE`。
- 拒付是资金层事实，**不直接改订单宏状态**：订单退款/发货仍由售后与履约链路决定，拒付只把「应收减少、损失增加」记进总账并留痕，供运营查询与处置。

## 2. 数据模型

新增 `chargebacks` 表（Alembic 迁移 `migrations/payment/versions/0003_add_chargebacks.py`）：

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | String(36) PK | 拒付单号 |
| `tenant_id` | String(36) index | 租户隔离 |
| `payment_id` | String(36) index | 关联支付单 |
| `provider_case_id` | String(64) UNIQUE index | 渠道争议单号，幂等键 |
| `amount_cents` | BigInteger | 拒付金额，≤ 支付金额 |
| `currency` | String(8) | 币种 |
| `status` | String(16) index | `opened` / `won` / `lost` |
| `reason` | Text | 渠道给的拒付原因，默认空 |
| `opened_at` | UTCDateTime | 拒付发生时间 |
| `closed_at` | UTCDateTime nullable | 处置完成时间 |
| `created_at` | UTCDateTime | 落库时间 |

## 3. 接口契约

全部挂在 `services/payment`，复用 `ApiError` 统一出口与既有验签能力。

### 3.1 渠道拒付回调 `POST /payments/chargebacks/callback/{provider_name}`

请求为渠道签名回调（复用 `provider.verify_callback` 验签，签名算法不变）。报文字段：`provider_case_id`（必填，唯一）、`provider_txn_id`（定位原支付单，与 `payment_id` 二选一）、`amount_cents`、`reason`（可选）。

- 按 `provider_case_id` 查重：重复回调幂等返回当前状态。
- 按 `provider_txn_id`（或 `payment_id`）定位支付单；不存在 → 404；金额/币种与支付单不一致 → 409；支付单非 `paid/refunded` → 409（无资金可扣）。
- 拒付金额 > 支付金额 → 409。
- 落库 `status=opened` + 总账分录（借 `CHARGEBACK` / 贷 `PROVIDER_RECEIVABLE`）+ outbox 发 `payment.chargeback_opened` + 审计 `payment.chargeback_opened`（actor=channel）。

### 3.2 运营处置 `POST /payments/chargebacks/{chargeback_id}/resolve`

`require_staff()`；请求体 `{outcome: "won" | "lost"}`；已处置（非 `opened`）幂等返回当前状态。

- `won`（申诉成功，渠道退回资金）：反向分录（借 `PROVIDER_RECEIVABLE` / 贷 `CHARGEBACK`）。
- `lost`（败诉，损失确认）：不产生额外分录，保持 `opened` 时的损失借方。
- 二者都置 `closed_at`、发 `payment.chargeback_resolved` 事件、审计 `payment.chargeback_resolved`（actor=运营）。

### 3.3 运营查询 `GET /payments/chargebacks`

`require_staff()`；查询参数 `status`（可选）、`page`/`size` 分页；返回 `{items, page, size, total}`，`items` 显式挑字段（含 `payment_id`、`order_id` 便于关联）。

## 4. 前端要求

- `web/src/lib/api.ts`：新增 `Chargeback`、`ChargebackPage` 类型与 `listChargebacks(params)`、`resolveChargeback(id, outcome)`。
- 新增 `web/src/pages/Chargeback.tsx`（运营/管理员可见）：拒付列表（状态筛选 + 分页）+ 每行「申诉成功/败诉」处置按钮。
- `web/src/App.tsx` 加 `/chargebacks` 路由；`web/src/pages/Ops.tsx` 的 tabs 加一个跳转链接（与「Agent 工作台」同款 chip 链接）。

## 5. 验收标准

1. 渠道拒付回调验签通过后落 `chargebacks` 表，重复回调（同 `provider_case_id`）幂等不重复记账。
2. 拒付发生写入平衡分录（借 `CHARGEBACK` 贷 `PROVIDER_RECEIVABLE`，金额=拒付额），总账借贷平衡校验通过。
3. 处置 `won` 写入反向分录；`lost` 不产生额外分录；两者都更新 `closed_at` 并幂等。
4. 金额校验：拒付额 > 支付金额 → 409；伪造金额/签名 → 401/409；非 `paid/refunded` 支付单不可拒付。
5. 查询与处置均需 `require_staff`；处置后留审计 `payment.chargeback_resolved`（操作人可追）。
6. 拒付不改订单宏状态；`payment.chargeback_opened/resolved` 事件以 outbox 同事务入队。
7. 全量验证绿：`.venv/Scripts/python -m pytest -q`、`.venv/Scripts/python -m ruff check packages services tests`、`cd web && npm run build`。

## 6. 边界（V1 明确不做）

- 拒付与退款的交叉场景（同一支付单既退款又被拒付）不做联动封顶，靠日终对账差异暴露。
- 拒付暂不并入 `reconciliation` 的本地记录比对（后续增强）。
- 订单侧暂不消费 `payment.chargeback_*` 事件（事件已在 outbox，未来需要时再消费）。
