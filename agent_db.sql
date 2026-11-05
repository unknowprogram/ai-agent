-- 项目统一初始化脚本，在已配置的数据库中执行。
-- 仅创建缺失的表，保留现有表结构和数据。
SET NAMES utf8mb4;

-- 账号通过网页注册或 python -m storage.users 创建，数据库不保存明文密码。
CREATE TABLE IF NOT EXISTS users (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    username VARCHAR(64) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    failed_login_count INT UNSIGNED NOT NULL DEFAULT 0,
    locked_until DATETIME NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uk_users_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS user_sessions (
    token_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL,
    expires_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_sessions_user (user_id),
    KEY idx_sessions_expires (expires_at),
    CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- ----------------------------
-- Table structure for conversations
-- ----------------------------
CREATE TABLE IF NOT EXISTS `conversations`  (
  `id` varchar(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `title` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '新对话',
  `user_id` VARCHAR(36) NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`) USING BTREE,
  KEY idx_conversations_user_updated (user_id, updated_at),
  CONSTRAINT fk_conversations_user FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci ROW_FORMAT = Dynamic;

-- ----------------------------
-- Table structure for messages
-- ----------------------------
CREATE TABLE IF NOT EXISTS `messages`  (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `conversation_id` varchar(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `role` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `content` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`) USING BTREE,
  INDEX `idx_conversation_created`(`conversation_id` ASC, `created_at` ASC, `id` ASC) USING BTREE,
  CONSTRAINT `fk_messages_conversation` FOREIGN KEY (`conversation_id`) REFERENCES `conversations` (`id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci ROW_FORMAT = Dynamic;

-- 文档元数据
CREATE TABLE IF NOT EXISTS documents (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    user_id VARCHAR(36) NULL,
    filename VARCHAR(255) NOT NULL,
    file_type VARCHAR(16) NOT NULL,
    char_count INT UNSIGNED NOT NULL,
    warnings TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_documents_user (user_id),
    CONSTRAINT fk_documents_user FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB
DEFAULT CHARSET=utf8mb4
COLLATE=utf8mb4_unicode_ci;

-- 文档分块
CREATE TABLE IF NOT EXISTS document_chunks (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    document_id VARCHAR(36) NOT NULL,
    chunk_index INT UNSIGNED NOT NULL,
    location VARCHAR(100) NOT NULL,
    content TEXT NOT NULL,
    UNIQUE KEY uk_document_chunk (document_id, chunk_index),
    CONSTRAINT fk_document_chunks_document
        FOREIGN KEY (document_id)
        REFERENCES documents(id)
        ON DELETE CASCADE
) ENGINE=InnoDB
DEFAULT CHARSET=utf8mb4
COLLATE=utf8mb4_unicode_ci;

-- 会话与文档的关联
CREATE TABLE IF NOT EXISTS conversation_documents (
    conversation_id VARCHAR(36) NOT NULL,
    document_id VARCHAR(36) NOT NULL,
    PRIMARY KEY (conversation_id, document_id),
    CONSTRAINT fk_conversation_documents_conversation
        FOREIGN KEY (conversation_id)
        REFERENCES conversations(id)
        ON DELETE CASCADE,
    CONSTRAINT fk_conversation_documents_document
        FOREIGN KEY (document_id)
        REFERENCES documents(id)
        ON DELETE CASCADE
) ENGINE=InnoDB
DEFAULT CHARSET=utf8mb4
COLLATE=utf8mb4_unicode_ci;

-- 生成图片与最终助手消息的关联；原件保存在 .data/generated。
CREATE TABLE IF NOT EXISTS generated_images (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    user_id VARCHAR(36) NULL,
    conversation_id VARCHAR(36) NOT NULL,
    message_id BIGINT NOT NULL,
    prompt TEXT NOT NULL,
    model VARCHAR(128) NOT NULL,
    file_type VARCHAR(16) NOT NULL,
    width INT UNSIGNED NOT NULL,
    height INT UNSIGNED NOT NULL,
    watermark BOOLEAN NOT NULL DEFAULT TRUE,
    elapsed_seconds DOUBLE NOT NULL,
    `usage` TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    KEY idx_generated_images_message (conversation_id, message_id),
    KEY idx_images_user (user_id),
    CONSTRAINT fk_images_user FOREIGN KEY (user_id) REFERENCES users(id),
    CONSTRAINT fk_generated_images_conversation
        FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    CONSTRAINT fk_generated_images_message
        FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
) ENGINE=InnoDB
DEFAULT CHARSET=utf8mb4
COLLATE=utf8mb4_unicode_ci;
