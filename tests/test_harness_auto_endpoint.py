import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from harness import app as harness_app
from harness.core.agents import build_builtin_agents
from harness.core.llm import LLMProvider
from harness.core.manifest import SkillManifest
from harness.core.orchestrator import AgentOrchestrator, AgentRunSummary, OrchestratedAgentRunResult
from harness.core.runtime import AgentRunResult, AgentRuntime
from shop_common.auth import Principal
from shop_common.errors import ApiError

CUSTOMER = Principal("customer-1", "customer", "tenant-1")
OPERATOR = Principal("operator-1", "operator", "tenant-1")
ADMIN = Principal("admin-1", "admin", "tenant-1")


@pytest.fixture
def api(monkeypatch):
    builtins = build_builtin_agents()
    ops = SkillManifest(
        name="ops-catalog",
        version="1.0.0",
        description="运营目录",
        trust="reviewed",
        allowed_tools=[],
        permissions={},
        max_steps=1,
        body="运营技能",
        path="ops-catalog",
        allowed_roles=["operator", "admin"],
    )
    runtime = SimpleNamespace(
        skills={
            **{name: definition.manifest for name, definition in builtins.items()},
            "ops-catalog": ops,
        }
    )
    monkeypatch.setattr(harness_app, "runtime", runtime)
    monkeypatch.setattr(harness_app, "builtin_agents", builtins, raising=False)
    monkeypatch.setattr(harness_app, "orchestrator", object(), raising=False)
    return SimpleNamespace(runtime=runtime, builtins=builtins)


@pytest.mark.asyncio
async def test_customer_agent_list_puts_auto_before_visible_experts(api):
    result = await harness_app.list_agents(CUSTOMER)

    names = [item["name"] for item in result["items"]]
    assert names[0] == "auto"
    assert {"auto", "chitchat", "presales", "inventory", "aftersales", "customer-service"} <= set(names)
    assert "ops-catalog" not in names
    assert result["items"][0] == {
        "name": "auto",
        "description": "自动识别问题并串行分派专家",
        "trust": "reviewed",
        "allowed_tools": [],
        "display_name": "自动分派",
        "verdict": "allow",
    }
    assert next(item for item in result["items"] if item["name"] == "inventory")["display_name"] == "库存助手"


@pytest.mark.asyncio
async def test_operator_agent_list_excludes_auto_and_keeps_ops(api):
    result = await harness_app.list_agents(OPERATOR)

    names = {item["name"] for item in result["items"]}
    assert "auto" not in names
    assert "ops-catalog" in names
    assert "inventory" not in names
    assert next(item for item in result["items"] if item["name"] == "ops-catalog")["display_name"] == "ops-catalog"


@pytest.mark.asyncio
async def test_builtin_verify_returns_runtime_manifest_without_disk_scan(api, monkeypatch):
    scan = Mock(side_effect=AssertionError("内置 Agent 不应扫描磁盘"))
    monkeypatch.setattr(harness_app, "load_skill", scan)

    result = await harness_app.verify_skill("inventory")

    assert result == api.runtime.skills["inventory"].to_dict()
    assert result["path"] == "builtin:inventory"
    scan.assert_not_called()


@pytest.mark.asyncio
async def test_disk_skill_verify_still_rescans(api, monkeypatch):
    scanned = SkillManifest(
        name="ops-catalog",
        version="2.0.0",
        description="最新扫描",
        trust="reviewed",
        allowed_tools=[],
        permissions={},
        max_steps=1,
        body="最新内容",
        path="disk",
        allowed_roles=["operator"],
    )
    scan = Mock(return_value=scanned)
    monkeypatch.setattr(harness_app, "load_skill", scan)

    result = await harness_app.verify_skill("ops-catalog")

    assert result == scanned.to_dict()
    scan.assert_called_once_with(harness_app.SKILL_ROOT / "ops-catalog" / "SKILL.md")


def _result(skill, *, route=None, agent_runs=None):
    kwargs = {}
    if route is not None:
        kwargs = {"route": route, "agent_runs": agent_runs or []}
    result_type = OrchestratedAgentRunResult if skill == "auto" else AgentRunResult
    return result_type(
        run_id=f"{skill}-run",
        skill=skill,
        answer=f"{skill} answer",
        status="completed",
        evidence={"run_id": f"{skill}-run"},
        **kwargs,
    )


