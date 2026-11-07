# 模型熔断降级 Implementation Plan

**Goal:** 让 harness 的主模型具备「15s 超时、3 次失败熔断 5 分钟、熔断/失败后降级到备用模型、双模型都失败时返回固定话术并转人工」，模型故障不再阻塞客服。

**Architecture:** 在 `llm.py` 之上加一层 `FailoverProvider`（主模型 → 降级模型 → 固定话术），用一个独立的 `CircuitBreaker` 状态机跟踪主模型健康度。熔断器只做主模型降级判断，不感知业务；`FailoverProvider` 负责编排与打点。运行循环 `runtime.py` 无需改动循环逻辑，只在证据卡里记录「本次答案由哪个模型产出」。

**Tech Stack:** Python 3.11 · asyncio（单事件循环，无需锁）· `time.monotonic()` · `httpx` · `prometheus_client`（现有 `shop_common.metrics`）。

**Spec:** README.md「生产边界」：*主模型 + 降级模型（15s 超时、3 次失败熔断 5 分钟、熔断后固定话术 + 转人工）*；需求文档 FR-14（模型不可用时客服仍可用）、NFR-10（成本与安全）。

## Global Constraints

- 新增/修改代码必须写**简明中文注释**（说明为什么），不写废话注释；沿用文件现有注释语言。
- 不吞异常：降级是显式的业务边界（记录日志 + 打点 + 降级），不是静默 `except: pass`。
- 响应/证据只挑字段显式返回，不整对象透传。
- 单文件不超过 300 行；不引入新依赖（`httpx`、`prometheus_client` 均已在用）。
- 提交：Conventional Commits 前缀 + 简体中文；**提交前向用户展示变更摘要，待确认后再提交**（AGENTS.md §9）。

## Review Focus

1. **降级模型也失败**：双模型都不可用时，必须返回固定话术且 `kind="final"`、不抛 5xx —— Task 3 的 `test_failover_canned_when_both_fail` 锁定。
2. **熔断期间不碰主模型**：电路打开时不允许再对主模型发起请求（否则 15s 超时 ×3 白等）—— Task 3 的 `test_failover_skips_primary_when_circuit_open` 用「主模型未被调用」断言锁定。
3. **单次成功不能抹掉历史失败**：熔断要求**连续**失败，一次成功要重置计数 —— Task 1 的 `test_breaker_reset_on_success` 锁定。
4. **冷却后能自愈**：冷却期满后要放行请求，否则永远熔断 —— Task 1 的 `test_breaker_recovers_after_cooldown` 锁定。
5. **超时值要真的生效**：超时必须落在 `httpx` 客户端上（不是外层空转），否则一个挂死的连接会拖满 60s —— Task 2 的 `test_openai_provider_stores_timeout` 锁定。

---

### Task 1: CircuitBreaker 状态机

**Files:**
- Create: `services/harness/core/circuit.py`
- Test: `tests/test_circuit.py`

**Interfaces:**
- Produces:
  ```python
  class CircuitBreaker:
      def __init__(self, failure_threshold: int = 3, cooldown_seconds: float = 300.0) -> None
      def allow_request(self) -> bool   # 熔断中且未到冷却期 → False，否则 True
      def record_success(self) -> None  # 重置连续失败计数、关闭电路
      def record_failure(self) -> None  # 累计失败；达到阈值 → 打开电路（记录 opened_at）
  ```

- [ ] **Step 1: 写失败测试** `tests/test_circuit.py`

```python
import pytest
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_circuit.py -v`
Expected: FAIL（`ModuleNotFoundError: harness.core.circuit` 或 import 错误）

- [ ] **Step 3: 实现 `circuit.py`**

