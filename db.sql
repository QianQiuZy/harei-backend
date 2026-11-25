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
  room_id BIGINT NULL,
  paid_price BIGINT NULL,
  legacy_assumed_paid BOOL NOT NULL DEFAULT FALSE,
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

CREATE TABLE IF NOT EXISTS songs (
  song_id INT AUTO_INCREMENT PRIMARY KEY, source_key VARCHAR(80) NOT NULL UNIQUE,
  title VARCHAR(255) NOT NULL, artist VARCHAR(500) NOT NULL, artists JSON NOT NULL,
  genre VARCHAR(100) NOT NULL, language VARCHAR(50) NOT NULL, work_type VARCHAR(50) NOT NULL,
  notes TEXT NOT NULL, metadata_status VARCHAR(30) NOT NULL DEFAULT 'complete',
  status VARCHAR(20) NOT NULL DEFAULT 'active', version INT NOT NULL DEFAULT 1,
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  INDEX idx_songs_status (status), INDEX idx_songs_filters (genre, language, work_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS song_performances (
  performance_id INT AUTO_INCREMENT PRIMARY KEY, source_key VARCHAR(80) NOT NULL UNIQUE, song_id INT NOT NULL,
  performed_on DATE NOT NULL, platform VARCHAR(100) NOT NULL, stream_id VARCHAR(100) NULL, stream_title VARCHAR(255) NULL,
  stream_url VARCHAR(2048) NULL, clip_url VARCHAR(2048) NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP, INDEX idx_performances_song_date (song_id, performed_on),
  CONSTRAINT fk_song_performances_song FOREIGN KEY (song_id) REFERENCES songs(song_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS music_catalog_revision (id INT PRIMARY KEY, revision INT NOT NULL DEFAULT 0, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
INSERT IGNORE INTO music_catalog_revision (id, revision) VALUES (1, 0);
CREATE TABLE IF NOT EXISTS music_audit_events (audit_id INT AUTO_INCREMENT PRIMARY KEY, actor VARCHAR(255) NOT NULL, action VARCHAR(80) NOT NULL, entity_type VARCHAR(40) NOT NULL, entity_id VARCHAR(100) NOT NULL, details JSON NOT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, INDEX idx_music_audit_created (created_at)) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS gift_months (
	room_id INTEGER NOT NULL,
	month VARCHAR(6) NOT NULL,
	settled BOOL NOT NULL,
	roster_finalized_at DATETIME,
	deadline DATETIME,
	reminded BOOL NOT NULL,
	revision INTEGER NOT NULL,
	PRIMARY KEY (room_id, month)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS gift_purchase_levels (
	room_id INTEGER NOT NULL,
	month VARCHAR(6) NOT NULL,
	uid VARCHAR(20) NOT NULL,
	level INTEGER NOT NULL,
	paid_level INTEGER,
	applied_level INTEGER,
	nickname VARCHAR(255),
	INDEX ix_gift_purchase_pending (room_id, applied_level, month),
	PRIMARY KEY (room_id, month, uid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS gift_system (
	id INTEGER NOT NULL,
	first_month VARCHAR(6) NOT NULL,
	PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS member_accounts (
	id INTEGER NOT NULL AUTO_INCREMENT,
	email VARCHAR(254),
	password_hash VARCHAR(255),
	address TEXT,
	auto_fill_address BOOL NOT NULL DEFAULT TRUE,
	session_version INTEGER NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS member_appeals (
	id INTEGER NOT NULL AUTO_INCREMENT,
	ticket VARCHAR(64) NOT NULL,
	contact VARCHAR(254) NOT NULL,
	uid VARCHAR(20),
	message TEXT NOT NULL,
	reply TEXT,
	created_at DATETIME NOT NULL,
	closed BOOL NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (ticket)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS member_gift_audit (
	id INTEGER NOT NULL AUTO_INCREMENT,
	created_at DATETIME NOT NULL,
	actor VARCHAR(128) NOT NULL,
	action VARCHAR(40) NOT NULL,
	subject VARCHAR(100) NOT NULL,
	details JSON NOT NULL,
	PRIMARY KEY (id),
	INDEX ix_member_gift_audit_subject (subject)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS member_gift_mail (
	id INTEGER NOT NULL AUTO_INCREMENT,
	month VARCHAR(6) NOT NULL,
	member_id INTEGER NOT NULL,
	revision INTEGER NOT NULL,
	kind VARCHAR(16) NOT NULL,
	uids JSON NOT NULL,
	deadline DATETIME NOT NULL,
	attempts INTEGER NOT NULL,
	next_try DATETIME NOT NULL,
	sent BOOL NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_gift_mail_revision UNIQUE (month, member_id, revision),
	FOREIGN KEY(member_id) REFERENCES member_accounts (id),
	INDEX ix_member_gift_mail_next_try (next_try)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS member_gifts (
	room_id INTEGER NOT NULL,
	month VARCHAR(6) NOT NULL,
	uid VARCHAR(20) NOT NULL,
	nickname VARCHAR(255) NOT NULL,
	bound_username VARCHAR(255),
	roster_level INTEGER,
	needs_reship BOOL NOT NULL DEFAULT FALSE,
	level INTEGER NOT NULL,
	state VARCHAR(16) NOT NULL,
	owner_id INTEGER,
	waived_by INTEGER,
	grace_until DATETIME,
	extension_until DATETIME,
	refill_requested BOOL NOT NULL DEFAULT FALSE,
	returned_address TEXT,
	frozen_at DATETIME,
	address TEXT,
	tracking VARCHAR(100),
	manual_level BOOL NOT NULL,
	PRIMARY KEY (room_id, month, uid),
	FOREIGN KEY(room_id, month) REFERENCES gift_months (room_id, month),
	FOREIGN KEY(owner_id) REFERENCES member_accounts (id),
	FOREIGN KEY(waived_by) REFERENCES member_accounts (id),
	INDEX ix_member_gifts_owner_id (owner_id),
	INDEX ix_member_gifts_state (state),
	INDEX ix_member_gifts_uid (uid)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS member_uid_bindings (
	uid VARCHAR(20) NOT NULL,
	username VARCHAR(255),
	member_id INTEGER NOT NULL,
	address TEXT,
	PRIMARY KEY (uid),
	FOREIGN KEY(member_id) REFERENCES member_accounts (id),
	INDEX ix_member_uid_bindings_member_id (member_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO gift_system (id, first_month) VALUES (1, DATE_FORMAT(UTC_TIMESTAMP() + INTERVAL 8 HOUR, '%Y%m'));
CREATE TABLE IF NOT EXISTS hot_cache_fences (
    name VARCHAR(32) NOT NULL PRIMARY KEY
) ENGINE=InnoDB;
INSERT IGNORE INTO hot_cache_fences (name) VALUES ('music'), ('tags'), ('downloads'), ('archives');
