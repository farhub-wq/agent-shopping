# M2 资金与订单实施计划

> 目标：把 mock 支付替换为独立 `payment` 服务，补齐复式总账、回调幂等、超时关单、部分发货、部分退款与日终对账。
> 依据：[system-design.md](docs/design/system-design.md) 4.2、5.2–5.4、6、12；M1 已完成，见 [implementation_plan.md](implementation_plan.md)。
> 渠道：本轮使用 Mock 聚合支付渠道（Provider 抽象 + HMAC 签名回调），真实渠道后续只替换 Provider 实现。

## 全局约束

- 金额一律 `amount_cents` 整数，禁止浮点；每个资金交易借贷合计必须相等。
- 支付回调、退款、释放库存全部按业务键幂等；重复回调返回 2xx 且不重复记账。
- 已入账分录只增不改，冲正走反向分录。
- 状态变更仍只经 `resolve_target`；支付/退款事件只由 outbox 投递。
- 注释用简体中文，说明决策点；提交按 `feat(payment)` / `feat(order)` / `feat(fulfillment)` / `feat(aftersales)` 拆分，不自动 push。

## Task 1: payment 服务与复式总账

- `services/payment/models.py`：`Payment`、`Refund`、`Settlement`、`LedgerTransaction`、`LedgerEntry`、`ReconciliationDiff`。
- `services/payment/ledger.py`：账户白名单 `CASH/PROVIDER_RECEIVABLE/REVENUE/REFUND/FEE`，写入前校验借贷平衡。
- `services/payment/provider.py`：Provider 接口 + `MockProvider`（建单、验签、退款、渠道账单）。
- `services/payment/app.py`：
  - `POST /payments`：按 `order_id` 幂等建支付单，返回 `payment_id/pay_url/expires_at`。
  - `POST /payments/callback/{provider}`：校验 HMAC 与时间窗，按 `provider_txn_id` 去重；同事务写支付状态、分录、outbox `payment.succeeded`。
  - `GET /payments/cashier/{id}` 与 `POST /payments/cashier/{id}/pay`：仅 dev + `PAYMENT_MOCK_MODE=true` 提供本地收银台。
- 测试：伪造签名、重复回调、金额不符、分录不平。

## Task 2: 订单支付链路与超时关单

- `orders` 增加 `payment_id/pay_url/expires_at/payment_status`；下单预占成功后创建支付单并流转 `RESERVED → PAYING`。
- 订单消费 `payment.succeeded/failed/expired`：成功 `PAYING → PAID` 并提交库存；失败回到 `RESERVED`；过期取消并释放库存。
- payment 后台扫描 `expires_at` 过期支付单，写 `payment.expired`。
- 删除订单服务内的 mock 支付；前端改用 payment 收银台接口。

## Task 3: 部分发货

- `fulfillment` 增加 `shipment_items`；同一订单允许多个 `shipments`。
- `POST /shipments/{id}/ship` 支持传 `items`：未发货部分自动拆到新的 pending 履约单。
- 订单按 item 聚合 `shipped_qty`；全部发完才 `PAID → SHIPPED`，全部签收才 `SHIPPED → COMPLETED`。
- 不变量：`sum(shipment_items.qty) ≤ order_items.qty`。

## Task 4: 部分退款

- 售后工单支持 `order_item_id` 与 `quantity`；审批金额不得超过 item 可退额（已付 − 已退）。
- 审批通过后调用 payment `POST /payments/refunds`（按 `ticket_id` 幂等），写退款分录与 outbox `payment.refunded`。
- 订单消费 `payment.refunded`：累加 item 已退数量/金额；全额退完 `REFUNDED`，部分退款回到退款前状态。
- `REFUNDING → refund_partial_complete` 由状态机放行，回退依据 `refund_previous`。

## Task 5: 日终对账

- `POST /payments/reconciliation`：按业务日拉渠道账单，与本地支付/退款逐笔比对。
- 差异写 `reconciliation_diffs`（missing_local / missing_provider / amount_mismatch），不自动重试资金操作。
- 校验总账借贷平衡；差异仅告警。

## Task 6: 网关、前端、编排与验证

- 网关：代理 `payments`，新增免鉴权回调入口 `/api/payments/callback/{provider}`，透传原始 body 与签名头。
- 前端：订单页按 `payment_id` 调 mock 收银台；运营页支持部分发货与 item 退款说明。
- Compose：新增 `payment` + `payment_db`，注入 `PAYMENT_WEBHOOK_SECRET`、`PAYMENT_MOCK_MODE`。
- 验证：`.venv/Scripts/python -m pytest -q`、`ruff check`、`cd web && npm run build`。

## 不做

- 真实聚合支付商户对接、渠道退款异步回调、拒付处理、自动清结算；这些依赖商户号与渠道文档，M3/M4 接入。
- 退货物流回传与仓库收货回补库存；本轮只做退款金额链路。
