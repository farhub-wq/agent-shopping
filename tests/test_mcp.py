import pytest
from harness.core.mcp import (
    MCPHub,
    MCPServer,
    build_mcp_hub,
    discover_and_register,
    parse_mcp_servers,
    register_mcp_tools,
)
from harness.core.policy import ToolPolicy
from harness.core.tools import Tool, ToolContext, ToolRegistry


def _registry() -> ToolRegistry:
    registry = ToolRegistry()

    async def echo(args, ctx):
        return {"echo": args.get("text"), "user": ctx.user_id}

    registry.register(
        Tool(
            "echo",
            "回显文本",
            {"type": "object", "properties": {"text": {"type": "string"}}},
            echo,
            ToolPolicy("echo", permission="read"),
        )
    )
    return registry


@pytest.mark.asyncio
async def test_mcp_initialize_and_list_tools():
    server = MCPServer("test-server", _registry())
    ctx = ToolContext("u1", "customer", "default", "s1", "r1")
    init = await server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, ctx, "reviewed")
    assert init["result"]["serverInfo"]["name"] == "test-server"
    tools = await server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, ctx, "reviewed")
    assert [item["name"] for item in tools["result"]["tools"]] == ["echo"]


@pytest.mark.asyncio
async def test_mcp_tool_call_reaches_handler():
    server = MCPServer("test-server", _registry())
    ctx = ToolContext("u1", "customer", "default", "s1", "r1")
    response = await server.handle(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "hi"}}},
        ctx,
        "reviewed",
    )
    assert response["result"]["isError"] is False
    assert "hi" in response["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_mcp_unknown_method_returns_error():
    server = MCPServer("test-server", _registry())
    ctx = ToolContext("u1", "customer", "default", "s1", "r1")
    response = await server.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"}, ctx, "reviewed")
    assert response["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_mcp_permission_denied_is_error_payload():
    registry = ToolRegistry()

    async def secret(args, ctx):
        return {"secret": True}

    registry.register(
        Tool(
            "secret",
            "敏感工具",
            {"type": "object"},
            secret,
            ToolPolicy("secret", permission="read", data_class="secrets"),
        )
    )
    server = MCPServer("test-server", registry)
    ctx = ToolContext("u1", "customer", "default", "s1", "r1")
    response = await server.handle(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "secret", "arguments": {}}},
        ctx,
        "reviewed",
    )
    assert response["result"]["isError"] is True


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
    registry = ToolRegistry()
    register_mcp_tools(
        registry, MCPHub(), [{"name": "mcp__srv__bare", "server": "srv", "description": "d"}]
    )
    tool = registry.get("mcp__srv__bare")
    assert tool is not None
    assert tool.input_schema == {"type": "object", "properties": {}}


def test_settings_have_mcp_servers_default():
    from shop_common.config import ServiceSettings

    assert ServiceSettings().mcp_http_servers == ""
