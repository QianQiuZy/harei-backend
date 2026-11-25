-- Run in the application database after 001_member_gifts.sql.
SET @ddl = IF(
    EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'member_uid_bindings' AND column_name = 'username'),
    'SELECT 1', 'ALTER TABLE member_uid_bindings ADD COLUMN username VARCHAR(255)'
);
PREPARE gift_migration FROM @ddl;
EXECUTE gift_migration;
DEALLOCATE PREPARE gift_migration;

SET @additions = CONCAT_WS(', ',
    IF(EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'member_gifts' AND column_name = 'bound_username'), NULL, 'ADD COLUMN bound_username VARCHAR(255)'),
    IF(EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'member_gifts' AND column_name = 'extension_until'), NULL, 'ADD COLUMN extension_until DATETIME'),
    IF(EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'member_gifts' AND column_name = 'refill_requested'), NULL, 'ADD COLUMN refill_requested BOOL NOT NULL DEFAULT FALSE')
);
SET @ddl = IF(@additions = '', 'SELECT 1', CONCAT('ALTER TABLE member_gifts ', @additions));
PREPARE gift_migration FROM @ddl;
EXECUTE gift_migration;
DEALLOCATE PREPARE gift_migration;

-- Older releases recorded the scope and end of each manual extension in the audit log.
UPDATE member_gifts AS g
SET g.extension_until = (
    SELECT MAX(CAST(a.details->>'$.until' AS DATETIME))
    FROM member_gift_audit AS a
    WHERE a.action = 'extend' AND a.details->'$.ready_count' IS NULL
      AND a.subject IN (CONCAT(g.month, '/*'), CONCAT(g.month, '/', g.uid))
)
WHERE g.extension_until IS NULL AND EXISTS (
    SELECT 1 FROM member_gift_audit AS a
    WHERE a.action = 'extend' AND a.details->'$.ready_count' IS NULL
      AND a.subject IN (CONCAT(g.month, '/*'), CONCAT(g.month, '/', g.uid))
);
