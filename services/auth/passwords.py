"""Password hashing with scrypt (memory-hard, standard library)."""

import base64
import hashlib
import hmac
import os

N, R, P = 2**14, 8, 1
MAXMEM = 64 * 1024 * 1024


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32, maxmem=MAXMEM)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = _derive(password, salt, N, R, P)
    salt_b64 = base64.b64encode(salt).decode()
    return f"scrypt${N}${R}${P}${salt_b64}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        expected = base64.b64decode(digest)
        actual = _derive(password, base64.b64decode(salt), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Verified against when the user does not exist, so that unknown and known users take the
# same time to reject.
DUMMY_HASH = hash_password("not-a-real-password")
