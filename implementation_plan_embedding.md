# 真实 Embedding Implementation Plan

**Goal:** 配置 OpenAI 兼容 embedding 端点后，记忆向量改用真实语义向量；未配置时保持哈希 n-gram 离线向量，嵌入调用失败时自动回退哈希保证客服不阻塞。

**Architecture:** 在 `memory.py` 里把嵌入器抽成 `Embedder` 基类（`name` + `async embed`），`HashingEmbedder` 改为异步接口保留离线默认；新增 `OpenAIEmbedder`（POST `/embeddings`）与 `build_embedder()` 工厂（配置了 `embedding_base_url`+`embedding_api_key` 才启用）。`MemoryManager` 注入 embedder，`remember`/`retrieve` 改走 `await _embed`，每条记忆落盘带 `embedder` 标签，召回时用 `_compatible` 按向量维度跳过换模型前的旧向量；`_embed` 在 embedder 失败时回退哈希并打 `EMBEDDING_FALLBACK` 指标。

**Tech Stack:** Python 3.11 · asyncio · httpx · 现有 Redis/Mongo 记忆框架（不引入新依赖）。

**Spec:** `docs/requirements/requirements-shop-agent.md` FR-14（模型故障不阻塞客服）、FR-12/13（越权拒绝 + 证据可回放）；`docs/ARCHITECTURE.md:139` 已预留「接真实 embedding 时只替换 HashingEmbedder」。

## 决策契约（本计划落定）

```python
# 嵌入器统一接口：name 标识向量空间，embed 为异步（真实模型走网络）。
class Embedder:
    name: str = "base"
    async def embed(self, text: str) -> list[float]:
        raise NotImplementedError

# 哈希向量：dim 可配，name 带 dim 便于审计；embed 只包一层 async，核心逻辑不变。
class HashingEmbedder(Embedder):
    def __init__(self, dim: int = 256) -> None:
        self.dim = dim
        self.name = f"hashing-{dim}"

# 真实嵌入：POST {base_url}/embeddings，返回 data[0].embedding；缺向量抛 ApiError。
class OpenAIEmbedder(Embedder):
    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 15.0, transport=None): ...
    async def embed(self, text: str) -> list[float]: ...

def build_embedder() -> Embedder:
    # 配置了 base_url + api_key 才启用真实嵌入，否则回退哈希（与 build_provider 同一策略）。

# 维度守卫：换 embedder 后新旧向量维度不同，cosine 无意义；只与同维度向量比较。
def _compatible(query: list[float], stored: list[float] | None) -> bool:
    return bool(stored) and len(stored) == len(query)
```

## Global Constraints

- 不回归离线默认：未配置 embedding 端点时行为与现在完全一致（哈希 256 维）。
- 嵌入失败**不抛 5xx 阻断客服**：`_embed` 捕获异常回退哈希向量并打 `EMBEDDING_FALLBACK` 指标。
- 换 embedder 后旧向量维度不匹配：召回与去重都跳过（`_compatible`），不产生伪相似度。
- 坏响应（缺 `data[0].embedding`）抛 `ApiError`（不吞异常、不返回 null）。
- 提交：Conventional Commits + 简体中文；每任务提交前展示变更摘要。
- 全量验证：`.venv/Scripts/python -m pytest -q` + `.venv/Scripts/python -m ruff check packages services tests`。

## Review Focus

1. **换 embedder 后新旧向量维度不同**：cosine 用 `zip` 截断，跨维度比较会得到垃圾相似度，去重可能误覆盖、召回可能误召回 —— Task 3 `test_compatible_guards_dim_mismatch`（去重与召回两处都用 `_compatible` 跳过）。
2. **嵌入端点挂掉**：记忆写入/召回都在 chat 路径上，失败必须回退哈希而不是 500 —— Task 3 `test_embed_falls_back_to_hashing_on_failure`。
3. **端点返回缺 embedding 字段**：不能静默存空向量 —— Task 2 `test_openai_embedder_rejects_missing_embedding`。
4. **配置半填（只配 base_url 缺 api_key）**：应退回离线哈希而非启动报错 —— Task 2 `test_build_embedder_defaults_to_hashing`。
5. **`HashingEmbedder.embed` 异步化后调用点漏 await**：拿到协程对象当向量，`remember`/`retrieve` 会崩 —— Task 1 三个既有测试改 async + Task 3 全量回归。

---

### Task 1: Embedder 接口 + HashingEmbedder 异步化

