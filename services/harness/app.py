import json
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from shop_common.auth import PrincipalDep
from shop_common.config import get_settings
from shop_common.db import init_models
from shop_common.errors import ApiError, install_error_handlers
from shop_common.events import create_redis
from shop_common.logging import configure_logging, get_logger

from .core.llm import build_provider
from .core.manifest import load_all, load_skill
from .core.mcp import MCPServer
from .core.memory import MemoryManager
from .core.policy import summarize
from .core.runtime import AgentRuntime
from .core.sandbox import SandboxManager
from .core.tools import ToolContext, registry
from .core.tools_impl import BUSINESS_TOOLS  # noqa: F401  （导入即注册业务工具）
from .core.trace import load_trace

settings = get_settings()
logger = get_logger(__name__)

SKILL_ROOT = Path(__file__).parent / "skills"

redis: Redis | None = None
memory: MemoryManager | None = None
runtime: AgentRuntime | None = None
mcp_server: MCPServer | None = None
sandbox = SandboxManager()


@asynccontextmanager
async def lifespan(_: FastAPI):
    global redis, memory, runtime, mcp_server
    configure_logging("harness")
    await init_models()
    redis = create_redis(settings.redis_url)
    memory = MemoryManager(redis)
    # 启动时一次性完成 SKILL.md 准入扫描，findings 随 manifest 缓存；
    # 运行期只读这份快照，保证同一次进程生命周期内准入结论稳定。
    skills = {item.name: item for item in load_all(SKILL_ROOT)}
    runtime = AgentRuntime(registry, build_provider(), skills, memory)
    mcp_server = MCPServer("shop-agent-harness", registry)
    logger.info("已加载 %s 个技能：%s", len(skills), ", ".join(skills))
    yield
    await redis.close()


app = FastAPI(title="agent-harness", version="0.1.0", lifespan=lifespan)
install_error_handlers(app, "harness")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: str | None = None


class MemoryWrite(BaseModel):
    text: str
    kind: str = "episodic"
    importance: float = 0.5


class MemoryQuery(BaseModel):
    query: str
    k: int = 5


class SandboxRequest(BaseModel):
    code: str
    language: str = "python"
    timeout: int | None = None


def _ctx(principal, session_id: str | None) -> ToolContext:
    return ToolContext(
        user_id=principal.user_id,
        role=principal.role,
        tenant=principal.tenant,
        session_id=session_id or f"s-{uuid.uuid4().hex[:12]}",
        run_id=uuid.uuid4().hex,
    )


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "harness",
        "skills": len(runtime.skills) if runtime else 0,
        "tools": len(registry.names()),
        "sandbox_enabled": settings.sandbox_enabled,
    }


@app.get("/agents")
async def list_agents() -> dict[str, Any]:
    assert runtime is not None
    return {
        "items": [
            {
                "name": manifest.name,
                "description": manifest.description,
                "trust": manifest.trust,
                "allowed_tools": manifest.allowed_tools,
                # 只要存在一条 high 级 finding 就整体判 deny：准入是「一票否决」，
                # 避免部分风险被 medium 项稀释后放行。
                "verdict": "deny"
                if any(item.severity == "high" for item in manifest.findings)
                else "allow",
            }
            for manifest in runtime.skills.values()
        ]
    }


@app.get("/skills")
async def list_skills() -> dict[str, Any]:
    assert runtime is not None
    return {"items": [manifest.to_dict() for manifest in runtime.skills.values()]}


@app.post("/skills/{name}/verify")
async def verify_skill(name: str) -> dict[str, Any]:
    path = SKILL_ROOT / name / "SKILL.md"
    if not path.exists():
        raise ApiError("NOT_FOUND", f"技能 {name} 不存在")
    # 重新扫描磁盘上的最新内容，供发布前/排障时核对准入结论；
    # 这里的结果不会替换 lifespan 中缓存的运行时 manifest。
    manifest = load_skill(path)
    return manifest.to_dict()


@app.get("/policies")
async def policies() -> dict[str, Any]:
    return {
        "trust_tiers": summarize(),
        "tools": registry.specs(),
        "note": "信任等级决定权限上限；技能 manifest 不能自我提权。",
    }


@app.get("/tools")
async def list_tools() -> dict[str, Any]:
    return {"items": registry.specs()}


@app.post("/agents/{skill_name}/chat")
async def chat(
    skill_name: str,
    payload: ChatRequest,
    principal: PrincipalDep,
    stream: bool = Query(True),
):
    assert runtime is not None
    ctx = _ctx(principal, payload.session_id)
    if not stream:
        result = await runtime.run(skill_name, payload.message, ctx)
        return result.to_dict()

    async def generator():
        async for event in runtime.run_stream(skill_name, payload.message, ctx):
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream")


