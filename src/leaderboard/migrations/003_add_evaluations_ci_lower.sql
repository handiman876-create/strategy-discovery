-- Fix 1 (ci_lower promotion gate): add a first-class ci_lower column to the
-- evaluations table.
--
-- Before this migration the bootstrap CI lower bound was only surfaced inside
-- the failed_gates TEXT column, and only when it FAILED a gate — so a passing
-- ci_lower left no trace and the metric could not be tracked historically.
-- The fast screen now promotes on ci_lower (edge separability) instead of
-- PF/score, which makes ci_lower a first-class metric worth persisting for
-- every eval row.
--
-- Nullable with no default: pre-existing rows keep NULL (ci_lower was genuinely
-- not recorded before this migration — NULL means "unknown", not 0.0). New rows
-- written by record_evaluation carry the real aggregate value.
--
-- The migration runner (db.py) wraps this script in BEGIN/COMMIT and appends
-- the schema_version row, so this file is just the DDL.

ALTER TABLE evaluations ADD COLUMN ci_lower REAL;