**Files:**
- Modify: `services/harness/core/memory.py`（新增 `Embedder` 基类；`HashingEmbedder` 改 async、加 `name`）
- Test: `tests/test_memory.py`

**Interfaces:**
- Produces:
  ```python
  class Embedder:
      name: str = "base"
      async def embed(self, text: str) -> list[float]:
          raise NotImplementedError

  class HashingEmbedder(Embedder):
      def __init__(self, dim: int = 256) -> None:
          self.dim = dim
          self.name = f"hashing-{dim}"
      async def embed(self, text: str) -> list[float]:
          return self._embed_sync(text)
      def _embed_sync(self, text: str) -> list[float]:
          # 原 embed 的 n-gram 哈希逻辑整体搬进来，一字不改
  ```

- [ ] **Step 1: 写失败测试** `tests/test_memory.py`（三个既有测试改 async，并加 `name` 断言）

```python
import pytest
from harness.core.memory import HashingEmbedder, cosine


@pytest.mark.asyncio
async def test_embedding_is_normalized():
    embedder = HashingEmbedder(128)
    vector = await embedder.embed("SKU-1001 的库存还有多少")
    norm = sum(value * value for value in vector) ** 0.5
    assert 0.99 < norm < 1.01
    assert embedder.name == "hashing-128"


@pytest.mark.asyncio
async def test_similar_sentences_score_higher_than_unrelated():
    embedder = HashingEmbedder(256)
    base = await embedder.embed("我想查询订单的物流进度")
    related = await embedder.embed("帮我看看订单发货到哪里了")
    unrelated = await embedder.embed("这款防晒霜的保质期是多久")
    assert cosine(base, related) > cosine(base, unrelated)


@pytest.mark.asyncio
async def test_deterministic_embedding():
    embedder = HashingEmbedder(64)
    assert await embedder.embed("同样的输入") == await embedder.embed("同样的输入")
    assert embedder.name == "hashing-64"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_memory.py -v`
Expected: FAIL（`embed` 是同步方法，`await` 报 `TypeError: object list can't be used in 'await' expression`；`name` 属性不存在）

- [ ] **Step 3: 实现** 在 `memory.py` 加 `Embedder` 基类；`HashingEmbedder` 继承它，`__init__` 设 `self.dim`/`self.name`，原 `embed` 逻辑改名 `_embed_sync`，新增 `async def embed` 直接返回 `self._embed_sync(text)`。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_memory.py -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交（先展示变更摘要）**

`git add services/harness/core/memory.py tests/test_memory.py` → `feat(harness): 嵌入器抽象为异步接口并加 name 标识`

---

### Task 2: OpenAIEmbedder + build_embedder + 配置字段 + 指标

**Files:**
- Modify: `services/harness/core/memory.py`（新增 `OpenAIEmbedder`、`build_embedder`；`import httpx`、`from shop_common.errors import ApiError`）
- Modify: `packages/shop_common/config.py`（4 个配置字段）
- Modify: `packages/shop_common/metrics.py`（`EMBEDDING_FALLBACK` 计数器）
- Modify: `.env.example`（4 个环境变量）
- Test: `tests/test_memory.py`

**Interfaces:**
- Consumes: `Embedder`（Task 1）
- Produces:
  ```python
  class OpenAIEmbedder(Embedder):
      def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 15.0, transport=None) -> None:
          # transport 是 httpx 的测试注入点；self.name = f"openai:{model}"
      async def embed(self, text: str) -> list[float]:
          # POST {base_url}/embeddings，body {"model": model, "input": text}，Bearer 头；
          # raise_for_status()；data[0]["embedding"] 缺失或非 list 抛 ApiError("EMBEDDING_FAILED", ...)

  def build_embedder() -> Embedder:
      # settings.embedding_base_url 与 embedding_api_key 都非空 → OpenAIEmbedder(...)
      # 否则 → HashingEmbedder(settings.embedding_dim)
  ```

  配置字段（`ServiceSettings`）：
  ```python
  embedding_base_url: str = ""
  embedding_api_key: str = ""
  embedding_model: str = "text-embedding-3-small"
  embedding_timeout_seconds: float = 15.0
  ```
  指标：`EMBEDDING_FALLBACK = Counter("embedding_fallback_total", "嵌入降级次数", ["service"])`

- [ ] **Step 1: 写失败测试** `tests/test_memory.py`（追加）

