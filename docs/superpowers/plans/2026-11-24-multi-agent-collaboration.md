# Multi-Agent Collaboration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在现有安全运行时之上增加顾客侧“自动分派、单主控串行协作”的多 Agent 体验，支持闲聊、售前、库存、售后，并保持人工审批、权限隔离、预算与证据链不退化。

**Architecture:** 新增保留入口 `auto`，由路由器把一次用户请求分派给 1–2 个受信内置 Agent；每个 Agent 仍使用现有 `AgentRuntime`、工具注册表、审批边界和独立证据链。主控运行只做一次配额预留、一次记忆写入和一条根用量记录，子运行共享剩余 token/工具预算；现有直接技能接口继续可用，前端默认自动分派但允许手动切换。

**Tech Stack:** Python 3.11+、FastAPI、Pydantic v2、现有 `LLMProvider`/`DeterministicPlanner`、Redis、MongoDB TraceLedger、React 18、TypeScript、SSE。

**Spec:**
- `docs/requirements/requirements-shop-agent.md`（FR-12～FR-14、FR-17～FR-18、V1 不可逆动作边界）
- `docs/design/system-design.md` §9（Agent 控制面、权限、预算、降级、证据）
- `docs/ARCHITECTURE.md`（harness 控制面与业务服务边界）
- 本次已确认决策：自动路由＋手动切换；查询＋创建申请，执行需审批；单主控串行委派；Agent 建单强制人工审核。

## Global Constraints

- 不引入新依赖；不新增数据库字段或迁移；不新增环境变量。
- 自动路由第一阶段仅开放给 `customer`，包括现有匿名访客身份；`operator`/`admin` 继续使用直接运营技能。
- 自动路由候选只允许 `chitchat`、`presales`、`inventory`、`aftersales`；`customer-service` 仅作为安全兜底，绝不自动选择 `ops-*`。
- 一次请求最多串行委派 2 个专家；所有子运行合计最多 8 次工具调用；每个子运行继续受其 manifest 的 `max_steps` 约束。
- 用户级每日配额、单次 token 预算、工具权限、审批要求、签名身份头、注入扫描、脱敏与超时不得削弱。
- Agent 创建的售后单必须 `review_mode="manual"` 并保持 `pending`；普通售后页默认 `policy`，保留现有自动政策。
- 子运行可读取当前用户/会话上下文，但不得独立写工作记忆、情节记忆、语义记忆、会话摘要或独立用户用量记录。
- 根运行返回现有 `run_id` 契约；每个子运行保留自己的 `run_id`、trace 和证据卡，根证据通过 `agent_runs` 关联。
- 日志和指标标签不得包含消息正文、Token、完整工具参数或用户标识；不得记录完整请求/响应体。
- 测试文件除非本计划明确要求，否则不得修改断言来让测试通过；现有 `test_manifest_admission.py` 的磁盘技能集合保持不变。
- 每个任务最多修改/新建 5 个文件；每个逻辑单元完成后更新 `.ai/task.md`。
- 未得到用户明确授权不得 commit 或 push；计划中的 commit 步骤均以此为前提。

## Current-State Evidence

- `services/harness/app.py:446-468` 的 `/agents` 只投影运行时 manifest；`app.py:502-621` 的一次聊天固定一个 `skill_name` 和一个 runtime run。
- `services/harness/core/runtime.py:180-395` 的 `_run_events` 同时负责注入扫描、每日配额、ReAct、记忆、摘要、用量和证据，子 Agent 不能直接复用否则会重复记账。
- `services/harness/core/llm.py` 已有 `Decision.kind=plan`，但语义是“多个工具串行计划”，不是“多个 Agent 委派”。
- 现有磁盘技能只有 `customer-service`、`ops-catalog`、`ops-aftersales`；`tests/test_manifest_admission.py` 精确断言该集合。
- `services/harness/core/tools_impl.py` 的 `after_sales_draft` 未传审核模式；`services/aftersales/app.py` 当前可能自动通过并触发退款。
- `web/src/pages/Agent.tsx:18-19` 默认 `customer-service`，切换 Agent 会清空消息与证据；`web/src/api/index.ts` 尚无路由/子 Agent 事件类型。

## File Structure

**Create:**
- `services/harness/core/agents.py`：受信内置 Agent 定义、路由 schema、LLM/离线路由、严格候选校验。
- `services/harness/core/orchestrator.py`：父运行、串行子运行、共享预算、合成、根记忆/用量/证据。
- `tests/test_agent_router.py`：路由与角色候选安全测试。
- `tests/test_runtime_budget.py`：委派运行与共享预算的运行时测试。
- `tests/test_agent_orchestrator.py`：主控编排端到端单元测试。
- `tests/test_aftersales_agent_review.py`：Agent 建单强制人工审核回归测试。
- `tests/test_harness_auto_endpoint.py`：`auto` API、角色和 SSE 契约测试。

