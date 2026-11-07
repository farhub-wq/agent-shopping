# MCP 客户端接线 Implementation Plan

**Goal:** 把已写好但未启用的 MCP 客户端代码（`HTTPMCPClient` / `MCPHub` / `register_mcp_tools`）接进 harness 启动流程：按配置发现远端 MCP Server 的工具，以最小权限注册进本地工具网关，Agent 即可像调用本地工具一样调用远端 MCP 工具。

**Architecture:** 新增配置 `mcp_http_servers`（逗号分隔的 `name=endpoint`），启动时 `build_mcp_hub` 建 hub → `discover_and_register` 做 `refresh()`（逐个 server initialize + tools/list，单 server 失败只降级该来源）→ `register_mcp_tools` 把远端工具以 `mcp__<server>__<tool>` 前缀 + `read/low/public` 最小权限注册进 `registry`。运行循环、技能、护栏全部复用现有代码，无需改动。

**Tech Stack:** Python 3.11 · asyncio · httpx（已在用）· 现有 `ToolRegistry` / `ToolPolicy`。

**Spec:** README「Harness 控制面：…MCP（HTTP / stdio）…」；`core/mcp.py` 现有 docstring 与注释（工具发现/调用/降级语义）。

## 范围说明

- **本次只接 HTTP 客户端**（代码已完备，纯接线 + 配置 + 测试）。
- **stdio 传输不在本次范围**：`StdioMCPClient` 已实现但 `MCPHub` 只聚合 HTTP 客户端，且 `mcp_servers/` 目录为空（需要一个真正的 stdio server 模块）。接通 stdio 需要泛化 `MCPHub` 的客户端接口 + 新建 stdio server runner，属独立任务，后续单独立计划。

## Global Constraints

- 新增/修改代码写**简明中文注释**（说明为什么），沿用文件现有注释语言与密度。
- 不吞异常：MCP 发现失败是「降级该来源 + 记日志」，不是静默跳过（`MCPHub.refresh` 已有此语义，沿用）。
- MCP 工具一律按最小权限登记（`read/low/public`），**不得**让远端声明自己的权限——本地这层是防绕过审批的闸门。
- 单文件不超 300 行；不引入新依赖。
- 提交：Conventional Commits + 简体中文，提交前向用户展示变更摘要，待确认后提交。

## Review Focus

1. **空/畸形配置不能崩启动**：`MCP_SERVERS` 未配置、空段、缺 `=` 的段都要被跳过并告警，而不是抛异常 —— Task 1 的 `test_parse_mcp_servers_skips_malformed_and_empty` 锁定。
2. **单个 server 挂掉不能阻断其余工具发现**：`refresh()` 遇到异常要跳过该 server 继续 —— Task 2 的 `test_hub_refresh_skips_failing_server` 锁定。
3. **远端工具名冲突被前缀隔离**：同名工具在不同 server 下要互不覆盖，且调用时前缀要正确剥回原名 —— Task 2 的 `test_hub_discovers_registers_and_routes_tool` 锁定。
4. **远端工具必须以最小权限登记**：即便远端声称高风险，本地也只给 `read/low/public` —— Task 2 同测试断言 `tool.policy.permission == "read"` 与 `data_class == "public"` 锁定。
5. **远端 inputSchema 缺失不崩**：缺 `inputSchema` 的工具回退为空对象 schema —— Task 2 的 `test_register_mcp_tools_falls_back_schema` 锁定。

---

### Task 1: 配置解析 + hub 构建

**Files:**
- Modify: `services/harness/core/mcp.py`（新增 `parse_mcp_servers`、`build_mcp_hub`）
- Test: `tests/test_mcp.py`

**Interfaces:**
- Produces:
  ```python
  def parse_mcp_servers(raw: str) -> list[tuple[str, str]]:
      # 逗号分隔的 "name=endpoint" 列表；空串/空段跳过，缺 "=" 或 name/endpoint 为空记 warning 跳过
      # 返回 [(name, endpoint), ...]

  def build_mcp_hub(raw: str) -> MCPHub:
      # 对每个 (name, endpoint) 注册一个 HTTPMCPClient；未配置返回空 hub
  ```

- [ ] **Step 1: 写失败测试** `tests/test_mcp.py`（追加）

