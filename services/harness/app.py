import json
import math
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from shop_common.audit import publish_audit
from shop_common.auth import PrincipalDep
from shop_common.config import (
    COMMON_SECRETS,
    RUNNER_SECRETS,
    get_settings,
    validate_production_settings,
)
from shop_common.db import dispose_engine, get_session_factory, init_models
from shop_common.errors import ApiError, install_error_handlers
from shop_common.events import EventBus, create_redis
from shop_common.logging import configure_logging, get_logger
from shop_common.metrics import install_metrics
from shop_common.mongo import close_mongo
from shop_common.sandbox import MAX_CODE_CHARS
from sqlalchemy import and_, case, func, or_, select, update

from .core.context import context_window, estimate_tokens
from .core.conversation import (
    ARCHIVE_ENCODING,
    compress_records,
    conversation_state,
    decode_cursor,
    decompress_records,
    encode_cursor,
    should_split,
)
from .core.crypto import decrypt_model_key
from .core.llm import build_provider, build_provider_from_record
from .core.manifest import load_all, load_skill
from .core.mcp import MCPHub, MCPServer, build_mcp_hub, discover_and_register
from .core.memory import MemoryManager, merge_session_summary
from .core.policy import summarize
from .core.providers import (
    PRESETS,
    create_provider,
    delete_provider,
    ensure_default_free_ai,
    list_visible_providers,
    resolve_provider,
    test_provider,
    to_dict,
    update_provider,
)
from .core.runtime import AgentRuntime, persist_usage
from .core.sandbox import SandboxManager, validate_sandbox_production
from .core.tools import ToolContext, registry
from .core.tools_impl import BUSINESS_TOOLS  # noqa: F401  （导入即注册业务工具）
from .core.trace import load_trace
from .models import (
    Conversation,
    ConversationSummary,
    Message,
    MessageArchive,
    ModelProvider,
    UsageRecord,
)

settings = get_settings()
logger = get_logger(__name__)

SKILL_ROOT = Path(__file__).parent / "skills"

redis: Redis | None = None
bus: EventBus | None = None
memory: MemoryManager | None = None
runtime: AgentRuntime | None = None
mcp_server: MCPServer | None = None
mcp_hub: MCPHub | None = None
sandbox = SandboxManager()


@asynccontextmanager
async def lifespan(_: FastAPI):
    global redis, bus, memory, runtime, mcp_server, mcp_hub
    configure_logging("harness")
    # 生产密钥仍是公开默认值时拒绝启动：否则 JWT 与内网签名身份头都可被任意伪造。
    # harness 还要签发给 runner 的请求，因此额外要求独立的 RUNNER_SIGNING_SECRET，
    # 避免业务内网密钥同时成为执行沙箱代码的凭据。模型密钥主密钥同样不许沿用默认值。
    validate_production_settings(
        settings, secrets=(*COMMON_SECRETS, *RUNNER_SECRETS, "model_key_encryption_key")
    )
    # 生产未启用远程沙箱时拒绝启动：本地子进程与 harness 同权限，等于内网任意代码执行。
    validate_sandbox_production(settings)
    # harness 自本轮起有独立关系库（模型提供商/会话/消息/用量），dev/test 用 create_all 补表，
    # 生产走 Alembic 迁移；记忆与轨迹仍在 Mongo，不受影响。
    await init_models()
    async with get_session_factory()() as session:
        await ensure_default_free_ai(session)
    # 记忆与轨迹都在 Mongo 里；索引由各模块首次使用时幂等补建。
    redis = create_redis(settings.resolved_redis_url)
    # harness 只发布审计事件不消费，无需会话工厂；session_factory 仅消费路径会用到
    bus = EventBus(redis, namespace="shop", session_factory=None)
    memory = MemoryManager(redis)
    # 启动时一次性完成 SKILL.md 准入扫描，findings 随 manifest 缓存；
    # 运行期只读这份快照，保证同一次进程生命周期内准入结论稳定。
    skills = {item.name: item for item in load_all(SKILL_ROOT)}
    # usage_sink 注入真实用量落库；离线/测试环境不写库也不影响运行。
    # 提前构建 provider 并注入 runtime，以便在 shutdown 时关闭其连接池
    provider = build_provider(redis)
    runtime = AgentRuntime(registry, provider, skills, memory, usage_sink=persist_usage)
    # 启动时发现并注册远端 MCP 工具；放在 MCPServer 之前，让 /mcp、/tools 清单与工具网关一致。
    mcp_hub = build_mcp_hub(settings.mcp_http_servers)
    discovered = await discover_and_register(registry, mcp_hub)
    logger.info("已从 %d 个 MCP 源注册 %d 个工具", len(mcp_hub.clients), len(discovered))
    mcp_server = MCPServer("shop-agent-harness", registry)
    logger.info("已加载 %s 个技能：%s", len(skills), ", ".join(skills))
    yield
    # 优雅关闭：先关闭 HTTP 客户端连接池，再释放数据库与缓存连接
    if hasattr(provider, 'close'):
        await provider.close()
    await redis.close()
    await close_mongo()
    await dispose_engine()


