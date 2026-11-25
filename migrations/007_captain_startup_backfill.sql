-- Rows written before this upgrade are eligible under the agreed legacy rule.
-- Old rows keep TRUE when the default for subsequent inserts is switched to FALSE.
SET @ddl = IF(EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE()
    AND table_name = 'captains' AND column_name = 'room_id'),
    'SELECT 1', 'ALTER TABLE captains ADD room_id BIGINT NULL');
PREPARE gift_migration FROM @ddl;
EXECUTE gift_migration;
DEALLOCATE PREPARE gift_migration;

SET @ddl = IF(EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE()
    AND table_name = 'captains' AND column_name = 'paid_price'),
    'SELECT 1', 'ALTER TABLE captains ADD paid_price BIGINT NULL');
PREPARE gift_migration FROM @ddl;
EXECUTE gift_migration;
DEALLOCATE PREPARE gift_migration;

SET @ddl = IF(EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE()
    AND table_name = 'captains' AND column_name = 'legacy_assumed_paid'),
    'SELECT 1', 'ALTER TABLE captains ADD legacy_assumed_paid BOOL NOT NULL DEFAULT TRUE');
PREPARE gift_migration FROM @ddl;
EXECUTE gift_migration;
DEALLOCATE PREPARE gift_migration;

SET @ddl = IF(EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE()
    AND table_name = 'captains' AND column_name = 'legacy_assumed_paid' AND column_default = '1'),
    'ALTER TABLE captains ALTER COLUMN legacy_assumed_paid SET DEFAULT FALSE', 'SELECT 1');
PREPARE gift_migration FROM @ddl;
EXECUTE gift_migration;
DEALLOCATE PREPARE gift_migration;
