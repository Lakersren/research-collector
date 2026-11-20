-- MySQL 8.0+. IT 手动初始化入口，正常采集程序不会执行本文件。
-- 在选定的数据库中创建缺失表，不修改现有表。
CREATE TABLE IF NOT EXISTS `source` (
  `id` VARCHAR(191) NOT NULL,
  `name` VARCHAR(255) NOT NULL,
  `channel` VARCHAR(32) NOT NULL DEFAULT 'web',
  `trust` TINYINT NOT NULL DEFAULT 3,
  `note` TEXT NULL,
  `first_ts` DATETIME(6) NULL,
  `last_ts` DATETIME(6) NULL,
  `n_docs` INT NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  CHECK (`trust` BETWEEN 0 AND 5),
  CHECK (`n_docs` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS `doc` (
  `id` VARCHAR(191) NOT NULL,
  `source_id` VARCHAR(191) NOT NULL,
  `title` TEXT NOT NULL,
  `authors` JSON NOT NULL,
  `published_at` DATETIME(6) NULL,
  `url` TEXT NOT NULL,
  `content_state` VARCHAR(16) NOT NULL,
  `content` LONGTEXT NULL,
  `ingested_at` DATETIME(6) NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_doc_source_published` (`source_id`, `published_at`),
  CONSTRAINT `fk_overseas_doc_source` FOREIGN KEY (`source_id`) REFERENCES `source` (`id`),
  CHECK (`content_state` IN ('ready', 'preview', 'no_text')),
  CHECK (JSON_TYPE(`authors`) = 'ARRAY')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS `doc_image` (
  `doc_id` VARCHAR(191) NOT NULL,
  `position` INT NOT NULL,
  `file_key` VARCHAR(1024) NULL,
  `status` VARCHAR(16) NOT NULL,
  `width` INT NULL,
  `height` INT NULL,
  PRIMARY KEY (`doc_id`, `position`),
  CONSTRAINT `fk_overseas_image_doc` FOREIGN KEY (`doc_id`) REFERENCES `doc` (`id`),
  CHECK (`position` > 0),
  CHECK (`status` IN ('pending', 'ok', 'failed')),
  CHECK (`status` <> 'ok' OR (`file_key` IS NOT NULL AND CHAR_LENGTH(`file_key`) > 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS `collection_run` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `source_id` VARCHAR(191) NOT NULL,
  `started_at` DATETIME(6) NOT NULL,
  `finished_at` DATETIME(6) NULL,
  `n_discovered` INT NOT NULL DEFAULT 0,
  `n_fetched` INT NOT NULL DEFAULT 0,
  `n_failed` INT NOT NULL DEFAULT 0,
  `status` VARCHAR(16) NOT NULL,
  `error` TEXT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_run_source_started` (`source_id`, `started_at`),
  CONSTRAINT `fk_overseas_run_source` FOREIGN KEY (`source_id`) REFERENCES `source` (`id`),
  CHECK (`status` IN ('running', 'success', 'failed')),
  CHECK (`n_discovered` >= 0 AND `n_fetched` >= 0 AND `n_failed` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
