from harness.core.circuit import CircuitBreaker


def test_breaker_opens_after_threshold_failures():
    b = CircuitBreaker(failure_threshold=2, cooldown_seconds=60)
    assert b.allow_request() is True
    b.record_failure()
    assert b.allow_request() is True
    b.record_failure()
    assert b.allow_request() is False  # 连续 2 次失败后熔断


def test_breaker_denies_while_open():
    b = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
    b.record_failure()
    assert b.allow_request() is False
    assert b.allow_request() is False  # 冷却期内持续拒绝


def test_breaker_recovers_after_cooldown():
    b = CircuitBreaker(failure_threshold=1, cooldown_seconds=0.0)
    b.record_failure()
    assert b.allow_request() is True  # 冷却期已过，放行
    b.record_success()
    assert b.allow_request() is True


def test_breaker_reset_on_success():
    b = CircuitBreaker(failure_threshold=2, cooldown_seconds=60)
    b.record_failure()
    b.record_success()  # 一次成功清零
    b.record_failure()
    assert b.allow_request() is True  # 只有 1 次连续失败，不应熔断
