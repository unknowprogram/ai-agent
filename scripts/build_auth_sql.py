"""从完整建表脚本生成可直接执行、可重复运行的 MySQL 登录迁移。"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def conditional(kind: str, table: str, name: str, ddl: str) -> str:
    catalog, field = {
        "column": ("COLUMNS", "COLUMN_NAME"),
        "index": ("STATISTICS", "INDEX_NAME"),
        "constraint": ("TABLE_CONSTRAINTS", "CONSTRAINT_NAME"),
    }[kind]
    return (
        f"\n-- {table}.{name}\n"
        f"SET @auth_ddl = IF(EXISTS (SELECT 1 FROM information_schema.{catalog} "
        f"WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = '{table}' AND {field} = '{name}'),\n"
        f"    'SELECT 1', '{ddl}');\n"
        "PREPARE auth_statement FROM @auth_ddl;\n"
        "EXECUTE auth_statement;\n"
        "DEALLOCATE PREPARE auth_statement;\n"
    )


def build():
    sql = (
        "-- 登录功能迁移，MySQL 5.7 / 8.x，可直接执行且可重复运行。\n"
        "-- 默认数据库 agent_db；使用其他名称时修改下面的 USE。\n"
        "-- 保留旧数据；旧数据 user_id 为 NULL，需管理员明确认领后才可访问。\n"
        "-- DDL 会自动提交，请在应用停机期间执行；不删除表、不清空业务数据。\n"
        "USE agent_db;\n\n"
    ) + (ROOT / "agent_db.sql").read_text(encoding="utf-8-sig")
    sql += conditional("column", "users", "failed_login_count",
                       "ALTER TABLE users ADD COLUMN failed_login_count INT UNSIGNED NOT NULL DEFAULT 0")
    sql += conditional("column", "users", "locked_until", "ALTER TABLE users ADD COLUMN locked_until DATETIME NULL")
    for table, index, fields, foreign_key in (
        ("conversations", "idx_conversations_user_updated", "user_id, updated_at", "fk_conversations_user"),
        ("documents", "idx_documents_user", "user_id", "fk_documents_user"),
        ("generated_images", "idx_images_user", "user_id", "fk_images_user"),
    ):
        sql += conditional("column", table, "user_id", f"ALTER TABLE {table} ADD COLUMN user_id VARCHAR(36) NULL")
        sql += conditional("index", table, index, f"ALTER TABLE {table} ADD INDEX {index} ({fields})")
        sql += conditional("constraint", table, foreign_key,
                           f"ALTER TABLE {table} ADD CONSTRAINT {foreign_key} FOREIGN KEY (user_id) REFERENCES users(id)")
    sql += "\nSELECT 'Login schema migration complete' AS result;\n"
    target = ROOT / "migrations" / "001_auth.sql"
    target.parent.mkdir(exist_ok=True)
    target.write_text(sql, encoding="utf-8")


if __name__ == "__main__":
    build()
