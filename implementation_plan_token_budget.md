# Agent token 成本硬预算

## 背景

运行时此前只有 `max_steps` 限制模型调用次数，对「单次运行上下文总大小」和「单用户
累计消耗」没有任何硬约束：一个 run 可以滚出无限大的 prompt，一个用户可以在网关
限流（120 次/分）内无限发起 run。usage 也仅是 `chars//3` 的事后估算，不参与控制。

## 改动文件树

```
packages/shop_common/config.py     # 新增 agent_run_token_budget / agent_user_daily_token_quota
packages/shop_common/metrics.py    # 新增 agent_budget_exceeded_total{service,kind}
services/harness/core/runtime.py   # 运行级预算检查 + 用户级每日配额查账/记账
tests/test_runtime_stream.py       # FakeRedis 补 incrby/expire；新增 3 个预算用例
```

## 技术方案

1. **用户级每日配额（防刷量）**：进入 `_run_events` 先做 manifest 存在性检查，
   然后用 Lua 脚本对 Redis 账本 `shop:harness:quota:{user_id}:{UTC日期}`（TTL 两天）
   原子完成「查余量 + 扣预留」（预留量 = 运行级预算）——并发 run 只剩一份配额时
   只有一个能拿到预留，杜绝先查后扣的并发超支。拿不到预留 → 直接产出
   `quota_exceeded` 的 done 结果：诚实话术 + trace + 证据卡（estimated_tokens=0），
   **一次模型/嵌入调用都不发生**，SSE 契约不断裂。
2. **运行级预算（防单次滚雪球）**：每步模型调用后累计 prompt_chars，决策非 final 且
   `prompt_chars//3 > agent_run_token_budget` 时停止循环：不执行本步工具（执行了也没
   预算让模型消化结果），状态 `budget_exceeded`，话术引导缩小范围或开新会话。
3. **结算**：运行结束按「实际估算 - 预留量」单条 `INCRBY` 补扣/回补（负值即回补）；
   pending_approval 等所有发生过模型调用的结局都照结算。usage 增加
   `user_spent_today` 字段流入证据卡。
4. **观测**：`agent_budget_exceeded_total{kind=run|user}` 计数，可配告警。
5. **口径说明**：`chars//3` 是护栏口径不是计费口径，确定性、可测、对降级路径一致；
   OpenAI 真实 usage 字段的接入留作后续增强（流式响应需服务端开 include_usage）。

## 风险点

- ~~先查后扣允许少量超支~~ → 已改为 Lua 原子「查余量 + 扣预留」（与库存扣减同模式），
  并发 run 不可能互相超支；代价是进程崩溃时预留量泄漏，随配额键 TTL 自愈（有界）。
- 配额尾部收紧：余量不足一份运行预算时即拒绝，用户可能在额度耗尽前最多
  「少用一个运行预算的尾巴」，护栏语义下宁严勿宽。
- UTC 日期分桶在零点切换，跨零点运行的记账归入结束日，口径文档化即可。
- 默认值（2 万/run、20 万/用户/日）为保守起点，生产按实际模型与客单调优；
  存量行为在默认配额下无感知变化。

## 验证

- `pytest tests/test_runtime_stream.py`：配额超额零调用、运行预算截断不执行工具、
  正常运行记账正确。
- 全量 `pytest -q` + `ruff check`。
