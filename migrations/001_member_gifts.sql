CREATE TABLE IF NOT EXISTS gift_months (
	room_id INTEGER NOT NULL,
	month VARCHAR(6) NOT NULL,
	settled BOOL NOT NULL,
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
	roster_level INTEGER NOT NULL,
	level INTEGER NOT NULL,
	state VARCHAR(16) NOT NULL,
	owner_id INTEGER,
	waived_by INTEGER,
	grace_until DATETIME,
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
	member_id INTEGER NOT NULL,
	address TEXT,
	PRIMARY KEY (uid),
	FOREIGN KEY(member_id) REFERENCES member_accounts (id),
	INDEX ix_member_uid_bindings_member_id (member_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO gift_system (id, first_month) VALUES (1, DATE_FORMAT(UTC_TIMESTAMP() + INTERVAL 8 HOUR, '%Y%m'));
