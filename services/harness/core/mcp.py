"""MCP 协议层（自研最小实现）：JSON-RPC 2.0 + tools/list + tools/call。

支持两种传输：
- HTTP：一次请求一个 JSON-RPC 包，适合容器间调用；
- stdio：按行收发，适合把本地进程当作 MCP Server。

不追求覆盖完整规范，只保证「发现工具 / 调用工具 / 错误可诊断」三件事闭环。
"""

import asyncio
import itertools
import json
import sys
from typing import Any

import httpx

from shop_common.logging import get_logger

from .tools import Tool, ToolContext, ToolRegistry

logger = get_logger(__name__)

PROTOCOL_VERSION = "2024-11-05"


class MCPError(Exception):
    def __init__(self, code: int, message: str, data: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class MCPServer:
    """把本地 ToolRegistry 暴露成 MCP Server。"""

    def __init__(self, name: str, registry: ToolRegistry, version: str = "0.1.0") -> None:
        self.name = name
        self.registry = registry
        self.version = version

    async def handle(self, request: dict[str, Any], ctx: ToolContext, trust: str) -> dict[str, Any]:
        request_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params") or {}
        try:
            result = await self._dispatch(method, params, ctx, trust)
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except MCPError as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": exc.code, "message": exc.message, "data": exc.data},
            }
        except Exception as exc:
            logger.exception("MCP 请求处理失败 method=%s", method)
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": f"内部错误: {type(exc).__name__}"},
            }

    async def _dispatch(
        self, method: str, params: dict[str, Any], ctx: ToolContext, trust: str
    ) -> dict[str, Any]:
        if method == "initialize":
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {"name": self.name, "version": self.version},
                "capabilities": {"tools": {"listChanged": False}},
            }
        if method == "ping":
            return {"pong": True}
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": spec["name"],
                        "description": spec["description"],
                        "inputSchema": spec["input_schema"],
                    }
                    for spec in self.registry.specs()
                ]
            }
        if method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments") or {}
            result = await self.registry.execute(name, arguments, ctx, trust=trust)
            return {
                "content": [{"type": "text", "text": json.dumps(result.to_dict(), ensure_ascii=False)}],
                "isError": not result.ok,
            }
        raise MCPError(-32601, f"不支持的方法 {method}")


class HTTPMCPClient:
    def __init__(self, endpoint: str, *, timeout: float = 15.0) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self._ids = itertools.count(1)

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or {}}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(self.endpoint, json=payload)
        body = response.json()
        if "error" in body:
            error = body["error"]
            raise MCPError(int(error.get("code", -32000)), str(error.get("message", "MCP 调用失败"),), error.get("data"))
        return body.get("result")

    async def initialize(self) -> dict[str, Any]:
        return await self._rpc("initialize", {"protocolVersion": PROTOCOL_VERSION})

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._rpc("tools/list")
        return result.get("tools", [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._rpc("tools/call", {"name": name, "arguments": arguments})


class StdioMCPClient:
    """按行 JSON 的 stdio 传输；使用 python -m 启动子进程服务。"""

    def __init__(self, module: str) -> None:
        self.module = module
        self.process: asyncio.subprocess.Process | None = None
        self._ids = itertools.count(1)

    async def start(self) -> None:
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            "-m",
            self.module,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self.process is None or self.process.stdin is None or self.process.stdout is None:
            raise MCPError(-32000, "stdio MCP 客户端未启动")
        payload = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, "params": params or {}}
        self.process.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
        await self.process.stdin.drain()
        line = await asyncio.wait_for(self.process.stdout.readline(), timeout=15)
        body = json.loads(line.decode("utf-8"))
        if "error" in body:
            raise MCPError(int(body["error"].get("code", -32000)), body["error"].get("message", "MCP 调用失败"))
        return body.get("result")

    async def initialize(self) -> dict[str, Any]:
        return await self._rpc("initialize", {"protocolVersion": PROTOCOL_VERSION})

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._rpc("tools/list")
        return result.get("tools", [])

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._rpc("tools/call", {"name": name, "arguments": arguments})

    async def close(self) -> None:
        if self.process is not None:
            self.process.terminate()
            await self.process.wait()


class MCPHub:
    """聚合多个 MCP Server 的工具，并把调用路由回对应客户端。"""

    def __init__(self) -> None:
        self.clients: dict[str, HTTPMCPClient] = {}
        self._routes: dict[str, str] = {}

    def register(self, name: str, client: HTTPMCPClient) -> None:
        self.clients[name] = client

    async def refresh(self) -> list[dict[str, Any]]:
        self._routes.clear()
        tools: list[dict[str, Any]] = []
        for name, client in self.clients.items():
            try:
                await client.initialize()
                discovered = await client.list_tools()
            except Exception:
                logger.exception("MCP Server %s 不可用，跳过", name)
                continue
            for tool in discovered:
                qualified = f"mcp__{name}__{tool['name']}"
                self._routes[qualified] = name
                tools.append({**tool, "name": qualified, "server": name})
        return tools

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        server = self._routes.get(name)
        if server is None:
            raise MCPError(-32601, f"未知 MCP 工具 {name}")
        return await self.clients[server].call_tool(name.split("__", 2)[2], arguments)


def register_mcp_tools(registry: ToolRegistry, hub: MCPHub, discovered: list[dict[str, Any]]) -> None:
    """把远端 MCP 工具包装成本地 Tool，统一走同一套权限与审批护栏。"""
    for spec in discovered:
        name = spec["name"]

        def _make_handler(tool_name: str):
            async def handler(args: dict[str, Any], _ctx: ToolContext) -> Any:
                return await hub.call(tool_name, args)

            return handler

        from .policy import ToolPolicy

        registry.register(
            Tool(
                name=name,
                description=f"[MCP:{spec.get('server')}] {spec.get('description', '')}",
                input_schema=spec.get("inputSchema") or {"type": "object", "properties": {}},
                handler=_make_handler(name),
                policy=ToolPolicy(name=name, permission="read", risk="low", data_class="public"),
            )
        )
