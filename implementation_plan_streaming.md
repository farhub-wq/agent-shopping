# 流式输出 Implementation Plan

**Goal:** 让 Agent 对话真正流式：工具调用轨迹**实时**推送（而不是跑完一次性重放），最终答案**逐 token** 流出（而不是整段一起出现）。

**Architecture:** 三层改动。LLM 层新增 `decide_stream` 接口（默认退回非流式），OpenAI 兼容端点用 `stream: true` 逐 token 产出，`DeterministicPlanner` 对全文分块模拟流式；`FailoverProvider.decide_stream` 只在「首 token 前失败」时降级（流已开始则无法干净回退）。运行时把 `run()` 的 ReAct 循环抽成共享生成器 `_run_events`，`run()` 排空它取最终结果，`run_stream()` 把它映射成 SSE 事件（新增 `delta` 事件）。前端 `Agent.tsx` 累积 delta 渲染打字机效果。

**Tech Stack:** Python 3.11 · asyncio · httpx（`client.stream` + `aiter_lines`）· 现有 SSE 管道 · React 状态。

**Spec:** README「Agent 客服对话（SSE 流式）」；`runtime.run_stream` 现有 docstring 与实际行为不符（目前是重放，非流式）。

## 事件契约（本计划落定）

`run_stream` 产出的 SSE 事件形状：

```jsonc
{"type": "start", "skill": "...", "trust": "..."}      // 已有
{"type": "delta", "text": "..."}                        // 新增：final 答案的 token 分片
{"type": "trace", "event": {...}}                        // 已有，改为实时
{"type": "final", "result": {...}}                       // 已有
{"type": "error", "message": "..."}                      // 已有
```

## Global Constraints

- 新增/修改代码写**简明中文注释**（说明为什么），沿用文件现有注释密度。
- 不吞异常：流式中断要记日志并给 terminal 帧收尾，不能让前端 `busy` 永久卡死。
- `run()` 的非流式行为与返回结构**保持不变**（`approve_and_replay`、`stream=false` 路径、现有测试全部不受影响）。
- 前端不新增 `any`，不改 `api.ts` 的既有 `StreamEvent` 结构，只在 `Agent.tsx` 消费 `delta`。
- 提交：Conventional Commits + 简体中文；**后端完成后停下等确认再进前端**（AGENTS.md §3.3）。

## Review Focus

1. **流式中断必须有 terminal 帧收尾**：否则前端 `busy` 卡死 —— Task 3 的 `test_failover_stream_interrupt_ends_with_terminal` 锁定。
2. **`run()` 非流式路径不受 delta 影响**：`run()` 必须忽略 delta 帧、只吃 terminal 决策，返回结果与重构前一致 —— Task 4 的 `test_run_ignores_deltas` 锁定。
3. **OpenAI SSE 数据块字段缺失不崩**：无 `choices`/无 `delta`/`finish_reason` 为空都要容忍 —— Task 2 的 `test_openai_stream_delta_tolerates_missing_fields` 锁定。
4. **流式降级只发生在首 token 前**：已流出 token 后主模型失败，不得再叠降级模型输出造成内容错乱 —— Task 3 的 `test_failover_stream_no_fallback_after_first_token` 锁定。
5. **delta 与 final 不重不漏**：最终 `answer` 必须是完整文本，前端 `final` 落库的答案与 delta 累积一致 —— Task 4 的 `test_run_stream_delta_then_final` 锁定。

---

### Task 1: Decision 增量约定 + decide_stream 接口 + 离线规划器分块

**Files:**
- Modify: `services/harness/core/llm.py`（`Decision`、`LLMProvider`、`DeterministicPlanner`）
- Test: `tests/test_llm.py`

**Interfaces:**
- Produces:
  ```python
  # Decision.kind 新增第三种取值 "delta"：final 答案的 token 分片，content 为该片文本；
  # 流式序列以 0..n 个 delta 开头，恰好一帧 terminal（final|tool）收尾。
  class LLMProvider:
      async def decide(self, system, messages, tools) -> Decision: ...   # 不变
      async def decide_stream(self, system, messages, tools) -> AsyncIterator[Decision]:
          # 默认实现：退回非流式，只产出一帧 terminal。
          yield await self.decide(system, messages, tools)
  # DeterministicPlanner 覆盖 decide_stream：final 且 content 非空时按固定步长分块，
  # 先产出若干 delta 帧，再产出原 terminal 帧；tool 决策直接产出 terminal（无 delta）。
  ```