用 `time.monotonic()` 计时；两个内部字段 `_failures: int` 与 `_opened_at: float | None`（None 表示闭合）。`allow_request()`：闭合返回 True；打开时若 `monotonic() - _opened_at >= cooldown_seconds` 返回 True（放行试探），否则 False。`record_failure()` 达到阈值时写 `_opened_at`。单事件循环不加锁（注释说明）。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_circuit.py -v`
Expected: PASS（4 passed）

- [ ] **Step 5: 提交（先向用户展示变更摘要）**

`git add services/harness/core/circuit.py tests/test_circuit.py` → `feat(harness): 新增模型熔断器状态机`

---

### Task 2: Decision 增加 model 字段 + 主模型超时可配

**Files:**
- Modify: `services/harness/core/llm.py`（`Decision` 数据类、`OpenAICompatibleProvider.__init__`）
- Test: `tests/test_llm.py`

**Interfaces:**
- Produces:
  ```python
  # Decision 新增字段（默认空串，不破坏现有调用方）
  @dataclass
  class Decision:
      kind: str
      content: str = ""
      tool: str = ""
      args: dict[str, Any] = field(default_factory=dict)
      usage: dict[str, Any] = field(default_factory=dict)
      model: str = ""          # 产出该决策的模型名，供证据卡审计

  # OpenAICompatibleProvider 新增超时参数（存为实例属性 self.timeout）
  def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 15.0) -> None
  ```

- [ ] **Step 1: 写失败测试** `tests/test_llm.py`（追加）

```python
from harness.core.llm import Decision, OpenAICompatibleProvider


def test_decision_model_defaults_to_empty():
    assert Decision(kind="final").model == ""


def test_openai_provider_stores_timeout():
    provider = OpenAICompatibleProvider("http://x", "k", "m", timeout=3.0)
    assert provider.timeout == 3.0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_llm.py::test_decision_model_defaults_to_empty tests/test_llm.py::test_openai_provider_stores_timeout -v`
Expected: FAIL

- [ ] **Step 3: 实现**

给 `Decision` 加 `model: str = ""`（放最后，默认值不破坏既有位置/关键字构造）。给 `OpenAICompatibleProvider.__init__` 加 `timeout: float = 15.0` 并 `self.timeout = timeout`；把 `httpx.AsyncClient(timeout=60)` 改为 `httpx.AsyncClient(timeout=self.timeout)`。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_llm.py -v`
Expected: PASS（原 4 个 + 新 2 个）

- [ ] **Step 5: 提交（先向用户展示变更摘要）**

`git add services/harness/core/llm.py tests/test_llm.py` → `feat(harness): 主模型超时可配并在决策中标记来源模型`

---

### Task 3: FailoverProvider 编排 + 固定话术 + 指标

**Files:**
- Modify: `services/harness/core/llm.py`（新增 `FALLBACK_MESSAGE` 常量与 `FailoverProvider`）
- Modify: `packages/shop_common/metrics.py`（新增两个 Counter）
- Test: `tests/test_llm.py`（追加）

**Interfaces:**
- Consumes: `CircuitBreaker`（Task 1）、`Decision.model`（Task 2）
- Produces:
  ```python
  FALLBACK_MESSAGE = "抱歉，智能客服暂时不可用，请稍后重试或联系人工客服。"

  class FailoverProvider(LLMProvider):
      name = "failover"
      def __init__(self, primary: LLMProvider, fallback: LLMProvider,
                   breaker: CircuitBreaker, canned_message: str = FALLBACK_MESSAGE) -> None
      async def decide(self, system, messages, tools) -> Decision
  ```
  decide 逻辑：`breaker.allow_request()` 为 False → 记 `LLM_CIRCUIT_OPEN`，跳过主模型；否则尝试主模型，成功 `record_success()` 并 `decision.model = primary.name` 返回，异常 `record_failure()` 并记日志。落到降级路径时记 `LLM_FALLBACK`，试降级模型；降级也失败 → 返回 `Decision(kind="final", content=canned_message, model="unavailable")`。
- 指标（`packages/shop_common/metrics.py`，沿用 `AGENT_TOOL_DENIED.labels("harness")` 的硬编码 service 标签风格）：
  ```python
  LLM_FALLBACK = Counter("llm_fallback_total", "模型降级次数", ["service"])
  LLM_CIRCUIT_OPEN = Counter("llm_circuit_open_total", "模型熔断拦截次数", ["service"])
  ```

- [ ] **Step 1: 写失败测试** `tests/test_llm.py`（追加，文件内定义两个 stub）