**Modify:**
- `services/harness/core/runtime.py`：抽取配额助手，增加委派执行模式和运行预算。
- `services/harness/app.py`：装配内置 Agent、暴露 `auto`、复用现有会话/provider 流程。
- `services/harness/core/tools_impl.py`：`after_sales_draft` 发送 `review_mode="manual"`。
- `services/aftersales/app.py`：创建请求支持 `policy|manual`，manual 永不自动通过/退款。
- `services/harness/core/metrics.py`：路由、委派、编排耗时指标。
- `web/src/api/index.ts`：Agent 展示名、路由/子运行 SSE 类型。
- `web/src/pages/Agent.tsx`：默认自动分派、手动切换保留上下文、展示委派状态。
- `docs/design/system-design.md`、`docs/requirements/requirements-shop-agent.md`：更新架构与验收边界。
- `.ai/task.md`：按仓库规则持续记录进度。

## Review Focus

- **提示注入或恶意历史内容**：父运行必须先扫描并阻断，且不预留配额、不调用模型、不写记忆；由 Task 3 测试固定。
- **模型返回低置信度、未知 ID 或 `ops-*`**：不得执行，回退到无越权风险的 `customer-service`；由 Task 1、Task 3 测试固定。
- **多 Agent 共享预算耗尽**：前一个 Agent 用尽工具/token 后，后一个 Agent 必须停止或降级，不能重新获得完整预算；由 Task 2、Task 3 测试固定。
- **多 Agent 中途模型故障/合成失败**：不得编造事实；保留已完成专家答案并明确标记降级；由 Task 3 测试固定。
- **Agent 售后命中自动通过政策**：仍必须保持待人工审核且不请求 payment 退款；普通 `policy` 模式行为不变；由 Task 4 测试固定。

---

### Task 1: Built-in Agent Registry and Safe Router

**Files:**
- Create: `services/harness/core/agents.py`
- Test: `tests/test_agent_router.py`

**Interfaces:**
- Produces:
  - `AgentDefinition(id: str, display_name: str, category: str, manifest: SkillManifest, routing_examples: tuple[str, ...])`
  - `RouteDecision(agent_ids: tuple[str, ...], confidence: float, reason: str, source: Literal["llm", "rules", "fallback"])`
  - `build_builtin_agents() -> dict[str, AgentDefinition]`
  - `parse_route_decision(content: str, allowed_ids: set[str]) -> RouteDecision`
  - `AgentRouter(agents: Mapping[str, AgentDefinition]).route(message: str, history: list[dict[str, str]], provider: LLMProvider) -> RouteDecision`
  - 常量 `AUTO_AGENT_ID = "auto"`、`MAX_DELEGATED_AGENTS = 2`、`ROUTE_CONFIDENCE_THRESHOLD = 0.55`。

- [ ] **Step 1: Write failing router tests**

覆盖以下断言：

```python
def test_llm_route_selects_inventory():
    decision = parse_route_decision(
        '{"agents":["inventory"],"confidence":0.93,"reason":"stock query"}',
        {"inventory", "aftersales"},
    )
    assert decision.agent_ids == ("inventory",)
    assert decision.source == "llm"


def test_route_rejects_unknown_or_ops_agent():
    with pytest.raises(ValueError):
        parse_route_decision(
            '{"agents":["ops-catalog"],"confidence":0.99,"reason":"x"}',
            {"inventory", "aftersales"},
        )


def test_route_deduplicates_and_caps_at_two():
    decision = parse_route_decision(
        '{"agents":["inventory","inventory","aftersales","presales"],'
        '"confidence":0.8,"reason":"mixed"}',
        {"inventory", "aftersales", "presales"},
    )
    assert decision.agent_ids == ("inventory", "aftersales")


@pytest.mark.asyncio
async def test_low_confidence_falls_back_to_customer_service():
    provider = _FinalProvider('{"agents":["inventory"],"confidence":0.2,"reason":"unsure"}')
    decision = await AgentRouter(build_builtin_agents()).route("这个有货吗", [], provider)
    assert decision.agent_ids == ("customer-service",)
    assert decision.source == "fallback"
```

