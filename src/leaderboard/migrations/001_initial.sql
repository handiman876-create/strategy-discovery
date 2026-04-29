-- Phase 4 initial schema.
--
-- Four tables — strategies, generations, evaluations, paper_trading_sessions —
-- plus the indexes the supplement specifies. The schema_version table is
-- created by the migration runner (see leaderboard/db.py); migration files
-- contain DDL only, so a partial-failure leaves a known-bad state rather
-- than ambiguously claiming itself applied.

-- ── strategies ───────────────────────────────────────────────────────────────

CREATE TABLE strategies (
    behavioral_hash TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    archetype TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    first_generated_at TIMESTAMP NOT NULL,
    last_seen_at TIMESTAMP NOT NULL,
    generation_count INTEGER NOT NULL DEFAULT 1,

    status TEXT NOT NULL DEFAULT 'generated' CHECK (status IN (
        'generated',
        'fast_evaluated',
        'canonical_evaluated',
        'holdout_evaluated',
        'paper_candidate',
        'paper_trading',
        'paper_complete',
        'real_money_candidate',
        'archived'
    )),

    fast_evaluated_at TIMESTAMP,
    canonical_evaluated_at TIMESTAMP,
    holdout_evaluated_at TIMESTAMP,
    paper_candidate_at TIMESTAMP,
    paper_started_at TIMESTAMP,
    paper_ended_at TIMESTAMP,
    archived_at TIMESTAMP,

    paper_outcome TEXT CHECK (paper_outcome IS NULL OR paper_outcome IN ('pass', 'fail', 'inconclusive')),
    paper_notes TEXT,
    archive_reason TEXT,

    imported_from TEXT
);

CREATE INDEX idx_strategies_archetype ON strategies(archetype);
CREATE INDEX idx_strategies_status ON strategies(status);
CREATE INDEX idx_strategies_timeframe ON strategies(timeframe);
CREATE INDEX idx_strategies_first_generated ON strategies(first_generated_at);

-- ── generations ──────────────────────────────────────────────────────────────

CREATE TABLE generations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_hash TEXT NOT NULL REFERENCES strategies(behavioral_hash),
    generated_at TIMESTAMP NOT NULL,
    archetype TEXT NOT NULL,
    requested_timeframe TEXT,
    model_version TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    cost_usd REAL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL,
    stringification_firings INTEGER NOT NULL DEFAULT 0,
    kwarg_validator_firings INTEGER NOT NULL DEFAULT 0,
    unreachable_default_firings INTEGER NOT NULL DEFAULT 0,
    raw_response_path TEXT,
    spec_path TEXT,
    imported_from TEXT
);

CREATE INDEX idx_generations_strategy ON generations(strategy_hash);
CREATE INDEX idx_generations_archetype ON generations(archetype);
CREATE INDEX idx_generations_generated_at ON generations(generated_at);
CREATE INDEX idx_generations_prompt_hash ON generations(prompt_hash);

-- ── evaluations ──────────────────────────────────────────────────────────────

CREATE TABLE evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_hash TEXT NOT NULL REFERENCES strategies(behavioral_hash),
    eval_type TEXT NOT NULL CHECK (eval_type IN ('fast', 'canonical', 'holdout')),
    evaluated_at TIMESTAMP NOT NULL,
    duration_seconds REAL,
    n_oos_trades INTEGER NOT NULL,
    median_pf REAL,
    score REAL,
    promising INTEGER NOT NULL CHECK (promising IN (0, 1)),
    failed_gates TEXT,
    results_dir TEXT NOT NULL,
    config_json TEXT NOT NULL,
    imported_from TEXT
);

CREATE INDEX idx_evaluations_strategy ON evaluations(strategy_hash);
CREATE INDEX idx_evaluations_type_date ON evaluations(eval_type, evaluated_at);
CREATE INDEX idx_evaluations_promising ON evaluations(promising) WHERE promising = 1;

-- ── paper_trading_sessions (stub for Phase 4.5+) ─────────────────────────────

CREATE TABLE paper_trading_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_hash TEXT NOT NULL REFERENCES strategies(behavioral_hash),
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    notes TEXT
);
