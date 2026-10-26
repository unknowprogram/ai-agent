import json
import uuid
from storage.mysql import _transaction
from core.exceptions import (
     DocumentLimitError,DocumentNotFoundError
)

MAX_CONVERSATION_FILES = 5

def save_document(
    filename: str,
    file_type: str,
    char_count: int,
    chunks: list[dict],
    warnings: list[str],
    raw: bytes | None = None,
) -> str:
    file_id = str(uuid.uuid4())

    if raw is not None:
        # 先保存原文件，成功提交数据库后才向调用方返回文件 ID。
        from storage.file_data import save_file_data
        save_file_data(file_id, raw)

    try:
        _save_document_metadata(file_id, filename, file_type, char_count, chunks, warnings)
    except BaseException:
        if raw is not None:
            from storage.file_data import remove_file_data
            remove_file_data(file_id)
        raise
    return file_id


def _save_document_metadata(file_id, filename, file_type, char_count, chunks, warnings):
    with _transaction() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO documents (
                id, filename, file_type, char_count, warnings
            )
            VALUES (%s, %s, %s, %s, %s)
            """,
            (
                file_id,
                filename,
                file_type,
                char_count,
                json.dumps(warnings, ensure_ascii=False),
            ),
        )

        cursor.executemany(
            """
            INSERT INTO document_chunks (
                document_id, chunk_index, location, content
            )
            VALUES (%s, %s, %s, %s)
            """,
            [
                (
                    file_id,
                    index,
                    chunk["location"],
                    chunk["content"],
                )
                for index, chunk in enumerate(chunks, start=1)
            ],
        )


def get_document(file_id: str) -> dict:
    with _transaction() as conn, conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT
                d.id AS file_id,
                d.filename,
                d.file_type,
                d.char_count,
                d.warnings,
                d.created_at,
                (
                    SELECT COUNT(*)
                    FROM document_chunks c
                    WHERE c.document_id = d.id
                ) AS chunk_count,
                COALESCE(
                    (
                        SELECT c.content
                        FROM document_chunks c
                        WHERE c.document_id = d.id
                        ORDER BY c.chunk_index
                        LIMIT 1
                    ),
                    ''
                ) AS preview
            FROM documents d
            WHERE d.id = %s
            """,
            (file_id,),
        )

        document = cursor.fetchone()

    if document is None:
        raise DocumentNotFoundError()

    document["warnings"] = json.loads(document["warnings"])
    return document


def resolve_document_ids(
    conversation_id: str | None,
    requested_ids: list[str],
) -> list[str]:
    """合并本次上传文件与会话已关联文件，并检查是否存在。"""
    if conversation_id is None and not requested_ids:
        return []

    with _transaction() as conn, conn.cursor() as cursor:
        linked_ids = []

        if conversation_id:
            cursor.execute(
                """
                SELECT document_id
                FROM conversation_documents
                WHERE conversation_id = %s
                ORDER BY document_id
                """,
                (conversation_id,),
            )

            linked_ids = [
                row["document_id"]
                for row in cursor.fetchall()
            ]

        document_ids = list(
            dict.fromkeys([*requested_ids, *linked_ids])
        )

        if len(document_ids) > MAX_CONVERSATION_FILES:
            raise DocumentLimitError()

        if not document_ids:
            return []

        placeholders = ",".join(["%s"] * len(document_ids))

        cursor.execute(
            f"""
            SELECT id
            FROM documents
            WHERE id IN ({placeholders})
            """,
            tuple(document_ids),
        )

        found_ids = {
            row["id"]
            for row in cursor.fetchall()
        }

        if found_ids != set(document_ids):
            raise DocumentNotFoundError()

    return document_ids


def list_conversation_documents(conversation_id: str) -> list[dict]:
    ids = resolve_document_ids(conversation_id, [])
    return [get_document(file_id) for file_id in ids]


def get_document_metadata(document_ids: list[str]) -> list[dict]:
    """读取供 Agent 选择文件的元数据，不读取分块正文。"""
    if not document_ids:
        return []
    placeholders = ",".join(["%s"] * len(document_ids))
    with _transaction() as conn, conn.cursor() as cursor:
        cursor.execute(
            f"SELECT id AS file_id, filename, file_type, char_count FROM documents WHERE id IN ({placeholders}) ORDER BY id",
            tuple(document_ids),
        )
        return list(cursor.fetchall())


def get_document_chunks(document_ids: list[str]) -> list[dict]:
    if not document_ids:
        return []

    placeholders = ",".join(["%s"] * len(document_ids))

    with _transaction() as conn, conn.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT
                d.id AS file_id,
                d.filename,
                c.chunk_index,
                c.location,
                c.content
            FROM document_chunks c
            INNER JOIN documents d
                ON d.id = c.document_id
            WHERE d.id IN ({placeholders})
            ORDER BY d.id, c.chunk_index
            """,
            tuple(document_ids),
        )

        return list(cursor.fetchall())
