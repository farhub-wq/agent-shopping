"""模型提供商领域：预设、SSRF 校验、CRUD、降级链解析与连通测试。

app.py 只做参数解析、鉴权与响应组装，这里承载「谁能看到/修改哪些 provider、
按什么顺序选 provider、怎么测连通」这些业务规则。api_key 的加解密统一走
.crypto，本模块只负责编排，绝不把明文 key 放进响应或日志。
"""

import ipaddress
import socket
import time
import uuid
from urllib.parse import urlparse

import httpx
from shop_common.errors import ApiError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ModelProvider
from .crypto import encrypt_model_key, mask_key

# 内置厂商预设：前端下拉的公开元数据，用户只需补 key 与模型名。
# anthropic 与 openai_compatible 的 base_url 统一带版本段，调用方直接拼 /messages
# 或 /chat/completions，不再各自脑补版本前缀。
PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {
        "type": "openai_compatible",
        "name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "key_label": "DeepSeek API Key",
    },
    "glm": {
        "type": "openai_compatible",
        "name": "智谱 GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4-flash",
        "key_label": "智谱 API Key",
    },
    "openai-codex": {
        "type": "openai_compatible",
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
        "key_label": "OpenAI API Key",
    },
    "claude": {
        "type": "anthropic",
        "name": "Anthropic Claude",
        "base_url": "https://api.anthropic.com/v1",
        "default_model": "claude-3-5-haiku-latest",
        "key_label": "Anthropic API Key",
    },
    "chatanywhere": {
        "type": "openai_compatible",
        "name": "默认免费 (ChatAnywhere)",
        "base_url": "https://api.chatanywhere.tech/v1",
        "default_model": "gpt-4o-mini",
        "key_label": "ChatAnywhere API Key",
    },
    "custom": {
        "type": "openai_compatible",
        "name": "自定义 OpenAI 兼容",
        "base_url": "",
        "default_model": "",
        "key_label": "API Key",
    },
}

# 服务启动时幂等写入的默认免费 AI（对齐 Agent-Harness 的 ChatAnywhere 网关）。
# key 由管理员在设置页填一次；为空时解析链最终回退离线确定性规划器。
DEFAULT_FREE_AI = {
    "provider_type": "openai_compatible",
    "name": "默认免费",
    "base_url": "https://api.chatanywhere.tech/v1",
    "model": "gpt-4o-mini",
}

_VALID_TYPES = {"openai_compatible", "anthropic"}

# SSRF 黑名单：内网/回环/链路本地地址一律拒绝，防用户自填 base_url 探测内网。
_PRIVATE_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def validate_base_url(base_url: str) -> str:
    """校验并规范化 base_url：只允许 http(s)，主机不得落在内网地址段。"""
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https"):
        raise ApiError("BAD_REQUEST", "base_url 只支持 http/https")
    host = parsed.hostname
    if not host:
        raise ApiError("BAD_REQUEST", "base_url 缺少主机名")
    try:
        addrs = {ipaddress.ip_address(host)}
    except ValueError:
        # 主机名：解析出所有地址，任一命中内网即拒绝，防 DNS 重绑定绕过。
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError as exc:
            raise ApiError("BAD_REQUEST", f"base_url 主机无法解析：{exc}") from exc
        addrs = {ipaddress.ip_address(info[4][0]) for info in infos}
    for addr in addrs:
        if any(addr in net for net in _PRIVATE_NETS):
            raise ApiError("BAD_REQUEST", "base_url 指向内网地址，已拒绝")
    return base_url.rstrip("/")


def to_dict(record: ModelProvider) -> dict:
    """provider 响应投影：api_key 永不回传，只给 has_key 与尾 4 位掩码。"""
    return {
        "id": record.id,
        "owner_user_id": record.owner_user_id,
        "tenant_id": record.tenant_id,
        "provider_type": record.provider_type,
        "name": record.name,
        "base_url": record.base_url,
        "model": record.model,
        "is_default": record.is_default,
        "enabled": record.enabled,
        "has_key": bool(record.api_key_enc),
        "key_masked": mask_key(record.api_key_enc),
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "updated_at": record.updated_at.isoformat() if record.updated_at else None,
    }


def _visible_filter(user_id: str, tenant_id: str, is_staff: bool):
    # 非 staff 只能看到「同租户的全局共享 + 本人私有」；staff 看本租户全部。
    if is_staff:
        return ModelProvider.tenant_id == tenant_id
    return (ModelProvider.tenant_id == tenant_id) & (
        (ModelProvider.owner_user_id.is_(None)) | (ModelProvider.owner_user_id == user_id)
    )


async def list_visible_providers(
    session: AsyncSession, *, user_id: str, tenant_id: str, is_staff: bool
) -> list[ModelProvider]:
    result = await session.execute(
        select(ModelProvider)
        .where(_visible_filter(user_id, tenant_id, is_staff))
        .order_by(ModelProvider.created_at)
    )
    return list(result.scalars().all())


