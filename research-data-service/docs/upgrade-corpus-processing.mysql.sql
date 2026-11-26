-- MySQL 8.0+. Apply once to the previous four-table schema.
ALTER TABLE `doc`
  ADD COLUMN `content_hash` BINARY(32) NULL,
  ADD KEY `idx_doc_content_hash` (`content_hash`);

CREATE TABLE IF NOT EXISTS `doc_label` (
  `doc_id` VARCHAR(191) NOT NULL,
  `label_type` ENUM('industry','theme') NOT NULL,
  `label_value` VARCHAR(128) NOT NULL,
  `is_primary` BOOLEAN NOT NULL,
  `labeled_at` DATETIME(6) NOT NULL,
  PRIMARY KEY (`doc_id`, `label_type`, `label_value`),
  KEY `idx_doc_label_filter` (`label_type`, `label_value`, `doc_id`),
  CONSTRAINT `fk_overseas_label_doc` FOREIGN KEY (`doc_id`) REFERENCES `doc` (`id`) ON DELETE CASCADE,
  CHECK (`is_primary` IN (0,1) AND (`label_type` = 'industry' OR `is_primary` = 0))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
