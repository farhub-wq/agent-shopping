"""生产配置守卫测试：密钥默认值、身份头信任与开发开关。

这些断言对应 SBX-03：生产环境不允许沿用仓库里公开的开发默认密钥，
而信任身份头本身是设计需要（gateway 签名后下游才能拿到 staff 身份），
安全性由强签名密钥兜底。
"""

import pytest
from shop_common.config import (
    COMMON_SECRETS,
    DEV_SECRET_DEFAULTS,
    RUNNER_SECRETS,
    ServiceSettings,
    validate_production_settings,
)

STRONG_SECRETS = {
    "jwt_secret": "j" * 40,
    "internal_signing_secret": "i" * 40,
    "payment_webhook_secret": "p" * 40,
    "mfa_encryption_key": "M" * 44,
    # 业务 Redis 认证密码：生产禁止匿名 default 用户
    "redis_password": "r" * 32,
}


def _settings(**overrides) -> ServiceSettings:
    # _env_file=None：不受仓库根目录 .env 影响，避免本机已注入强密钥导致用例失真。
    values = {"env": "prod", **STRONG_SECRETS}
    values.update(overrides)
    return ServiceSettings(_env_file=None, **values)


def test_prod_rejects_dev_default_secrets():
    with pytest.raises(RuntimeError) as exc:
        validate_production_settings(ServiceSettings(_env_file=None, env="prod"))
    # 报错必须点名具体字段，运维才知道该换哪个密钥。
    assert "JWT_SECRET" in str(exc.value)
    assert "INTERNAL_SIGNING_SECRET" in str(exc.value)


def test_prod_rejects_short_secret():
    with pytest.raises(RuntimeError):
        validate_production_settings(_settings(jwt_secret="short"))


def test_prod_accepts_strong_secrets_with_trusted_headers():
    validate_production_settings(_settings(trust_identity_headers=True))


def test_prod_rejects_dev_switches():
    with pytest.raises(RuntimeError):
        validate_production_settings(_settings(auth_dev_mode=True))
    with pytest.raises(RuntimeError):
        validate_production_settings(_settings(payment_mock_mode=True))


def test_mfa_key_only_checked_by_identity():
    # MFA 主密钥只有 identity 使用，普通服务不应因它而启动失败。
    dev_mfa = DEV_SECRET_DEFAULTS["mfa_encryption_key"]
    validate_production_settings(_settings(mfa_encryption_key=dev_mfa))
    with pytest.raises(RuntimeError):
        validate_production_settings(
            _settings(mfa_encryption_key=dev_mfa),
            secrets=("jwt_secret", "internal_signing_secret", "mfa_encryption_key"),
        )


def test_payment_webhook_secret_only_checked_by_payment():
    validate_production_settings(_settings(payment_webhook_secret="dev-payment-webhook-secret"))
    with pytest.raises(RuntimeError):
        validate_production_settings(
            _settings(payment_webhook_secret="dev-payment-webhook-secret"),
            secrets=("jwt_secret", "internal_signing_secret", "payment_webhook_secret"),
        )


def test_harness_requires_dedicated_runner_secret():
    # harness 要驱动 runner，必须持有 runner 专用密钥；缺省时不能在 prod 静默回落到内网密钥。
    with pytest.raises(RuntimeError):
        validate_production_settings(_settings(), secrets=(*COMMON_SECRETS, *RUNNER_SECRETS))
    validate_production_settings(
        _settings(runner_signing_secret="r" * 40),
        secrets=(*COMMON_SECRETS, *RUNNER_SECRETS),
    )


def test_dev_allows_defaults():
    # 本地开发必须零配置可跑；守卫只在非 dev 生效。
    validate_production_settings(ServiceSettings(_env_file=None, env="dev"))


def test_prod_rejects_anonymous_redis():
    # 生产不能留匿名 default 用户：否则 runner 可绕过受限 ACL 直接全库读写。
    with pytest.raises(RuntimeError) as exc:
        validate_production_settings(
            _settings(redis_password="", redis_url="redis://redis:6379/0")
        )
    assert "REDIS" in str(exc.value)

    # URL 自带凭据（托管 Redis 专用用户）同样达标
    validate_production_settings(
        _settings(
            redis_password="",
            redis_url="redis://default:" + "r" * 32 + "@redis:6379/0",
        )
    )


def test_prod_rejects_whitespace_redis_password():
    # ACL 文件按空白分隔字段，密码含空白会让 Redis 无法加载受限用户。
    with pytest.raises(RuntimeError):
        validate_production_settings(_settings(redis_password="has space"))


def test_prod_rejects_default_redis_password():
    # compose 的本地默认密码公开可见，生产沿用等于没有认证。
    with pytest.raises(RuntimeError) as exc:
        validate_production_settings(
            _settings(redis_password="dev-redis-password")
        )
    assert "默认值" in str(exc.value)


def test_resolved_redis_url_injects_password():
    settings = ServiceSettings(
        _env_file=None,
        env="dev",
        redis_url="redis://redis:6379/0",
        redis_password="p@ss word!",
    )
    # 密码必须编码后再拼进 URL，否则 @ / 空格会破坏 host 解析
    assert settings.resolved_redis_url == "redis://default:p%40ss%20word%21@redis:6379/0"

    # URL 已带凭据时原样保留，不再叠加 REDIS_PASSWORD
    existing = ServiceSettings(
        _env_file=None,
        env="dev",
        redis_url="redis://svc:secret@redis:6379/0",
        redis_password="ignored",
    )
    assert existing.resolved_redis_url == "redis://svc:secret@redis:6379/0"