再增加：空列表、重复 ID 后为空、confidence 非数字/越界、JSON 数组或尾随文本、`DeterministicPlanner` 的混合意图顺序、分类前手机号脱敏、内置 Agent 工具/角色/max_steps 精确值。

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_agent_router.py -v`
Expected: FAIL with `ModuleNotFoundError: harness.core.agents`。

- [ ] **Step 3: Implement `services/harness/core/agents.py`**

使用代码内置的受信 `SkillManifest`，不新增 `SKILL.md`，因此不改变磁盘技能准入测试。内置配置固定为：

| ID | 展示名 | trust | allowed_tools | max_steps | 职责 |
|---|---|---|---|---:|---|
| `chitchat` | 闲聊助手 | `reviewed` | `[]` | 2 | 问候、寒暄、通用问答；禁止工具 |
| `presales` | 售前顾问 | `reviewed` | `catalog_search`, `catalog_detail` | 4 | 商品搜索、参数、选购建议 |
| `inventory` | 库存助手 | `reviewed` | `inventory_check` | 3 | SKU 可售数量查询 |
| `aftersales` | 售后助手 | `vendor` | `order_lookup`, `after_sales_draft` | 5 | 本人订单查询、创建待审核申请 |
| `customer-service` | 综合客服 | 现有 manifest | 现有工具 | 现有值 | 仅兜底/直接入口 |

所有内置 Agent 的 `allowed_roles=["customer"]`、`permissions={}`、`path="builtin:<id>"`，`content_hash` 为规范化 manifest 内容的完整 SHA-256 hexdigest，`findings=[]`。内置内容只来自已审查代码，不接受用户/远端动态扩展。

路由器规则：
- 把 `message` 和最近历史先经现有 `neutralize`/脱敏处理，最多带 6 条、约 800 token 的中性上下文。
- 非 `DeterministicPlanner` 时调用 `provider.decide(system, messages, [])`，只接受 `Decision.kind=="final"`；提示模型只输出 `{"agents":[...],"confidence":0..1,"reason":"..."}`。
- `parse_route_decision` 必须拒绝未知 ID、`ops-*`、空列表、超过 2 个或非法 confidence；去重后截断到 2 个。
- 只有 `DeterministicPlanner` 使用关键词规则，返回 `source="rules"`、confidence `0.7`；混合意图按关键词首次出现位置排序。
- LLM 输出为降级话术、解析失败或 confidence `<0.55` 时返回 `customer-service`/`fallback`，不得用关键词规则代替真实模型执行写操作。
- `AgentRouter.route` 只负责选择，不创建子运行、不执行工具、不写记忆。

- [ ] **Step 4: Run router tests**

Run: `pytest tests/test_agent_router.py -v`
Expected: PASS。

- [ ] **Step 5: Commit（仅在用户明确授权后）**

```bash
git add services/harness/core/agents.py tests/test_agent_router.py
git commit -m "feat: add role-safe customer agent routing" -m "Introduce reviewed built-in specialist profiles and strict route validation so model output cannot select unauthorized operational skills."
```

### Task 2: Shared Runtime Budget and Delegated Execution

**Files:**
- Modify: `services/harness/core/runtime.py`
- Create: `tests/test_runtime_budget.py`

**Interfaces:**
- Consumes: `SkillManifest`、现有 `MemoryManager`、`ToolContext`、`TraceLedger`。
- Produces:
  - `QuotaReservation(key: str, reserved: int, acquired: int)`
  - `RunLimits(parent_run_id: str, token_budget: int, tool_call_budget: int)`
  - `reserve_quota(memory: MemoryManager, user_id: str) -> QuotaReservation | None`
  - `settle_quota(memory: MemoryManager, reservation: QuotaReservation, estimated_tokens: int) -> int`
  - `AgentRuntime.run_delegated(skill_name, user_message, ctx, *, provider, limits) -> AgentRunResult`
  - `AgentRuntime.run_delegated_stream(skill_name, user_message, ctx, *, provider, limits) -> AsyncIterator[dict[str, Any]]`
  - `AgentRunResult` 增加 `usage: dict[str, Any]` 与 `tool_calls_count: int`，并进入 `to_dict()`。

- [ ] **Step 1: Write failing runtime tests**

固定以下行为：

```python
@pytest.mark.asyncio
async def test_delegated_run_does_not_reserve_quota_or_write_memory():
    result = await runtime.run_delegated(
        "inventory", "SKU-1001 还有货吗", child_ctx,
        provider=StepProvider([Decision(kind="final", content="有货")]),
        limits=RunLimits(parent_run_id="root", token_budget=1000, tool_call_budget=8),
    )
    assert result.status == "completed"
    assert fake_redis.eval_calls == 0
    assert fake_memory.append_calls == 0
    assert usage_sink.calls == 0


@pytest.mark.asyncio
async def test_delegated_run_cannot_execute_tool_with_zero_shared_budget():
    result = await runtime.run_delegated(
        "inventory", "SKU-1001 还有货吗", child_ctx,
        provider=StepProvider([Decision(kind="tool", tool="inventory_check", args={"sku": "SKU-1001"})]),
        limits=RunLimits(parent_run_id="root", token_budget=1000, tool_call_budget=0),
    )
    assert result.status == "budget_exceeded"
    assert registry.inventory_calls == 0
```

再增加：直接运行仍只预留/结算一次；委派 token 预算在执行下一步工具前停止；plan 被剩余工具预算截断并记录 `run.budget_exceeded`；委派流式与普通流式输出等价；`pending` 仍可生成；子运行保留自己的 `run_id`/trace/evidence。

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_runtime_budget.py -v`
Expected: FAIL with missing `QuotaReservation`/`RunLimits`/`run_delegated`。

- [ ] **Step 3: Refactor `services/harness/core/runtime.py`**

