"""RFC 6238 TOTP：HMAC-SHA1 + 时间步 + 动态截断，窗口 ±1 步。

自研而非引入 pyotp：算法是公开标准、代码量小且可完整单测；
能直接返回匹配的时间步，支撑登录时的验证码重放防护。
"""

import base64
import hashlib
import hmac
import os
import struct

_PERIOD = 30
_DIGITS = 6


def new_secret() -> str:
    # 20 字节（160 bit）熵，base32 编码后可直接录入 Authenticator
    return base64.b32encode(os.urandom(20)).decode()


def otpauth_url(secret: str, account: str, issuer: str = "shop-agent") -> str:
    # otpauth 协议链接：Authenticator 扫码或手动录入时需要的标准参数
    label = f"{issuer}:{account}"
    return (
        f"otpauth://totp/{label}?secret={secret}"
        f"&issuer={issuer}&algorithm=SHA1&digits={_DIGITS}&period={_PERIOD}"
    )


def _hotp(secret: str, counter: int) -> str:
    # HOTP：HMAC-SHA1 后按 RFC 4226 动态截断，生成 6 位数字
    key = base64.b32decode(secret, casefold=True)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{binary % (10 ** _DIGITS):0{_DIGITS}d}"


def _step(now: int) -> int:
    return now // _PERIOD


def code_at(secret: str, now: int) -> str:
    return _hotp(secret, _step(now))


def verify(secret: str, code: str, now: int, window: int = 1) -> int | None:
    """校验 6 位动态码，返回匹配的时间步（供重放防护）；不匹配返回 None。

    窗口 ±window 步覆盖前后时间漂移；比对走常量时间，避免时序侧信道。
    """
    current = _step(now)
    for offset in range(-window, window + 1):
        if hmac.compare_digest(_hotp(secret, current + offset), code):
            return current + offset
    return None
