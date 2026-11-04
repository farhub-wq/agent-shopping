import pytest
from harness.core.mcp import MCPServer
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
