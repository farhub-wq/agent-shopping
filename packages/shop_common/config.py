from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class ServiceSettings(BaseSettings):
    """所有服务共享的配置契约，差异通过环境变量注入。

    本地不带 Docker 运行时，DATABASE_URL 缺省会退化为每服务独立的 SQLite 文件，
    这样 Harness 单测和离线演示都不依赖外部中间件。
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "shop"
    # 生产注入 mysql+aiomysql://...；本地缺省仍退化为 SQLite，单测不依赖中间件
    database_url: str = ""

    redis_url: str = "redis://localhost:6379/0"

    # Harness 的文档型数据（记忆与 Agent 轨迹）存 MongoDB，懒连接，缺省本地地址
    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "shop_harness"

    jwt_secret: str = "dev-only-secret-change-me"
    jwt_algorithm: str = "HS256"
    # access token 只活 15 分钟，长期会话靠 refresh cookie 轮换
    jwt_ttl_seconds: int = 900
    # 运行环境标识：prod 下 Cookie 加 secure、dev 换 token 接口关闭
    env: str = "dev"
    refresh_ttl_seconds: int = 604800
    login_max_failures: int = 5
    login_lock_minutes: int = 15
    # 内网身份头签名密钥与信任开关：仅网关签名后的 X-User-* 才可信，生产必须注入密钥
    internal_signing_secret: str = ""
    trust_identity_headers: bool = False
    identity_url: str = "http://localhost:8007"
    # 本地演示用的自选角色换 token 接口：只在 env=dev 且显式开启时挂载，生产必须关闭
    auth_dev_mode: bool = False
    # 管理员 TOTP 共享密钥的对称加密主密钥（Fernet，32 字节 urlsafe-base64）。
    # 生产必须注入独立密钥，dev 默认值仅保证本地/单测可跑，明文密钥与 JWT_SECRET 同等级对待。
    mfa_encryption_key: str = "0y5-6ym0zsmuIEBUg5OL133pRODLkiRFpQhgbIVZWmU="

    catalog_url: str = "http://localhost:8001"
    inventory_url: str = "http://localhost:8002"
    order_url: str = "http://localhost:8003"
    fulfillment_url: str = "http://localhost:8004"
    aftersales_url: str = "http://localhost:8005"
    harness_url: str = "http://localhost:8006"
    payment_url: str = "http://localhost:8008"
    audit_url: str = "http://localhost:8009"
    # 浏览器可达的网关地址：mock 收银台 pay_url 必须指向这里，而不是容器内地址
    payment_public_base_url: str = "http://localhost:8080"
    gateway_url: str = "http://localhost:8080"

    # 支付渠道：本轮用 mock 沙箱，真实聚合支付只需替换 provider 实现
    payment_provider: str = "mock"
    payment_webhook_secret: str = "dev-payment-webhook-secret"
    payment_ttl_seconds: int = 900
    payment_fee_bps: int = 60
    # 本地 mock 收银台只在 dev 且显式开启时可用，生产必须关闭
    payment_mock_mode: bool = False
    # 后台中继/扫描任务开关：单测关闭，避免测试依赖 Redis 与常驻任务
    background_tasks: bool = True

    # 业务参数
    flash_sale_stock: int = 20
    fulfillment_sla_hours: int = 48
    refund_window_days: int = 7

    # 模型与嵌入
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    embedding_dim: int = 256
    # 嵌入模型：base_url 与 api_key 齐备才启用真实语义向量，否则回退哈希
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    embedding_timeout_seconds: float = 15.0
    # 降级模型：留空则降级到固定话术（CannedProvider），不冒充模型产出
    llm_fallback_base_url: str = ""
    llm_fallback_api_key: str = ""
    llm_fallback_model: str = ""
    # 模型熔断：超时秒数 / 连续失败阈值 / 冷却秒数
    llm_timeout_seconds: float = 15.0
    llm_failure_threshold: int = 3
    llm_cooldown_seconds: int = 300
    # Agent 成本预算：单次运行估算 token 上限 / 单用户每日估算 token 配额
    # （口径为 字符数//3 粗估，是护栏而非计费）
    agent_run_token_budget: int = 20000
    agent_user_daily_token_quota: int = 200000

    # MCP：逗号分隔的 name=endpoint 列表，留空则不接远端 MCP Server
    mcp_http_servers: str = ""

    # 沙箱
    sandbox_enabled: bool = False
    # 独立 sandbox-runner 列表（逗号分隔）：生产按沙箱主机数量列全，harness 做故障转移。
    # 单机默认只指向本地 runner；多主机部署形如 http://runner-a:8000,http://runner-b:8000。
    sandbox_runner_urls: str = "http://localhost:8011"
    # 每个 runner 进程允许同时执行的沙箱容器数；超过即排队，排队超时按繁忙拒绝。
    sandbox_max_concurrency: int = 4
    sandbox_queue_timeout_seconds: int = 10
    # 同一 execution_id 在其他 runner 执行中时，本实例等待结果复用的时长
    sandbox_dedupe_wait_seconds: int = 5
    sandbox_image: str = "shop-agent-sandbox:latest"
    sandbox_cpu: float = 0.5
    sandbox_memory_mb: int = 256
    sandbox_timeout_seconds: int = 20
    sandbox_network: str = "none"
    # 沙箱 runner 连接的 Docker 端点：留空沿用 SDK 默认 socket（仅本地开发）。
    # 生产必须显式指向 rootless dockerd（如 unix:///run/rootless/docker.sock），
    # 指向默认 root socket 会被 runner 启动守卫直接拒绝，避免被攻破后等价宿主 root。
    sandbox_docker_host: str = ""
    # 沙箱容器运行时：生产强制 gVisor（runsc），用用户态内核隔离 syscall，
    # 收敛共享宿主内核的 CVE 面；留空仅用于本地开发，届时回落到 runc。
    sandbox_runtime: str = ""

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///./data/{self.service_name}.db"


# 生产禁止沿用的开发默认值：这些值在仓库里公开可见，命中即等于没有密钥。
DEV_SECRET_DEFAULTS = {
    "jwt_secret": "dev-only-secret-change-me",
    "internal_signing_secret": "dev-internal-signing-secret",
    "payment_webhook_secret": "dev-payment-webhook-secret",
    "mfa_encryption_key": "0y5-6ym0zsmuIEBUg5OL133pRODLkiRFpQhgbIVZWmU=",
}

# 所有服务都会用到的密钥：JWT 校验身份，内网签名校验网关/runner 转发身份头。
COMMON_SECRETS = ("jwt_secret", "internal_signing_secret")

# HS256 / HMAC-SHA256 密钥最短长度：低于 32 字符的密钥可被暴力枚举，失去防伪意义。
MIN_SECRET_LENGTH = 32


def production_security_issues(
    settings: ServiceSettings, *, secrets: tuple[str, ...] = COMMON_SECRETS
) -> list[str]:
    """列出生产环境的安全配置问题；dev 返回空列表，保证本地零配置可启动。

    只校验「是否仍为开发默认值 / 是否过短」，不读取或打印任何密钥内容。
    """
    if settings.env.strip().lower() == "dev":
        return []
    issues: list[str] = []
    for field in secrets:
        value = str(getattr(settings, field) or "").strip()
        if not value:
            issues.append(f"{field.upper()} 未注入")
        elif value == DEV_SECRET_DEFAULTS[field]:
            issues.append(f"{field.upper()} 仍是开发默认值")
        elif len(value) < MIN_SECRET_LENGTH:
            issues.append(f"{field.upper()} 长度不足 {MIN_SECRET_LENGTH} 字符")
    # 这两个开关在代码里都要求 env=dev 才生效，这里再拦一次，
    # 防止部署时把 ENV 写成其它非 dev 值却仍按 dev 语义理解配置。
    if settings.auth_dev_mode:
        issues.append("AUTH_DEV_MODE 必须在生产关闭")
    if settings.payment_mock_mode:
        issues.append("PAYMENT_MOCK_MODE 必须在生产关闭")
    return issues


def validate_production_settings(
    settings: ServiceSettings, *, secrets: tuple[str, ...] = COMMON_SECRETS
) -> None:
    """生产启动守卫：密钥仍是开发默认值或强度不足时直接拒绝启动（fail-fast）。

    信任身份头保持开启是设计要求（gateway 签名后下游才能拿到 staff 身份），
    其安全性由 INTERNAL_SIGNING_SECRET 的强度兜底，而不是靠关闭信任。
    """
    issues = production_security_issues(settings, secrets=secrets)
    if issues:
        raise RuntimeError("生产环境安全配置不合格：" + "；".join(issues))


# 配置是进程级单例：lru_cache 保证环境变量只解析一次，避免各模块持有不同配置快照。
@lru_cache
def get_settings() -> ServiceSettings:
    return ServiceSettings()