class _FailingRouteProvider(LLMProvider):
    name = "fake"

    def __init__(self):
        self.calls = 0

    async def decide(self, system, messages, tools):
        self.calls += 1
        raise RuntimeError("route provider unavailable")


class _MemoryLedger:
    def __init__(self, run_id):
        self.run_id = run_id
        self.events = []

    @classmethod
    async def load(cls, run_id):
        return cls(run_id)

    async def append(self, event_type, **fields):
        event = {"type": event_type, **fields}
        self.events.append(event)
        return event

    def evidence_card(self, **fields):
        return {"run_id": self.run_id, **fields}


class _FallbackRuntime:
    def __init__(self, agents):
        self.skills = {name: definition.manifest for name, definition in agents.items()}
        self.memory = SimpleNamespace(
            redis=SimpleNamespace(
                eval=AsyncMock(return_value=1000),
                incrby=AsyncMock(return_value=0),
            ),
            working_messages=AsyncMock(return_value=[]),
            extract_semantic=AsyncMock(return_value=[]),
            append_turn=AsyncMock(),
            remember=AsyncMock(return_value="memory-1"),
            summarize_session=AsyncMock(),
        )
        self._record_usage = AsyncMock()

    def _child_result(self, skill_name, ctx):
        return AgentRunResult(
            run_id=ctx.run_id,
            skill=skill_name,
            answer="综合客服答复",
            status="completed",
            evidence={"run_id": ctx.run_id, "model": "fake"},
        )

    async def run_delegated(self, skill_name, message, ctx, *, provider, limits):
        return self._child_result(skill_name, ctx)

    async def run_delegated_stream(self, skill_name, message, ctx, *, provider, limits):
        yield {"type": "start", "skill": skill_name}
        yield {"type": "final", "result": self._child_result(skill_name, ctx).to_dict()}


