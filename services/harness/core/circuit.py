"""模型熔断器：跟踪主模型的连续失败次数，达到阈值后打开电路、冷却期内拒绝请求。

只负责「该不该放行」这一件事，不感知模型与业务；降级编排在 llm.FailoverProvider。
单事件循环下无需加锁；多 worker 各自独立计数（见 implementation_plan_llm_failover.md 风险点）。
"""

import time


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, cooldown_seconds: float = 300.0) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._failures = 0
        self._opened_at: float | None = None  # None 表示电路闭合

    def allow_request(self) -> bool:
        # 闭合直接放行；打开时只有过了冷却期才放行一次试探请求。
        if self._opened_at is None:
            return True
        if time.monotonic() - self._opened_at >= self.cooldown_seconds:
            return True
        return False

    def record_success(self) -> None:
        # 一次成功清零连续失败计数并闭合电路。
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        # 只对「连续」失败计数：达到阈值即打开电路，记录打开时刻。
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = time.monotonic()
