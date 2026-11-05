from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class ServiceSettings(BaseSettings):
    """所有服务共享的配置契约，差异通过环境变量注入。

    本地不带 Docker 运行时，DATABASE_URL 缺省会退化为每服务独立的 SQLite 文件，
    这样 Harness 单测和离线演示都不依赖外部中间件。
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    service_name: str = "shop"
    database_url: str = ""

    redis_url: str = "redis://localhost:6379/0"

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

    catalog_url: str = "http://localhost:8001"
    inventory_url: str = "http://localhost:8002"
    order_url: str = "http://localhost:8003"
    fulfillment_url: str = "http://localhost:8004"
    aftersales_url: str = "http://localhost:8005"
    harness_url: str = "http://localhost:8006"
    gateway_url: str = "http://localhost:8080"

    # 业务参数
    flash_sale_stock: int = 20
    fulfillment_sla_hours: int = 48
    refund_window_days: int = 7

    # 模型与嵌入
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    embedding_dim: int = 256

    # 沙箱
    sandbox_enabled: bool = False
    sandbox_image: str = "shop-agent-sandbox:latest"
    sandbox_cpu: float = 0.5
    sandbox_memory_mb: int = 256
    sandbox_timeout_seconds: int = 20
    sandbox_network: str = "none"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite+aiosqlite:///./data/{self.service_name}.db"


# 配置是进程级单例：lru_cache 保证环境变量只解析一次，避免各模块持有不同配置快照。
@lru_cache
def get_settings() -> ServiceSettings:
    return ServiceSettings()