app = FastAPI(title="agent-harness", version="0.1.0", lifespan=lifespan)
install_error_handlers(app, "harness")
install_metrics(app, "harness")


class ChatRequest(BaseModel):
    message: str = Field(min_length=1)
    session_id: str | None = None
    conversation_id: str | None = None
    provider_id: str | None = None


class ProviderCreate(BaseModel):
    provider_type: str
    name: str = Field(min_length=1, max_length=64)
    base_url: str = Field(min_length=1, max_length=256)
    model: str = Field(min_length=1, max_length=64)
    api_key: str = ""
    # shared=True 表示创建全局共享（owner 为空，仅 staff 可创建）；否则记当前用户私有。
    shared: bool = False
    is_default: bool = False


class ProviderUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    model: str | None = Field(default=None, min_length=1, max_length=64)
    base_url: str | None = Field(default=None, min_length=1, max_length=256)
    api_key: str | None = None
    is_default: bool | None = None
    enabled: bool | None = None


class MemoryWrite(BaseModel):
    text: str
    kind: str = "episodic"
    importance: float = 0.5


class MemoryQuery(BaseModel):
    query: str
    k: int = 5


class SandboxRequest(BaseModel):
    # 长度上限与 runner 侧一致：入口就拒绝，不让超大请求占网络与容器队列。
    code: str = Field(min_length=1, max_length=MAX_CODE_CHARS)
    language: str = "python"
    # 超时只允许正数；上限在 SandboxManager 内按配置收敛，避免入口直接占满并发槽。
    timeout: int | None = Field(default=None, ge=1)


def _ctx(principal, session_id: str | None, *, conversation_id: str | None = None,
         provider_id: str | None = None, provider_model: str | None = None) -> ToolContext:
    return ToolContext(
        user_id=principal.user_id,
        role=principal.role,
        tenant=principal.tenant,
        session_id=session_id or f"s-{uuid.uuid4().hex[:12]}",
        run_id=uuid.uuid4().hex,
        conversation_id=conversation_id,
        provider_id=provider_id,
        provider_model=provider_model,
    )


async def _append_message(session, conv: Conversation, role: str, content: str) -> Message:
    """写一条会话消息并同步累计 token / 上下文状态，作为拆分判定的唯一数据源。"""
    seq = (
        await session.scalar(
            select(func.max(Message.seq)).where(Message.conversation_id == conv.id)
        )
    ) or 0
    message = Message(
        id=uuid.uuid4().hex,
        conversation_id=conv.id,
        role=role,
        content=content or "",
        seq=seq + 1,
        token_count=estimate_tokens(content),
    )
    session.add(message)
    conv.token_count = int(conv.token_count or 0) + message.token_count
    conv.context_state = conversation_state(conv.token_count, context_window())
    conv.last_message_at = datetime.now(UTC)
    return message