```python
import httpx
from shop_common.errors import ApiError
from harness.core.memory import HashingEmbedder, OpenAIEmbedder, build_embedder


@pytest.mark.asyncio
async def test_openai_embedder_returns_embedding():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/embeddings"
        body = json.loads(request.content)
        assert body["model"] == "text-embedding-3-small"
        assert body["input"] == "你好"
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    embedder = OpenAIEmbedder("http://embed", "k", "text-embedding-3-small", transport=httpx.MockTransport(handler))
    assert await embedder.embed("你好") == [0.1, 0.2, 0.3]
    assert embedder.name == "openai:text-embedding-3-small"


@pytest.mark.asyncio
async def test_openai_embedder_rejects_missing_embedding():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    embedder = OpenAIEmbedder("http://embed", "k", "m", transport=httpx.MockTransport(handler))
    with pytest.raises(ApiError):
        await embedder.embed("你好")


def test_build_embedder_defaults_to_hashing(monkeypatch):
    import harness.core.memory as mem

    class _S:
        embedding_base_url = ""
        embedding_api_key = ""
        embedding_model = ""
        embedding_dim = 128
        embedding_timeout_seconds = 15.0

    monkeypatch.setattr(mem, "get_settings", lambda: _S)
    embedder = mem.build_embedder()
    assert isinstance(embedder, HashingEmbedder)
    assert embedder.dim == 128


def test_build_embedder_uses_openai_when_configured(monkeypatch):
    import harness.core.memory as mem

    class _S:
        embedding_base_url = "http://embed"
        embedding_api_key = "k"
        embedding_model = "text-embedding-3-small"
        embedding_dim = 128
        embedding_timeout_seconds = 15.0

    monkeypatch.setattr(mem, "get_settings", lambda: _S)
    embedder = mem.build_embedder()
    assert isinstance(embedder, OpenAIEmbedder)
    assert embedder.name == "openai:text-embedding-3-small"
```

（测试文件顶部加 `import json`。）

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_memory.py -k "openai or build_embedder" -v`
Expected: FAIL（`ImportError: cannot import name 'OpenAIEmbedder'`）

- [ ] **Step 3: 实现**

`OpenAIEmbedder`：`httpx.AsyncClient(timeout=self.timeout, transport=self.transport)` POST `/embeddings`；`build_embedder`：`get_settings()` 判 `embedding_base_url and embedding_api_key`。`config.py` 加 4 字段（放 `embedding_dim` 旁）；`metrics.py` 加 `EMBEDDING_FALLBACK`；`.env.example` 在「模型接入」段加 4 个变量。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_memory.py -q`
Expected: PASS（3 既有 + 4 新增）

- [ ] **Step 5: 提交（先展示变更摘要）**

`git add services/harness/core/memory.py packages/shop_common/config.py packages/shop_common/metrics.py tests/test_memory.py .env.example` → `feat(harness): 接入 OpenAI 兼容嵌入端点与离线回退配置`

---

### Task 3: MemoryManager 接线（_embed 回退 + 维度守卫 + embedder 标签）

**Files:**
- Modify: `services/harness/core/memory.py`（`MemoryManager`）
- Test: `tests/test_memory.py`

**Interfaces:**
- Consumes: `Embedder` / `OpenAIEmbedder` / `build_embedder`（Task 1–2）、`EMBEDDING_FALLBACK`（Task 2）
- Produces:
  ```python
  def _compatible(query: list[float], stored: list[float] | None) -> bool:
      return bool(stored) and len(stored) == len(query)

  class MemoryManager:
      def __init__(self, redis: Redis, namespace: str = "shop", embedder: Embedder | None = None) -> None:
          self.embedder = embedder or build_embedder()
          self._fallback = HashingEmbedder(settings.embedding_dim)  # 主嵌入失败时的离线保底

      async def _embed(self, text: str) -> tuple[list[float], str]:
          # 先试 self.embedder；失败记日志 + EMBEDDING_FALLBACK.inc() + 回退 self._fallback；
          # 返回 (向量, 实际产出向量的 embedder.name)，保证落盘标签诚实。
  ```
  `remember`：`embedding, embedder_name = await self._embed(text)`，去重循环先 `_compatible` 再比 cosine，insert 落 `"embedder": embedder_name`。`retrieve`：`query_vector, _ = await self._embed(query)`，评分循环先 `_compatible` 跳过维度不匹配的旧记录。`memory.py` 头部加 `from shop_common.logging import get_logger` + `logger = get_logger(__name__)`。

- [ ] **Step 1: 写失败测试** `tests/test_memory.py`（追加）

