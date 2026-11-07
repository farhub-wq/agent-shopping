# LLM 规划与结构化输出 Implementation Plan

**Goal:** 让模型一次性产出多步计划（顺序执行多个工具调用），并用统一的决策校验器保证结构合法——多意图请求不再被单工具决策截断，坏输出不再静默降级成空。

**Architecture:** 两层改动。决策层给 `Decision` 增加 `plan` 类型与 `steps` 字段，新增 `parse_decision` 纯函数把 OpenAI 响应消息校验成决策（多个 `tool_calls` → plan，单个 → tool，无 → final，坏结构抛 `ApiError`）；`OpenAICompatibleProvider` 的 `decide`/`decide_stream` 都改走 `parse_decision`，`DeterministicPlanner` 对多意图查询产出 `plan`。运行时 `_run_events` 把「单工具」与「计划」统一成步骤列表，逐 step 过同一工具网关（权限→审批→执行→脱敏→trace），全部执行完继续 ReAct 让模型 compose final。

**Tech Stack:** Python 3.11 · asyncio · 现有 Decision/ToolRegistry/运行时 ReAct 循环（不引入新依赖）。

**Spec:** `docs/requirements/requirements-shop-agent.md` NFR-10（单次会话工具调用有上限）、FR-12/13（越权拒绝 + 证据可回放）。

## 决策契约（本计划落定）

```python
# 新增第三种决策类型 plan：有序工具步骤，顺序执行（并行属后续「并行多工具」项，不在本计划）
Decision(kind="plan", steps=[{"tool": "inventory_check", "args": {"sku": "SKU-1001"}},
                              {"tool": "catalog_validate", "args": {"sku": "SKU-1001"}}])

MAX_PLAN_STEPS = 6  # 单次 plan 的步骤上限，超出截断

def parse_decision(message: dict[str, Any], tools: list[dict[str, Any]]) -> Decision:
    # message 为 OpenAI 响应 message（含 content 与可选 tool_calls）；
    # tools 为已暴露给模型的工具 spec 列表（含 name/input_schema）。
    # 校验规则：tool_calls 中的 tool 名必须在 tools 内、arguments 必须是合法 JSON 对象；
    # 0 个调用 → final(content)；1 个 → tool；≥2 个 → plan(steps)。
    # 任何不合法（未知工具、arguments 非法 JSON/非对象）抛 ApiError("BAD_DECISION", ...)。
```

## Global Constraints

- 不回归现有 final 答案的流式（delta）与单工具路径；`plan` 是新增分支，`run()` 非流式行为不变。
- plan 步骤数上限 `MAX_PLAN_STEPS = 6`，超出部分截断并在 trace 记 `plan.truncated`（含 planned/executed 计数）。
- plan 的每个 step 都必须过工具网关；任一 `needs_approval` 即停，后续 step 不执行。
- 坏决策抛 `ApiError`（不吞异常、不返回 null），由 FailoverProvider 捕获降级。
- 提交：Conventional Commits + 简体中文；**后端完成后停下等确认再进前端**（本次前端大概率无需改动，若确认无改动则跳过）。
- 全量验证：`.venv/Scripts/python -m pytest -q` + `.venv/Scripts/python -m ruff check packages services tests`。

## Review Focus

1. **plan 超长必须截断**：模型可能返回几十步，超出 `MAX_PLAN_STEPS` 要截断并记 trace，不能让一次 decide 无限执行工具 —— Task 4 `test_plan_truncates_beyond_max`。
2. **plan 中某步 needs_approval 必须停**：后续 step 不得继续执行 —— Task 4 `test_plan_stops_on_approval`。
3. **多个 tool_calls 正确解析为 plan**：≥2 → plan、1 → tool、0 → final —— Task 1 `test_parse_decision_multiple_tool_calls_becomes_plan`。
4. **坏决策抛错不静默**：未知工具 / arguments 非法 JSON 抛 `ApiError` —— Task 1 `test_parse_decision_rejects_unknown_tool` / `test_parse_decision_rejects_bad_arguments_json`。
5. **流式多工具按 index 归位**：并行 tool_calls 的 delta 带 `index`，必须按 index 分片累积，不能串到同一工具的 args 里 —— Task 2 `test_openai_stream_delta_multiple_tool_calls_by_index`。

---

### Task 1: Decision 扩展 + parse_decision 校验器

**Files:**
- Modify: `services/harness/core/llm.py`（`Decision`、新增 `parse_decision`、`MAX_PLAN_STEPS`）
- Test: `tests/test_llm.py`