- 将 `_run_events` 中现有 Redis Lua 配额逻辑原样抽取为 `reserve_quota`/`settle_quota`；保留键名、日配额、预留额、TTL 和结算语义。
- `AgentRunResult` 增加带默认值的 `usage`、`tool_calls_count`，避免破坏现有构造与序列化。
- `_run_events` 增加内部 `delegated: RunLimits | None = None`；委派模式跳过配额预留/结算、语义抽取、工作记忆、情节记忆、摘要和 `usage_sink`，但仍执行注入扫描、工具策略、审批、trace 和证据卡。
- `_run_loop` 接受可选 `token_budget`、`remaining_tool_calls`；直接运行使用现有默认值，委派运行使用 `RunLimits`。
- 预算判断发生在工具调用前；`remaining_tool_calls <= 0` 时状态置 `budget_exceeded`，返回现有预算话术，不调用 registry。plan 最多执行 `min(MAX_PLAN_STEPS, remaining_tool_calls)` 步。
- 新增 `run_delegated` 与 `run_delegated_stream`，仅暴露非流式结果/现有 SSE 事件形状；不得复制第二份 ReAct 循环。

- [ ] **Step 4: Run runtime regression tests**

Run: `pytest tests/test_runtime_budget.py tests/test_runtime_stream.py -v`
Expected: PASS；直接技能行为保持不变。

- [ ] **Step 4a: Close approval replay safety gaps found in Task 7 review**

- pending 快照保留完整剩余计划、累计 `tool_errors`、共享预算和消息配对；Phase A/B 再次 pending 时重新持久化，委派审批只结算 Phase B 新增用量。
- `consumed_approvals` 使一次审批只消费一个工具调用；`RedisLock` 串行化同一 run 的审批请求。
- 审批在 token 预算前置检查通过后，用 Redis Lua `GET + DEL` 原子消费 pending，再执行任何 handler；消费后异常/取消 fail-closed 并补 `run.end`，不得重新放行旧快照。
- 记录同一 plan 已消耗步数，Phase A 续跑与原计划合计不超过 `MAX_PLAN_STEPS`；超限明确终止，不补齐并执行超限 tool_calls。
- 加载旧 pending 时按剩余 `steps/call_ids` 规范化 assistant 声明，保证每个 tool result 都有对应 tool_call；证据接口读取最后一个 `run.end`。
- Phase B 配额预留/结算异常也补一次终态；最终工具错误提示覆盖 completed/degraded/budget/max_steps/pending 等终态。
- 回归测试覆盖：截断 plan 后续跑、Phase A/B 重 pending、同名工具单次消费、并发审批、handler 后账本异常、旧快照、10 步计划审批不超过 6 步、配额结算异常、证据终态和工具错误提示。

- [ ] **Step 4b: Run approval replay regression tests**

Run: `pytest tests/test_runtime_budget.py tests/test_runtime_stream.py tests/test_tools.py tests/test_harness_evidence_endpoint.py -v`
Expected: PASS；直接技能行为保持不变。

- [ ] **Step 5: Commit（仅在用户明确授权后）**

```bash
git add services/harness/core/runtime.py tests/test_runtime_budget.py
git commit -m "refactor: share agent run budgets across delegated runs" -m "Extract quota settlement and add delegated execution so child agents cannot double-charge users or duplicate conversation memory."
```

### Task 3: Serial Multi-Agent Orchestrator

**Files:**
- Create: `services/harness/core/orchestrator.py`
- Test: `tests/test_agent_orchestrator.py`

**Interfaces:**
- Consumes: Task 1 的 `AgentDefinition`/`AgentRouter`/`RouteDecision`；Task 2 的 `reserve_quota`/`settle_quota`/`RunLimits`/`run_delegated*`；现有 `scan`、`TraceLedger`、`build_context_usage`。
- Produces:
  - `AgentRunSummary(agent: str, run_id: str, status: str, answer: str, evidence: dict[str, Any])`
  - `AgentOrchestrator(runtime: AgentRuntime, agents: Mapping[str, AgentDefinition])`
  - `run(message: str, ctx: ToolContext, *, provider: LLMProvider) -> AgentRunResult`
  - `run_stream(message: str, ctx: ToolContext, *, provider: LLMProvider) -> AsyncIterator[dict[str, Any]]`
  - 根结果 `skill="auto"`，增加 `route` 与 `agent_runs` 字段并进入 `to_dict()`。

- [ ] **Step 1: Write failing orchestrator tests**

至少覆盖：

```python
@pytest.mark.asyncio
async def test_single_agent_stream_uses_one_root_quota_and_one_usage_record():
    events = [event async for event in orchestrator.run_stream("SKU-1001 还有货吗", root_ctx, provider=provider)]
    final = events[-1]["result"]
    assert final["skill"] == "auto"
    assert final["route"]["agent_ids"] == ["inventory"]
    assert len(final["agent_runs"]) == 1
    assert fake_redis.eval_calls == 1
    assert len(usage_sink.calls) == 1
    assert fake_memory.append_calls == 2


@pytest.mark.asyncio
async def test_mixed_intent_runs_two_agents_serially_then_synthesizes():
    result = await orchestrator.run("库存多少，同时我想申请售后", root_ctx, provider=provider)
    assert [item.agent for item in result.agent_runs] == ["inventory", "aftersales"]
    assert execution_order == ["route", "inventory", "aftersales", "synthesize"]
    assert result.status == "completed"
```