```python
from harness.core.mcp import build_mcp_hub, parse_mcp_servers


def test_parse_mcp_servers_parses_name_endpoint_pairs():
    assert parse_mcp_servers("ops=http://ops:8000/mcp, erp=http://erp:8000/mcp") == [
        ("ops", "http://ops:8000/mcp"),
        ("erp", "http://erp:8000/mcp"),
    ]


def test_parse_mcp_servers_skips_malformed_and_empty():
    assert parse_mcp_servers("") == []
    assert parse_mcp_servers("ops=http://x, bad-entry, =http://y") == [("ops", "http://x")]


def test_build_mcp_hub_registers_clients():
    hub = build_mcp_hub("ops=http://ops:8000/mcp")
    assert set(hub.clients) == {"ops"}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_mcp.py -k "parse or build_mcp_hub" -v`
Expected: FAIL（`ImportError: cannot import name 'parse_mcp_servers'`）

- [ ] **Step 3: 实现**

`parse_mcp_servers`：按 `,` 拆分，`strip` 每段，跳过空段；`partition("=")` 拆分，name/endpoint 任一为空则 `logger.warning` 并跳过。`build_mcp_hub`：遍历解析结果 `hub.register(name, HTTPMCPClient(endpoint))`。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_mcp.py -k "parse or build_mcp_hub" -v`
Expected: PASS（3 passed）

- [ ] **Step 5: 提交（先向用户展示变更摘要）**

`git add services/harness/core/mcp.py tests/test_mcp.py` → `feat(harness): MCP Server 配置解析与 hub 构建`

---

### Task 2: 工具发现注册 + 锁定既有行为

**Files:**
- Modify: `services/harness/core/mcp.py`（新增 `discover_and_register`）
- Test: `tests/test_mcp.py`（追加）

**Interfaces:**
- Consumes: `MCPHub`、`register_mcp_tools`（均已存在）
- Produces:
  ```python
  async def discover_and_register(registry: ToolRegistry, hub: MCPHub) -> list[dict[str, Any]]:
      # discovered = await hub.refresh(); register_mcp_tools(registry, hub, discovered); return discovered
  ```

- [ ] **Step 1: 写失败测试** `tests/test_mcp.py`（追加）

```python
class _FakeMCPClient:
    def __init__(self, tools):
        self.tools = tools
        self.calls = []

    async def initialize(self):
        return {}

    async def list_tools(self):
        return self.tools

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"ok": True}


@pytest.mark.asyncio
async def test_hub_discovers_registers_and_routes_tool():
    from harness.core.mcp import MCPHub, discover_and_register

    registry = ToolRegistry()
    hub = MCPHub()
    client = _FakeMCPClient([{"name": "echo", "description": "回显", "inputSchema": {"type": "object"}}])
    hub.register("ops", client)
    discovered = await discover_and_register(registry, hub)
    assert [t["name"] for t in discovered] == ["mcp__ops__echo"]
    tool = registry.get("mcp__ops__echo")
    assert tool is not None
    assert tool.policy.permission == "read"
    assert tool.policy.data_class == "public"
    result = await registry.execute(
        "mcp__ops__echo", {"x": 1}, ToolContext("u1", "customer", "default", "s1", "r1"), trust="reviewed"
    )
    assert result.status == "ok"
    assert client.calls == [("echo", {"x": 1})]  # 前缀被剥回原工具名


@pytest.mark.asyncio
async def test_hub_refresh_skips_failing_server():
    from harness.core.mcp import MCPHub

    hub = MCPHub()
    hub.register("good", _FakeMCPClient([{"name": "echo", "description": "d", "inputSchema": {}}]))

    class _Broken:
        async def initialize(self):
            raise RuntimeError("down")

        async def list_tools(self):
            return []

    hub.register("broken", _Broken())
    discovered = await hub.refresh()
    assert [t["name"] for t in discovered] == ["mcp__good__echo"]


def test_register_mcp_tools_falls_back_schema():
    from harness.core.mcp import MCPHub, register_mcp_tools

    registry = ToolRegistry()
    register_mcp_tools(
        registry, MCPHub(), [{"name": "mcp__srv__bare", "server": "srv", "description": "d"}]
    )
    tool = registry.get("mcp__srv__bare")
    assert tool is not None
    assert tool.input_schema == {"type": "object", "properties": {}}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_mcp.py -k "hub or register_mcp_tools" -v`
Expected: 前两个 FAIL（`ImportError: discover_and_register`），第三个 PASS（`register_mcp_tools` 已存在，仅锁定行为）

- [ ] **Step 3: 实现 `discover_and_register`**

三行：`discovered = await hub.refresh()` → `register_mcp_tools(registry, hub, discovered)` → `return discovered`。注释说明「单 server 失败已在 refresh 内降级」。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_mcp.py -v`
Expected: 全部 PASS（原 4 个 + 新 3 个）