- [ ] **Step 1: 写失败测试** `tests/test_llm.py`（追加）

```python
from harness.core.llm import DeterministicPlanner


def _collect(gen):
    import asyncio
    return asyncio.run(_collect_all(gen))


async def _collect_all(gen):
    return [item async for item in gen]


@pytest.mark.asyncio
async def test_planner_decide_stream_chunks_final_answer():
    planner = DeterministicPlanner()
    frames = [item async for item in planner.decide_stream(
        "system", [{"role": "user", "content": "今天天气怎么样"}], []
    )]
    assert len(frames) >= 2  # 至少一帧 delta + 一帧 terminal
    assert all(item.kind == "delta" for item in frames[:-1])
    assert frames[-1].kind == "final"
    assert "".join(item.content for item in frames[:-1]) == frames[-1].content


@pytest.mark.asyncio
async def test_planner_decide_stream_tool_decision_is_single_frame():
    planner = DeterministicPlanner()
    frames = [item async for item in planner.decide_stream(
        "system", [{"role": "user", "content": "SKU-1001 还有货吗？"}],
        [{"name": "inventory_check"}],
    )]
    assert len(frames) == 1
    assert frames[0].kind == "tool"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_llm.py -k decide_stream -v`
Expected: FAIL（`AttributeError: LLMProvider.decide_stream`）

- [ ] **Step 3: 实现**

`LLMProvider` 加 `decide_stream`（默认退回 `decide`；顶部 import `AsyncIterator` from `collections.abc`）。`DeterministicPlanner.decide_stream`：调 `decide` 得 terminal；`kind == "final"` 且 `content` 非空时按 `CHUNK = 8` 字符切块产 delta，再产 terminal。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_llm.py -k decide_stream -v`
Expected: PASS（2 passed）

- [ ] **Step 5: 提交（先向用户展示变更摘要）**

`git add services/harness/core/llm.py tests/test_llm.py` → `feat(harness): 模型决策增加流式接口，离线规划器分块输出`

---

### Task 2: OpenAI 流式解析 + decide_stream

**Files:**
- Modify: `services/harness/core/llm.py`（`OpenAICompatibleProvider` + 纯函数 `_openai_stream_delta`）
- Test: `tests/test_llm.py`

**Interfaces:**
- Produces:
  ```python
  def _openai_stream_delta(chunk: dict[str, Any]) -> tuple[str, str, str]:
      # 解析单个 OpenAI SSE 数据块，返回 (content_delta, tool_name_delta, tool_args_delta)；
      # 缺失的字段返回空串，绝不抛 KeyError。
  # OpenAICompatibleProvider.decide_stream：payload 加 "stream": True，用 client.stream + aiter_lines 逐行
  # 读 "data:" 行；[DONE] 或流结束时：有 tool_call 则 json.loads 累积的 args 产 terminal tool，
  # 否则产 terminal final（content 为累积全文，model=self.model）；过程中 content delta 逐片产 delta 帧。
  ```

- [ ] **Step 1: 写失败测试** `tests/test_llm.py`（追加）

```python
from harness.core.llm import _openai_stream_delta


def test_openai_stream_delta_extracts_content():
    chunk = {"choices": [{"delta": {"content": "你好"}, "finish_reason": None}]}
    assert _openai_stream_delta(chunk) == ("你好", "", "")


def test_openai_stream_delta_extracts_tool_call():
    chunk = {"choices": [{"delta": {"tool_calls": [{"function": {"name": "echo", "arguments": "{\"x\":"}}]}}]}
    assert _openai_stream_delta(chunk) == ("", "echo", "{\"x\":")