async def _archive_old_messages(session, conv: Conversation, keep_recent: int) -> int:
    """把超出 keep_recent 的较早消息批量压缩归档，原行保留并标记软归档。"""
    rows = (
        await session.execute(
            select(Message)
            .where(Message.conversation_id == conv.id, Message.is_archived.is_(False))
            .order_by(Message.created_at.desc(), Message.id.desc())
            .offset(max(keep_recent, 0))
        )
    ).scalars().all()
    if not rows:
        return 0
    records = [
        {
            "id": msg.id,
            "role": msg.role,
            "content": msg.content,
            "tool_calls": msg.tool_calls_json,
            "seq": msg.seq,
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
        }
        for msg in rows
    ]
    archive = MessageArchive(
        id=uuid.uuid4().hex,
        conversation_id=conv.id,
        user_id=conv.user_id,
        tenant_id=conv.tenant_id,
        message_count=len(records),
        first_message_at=rows[-1].created_at,
        last_message_at=rows[0].created_at,
        encoding=ARCHIVE_ENCODING,
        payload=compress_records(records),
    )
    session.add(archive)
    await session.flush()
    await session.execute(
        update(Message)
        .where(Message.id.in_([msg.id for msg in rows]))
        .values(is_archived=True, archive_id=archive.id)
    )
    return len(records)


async def _persist_assistant(conversation_id: str, answer: str) -> None:
    """会话助手消息落库，并按阈值触发归档；失败只记日志，不阻断已完成的回答。"""
    try:
        async with get_session_factory()() as session:
            conv = await session.get(Conversation, conversation_id)
            if conv is None:
                return
            await _append_message(session, conv, "assistant", answer)
            await session.flush()
            active = await session.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.conversation_id == conversation_id, Message.is_archived.is_(False))
            )
            if (active or 0) > int(settings.agent_conversation_archive_threshold):
                await _archive_old_messages(
                    session, conv, int(settings.agent_conversation_archive_keep)
                )
            await session.commit()
    except Exception:
        logger.exception("会话助手消息持久化失败 conversation_id=%s", conversation_id)


async def _summary_text_for(session, conv: Conversation) -> tuple[str, list[str], int]:
    """取会话交接摘要：摘要表 → Mongo 会话摘要 → 最近原文兜底，返回 (文本, 关键事实, 级别)。"""
    row: ConversationSummary | None = None
    if conv.summary_id:
        row = await session.get(ConversationSummary, conv.summary_id)
    if row is None:
        row = await session.scalar(
            select(ConversationSummary)
            .where(ConversationSummary.conversation_id == conv.id)
            .order_by(ConversationSummary.created_at.desc())
            .limit(1)
        )
    if row is not None and row.text:
        try:
            pinned = json.loads(row.pinned_json or "[]")
        except ValueError:
            pinned = []
        return row.text, [str(item) for item in pinned], int(row.summary_level or 1)

    if memory is not None:
        record = await memory.latest_summary(conv.user_id, conv.id)
        if record and record.get("text"):
            meta = record.get("metadata") or {}
            return (
                str(record["text"]),
                [str(item) for item in (meta.get("pinned") or [])],
                int(meta.get("summary_level") or 1),
            )

    rows = (
        await session.execute(
            select(Message)
            .where(Message.conversation_id == conv.id, Message.is_archived.is_(False))
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(12)
        )
    ).scalars().all()
    if not rows:
        return "", [], 1
    text, meta = merge_session_summary(
        None,
        None,
        [{"role": msg.role, "content": msg.content} for msg in reversed(rows)],
        max_tokens=int(settings.agent_session_summary_max_tokens),
    )
    return text, [str(item) for item in (meta.get("pinned") or [])], int(meta.get("summary_level") or 1)


def _create_split_child(
    session,
    conv: Conversation,
    text: str,
    pinned: list[str],
    level: int,
    *,
    source: str,
) -> tuple[Conversation, str]:
    """创建子会话并挂上交接摘要；旧会话标记为已拆分。返回 (子会话, 摘要文本)。"""
    child = Conversation(
        id=uuid.uuid4().hex,
        user_id=conv.user_id,
        tenant_id=conv.tenant_id,
        skill=conv.skill,
        title=(conv.title or "会话")[:120],
        root_id=conv.root_id or conv.id,
        parent_id=conv.id,
        token_count=0,
        context_state="ok",
    )
    session.add(child)
    if text:
        summary = ConversationSummary(
            id=uuid.uuid4().hex,
            conversation_id=child.id,
            user_id=conv.user_id,
            tenant_id=conv.tenant_id,
            text=text,
            pinned_json=json.dumps(pinned, ensure_ascii=False),
            summary_level=level,
            source=source,
        )
        session.add(summary)
        child.summary_id = summary.id
    conv.context_state = "split"
    return child, text


