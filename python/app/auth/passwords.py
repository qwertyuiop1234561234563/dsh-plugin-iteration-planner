"""口令散列与校验（纯标准库 ``hashlib.scrypt``，不引入额外依赖）。

为什么用 scrypt 而不是 PBKDF2 / 明文加盐：scrypt 是**内存硬**（memory-hard）算法，
在保持单次校验可控耗时的同时，把 GPU / ASIC 并行爆破的性价比压到最低；参数
（``N`` / ``r`` / ``p``）随散列一并编码进结果串，后续调参不会导致旧口令失效。

编码格式：``scrypt$N$r$p$<salt-b64>$<derived-key-b64>``。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

#: scrypt 参数：N=2^14（16 MiB 内存）、r=8、p=1——单次约几十毫秒，足以抵御在线爆破
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_LEN = 32
_SALT_LEN = 16
#: 算法标识（前缀），便于后续平滑切换算法
_SCHEME = "scrypt"


def _maxmem(n: int, r: int) -> int:
    """OpenSSL 默认 ``maxmem`` 偏小，显式给足内存上限以免大参数直接报错。"""
    return max(32 * 1024 * 1024, 128 * n * r * 2)


def hash_password(password: str) -> str:
    """生成口令散列串（每次调用使用新的随机盐）。"""
    salt = secrets.token_bytes(_SALT_LEN)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_KEY_LEN,
        maxmem=_maxmem(_SCRYPT_N, _SCRYPT_R),
    )
    return "$".join(
        (
            _SCHEME,
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(derived).decode("ascii"),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    """常量时间校验口令；散列串损坏 / 格式非法一律返回 ``False``（不抛异常）。"""
    parts = (encoded or "").split("$")
    if len(parts) != 6 or parts[0] != _SCHEME:
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = base64.b64decode(parts[4], validate=True)
        expected = base64.b64decode(parts[5], validate=True)
    except (ValueError, TypeError):
        return False
    try:
        derived = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=_maxmem(n, r),
        )
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(derived, expected)


__all__ = ["hash_password", "verify_password"]