再增加：
- 注入输入在任何 router/provider/配额调用前返回 `blocked`。
- LLM 返回 `ops-*`、未知 ID、低置信度时根运行选择 `customer-service`，不执行运营工具。
- 第一个子运行 `pending_approval` 时立即停止，根状态 `pending_approval`，不启动第二个。
- 两个子运行共享 8 次工具上限；第一个耗尽后第二个不得越界。
- 单专家透传 start/trace/delta/context；根 final 只出现一次且携带子运行摘要。
- 多专家不流式输出中间答案，只流式输出合成答案。
- 合成 provider 故障/返回 unavailable 时，确定性拼接已完成答案，根状态 `degraded`，不新增工具调用。
- 任一子运行失败时 synthesis 明确标注不完整；全部失败时返回安全错误话术。
- 根 trace 始终含 `run.start`、`agent.route`、每个 `agent.start`/`agent.end`、`run.end`，可被现有 evidence owner 校验读取。
- 根记忆只写一对 user/assistant，usage sink 只收到聚合后的根记录。

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_agent_orchestrator.py -v`
Expected: FAIL with `ModuleNotFoundError: harness.core.orchestrator`。

- [ ] **Step 3: Implement `services/harness/core/orchestrator.py`**

主流程固定为：
1. 创建根 `TraceLedger(ctx.run_id)` 并写 `run.start`（`skill="auto"`、`trust="reviewed"`、`user=ctx.user_id`）。
2. 复用 `scan(user_message)`；命中则写 blocked/end 并返回，零模型、零配额、零记忆。
3. 调用 `reserve_quota` 一次；失败返回现有 `quota_exceeded` 话术和根证据。
4. 读取少量工作历史，调用 `AgentRouter.route`；记录 `agent.route`，包含 source/confidence/目标/原因，不记录原文。
5. 计算路由调用后的剩余 token；按 `RouteDecision.agent_ids` 串行执行，每次创建新的 child `ToolContext`（新 `run_id`、相同用户/租户/session/conversation/provider）。
6. 每个 child 调 `run_delegated`/`run_delegated_stream`，传入共享剩余 token 和工具预算；结束后累加 `usage.estimated_tokens`、真实 token、latency 与 `tool_calls_count`，并记录 `agent.start`/`agent.end`。
7. 单专家可直接透传 child delta；多专家使用无工具 synthesis。真实 provider 合成失败、返回 `model="unavailable"` 或 `DeterministicPlanner` 时，使用确定性格式：

```text
已根据您的问题分别处理：
- 库存助手：<child answer>
- 售后助手：<child answer>
```

任何缺失/失败专家前加“该部分未完成”，状态置 `degraded`；不得补造库存、订单或退款结论。
8. 根上下文用 synthesis system + 最近工作消息 + 最终答案、空工具集计算；根结果包含 `route`、`agent_runs`、聚合 usage、首个 pending；若 pending 来自子运行，额外携带该子运行的 `run_id`，供既有审批接口定位。
9. 父运行统一写一对工作记忆、一次语义抽取、一条情节记忆、一次会话摘要和一条聚合 usage；`finally` 中按聚合 estimated tokens 结算唯一配额预留。
10. 根证据使用固定 `AUTO_SKILL_HASH = sha256("shop-agent:auto:v1")`，child evidence 通过 `agent_runs` 引用；不把 child 当独立用户会话。

`AgentRunSummary` 的对外字段只含 `agent/run_id/status/answer/evidence`；`route` 对外只含 `agent_ids/confidence/reason/source`。

- [ ] **Step 4: Run orchestrator and safety regressions**

Run: `pytest tests/test_agent_orchestrator.py tests/test_runtime_budget.py tests/test_runtime_stream.py -v`
Expected: PASS。

- [ ] **Step 5: Commit（仅在用户明确授权后）**

```bash
git add services/harness/core/orchestrator.py tests/test_agent_orchestrator.py
git commit -m "feat: orchestrate customer agents serially" -m "Add a parent run that owns quota, memory, synthesis and evidence while specialists retain bounded tool execution."
```

### Task 4: Force Manual Review for Agent Aftersales Tickets

**Files:**
- Modify: `services/aftersales/app.py`
- Modify: `services/harness/core/tools_impl.py`
- Test: `tests/test_aftersales_agent_review.py`

**Interfaces:**
- Produces:
  - `TicketCreate.review_mode: Literal["policy", "manual"] = "policy"`
  - `after_sales_draft` 请求体固定包含 `"review_mode": "manual"`。
  - manual 模式仍返回风险分/原因，但 ticket 始终 `pending`、`decided_by=""`、不调用 `_request_refund`。

- [ ] **Step 1: Write failing aftersales tests**

```python
@pytest.mark.asyncio
async def test_manual_review_never_auto_approves_or_requests_refund(monkeypatch):
    refund = AsyncMock()
    monkeypatch.setattr(aftersales_app, "_request_refund", refund)
    result = await create_ticket(
        TicketCreate(order_id="SO1", reason="quality_issue", review_mode="manual"),
        customer_principal,
    )
    assert result["status"] == "pending"
    assert result["decided_by"] == ""
    assert result["policy"]["decision"] == "auto_approve"
    refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_policy_mode_keeps_existing_auto_approval(monkeypatch):
    refund = AsyncMock()
    monkeypatch.setattr(aftersales_app, "_request_refund", refund)
    result = await create_ticket(
        TicketCreate(order_id="SO1", reason="quality_issue"),
        customer_principal,
    )
    assert result["status"] == "approved"
    refund.assert_awaited_once()
