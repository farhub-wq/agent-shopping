import pytest
from harness.core.llm import MAX_PLAN_STEPS, Decision, LLMProvider
from harness.core.manifest import SkillManifest
from harness.core.policy import ToolPolicy
from harness.core.runtime import AgentRuntime
from harness.core.tools import Tool, ToolContext, ToolRegistry


class _FakeRedis:
    # 待审批负载重放依赖 redis 的 set/get/delete，仅保留运行时用到的三个方法。
    def __init__(self):
        self.store: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)


class _FakeMemory:
    def __init__(self):
        self.redis = _FakeRedis()

    async def extract_semantic(self, user_id, message, tenant):
        return None

    async def retrieve(self, user_id, message, k=5):
        return []

    async def working_messages(self, session_id, limit=6):
        return []

    async def append_turn(self, session_id, role, text):
        return None

    async def remember(self, user_id, text, *, kind, importance, tenant, metadata):
        return "r1"

    async def summarize_session(self, session_id, user_id):
        return None


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

    async def echo(args, ctx):
        return {"echo": args.get("text")}

    registry.register(Tool("echo", "d", {"type": "object"}, echo, ToolPolicy("echo", permission="read")))
    manifest = SkillManifest("t", "0.1", "d", "vendor", ["echo"], {}, 3, "你是助手", "x", findings=[])
    return AgentRuntime(registry, provider, {"t": manifest}, _FakeMemory())


def _runtime_with_approval(provider):
    # 在 _runtime 基础上多注册一个需审批工具，用来验证 plan 中途撞审批即停。
    registry = ToolRegistry()

    async def echo(args, ctx):
        return {"echo": args.get("text")}

    async def approve_me(args, ctx):
        return {"approved": True}

    registry.register(Tool("echo", "d", {"type": "object"}, echo, ToolPolicy("echo", permission="read")))
    registry.register(
        Tool(
            "approve_me",
            "d",
            {"type": "object"},
            approve_me,
            ToolPolicy("approve_me", permission="read", requires_approval=True),
        )
    )
    manifest = SkillManifest(
        "t", "0.1", "d", "vendor", ["echo", "approve_me"], {}, 3, "你是助手", "x", findings=[]
    )
    return AgentRuntime(registry, provider, {"t": manifest}, _FakeMemory())


@pytest.mark.asyncio
async def test_run_stream_yields_delta_then_final(monkeypatch):
    import harness.core.runtime as rt

    monkeypatch.setattr(rt, "TraceLedger", _FakeLedger)
    provider = _StepProvider(
        [[Decision(kind="delta", content="你好"), Decision(kind="final", content="你好世界", model="fake")]]
    )
    runtime = _runtime(provider)
    events = [item async for item in runtime.run_stream("t", "hi", ToolContext("u1", "customer", "d", "s1", "r1"))]
    assert events[0]["type"] == "start"
    assert any(e["type"] == "delta" and e["text"] == "你好" for e in events)
    final = next(e for e in events if e["type"] == "final")
    assert final["result"]["answer"] == "你好世界"


@pytest.mark.asyncio
async def test_run_ignores_deltas(monkeypatch):
    import harness.core.runtime as rt

    monkeypatch.setattr(rt, "TraceLedger", _FakeLedger)
    provider = _StepProvider(
        [[Decision(kind="delta", content="x"), Decision(kind="final", content="答案", model="fake")]]
    )
    runtime = _runtime(provider)
    result = await runtime.run("t", "hi", ToolContext("u1", "customer", "d", "s1", "r1"))
    assert result.answer == "答案"
    assert result.status == "completed"


@pytest.mark.asyncio
async def test_run_plan_executes_all_steps_then_final(monkeypatch):
    import harness.core.runtime as rt

    monkeypatch.setattr(rt, "TraceLedger", _FakeLedger)
    steps = [
        {"tool": "echo", "args": {"text": "a"}},
        {"tool": "echo", "args": {"text": "b"}},
    ]
    provider = _StepProvider(
        [
            [Decision(kind="plan", steps=steps, model="fake")],
            [Decision(kind="final", content="答案", model="fake")],
        ]
    )
    runtime = _runtime(provider)
    result = await runtime.run("t", "hi", ToolContext("u1", "customer", "d", "s1", "r1"))
    assert result.status == "completed"
    assert result.answer == "答案"
    assert result.evidence["usage"]["tool_calls"] == 2


@pytest.mark.asyncio
async def test_plan_stops_on_approval(monkeypatch):
    import harness.core.runtime as rt

    monkeypatch.setattr(rt, "TraceLedger", _FakeLedger)
    provider = _StepProvider(
        [
            [
                Decision(
                    kind="plan",
                    steps=[
                        {"tool": "echo", "args": {"text": "a"}},
                        {"tool": "approve_me", "args": {}},
                    ],
                    model="fake",
                )
            ]
        ]
    )
    runtime = _runtime_with_approval(provider)
    result = await runtime.run("t", "hi", ToolContext("u1", "customer", "d", "s1", "r1"))
    assert result.status == "pending_approval"
    assert result.pending["tool"] == "approve_me"


@pytest.mark.asyncio
async def test_plan_truncates_beyond_max(monkeypatch):
    import harness.core.runtime as rt

    monkeypatch.setattr(rt, "TraceLedger", _FakeLedger)
    steps = [{"tool": "echo", "args": {"text": str(i)}} for i in range(MAX_PLAN_STEPS + 2)]
    provider = _StepProvider(
        [
            [Decision(kind="plan", steps=steps, model="fake")],
            [Decision(kind="final", content="答案", model="fake")],
        ]
    )
    runtime = _runtime(provider)
    result = await runtime.run("t", "hi", ToolContext("u1", "customer", "d", "s1", "r1"))
    assert result.evidence["usage"]["tool_calls"] == MAX_PLAN_STEPS