async def _owned_provider(session, provider_id: str, principal) -> ModelProvider:
    """加载 provider 并做归属校验：共享只许 staff 动，私有只许本人动，跨租户一律 404。"""
    record = await session.get(ModelProvider, provider_id)
    if record is None or record.tenant_id != principal.tenant:
        raise ApiError("NOT_FOUND", "provider 不存在")
    if record.owner_user_id is None:
        if not principal.is_staff():
            raise ApiError("FORBIDDEN", "无权操作共享 provider")
    elif record.owner_user_id != principal.user_id:
        raise ApiError("FORBIDDEN", "无权操作他人 provider")
    return record


def _decrypt_or_error(token: str) -> str:
    try:
        return decrypt_model_key(token)
    except ValueError as exc:
        raise ApiError("BAD_REQUEST", str(exc)) from exc


def _split_reason(conv: Conversation) -> str | None:
    return should_split(
        int(conv.token_count or 0),
        conv.last_message_at,
        now=datetime.now(UTC),
        window=context_window(),
        split_ratio=float(settings.agent_context_split_ratio),
        idle_hours=int(settings.agent_conversation_idle_hours),
    )


def _conv_dict(conv: Conversation) -> dict[str, Any]:
    return {
        "id": conv.id,
        "skill": conv.skill,
        "title": conv.title,
        "root_id": conv.root_id,
        "parent_id": conv.parent_id,
        "token_count": int(conv.token_count or 0),
        "context_state": conv.context_state or "ok",
        "summary_id": conv.summary_id,
        # context = 累计 token 到拆分阈值；idle = 空闲超时。仅作建议，不自动切换会话。
        "split_suggested": _split_reason(conv),
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
        "last_message_at": conv.last_message_at.isoformat() if conv.last_message_at else None,
    }


def _parse_dt(value: str | None, name: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ApiError("BAD_REQUEST", f"{name} 时间格式不合法") from exc


def _p95(values: list[int]) -> int:
    if not values:
        return 0
    values.sort()
    return values[math.ceil(0.95 * len(values)) - 1]


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "harness",
        "skills": len(runtime.skills) if runtime else 0,
        "tools": len(registry.names()),
        "mcp_sources": len(mcp_hub.clients) if mcp_hub else 0,
        "sandbox_enabled": settings.sandbox_enabled,
    }


