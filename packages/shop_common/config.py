from functools import lru_cache
from urllib.parse import quote, unquote, urlparse, urlsplit, urlunsplit

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
    # 业务 Redis 认证密码：URL 未自带凭据时由它注入 default 用户，
    # 避免各服务继续用匿名连接，runner 也就无法绕过受限 ACL 走默认用户。
    redis_password: str = ""

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
    # runner 专属签名密钥：与 INTERNAL_SIGNING_SECRET 分开，gateway/业务密钥泄露
    # 也不等于能直接调用 runner 执行代码；留空时仅在 dev 回落内网密钥，生产守卫强制独立注入。
    runner_signing_secret: str = ""
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
    # runner 专用 Redis 连接：生产必须使用受限 ACL 用户（只允许 sandbox:exec:* 与审计流），
    # 不复用业务共享 REDIS_URL；留空仅在 dev 回落，生产守卫直接拒绝启动。
    sandbox_redis_url: str = ""
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

    @property
    def resolved_redis_url(self) -> str:
        """业务 Redis 连接串：URL 未带密码时注入 REDIS_PASSWORD，禁止匿名 default 用户。

        密码集中到一个环境变量，URL 仍可独立指定 host/db；已自带凭据的 URL（托管 Redis
        专用用户）原样保留，不做二次拼装。
        """
        if not self.redis_password:
            return self.redis_url
        parsed = urlsplit(self.redis_url)
        if parsed.password:
            return self.redis_url
        host = parsed.hostname or "localhost"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
        user = quote(parsed.username, safe="") if parsed.username else "default"
        netloc = f"{user}:{quote(self.redis_password, safe='')}@{host}{port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))

    @property
    def resolved_runner_signing_secret(self) -> str:
        # dev 允许复用内网密钥保持零配置；非 dev 由 runner_security_issues 强制独立注入。
        return self.runner_signing_secret or self.internal_signing_secret

    @property
    def resolved_sandbox_redis_url(self) -> str:
        # 生产必须提供带 ACL 凭据的独立 URL，回落共享地址只用于本地开发。
        return self.sandbox_redis_url or self.resolved_redis_url


# 生产禁止沿用的开发默认值：这些值在仓库里公开可见，命中即等于没有密钥。
DEV_SECRET_DEFAULTS = {
    "jwt_secret": "dev-only-secret-change-me",
    "internal_signing_secret": "dev-internal-signing-secret",
    "runner_signing_secret": "dev-runner-signing-secret",
    "payment_webhook_secret": "dev-payment-webhook-secret",
    "mfa_encryption_key": "0y5-6ym0zsmuIEBUg5OL133pRODLkiRFpQhgbIVZWmU=",
    # 业务 Redis 的 default 用户密码，生产必须替换
    "redis_password": "dev-redis-password",
}

# runner 的 ACL 用户密码默认值：只用于 compose 本地启动，生产必须替换。
DEV_SANDBOX_REDIS_PASSWORD = "dev-sandbox-redis-password"

# 所有服务都会用到的密钥：JWT 校验身份，内网签名校验网关/runner 转发身份头。
COMMON_SECRETS = ("jwt_secret", "internal_signing_secret")

# runner 只持有自己的签名密钥：不再接收 JWT 或网关内网密钥，缩小被攻破后的密钥半径。
RUNNER_SECRETS = ("runner_signing_secret",)

# HS256 / HMAC-SHA256 密钥最短长度：低于 32 字符的密钥可被暴力枚举，失去防伪意义。
MIN_SECRET_LENGTH = 32


