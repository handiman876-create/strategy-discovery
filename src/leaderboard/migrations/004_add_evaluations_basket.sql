-- Fast-basket diversification (2026-07-17): record WHICH symbol basket produced
-- each evaluation.
--
-- Every fast eval through 2026-07-16 (221 rows) ran the same hardcoded roster,
-- ["AMD", "NFLX", "SPY", "QQQ", "NVDA"] — 3/5 high-beta tech. That basket rates
-- beta as signal: it promoted two strategies to canonical on ci_lower > 1.0 and
-- both then failed (d0cc300e5c07 fast 1.054 -> canonical 0.288; fdc88ceb54fd
-- fast 1.145 -> canonical 0.963). Re-run on the diversified basket, both are
-- screened out at the fast tier (0.218 / 0.634) — the fast tier starts agreeing
-- with canonical instead of contradicting it.
--
-- ci_lower is only comparable WITHIN a basket. Without these columns a
-- tech5_v1 row and a diverse8_v1 row are indistinguishable and silently rank
-- against each other — the exact failure this migration exists to prevent.
--
-- WHY TWO COLUMNS: basket_version is the readable name (what a human reads in
-- a query); basket_hash is order-independent proof the name still matches its
-- symbols (an in-place edit to KNOWN_BASKETS would otherwise let a label drift
-- from its contents undetected). See evaluation/baskets.py — basket_identity()
-- is the single writer of the pair for new rows.
--
-- BACKFILL IS SAFE HERE, unlike migration 003. 003 left ci_lower NULL because
-- the value was genuinely never recorded — NULL meant "unknown". Here the
-- basket IS recoverable: config_json records the symbol list on every row, and
-- all 221 fast rows were verified identical to tech5_v1 before writing this.
-- So the backfill restores a known fact rather than inventing one.
--
-- Hashes below are sha256(",".join(sorted(symbols)))[:12], computed from
-- evaluation.baskets.KNOWN_BASKETS. They are literals because migrations are
-- plain SQL; baskets.py is the source of truth and a test asserts they match.
--
-- The migration runner (db.py) wraps this script in BEGIN/COMMIT and appends
-- the schema_version row, so this file is just the DDL.

ALTER TABLE evaluations ADD COLUMN basket_version TEXT;
ALTER TABLE evaluations ADD COLUMN basket_hash TEXT;

-- Retired fast basket: ["AMD", "NFLX", "NVDA", "QQQ", "SPY"]
UPDATE evaluations
   SET basket_version = 'tech5_v1',
       basket_hash    = '81d68b662f8a'
 WHERE eval_type = 'fast'
   AND basket_version IS NULL;

-- Canonical and holdout have always run the 10-symbol sp500_phase2_seed42
-- roster; holdout differs by DATE RANGE (>= HOLDOUT_BOUNDARY), not by basket.
UPDATE evaluations
   SET basket_version = 'sp500_phase2_seed42',
       basket_hash    = 'b6db2ae8589f'
 WHERE eval_type IN ('canonical', 'holdout')
   AND basket_version IS NULL;