def test_openai_stream_delta_tolerates_missing_fields():
    assert _openai_stream_delta({}) == ("", "", "")
    assert _openai_stream_delta({"choices": [{"delta": {}}]}) == ("", "", "")
    assert _openai_stream_delta({"choices": []}) == ("", "", "")
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_llm.py -k openai_stream_delta -v`
Expected: FAIL（`ImportError: cannot import name '_openai_stream_delta'`）

- [ ] **Step 3: 实现**

先写纯函数 `_openai_stream_delta`（用 `.get` 链安全取字段）。再写 `OpenAICompatibleProvider.decide_stream`：复用 `decide` 的 payload 构造（抽出 `_payload(...)` 小助手避免重复），加 `"stream": True`；`client.stream("POST", ...)` → `aiter_lines()`，逐行处理 `data:` 前缀，`[DONE]` 终止；用 `_openai_stream_delta` 累积文本与 tool name/args。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_llm.py -k openai_stream_delta -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交（先向用户展示变更摘要）**

`git add services/harness/core/llm.py tests/test_llm.py` → `feat(harness): OpenAI 兼容端点支持流式决策`

---

### Task 3: FailoverProvider 流式降级

**Files:**
- Modify: `services/harness/core/llm.py`（`FailoverProvider`）
- Test: `tests/test_llm.py`

**Interfaces:**
- Produces: `FailoverProvider.decide_stream(system, messages, tools) -> AsyncIterator[Decision]`
  - 熔断中：记 `LLM_CIRCUIT_OPEN`，直接走降级模型的流。
  - 主模型流式：转发明模型产出的每一帧；正常结束 `record_success()`。
  - 主模型异常：`record_failure()`；**若已转发过至少一帧**（无法撤回），产一帧 `Decision(kind="final", content="", model="unavailable")` 收尾并结束（不叠降级输出）；**若首帧前就失败**，记 `LLM_FALLBACK` 并转降级模型的流。

- [ ] **Step 1: 写失败测试** `tests/test_llm.py`（追加，复用已有 `_EchoProvider`/`_RaisingProvider` 思路，新增流式 stub）

```python
class _StreamProvider(LLMProvider):
    name = "stream"
    def __init__(self, frames, fail_after=0):
        self.frames = frames
        self.fail_after = fail_after
        self.emitted = 0
    async def decide_stream(self, system, messages, tools):
        for item in self.frames:
            self.emitted += 1
            yield item
            if self.fail_after and self.emitted >= self.fail_after:
                raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_failover_stream_relays_primary():
    from harness.core.circuit import CircuitBreaker
    from harness.core.llm import FailoverProvider

    primary = _StreamProvider([Decision(kind="delta", content="a"), Decision(kind="final", content="ab", model="stream")])
    fallback = _EchoProvider("B")
    provider = FailoverProvider(primary, fallback, CircuitBreaker())
    frames = [item async for item in provider.decide_stream("s", [], [])]
    assert [f.kind for f in frames] == ["delta", "final"]
    assert fallback.called is False


@pytest.mark.asyncio
async def test_failover_stream_falls_back_before_first_token():
    from harness.core.circuit import CircuitBreaker
    from harness.core.llm import FailoverProvider

    primary = _StreamProvider([], fail_after=0)  # 首帧前即失败
    fallback = _EchoProvider("B")
    provider = FailoverProvider(primary, fallback, CircuitBreaker())
    frames = [item async for item in provider.decide_stream("s", [], [])]
    assert frames[-1].kind == "final"
    assert frames[-1].content == "B"


@pytest.mark.asyncio
async def test_failover_stream_no_fallback_after_first_token():
    from harness.core.circuit import CircuitBreaker
    from harness.core.llm import FailoverProvider

    primary = _StreamProvider([Decision(kind="delta", content="a")], fail_after=1)
    fallback = _EchoProvider("B")
    provider = FailoverProvider(primary, fallback, CircuitBreaker())
    frames = [item async for item in provider.decide_stream("s", [], [])]
    assert frames[0].kind == "delta"
    assert frames[-1].kind == "final"
    assert frames[-1].model == "unavailable"  # 收尾帧，不回退降级模型
    assert fallback.called is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_llm.py -k failover_stream -v`
Expected: FAIL（`AttributeError: FailoverProvider.decide_stream`）

- [ ] **Step 3: 实现**

按 Interfaces 写 `FailoverProvider.decide_stream`；用 `emitted = False` 标记是否已转发过帧。熔断/降级/指标路径与现有 `decide` 一致（复用 `LLM_FALLBACK`/`LLM_CIRCUIT_OPEN`）。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_llm.py -v`
Expected: 全部 PASS（含既有 17 个）

