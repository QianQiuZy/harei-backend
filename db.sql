CREATE DATABASE IF NOT EXISTS harei
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE harei;

CREATE TABLE IF NOT EXISTS captains (
  id INT AUTO_INCREMENT PRIMARY KEY,
  user_uid VARCHAR(255) NOT NULL,
  username VARCHAR(255) NULL,
  joined_at DATETIME NOT NULL,
  joined_month VARCHAR(6) NOT NULL,
  level ENUM('舰长', '提督', '总督') NOT NULL,
  ship_count INT NOT NULL DEFAULT 1,
  is_red_packet TINYINT(1) NOT NULL DEFAULT 0,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_captains_uid (user_uid),
  INDEX idx_captains_month_level (joined_month, level),
  INDEX idx_captains_joined_at (joined_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS captain_gift_archives (
  id INT AUTO_INCREMENT PRIMARY KEY,
  gift_month VARCHAR(6) NOT NULL,
  image_path VARCHAR(255) NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_captain_gift_archives_month (gift_month)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS gift_ranking (
  user_uid VARCHAR(255) PRIMARY KEY,
  username VARCHAR(255) NULL,
  gift_count INT NOT NULL DEFAULT 0,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_gift_ranking_gift_count (gift_count)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS messages (
  message_id INT AUTO_INCREMENT PRIMARY KEY,
  ip_address VARCHAR(45) NOT NULL,
  message_text TEXT NULL,
  tag VARCHAR(255) NULL,
  status ENUM('pending', 'approved', 'archived', 'delete') NOT NULL DEFAULT 'pending',
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_messages_ip_created (ip_address, created_at),
  INDEX idx_messages_status_created (status, created_at),
  INDEX idx_messages_tag (tag)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS images (
  image_id INT AUTO_INCREMENT PRIMARY KEY,
  message_id INT NOT NULL,
  image_path VARCHAR(255) NOT NULL,
  thumb_path VARCHAR(255) NULL,
  jpg_path VARCHAR(255) NULL,
  uploaded_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_images_message_id (message_id),
  CONSTRAINT fk_images_message_id
    FOREIGN KEY (message_id)
    REFERENCES messages (message_id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS tags (
  tag_id INT AUTO_INCREMENT PRIMARY KEY,
  tag_name VARCHAR(255) NOT NULL UNIQUE,
  status ENUM('pending', 'approved', 'archived') NOT NULL DEFAULT 'approved',
  expires_at DATETIME NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_tags_status_expires (status, expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS downloads (
  download_id INT AUTO_INCREMENT PRIMARY KEY,
  description VARCHAR(255) NOT NULL,
  path VARCHAR(1024) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS music (
  music_id INT AUTO_INCREMENT PRIMARY KEY,
  title VARCHAR(255) NOT NULL,
  artist VARCHAR(255) NOT NULL,
  type VARCHAR(50) NULL,
  language VARCHAR(50) NULL,
  note TEXT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
