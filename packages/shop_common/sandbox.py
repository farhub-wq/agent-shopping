"""沙箱执行结果的跨服务契约：harness 与 sandbox-runner 共用同一字段口径。"""

from dataclasses import dataclass
from typing import Any

# status 取值：ok 成功 / error 执行失败 / timeout 超时 / unavailable 运行时不可用
# / infra_error 容器已启动但 runner 侧基础设施故障（结果不可信，只用于去重）
STATUSES = {"ok", "error", "timeout", "unavailable", "infra_error"}

# 真正执行过容器的状态：audit 要留痕，结果缓存也要落盘。
# 缓存 infra_error 不是把它当可信结果，而是让同一 execution_id 的重复请求
# 返回同一次执行结论，避免换 runner 重复执行；重试必须使用新的 execution_id。
EXECUTED_STATUSES = {"ok", "error", "timeout", "infra_error"}

# 代码长度上限：harness 与 runner 共用同一数值，避免大请求跨网传到 runner 才被拒。
MAX_CODE_CHARS = 65536

# 执行超时下限：0/负数会退化成「立即超时」，统一按配置默认值处理。
MIN_TIMEOUT_SECONDS = 1


def clamp_timeout(requested: int | None, configured: int) -> int:
    """把调用方请求的超时收敛到 [1, 配置值]。

    配置值是上限而不是建议值：否则调用方传超大 timeout 就能长期占住并发槽，
    甚至在被调用方断开后仍让容器继续运行；非正数按配置值处理，避免进入
    Docker wait 的异常路径。
    """
    maximum = max(int(configured), MIN_TIMEOUT_SECONDS)
    if requested is None:
        return maximum
    try:
        value = int(requested)
    except (TypeError, ValueError):
        return maximum
    if value <= 0:
        return maximum
    return min(value, maximum)


@dataclass
class SandboxResult:
    status: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    duration_ms: int = 0
    backend: str = "none"
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "backend": self.backend,
            "truncated": self.truncated,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SandboxResult":
        # 跨进程响应不信任其形状：只取已知字段，未知 status 一律按 error 处理，
        # 避免 runner 的异常响应被当成成功结果喂回模型。
        status = str(data.get("status", "error"))
        if status not in STATUSES:
            status = "error"
        # exit_code 必须显式判空：0 是合法退出码，用 `or -1` 会把它误写成 -1，
        # 导致「执行成功」在跨服务往返后变成失败。
        raw_exit = data.get("exit_code")
        raw_duration = data.get("duration_ms")
        return cls(
            status=status,
            stdout=str(data.get("stdout") or ""),
            stderr=str(data.get("stderr") or ""),
            exit_code=int(raw_exit) if raw_exit is not None else -1,
            duration_ms=int(raw_duration) if raw_duration is not None else 0,
            backend=str(data.get("backend") or "remote"),
            truncated=bool(data.get("truncated")),
        )