```python
from harness.core.memory import MemoryManager, _compatible


class _BoomEmbedder:
    name = "boom"

    async def embed(self, text):
        raise RuntimeError("down")


class _StubEmbedder:
    name = "stub"

    async def embed(self, text):
        return [1.0, 0.0]


@pytest.mark.asyncio
async def test_embed_falls_back_to_hashing_on_failure():
    manager = MemoryManager(None, embedder=_BoomEmbedder())
    vector, name = await manager._embed("你好")
    assert name == "hashing-256"
    assert len(vector) == 256


@pytest.mark.asyncio
async def test_embed_uses_primary_when_ok():
    manager = MemoryManager(None, embedder=_StubEmbedder())
    vector, name = await manager._embed("你好")
    assert name == "stub"
    assert vector == [1.0, 0.0]


def test_compatible_guards_dim_mismatch():
    assert _compatible([1.0, 0.0], [0.0, 1.0]) is True
    assert _compatible([1.0, 0.0], [1.0, 0.0, 0.0]) is False
    assert _compatible([1.0, 0.0], []) is False
    assert _compatible([1.0, 0.0], None) is False
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_memory.py -k "embed or compatible" -v`
Expected: FAIL（`_embed`/`_compatible` 不存在，`AttributeError`）

- [ ] **Step 3: 实现**

按 Interfaces 改 `MemoryManager.__init__`，加 `_embed`/`_compatible`；`remember`（原 line 125 的 `self.embedder.embed(text)` → `await self._embed`，去重循环加 `_compatible`，insert 加 `embedder` 字段）；`retrieve`（原 line 166 同理，评分循环加 `_compatible`）。

- [ ] **Step 4: 运行测试确认通过 + 全量回归**

Run: `.venv/Scripts/python -m pytest tests/test_memory.py -v` 然后 `.venv/Scripts/python -m pytest -q`
Expected: 新测试 PASS；全量（155 + 新增 11）全绿

- [ ] **Step 5: 提交（先展示变更摘要）**

`git add services/harness/core/memory.py tests/test_memory.py` → `feat(harness): 记忆接入真实嵌入并带维度守卫与失败回退`

---

### Task 4: 文档更新 + 全量验证 + 提交计划文档

**Files:**
- Modify: `docs/RUNBOOK.md`（line 229 附近：把「接真实 embedding 后重建索引」改成已支持 + 换模型需重建向量的说明）
- Modify: `docs/ARCHITECTURE.md`（line 139 附近：哈希向量 → 默认哈希、可切真实嵌入 + 失败回退 + 维度守卫）
- Modify: `services/harness/core/memory.py`（顶部模块 docstring line 7：「配置模型后可替换」→「配置端点后自动切换，失败回退哈希」）

- [ ] **Step 1: 更新文档与 docstring**

- [ ] **Step 2: 全量验证**

Run: `.venv/Scripts/python -m pytest -q` + `.venv/Scripts/python -m ruff check packages services tests`
Expected: 全绿

- [ ] **Step 3: 提交**

`git add services/harness/core/memory.py docs/RUNBOOK.md docs/ARCHITECTURE.md` → `docs(harness): 更新嵌入方案说明`
`git add implementation_plan_embedding.md` → `docs: 真实 embedding 实现计划`

---

## 验证命令（全部完成后）

```bash
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff check packages services tests
cd web && npm run build
```

## 风险点与取舍

1. **换 embedder 后旧向量不可召回**：维度不同直接跳过，等价于「重建索引」。记忆是短生命周期数据（工作记忆 24h、情节摘要按会话滚动），无回填机制在本计划外；RUNBOOK 注明。维度守卫保证「不误召回」优先于「全召回」。
2. **失败回退哈希而非熔断**：与 LLM 的 FailoverProvider 不同，嵌入回退放在 `MemoryManager._embed` 内——因为向量要落盘，记录必须诚实标注「实际产出向量的 embedder」，用包装器隐藏回退会把标签写错；`_compatible` 兜住维度混存。
3. **不做批量嵌入**：记忆写入/召回都是单条文本，OpenAI 端点 `input` 传字符串即可；批量优化等有真实规模需求再说（YAGNI）。
4. **`transport` 注入仅用于测试**：与 `OpenAICompatibleProvider` 不同，这里显式注入 `httpx.MockTransport` 做离线单测，不引入 respx 依赖。
5. **嵌入失败不熔断**：嵌入是增强路径不是关键路径，回退哈希即满足 FR-14「不阻塞客服」，无需熔断状态机。