@app.get("/agents")
async def list_agents(principal: PrincipalDep) -> dict[str, Any]:
    assert runtime is not None
    visible = [
        manifest for manifest in runtime.skills.values()
        if principal.role in manifest.allowed_roles
    ]
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
            for manifest in visible
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
    # 技能按 SKILL.md 声明的 roles 过滤：顾客不能调用 ops-* 运营侧技能，
    # 即使前端绕过了列表直接调 chat 接口，后端也要拒绝。
    manifest = runtime.skills.get(skill_name)
    if manifest is None:
        raise ApiError("NOT_FOUND", f"技能 {skill_name} 不存在")
    if principal.role not in manifest.allowed_roles:
        raise ApiError("FORBIDDEN", f"技能 {skill_name} 不对角色 {principal.role} 开放")
    # 访客只用环境默认/离线模型，会话与用量都不落库，保持既有匿名体验。
    is_guest = principal.user_id == "guest"

    provider = build_provider(redis)
    provider_id: str | None = None
    provider_model: str | None = None
    conversation_id = payload.conversation_id
    # 回灌内容：Redis 工作记忆过期后，用会话摘要把上一段上下文接回来。
    backfill: tuple[str, list[str]] | None = None

    if not is_guest:
        async with get_session_factory()() as session:
            # provider 解析：显式 provider_id → 用户私有最近 → 共享默认免费 AI。
            # 命中但 key 为空（免费 AI 尚未填 key）时回退环境/离线模型。
            record = await resolve_provider(
                session,
                user_id=principal.user_id,
                tenant_id=principal.tenant,
                provider_id=payload.provider_id,
            )
            if record is not None and record.api_key_enc:
                provider = build_provider_from_record(
                    redis,
                    base_url=record.base_url,
                    api_key=decrypt_model_key(record.api_key_enc),
                    model=record.model,
                    provider_type=record.provider_type,
                    provider_id=record.id,
                )
                provider_id = record.id
                provider_model = record.model

            # 会话归属校验：conversation_id 非空必须属于当前用户与租户，否则 403（IDOR）。
            if conversation_id:
                conv = await session.get(Conversation, conversation_id)
                if conv is None or conv.user_id != principal.user_id or conv.tenant_id != principal.tenant:
                    raise ApiError("FORBIDDEN", "无权访问该会话")
                # 空闲超时：本轮开始前先拆出子会话，旧会话继续保持只读，
                # 避免隔天回来继续往一条已超时的上下文里堆消息。
                if _split_reason(conv) == "idle":
                    text, pinned, level = await _summary_text_for(session, conv)
                    conv, _ = _create_split_child(
                        session, conv, text, pinned, level, source="idle"
                    )
                    if text:
                        backfill = (text, pinned)
                elif memory is not None and not await memory.has_working(principal.user_id, conv.id):
                    # Redis 已过期（换设备/隔天/重启）：从摘要或最近原文重建最小上下文。
                    text, pinned, _ = await _summary_text_for(session, conv)
                    if text:
                        backfill = (text, pinned)
            else:
                conv_id = uuid.uuid4().hex
                conv = Conversation(
                    id=conv_id,
                    user_id=principal.user_id,
                    tenant_id=principal.tenant,
                    skill=skill_name,
                    title=payload.message[:30],
                    # 新会话自成一链，后续拆分继承该 root_id。
                    root_id=conv_id,
                )
                session.add(conv)
            await _append_message(session, conv, "user", payload.message)
            await session.commit()
            conversation_id = conv.id

    # 工作记忆按会话隔离：已有 conversation_id 时优先用它，保证换浏览器/刷新后
    # 仍能命中同一份 Redis 记忆；访客与无会话场景才退回前端生成的 session_id。
    session_id = conversation_id or payload.session_id or f"s-{uuid.uuid4().hex[:12]}"
    if backfill and memory is not None:
        await memory.seed_handoff(principal.user_id, session_id, backfill[0], backfill[1])
    ctx = _ctx(
        principal,
        session_id,
        conversation_id=conversation_id,
        provider_id=provider_id,
        provider_model=provider_model,
    )

    if not stream:
        result = await runtime.run(skill_name, payload.message, ctx, provider=provider)
        if not is_guest and conversation_id:
            await _persist_assistant(conversation_id, result.answer)
        data = result.to_dict()
        # 首轮新建/空闲拆分都会产生新的会话 id，必须回传前端，否则下一轮又新建一条会话。
        data["conversation_id"] = conversation_id
        return data

    async def generator():
        answer = ""
        try:
            async for event in runtime.run_stream(skill_name, payload.message, ctx, provider=provider):
                if event.get("type") == "final":
                    answer = event.get("result", {}).get("answer", "")
                    if conversation_id:
                        event.setdefault("result", {})["conversation_id"] = conversation_id
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            # 流结束（含异常收尾）后补落助手消息，不让持久化失败中断 SSE。
            if not is_guest and conversation_id and answer:
                await _persist_assistant(conversation_id, answer)
        yield "data: [DONE]\n\n"

    return StreamingResponse(generator(), media_type="text/event-stream")


@app.post("/runs/{run_id}/approve")
async def approve_run(run_id: str, principal: PrincipalDep, request: Request) -> dict[str, Any]:
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
    result = (await runtime.approve_and_replay(run_id, ctx)).to_dict()
    # harness 无关系库，审计直接投递 Redis Stream；失败不阻断审批结果返回
    if bus is not None:
        try:
            await publish_audit(
                bus,
                actor_id=principal.user_id,
                actor_role=principal.role,
                action="agent.execute_approved",
                target_type="run",
                target_id=run_id,
                request=request,
                tenant=principal.tenant,
            )
        except Exception:
            logger.exception("审计事件投递失败 run_id=%s", run_id)
    return result


