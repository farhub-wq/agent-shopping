# 实现计划：拒付处理（Chargeback）

> 状态：待确认。PRD 见 `.ai/PRD_chargeback.md`。

## 1. 改动文件树

```
services/payment/models.py            (改) 新增 Chargeback 模型
migrations/payment/versions/0003_add_chargebacks.py  (新) 建表迁移
services/payment/app.py               (改) 拒付回调/查询/处置三接口 + 总账 + 事件 + 审计
services/payment/provider.py          (改) 协议注释说明拒付回调复用 verify_callback（无代码变更或仅注释）
tests/test_chargeback.py              (新) 拒付流程测试
web/src/lib/api.ts                    (改) Chargeback 类型 + listChargebacks/resolveChargeback
web/src/pages/Chargeback.tsx          (新) 拒付列表与处置页
web/src/pages/Ops.tsx                 (改) tabs 加「拒付」跳转链接
web/src/App.tsx                       (改) /chargebacks 路由
```

## 2. 技术方案

### 2.1 模型与迁移

`Chargeback` 模型（见 PRD 2），`provider_case_id` 唯一索引承担幂等。迁移 `0003` 用 `op.create_table`（参照 payment 现有 0001/0002 风格）。

### 2.2 拒付回调（幂等 + 落账）

`POST /payments/chargebacks/callback/{provider_name}`：

1. `provider.verify_callback` 验签（复用现有签名算法，校验时间窗 + HMAC）。
2. 解析 `provider_case_id`（缺省 400）、`provider_txn_id`/`payment_id`（缺省 400）、`amount_cents`（int，>0）。
3. 按 `provider_case_id` 查重 → 已存在直接返回幂等结果。
4. 按 `provider_txn_id` 或 `payment_id` 定位 `Payment`；不存在 404；`amount_cents`/`currency` 与支付单不一致 409；`status not in {paid, refunded}` 409；`amount_cents > payment.amount_cents` 409。
5. 事务内：`session.add(Chargeback(status="opened", ...))` + `post_transaction(txn_type="chargeback", entries=[("CHARGEBACK","debit",amt),("PROVIDER_RECEIVABLE","credit",amt)])` + `enqueue(payment.chargeback_opened)` + `enqueue_audit(payment.chargeback_opened)` + `commit`（`IntegrityError` → rollback 幂等返回）。

### 2.3 处置（won 反向冲正）

`POST /payments/chargebacks/{chargeback_id}/resolve`：

- `require_staff`；`outcome in {won, lost}` 否则 400。
- 已 `closed_at` 非空（非 opened）→ 幂等返回当前状态。
- `won`：`post_transaction(txn_type="chargeback", entries=[("PROVIDER_RECEIVABLE","debit",amt),("CHARGEBACK","credit",amt)])`（反向分录）。
- 置 `status=outcome`、`closed_at=now`；发 `payment.chargeback_resolved`；审计 `payment.chargeback_resolved`。

### 2.4 查询

`GET /payments/chargebacks`：`require_staff`；`status` 可选过滤；`page/size` 分页（参照 audit 服务分页写法）；`_to_dict` 显式挑字段，附 `order_id`（经 `payment.order_id` 关联，通过 `payment_id` join Payment 或冗余存储——本实现从 `Chargeback.payment_id` 反查 Payment 取 `order_id`，避免跨表冗余；查询用单条 `select(Payment).where(id.in_(...))` 批量取，遵循禁循环逐条查库）。

### 2.5 前端

- `api.ts`：`Chargeback { id, payment_id, order_id, provider_case_id, amount_cents, status, reason, opened_at, closed_at }`；`ChargebackPage`；`listChargebacks({status,page,size})`；`resolveChargeback(id, outcome)`。
- `Chargeback.tsx`：状态筛选 + 表格（金额用 `money()` 换算）+ 分页 + 处置按钮（opened 时显示「申诉成功/败诉」）。
- `Ops.tsx` tabs 末尾加 `<Link className="chip" to="/chargebacks">拒付</Link>`；`App.tsx` 加路由与导航（运营/管理员可见）。

## 3. 风险点与对策

| 风险 | 对策 |
|---|---|
| 重复拒付回调重复记账 | `provider_case_id` 唯一索引 + IntegrityError 回滚幂等 |
| 拒付金额超限/伪造 | 金额与币种对支付单校验，> 支付金额 409；验签失败 401 |
| 总账不平 | 复用 `post_transaction` 的借贷平衡断言；won 反向分录金额与 opened 一致 |
| won 反向分录导致 CHARGEBACK 科目为负 | 分录只用正数方向，不直接改历史行；科目余额由聚合决定，单笔借贷平衡即可 |
| 处置并发 | 处置按 `closed_at` 非空幂等；并发撞同单时以「已处置」幂等返回 |

## 4. 验证

1. `tests/test_chargeback.py`：回调落库 + 幂等；伪造签名/金额/非已付单拒绝；总账借贷平衡；won 反向冲正；lost 不额外记账；resolve 需 staff 且幂等；查询过滤分页。
2. 全量：pytest + ruff + web build 全绿。
3. 手工冒烟（dev）：mock 支付成功 → 构造签名拒付回调 → Ops/拒付页看到 → 处置 won/lost 看总账与审计。
