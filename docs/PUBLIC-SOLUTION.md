# 公共电商解决方案抽象

这套骨架不是为某一个品类写的。它可以被切成「**可复用的内核 + 可替换的行业插件**」，复用的边界如下。

## 1. 稳定内核（不改代码就能复用）

| 能力 | 契约 | 复用方式 |
|---|---|---|
| 订单状态机 | `TRANSITIONS` + `resolve_target(current, event)` | 新增状态只改表；业务代码不直接改状态 |
| 库存原子操作 | `RESERVE_MULTI / COMMIT / RELEASE / SEC_KILL / SYNC` | 任何需要防超卖的品类（票务、库存机、名额）都能用 |
| 事务性 Outbox | `enqueue(session, topic, type, payload)` | 只要业务写库与事件同事务即可接入 |
| 事件总线 | `publish / poll / run_consumer` | 本地 Redis Streams，生产可换 Kafka，接口不变 |
| 幂等 | `IdempotencyStore.begin/get/finish` + `Idempotency-Key` | 支付、下单、退款等写操作统一复用 |
| 身份与租户 | `Principal(user_id, role, tenant)` + 透传头 | 多租户只增加 `tenant` 维度，不改业务表结构 |
| 错误契约 | `ApiError(code, message, details)` | 网关与前端按 code 渲染，不依赖中文文案 |
| Agent 控制面 | SKILL 准入 + 信任分级 + 工具策略 + 审批 + 证据 | 与电商业务无耦合，可平移到任何业务系统 |

## 2. 行业替换点

### 2.1 商品模型

`catalog` 的字段分为「通用字段」与「类目属性」：

- 通用：`sku / title / subtitle / description / category / price_cents / image_url / tags / status`
- 扩展：`attributes: dict`

替换方式：在 `catalog/rules.py` 里声明类目必填属性与违规词，不需要改表；前端按 `attributes` 动态渲染。跨境场景可增加 `hs_code / origin_country`；虚拟商品可增加 `delivery_type`，由 `fulfillment` 决定是否创建实物运单。

### 2.2 履约模型

`fulfillment` 只关心「订单支付后 → 截止时间 → 发运 → 签收」四件事。可替换的实现：

| 业务 | 适配方式 |
|---|---|
| 实物电商 | 当前实现：运单号 + SLA 超时扫描 |
| 虚拟商品 | 支付后直接发码，`ship` 改为「交付完成」事件 |
| 门店自提 | 增加 `pickup_store` 字段，`deliver` 改为核销 |
| 同城即时配 | 接入第三方配送 MCP 工具，Harness 只负责权限与审计 |

### 2.3 售后政策

政策引擎的输入是订单状态、金额、申请原因、历史频次与文本风险词；输出是 `auto_approve / manual / auto_reject` 与建议。替换方式：

- 金额阈值、时间窗口、风险因子权重都在 `aftersales/policy.py`，可按行业配置。
- 高风险原因词表由运营维护，不写死在提示词里。
- Agent 只能 `propose`；`approve / refund` 必须人工。

### 2.4 Agent 工具包

业务工具集中在 `harness/core/tools_impl.py`，每个工具声明：

```python
ToolPolicy(name, permission, risk, data_class, requires_approval)
```

新增行业能力 = 新增工具 + 声明策略 + 在 SKILL.md 的 `allowed-tools` 里列出。权限上限由信任等级决定，工具作者无法自我提权。

## 3. 多租户

- `Principal.tenant` 一路透传到业务服务与工具上下文。
- 记忆写入带 `tenant`，召回按用户隔离；生产可增加 `tenant` 索引与行级策略。
- 事件带 `tenant` 字段，消费者可按租户分流。
- 数据层当前是「每服务独立 DB」；多租户建议两种路径：共享库 + `tenant_id` 行级隔离（中小规模），或每租户独立 schema（强隔离）。

## 4. 接入一个 Agent 的标准流程

1. 写 `SKILL.md`：front matter 声明 `name / display_name / trust / allowed-tools / max-steps`，正文写系统提示与运行约束。
2. 运行准入：`POST /skills/{name}/verify`，确认 verdict=allow、无 high finding。
3. 选信任等级：客服导购通常 `vendor`（需要 PII 与执行权）；纯建议型 `reviewed`。
4. 只声明必需工具；`after_sales_approve` 这类不可逆工具默认不给 Agent。
5. 验证：用 `/agents/{skill}/chat` 跑真实问题，检查 trace 里是否有编造、越权、未处理错误。
6. 上线：证据卡随运行落盘，运营可按 `run_id` 复核。

## 5. 合规与运营接口

| 关注点 | 当前实现 | 生产建议 |
|---|---|---|
| 审计 | 网关 `shop:audit` + Harness trace | 落日志系统，保留 180 天以上 |
| 数据最小化 | 工具层脱敏（手机号/身份证/邮箱） | 增加字段级加密与访问审批 |
| 售后公平性 | 政策引擎可解释的 `risk_reasons` | 定期抽样复核自动决策 |
| 内容合规 | catalog `compliance-check`（类目属性 + 违规词） | 接入行业词库与人工复核队列 |
| SLA | fulfillment 截止时间扫描 + 事件 | 接入告警与升级链路 |

## 6. 什么不该抽象

- 不要为「将来可能的多仓、多币种、多平台」提前建模；当前抽象只覆盖已被垂直切片验证的部分。
- 不要把 Agent 的推理流程写进业务服务；控制面与数据面必须保持单向依赖。
- 不要让 manifest 决定权限；信任等级表是唯一事实源。
