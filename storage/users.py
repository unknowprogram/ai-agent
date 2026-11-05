"""认证持久化；失败次数在数据库累计，多进程共享锁定状态。"""

import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from pymysql.err import IntegrityError

from service.auth import SESSION_SECONDS, hash_password, token_hash, verify_password
from storage.mysql import _transaction

# 不存在的账号也进行同等密码计算。
_DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


def _now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _create_session(cursor, user_id: str, now: datetime) -> str:
    token = secrets.token_urlsafe(32)
    cursor.execute("DELETE FROM user_sessions WHERE expires_at <= %s LIMIT 1000", (now,))
    cursor.execute("INSERT INTO user_sessions (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
                   (token_hash(token), user_id, now + timedelta(seconds=SESSION_SECONDS)))
    return token


def register(username: str, password: str, *, previous_token: str | None = None) -> tuple[dict, str]:
    """账号和登录会话一起提交；并发注册由用户名唯一索引裁决。"""
    from schemas.auth import LoginRequest
    validated = LoginRequest(username=username, password=password)
    encoded = hash_password(validated.password)
    user_id = str(uuid.uuid4())
    try:
        with _transaction() as conn, conn.cursor() as cursor:
            cursor.execute("INSERT INTO users (id, username, password_hash) VALUES (%s, %s, %s)",
                           (user_id, validated.username, encoded))
            token = _create_session(cursor, user_id, _now())
            if previous_token:
                cursor.execute("DELETE FROM user_sessions WHERE token_hash = %s", (token_hash(previous_token),))
    except IntegrityError as exc:
        if exc.args and exc.args[0] == 1062:
            raise HTTPException(409, "这个用户名已被使用，请换一个。") from exc
        raise
    return {"id": user_id, "username": validated.username}, token


def login(username: str, password: str) -> tuple[dict, str]:
    now = _now()
    error = None
    result = None
    with _transaction() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT id, username, password_hash, is_active, failed_login_count, locked_until "
                       "FROM users WHERE username = %s FOR UPDATE", (username,))
        user = cursor.fetchone()
        if user and user["locked_until"] and user["locked_until"] > now:
            error = HTTPException(429, "尝试次数过多，请 15 分钟后重试。")
        else:
            valid = verify_password(password, user["password_hash"] if user else _DUMMY_HASH)
            if not user or not valid or not user["is_active"]:
                if user:
                    failures = (0 if user["locked_until"] else user["failed_login_count"]) + 1
                    locked = now + timedelta(minutes=15) if failures >= 5 else None
                    cursor.execute("UPDATE users SET failed_login_count = %s, locked_until = %s WHERE id = %s",
                                   (failures, locked, user["id"]))
                error = HTTPException(401, "用户名或密码错误。")
            else:
                cursor.execute("UPDATE users SET failed_login_count = 0, locked_until = NULL WHERE id = %s", (user["id"],))
                token = _create_session(cursor, user["id"], now)
                result = ({"id": user["id"], "username": user["username"]}, token)
    # 在事务提交后抛出，确保密码失败计数不会回滚。
    if error:
        raise error
    return result


def get_session(token: str) -> dict | None:
    if len(token) != 43:
        return None
    with _transaction() as conn, conn.cursor() as cursor:
        cursor.execute("SELECT u.id, u.username FROM user_sessions s JOIN users u ON u.id = s.user_id "
                       "WHERE s.token_hash = %s AND s.expires_at > %s AND u.is_active = TRUE",
                       (token_hash(token), _now()))
        return cursor.fetchone()


def delete_session(token: str) -> None:
    with _transaction() as conn, conn.cursor() as cursor:
        cursor.execute("DELETE FROM user_sessions WHERE token_hash = %s", (token_hash(token),))


def create_user(username: str, password: str, *, claim_legacy: bool = False) -> str:
    from schemas.auth import LoginRequest
    LoginRequest(username=username, password=password)
    encoded = hash_password(password)
    user_id = str(uuid.uuid4())
    with _transaction() as conn, conn.cursor() as cursor:
        cursor.execute("INSERT INTO users (id, username, password_hash) VALUES (%s, %s, %s)",
                       (user_id, username.strip(), encoded))
        if claim_legacy:
            # 明确执行此选项才接管无主数据，不改变已有用户的数据归属。
            for table in ("conversations", "documents", "generated_images"):
                cursor.execute(f"UPDATE {table} SET user_id = %s WHERE user_id IS NULL", (user_id,))
    return user_id


if __name__ == "__main__":
    import argparse
    import getpass
    from pymysql.err import IntegrityError

    parser = argparse.ArgumentParser(description="创建登录账号；密码通过隐藏输入读取。")
    parser.add_argument("username")
    parser.add_argument("--claim-legacy", action="store_true", help="将 user_id 为空的旧数据归入此账号")
    args = parser.parse_args()
    password = getpass.getpass("Password (12–128 characters): ")
    if password != getpass.getpass("Confirm password: "):
        raise SystemExit("Passwords do not match.")
    try:
        identifier = create_user(args.username, password, claim_legacy=args.claim_legacy)
    except (ValueError, IntegrityError) as exc:
        raise SystemExit("账号创建失败：用户名重复、用户名格式无效或密码长度不符合要求。") from exc
    print(f"User created: {args.username} ({identifier})")
