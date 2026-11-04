"""工具注册表与执行护栏：所有模型发起的动作都必须经过这里。

顺序固定：权限上限 → 审批门槛 → 执行 → 输出脱敏 → trace 记录。
"""

import re
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from shop_common.errors import ApiError
from shop_common.logging import get_logger

from .policy import ToolPolicy, enforce

logger = get_logger(__name__)

PHONE_PATTERN = re.compile(r"1[3-9]\d{9}")
ID_CARD_PATTERN = re.compile(r"\b\d{17}[\dXx]\b")
EMAIL_PATTERN = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.\w+")


def redact(text: str) -> str:
    text = PHONE_PATTERN.sub("1**********", text)
    text = ID_CARD_PATTERN.sub("******************", text)
    return EMAIL_PATTERN.sub("***@***", text)


@dataclass
class ToolContext:
    user_id: str
    role: str
    tenant: str
    session_id: str
    run_id: str
    approved: set[str] = field(default_factory=set)

    def is_staff(self) -> bool:
        return self.role in {"operator", "admin"}


@dataclass
class ToolResult:
    status: str  # ok | error | denied | needs_approval
    data: Any = None
    message: str = ""
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "data": self.data,
            "message": self.message,
            "duration_ms": self.duration_ms,
        }


Handler = Callable[[dict[str, Any], ToolContext], Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    policy: ToolPolicy

    def spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "permission": self.policy.permission,
            "risk": self.policy.risk,
        }


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"工具重复注册: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self, allowed: list[str] | None = None) -> list[dict[str, Any]]:
        names = allowed if allowed is not None else self.names()
        return [self._tools[name].spec() for name in names if name in self._tools]

    async def execute(
        self, name: str, args: dict[str, Any], ctx: ToolContext, *, trust: str
    ) -> ToolResult:
        started = time.perf_counter()
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult("error", message=f"未知工具 {name}")
        try:
            enforce(trust, tool.policy)
        except ApiError as exc:
            return ToolResult("denied", message=exc.message)

        if tool.policy.requires_approval and name not in ctx.approved:
            return ToolResult(
                "needs_approval",
                message=f"{name} 属于高风险动作，需要人工审批后重放",
                data={"tool": name, "args": args, "risk": tool.policy.risk},
            )

        try:
            data = await tool.handler(args, ctx)
        except ApiError as exc:
            return ToolResult(
                "error",
                message=exc.message,
                data={"code": exc.code, "details": exc.details},
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:
            logger.exception("工具执行异常 tool=%s", name)
            return ToolResult(
                "error",
                message=f"工具 {name} 执行失败：{type(exc).__name__}",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        if isinstance(data, str):
            data = redact(data)
        return ToolResult("ok", data=data, duration_ms=int((time.perf_counter() - started) * 1000))


registry = ToolRegistry()