@app.get("/runs/{run_id}/evidence")
async def get_evidence(run_id: str, principal: PrincipalDep) -> dict[str, Any]:
    trace = await load_trace(run_id)
    if not trace:
        raise ApiError("NOT_FOUND", "运行记录不存在")
    # 证据接口只做投影，不重算：直接读 Mongo 里已落盘的事件，
    # 保证返回内容与当时 trace_digest 覆盖的内容一致。
    start = next((item for item in trace if item["type"] == "run.start"), {})
    # 证据包含工具入参与结果，可能带用户数据：只允许运行发起人或运营人员查看，
    # 防止拿到 run_id 就能读别人的运行轨迹（IDOR）。
    owner = start.get("user")
    if not principal.is_staff() and owner != principal.user_id:
        raise ApiError("FORBIDDEN", "无权查看该运行证据")
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
    # harness 不再持有 Docker socket：enabled 时以全部 runner 的实时健康状态为准。
    runners = await sandbox.runner_status() if settings.sandbox_enabled else {"status": "disabled", "endpoints": []}
    return {
        "enabled": settings.sandbox_enabled,
        "backend": sandbox.backend,
        "runner_urls": sandbox.endpoints if settings.sandbox_enabled else [],
        "runners": runners,
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
    result = await sandbox.run(
        payload.code,
        language=payload.language,
        timeout=payload.timeout,
        # 透传调用者身份：runner 侧会再校验一次 staff，避免 harness 被绕过时直接执行代码。
        user_id=principal.user_id,
        role=principal.role,
        tenant=principal.tenant,
    )
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


@app.get("/providers/presets")
async def provider_presets() -> dict[str, Any]:
    # 公开元数据：只含厂商端点与默认模型，无任何密钥。
    return {"items": [{"key": key, **preset} for key, preset in PRESETS.items()]}


@app.get("/providers")
async def list_providers(principal: PrincipalDep) -> dict[str, Any]:
    async with get_session_factory()() as session:
        records = await list_visible_providers(
            session,
            user_id=principal.user_id,
            tenant_id=principal.tenant,
            is_staff=principal.is_staff(),
        )
    return {"items": [to_dict(record) for record in records]}


@app.post("/providers", status_code=201)
async def create_provider_route(payload: ProviderCreate, principal: PrincipalDep) -> dict[str, Any]:
    if payload.shared and not principal.is_staff():
        raise ApiError("FORBIDDEN", "仅运营人员可创建共享 provider")
    owner = None if payload.shared else principal.user_id
    # 系统默认只认共享 provider（解析链的默认步按 owner 为空过滤），
    # 私有 provider 的 is_default 无意义，这里直接不落。
    is_default = payload.is_default and payload.shared
    async with get_session_factory()() as session:
        record = await create_provider(
            session,
            owner_user_id=owner,
            tenant_id=principal.tenant,
            provider_type=payload.provider_type,
            name=payload.name,
            base_url=payload.base_url,
            model=payload.model,
            api_key=payload.api_key,
            is_default=is_default,
        )
    return to_dict(record)


@app.patch("/providers/{provider_id}")
async def patch_provider(provider_id: str, payload: ProviderUpdate, principal: PrincipalDep) -> dict[str, Any]:
    async with get_session_factory()() as session:
        record = await _owned_provider(session, provider_id, principal)
        if payload.is_default and record.owner_user_id is not None:
            raise ApiError("FORBIDDEN", "仅共享 provider 可设为默认")
        record = await update_provider(
            session,
            record,
            name=payload.name,
            model=payload.model,
            base_url=payload.base_url,
            api_key=payload.api_key,
            is_default=payload.is_default,
            enabled=payload.enabled,
        )
    return to_dict(record)


@app.delete("/providers/{provider_id}")
async def remove_provider(provider_id: str, principal: PrincipalDep) -> dict[str, Any]:
    async with get_session_factory()() as session:
        record = await _owned_provider(session, provider_id, principal)
        await delete_provider(session, record)
    return {"deleted": True}


@app.post("/providers/{provider_id}/test")
async def test_provider_route(provider_id: str, principal: PrincipalDep) -> dict[str, Any]:
    async with get_session_factory()() as session:
        record = await _owned_provider(session, provider_id, principal)
        if not record.api_key_enc:
            return {"ok": False, "latency_ms": 0, "message": "尚未配置密钥"}
        api_key = _decrypt_or_error(record.api_key_enc)
        base_url, model, provider_type = record.base_url, record.model, record.provider_type
    ok, latency_ms, message = await test_provider(base_url, api_key, model, provider_type)
    return {"ok": ok, "latency_ms": latency_ms, "message": message}


@app.get("/conversations")
async def list_conversations(
    principal: PrincipalDep,
    skill: str | None = Query(None),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    filters = [Conversation.user_id == principal.user_id, Conversation.tenant_id == principal.tenant]
    if skill:
        filters.append(Conversation.skill == skill)
    async with get_session_factory()() as session:
        total = await session.scalar(select(func.count()).select_from(Conversation).where(*filters))
        rows = await session.execute(
            select(Conversation)
            .where(*filters)
            .order_by(Conversation.last_message_at.desc())
            .offset((page - 1) * size)
            .limit(size)
        )
        items = [_conv_dict(conv) for conv in rows.scalars().all()]
    return {"items": items, "page": page, "size": size, "total": total or 0}


async def _owned_conversation(session, conversation_id: str, principal) -> Conversation:
    conv = await session.get(Conversation, conversation_id)
    if conv is None or conv.user_id != principal.user_id or conv.tenant_id != principal.tenant:
        raise ApiError("FORBIDDEN", "无权访问该会话")
    return conv


@app.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: str,
    principal: PrincipalDep,
    limit: int = Query(100, ge=1, le=200),
    cursor: str | None = Query(None),
) -> dict[str, Any]:
    """默认返回最新一页；cursor 沿时间向前翻更早的历史，归档消息不参与展示。"""
    async with get_session_factory()() as session:
        await _owned_conversation(session, conversation_id, principal)
        stmt = select(Message).where(
            Message.conversation_id == conversation_id, Message.is_archived.is_(False)
        )
        if cursor:
            decoded = decode_cursor(cursor)
            if decoded is None:
                raise ApiError("BAD_REQUEST", "cursor 不合法")
            stamp, message_id = decoded
            # 与排序保持一致：时间倒序，同一时间戳用 id 倒序兜底，避免翻页重复/漏条。
            stmt = stmt.where(
                or_(
                    Message.created_at < stamp,
                    and_(Message.created_at == stamp, Message.id < message_id),
                )
            )
        rows = (
            await session.execute(
                stmt.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit + 1)
            )
        ).scalars().all()
        has_more = len(rows) > limit
        page = list(rows[:limit])
        page.reverse()
        next_cursor = encode_cursor(page[0].created_at, page[0].id) if has_more and page else None
        items = [
            {
                "id": msg.id,
                "role": msg.role,
                "content": msg.content,
                "tool_calls": msg.tool_calls_json,
                "seq": msg.seq,
                "created_at": msg.created_at.isoformat() if msg.created_at else None,
            }
            for msg in page
        ]
    return {"items": items, "next_cursor": next_cursor, "has_more": has_more}