@pytest.fixture
def chat_api(api, monkeypatch):
    provider = SimpleNamespace(name="fake")
    session = SimpleNamespace(add=Mock(), commit=AsyncMock(), scalar=AsyncMock(return_value=None))

    @asynccontextmanager
    async def session_factory():
        yield session

    auto_result = _result(
        "auto",
        route={"agent_ids": ["inventory"], "confidence": 0.9, "reason": "库存", "source": "llm"},
        agent_runs=[AgentRunSummary("inventory", "child-run", "completed", "有货", {})],
    )
    orchestrator = SimpleNamespace(
        run=AsyncMock(return_value=auto_result),
        run_stream=Mock(),
    )
    runtime = SimpleNamespace(
        skills=api.runtime.skills,
        run=AsyncMock(return_value=_result("inventory")),
        run_stream=Mock(),
    )
    monkeypatch.setattr(harness_app, "build_provider", Mock(return_value=provider))
    monkeypatch.setattr(harness_app, "get_session_factory", Mock(return_value=session_factory))
    monkeypatch.setattr(harness_app, "resolve_provider", AsyncMock(return_value=None))
    monkeypatch.setattr(harness_app, "_append_message", AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr(harness_app, "_persist_assistant", AsyncMock())
    monkeypatch.setattr(harness_app, "memory", None)
    monkeypatch.setattr(harness_app, "runtime", runtime)
    monkeypatch.setattr(harness_app, "orchestrator", orchestrator)
    return SimpleNamespace(
        provider=provider,
        session=session,
        orchestrator=orchestrator,
        runtime=runtime,
        builtins=api.builtins,
        auto_result=auto_result,
        build_provider=harness_app.build_provider,
        get_session_factory=harness_app.get_session_factory,
        resolve_provider=harness_app.resolve_provider,
        persist=harness_app._persist_assistant,
        append=harness_app._append_message,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("principal", [OPERATOR, ADMIN], ids=["operator", "admin"])
@pytest.mark.parametrize("stream", [False, True])
async def test_staff_cannot_use_auto(chat_api, principal, stream):
    with pytest.raises(ApiError) as exc:
        await harness_app.chat(
            "auto", harness_app.ChatRequest(message="你好"), principal, stream=stream
        )

    assert exc.value.status_code == 403
    chat_api.orchestrator.run.assert_not_awaited()
    chat_api.build_provider.assert_not_called()
    chat_api.get_session_factory.assert_not_called()
    chat_api.resolve_provider.assert_not_called()
    chat_api.append.assert_not_called()
    chat_api.orchestrator.run.assert_not_called()
    chat_api.orchestrator.run_stream.assert_not_called()
    chat_api.runtime.run.assert_not_called()
    chat_api.runtime.run_stream.assert_not_called()


@pytest.mark.asyncio
async def test_lifespan_assembles_real_runtime_and_orchestrator_and_closes_resources(monkeypatch):
    disk_inventory = SkillManifest(
        name="inventory",
        version="9.0.0",
        description="磁盘库存",
        trust="reviewed",
        allowed_tools=[],
        permissions={},
        max_steps=1,
        body="磁盘内容",
        path="disk:inventory",
    )
    disk_ops = SkillManifest(
        name="ops-catalog",
        version="1.0.0",
        description="运营目录",
        trust="reviewed",
        allowed_tools=[],
        permissions={},
        max_steps=1,
        body="运营技能",
        path="disk:ops-catalog",
        allowed_roles=["operator", "admin"],
    )
    session = SimpleNamespace()
    provider = SimpleNamespace(close=AsyncMock())
    redis_client = SimpleNamespace(close=AsyncMock())
    hub = SimpleNamespace(clients={})

    @asynccontextmanager
    async def session_factory():
        yield session

    for name in ("redis", "bus", "memory", "runtime", "builtin_agents", "orchestrator", "mcp_server", "mcp_hub"):
        monkeypatch.setattr(harness_app, name, None)
    monkeypatch.setattr(harness_app, "configure_logging", Mock())
    monkeypatch.setattr(harness_app, "validate_production_settings", Mock())
    monkeypatch.setattr(harness_app, "validate_sandbox_production", Mock())
    monkeypatch.setattr(harness_app, "init_models", AsyncMock())
    monkeypatch.setattr(harness_app, "get_session_factory", Mock(return_value=session_factory))
    monkeypatch.setattr(harness_app, "ensure_default_free_ai", AsyncMock())
    monkeypatch.setattr(harness_app, "create_redis", Mock(return_value=redis_client))
    monkeypatch.setattr(harness_app, "load_all", Mock(return_value=[disk_inventory, disk_ops]))
    monkeypatch.setattr(harness_app, "build_provider", Mock(return_value=provider))
    monkeypatch.setattr(harness_app, "build_mcp_hub", Mock(return_value=hub))
    monkeypatch.setattr(harness_app, "discover_and_register", AsyncMock(return_value=[]))
    monkeypatch.setattr(harness_app, "close_mongo", AsyncMock())
    monkeypatch.setattr(harness_app, "dispose_engine", AsyncMock())

    async with harness_app.lifespan(harness_app.app):
        assert isinstance(harness_app.runtime, AgentRuntime)
        assert isinstance(harness_app.orchestrator, AgentOrchestrator)
        assert harness_app.orchestrator.runtime is harness_app.runtime
        assert set(harness_app.builtin_agents) == set(build_builtin_agents())
        for name, definition in harness_app.builtin_agents.items():
            assert harness_app.runtime.skills[name] is definition.manifest
        assert harness_app.runtime.skills["inventory"] is not disk_inventory
        assert harness_app.runtime.skills["inventory"].path == "builtin:inventory"
        assert harness_app.runtime.skills["ops-catalog"] is disk_ops
        assert harness_app.runtime.provider is provider
        assert harness_app.memory.redis is redis_client
        assert harness_app.mcp_hub is hub
        assert harness_app.mcp_server.registry is harness_app.registry
        harness_app.init_models.assert_awaited_once()
        harness_app.ensure_default_free_ai.assert_awaited_once_with(session)
        harness_app.discover_and_register.assert_awaited_once_with(harness_app.registry, hub)

    provider.close.assert_awaited_once()
    redis_client.close.assert_awaited_once()
    harness_app.close_mongo.assert_awaited_once()
    harness_app.dispose_engine.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_real_orchestrator_fallback_emits_event_and_metrics(
    chat_api, metric_probe, monkeypatch, stream
):
    import harness.core.orchestrator as orchestrator_module
    import harness.core.runtime as runtime_module

    provider = _FailingRouteProvider()
    runtime = _FallbackRuntime(chat_api.builtins)
    orchestrator = AgentOrchestrator(runtime, chat_api.builtins)
    chat_api.build_provider.return_value = provider
    monkeypatch.setattr(harness_app, "runtime", runtime)
    monkeypatch.setattr(harness_app, "orchestrator", orchestrator)
    monkeypatch.setattr(orchestrator_module, "TraceLedger", _MemoryLedger)
    monkeypatch.setattr(
        runtime_module,
        "settings",
        SimpleNamespace(agent_run_token_budget=1000, agent_user_daily_token_quota=100_000),
    )

    response = await harness_app.chat(
        "auto", harness_app.ChatRequest(message="无法识别的问题"), CUSTOMER, stream=stream
    )

    if stream:
        chunks = [chunk.decode() if isinstance(chunk, bytes) else chunk async for chunk in response.body_iterator]
        payloads = [json.loads(chunk.removeprefix("data: ")) for chunk in chunks[:-1]]
        agent_events = [item["event"] for item in payloads if item["type"] == "agent"]
        assert [event["type"] for event in agent_events] == [
            "route", "fallback", "agent.start", "agent.end"
        ]
        assert agent_events[0]["source"] == "fallback"
        assert agent_events[1] == {"type": "fallback", "reason": agent_events[0]["reason"]}
        assert sum(item["type"] == "final" for item in payloads) == 1
        final = payloads[-1]
        assert final["type"] == "final"
        result = final["result"]
    else:
        assert "events" not in response
        result = response

    assert result["route"]["source"] == "fallback"
    assert result["route"]["agent_ids"] == ["customer-service"]
    assert result["status"] == "completed"
    assert result["answer"] == "综合客服答复"
    assert [child["agent"] for child in result["agent_runs"]] == ["customer-service"]
    assert provider.calls == 1
    assert metric_probe.route.label_calls == [("harness", "fallback", "fallback")]
    assert metric_probe.delegation.label_calls == [("harness", "customer-service", "completed")]
    assert len(metric_probe.duration.observations) == 1


@pytest.mark.asyncio
async def test_auto_stream_emits_only_one_fallback_for_repeated_fallback_route(chat_api):
    fallback_route = {
        "type": "agent",
        "event": {"type": "route", "source": "fallback", "reason": "回退综合客服"},
    }

    async def stream(*args, **kwargs):
        yield fallback_route
        yield fallback_route
        yield {"type": "agent", "event": {"type": "fallback", "reason": "回退综合客服"}}
        yield {"type": "final", "result": chat_api.auto_result.to_dict()}

    chat_api.orchestrator.run_stream = Mock(return_value=stream())
    response = await harness_app.chat(
        "auto", harness_app.ChatRequest(message="查库存"), CUSTOMER, stream=True
    )
    chunks = [chunk.decode() if isinstance(chunk, bytes) else chunk async for chunk in response.body_iterator]
    payloads = [json.loads(chunk.removeprefix("data: ")) for chunk in chunks[:-1]]
    fallback_events = [
        item for item in payloads
        if item.get("type") == "agent" and item["event"]["type"] == "fallback"
    ]

    assert len(fallback_events) == 1
    assert sum(item.get("type") == "agent" and item["event"]["type"] == "route" for item in payloads) == 2
    assert sum(item["type"] == "final" for item in payloads) == 1


@pytest.mark.asyncio
async def test_auto_nonstream_returns_root_fields_and_persists_conversation(chat_api):
    response = await harness_app.chat(
        "auto", harness_app.ChatRequest(message="查库存"), CUSTOMER, stream=False
    )

    assert {"run_id", "conversation_id", "route", "agent_runs"} <= response.keys()
    assert response["route"]["agent_ids"] == ["inventory"]
    assert response["agent_runs"][0]["run_id"] == "child-run"
    conversation = chat_api.session.add.call_args.args[0]
    assert response["conversation_id"] == conversation.id
    assert conversation.skill == "auto"
    chat_api.orchestrator.run.assert_awaited_once()
    chat_api.runtime.run.assert_not_awaited()
    call = chat_api.orchestrator.run.await_args
    assert call.kwargs["provider"] is chat_api.provider
    assert call.args[1].conversation_id == conversation.id
    chat_api.append.assert_awaited_once()
    chat_api.persist.assert_awaited_once_with(conversation.id, "auto answer")


@pytest.mark.asyncio
async def test_auto_stream_forwards_agent_events_and_one_final(chat_api, metric_probe):
    final = {"type": "final", "result": chat_api.auto_result.to_dict()}
    events = [
        {"type": "start", "skill": "auto"},
        {"type": "agent", "event": {"type": "route", "source": "llm", "agent_ids": ["inventory"]}},
        {"type": "agent", "event": {"type": "agent.start", "agent": "inventory", "run_id": "child-run"}},
        {"type": "delta", "text": "有货"},
        {"type": "agent", "event": {"type": "agent.end", "agent": "inventory", "run_id": "child-run", "status": "completed"}},
        final,
    ]

    async def stream(*args, **kwargs):
        for event in events:
            yield event

    chat_api.orchestrator.run_stream = Mock(return_value=stream())
    response = await harness_app.chat(
        "auto", harness_app.ChatRequest(message="查库存"), CUSTOMER, stream=True
    )
    chunks = [chunk.decode() if isinstance(chunk, bytes) else chunk async for chunk in response.body_iterator]
    payloads = [json.loads(chunk.removeprefix("data: ")) for chunk in chunks[:-1]]

    assert chunks[-1] == "data: [DONE]\n\n"
    assert [item["event"]["type"] for item in payloads if item["type"] == "agent"] == [
        "route", "agent.start", "agent.end"
    ]
    assert sum(item["type"] == "final" for item in payloads) == 1
    assert payloads[-1]["type"] == "final"
    conversation = chat_api.session.add.call_args.args[0]
    assert payloads[-1]["result"]["conversation_id"] == conversation.id
    assert metric_probe.route.label_calls == [("harness", "llm", "completed")]
    assert metric_probe.delegation.label_calls == [("harness", "inventory", "completed")]
    assert len(metric_probe.duration.observations) == 1
    chat_api.persist.assert_awaited_once_with(conversation.id, "auto answer")
    chat_api.runtime.run.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_direct_inventory_uses_runtime_not_orchestrator(chat_api, stream):
    if stream:
        async def runtime_stream(*args, **kwargs):
            yield {"type": "final", "result": _result("inventory").to_dict()}

        chat_api.runtime.run_stream = Mock(side_effect=runtime_stream)
        response = await harness_app.chat(
            "inventory", harness_app.ChatRequest(message="查库存"), CUSTOMER, stream=True
        )
        chunks = [chunk.decode() if isinstance(chunk, bytes) else chunk async for chunk in response.body_iterator]
        assert json.loads(chunks[0].removeprefix("data: "))["result"]["skill"] == "inventory"
        chat_api.runtime.run_stream.assert_called_once()
    else:
        response = await harness_app.chat(
            "inventory", harness_app.ChatRequest(message="查库存"), CUSTOMER, stream=False
        )
        assert response["skill"] == "inventory"
        chat_api.runtime.run.assert_awaited_once()

    chat_api.orchestrator.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_customer_service_direct_response_stays_compatible(chat_api):
    direct = _result("customer-service")
    chat_api.runtime.run.return_value = direct

    response = await harness_app.chat(
        "customer-service", harness_app.ChatRequest(message="人工客服"), OPERATOR, stream=False
    )

    assert response["run_id"] == direct.run_id
    assert response["skill"] == "customer-service"
    assert response["answer"] == direct.answer
    assert response["status"] == direct.status
    assert response["conversation_id"]
    assert "route" not in response
    assert "agent_runs" not in response
    chat_api.runtime.run.assert_awaited_once()
    chat_api.orchestrator.run.assert_not_awaited()


class _MetricProbe:
    def __init__(self):
        self.label_calls = []
        self.increments = []
        self.observations = []

    def labels(self, *labels):
        self.label_calls.append(labels)
        return self

    def inc(self, amount=1):
        self.increments.append(amount)

    def observe(self, value):
        self.observations.append(value)


@pytest.fixture
def metric_probe(monkeypatch):
    from harness.core import metrics

    probe = SimpleNamespace(route=_MetricProbe(), delegation=_MetricProbe(), duration=_MetricProbe())
    monkeypatch.setattr(metrics, "AGENT_ROUTE_TOTAL", probe.route)
    monkeypatch.setattr(metrics, "AGENT_DELEGATION_TOTAL", probe.delegation)
    monkeypatch.setattr(metrics, "AGENT_ORCHESTRATION_DURATION_SECONDS", probe.duration)
    return probe


@pytest.mark.asyncio
async def test_auto_nonstream_metrics_count_route_child_and_duration_once(chat_api, metric_probe):
    await harness_app.chat(
        "auto", harness_app.ChatRequest(message="查库存"), CUSTOMER, stream=False
    )

    assert metric_probe.route.label_calls == [("harness", "llm", "completed")]
    assert metric_probe.route.increments == [1]
    assert metric_probe.delegation.label_calls == [("harness", "inventory", "completed")]
    assert metric_probe.delegation.increments == [1]
    assert metric_probe.duration.label_calls == [("harness",)]
    assert len(metric_probe.duration.observations) == 1
    assert metric_probe.duration.observations[0] >= 0


@pytest.mark.asyncio
async def test_metrics_normalize_unknown_labels_and_deduplicate_child(metric_probe):
    from harness.core.metrics import OrchestrationMetrics

    tracker = OrchestrationMetrics()
    tracker.observe_event(
        {"type": "agent", "event": {"type": "route", "source": "request-text"}}
    )
    tracker.observe_event(
        {"type": "agent", "event": {"type": "agent.start", "agent": "request-text", "run_id": "run-secret"}}
    )
    tracker.observe_event(
        {"type": "agent", "event": {"type": "agent.end", "agent": "request-text", "run_id": "run-secret", "status": "completed"}}
    )
    tracker.observe_result(
        _result(
            "auto",
            route={"source": "request-text"},
            agent_runs=[AgentRunSummary("request-text", "run-secret", "completed", "answer", {})],
        )
    )
    tracker.finish("completed")
    tracker.finish("error")

    assert metric_probe.route.label_calls == [("harness", "unknown", "completed")]
    assert metric_probe.delegation.label_calls == [("harness", "other", "completed")]
    assert metric_probe.duration.label_calls == [("harness",)]
    assert len(metric_probe.duration.observations) == 1
    assert all("request-text" not in labels and "run-secret" not in labels for labels in metric_probe.route.label_calls)


def test_metrics_map_unknown_result_status_and_service(metric_probe):
    from harness.core.metrics import OrchestrationMetrics

    tracker = OrchestrationMetrics("request-service")
    tracker.observe_result(
        _result(
            "auto",
            route={"source": "llm"},
            agent_runs=[AgentRunSummary("inventory", "child-run", "request-status", "answer", {})],
        )
    )
    tracker.finish("request-result")

    assert metric_probe.route.label_calls == [("other", "llm", "unknown")]
    assert metric_probe.delegation.label_calls == [("other", "inventory", "unknown")]
    assert metric_probe.duration.label_calls == [("other",)]


@pytest.mark.asyncio
@pytest.mark.parametrize("termination", ["error", "cancel"])
async def test_auto_nonstream_metrics_finalize_exception_and_cancellation(
    chat_api, metric_probe, termination
):
    failure = RuntimeError("sensitive provider message") if termination == "error" else asyncio.CancelledError()
    chat_api.orchestrator.run.side_effect = failure

    with pytest.raises(type(failure)):
        await harness_app.chat(
            "auto", harness_app.ChatRequest(message="查库存"), CUSTOMER, stream=False
        )

    expected = "error" if termination == "error" else "cancelled"
    assert metric_probe.route.label_calls == [("harness", "unknown", expected)]
    assert metric_probe.delegation.label_calls == []
    assert len(metric_probe.duration.observations) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("termination", ["close", "error", "cancel"])
async def test_auto_stream_metrics_finalize_interrupted_child_once(
    chat_api, metric_probe, termination
):
    started = asyncio.Event()

    async def stream(*args, **kwargs):
        yield {"type": "agent", "event": {"type": "route", "source": "rules"}}
        yield {"type": "agent", "event": {"type": "agent.start", "agent": "inventory", "run_id": "child-run"}}
        started.set()
        if termination == "error":
            raise RuntimeError("sensitive stream failure")
        await asyncio.Event().wait()

    chat_api.orchestrator.run_stream = Mock(return_value=stream())
    response = await harness_app.chat(
        "auto", harness_app.ChatRequest(message="查库存"), CUSTOMER, stream=True
    )
    expected = {"close": "cancelled", "error": "error", "cancel": "cancelled"}[termination]

    if termination == "close":
        await response.body_iterator.__anext__()
        await response.body_iterator.__anext__()
        await response.body_iterator.aclose()
    elif termination == "error":
        with pytest.raises(RuntimeError, match="sensitive stream failure"):
            async for _chunk in response.body_iterator:
                pass
    else:
        async def consume():
            async for _chunk in response.body_iterator:
                pass

        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert metric_probe.route.label_calls == [("harness", "rules", expected)]
    assert metric_probe.delegation.label_calls == [("harness", "inventory", expected)]
    assert metric_probe.route.increments == [1]
    assert metric_probe.delegation.increments == [1]
    assert len(metric_probe.duration.observations) == 1
