import hashlib
import hmac
import time
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import uuid4

import jwt
from fastapi import Depends, Header

from .config import get_settings
from .errors import ApiError

settings = get_settings()

# iss/aud 固定：令牌只允许在本系统签发、只允许给 web 前端用，跨系统令牌直接拒收
TOKEN_ISSUER = "shop-agent"
TOKEN_AUDIENCE = "shop-web"


def create_token(*, user_id: str, role: str = "customer", tenant: str = "default") -> str:
    now = datetime.now(UTC)
    # 令牌无状态：tenant 供多租户隔离，exp 由配置统一控制有效期，签发时不查库。
    payload = {
        "sub": user_id,
        "role": role,
        "tenant": tenant,
        "jti": uuid4().hex,
        "iss": TOKEN_ISSUER,
        "aud": TOKEN_AUDIENCE,
        "iat": now,
        "exp": now + timedelta(seconds=settings.jwt_ttl_seconds),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            issuer=TOKEN_ISSUER,
            audience=TOKEN_AUDIENCE,
        )
    except jwt.PyJWTError as exc:
        raise ApiError("UNAUTHORIZED", "登录凭证无效或已过期") from exc


def create_internal_signature(*, user_id: str, role: str, tenant: str, ts: str, request_id: str, secret: str) -> str:
    """网关给下游的签名：把身份五元组用 HMAC-SHA256 摘要，防内网身份头被篡改。"""
    msg = f"{user_id}|{role}|{tenant}|{ts}|{request_id}"
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


def signed_identity_headers(
    user_id: str, role: str, tenant: str = "default", *, secret: str | None = None
) -> dict[str, str]:
    """服务间调用使用的身份头：现签时间戳 + 请求号，下游按签名校验身份。

    服务间调用不能再手写 X-User-* 头，否则开启 TRUST_IDENTITY_HEADERS 后会被判无签名。
    `secret` 允许调用方改用专用密钥（例如 runner 的独立密钥），缺省仍走内网签名密钥。
    """
    ts = str(int(time.time()))
    request_id = uuid4().hex
    key = settings.internal_signing_secret if secret is None else secret
    signature = create_internal_signature(
        user_id=user_id,
        role=role,
        tenant=tenant,
        ts=ts,
        request_id=request_id,
        secret=key,
    )
    return {
        "X-User-Id": user_id,
        "X-User-Role": role,
        "X-Tenant": tenant,
        "X-Request-Id": request_id,
        "X-Internal-Signature": f"{ts}.{signature}",
    }


def verify_internal_signature(
    signature: str,
    *,
    user_id: str,
    role: str,
    tenant: str,
    request_id: str,
    secret: str,
    max_skew_seconds: int = 30,
) -> bool:
    """校验 `ts.hmac` 形式的签名；超出时间窗或摘要不一致都判无效。"""
    try:
        ts, sig = signature.split(".", 1)
        skew = abs(time.time() - int(ts))
    except ValueError:
        return False
    if skew > max_skew_seconds:
        return False
    expected = create_internal_signature(user_id=user_id, role=role, tenant=tenant, ts=ts, request_id=request_id, secret=secret)
    return hmac.compare_digest(sig, expected)


class Principal:
    def __init__(self, user_id: str, role: str, tenant: str):
        self.user_id = user_id
        self.role = role
        self.tenant = tenant

    def is_staff(self) -> bool:
        # 只有运营与管理员可执行后台写操作，其余角色一律按顾客权限处理。
        return self.role in {"operator", "admin"}

    def require_staff(self) -> None:
        if not self.is_staff():
            raise ApiError("FORBIDDEN", "该操作仅限运营人员")

    def require_admin(self) -> None:
        # 审计等敏感数据只对管理员开放，运营也不能越级读取
        if self.role != "admin":
            raise ApiError("FORBIDDEN", "该操作仅限管理员")


def principal_from_signed_headers(
    *,
    user_id: str | None,
    role: str | None,
    tenant: str | None,
    signature: str | None,
    request_id: str | None,
    secret: str,
    trusted: bool,
) -> Principal:
    """只按签名身份头解析主体：供 runner 等不接收 Bearer JWT 的服务复用。"""
    if not user_id:
        return Principal("guest", "customer", "default")
    if not trusted:
        # 未开启信任时身份头一律不生效，降级为访客而不是报错，保证直连调试仍可用
        return Principal("guest", "customer", "default")
    if not signature or not request_id:
        raise ApiError("UNAUTHORIZED", "身份头缺少签名")
    if not verify_internal_signature(
        signature,
        user_id=user_id,
        role=role or "customer",
        tenant=tenant or "default",
        request_id=request_id,
        secret=secret,
    ):
        raise ApiError("UNAUTHORIZED", "身份头签名无效或已过期")
    return Principal(user_id, role or "customer", tenant or "default")


async def get_principal(
    authorization: Annotated[str | None, Header()] = None,
    x_user_id: Annotated[str | None, Header()] = None,
    x_user_role: Annotated[str | None, Header()] = None,
    x_tenant: Annotated[str | None, Header()] = None,
    x_internal_signature: Annotated[str | None, Header()] = None,
    x_request_id: Annotated[str | None, Header()] = None,
) -> Principal:
    """鉴权顺序：Bearer JWT → 网关签名身份头 → 访客。

    身份头只在 trust_identity_headers 开启且签名有效时才被信任；
    生产环境若关闭直连访问，客户端伪造的 X-User-* 一律被忽略。
    """
    if authorization and authorization.startswith("Bearer "):
        claims = decode_token(authorization[7:])
        return Principal(str(claims.get("sub")), str(claims.get("role", "customer")), str(claims.get("tenant", "default")))
    return principal_from_signed_headers(
        user_id=x_user_id,
        role=x_user_role,
        tenant=x_tenant,
        signature=x_internal_signature,
        request_id=x_request_id,
        secret=settings.internal_signing_secret,
        trusted=settings.trust_identity_headers,
    )


PrincipalDep = Annotated[Principal, Depends(get_principal)]
