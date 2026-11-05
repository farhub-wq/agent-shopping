"""refresh 令牌工具：明文只出现在响应 Cookie 里，落库一律 sha256 哈希。"""

import hashlib
import secrets


def new_refresh_token() -> str:
    # 48 字节熵足够抵御在线猜测；令牌本体不做签名，有效性完全由服务端存储决定
    return secrets.token_urlsafe(48)


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()
