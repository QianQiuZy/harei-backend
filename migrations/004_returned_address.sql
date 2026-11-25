-- Keep the returned recipient snapshot available to its owner until they save a revision.
SET @ddl = IF(
    EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() AND table_name = 'member_gifts' AND column_name = 'returned_address'),
    'SELECT 1', 'ALTER TABLE member_gifts ADD COLUMN returned_address TEXT NULL'
);
PREPARE gift_migration FROM @ddl;
EXECUTE gift_migration;
DEALLOCATE PREPARE gift_migration;
