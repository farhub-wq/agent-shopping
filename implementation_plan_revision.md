# 设计修订落地实施计划（4 个代码单元）

> 依据：[system-design.md](docs/design/system-design.md) 修订版（2026-11-06）+ 已确认决策 Q1–Q36。
> 前置：设计文档已修订并冻结（ce2e416），本计划只覆盖**代码对齐**，不再评审已定决策。
> 验证：每个单元 `pytest + ruff + web build` 全绿后单独提交；不自动 push。

## 全局约束

- 注释用简体中文，说明决策点（竞态、幂等、事务边界、fail closed）。
- 订单状态只经 [state_machine.py](services/order/state_machine.py) `resolve_target`，并同事务写 `order_events`。
- 事件只走 outbox 同事务入队；消费者按 `event["type"]` 过滤 + 事件 ID 幂等。
- DDL：未上线、无历史数据，`reservations` 行级化与删 `payment_status` 列直接重建（`create_all` 只补建新表，删列需本地重建库）。
- 金额一律整数 `*_cents`；库存真相在 DB，Redis 是可重建缓存。

---

## Unit A：库存-支付竞态 + 行级化 + 兜底扫描 + 对账任务

### 改动文件树

- Modify: `services/inventory/models.py` — `Reservation` 行级化
- Modify: `services/inventory/app.py` — reserve/commit/release/restock 行级写入；release 前置校验；新增 5 分钟对账任务
- Modify: `services/inventory/scripts.py` — 如需补充按行级回补的 Lua（现有 RELEASE_ORDER 已按哈希回补）
- Modify: `services/payment/app.py` — 新增内部关单接口 `POST /payments/{order_id}/expire`
- Modify: `services/order/app.py` — `_release_inventory` 改为「先关支付单、再释放」，payment 不可用 fail closed
- Modify: `tests/` — 竞态与对账用例

### 技术方案

1. **行级化**：`reservations` 由「`order_id` 唯一 + JSON `items`」改为行级 `(order_id, sku)` 复合唯一，每行 `qty/status/expires_at`。预占接口写行级记录，`expires_at` = 支付窗口（15 分钟）+ 5 分钟缓冲。
2. **释放前先关支付单**：订单取消链路改调 `POST /payments/{order_id}/expire`（幂等、条件写 `pending → expired`）成功后再调 `/inventory/release`；payment 不可用则**不释放**（fail closed，宁少卖不超卖），由补偿扫单重试。
3. **兜底扫描**：inventory 的 TTL 到期释放扫描，释放前必须确认支付单已非 `pending`（查 payment），payment 不可用则不释放并告警。
4. **对账任务（Must）**：每 5 分钟比对 Redis 库存数字与 DB `total − reserved`，差异以 DB 为准 `SYNC_STOCK` 重建并告警；任务用 `shop_common.redis_lock` 防多实例重复跑（依赖 Unit C，先以进程内单实例实现，Unit C 完成后接入锁）。

### 风险点

- 释放前关支付单引入「payment 不可用→库存短暂占用」窗口，这是刻意的 fail closed 取舍。
- 行级化改动触碰 reserve/commit/release/restock 全部路径，需回归 `test_inventory_restock.py`、`test_order_compensation.py`。
- 对账任务与 Lua 闸门并发：重建必须原子 `SYNC_STOCK`，避免覆盖并发扣减。

---

## Unit B：SHIPPING 状态机 + 删 payment_status

### 改动文件树

- Modify: `services/order/state_machine.py` — 新增 `SHIPPING` 状态与流转
- Modify: `services/order/models.py` — 删除 `payment_status` 列
- Modify: `services/order/app.py` — 发货/退款/补偿状态集合补 `SHIPPING`；用 `paid_at` 等字段替代 `payment_status` 过滤
- Modify: `services/order/app.py` / `services/fulfillment/app.py` — 发货聚合推进 SHIPPING/SHIPPED
- Modify: `web/src/` — 若展示 `payment_status` 则移除
- Modify: `tests/test_state_machine.py`、`test_order_payment_flow.py`、`test_order_compensation.py`、`test_fulfillment_partial.py`

### 技术方案

1. **SHIPPING 状态**：`PAID →{ship_partial} SHIPPING →{ship} SHIPPED →{deliver} COMPLETED`；首次部分发货进 `SHIPPING`，全部发完进 `SHIPPED`，全部签收进 `COMPLETED`（部分签收不推进）。`fulfillment.shipped` 事件据此改聚合：`all_shipped` 全发完 → `ship`；否则首次部分发货 → `ship_partial`（进 SHIPPING），后续部分发货只 `_record("partial_ship")` 不迁移。
2. **删 payment_status**：订单不镜像支付状态。替代信号：
   - 迟到支付退款重试过滤 `payment_status == "paid"` → `paid_at IS NOT NULL`（退款按 ticket_id 幂等，重复重试无害）。
   - `payment_status in {paid,refunded}` 的迟到判定 → `paid_at IS NOT NULL and refund_amount_cents == 0`。
   - 其余写入点（pending/failed/expired/refunded/partially_refunded）直接删除，状态由订单宏状态 + `payment_id/pay_url/payment_expires_at` 快照表达。