```python
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
    from harness.core.circuit import CircuitBreaker
    from harness.core.llm import FailoverProvider
    primary, fallback = _EchoProvider("A"), _EchoProvider("B")
    provider = FailoverProvider(primary, fallback, CircuitBreaker())
    decision = await provider.decide("s", [], [])
    assert decision.content == "A"
    assert decision.model == "echo"
    assert fallback.called is False


@pytest.mark.asyncio
async def test_failover_switches_on_primary_exception():
    from harness.core.circuit import CircuitBreaker
    from harness.core.llm import FailoverProvider
    fallback = _EchoProvider("B")
    provider = FailoverProvider(_RaisingProvider(), fallback, CircuitBreaker())
    decision = await provider.decide("s", [], [])
    assert decision.content == "B"
    assert decision.model == "echo"


@pytest.mark.asyncio
async def test_failover_skips_primary_when_circuit_open():
    from harness.core.circuit import CircuitBreaker
    from harness.core.llm import FailoverProvider
    breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
    breaker.record_failure()  # 预置为熔断态
    primary, fallback = _RaisingProvider(), _EchoProvider("B")
    provider = FailoverProvider(primary, fallback, breaker)
    decision = await provider.decide("s", [], [])
    assert decision.content == "B"
    assert primary.called is False  # 熔断期间不碰主模型


@pytest.mark.asyncio
async def test_failover_canned_when_both_fail():
    from harness.core.circuit import CircuitBreaker
    from harness.core.llm import FailoverProvider, FALLBACK_MESSAGE
    provider = FailoverProvider(_RaisingProvider(), _RaisingProvider(), CircuitBreaker())
    decision = await provider.decide("s", [], [])
    assert decision.kind == "final"
    assert decision.model == "unavailable"
    assert FALLBACK_MESSAGE in decision.content
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_llm.py -k failover -v`
Expected: FAIL（`ImportError: FailoverProvider`）

- [ ] **Step 3: 实现**

`metrics.py` 加两个 Counter。`llm.py` 顶部 import `CircuitBreaker` 与两个指标；新增 `FALLBACK_MESSAGE` 与 `FailoverProvider`。降级/熔断分支用 `logger.warning/error` 记录原因，不静默。`except Exception` 只包住网络边界调用（主/降级模型的 `decide`），这是显式的降级边界，注释说明。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_llm.py -v`
Expected: PASS

- [ ] **Step 5: 提交（先向用户展示变更摘要）**

`git add services/harness/core/llm.py packages/shop_common/metrics.py tests/test_llm.py` → `feat(harness): 主模型失败熔断后降级，双模型失败返回固定话术`

---

### Task 4: 配置字段与 .env.example

**Files:**
- Modify: `packages/shop_common/config.py`（「模型与嵌入」段）
- Modify: `.env.example`（「模型接入」段）
- Test: `tests/test_llm.py`（追加，或 `tests/test_circuit.py` 同文件均可）

**Interfaces:**
- Produces（`ServiceSettings` 新增字段，均带默认值）：
  ```python
  llm_fallback_base_url: str = ""
  llm_fallback_api_key: str = ""
  llm_fallback_model: str = ""
  llm_timeout_seconds: float = 15.0
  llm_failure_threshold: int = 3
  llm_cooldown_seconds: int = 300
  ```

- [ ] **Step 1: 写失败测试**

```python
def test_settings_have_failover_defaults():
    from shop_common.config import ServiceSettings
    s = ServiceSettings()
    assert s.llm_timeout_seconds == 15.0
    assert s.llm_failure_threshold == 3
    assert s.llm_cooldown_seconds == 300
    assert s.llm_fallback_base_url == ""
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_llm.py::test_settings_have_failover_defaults -v`
Expected: FAIL（`ValidationError` 或属性缺失）

- [ ] **Step 3: 实现**

在 `config.py` 的 `llm_model` 之后追加六个字段；在 `.env.example` 的 `LLM_MODEL`/`EMBEDDING_DIM` 附近追加对应注释与变量（降级模型留空说明「不配则降级到离线确定性规划器」）。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_llm.py::test_settings_have_failover_defaults -v`
Expected: PASS

- [ ] **Step 5: 提交（先向用户展示变更摘要）**

`git add packages/shop_common/config.py .env.example tests/test_llm.py` → `feat(harness): 新增模型熔断与降级配置项`

---

### Task 5: build_provider 接线 + 证据卡记录来源模型