```

再增加：manual 模式即使政策建议 `auto_reject` 也保持 pending；非法 `review_mode` 返回 422；`after_sales_draft` 的服务调用参数包含 manual；policy 模式行级/整单退款既有回归测试通过。

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_aftersales_agent_review.py -v`
Expected: FAIL，manual 请求当前会进入自动政策或缺少字段。

- [ ] **Step 3: Implement manual-review boundary**

- `services/aftersales/app.py` 增加 `review_mode`，入口由 Pydantic 校验。
- 保留 `evaluate(...)` 计算并返回 `policy`；只有 `review_mode=="manual"` 时跳过 `auto_reject`/`auto_approve` 状态写入和后续 `_request_refund`，`decision_note` 写明“Agent 申请，待人工审核”。
- 默认 `policy` 分支保持现有代码路径与行为。
- `services/harness/core/tools_impl.py` 的 `after_sales_draft` 固定发送 manual；工具描述明确“只创建待审核申请，不执行退款”。
- 不修改 `AfterSaleTicket` 或 Alembic baseline。

- [ ] **Step 4: Run aftersales regressions**

Run: `pytest tests/test_aftersales_agent_review.py tests/test_aftersales_policy.py -v`
Expected: PASS。

- [ ] **Step 5: Commit（仅在用户明确授权后）**

```bash
git add services/aftersales/app.py services/harness/core/tools_impl.py tests/test_aftersales_agent_review.py
git commit -m "fix: require manual review for agent aftersales tickets" -m "Prevent agent-created requests from inheriting the storefront auto-approval policy and triggering refunds without staff review."
```

### Task 5: Harness API Wiring and Observability

**Files:**
- Modify: `services/harness/app.py`
- Modify: `services/harness/core/metrics.py`
- Test: `tests/test_harness_auto_endpoint.py`

**Interfaces:**
- Consumes: Task 1–4 的全部接口。
- Produces:
  - `/agents` 对 customer 追加合成 `auto`，并返回可选 `display_name`。
  - `/agents/auto/chat` 与 `/agents/auto/chat/stream` 复用 `AgentOrchestrator`。
  - 新增内置 Agent 的直接 `/agents/{id}/chat` 仍走 `AgentRuntime`。
  - SSE 增加 `{"type":"agent","event":{"type":"route"|"agent.start"|"agent.end"|"fallback", ...}}`。

- [ ] **Step 1: Write failing endpoint tests**

直接调用 FastAPI 路由函数并 monkeypatch 全局 runtime/orchestrator/provider/DB helper，避免测试依赖完整 lifespan：

```python
def test_customer_agent_list_contains_auto_and_specialists():
    result = asyncio.run(list_agents(customer_principal))
    names = {item["name"] for item in result["items"]}
    assert {"auto", "chitchat", "presales", "inventory", "aftersales"} <= names
    assert "ops-catalog" not in names


def test_operator_cannot_use_auto():
    with pytest.raises(ApiError) as exc:
        asyncio.run(chat("auto", ChatRequest(message="你好"), operator_principal, stream=False))
    assert exc.value.status_code == 403
```