- [ ] **Step 5: 提交（先向用户展示变更摘要）**

`git add services/harness/core/llm.py tests/test_llm.py` → `feat(harness): 熔断降级支持流式，仅首帧前可回退`

---

### Task 4: 运行时共享生成器 + 流式 SSE

**Files:**
- Modify: `services/harness/core/runtime.py`（抽 `_run_events`，重写 `run`/`run_stream`）
- Test: `tests/test_runtime_stream.py`（新建）

**Interfaces:**
- Consumes: `provider.decide_stream`（Task 1–3）
- Produces:
  ```python
  # AgentRuntime 内部：
  async def _run_events(self, skill_name, user_message, ctx, *, max_steps=None, stream: bool):
      # 产出 ("trace", event_dict) | ("delta", text) | ("done", AgentRunResult)
      # 与现有 run() 完全同一套 ReAct 循环 + 记忆 + 证据，唯一差异：
      #   决策用 decide_stream 逐帧读；delta 帧仅在 stream=True 时 yield；
      #   ledger 事件仍全部 append（证据不受影响），其中 4 类 SSE 事件额外 yield ("trace", ...)。
  async def run(self, ...) -> AgentRunResult:
      # 排空 _run_events(stream=False)，返回 ("done", result) 的 result。
  async def run_stream(self, ...) -> AsyncIterator[dict]:
      # start → 映射 _run_events(stream=True)：trace/delta/done → SSE 字典。
  ```

- [ ] **Step 1: 写失败测试** `tests/test_runtime_stream.py`（新建；monkeypatch `runtime.TraceLedger` 与注入 fake memory/provider）

```python
import pytest
from harness.core.llm import Decision, LLMProvider
from harness.core.manifest import SkillManifest
from harness.core.runtime import AgentRuntime
from harness.core.tools import Tool, ToolContext, ToolRegistry


class _FakeMemory:
    def __init__(self):
        self.redis = None

    async def extract_semantic(self, user_id, message, tenant): return None
    async def retrieve(self, user_id, message, k=5): return []
    async def working_messages(self, session_id, limit=6): return []
    async def append_turn(self, session_id, role, text): return None
    async def remember(self, user_id, text, *, kind, importance, tenant, metadata): return "r1"
    async def summarize_session(self, session_id, user_id): return None


class _FakeLedger:
    def __init__(self, run_id=None):
        self.run_id = run_id or "run-1"
        self.events = []

    async def append(self, event_type, **fields):
        self.events.append({"type": event_type, **fields})
        return self.events[-1]

    def evidence_card(self, **kwargs):
        return {"run_id": self.run_id, **kwargs}


class _StepProvider(LLMProvider):
    name = "fake"
    def __init__(self, steps):
        self.steps = steps  # 每个 step 是一列 Decision，按调用次序消费

    async def decide_stream(self, system, messages, tools):
        for decision in self.steps.pop(0):
            yield decision


def _runtime(provider):
    registry = ToolRegistry()
    async def echo(args, ctx): return {"echo": args.get("text")}
    registry.register(Tool("echo", "d", {"type": "object"}, echo,
                          __import__("harness.core.policy", fromlist=["ToolPolicy"]).ToolPolicy("echo", permission="read")))
    manifest = SkillManifest("t", "0.1", "d", "vendor", ["echo"], {}, 3, "你是助手", "x", findings=[])
    return AgentRuntime(registry, provider, {"t": manifest}, _FakeMemory()), registry


@pytest.mark.asyncio
async def test_run_stream_yields_delta_then_final(monkeypatch):
    import harness.core.runtime as rt
    monkeypatch.setattr(rt, "TraceLedger", _FakeLedger)
    provider = _StepProvider([[Decision(kind="delta", content="你好"), Decision(kind="final", content="你好世界", model="fake")]])
    runtime, _ = _runtime(provider)
    events = [item async for item in runtime.run_stream("t", "hi", ToolContext("u1", "customer", "d", "s1", "r1"))]
    assert events[0]["type"] == "start"
    assert any(e["type"] == "delta" and e["text"] == "你好" for e in events)
    final = next(e for e in events if e["type"] == "final")
    assert final["result"]["answer"] == "你好世界"


@pytest.mark.asyncio
async def test_run_ignores_deltas(monkeypatch):
    import harness.core.runtime as rt
    monkeypatch.setattr(rt, "TraceLedger", _FakeLedger)
    provider = _StepProvider([[Decision(kind="delta", content="x"), Decision(kind="final", content="答案", model="fake")]])
    runtime, _ = _runtime(provider)
    result = await runtime.run("t", "hi", ToolContext("u1", "customer", "d", "s1", "r1"))
    assert result.answer == "答案"
    assert result.status == "completed"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_runtime_stream.py -v`