**Files:**
- Modify: `services/harness/core/llm.py`（`build_provider`）
- Modify: `services/harness/core/runtime.py`（`run()` 内证据卡的 `model=` 取值）
- Test: `tests/test_llm.py`（追加 `build_provider` 两分支）

**Interfaces:**
- Consumes: `FailoverProvider`（Task 3）、`CircuitBreaker`（Task 1）、`ServiceSettings` 新字段（Task 4）
- Produces:
  ```python
  def build_provider() -> LLMProvider:
      # 主模型未配置 → 返回 DeterministicPlanner()（现状不变）
      # 主模型已配置 → OpenAICompatibleProvider(主, timeout=settings.llm_timeout_seconds)
      #   + fallback = OpenAICompatibleProvider(降级配置) 或 DeterministicPlanner()
      #   + CircuitBreaker(settings.llm_failure_threshold, settings.llm_cooldown_seconds)
      #   → 返回 FailoverProvider(primary, fallback, breaker)
  ```

- [ ] **Step 1: 写失败测试** `tests/test_llm.py`（追加）

```python
def test_build_provider_returns_failover_when_configured(monkeypatch):
    from shop_common.config import get_settings
    from harness.core.llm import FailoverProvider, build_provider
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:1")
    monkeypatch.setenv("LLM_API_KEY", "k")
    get_settings.cache_clear()
    try:
        assert isinstance(build_provider(), FailoverProvider)
    finally:
        get_settings.cache_clear()


def test_build_provider_returns_planner_without_config(monkeypatch):
    from shop_common.config import get_settings
    from harness.core.llm import DeterministicPlanner, build_provider
    # 置空而非 delenv：环境变量优先级高于 .env 文件，置空可确保覆盖本机可能存在的 .env 配置
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_API_KEY", "")
    get_settings.cache_clear()
    try:
        assert isinstance(build_provider(), DeterministicPlanner)
    finally:
        get_settings.cache_clear()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_llm.py -k build_provider -v`
Expected: FAIL（主模型配置分支返回的不是 `FailoverProvider`）

- [ ] **Step 3: 实现**

按 Interfaces 重写 `build_provider`。`runtime.py` 的 `run()`：在循环前加 `model_used = self.provider.name`，在每次 `decision = await self.provider.decide(...)` 后加 `if decision.model: model_used = decision.model`，并把证据卡 `model=self.provider.name` 改为 `model=model_used`（`approve_and_replay` 里的证据卡保持不变，审批重放不经过模型）。

- [ ] **Step 4: 运行测试确认通过 + 全量回归**

Run: `.venv/Scripts/python -m pytest tests/test_llm.py -v` 然后 `.venv/Scripts/python -m pytest -q`
Expected: 全绿（runtime 改动无回归）

- [ ] **Step 5: 提交（先向用户展示变更摘要）**

`git add services/harness/core/llm.py services/harness/core/runtime.py tests/test_llm.py` → `feat(harness): build_provider 接入熔断降级并在证据卡记录来源模型`

---

## 验证命令（全部任务完成后）

```bash
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff check packages services tests
cd web && npm run build   # 无前端改动，仅确认不被破坏
```

## 风险点与取舍

1. **无半开状态的简化**：熔断冷却期一过即放行下一请求；若主模型仍故障，该请求最多等 15s 才降级（3 次连续失败后再次熔断）。对当前单事件循环、低频客服流量可接受，不做显式 half-open 状态（YAGNI）。
2. **进程级熔断计数**：多 worker/多副本各自独立计数，不共享；熔断降级目标是「单实例不雪崩」，跨实例共享状态属过度设计，暂不做（如需可后续用 Redis 记 opened_at）。
3. **`except Exception` 边界**：只包主/降级模型调用这个网络边界，且每次都有日志 + 指标，非静默吞异常；若未来 `decide` 内部抛出的是编程错误而非网络错误，会被误判为「模型失败」而快速降级——日志保留原始异常栈，便于区分。
4. **「转人工」是话术不是工单**：固定话术引导用户联系人工，但当前 `notify` 服务仍是规划项（README M3），本计划不落地自动建工单；等 notify 就绪后再把 canned 分支接入通知。
5. **证据卡 model 字段的旧数据**：存量 `agent_traces` 无 `model` 字段差异，不影响新写入；不回填历史。