**Interfaces:**
- Produces:
  ```python
  MAX_PLAN_STEPS = 6

  @dataclass
  class Decision:
      kind: str            # final | tool | delta | plan
      content: str = ""
      tool: str = ""
      args: dict[str, Any] = field(default_factory=dict)
      steps: list[dict[str, Any]] = field(default_factory=list)  # 新增：plan 的有序工具步骤
      usage: dict[str, Any] = field(default_factory=dict)
      model: str = ""

  def parse_decision(message: dict[str, Any], tools: list[dict[str, Any]]) -> Decision: ...
  ```

- [ ] **Step 1: 写失败测试** `tests/test_llm.py`（追加）

```python
from harness.core.llm import Decision, parse_decision

TOOLS = [{"name": "inventory_check", "input_schema": {"type": "object"}},
         {"name": "catalog_validate", "input_schema": {"type": "object"}}]


def test_parse_decision_no_tool_calls_is_final():
    d = parse_decision({"content": "你好"}, TOOLS)
    assert d.kind == "final" and d.content == "你好"


def test_parse_decision_single_tool_call():
    d = parse_decision({"tool_calls": [{"function": {"name": "inventory_check", "arguments": "{\"sku\": \"SKU-1001\"}"}}]}, TOOLS)
    assert d.kind == "tool" and d.tool == "inventory_check" and d.args["sku"] == "SKU-1001"


def test_parse_decision_multiple_tool_calls_becomes_plan():
    calls = [
        {"function": {"name": "inventory_check", "arguments": "{}"}},
        {"function": {"name": "catalog_validate", "arguments": "{}"}},
    ]
    d = parse_decision({"tool_calls": calls}, TOOLS)
    assert d.kind == "plan"
    assert [s["tool"] for s in d.steps] == ["inventory_check", "catalog_validate"]


def test_parse_decision_rejects_unknown_tool():
    import pytest
    from shop_common.errors import ApiError
    with pytest.raises(ApiError):
        parse_decision({"tool_calls": [{"function": {"name": "nope", "arguments": "{}"}}]}, TOOLS)


def test_parse_decision_rejects_bad_arguments_json():
    import pytest
    from shop_common.errors import ApiError
    with pytest.raises(ApiError):
        parse_decision({"tool_calls": [{"function": {"name": "inventory_check", "arguments": "not json"}}]}, TOOLS)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_llm.py -k parse_decision -v`
Expected: FAIL（`ImportError: cannot import name 'parse_decision'`）

- [ ] **Step 3: 实现** `Decision` 加 `steps` 字段；`MAX_PLAN_STEPS = 6`；`parse_decision` 按 Interfaces 校验并构造（arguments 用 `json.loads`，非法或非 dict 抛 `ApiError("BAD_DECISION", ...)`；未知工具名抛同款）。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_llm.py -k parse_decision -v`
Expected: PASS（5 passed）

- [ ] **Step 5: 提交（先展示变更摘要）**

`git add services/harness/core/llm.py tests/test_llm.py` → `feat(harness): 决策增加 plan 类型与结构化校验器`

---

### Task 2: OpenAI 兼容端点走 parse_decision + 流式多工具按 index 归位

**Files:**
- Modify: `services/harness/core/llm.py`（`OpenAICompatibleProvider.decide` / `decide_stream` / `_openai_stream_delta`）
- Test: `tests/test_llm.py`

**Interfaces:**
- Consumes: `parse_decision`（Task 1）
- Produces:
  ```python
  def _openai_stream_delta(chunk: dict[str, Any]) -> tuple[str, list[tuple[int, str, str]]]:
      # 返回 (content_delta, [(index, name_delta, args_delta), ...])；
      # 并行 tool_calls 的每个 delta 带 index，按 index 归位，缺失字段安全返回空。
  ```

- [ ] **Step 1: 写失败测试** `tests/test_llm.py`

```python
from harness.core.llm import _openai_stream_delta


def test_openai_stream_delta_multiple_tool_calls_by_index():
    chunk = {"choices": [{"delta": {"tool_calls": [
        {"index": 0, "function": {"name": "echo", "arguments": "{\"x\":"}},
        {"index": 1, "function": {"name": "check", "arguments": "{\"y\":"}},
    ]}}]}
    content, deltas = _openai_stream_delta(chunk)
    assert content == ""
    assert deltas == [(0, "echo", "{\"x\":"), (1, "check", "{\"y\":")]
