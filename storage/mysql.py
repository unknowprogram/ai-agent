"""MySQL 会话存储：短事务读写，不在模型生成期间占用连接。"""

import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import pymysql
from dotenv import load_dotenv
from pymysql.cursors import DictCursor

from core.exceptions import (
    ConversationConflictError, ConversationNotFoundError, DatabaseConfigError,
)

load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _setting(primary: str, alias: str, default: str) -> str:
    return os.getenv(primary, os.getenv(alias, default))


def get_connection():
    """兼容现有 MYSQL_* 配置及 DB_* 别名。"""
    try:
        port = int(_setting("MYSQL_PORT", "DB_PORT", "3306"))
        if not 1 <= port <= 65535:
            raise ValueError("Invalid database port")
    except ValueError as exc:
        raise DatabaseConfigError() from exc
    try:
        return pymysql.connect(
            host=_setting("MYSQL_HOST", "DB_HOST", "111.229.181.165"),
            port=port,
            user=_setting("MYSQL_USER", "DB_USER", "agent_user"),
            password=_setting("MYSQL_PASSWORD", "DB_PASSWORD", ""),
            database=_setting("MYSQL_DATABASE", "DB_NAME", "agent_db"),
            charset="utf8mb4", cursorclass=DictCursor,
            connect_timeout=5, read_timeout=10, write_timeout=10, autocommit=False,
        )
    except RuntimeError as exc:
        # PyMySQL 在缺少某些认证依赖时抛出 RuntimeError。
        raise DatabaseConfigError() from exc


@contextmanager
def _transaction():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _require_conversation(cursor, conversation_id: str, *, lock: bool = False):
    sql = "SELECT id, title, created_at, updated_at FROM conversations WHERE id = %s"
    cursor.execute(sql + (" FOR UPDATE" if lock else ""), (conversation_id,))
    row = cursor.fetchone()
    if row is None:
        raise ConversationNotFoundError()
    return row


def _last_message_id(cursor, conversation_id: str) -> int:
    cursor.execute("SELECT COALESCE(MAX(id), 0) AS last_id FROM messages WHERE conversation_id = %s", (conversation_id,))
    return cursor.fetchone()["last_id"]


def create_conversation(title: str = "新对话") -> str:
    conversation_id = str(uuid.uuid4())
    with _transaction() as conn, conn.cursor() as cursor:
        cursor.execute("INSERT INTO conversations (id, title) VALUES (%s, %s)", (conversation_id, title[:200]))
    return conversation_id


def save_message(conversation_id: str, role: str, content: str) -> None:
    """独立保存消息；聊天流程使用 save_turn 原子保存整轮问答。"""
    if role not in {"user", "assistant", "system", "tool"}:
        raise ValueError(f"不支持的消息角色: {role}")
    with _transaction() as conn, conn.cursor() as cursor:
        _require_conversation(cursor, conversation_id, lock=True)
        cursor.execute("INSERT INTO messages (conversation_id, role, content) VALUES (%s, %s, %s)", (conversation_id, role, content))
        cursor.execute("UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = %s", (conversation_id,))


def load_memory(conversation_id: str, limit: int = 20) -> dict:
    """在同一个读事务中获取会话快照，保留版本用于提交时检测并发更新。"""
    with _transaction() as conn, conn.cursor() as cursor:
        conversation = _require_conversation(cursor, conversation_id)
        last_id = _last_message_id(cursor, conversation_id)
        cursor.execute(
            "SELECT role, content FROM messages WHERE conversation_id = %s "
            "AND role IN ('user', 'assistant') ORDER BY id DESC LIMIT %s",
            (conversation_id, max(1, min(limit, 100))),
        )
        history = list(reversed(cursor.fetchall()))
    return {"conversation": conversation, "history": history, "last_message_id": last_id}


def get_history(conversation_id: str, limit: int = 20) -> list[dict]:
    return load_memory(conversation_id, limit)["history"]


