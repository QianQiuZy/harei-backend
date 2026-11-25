-- Historical purchase aggregates have no payment evidence. Leave paid_level NULL.
SET @ddl = IF(EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE()
    AND table_name = 'gift_purchase_levels' AND column_name = 'paid_level'),
    'SELECT 1', 'ALTER TABLE gift_purchase_levels ADD paid_level INTEGER NULL');
PREPARE gift_migration FROM @ddl;
EXECUTE gift_migration;
DEALLOCATE PREPARE gift_migration;

SET @ddl = IF(EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE()
    AND table_name = 'gift_purchase_levels' AND column_name = 'applied_level'),
    'SELECT 1', 'ALTER TABLE gift_purchase_levels ADD applied_level INTEGER NULL');
PREPARE gift_migration FROM @ddl;
EXECUTE gift_migration;
DEALLOCATE PREPARE gift_migration;

SET @ddl = IF(EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE()
    AND table_name = 'gift_purchase_levels' AND column_name = 'nickname'),
    'SELECT 1', 'ALTER TABLE gift_purchase_levels ADD nickname VARCHAR(255) NULL');
PREPARE gift_migration FROM @ddl;
EXECUTE gift_migration;
DEALLOCATE PREPARE gift_migration;

SET @ddl = IF(EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE()
    AND table_name = 'gift_months' AND column_name = 'roster_finalized_at'),
    'SELECT 1', 'ALTER TABLE gift_months ADD roster_finalized_at DATETIME NULL');
PREPARE gift_migration FROM @ddl;
EXECUTE gift_migration;
DEALLOCATE PREPARE gift_migration;

SET @ddl = IF(EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE()
    AND table_name = 'member_gifts' AND column_name = 'needs_reship'),
    'SELECT 1', 'ALTER TABLE member_gifts ADD needs_reship BOOL NOT NULL DEFAULT FALSE');
PREPARE gift_migration FROM @ddl;
EXECUTE gift_migration;
DEALLOCATE PREPARE gift_migration;

SET @ddl = IF(EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE()
    AND table_name = 'member_gifts' AND column_name = 'roster_level' AND is_nullable = 'YES'),
    'SELECT 1', 'ALTER TABLE member_gifts MODIFY roster_level INTEGER NULL');
PREPARE gift_migration FROM @ddl;
EXECUTE gift_migration;
DEALLOCATE PREPARE gift_migration;

SET @ddl = IF(EXISTS (SELECT 1 FROM information_schema.statistics WHERE table_schema = DATABASE()
    AND table_name = 'gift_purchase_levels' AND index_name = 'ix_gift_purchase_pending'),
    'SELECT 1', 'CREATE INDEX ix_gift_purchase_pending ON gift_purchase_levels (room_id, applied_level, month)');
PREPARE gift_migration FROM @ddl;
EXECUTE gift_migration;
DEALLOCATE PREPARE gift_migration;