@app.post("/runs/{run_id}/approve")
async def approve_run(run_id: str, principal: PrincipalDep) -> dict[str, Any]:
    assert runtime is not None
    principal.require_staff()
    # 审批重放必须绑定原 run_id：待审批负载按 run_id 存在 Redis 里，
    # 审批人点击后取回原工具与参数执行，避免重新让模型生成参数。
    ctx = ToolContext(
        user_id=principal.user_id,
        role=principal.role,
        tenant=principal.tenant,
        session_id=f"approve-{run_id}",
        run_id=run_id,
    )
    return (await runtime.approve_and_replay(run_id, ctx)).to_dict()


@app.get("/runs/{run_id}/evidence")
async def get_evidence(run_id: str) -> dict[str, Any]:
    trace = load_trace(run_id)
    if not trace:
        raise ApiError("NOT_FOUND", "运行记录不存在")
    # 证据接口只做投影，不重算：直接读 jsonl 里已落盘的事件，
    # 保证返回内容与当时 trace_digest 覆盖的内容一致。
    start = next((item for item in trace if item["type"] == "run.start"), {})
    end = next((item for item in trace if item["type"] == "run.end"), {})
    tool_calls = [item for item in trace if item["type"] == "tool.result"]
    return {
        "run_id": run_id,
        "skill": start.get("skill"),
        "trust": start.get("trust"),
        "status": end.get("status"),
        "tool_calls": len(tool_calls),
        "denied_calls": len([item for item in tool_calls if item.get("status") in {"denied", "needs_approval"}]),
        "trace": trace,
    }


@app.get("/memory/{user_id}")
async def memory_records(user_id: str, principal: PrincipalDep) -> dict[str, Any]:
    assert memory is not None
    # 记忆是用户私有数据：普通用户只能读写自己的记录，运营/管理员才可跨用户查看。
    if not principal.is_staff() and principal.user_id != user_id:
        raise ApiError("FORBIDDEN", "无权查看他人记忆")
    return {"items": await memory.list_records(user_id)}


@app.post("/memory/{user_id}")
async def memory_write(user_id: str, payload: MemoryWrite, principal: PrincipalDep) -> dict[str, Any]:
    assert memory is not None
    if not principal.is_staff() and principal.user_id != user_id:
        raise ApiError("FORBIDDEN", "无权写入他人记忆")
    record_id = await memory.remember(
        user_id, payload.text, kind=payload.kind, importance=payload.importance, tenant=principal.tenant
    )
    return {"id": record_id}


@app.post("/memory/{user_id}/search")
async def memory_search(user_id: str, payload: MemoryQuery, principal: PrincipalDep) -> dict[str, Any]:
    assert memory is not None
    if not principal.is_staff() and principal.user_id != user_id:
        raise ApiError("FORBIDDEN", "无权检索他人记忆")
    return {"items": await memory.retrieve(user_id, payload.query, k=payload.k)}


@app.get("/sandbox/health")
async def sandbox_health() -> dict[str, Any]:
    return {
        "enabled": settings.sandbox_enabled,
        "docker_sdk": sandbox.docker_available(),
        "image": settings.sandbox_image,
        "network": settings.sandbox_network,
        "limits": {
            "cpu": settings.sandbox_cpu,
            "memory_mb": settings.sandbox_memory_mb,
            "timeout_seconds": settings.sandbox_timeout_seconds,
        },
    }


@app.post("/sandbox/run")
async def sandbox_run(payload: SandboxRequest, principal: PrincipalDep) -> dict[str, Any]:
    # 沙箱执行属于高危能力，只能由运营/管理员触发，不开放给普通终端用户。
    principal.require_staff()
    result = await sandbox.run(payload.code, language=payload.language, timeout=payload.timeout)
    return result.to_dict()


@app.post("/mcp")
async def mcp_endpoint(request: Request, principal: PrincipalDep) -> dict[str, Any]:
    """对外 MCP 入口：任何支持 MCP 的客户端都可以把这里当成工具服务器。"""
    assert mcp_server is not None
    body = await request.json()
    ctx = _ctx(principal, f"mcp-{principal.user_id}")
    # 信任等级由平台角色推导，不接受客户端在 JSON-RPC 包里自报；
    # 这是「manifest 不能提权」在 MCP 入口上的同一原则。
    trust = "certified" if principal.role == "admin" else ("vendor" if principal.role == "operator" else "reviewed")
    return await mcp_server.handle(body, ctx, trust=trust)
