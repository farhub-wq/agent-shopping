"""口令散列：Argon2id，参数按 OWASP 推荐基线（64MB 内存、3 轮、4 并行）。"""

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError, VerifyMismatchError

_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    # 哈希串损坏与密码不匹配都按校验失败处理，不向调用方区分细节
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError):
        return False