3. 退款/提交/回补的状态集合统一补 `SHIPPING`。

### 风险点

- **删列是本计划最大行为面改动**：需逐处核对 `payment_status` 的 8 个写入点与 2 个过滤点，漏一处即资损或重复退款。
- 前端若有 `payment_status` 展示需同步移除，否则构建/渲染报错。
- `SHIPPING` 影响 refund/commit/restock 的状态白名单与前端状态文案。

---

## Unit C：redis_lock + 消费去重落库 + relay 并行批量 + DLQ 落库

### 改动文件树

- Create: `packages/shop_common/redis_lock.py` — 分布式锁 + 心跳续租
- Create: `packages/shop_common/tables.py`（或并入现有模块）— `processed_events`、`dead_events`、`task_runs` 模型
- Modify: `packages/shop_common/events.py` — 消费去重落 `processed_events`；DLQ 落 `dead_events`（替换 Redis 死信流）
- Modify: `packages/shop_common/outbox.py` — relay 按 topic 并行 + 批量 200/轮 + 单活跃 relay 锁
- Modify: 各服务 `app.py` — 定时任务接入 redis_lock + task_runs
- Modify: `tests/` — 锁、去重、DLQ 用例

### 技术方案

1. **redis_lock**：`shop:relay:{topic}` / 任务锁，SETNX + 过期 + 心跳续租；供 relay 与全部定时任务（关单、预占释放、库存对账、日终对账、补偿）复用。
2. **消费去重落库**：`processed_events`（`event_id + topic` 唯一，保留 7 天清理）替代当前进程内占位；DB 唯一索引兜底。
3. **DLQ 落库**：超过最大投递次数的事件写 `dead_events`（含原因与原始事件），Redis 不再存死信；提供 `replay` 命令按时间/事件 id 重放。
4. **relay 并行批量**：按 topic 分组并行扫描，批量上限 200/轮，吞吐目标 ~400 条/s；同一 topic 只允许一个活跃 relay（锁 + 续租）。
5. `task_runs` 落各服务库，记录定时任务执行轨迹。

### 风险点

- relay 并行 + 锁存在短暂重复投递窗口，必须依赖消费幂等（`processed_events` 唯一索引）兜底。
- 删除 Redis 死信流后需保留重放入口，避免事件不可追溯。

---

## Unit D：入库/盘点 API + RESERVED 兜底 + 连接池 + 优雅停机

### 改动文件树

- Modify: `services/inventory/models.py` — 新增 `stock_adjustments` 表
- Modify: `services/inventory/app.py` — 入库（增量 + 幂等键）、盘点（绝对值）接口，写 `stock_adjustments`
- Modify: `services/order/app.py` — `RESERVED` 超 5 分钟兜底扫描（复用 cancel 链路）
- Modify: `packages/shop_common/db.py` — 连接池 `pool_timeout` 池满快速失败
- Modify: 各服务 `app.py` — 优雅停机：排空在途请求、释放 relay/任务锁
- Modify: `tests/` — 入库幂等、盘点、RESERVED 兜底用例

### 技术方案

1. **入库/盘点**：入库 API 增量 + `idempotency_key` 唯一；盘点 API 传绝对值覆盖；均写 `stock_adjustments` 轨迹；与 `stock.reserved` 条件写约束一致。
2. **RESERVED 兜底**：订单侧扫描 `RESERVED` 且 `created_at` 超 5 分钟的订单，复用 `_cancel` 链路取消并释放（先关支付单，见 Unit A）。
3. **连接池**：`create_async_engine` 增加 `pool_timeout`（短值），池满立即 `TimeoutError` 并转 `ApiError`，不排队等待。
4. **优雅停机**：`lifespan` finally 里主动 `cancel` 任务 → 释放 relay/任务锁 → 关闭连接与 Redis；与 LB 摘流配合。

### 风险点

- 入库/盘点与对账任务（Unit A）并发：都以 DB 为真相、`stock_adjustments` 只增不改，靠幂等键与条件写收敛。
- `pool_timeout` 过小会误伤正常长查询，需结合压测调参。

---

## 验收清单

- [ ] 库存：`reserved ≤ total`，任何路径无负库存；释放前支付单已关闭；Redis/DB 对账任务每 5 分钟跑通。
- [ ] 资金：迟到支付自动退款幂等；删 `payment_status` 后无重复退款/漏退款。
- [ ] 订单：`PAID → SHIPPING → SHIPPED → COMPLETED` 全链路 + 部分发货/部分退款/部分签收不误迁移。
- [ ] 事件：relay 并行 + 锁不丢不重；去重与 DLQ 落库；重放幂等。
- [ ] 三高：连接池池满快速失败、优雅停机无残留锁。
- [ ] `pytest` / `ruff` / `web build` 全绿。

## 提交粒度（4 个单元 4 个提交，可再拆）

1. `feat(inventory): 库存-支付竞态协议与行级预占、对账任务`
2. `feat(order): 新增 SHIPPING 状态并移除订单侧 payment_status`
3. `feat(common): 分布式锁、消费去重与 DLQ 落库、relay 并行批量`
4. `feat(inventory): 入库盘点 API、RESERVED 兜底与优雅停机`
