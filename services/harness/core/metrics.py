"""自动编排指标：标签固定有界，单次运行只结算一次。"""

import math
import time
from collections.abc import Mapping
from typing import Any

from prometheus_client import Counter, Histogram

SERVICE_NAMES = frozenset({"harness"})
ROUTE_SOURCES = frozenset({"llm", "rules", "fallback", "unknown"})
ROUTE_RESULTS = frozenset(
    {
        "completed",
        "degraded",
        "fallback",
        "blocked",
        "quota_exceeded",
        "budget_exceeded",
        "pending_approval",
        "error",
        "cancelled",
        "unknown",
    }
)
DELEGATION_AGENTS = frozenset(
    {"chitchat", "presales", "inventory", "aftersales", "customer-service", "other"}
)
DELEGATION_STATUSES = frozenset(
    {
        "completed",
        "degraded",
        "blocked",
        "quota_exceeded",
        "budget_exceeded",
        "pending_approval",
        "error",
        "cancelled",
        "unknown",
    }
)

AGENT_ROUTE_TOTAL = Counter(
    "agent_route_total", "自动编排路由结果", ["service", "source", "result"]
)
AGENT_DELEGATION_TOTAL = Counter(
    "agent_delegation_total", "专家委派结果", ["service", "agent", "status"]
)
AGENT_ORCHESTRATION_DURATION_SECONDS = Histogram(
    "agent_orchestration_duration_seconds", "自动编排耗时（秒）", ["service"]
)


def _bounded(value: Any, allowed: frozenset[str], fallback: str) -> str:
    return value if isinstance(value, str) and value in allowed else fallback


def _field(payload: Any, name: str, default: Any = None) -> Any:
    if isinstance(payload, Mapping):
        return payload.get(name, default)
    return getattr(payload, name, default)


def record_route(service: str, source: str, result: str) -> None:
    """记录一次根路由；调用方负责保证单次运行只调用一次。"""
    AGENT_ROUTE_TOTAL.labels(
        _bounded(service, SERVICE_NAMES, "other"),
        _bounded(source, ROUTE_SOURCES, "unknown"),
        _bounded(result, ROUTE_RESULTS, "unknown"),
    ).inc()


def record_delegation(service: str, agent: str, status: str) -> None:
    """记录一次专家委派；run_id 只用于内部去重，绝不进入标签。"""
    AGENT_DELEGATION_TOTAL.labels(
        _bounded(service, SERVICE_NAMES, "other"),
        _bounded(agent, DELEGATION_AGENTS, "other"),
        _bounded(status, DELEGATION_STATUSES, "unknown"),
    ).inc()


def record_duration(service: str, seconds: float) -> None:
    """记录根编排耗时；负值或非有限值统一收敛为 0。"""
    value = (
        float(seconds)
        if isinstance(seconds, (int, float)) and math.isfinite(seconds) and seconds >= 0
        else 0.0
    )
    AGENT_ORCHESTRATION_DURATION_SECONDS.labels(
        _bounded(service, SERVICE_NAMES, "other")
    ).observe(value)


class OrchestrationMetrics:
    """消费 orchestrator 事件/最终投影，并在正常、异常、取消路径幂等收尾。"""

    def __init__(self, service: str = "harness") -> None:
        self.service = _bounded(service, SERVICE_NAMES, "other")
        self._started = time.perf_counter()
        self._finished = False
        self._route_source = "unknown"
        self._route_result: str | None = None
        self._active: dict[str, str] = {}
        self._recorded: set[str] = set()

    @staticmethod
    def _child_key(agent: str, run_id: Any) -> str:
        return f"run:{run_id}" if isinstance(run_id, str) and run_id else f"agent:{agent}"

    def _record_child(self, agent: Any, run_id: Any, status: Any) -> None:
        normalized_agent = _bounded(agent, DELEGATION_AGENTS, "other")
        key = self._child_key(normalized_agent, run_id)
        self._active.pop(key, None)
        if key in self._recorded:
            return
        self._recorded.add(key)
        record_delegation(
            self.service,
            normalized_agent,
            _bounded(status, DELEGATION_STATUSES, "unknown"),
        )

    def observe_event(self, event: Mapping[str, Any]) -> None:
        """消费流式 agent 事件；route 在最终状态明确后才计数。"""
        if self._finished or event.get("type") != "agent":
            return
        payload = event.get("event")
        if not isinstance(payload, Mapping):
            return
        event_type = payload.get("type")
        if event_type == "route":
            self._route_source = _bounded(payload.get("source"), ROUTE_SOURCES, "unknown")
            if self._route_source == "fallback":
                self._route_result = "fallback"
        elif event_type == "fallback":
            self._route_source = "fallback"
            self._route_result = "fallback"
        elif event_type == "agent.start":
            agent = _bounded(payload.get("agent"), DELEGATION_AGENTS, "other")
            self._active[self._child_key(agent, payload.get("run_id"))] = agent
        elif event_type == "agent.end":
            self._record_child(payload.get("agent"), payload.get("run_id"), payload.get("status"))

    def observe_result(self, result: Any) -> None:
        """用非流式结果或流式 final 补记路由与子运行，并与事件路径去重。"""
        if self._finished:
            return
        route = _field(result, "route", {})
        if isinstance(route, Mapping):
            self._route_source = _bounded(route.get("source"), ROUTE_SOURCES, "unknown")
            if self._route_source == "fallback":
                self._route_result = "fallback"
        for child in _field(result, "agent_runs", []) or []:
            self._record_child(_field(child, "agent"), _field(child, "run_id"), _field(child, "status"))

    def finish(self, status: str) -> None:
        """结算根路由、未收到结束事件的专家以及总耗时；重复调用无副作用。"""
        if self._finished:
            return
        self._finished = True
        result = _bounded(status, ROUTE_RESULTS, "unknown")
        record_route(self.service, self._route_source, self._route_result or result)
        for key, agent in self._active.items():
            if key not in self._recorded:
                self._recorded.add(key)
                record_delegation(self.service, agent, result)
        record_duration(self.service, time.perf_counter() - self._started)
