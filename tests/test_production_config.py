"""生产配置守卫测试：密钥默认值、身份头信任与开发开关。

这些断言对应 SBX-03：生产环境不允许沿用仓库里公开的开发默认密钥，
而信任身份头本身是设计需要（gateway 签名后下游才能拿到 staff 身份），
安全性由强签名密钥兜底。
"""

import pytest
from shop_common.config import DEV_SECRET_DEFAULTS, ServiceSettings, validate_production_settings

STRONG_SECRETS = {
    "jwt_secret": "j" * 40,
    "internal_signing_secret": "i" * 40,
    "payment_webhook_secret": "p" * 40,
    "mfa_encryption_key": "M" * 44,
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


def test_dev_allows_defaults():
    # 本地开发必须零配置可跑；守卫只在非 dev 生效。
    validate_production_settings(ServiceSettings(_env_file=None, env="dev"))
