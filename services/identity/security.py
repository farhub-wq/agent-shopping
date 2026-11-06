"""口令散列与 TOTP 密钥加密。

- 口令：Argon2id，参数按 OWASP 推荐基线（64MB 内存、3 轮、4 并行）。
- TOTP 共享密钥：Fernet 对称加密落库，主密钥来自环境变量 MFA_ENCRYPTION_KEY。
"""

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken
from shop_common.config import get_settings

_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)


def _fernet() -> Fernet:
    return Fernet(get_settings().mfa_encryption_key.encode())


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    # 密钥不匹配或密文损坏时抛 InvalidToken，由调用方转成可理解的 ApiError，不静默吞错
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("MFA 密钥无法解密，请确认 MFA_ENCRYPTION_KEY 未变更") from exc


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    # 哈希串损坏与密码不匹配都按校验失败处理，不向调用方区分细节
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError):
        return False