- [ ] **Step 5: 提交（先向用户展示变更摘要）**

`git add services/harness/core/mcp.py tests/test_mcp.py` → `test(harness): 锁定 MCP 发现注册与路由行为`

---

### Task 3: 配置字段 + .env.example

**Files:**
- Modify: `packages/shop_common/config.py`（新增「MCP」段）
- Modify: `.env.example`
- Test: `tests/test_mcp.py`（追加）

**Interfaces:**
- Produces: `ServiceSettings.mcp_http_servers: str = ""`

- [ ] **Step 1: 写失败测试**

```python
def test_settings_have_mcp_servers_default():
    from shop_common.config import ServiceSettings

    assert ServiceSettings().mcp_http_servers == ""
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python -m pytest tests/test_mcp.py::test_settings_have_mcp_servers_default -v`
Expected: FAIL（`AttributeError: 'ServiceSettings' object has no attribute 'mcp_http_servers'`）

- [ ] **Step 3: 实现**

`config.py` 在「模型与嵌入」之后加 `mcp_http_servers: str = ""`（注释：逗号分隔 `name=endpoint`，留空不接远端 MCP）。`.env.example` 在模型接入段后加 `MCP_SERVERS=`（留空）。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python -m pytest tests/test_mcp.py::test_settings_have_mcp_servers_default -v`
Expected: PASS

- [ ] **Step 5: 提交（先向用户展示变更摘要）**

`git add packages/shop_common/config.py .env.example tests/test_mcp.py` → `feat(harness): 新增 MCP Server 配置项`

---

### Task 4: lifespan 接线 + 健康检查

**Files:**
- Modify: `services/harness/app.py`（lifespan、healthz、全局变量、import）

**Interfaces:**
- Consumes: `build_mcp_hub`、`discover_and_register`（Task 1/2）、`settings.mcp_http_servers`（Task 3）

- [ ] **Step 1: 实现接线**

`app.py` 顶部 import 增加 `build_mcp_hub, discover_and_register`；新增全局 `mcp_hub: MCPHub | None = None`；lifespan 中在 `mcp_server = MCPServer(...)` **之前**插入：

```python
mcp_hub = build_mcp_hub(settings.mcp_http_servers)
discovered = await discover_and_register(registry, mcp_hub)
logger.info("已从 %d 个 MCP 源注册 %d 个工具", len(mcp_hub.clients), len(discovered))
```

（放在 `MCPServer` 之前，让 `/mcp`、`/tools` 的清单与工具网关一致。）`healthz` 返回体增加 `"mcp_sources": len(mcp_hub.clients) if mcp_hub else 0`。

- [ ] **Step 2: 验证**

Run: `.venv/Scripts/python -m pytest -q`（lifespan 依赖 Redis，由完整套件间接覆盖导入与类型；无独立单测）
Expected: 全绿

Run（冒烟，可选，需本机 Redis/Mongo）：启动 harness 后 `curl localhost:8006/healthz` 看到 `mcp_sources` 字段。

- [ ] **Step 3: 提交（先向用户展示变更摘要）**

`git add services/harness/app.py` → `feat(harness): 启动时发现并注册远端 MCP 工具`

---

## 验证命令（全部任务完成后）

```bash
.venv/Scripts/python -m pytest -q
.venv/Scripts/python -m ruff check packages services tests
cd web && npm run build   # 无前端改动，仅确认不破坏
```

## 风险点与取舍

1. **启动即一次性发现，无周期刷新**：远端 server 若在启动后新增/移除工具，本进程不会感知；若启动时 server 未就绪，其工具整个进程生命周期内都缺失。周期刷新属后续增强（可复用 `background_tasks` 开关），本次不做。
2. **启动可能被慢 server 拖慢**：`refresh` 逐个串行，每个 server 最多等 15s 超时；配置多个不可达 server 会线性拖慢启动。当前 MCP server 数量少可接受，必要时后续并行化。
3. **同名 server 配置会覆盖**：`MCPHub.register` 按 name 覆盖，两个 `name` 相同的配置后者覆盖前者，未做唯一性校验（配置方需保证 name 唯一）。
4. **MCP 工具权限固定为 `read/low/public`**：这意味着跨服务写操作经 MCP 进来会被策略拦截（需要更高 permission 的工具无法通过 MCP 提供），这是**有意为之**的安全下限，不是缺陷；若后续需要经 MCP 提供写工具，需单独设计信任来源，不在本次范围。