Expected: FAIL（`run_stream` 目前不产出 delta 事件 / 或 `_run_events` 不存在）

- [ ] **Step 3: 实现**

把现有 `run()` 的循环体搬进 `_run_events`：决策改为 `async for frame in self.provider.decide_stream(...)`（delta 帧仅在 `stream` 时 yield；terminal 帧进入原有 final/tool/approval 分支）。`run()` 与 `run_stream()` 改为薄封装。`model_used`、`pending`、记忆写入、证据卡逻辑原样保留。`run_stream` 的 trace 过滤集合保持 `{"agent.step", "tool.call", "tool.result", "approval.requested"}`。

- [ ] **Step 4: 运行测试确认通过 + 全量回归**

Run: `.venv/Scripts/python -m pytest tests/test_runtime_stream.py -v` 然后 `.venv/Scripts/python -m pytest -q`
Expected: 新测试 PASS；全量（原 135 + 新增）全绿

- [ ] **Step 5: 提交（先向用户展示变更摘要）**

`git add services/harness/core/runtime.py tests/test_runtime_stream.py` → `feat(harness): 运行时流式输出，工具轨迹实时推送、答案逐 token 流出`

> **后端到此暂停，等人工确认后再进前端（AGENTS.md §3.3）。**

---

### Task 5: 前端增量渲染

**Files:**
- Modify: `web/src/pages/Agent.tsx`

**Interfaces:**
- Consumes: SSE `delta` 事件（Task 4）

- [ ] **Step 1: 实现**

`Agent.tsx` 新增 `streaming` 字符串 state；`send()` 里 `onEvent` 增加 `if (event.type === "delta") setStreaming(prev => prev + event.text)`；`final` 分支追加消息后 `setStreaming("")`；`send` 开始时 `setStreaming("")`。渲染：`busy` 时气泡显示 `streaming || "思考中…"`。

- [ ] **Step 2: 验证**

Run: `cd web && npm run build`
Expected: 构建成功

Run（冒烟）：起后端 + 前端，在 Agent 页发一条消息，观察工具轨迹实时出现、答案打字机式逐字流出（离线规划器为分块模拟）。

- [ ] **Step 3: 提交（先向用户展示变更摘要）**

`git add web/src/pages/Agent.tsx` → `feat(web): Agent 对话打字机式流式渲染`

---

## 验证命令（全部完成后）

```bash
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff check packages services tests
cd web && npm run build
```

## 风险点与取舍

1. **流式降级只在首 token 前**：主模型流出中途失败无法撤回已推文本，只能收尾（不回退降级模型），否则会叠出两段答案。这是 SSE 语义下的必然取舍。
2. **工具调用不流式**：工具是离散决策（快、非文本），只流式 final 答案；工具轨迹以 `trace` 事件实时推送即可。
3. **离线规划器是「分块伪流式」**：它瞬间出全文，分块只是让默认演示模式也有打字机效果；真实逐 token 流式只在配了 OpenAI 兼容端点时生效。
4. **兼容端点的 SSE 格式差异**：不同 OpenAI 兼容服务对 `delta.tool_calls` 的字段命名可能略有差异，`_openai_stream_delta` 用安全取值容忍缺失，异常端点表现为「不流式但仍返回完整 final」而非崩溃。
5. **运行时重构是本次最大风险**：`run()`/`run_stream()` 从「各自实现」改为「共享 `_run_events`」，靠现有 135 个测试 + 新增运行时测试兜底行为不变；`approve_and_replay` 不涉及模型决策，保持原样。
