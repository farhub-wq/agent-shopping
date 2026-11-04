from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import jwt
from fastapi import Depends, Header

from .config import get_settings
from .errors import ApiError

settings = get_settings()


def create_token(*, user_id: str, role: str = "customer", tenant: str = "default") -> str:
    now = datetime.now(UTC)
    # 令牌无状态：tenant 供多租户隔离，exp 由配置统一控制有效期，签发时不查库。
    payload = {
        "sub": user_id,
        "role": role,
        "tenant": tenant,
        "iat": now,
        "exp": now + timedelta(seconds=settings.jwt_ttl_seconds),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError as exc:
        raise ApiError("UNAUTHORIZED", "登录凭证无效或已过期") from exc


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


async def get_principal(
    authorization: Annotated[str | None, Header()] = None,
    x_user_id: Annotated[str | None, Header()] = None,
    x_user_role: Annotated[str | None, Header()] = None,
    x_tenant: Annotated[str | None, Header()] = None,
) -> Principal:
    """网关已完成鉴权并透传身份头；直连服务时回退到 JWT 或本地访客身份。

    生产环境应关闭直连访问，只允许网关内网调用并配合 mTLS。
    """
    if authorization and authorization.startswith("Bearer "):
        claims = decode_token(authorization[7:])
        return Principal(str(claims.get("sub")), str(claims.get("role", "customer")), str(claims.get("tenant", "default")))
    if x_user_id:
        return Principal(x_user_id, x_user_role or "customer", x_tenant or "default")
    return Principal("guest", "customer", "default")


PrincipalDep = Annotated[Principal, Depends(get_principal)]