@app.post("/conversations/{conversation_id}/split")
async def split_conversation(conversation_id: str, principal: PrincipalDep) -> dict[str, Any]:
    """手动拆分：旧会话冻结，子会话只带交接摘要与关键事实继续。"""
    pinned: list[str] = []
    async with get_session_factory()() as session:
        conv = await _owned_conversation(session, conversation_id, principal)
        text, pinned, level = await _summary_text_for(session, conv)
        child, text = _create_split_child(
            session, conv, text, pinned, level, source="split"
        )
        await session.flush()
        child_data = _conv_dict(child)
        child_id = child.id
        await session.commit()
    if text and memory is not None:
        # 立即回灌子会话，避免首次提问时再查一次库。
        await memory.seed_handoff(principal.user_id, child_id, text, pinned)
    return {"conversation": child_data, "handoff": text}


@app.post("/conversations/{conversation_id}/archive")
async def archive_conversation(
    conversation_id: str,
    principal: PrincipalDep,
    keep_recent: int = Query(200, ge=0, le=1000),
) -> dict[str, Any]:
    """把较早消息压缩归档，仅保留最近 keep_recent 条原文参与历史展示。"""
    async with get_session_factory()() as session:
        conv = await _owned_conversation(session, conversation_id, principal)
        archived = await _archive_old_messages(session, conv, keep_recent)
        await session.commit()
    return {"archived": archived, "keep_recent": keep_recent}


