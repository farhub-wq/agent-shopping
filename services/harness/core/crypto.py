"""模型提供商 API Key 的对称加密。

用户自填的各厂商密钥不能明文落库：用 Fernet（主密钥来自环境变量
MODEL_KEY_ENCRYPTION_KEY）加密后存储，前端只回显尾 4 位，永不回传明文。
与 identity/security.py 的 TOTP 加密同一模式，但使用独立主密钥——
模型密钥泄露不影响管理员 TOTP，反之亦然。
"""

from cryptography.fernet import Fernet, InvalidToken
from shop_common.config import get_settings


def _fernet() -> Fernet:
    return Fernet(get_settings().model_key_encryption_key.encode())


def encrypt_model_key(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_model_key(token: str) -> str:
    # 主密钥变更或密文损坏时抛 InvalidToken，由调用方转成可理解的 ApiError，不静默吞错。
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("模型密钥无法解密，请确认 MODEL_KEY_ENCRYPTION_KEY 未变更") from exc


def mask_key(plain: str) -> str:
    """展示用掩码：只留尾 4 位，前面用占位符，避免整串密钥出现在任何响应或日志里。"""
    if not plain:
        return ""
    if len(plain) <= 4:
        return "****"
    return f"****{plain[-4:]}"
