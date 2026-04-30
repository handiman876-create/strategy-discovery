"""Write functions for the leaderboard: record_generation, record_evaluation,
transition_status.

Each public function runs as a single explicit transaction (BEGIN/COMMIT
inside the function body). Failures bubble up to the caller; the caller —
typically an integration hook in generator/pipeline.py or evaluation/pipeline.py
— decides whether to log a warning or to swallow the failure.

Status transitions (the strategies.status column and its companion *_at
timestamps) are written by exactly one helper, _apply_status_transition.
The public transition_status wraps it in its own transaction; record_evaluation
calls it inside its own transaction so the eval row and the auto-promotion
land atomically.

failed_gates JSON shape (encoded by record_evaluation, decoded by query.py):
    [{"name": str, "required": str, "actual": float, "deficit": float}, ...]
mirroring evaluation.scoring.FailedCondition.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Optional

from generator.spec import StrategySpec

from .models import (
    EVAL_TYPE_AUTO_TRANSITIONS,
    LEGAL_MANUAL_TRANSITIONS,
    EvaluationRecord,
    GenerationMetadata,
    Status,
    _utcnow_iso,
)

logger = logging.getLogger(__name__)


# ── Status → companion *_at column on strategies ─────────────────────────────

_STATUS_TIMESTAMP_COLUMN: dict[Status, Optional[str]] = {
    Status.GENERATED: None,  # set by initial INSERT, not by transition
    Status.FAST_EVALUATED: "fast_evaluated_at",
    Status.CANONICAL_EVALUATED: "canonical_evaluated_at",
    Status.HOLDOUT_EVALUATED: "holdout_evaluated_at",
    Status.PAPER_CANDIDATE: "paper_candidate_at",
    Status.PAPER_TRADING: "paper_started_at",
    Status.PAPER_COMPLETE: "paper_ended_at",
    Status.REAL_MONEY_CANDIDATE: None,  # supplement reserves no *_at; promotion is logged via reason
    Status.ARCHIVED: "archived_at",
}


# ── Public API ───────────────────────────────────────────────────────────────


def record_generation(
    conn: sqlite3.Connection,
    spec: StrategySpec,
    behavioral_hash: str,
    metadata: GenerationMetadata,
    *,
    imported_from: Optional[str] = None,
) -> int:
    """Persist one generation event. Upserts the strategy row by
    behavioral_hash (insert if new, update last_seen_at + generation_count++
    if existing) and inserts a generation row. Returns the new generation id.

    Single transaction: if the generation insert fails, the strategy upsert
    is rolled back too."""
    logger.debug("recording generation for hash %s", behavioral_hash)
    spec_json = spec.model_dump_json()
    if not spec.timeframes:
        raise ValueError(
            f"spec {spec.name!r} has no timeframes; cannot record without a timeframe"
        )
    timeframe = spec.timeframes[0]

    conn.execute("BEGIN")
    try:
        # UPSERT strategy. ON CONFLICT updates last_seen_at and bumps
        # generation_count by 1 — without touching first_generated_at,
        # status, or the *_at columns. spec_json and name are also
        # refreshed in case the generation produced a textually different
        # spec that hashes to the same trades.
        conn.execute(
            """
            INSERT INTO strategies (
                behavioral_hash, name, archetype, timeframe, spec_json,
                first_generated_at, last_seen_at, generation_count,
                status, imported_from
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'generated', ?)
            ON CONFLICT (behavioral_hash) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                generation_count = strategies.generation_count + 1,
                name = excluded.name,
                spec_json = excluded.spec_json
            """,
            (
                behavioral_hash,
                spec.name,
                spec.archetype,
                timeframe,
                spec_json,
                metadata.generated_at,
                metadata.generated_at,
                imported_from,
            ),
        )

        cur = conn.execute(
            """
            INSERT INTO generations (
                strategy_hash, generated_at, archetype, requested_timeframe,
                model_version, prompt_hash, cost_usd, retry_count,
                duration_seconds,
                stringification_firings, kwarg_validator_firings,
                unreachable_default_firings,
                raw_response_path, spec_path, imported_from
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                behavioral_hash,
                metadata.generated_at,
                metadata.archetype,
                metadata.requested_timeframe,
                metadata.model_version,
                metadata.prompt_hash,
                metadata.cost_usd,
                metadata.retry_count,
                metadata.duration_seconds,
                metadata.stringification_firings,
                metadata.kwarg_validator_firings,
                metadata.unreachable_default_firings,
                metadata.raw_response_path,
                metadata.spec_path,
                imported_from,
            ),
        )
        gen_id = cur.lastrowid
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise

    logger.debug("recorded generation id %d", gen_id)
    return gen_id