def save_turn(
    conversation_id: str | None, message: str, reply: str,
    *, expected_message_id: int = 0, initial_history: list[dict] | None = None,
    file_ids: list[str] | None = None,
) -> str:
    """一次事务保存问答；新会话也在此事务中创建，失败时不留下半轮消息。"""
    is_new = conversation_id is None
    conversation_id = conversation_id or str(uuid.uuid4())
    with _transaction() as conn, conn.cursor() as cursor:
        if is_new:
            cursor.execute("INSERT INTO conversations (id, title) VALUES (%s, %s)", (conversation_id, message[:200]))
        else:
            _require_conversation(cursor, conversation_id, lock=True)
            if _last_message_id(cursor, conversation_id) != expected_message_id:
                raise ConversationConflictError()
        if file_ids:
            from core.exceptions import DocumentLimitError, DocumentNotFoundError
            cursor.execute("SELECT document_id FROM conversation_documents WHERE conversation_id = %s", (conversation_id,))
            linked = {row["document_id"] for row in cursor.fetchall()}
            ids = set(file_ids) | linked
            if len(ids) > 5:
                raise DocumentLimitError()
            placeholders = ",".join(["%s"] * len(ids))
            cursor.execute(f"SELECT id FROM documents WHERE id IN ({placeholders}) FOR UPDATE", tuple(sorted(ids)))
            if {row["id"] for row in cursor.fetchall()} != ids:
                raise DocumentNotFoundError()
            new_ids = ids - linked
            if new_ids:
                cursor.executemany("INSERT INTO conversation_documents (conversation_id, document_id) VALUES (%s, %s)", [(conversation_id, file_id) for file_id in sorted(new_ids)])
        messages = [*(initial_history or [])] if is_new else []
        messages.extend([{"role": "user", "content": message}, {"role": "assistant", "content": reply}])
        if any(item["role"] not in {"user", "assistant"} for item in messages):
            raise ValueError("会话记忆只接收用户和助手消息。")
        cursor.executemany(
            "INSERT INTO messages (conversation_id, role, content) VALUES (%s, %s, %s)",
            [(conversation_id, item["role"], item["content"]) for item in messages],
        )
        cursor.execute("UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = %s", (conversation_id,))
    return conversation_id


def list_conversations(limit: int = 20) -> list[dict]:
    with _transaction() as conn, conn.cursor() as cursor:
        cursor.execute(
            "SELECT id, title, created_at, updated_at FROM conversations ORDER BY updated_at DESC, id DESC LIMIT %s",
            (max(1, min(limit, 100)),),
        )
        return list(cursor.fetchall())


def get_conversation(conversation_id: str, limit: int = 80) -> dict:
    with _transaction() as conn, conn.cursor() as cursor:
        conversation = _require_conversation(cursor, conversation_id)
        cursor.execute(
            "SELECT id, role, content, created_at FROM messages WHERE conversation_id = %s ORDER BY id DESC LIMIT %s",
            (conversation_id, max(1, min(limit, 100))),
        )
        conversation["messages"] = list(reversed(cursor.fetchall()))
    from storage.documents import list_conversation_documents
    conversation["files"] = list_conversation_documents(conversation_id)
    return conversation


def delete_conversation(conversation_id: str) -> None:
    with _transaction() as conn, conn.cursor() as cursor:
        _require_conversation(cursor, conversation_id, lock=True)
        # 现有外键为 RESTRICT，因此先删消息，再删会话。
        cursor.execute("DELETE FROM messages WHERE conversation_id = %s", (conversation_id,))
        cursor.execute("DELETE FROM conversations WHERE id = %s", (conversation_id,))


def initialize_schema() -> None:
    """执行根目录 agent_db.sql，仅补建缺失的表。"""
    schema = (Path(__file__).resolve().parents[1] / "agent_db.sql").read_text(encoding="utf-8-sig")
    with _transaction() as conn, conn.cursor() as cursor:
        for statement in schema.split(";"):
            if statement.strip():
                cursor.execute(statement)


if __name__ == "__main__":
    import sys

    if sys.argv[1:] != ["--init"]:
        raise SystemExit("Usage: python -m storage.mysql --init")
    initialize_schema()
    print("Database tables are ready (agent_db.sql).")
