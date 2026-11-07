import json

import pytest
from harness.core.circuit import CircuitBreaker
from harness.core.llm import (
    FALLBACK_MESSAGE,
    Decision,
    DeterministicPlanner,
    FailoverProvider,
    LLMProvider,
    OpenAICompatibleProvider,
)

SPECS = [
    {"name": "catalog_search"},
    {"name": "inventory_check"},
    {"name": "order_lookup"},
    {"name": "after_sales_draft"},
]


@pytest.mark.asyncio
async def test_planner_routes_stock_question_to_inventory_tool():
    planner = DeterministicPlanner()
    decision = await planner.decide("system", [{"role": "user", "content": "SKU-1001 还有货吗？"}], SPECS)
    assert decision.kind == "tool"
    assert decision.tool == "inventory_check"
    assert decision.args["sku"] == "SKU-1001"


@pytest.mark.asyncio
async def test_planner_routes_refund_intent():
    planner = DeterministicPlanner()
    decision = await planner.decide(
        "system", [{"role": "user", "content": "订单 SO20260101123456 想申请退款，质量有问题"}], SPECS
    )
    assert decision.tool == "after_sales_draft"
    assert decision.args["reason"] == "quality_issue"
    assert decision.args["order_id"] == "SO20260101123456"


@pytest.mark.asyncio
async def test_planner_finishes_after_tool_observation():
    planner = DeterministicPlanner()
    observation = json.dumps({"status": "ok", "data": {"sku": "SKU-1001", "available": 12}})
    decision = await planner.decide(
        "system",
        [
            {"role": "user", "content": "SKU-1001 还有货吗？"},
            {"role": "assistant", "content": None},
            {"role": "tool", "tool_call_id": "call_1", "content": observation},
        ],
        SPECS,
    )
    assert decision.kind == "final"
    assert "SKU-1001" in decision.content


@pytest.mark.asyncio
async def test_planner_does_not_invent_when_no_tool_matches():
    planner = DeterministicPlanner()
    decision = await planner.decide("system", [{"role": "user", "content": "今天天气怎么样"}], [])
    assert decision.kind == "final"
    assert "库存" in decision.content


def test_decision_model_defaults_to_empty():
    assert Decision(kind="final").model == ""


@pytest.mark.asyncio
async def test_planner_decide_stream_chunks_final_answer():
    planner = DeterministicPlanner()
    frames = [
        item
        async for item in planner.decide_stream(
            "system", [{"role": "user", "content": "今天天气怎么样"}], []
        )
    ]
    assert len(frames) >= 2  # 至少一帧 delta + 一帧 terminal
    assert all(item.kind == "delta" for item in frames[:-1])
    assert frames[-1].kind == "final"
    assert "".join(item.content for item in frames[:-1]) == frames[-1].content


@pytest.mark.asyncio
async def test_planner_decide_stream_tool_decision_is_single_frame():
    planner = DeterministicPlanner()
    frames = [
        item
        async for item in planner.decide_stream(
            "system",
            [{"role": "user", "content": "SKU-1001 还有货吗？"}],
            [{"name": "inventory_check"}],
        )
    ]
    assert len(frames) == 1
    assert frames[0].kind == "tool"


def test_openai_provider_stores_timeout():
    provider = OpenAICompatibleProvider("http://x", "k", "m", timeout=3.0)
    assert provider.timeout == 3.0


def test_openai_stream_delta_extracts_content():
    from harness.core.llm import _openai_stream_delta

    chunk = {"choices": [{"delta": {"content": "你好"}, "finish_reason": None}]}
    assert _openai_stream_delta(chunk) == ("你好", "", "")


def test_openai_stream_delta_extracts_tool_call():
    from harness.core.llm import _openai_stream_delta

    chunk = {"choices": [{"delta": {"tool_calls": [{"function": {"name": "echo", "arguments": "{\"x\":"}}]}}]}
    assert _openai_stream_delta(chunk) == ("", "echo", "{\"x\":")


def test_openai_stream_delta_tolerates_missing_fields():
    from harness.core.llm import _openai_stream_delta

    assert _openai_stream_delta({}) == ("", "", "")
    assert _openai_stream_delta({"choices": [{"delta": {}}]}) == ("", "", "")
    assert _openai_stream_delta({"choices": []}) == ("", "", "")


def test_settings_have_failover_defaults():
    from shop_common.config import ServiceSettings

    s = ServiceSettings()
    assert s.llm_timeout_seconds == 15.0
    assert s.llm_failure_threshold == 3
    assert s.llm_cooldown_seconds == 300
    assert s.llm_fallback_base_url == ""


def test_build_provider_returns_failover_when_configured(monkeypatch):
    from harness.core.llm import build_provider
    from shop_common.config import get_settings

    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:1")
    monkeypatch.setenv("LLM_API_KEY", "k")
    get_settings.cache_clear()
    try:
        assert isinstance(build_provider(), FailoverProvider)
    finally:
        get_settings.cache_clear()


def test_build_provider_returns_planner_without_config(monkeypatch):
    from harness.core.llm import build_provider
    from shop_common.config import get_settings

    # 置空而非 delenv：环境变量优先级高于 .env 文件，置空可覆盖本机可能存在的 .env 配置
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    get_settings.cache_clear()
    try:
        assert isinstance(build_provider(), DeterministicPlanner)
    finally:
        get_settings.cache_clear()


class _EchoProvider(LLMProvider):
    name = "echo"

    def __init__(self, text: str):
        self.text = text
        self.called = False

    async def decide(self, system, messages, tools):
        self.called = True
        return Decision(kind="final", content=self.text)


class _RaisingProvider(LLMProvider):
    name = "raising"

    def __init__(self):
        self.called = False

    async def decide(self, system, messages, tools):
        self.called = True
        raise RuntimeError("boom")


@pytest.mark.asyncio
async def test_failover_uses_primary_when_healthy():
    primary, fallback = _EchoProvider("A"), _EchoProvider("B")
    provider = FailoverProvider(primary, fallback, CircuitBreaker())
    decision = await provider.decide("s", [], [])
    assert decision.content == "A"
    assert decision.model == "echo"
    assert fallback.called is False


@pytest.mark.asyncio
async def test_failover_switches_on_primary_exception():
    fallback = _EchoProvider("B")
    provider = FailoverProvider(_RaisingProvider(), fallback, CircuitBreaker())
    decision = await provider.decide("s", [], [])
    assert decision.content == "B"
    assert decision.model == "echo"


@pytest.mark.asyncio
async def test_failover_skips_primary_when_circuit_open():
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
    breaker.record_failure()  # 预置为熔断态
    primary, fallback = _RaisingProvider(), _EchoProvider("B")
    provider = FailoverProvider(primary, fallback, breaker)
    decision = await provider.decide("s", [], [])
    assert decision.content == "B"
    assert primary.called is False  # 熔断期间不碰主模型


@pytest.mark.asyncio
async def test_failover_canned_when_both_fail():
    provider = FailoverProvider(_RaisingProvider(), _RaisingProvider(), CircuitBreaker())
    decision = await provider.decide("s", [], [])
    assert decision.kind == "final"
    assert decision.model == "unavailable"
    assert FALLBACK_MESSAGE in decision.content