def record_evaluation(
    conn: sqlite3.Connection,
    strategy_hash: str,
    result: EvaluationRecord,
    eval_type: str,
    *,
    imported_from: Optional[str] = None,
) -> int:
    """Persist one evaluation event and, if applicable, advance the strategy's
    status. Returns the new evaluation id.

    Auto-transition rules (from leaderboard.models.EVAL_TYPE_AUTO_TRANSITIONS):
        eval_type='fast'      moves status from 'generated' → 'fast_evaluated'
        eval_type='canonical' moves 'generated' or 'fast_evaluated' → 'canonical_evaluated'
        eval_type='holdout'   moves any pre-paper status → 'holdout_evaluated'
    The transition is monotonic: a strategy that has already moved past the
    auto-target is left where it is, and the eval row is still recorded.

    Single transaction — if the auto-transition fails, the eval insert is
    rolled back too. Status writes go through _apply_status_transition so
    transition_status remains the only writer of status-change timestamps."""
    if eval_type not in EVAL_TYPE_AUTO_TRANSITIONS:
        raise ValueError(
            f"unknown eval_type {eval_type!r}; expected one of "
            f"{sorted(EVAL_TYPE_AUTO_TRANSITIONS)}"
        )
    logger.debug("recording %s evaluation for hash %s", eval_type, strategy_hash)
    failed_gates_json = (
        json.dumps(result.failed_conditions) if result.failed_conditions else None
    )

    conn.execute("BEGIN")
    try:
        # Existence + status read up front: gives a friendlier error than the
        # FK IntegrityError we'd otherwise see on the eval INSERT, and
        # consolidates the auto-transition status read into the same query.
        row = conn.execute(
            "SELECT status FROM strategies WHERE behavioral_hash = ?",
            (strategy_hash,),
        ).fetchone()
        if row is None:
            raise ValueError(
                f"cannot record evaluation: strategy {strategy_hash!r} not found"
            )

        cur = conn.execute(
            """
            INSERT INTO evaluations (
                strategy_hash, eval_type, evaluated_at, duration_seconds,
                n_oos_trades, median_pf, score, promising, failed_gates,
                results_dir, config_json, imported_from
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                strategy_hash,
                eval_type,
                result.evaluated_at,
                result.duration_seconds,
                result.n_oos_trades,
                result.median_pf,
                result.score,
                1 if result.promising else 0,
                failed_gates_json,
                result.results_dir,
                result.config_json,
                imported_from,
            ),
        )
        eval_id = cur.lastrowid

        # Auto-promote status if the strategy is in a predecessor state.
        target_status, predecessors = EVAL_TYPE_AUTO_TRANSITIONS[eval_type]
        current = Status(row["status"])
        if current in predecessors:
            _apply_status_transition(
                conn,
                strategy_hash,
                target_status,
                timestamp=result.evaluated_at,
            )

        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise

    logger.debug("recorded evaluation id %d", eval_id)
    return eval_id


def transition_status(
    conn: sqlite3.Connection,
    strategy_hash: str,
    new_status: Status,
    *,
    paper_outcome: Optional[str] = None,
    paper_notes: Optional[str] = None,
    archive_reason: Optional[str] = None,
) -> None:
    """The single public path for manual status changes (CLI promote/archive).
    Validates that new_status is reachable from the current state via either
    the eval-driven auto-transition matrix or the manual-transition matrix.

    Special rule: REAL_MONEY_CANDIDATE additionally requires the strategy's
    paper_outcome column to equal 'pass' — that constraint depends on row
    data, not state alone, so it lives here rather than in the matrix.

    Side effects per target:
        FAST/CANONICAL/HOLDOUT_EVALUATED → corresponding *_at column
        PAPER_CANDIDATE                  → paper_candidate_at
        PAPER_TRADING                    → paper_started_at
        PAPER_COMPLETE                   → paper_ended_at + paper_outcome + paper_notes
        ARCHIVED                         → archived_at + archive_reason
    Idempotent on no-op transitions (current == new_status)."""
    logger.debug(
        "transitioning hash %s to %s", strategy_hash, new_status.value
    )
    conn.execute("BEGIN")
    try:
        _apply_status_transition(
            conn,
            strategy_hash,
            new_status,
            paper_outcome=paper_outcome,
            paper_notes=paper_notes,
            archive_reason=archive_reason,
        )
        conn.execute("COMMIT")
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise

    logger.debug("transitioned hash %s to %s", strategy_hash, new_status.value)


# ── Internals ────────────────────────────────────────────────────────────────


def _apply_status_transition(
    conn: sqlite3.Connection,
    strategy_hash: str,
    new_status: Status,
    *,
    timestamp: Optional[str] = None,
    paper_outcome: Optional[str] = None,
    paper_notes: Optional[str] = None,
    archive_reason: Optional[str] = None,
) -> None:
    """Single source of truth for status writes. Caller manages the enclosing
    transaction. Validates legality, writes the new status, the appropriate
    *_at column, and any side-effect columns (paper_outcome / paper_notes /
    archive_reason). Raises ValueError on illegal transitions or missing
    strategy."""
    row = conn.execute(
        "SELECT status, paper_outcome FROM strategies WHERE behavioral_hash = ?",
        (strategy_hash,),
    ).fetchone()
    if row is None:
        raise ValueError(f"strategy not found: {strategy_hash!r}")

    current = Status(row["status"])
    if new_status == current:
        return  # no-op

    if not _is_legal_transition(current, new_status):
        legal_next = _legal_next_states(current)
        raise ValueError(
            f"illegal transition: {current.value} → {new_status.value}; "
            f"legal next states from {current.value}: "
            f"{sorted(s.value for s in legal_next)}"
        )

    # PAPER_COMPLETE → REAL_MONEY_CANDIDATE requires paper_outcome=='pass'.
    if (
        new_status is Status.REAL_MONEY_CANDIDATE
        and row["paper_outcome"] != "pass"
    ):
        raise ValueError(
            "promotion to real_money_candidate requires paper_outcome='pass'; "
            f"current paper_outcome is {row['paper_outcome']!r}"
        )

    ts = timestamp or _utcnow_iso()
    set_clauses = ["status = ?"]
    params: list = [new_status.value]

    at_col = _STATUS_TIMESTAMP_COLUMN.get(new_status)
    if at_col is not None:
        set_clauses.append(f"{at_col} = ?")
        params.append(ts)

    if new_status is Status.PAPER_COMPLETE:
        if paper_outcome is not None:
            set_clauses.append("paper_outcome = ?")
            params.append(paper_outcome)
        if paper_notes is not None:
            set_clauses.append("paper_notes = ?")
            params.append(paper_notes)

    if new_status is Status.ARCHIVED and archive_reason is not None:
        set_clauses.append("archive_reason = ?")
        params.append(archive_reason)

    params.append(strategy_hash)
    conn.execute(
        f"UPDATE strategies SET {', '.join(set_clauses)} WHERE behavioral_hash = ?",
        params,
    )


def _is_legal_transition(current: Status, target: Status) -> bool:
    """True iff target is reachable from current via either the auto-transition
    matrix (eval-driven) or the manual-transition matrix (CLI-driven)."""
    if target in LEGAL_MANUAL_TRANSITIONS.get(current, frozenset()):
        return True
    for _eval_type, (auto_target, predecessors) in EVAL_TYPE_AUTO_TRANSITIONS.items():
        if target == auto_target and current in predecessors:
            return True
    return False


def _legal_next_states(current: Status) -> set[Status]:
    """Union of manual + auto next-state sets from `current`. Used for
    error messages so callers see what was actually allowed."""
    out = set(LEGAL_MANUAL_TRANSITIONS.get(current, frozenset()))
    for _eval_type, (auto_target, predecessors) in EVAL_TYPE_AUTO_TRANSITIONS.items():
        if current in predecessors:
            out.add(auto_target)
    return out
