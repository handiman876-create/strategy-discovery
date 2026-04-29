"""Plain dataclasses + Status enum + state machine constants.

No ORM. Conversion between sqlite3.Row instances and dataclasses lives in
record.py and query.py — keeping this module side-effect-free makes the
state machine independently testable from the DB layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


def _utcnow_iso() -> str:
    """ISO-8601 UTC timestamp for default timestamp factories. Module-level
    so tests and callers can monkeypatch a fixed clock if needed."""
    return datetime.now(timezone.utc).isoformat()


class Status(str, Enum):
    """The 9 strategy lifecycle states from the supplement's CHECK constraint.
    The (str, Enum) hybrid lets `row["status"] == Status.GENERATED` compare
    equal to the TEXT pulled out of SQLite, so callers don't need to wrap or
    unwrap to compare."""

    GENERATED = "generated"
    FAST_EVALUATED = "fast_evaluated"
    CANONICAL_EVALUATED = "canonical_evaluated"
    HOLDOUT_EVALUATED = "holdout_evaluated"
    PAPER_CANDIDATE = "paper_candidate"
    PAPER_TRADING = "paper_trading"
    PAPER_COMPLETE = "paper_complete"
    REAL_MONEY_CANDIDATE = "real_money_candidate"
    ARCHIVED = "archived"


# Auto-transitions triggered by record_evaluation.
#   eval_type → (target_status, set_of_predecessor_statuses_that_advance)
# A 'fast' eval moves a `generated` strategy to `fast_evaluated`; a strategy
# already past that point is left where it is (and the evaluation row is still
# recorded). Same monotonic shape for 'canonical' and 'holdout'.
EVAL_TYPE_AUTO_TRANSITIONS: dict[str, tuple[Status, frozenset[Status]]] = {
    "fast": (Status.FAST_EVALUATED, frozenset({Status.GENERATED})),
    "canonical": (
        Status.CANONICAL_EVALUATED,
        frozenset({Status.GENERATED, Status.FAST_EVALUATED}),
    ),
    "holdout": (
        Status.HOLDOUT_EVALUATED,
        frozenset(
            {Status.GENERATED, Status.FAST_EVALUATED, Status.CANONICAL_EVALUATED}
        ),
    ),
}


# Manual transitions issued via the CLI (`promote` / `archive`). Each entry
# is the legal next-state set from the keyed state. ARCHIVED is terminal
# (empty next-set) and reachable from every non-archived state. The
# PAPER_COMPLETE → REAL_MONEY_CANDIDATE step additionally requires
# paper_outcome == 'pass' — that constraint lives in transition_status,
# not in the matrix, because it depends on row data rather than on state
# alone.
LEGAL_MANUAL_TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.GENERATED: frozenset({Status.ARCHIVED}),
    Status.FAST_EVALUATED: frozenset({Status.ARCHIVED}),
    Status.CANONICAL_EVALUATED: frozenset({Status.ARCHIVED}),
    Status.HOLDOUT_EVALUATED: frozenset(
        {Status.PAPER_CANDIDATE, Status.ARCHIVED}
    ),
    Status.PAPER_CANDIDATE: frozenset(
        {Status.PAPER_TRADING, Status.ARCHIVED}
    ),
    Status.PAPER_TRADING: frozenset(
        {Status.PAPER_COMPLETE, Status.ARCHIVED}
    ),
    Status.PAPER_COMPLETE: frozenset(
        {Status.REAL_MONEY_CANDIDATE, Status.ARCHIVED}
    ),
    Status.REAL_MONEY_CANDIDATE: frozenset({Status.ARCHIVED}),
    Status.ARCHIVED: frozenset(),
}


@dataclass
class Strategy:
    behavioral_hash: str
    name: str
    archetype: str
    timeframe: str
    spec_json: str
    first_generated_at: str  # ISO-8601 string; SQLite TIMESTAMP is TEXT
    last_seen_at: str
    generation_count: int = 1
    status: Status = Status.GENERATED
    fast_evaluated_at: Optional[str] = None
    canonical_evaluated_at: Optional[str] = None
    holdout_evaluated_at: Optional[str] = None
    paper_candidate_at: Optional[str] = None
    paper_started_at: Optional[str] = None
    paper_ended_at: Optional[str] = None
    archived_at: Optional[str] = None
    paper_outcome: Optional[str] = None  # 'pass' | 'fail' | 'inconclusive' | None
    paper_notes: Optional[str] = None
    archive_reason: Optional[str] = None
    imported_from: Optional[str] = None  # 'backfill' or None


@dataclass
class Generation:
    """One model-generation event. id is None until the row is inserted."""

    id: Optional[int]
    strategy_hash: str
    generated_at: str
    archetype: str
    model_version: str
    prompt_hash: str
    requested_timeframe: Optional[str] = None
    cost_usd: Optional[float] = None
    retry_count: int = 0
    duration_seconds: Optional[float] = None
    stringification_firings: int = 0
    kwarg_validator_firings: int = 0
    unreachable_default_firings: int = 0
    raw_response_path: Optional[str] = None
    spec_path: Optional[str] = None
    imported_from: Optional[str] = None


@dataclass
class Evaluation:
    """One evaluation event of any tier. id is None until the row is inserted.
    `promising` is bool here; record.py converts to/from the schema's
    INTEGER CHECK (0|1)."""

    id: Optional[int]
    strategy_hash: str
    eval_type: str  # 'fast' | 'canonical' | 'holdout'
    evaluated_at: str
    n_oos_trades: int
    promising: bool
    results_dir: str
    config_json: str
    duration_seconds: Optional[float] = None
    median_pf: Optional[float] = None
    score: Optional[float] = None
    # JSON-encoded list of {"name", "required", "actual", "deficit"} dicts
    # mirroring evaluation.scoring.FailedCondition. Encoded by record.py;
    # query.py docstrings show example SQLite JSON1 patterns.
    failed_gates: Optional[str] = None
    imported_from: Optional[str] = None


# ── Inputs to record.py ──────────────────────────────────────────────────────


@dataclass
class GenerationMetadata:
    """Inputs to record_generation that aren't derivable from the StrategySpec
    or the behavioral hash. Field ordering follows Python's required-before-
    defaults rule; semantic groupings are noted in comments."""

    # Required — these come from the call site (claude_client / discover.py)
    model_version: str
    prompt_hash: str
    archetype: str
    cost_usd: float
    retry_count: int
    duration_seconds: float

    # Quirk firing counters captured during this generation. Default 0 so
    # the call site only has to pass them when they're nonzero.
    stringification_firings: int = 0
    kwarg_validator_firings: int = 0
    unreachable_default_firings: int = 0

    # Optional file refs; None when the generation didn't archive anything.
    raw_response_path: Optional[str] = None
    spec_path: Optional[str] = None

    # Set when discover.py was invoked with --timeframe X. None means the
    # model picked freely.
    requested_timeframe: Optional[str] = None

    # Defaults to UTC-now if the caller doesn't pin it. Integration hooks
    # should pass the timestamp captured at generation start so the row
    # matches archived artifacts.
    generated_at: str = field(default_factory=_utcnow_iso)


@dataclass
class EvaluationResult:
    """Inputs to record_evaluation alongside (strategy_hash, eval_type).

    `failed_conditions` mirrors evaluation.scoring.FailedCondition; pass an
    asdict-style list (keys: name, required, actual, deficit). record.py
    JSON-encodes it into the schema's failed_gates TEXT column."""

    n_oos_trades: int
    promising: bool
    results_dir: str
    config_json: str
    median_pf: Optional[float] = None
    score: Optional[float] = None
    duration_seconds: Optional[float] = None
    failed_conditions: list[dict[str, Any]] = field(default_factory=list)
    evaluated_at: str = field(default_factory=_utcnow_iso)


# ── Outputs from query.py ────────────────────────────────────────────────────


@dataclass
class ArchetypeSummary:
    """Aggregate metrics for one archetype, optionally filtered by timeframe
    and a since-date. Returned by query.get_archetype_summary.

    n_evaluations_by_type and n_promising_by_type always have the three keys
    ('fast', 'canonical', 'holdout') present, even when zero. quirk_counts
    always has the three quirk keys ('stringification', 'kwarg_validator',
    'unreachable_default') present. by_status only includes statuses that
    actually appear in scope — no zero-fill."""

    archetype: str
    timeframe: Optional[str]
    since: Optional[str]
    n_strategies: int
    n_generations: int
    n_evaluations_by_type: dict[str, int]
    n_promising_by_type: dict[str, int]
    by_status: dict[str, int]
    median_score: Optional[float]
    total_cost_usd: Optional[float]
    quirk_counts: dict[str, int]
