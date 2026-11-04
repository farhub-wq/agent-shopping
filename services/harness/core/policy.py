"""信任等级决定运行时权限上限，技能作者无法在 manifest 里自我提权。"""

from dataclasses import dataclass, field

from shop_common.errors import ApiError

TIERS: dict[str, dict] = {
    "community": {"level": 1, "exec": False, "net": False, "fs": "skill-read", "data": set(), "irreversible": False},
    "reviewed": {"level": 2, "exec": False, "net": False, "fs": "skill-write", "data": set(), "irreversible": False},
    "vendor": {"level": 3, "exec": True, "net": True, "fs": "skill-write", "data": {"pii"}, "irreversible": True},
    "certified": {
        "level": 4,
        "exec": True,
        "net": True,
        "fs": "declared-write",
        "data": {"pii", "secrets"},
        "irreversible": True,
    },
}


@dataclass(frozen=True)
class ToolPolicy:
    name: str
    permission: str = "read"
    risk: str = "low"
    data_class: str = "public"
    requires_approval: bool = False


@dataclass
class Capabilities:
    level: int
    exec: bool
    net: bool
    fs: str
    irreversible: bool = False
    data: set[str] = field(default_factory=set)


def capabilities(trust: str) -> Capabilities:
    tier = TIERS.get(trust)
    if tier is None:
        raise ApiError("FORBIDDEN", f"未知信任等级 {trust}")
    return Capabilities(
        level=tier["level"],
        exec=tier["exec"],
        net=tier["net"],
        fs=tier["fs"],
        data=set(tier["data"]),
        irreversible=tier["irreversible"],
    )


PERMISSION_LEVEL = {"read": 1, "propose": 2, "write": 3, "execute": 3, "admin": 4}


def enforce(trust: str, policy: ToolPolicy) -> None:
    caps = capabilities(trust)
    need = PERMISSION_LEVEL.get(policy.permission, 99)
    if need > caps.level:
        raise ApiError(
            "FORBIDDEN",
            f"信任等级 {trust} 无权调用 {policy.name}（需要 {policy.permission}）",
            details={"trust": trust, "tool": policy.name, "required": policy.permission},
        )
    if policy.permission == "execute" and not caps.exec:
        raise ApiError("FORBIDDEN", f"信任等级 {trust} 不授予脚本执行权")
    if policy.data_class in {"pii", "secrets"} and policy.data_class not in caps.data:
        raise ApiError("FORBIDDEN", f"信任等级 {trust} 不允许访问 {policy.data_class} 数据")
    if policy.risk == "irreversible" and not caps.irreversible:
        raise ApiError("FORBIDDEN", f"信任等级 {trust} 不允许不可逆操作")


def summarize() -> list[dict]:
    return [
        {
            "trust": name,
            "level": caps["level"],
            "exec": caps["exec"],
            "net": caps["net"],
            "fs": caps["fs"],
            "data": sorted(caps["data"]),
            "irreversible": caps["irreversible"],
        }
        for name, caps in TIERS.items()
    ]
