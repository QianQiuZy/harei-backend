-- Existing accounts keep automatic monthly address collection enabled.
SET @ddl = IF(
    EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'member_accounts' AND column_name = 'auto_fill_address'),
    'SELECT 1', 'ALTER TABLE member_accounts ADD COLUMN auto_fill_address BOOL NOT NULL DEFAULT TRUE'
);
PREPARE gift_migration FROM @ddl;
EXECUTE gift_migration;
DEALLOCATE PREPARE gift_migration;
