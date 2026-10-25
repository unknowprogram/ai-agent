-- 项目统一初始化脚本，在已配置的数据库中执行。
-- 仅创建缺失的表，保留现有表结构和数据。
SET NAMES utf8mb4;

-- ----------------------------
-- Table structure for conversations
-- ----------------------------
CREATE TABLE IF NOT EXISTS `conversations`  (
  `id` varchar(36) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `title` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT '新对话',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`) USING BTREE
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
    filename VARCHAR(255) NOT NULL,
    file_type VARCHAR(16) NOT NULL,
    char_count INT UNSIGNED NOT NULL,
    warnings TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
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
