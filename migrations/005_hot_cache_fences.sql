CREATE TABLE IF NOT EXISTS hot_cache_fences (
    name VARCHAR(32) NOT NULL PRIMARY KEY
) ENGINE=InnoDB;
INSERT IGNORE INTO hot_cache_fences (name) VALUES ('music'), ('tags'), ('downloads'), ('archives');
