# 降级链终点修复：生产降级不再退回关键词规划器

## 背景

`build_provider()` 在「有主模型、无降级模型」配置下把 `DeterministicPlanner` 当 fallback。
该规划器是离线演示/CI 回归设施，靠关键词路由工具；生产降级时它会错选工具（含
`after_sales_draft` 写操作）并把不可靠结果包装成正常答案，证据卡只记 model 名，
用户与运营都无从感知答案质量已降级。

## 改动文件树

```
services/harness/core/llm.py   # 新增 CannedProvider；build_provider 降级链改造
tests/test_llm.py              # 降级链三种配置形态 + CannedProvider 契约测试
```

## 技术方案

1. **新增 `CannedProvider`**：`decide()` 恒返回 `Decision(kind="final", content=FALLBACK_MESSAGE,
   model="unavailable")`；`decide_stream()` 按契约产恰好一帧 terminal。`model="unavailable"`
   沿用既有语义，证据卡可直接审计「本次答案非模型产出」。
2. **build_provider 三种形态**：
   - 无主模型配置 → `DeterministicPlanner`（离线演示/CI，现状不变）；
   - 主 + 降级端点齐备 → `FailoverProvider(primary, OpenAICompatibleProvider, breaker)`（现状不变）；
   - 仅主模型 → `FailoverProvider(primary, CannedProvider(), breaker)`：降级即固定话术转人工，
     不再即兴调用工具。既有「降级模型也失败 → canned」兜底逻辑自然覆盖 CannedProvider 之外的失败路径。
3. **行为不变量**：离线模式、双模型模式、熔断器、流式「首 token 前才可降级」约束全部不变；
   FALLBACK_MESSAGE 文案不变。

## 风险点

- 依赖「规划器兜底」的部署会失去该行为——这正是修复目标：生产降级宁可明确转人工，
  不可用关键词规划器冒充模型产出。
- CannedProvider 永远 final，降级期间工具调用数为 0，监控上体现为工具调用量下跌，
  属预期信号而非新故障。

## 验证

- `pytest tests/test_llm.py`：新增 4 个用例（仅主模型→CannedProvider、双模型→真实 fallback、
  CannedProvider 决策契约、流式单帧契约）。
- 全量 `pytest -q` + `ruff check services/harness tests`。
