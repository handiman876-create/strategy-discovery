-- Phase 4 step 10 commit 3: rename strategies.behavioral_hash → strategy_hash.
--
-- SQLite 3.25+ ALTER TABLE RENAME COLUMN automatically updates references
-- in this table's schema, in triggers/views, AND in the FK clauses of
-- OTHER tables that reference the renamed column. Both `generations` and
-- `evaluations` have `REFERENCES strategies(behavioral_hash)` clauses;
-- after this migration those become `REFERENCES strategies(strategy_hash)`
-- automatically — no manual FK rewrite, no foreign_keys pragma juggling.
--
-- Verified empirically against /tmp/smoke_lb.db (5 strategies +
-- 171 generations + 32 evaluations from the prior step-10 smoke test):
-- row counts unchanged, PRAGMA foreign_key_check reports no violations,
-- FK enforcement still active (orphan inserts correctly rejected post-rename).
--
-- The migration runner (db.py) wraps this script in BEGIN/COMMIT and
-- appends the schema_version row, so this file is just the DDL.

ALTER TABLE strategies RENAME COLUMN behavioral_hash TO strategy_hash;