@app.get("/conversations/{conversation_id}/archives")
async def list_archives(conversation_id: str, principal: PrincipalDep) -> dict[str, Any]:
    async with get_session_factory()() as session:
        await _owned_conversation(session, conversation_id, principal)
        rows = (
            await session.execute(
                select(MessageArchive)
                .where(MessageArchive.conversation_id == conversation_id)
                .order_by(MessageArchive.created_at.desc())
            )
        ).scalars().all()
        items = [
            {
                "id": row.id,
                "message_count": row.message_count,
                "encoding": row.encoding,
                "first_message_at": row.first_message_at.isoformat()
                if row.first_message_at
                else None,
                "last_message_at": row.last_message_at.isoformat() if row.last_message_at else None,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    return {"items": items}


@app.get("/conversations/{conversation_id}/archives/{archive_id}")
async def get_archive(
    conversation_id: str, archive_id: str, principal: PrincipalDep
) -> dict[str, Any]:
    async with get_session_factory()() as session:
        await _owned_conversation(session, conversation_id, principal)
        row = await session.get(MessageArchive, archive_id)
        if row is None or row.conversation_id != conversation_id:
            raise ApiError("NOT_FOUND", "归档不存在")
        items = decompress_records(row.payload)
    return {"items": items}


@app.get("/usage")
async def usage(
    principal: PrincipalDep,
    group_by: str = Query("day"),
    start: str | None = Query(None, alias="from"),
    end: str | None = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    # 用量聚合涉及跨用户与成本口径，只对运营/管理员开放。
    principal.require_staff()
    if group_by not in {"day", "model", "user"}:
        raise ApiError("BAD_REQUEST", "group_by 必须是 day/model/user 之一")
    started, ended = _parse_dt(start, "from"), _parse_dt(end, "to")
    filters = []
    if started:
        filters.append(UsageRecord.created_at >= started)
    if ended:
        filters.append(UsageRecord.created_at < ended)

    if group_by == "day":
        key = func.date(UsageRecord.created_at)
    elif group_by == "model":
        key = UsageRecord.model
    else:
        key = UsageRecord.user_id

    async with get_session_factory()() as session:
        rows = await session.execute(
            select(
                key.label("key"),
                func.sum(UsageRecord.prompt_tokens).label("prompt_tokens"),
                func.sum(UsageRecord.completion_tokens).label("completion_tokens"),
                func.sum(UsageRecord.total_tokens).label("total_tokens"),
                func.count().label("calls"),
                func.sum(case((UsageRecord.status != "ok", 1), else_=0)).label("errors"),
                func.avg(UsageRecord.latency_ms).label("avg_latency_ms"),
            )
            .where(*filters)
            .group_by(key)
        )
        # p95 无法用 SQL 聚合表达式统一表示（MySQL/SQLite 差异大），
        # 单独取各组的延迟值在 Python 里算分位，量级受时间范围约束。
        latency_rows = await session.execute(
            select(key.label("key"), UsageRecord.latency_ms).where(*filters)
        )
        latencies: dict[Any, list[int]] = {}
        for group_key, latency in latency_rows.all():
            latencies.setdefault(group_key, []).append(int(latency or 0))

    items = []
    for row in rows.all():
        group_key = row.key
        items.append(
            {
                "key": str(group_key),
                "prompt_tokens": int(row.prompt_tokens or 0),
                "completion_tokens": int(row.completion_tokens or 0),
                "total_tokens": int(row.total_tokens or 0),
                "calls": int(row.calls or 0),
                "errors": int(row.errors or 0),
                "avg_latency_ms": int(row.avg_latency_ms or 0),
                "p95_latency_ms": _p95(latencies.get(group_key, [])),
            }
        )
    total = len(items)
    start_idx = (page - 1) * size
    return {"items": items[start_idx : start_idx + size], "page": page, "size": size, "total": total}