再增加：customer auto 非流式返回根 `run_id`/`conversation_id`/`route`/`agent_runs`；auto 流式包含 agent 事件且只有一个 final；直接 `inventory` 不经过 orchestrator；现有 `customer-service` 响应形状兼容；内置 Agent `/skills/{name}/verify` 返回其 manifest 而不是 404；指标标签只含有界枚举。

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_harness_auto_endpoint.py -v`
Expected: FAIL，当前 `auto` 不存在或落到单技能查找。

- [ ] **Step 3: Wire startup and chat branch**

- lifespan 中保留 `load_all(SKILL_ROOT)` 结果，再 `skills.update({id: definition.manifest ...})` 装配内置 Agent；创建 `orchestrator = AgentOrchestrator(runtime, builtin_agents)`。
- `/agents` 对 customer 在普通 manifest 前插入 `auto`：`trust="reviewed"`、`allowed_tools=[]`、`description="自动识别问题并串行分派专家"`、`display_name="自动分派"`；其他角色不返回 auto。
- 普通 manifest 使用内置定义的 `display_name`，磁盘技能回退为 `name`。
- `chat` 在现有 provider 解析、会话归属、用户消息持久化和 `_ctx` 之后分支：`auto` 且 customer 调 orchestrator；其他 skill 保持原 runtime 调用。
- 流式 generator 只替换 runtime 事件源，现有 assistant 消息补落、`conversation_id` 注入和 `[DONE]` 保持不变。
- `metrics.py` 增加：
  - `agent_route_total{service,source,result}`
  - `agent_delegation_total{service,agent,status}`
  - `agent_orchestration_duration_seconds{service}`
- orchestrator 在 Task 3 已定义的 route/child 边界递增指标；result/status 使用 `completed|degraded|fallback|blocked|quota_exceeded|budget_exceeded|error` 等有界值。

- [ ] **Step 4: Run API and harness regressions**

Run: `pytest tests/test_harness_auto_endpoint.py tests/test_agent_orchestrator.py tests/test_runtime_stream.py -v`
Expected: PASS。

- [ ] **Step 5: Commit（仅在用户明确授权后）**

```bash
git add services/harness/app.py services/harness/core/metrics.py tests/test_harness_auto_endpoint.py
git commit -m "feat: expose automatic agent orchestration endpoint" -m "Wire the reserved auto entry through existing auth, provider and conversation flows without changing direct skill endpoints."
```

### Task 6: Frontend Auto Routing and Manual Switch

**Files:**
- Modify: `web/src/api/index.ts`
- Modify: `web/src/pages/Agent.tsx`

**Interfaces:**
- Produces:
  - `AgentInfo.display_name?: string`
  - `AgentRouteInfo { agent_ids: string[]; confidence: number; reason: string; source: "llm"|"rules"|"fallback" }`
  - `AgentRunSummary { agent: string; run_id: string; status: string; answer: string }`
  - `StreamFinal.route?: AgentRouteInfo`
  - `StreamFinal.status: string`
  - `StreamFinal.agent_runs?: AgentRunSummary[]`
  - `AgentEvent` 与 `StreamEvent` 的 `agent` 分支。

- [ ] **Step 1: Update API types**

在 `web/src/api/index.ts` 增加上述类型；`chatStream` URL 仍为 `/api/agents/${skill}/chat`，传 `skill="auto"` 即可，不新增客户端 endpoint。

- [ ] **Step 2: Update `web/src/pages/Agent.tsx`**

- 默认 `skill="auto"`；`QUICK_PROMPTS.auto` 至少包含闲聊、售前、库存、售后、混合意图各一条。
- Agent 按钮显示 `display_name || name`；auto 显示“自动分派”。
- 手动切换只更新 `skill` 和对应会话列表，不清空当前 `messages`、`conversationId`、trace 或 evidence；下一次发送沿用当前会话，实现“同一对话切换专家”。
- `startNewSession` 同时清空路由状态和子 Agent 状态。
- 新增 `routeLabel`、`activeAgents` 状态；处理 `event.type==="agent"`：
  - `route`：显示“已识别：库存＋售后”或“已回退综合客服”。
  - `agent.start`：加入 `activeAgents` 并显示当前专家。
  - `agent.end`：标记完成/失败。
  - `fallback`：显示降级原因。
- `final` 保存 `route`/`agent_runs`，在证据区显示每个 child `run_id` 和状态；根证据按钮仍使用根 `run_id`。
- 根 `final.status` 独立展示，`degraded` 不得由子 Agent 状态推导或显示为 completed；空 `agent_ids` 的 quota/blocked final 不显示“已回退综合客服”。
- 发送新请求时同步清空旧 `runId`/evidence 并禁用证据按钮；切换历史时作废在途证据请求并清 trace/runId/evidence，防止旧响应回填。
- `/agents` 就绪前禁发；`busyRef` 实时守卫发送，拆分请求用独立 pending ref 同步置位并在 finally 清除，防止 await 期间重复发送/拆分。
- 不在前端执行任何权限判断或退款动作；所有控制仅改变请求入口。

- [ ] **Step 3: Run frontend checks**

Run: `npm --prefix web run build`
Expected: TypeScript 与 Vite build PASS。

- [ ] **Step 4: Browser smoke test**

在完整 dev/compose 环境用浏览器验证：
1. 默认 auto；“你好”路由到闲聊且无工具 trace。
2. “SKU-1001 还有货吗”路由库存并流式回答。
3. “推荐一款耳机”路由售前。
4. “订单 SO… 想退款”路由售后，创建工单后状态为 pending，payment 无退款调用。
5. 混合意图显示两个专家串行执行和一个最终答案。
6. 运行中切换手动 Agent 不清空当前对话；下一条消息沿用 conversation_id。
7. 模拟 provider 不可用显示综合客服/降级提示，不触发工具写操作。
8. customer 页面不存在 `ops-*` 自动路由；直接请求 `auto` 的 operator 返回 403。
9. 连续两轮请求不加载上一轮证据；切换历史后旧证据响应不回填。
10. 子 Agent 均 completed 但 synthesis 降级时，根状态独立显示“已降级”；拆分 await 期间不能重复发送。

- [ ] **Step 5: Commit（仅在用户明确授权后）**

```bash
git add web/src/api/index.ts web/src/pages/Agent.tsx
git commit -m "feat: add agent routing controls to customer chat" -m "Default to orchestrated routing while preserving direct specialist selection and the active conversation context."
```

### Task 7: Documentation and Full Verification

**Files:**
- Modify: `docs/design/system-design.md`
- Modify: `docs/requirements/requirements-shop-agent.md`
- Modify: `.ai/task.md`

- [x] **Step 1: Update architecture and acceptance docs**

- 在 system design §9 增加：auto 入口、4 个 customer 专家、customer-service 兜底、最多 2 个串行专家、共享 8 次工具预算、父/子证据关系。
- 明确 ops 技能不参与自动路由；内置 Agent 是受信代码配置，不是可动态加载的第四方 skill。
- 在 FR-12/FR-14 附近增加：Agent 售后只能查询/创建 pending 申请，执行需既有审批；模型故障时回退综合客服或确定性合成，不得编造事实。
- `.ai/task.md` 记录每个 Task 的完成命令、结果和未决项。

- [ ] **Step 2: Run targeted backend verification**

```bash
pytest tests/test_agent_router.py tests/test_runtime_budget.py tests/test_agent_orchestrator.py tests/test_aftersales_agent_review.py tests/test_harness_auto_endpoint.py tests/test_runtime_stream.py tests/test_llm.py tests/test_aftersales_policy.py tests/test_harness_evidence_endpoint.py -v
ruff check packages services tests .github/scripts
```

Expected: targeted tests PASS；Ruff 0 errors。若仓库既有失败与本次 diff 无关，记录文件、规则和原始输出，不静默跳过。

- [ ] **Step 3: Run full backend and frontend verification**

```bash
pytest -q
ruff check packages services tests .github/scripts
docker compose -f docker-compose.yml config -q
npm --prefix web ci
npm --prefix web run build
npm --prefix web audit --omit=dev
```

Expected: 后端测试、Compose 校验和前端 CI 安装/构建 PASS；生产依赖无 Critical/High。根 `package.json` 的 `pnpm typecheck/lint/test/audit` 仅作诊断：当前缺少根 tsconfig、ESLint 配置、Vitest 用例和 pnpm lockfile，不是 CI 入口；必须记录真实输出，不修改断言或静默跳过。完整 `npm audit` 的既有开发依赖风险单独记录，不得把生产审计结果表述为全量审计通过。

- [ ] **Step 4: Verify production safeguards**

检查并记录：
- 无新增 migration、依赖、密钥或环境变量。
- `git diff` 无完整请求/响应日志、Token、密码、用户正文指标标签。
- 自动路由不能选择 `ops-*`；manual 售后不触发 payment。
- 父/子 `run_id` 均可按现有 owner/staff 规则读取证据。
- 配额在异常/客户端断流路径通过 `finally` 结算。

- [ ] **Step 5: Commit（仅在用户明确授权后）**

```bash
git add docs/design/system-design.md docs/requirements/requirements-shop-agent.md .ai/task.md
git commit -m "docs: document multi-agent collaboration safeguards" -m "Record the routing, shared-budget, evidence and manual aftersales boundaries required for production operation."
```

## Rollout and Rollback

- 先上线后端，`auto` 与直接技能并存；旧 `/agents/{skill}/chat` 行为不变。
- 前端随后默认 `auto`；如路由质量不佳，可立即把默认值改回 `customer-service`，无需数据迁移。
- 回滚后端镜像即移除 auto；已创建的 manual 售后工单仍由既有运营审批接口处理。
- 不引入数据库开关；回滚不删除 trace、会话或证据，保证审计连续。

## Out of Scope

- 并行 Agent、动态第三方 Agent、跨租户协作、运营侧自动路由。
- Agent 自动执行退款、改价、发布等不可逆动作。
- 新的向量库、模型供应商、前端框架或数据库表。
- 重做会话历史/长上下文治理；本计划只复用现有会话与记忆边界。

## Self-Review

- **Spec coverage:** FR-12 权限/审批由 Task 1、4、5 覆盖；FR-13 可回放由 Task 3、5 的父子证据覆盖；FR-14 降级由 Task 1、3、6 覆盖。
- **Type consistency:** `AgentDefinition`、`RouteDecision`、`RunLimits`、`AgentRunResult`、`AgentOrchestrator.run*` 在后续任务中名称与返回类型一致。
- **Review Focus:** 五类风险均有 Task 1–4 的明确测试，UI 上下文保留由 Task 6 浏览器场景覆盖。
- **Proportion:** 计划固定接口、边界、测试和验证，不提供完整业务实现；每个任务均可独立审查与回滚。
