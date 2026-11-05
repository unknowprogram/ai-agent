"""密码与会话安全：标准库 scrypt，随机 Token，数据库只存 Token 哈希。"""

import base64
import hashlib
import hmac
import os
import secrets


COOKIE_NAME = "zhixu_session"
SESSION_SECONDS = 7 * 24 * 60 * 60


def secure_cookie() -> bool:
    return os.getenv("AUTH_COOKIE_SECURE", "false").lower() in {"true", "1", "yes"}


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 128:
        raise ValueError("密码长度必须为 12–128 个字符。")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=32768, r=8, p=1,
                            dklen=32, maxmem=64 * 1024 * 1024)
    return "scrypt$32768$8$1$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$")
        # 限制哈希参数，防止损坏的数据导致不受控的资源消耗。
        if (algorithm, n, r, p) != ("scrypt", "32768", "8", "1"):
            return False
        salt_bytes = base64.b64decode(salt, validate=True)
        expected_bytes = base64.b64decode(expected, validate=True)
        if len(salt_bytes) != 16 or len(expected_bytes) != 32:
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt_bytes,
                                n=32768, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(actual, expected_bytes)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def csrf_token(token: str) -> str:
    return hmac.new(token.encode("utf-8"), b"zhixu-csrf-v1", hashlib.sha256).hexdigest()
