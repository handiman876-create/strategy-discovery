-- Free-text run label per evaluation (2026-09-23), set by `discover.py --note`.
--
-- The first volume-archetype test ran three discover.py passes that could not
-- be told apart from later nightly volume evals except by an evaluated_at
-- window. A label lets a query select a run directly: mutation rounds, A/B
-- tests, archetype experiments.
--
-- WHY NOT imported_from: that column is PROVENANCE — it marks a replayed or
-- re-screened eval ('recovered:<src_file>') so it isn't mistaken for a fresh
-- nightly one. A note is a label on a fresh eval. Overloading imported_from
-- would make every labelled run look like a replay.
--
-- Nullable, no default, no backfill: existing rows were never labelled, and
-- NULL means exactly that.
--
-- The migration runner (db.py) wraps this script in BEGIN/COMMIT and appends
-- the schema_version row, so this file is just the DDL.

ALTER TABLE evaluations ADD COLUMN note TEXT;