async def resolve_provider(
    session: AsyncSession, *, user_id: str, tenant_id: str, provider_id: str | None = None
) -> ModelProvider | None:
    """解析降级链：显式 provider_id → 用户私有最近更新 → 系统默认免费 → 空。

    被选 provider 必须 enabled 且对该用户可见（同租户的共享或本人私有），
    否则沿链下探；最终返回空由调用方回退离线确定性规划器。
    """
    if provider_id:
        result = await session.execute(
            select(ModelProvider).where(
                ModelProvider.id == provider_id,
                ModelProvider.tenant_id == tenant_id,
                ModelProvider.enabled.is_(True),
                (ModelProvider.owner_user_id.is_(None)) | (ModelProvider.owner_user_id == user_id),
            )
        )
        record = result.scalar_one_or_none()
        if record is not None:
            return record

    result = await session.execute(
        select(ModelProvider)
        .where(
            ModelProvider.tenant_id == tenant_id,
            ModelProvider.owner_user_id == user_id,
            ModelProvider.enabled.is_(True),
        )
        .order_by(ModelProvider.updated_at.desc())
        .limit(1)
    )
    record = result.scalar_one_or_none()
    if record is not None:
        return record

    result = await session.execute(
        select(ModelProvider)
        .where(
            ModelProvider.tenant_id == tenant_id,
            ModelProvider.owner_user_id.is_(None),
            ModelProvider.is_default.is_(True),
            ModelProvider.enabled.is_(True),
        )
        .order_by(ModelProvider.created_at)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def create_provider(
    session: AsyncSession,
    *,
    owner_user_id: str | None,
    tenant_id: str,
    provider_type: str,
    name: str,
    base_url: str,
    model: str,
    api_key: str,
    is_default: bool = False,
) -> ModelProvider:
    if provider_type not in _VALID_TYPES:
        raise ApiError("BAD_REQUEST", f"provider_type 必须是 {sorted(_VALID_TYPES)} 之一")
    if not name.strip():
        raise ApiError("BAD_REQUEST", "name 不能为空")
    if not model.strip():
        raise ApiError("BAD_REQUEST", "model 不能为空")
    base_url = validate_base_url(base_url)
    record = ModelProvider(
        id=uuid.uuid4().hex,
        owner_user_id=owner_user_id,
        tenant_id=tenant_id,
        provider_type=provider_type,
        name=name.strip(),
        base_url=base_url,
        model=model.strip(),
        api_key_enc=encrypt_model_key(api_key) if api_key else "",
        is_default=is_default,
        enabled=True,
    )
    session.add(record)
    await session.commit()
    return record


async def update_provider(
    session: AsyncSession,
    record: ModelProvider,
    *,
    name: str | None,
    model: str | None,
    base_url: str | None,
    api_key: str | None,
    is_default: bool | None,
    enabled: bool | None,
) -> ModelProvider:
    # 只更新显式传入的字段；api_key 为空串表示「不更新 key」，None 表示本次请求未携带。
    if name is not None:
        if not name.strip():
            raise ApiError("BAD_REQUEST", "name 不能为空")
        record.name = name.strip()
    if model is not None:
        if not model.strip():
            raise ApiError("BAD_REQUEST", "model 不能为空")
        record.model = model.strip()
    if base_url is not None:
        record.base_url = validate_base_url(base_url)
    if api_key:
        record.api_key_enc = encrypt_model_key(api_key)
    if is_default is not None:
        record.is_default = is_default
    if enabled is not None:
        record.enabled = enabled
    await session.commit()
    return record


async def delete_provider(session: AsyncSession, record: ModelProvider) -> None:
    # 系统默认免费 AI 记录不允许删除，否则重启后幂等逻辑会再塞一条，语义上它属于系统配置。
    if record.is_default and record.owner_user_id is None:
        raise ApiError("BAD_REQUEST", "系统默认免费 AI 不可删除，可改为停用")
    await session.delete(record)
    await session.commit()


async def ensure_default_free_ai(session: AsyncSession) -> None:
    """启动时幂等写入默认免费 AI：表里已有任意记录就不再插入，避免重复启动产生第二条。"""
    existing = await session.execute(select(ModelProvider).limit(1))
    if existing.scalar_one_or_none() is not None:
        return
    session.add(
        ModelProvider(
            id=uuid.uuid4().hex,
            owner_user_id=None,
            tenant_id="default",
            provider_type=DEFAULT_FREE_AI["provider_type"],
            name=DEFAULT_FREE_AI["name"],
            base_url=DEFAULT_FREE_AI["base_url"],
            model=DEFAULT_FREE_AI["model"],
            api_key_enc="",
            is_default=True,
            enabled=True,
        )
    )
    await session.commit()


async def test_provider(
    base_url: str, api_key: str, model: str, provider_type: str, timeout: float = 10.0
) -> tuple[bool, int, str]:
    """发一次最小请求测连通与延迟；错误信息脱敏，绝不携带 key 或响应体原文。"""
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if provider_type == "anthropic":
                response = await client.post(
                    f"{base_url}/messages",
                    json={"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]},
                    headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
                )
            else:
                response = await client.post(
                    f"{base_url}/chat/completions",
                    json={"model": model, "max_tokens": 1, "messages": [{"role": "user", "content": "ping"}]},
                    headers={"Authorization": f"Bearer {api_key}"},
                )
    except httpx.TimeoutException:
        return False, int((time.perf_counter() - started) * 1000), "连接超时"
    except httpx.HTTPError as exc:
        return False, int((time.perf_counter() - started) * 1000), f"连接失败：{exc.__class__.__name__}"
    latency_ms = int((time.perf_counter() - started) * 1000)
    if response.status_code >= 400:
        # 厂商错误体常含认证细节，只回状态码，让用户自行核对 key 与模型名。
        return False, latency_ms, f"HTTP {response.status_code}"
    return True, latency_ms, "ok"