```

同步调整既有 3 个 `_openai_stream_delta` 测试到新返回形状（`("你好", [])`、`("", [(0, "echo", "{\"x\":")])`、缺字段 → `("", [])`）。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_llm.py -k openai_stream_delta -v`
Expected: FAIL（旧 3 元组断言与新形状不符）

- [ ] **Step 3: 实现**

`decide`：删掉 `calls[0]` 单取逻辑，改 `parse_decision(message, tools)`（tools 即传入的 spec 列表）。`decide_stream`：用 `dict[int, list[str, str]]` 按 index 累积 name/args，流结束后把累积结果组装成伪 `message`（`{"content": 全文, "tool_calls": [...], }`）交给 `parse_decision`；plan/tool/final 分支同 Task 1。`_openai_stream_delta` 改返回 `(content, [(index, name, args)])`。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_llm.py -q`
Expected: 全部 PASS（含既有 21 个 + 新增）

- [ ] **Step 5: 提交（先展示变更摘要）**

`git add services/harness/core/llm.py tests/test_llm.py` → `feat(harness): OpenAI 兼容端点多工具调用解析为计划`

---

### Task 3: 离线规划器多意图 → plan

**Files:**
- Modify: `services/harness/core/llm.py`（`DeterministicPlanner.decide`）
- Test: `tests/test_llm.py`

**Interfaces:**
- Produces: `DeterministicPlanner.decide` 在多意图查询下返回 `Decision(kind="plan", steps=[...])`。

- [ ] **Step 1: 写失败测试** `tests/test_llm.py`（追加）

```python
@pytest.mark.asyncio
async def test_planner_multi_intent_produces_plan():
    planner = DeterministicPlanner()
    decision = await planner.decide(
        "system", [{"role": "user", "content": "SKU-1001 库存还有多少，合规性也查一下"}], SPECS
    )
    assert decision.kind == "plan"
    assert [s["tool"] for s in decision.steps] == ["inventory_check", "catalog_validate"]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_llm.py -k multi_intent -v`
Expected: FAIL（当前只路由到 inventory_check，kind=="tool"）

- [ ] **Step 3: 实现**

在 `decide` 的 if/elif 链**之前**加多意图检测：仅当文本同时命中 ≥2 个「独立查询类」意图（库存 / 合规-属性 / 订单查询 / 推荐）时收集对应工具调用组成 `plan`；单意图与售后/退款仍走原逻辑（`choose()` 的「首选/退路」语义不变）。先取 `sku` 供各 step 复用。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_llm.py -q`
Expected: 全部 PASS（既有单意图用例不受影响）

- [ ] **Step 5: 提交（先展示变更摘要）**

`git add services/harness/core/llm.py tests/test_llm.py` → `feat(harness): 离线规划器多意图查询产出计划`

---

### Task 4: 运行时 plan 顺序执行

**Files:**
- Modify: `services/harness/core/runtime.py`（`_run_events`）
- Test: `tests/test_runtime_stream.py`（追加）

**Interfaces:**
- Consumes: `Decision(kind="plan", steps=[...])`（Task 1–3）
- Produces: `_run_events` 把单工具与 plan 统一为步骤列表逐条执行。

- [ ] **Step 1: 写失败测试** `tests/test_runtime_stream.py`（追加；复用 `_StepProvider`/`_FakeLedger`/`_runtime`）