def production_security_issues(
    settings: ServiceSettings,
    *,
    secrets: tuple[str, ...] = COMMON_SECRETS,
    require_redis_auth: bool = True,
) -> list[str]:
    """列出生产环境的安全配置问题；dev 返回空列表，保证本地零配置可启动。

    只校验「是否仍为开发默认值 / 是否过短」，不读取或打印任何密钥内容。
    `require_redis_auth=False` 仅供 runner 使用：它不持有业务 Redis，改由
    `runner_security_issues` 校验受限 ACL 连接。
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
    if require_redis_auth:
        # 匿名 default 用户等于全库可写；URL 自带凭据或注入 REDIS_PASSWORD 都算达标。
        parsed = urlparse(settings.resolved_redis_url.strip())
        password = unquote(parsed.password) if parsed.password else ""
        if not password:
            issues.append("REDIS_URL 必须包含认证密码，或注入 REDIS_PASSWORD：禁止匿名 default 用户")
        elif any(ch.isspace() for ch in password):
            issues.append("Redis 密码不能包含空白字符（ACL 文件按空白分隔字段）")
        elif password == DEV_SECRET_DEFAULTS["redis_password"]:
            issues.append("Redis 密码仍是开发默认值")
        elif len(password) < MIN_SECRET_LENGTH:
            issues.append(f"Redis 密码长度不足 {MIN_SECRET_LENGTH} 字符")
    # 这两个开关在代码里都要求 env=dev 才生效，这里再拦一次，
    # 防止部署时把 ENV 写成其它非 dev 值却仍按 dev 语义理解配置。
    if settings.auth_dev_mode:
        issues.append("AUTH_DEV_MODE 必须在生产关闭")
    if settings.payment_mock_mode:
        issues.append("PAYMENT_MOCK_MODE 必须在生产关闭")
    return issues


def validate_production_settings(
    settings: ServiceSettings,
    *,
    secrets: tuple[str, ...] = COMMON_SECRETS,
    require_redis_auth: bool = True,
) -> None:
    """生产启动守卫：密钥仍是开发默认值或强度不足时直接拒绝启动（fail-fast）。

    信任身份头保持开启是设计要求（gateway 签名后下游才能拿到 staff 身份），
    其安全性由 INTERNAL_SIGNING_SECRET 的强度兜底，而不是靠关闭信任。
    """
    issues = production_security_issues(
        settings, secrets=secrets, require_redis_auth=require_redis_auth
    )
    if issues:
        raise RuntimeError("生产环境安全配置不合格：" + "；".join(issues))


def runner_security_issues(settings: ServiceSettings) -> list[str]:
    """runner 专属安全守卫：独立签名密钥 + 受限 Redis ACL 凭据。

    runner 不再接收 JWT 与 INTERNAL_SIGNING_SECRET，所以只校验它实际持有的密钥；
    另要求 Redis 连接使用带密码的非 default 用户，避免 runner 被攻破后直接读写业务数据。
    """
    issues = production_security_issues(
        settings, secrets=RUNNER_SECRETS, require_redis_auth=False
    )
    if settings.env.strip().lower() == "dev":
        return issues
    if not settings.trust_identity_headers:
        issues.append("TRUST_IDENTITY_HEADERS 必须开启：runner 只接受签名身份头")
    if settings.runner_signing_secret.strip() and (
        settings.runner_signing_secret.strip() == settings.internal_signing_secret.strip()
    ):
        issues.append("RUNNER_SIGNING_SECRET 必须与 INTERNAL_SIGNING_SECRET 不同")
    redis_url = settings.resolved_sandbox_redis_url.strip()
    parsed = urlparse(redis_url)
    if not parsed.username or not parsed.password:
        issues.append("SANDBOX_REDIS_URL 必须使用带密码的受限 ACL 用户")
    elif parsed.username == "default":
        issues.append("SANDBOX_REDIS_URL 不能使用 default 用户")
    elif any(ch.isspace() for ch in unquote(parsed.password)):
        # ACL 文件按空白分隔字段，密码含空白会让生成的 ACL 行解析失败。
        issues.append("SANDBOX_REDIS_URL 密码不能包含空白字符")
    elif unquote(parsed.password) == DEV_SANDBOX_REDIS_PASSWORD:
        issues.append("SANDBOX_REDIS_PASSWORD 仍是开发默认值")
    elif len(unquote(parsed.password)) < MIN_SECRET_LENGTH:
        issues.append(f"SANDBOX_REDIS_PASSWORD 长度不足 {MIN_SECRET_LENGTH} 字符")
    if redis_url == settings.resolved_redis_url.strip():
        issues.append("SANDBOX_REDIS_URL 不能复用业务共享 REDIS_URL")
    return issues


def validate_runner_settings(settings: ServiceSettings) -> None:
    """runner 生产启动守卫：密钥半径未收敛时宁可起不来，也不接受共享凭据。"""
    issues = runner_security_issues(settings)
    if issues:
        raise RuntimeError("sandbox-runner 安全配置不合格：" + "；".join(issues))


# 配置是进程级单例：lru_cache 保证环境变量只解析一次，避免各模块持有不同配置快照。
@lru_cache
def get_settings() -> ServiceSettings:
    return ServiceSettings()