```python
@pytest.mark.asyncio
async def test_run_plan_executes_all_steps_then_final(monkeypatch):
    import harness.core.runtime as rt
    monkeypatch.setattr(rt, "TraceLedger", _FakeLedger)
    steps = [
        {"tool": "echo", "args": {"text": "a"}},
        {"tool": "echo", "args": {"text": "b"}},
    ]
    provider = _StepProvider([
        [Decision(kind="plan", steps=steps, model="fake")],
        [Decision(kind="final", content="答案", model="fake")],
    ])
    runtime = _runtime(provider)
    result = await runtime.run("t", "hi", ToolContext("u1", "customer", "d", "s1", "r1"))
    assert result.status == "completed"
    assert result.answer == "答案"
    assert result.evidence["usage"]["tool_calls"] == 2


@pytest.mark.asyncio
async def test_plan_stops_on_approval(monkeypatch):
    import harness.core.runtime as rt
    from harness.core.policy import ToolPolicy
    monkeypatch.setattr(rt, "TraceLedger", _FakeLedger)
    provider = _StepProvider([[
        Decision(kind="plan", steps=[
            {"tool": "echo", "args": {"text": "a"}},
            {"tool": "approve_me", "args": {}},
        ], model="fake"),
    ]])
    runtime = _runtime_with_approval(provider)  # 注册 echo + approve_me(requires_approval=True)
    result = await runtime.run("t", "hi", ToolContext("u1", "customer", "d", "s1", "r1"))
    assert result.status == "pending_approval"
    assert result.pending["tool"] == "approve_me"


@pytest.mark.asyncio
async def test_plan_truncates_beyond_max(monkeypatch):
    import harness.core.runtime as rt
    from harness.core.llm import MAX_PLAN_STEPS
    monkeypatch.setattr(rt, "TraceLedger", _FakeLedger)
    steps = [{"tool": "echo", "args": {"text": str(i)}} for i in range(MAX_PLAN_STEPS + 2)]
    provider = _StepProvider([
        [Decision(kind="plan", steps=steps, model="fake")],
        [Decision(kind="final", content="答案", model="fake")],
    ])
    runtime = _runtime(provider)
    result = await runtime.run("t", "hi", ToolContext("u1", "customer", "d", "s1", "r1"))
    assert result.evidence["usage"]["tool_calls"] == MAX_PLAN_STEPS
```

（新增 `_runtime_with_approval` 辅助：在 `_runtime` 基础上多注册 `approve_me` 工具，`ToolPolicy("approve_me", permission="read", requires_approval=True)`。）

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_runtime_stream.py -v`
Expected: FAIL（plan 决策当前按 `tool` 走，`decision.tool` 为空导致未知工具 error）

- [ ] **Step 3: 实现**

`_run_events` 的决策分支改为：`final` 不变；其余统一成 `steps = decision.steps[:MAX_PLAN_STEPS] if kind=="plan" else [{"tool": decision.tool, "args": decision.args}]`。生成 `call_ids`、先 append 一条 assistant 消息（含全部 tool_calls），再逐 step：`tool.call`/`tool.result` ledger（+ trace 外抛）、`needs_approval` 置 `halted` 并 break、`error/denied` 计 tool_errors、append 对应 tool 消息。`halted` 时 break 外层循环；`plan.truncated` 在截断时记 ledger。单工具路径行为与重构前完全一致。

- [ ] **Step 4: 运行测试确认通过 + 全量回归**

Run: `.venv/Scripts/python -m pytest tests/test_runtime_stream.py -v` 然后 `.venv/Scripts/python -m pytest -q`
Expected: 新测试 PASS；全量（145 + 新增）全绿

- [ ] **Step 5: 提交（先展示变更摘要）**

`git add services/harness/core/runtime.py tests/test_runtime_stream.py` → `feat(harness): 运行时顺序执行计划步骤`

> **后端到此暂停，等人工确认后再看前端**（本次前端预计无需改动：`Agent.tsx` 只消费 SSE 事件，plan 的 trace 已实时推送）。

---

### Task 5: 前端确认 + 全量验证

**Files:**
- 大概率不改；确认 `web/src/pages/Agent.tsx` 无需改动（plan 只影响 trace 里的 tool.call/tool.result 数量与顺序，现有渲染已兼容）。

- [ ] **Step 1: 验证**

Run: `cd web && npm run build`
Expected: 构建成功（无代码改动也应通过）

- [ ] **Step 2: 提交计划文档**

`git add implementation_plan_planning.md` → `docs: LLM 规划与结构化输出实现计划`

---

## 验证命令（全部完成后）

```bash
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff check packages services tests
cd web && npm run build
```

## 风险点与取舍

1. **plan 是「顺序执行」，不是「并行」**：数据有依赖的多步必须顺序；独立步骤的并发执行（asyncio.gather）属路线图「并行多工具」单独项，本计划只搭好 `steps` 结构。
2. **多 tool_calls 依赖模型能力**：部分自建模型单轮只返回一个 tool_call，此时 plan 不触发，自动退化为现有 ReAct 单步，行为不变。
3. **结构化校验是「名字 + JSON 形状」层**：不做完整 JSON-Schema 校验（需引入 jsonschema 依赖）；参数语义正确性仍由工具 handler 与权限网关兜底。
4. **MAX_PLAN_STEPS=6 截断**：极端长计划被截断可能让 final compose 信息不全，但优于无界执行；截断在 trace 里可见（`plan.truncated`），可复盘。
5. **运行时改动复用同一工具网关**：单工具与 plan 归一化到同一循环，靠现有 145 个测试 + 新增 3 个运行时用例兜底不回归。
