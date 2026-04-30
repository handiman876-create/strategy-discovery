"""Tests for record_generation, record_evaluation, transition_status."""

from __future__ import annotations

import json
import sqlite3

import pytest

from generator.spec import IndicatorSpec, ParameterSpec, StrategySpec
from leaderboard.db import initialize_db
from leaderboard.models import EvaluationRecord, GenerationMetadata, Status
from leaderboard.record import (
    record_evaluation,
    record_generation,
    transition_status,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "lb.db"
    c = initialize_db(db)
    yield c
    c.close()


def _spec(name: str = "demo_strategy"):
    return StrategySpec(
        name=name,
        archetype="mean_reversion",
        thesis="Demo strategy used by leaderboard record tests; not for production.",
        supported_assets=["stocks"],
        timeframes=["1d"],
        parameters=[ParameterSpec(name="x", type="float", default=1.0)],
        indicators=[IndicatorSpec(name="rsi_14", type="rsi", params={"period": 14})],
        entry_long={
            "op": "compare", "operator": "<",
            "lhs": {"op": "indicator", "name": "rsi_14"},
            "rhs": {"op": "param", "name": "x"},
        },
    )


def _meta(**overrides):
    base = dict(
        model_version="claude-sonnet-4-6",
        prompt_hash="ph_001",
        archetype="mean_reversion",
        cost_usd=0.05,
        retry_count=0,
        duration_seconds=12.3,
    )
    base.update(overrides)
    return GenerationMetadata(**base)


def _result(**overrides):
    base = dict(
        n_oos_trades=42,
        promising=True,
        results_dir="/tmp/eval/demo",
        config_json="{}",
        median_pf=1.5,
        score=2.0,
        duration_seconds=8.0,
    )
    base.update(overrides)
    return EvaluationRecord(**base)


def _insert_strategy(conn, hash_="h1", status=Status.GENERATED, paper_outcome=None):
    conn.execute(
        "INSERT INTO strategies (behavioral_hash, name, archetype, timeframe, "
        "spec_json, first_generated_at, last_seen_at, status, paper_outcome) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'), ?, ?)",
        (hash_, "demo", "mean_reversion", "1d", "{}", status.value, paper_outcome),
    )


# ── record_generation ────────────────────────────────────────────────────────


def test_record_generation_inserts_strategy_and_generation(conn):
    spec = _spec()
    gen_id = record_generation(conn, spec, behavioral_hash="abc123", metadata=_meta())
    assert gen_id == 1

    s = conn.execute(
        "SELECT * FROM strategies WHERE behavioral_hash = ?", ("abc123",)
    ).fetchone()
    assert s["name"] == "demo_strategy"
    assert s["archetype"] == "mean_reversion"
    assert s["timeframe"] == "1d"
    assert s["generation_count"] == 1
    assert s["status"] == "generated"
    assert s["spec_json"]  # populated

    g = conn.execute("SELECT * FROM generations WHERE id = ?", (gen_id,)).fetchone()
    assert g["strategy_hash"] == "abc123"
    assert g["model_version"] == "claude-sonnet-4-6"
    assert g["prompt_hash"] == "ph_001"
    assert g["cost_usd"] == 0.05
    assert g["retry_count"] == 0


def test_record_generation_upserts_existing_strategy(conn):
    spec = _spec()
    record_generation(conn, spec, behavioral_hash="abc123", metadata=_meta())
    # Second call: same behavioral hash, different prompt → bumps gen count.
    record_generation(
        conn, spec, behavioral_hash="abc123",
        metadata=_meta(prompt_hash="ph_002", retry_count=2),
    )

    s = conn.execute(
        "SELECT generation_count, name FROM strategies WHERE behavioral_hash = ?",
        ("abc123",),
    ).fetchone()
    assert s["generation_count"] == 2

    rows = conn.execute(
        "SELECT prompt_hash FROM generations WHERE strategy_hash = ? ORDER BY id",
        ("abc123",),
    ).fetchall()
    assert [r["prompt_hash"] for r in rows] == ["ph_001", "ph_002"]


def test_record_generation_rolls_back_on_failure(conn):
    """If the generation insert fails (e.g. FK to a hash that fails to be
    created), the strategy upsert must roll back too. Provoke this by
    forcing the generation INSERT to violate an FK by patching the
    strategies row out from under it mid-call."""
    spec = _spec()
    # Pre-populate a strategy that will be upserted; then insert a generation
    # with a NULL model_version (NOT NULL violation) to force the second
    # statement of the transaction to fail.
    bad_meta = _meta()
    bad_meta.model_version = None  # type: ignore[assignment]

    with pytest.raises(sqlite3.IntegrityError):
        record_generation(conn, spec, behavioral_hash="rb_hash", metadata=bad_meta)

    # Strategy upsert must have been rolled back: hash should not exist.
    row = conn.execute(
        "SELECT 1 FROM strategies WHERE behavioral_hash = ?", ("rb_hash",)
    ).fetchone()
    assert row is None


def test_record_generation_rejects_spec_with_no_timeframes(conn):
    spec = _spec()
    spec.timeframes = []  # type: ignore[assignment]
    with pytest.raises(ValueError, match="no timeframes"):
        record_generation(conn, spec, behavioral_hash="h", metadata=_meta())


# ── record_evaluation ────────────────────────────────────────────────────────


def test_record_evaluation_inserts_row_and_serializes_failed_gates_as_json(conn):
    _insert_strategy(conn, "h1")
    res = _result(
        promising=False,
        failed_conditions=[
            {"name": "score", "required": ">1.5", "actual": 0.3, "deficit": 1.2},
            {"name": "ci_lower", "required": ">1.0", "actual": 0.05, "deficit": 0.95},
        ],
    )
    eval_id = record_evaluation(conn, "h1", res, "fast")
    row = conn.execute(
        "SELECT * FROM evaluations WHERE id = ?", (eval_id,)
    ).fetchone()
    assert row["eval_type"] == "fast"
    assert row["promising"] == 0  # bool→int
    decoded = json.loads(row["failed_gates"])
    assert [c["name"] for c in decoded] == ["score", "ci_lower"]


def test_record_evaluation_with_no_failed_conditions_writes_null(conn):
    _insert_strategy(conn, "h1")
    eval_id = record_evaluation(conn, "h1", _result(), "fast")
    row = conn.execute(
        "SELECT failed_gates FROM evaluations WHERE id = ?", (eval_id,)
    ).fetchone()
    assert row["failed_gates"] is None


def test_record_evaluation_auto_promotes_generated_to_fast_evaluated(conn):
    _insert_strategy(conn, "h1", Status.GENERATED)
    record_evaluation(conn, "h1", _result(), "fast")
    s = conn.execute(
        "SELECT status, fast_evaluated_at FROM strategies WHERE behavioral_hash = ?",
        ("h1",),
    ).fetchone()
    assert s["status"] == "fast_evaluated"
    assert s["fast_evaluated_at"] is not None


def test_record_evaluation_canonical_promotes_skipping_fast(conn):
    """A canonical eval directly on a 'generated' strategy advances it to
    canonical_evaluated — no fast-eval intermediate required."""
    _insert_strategy(conn, "h1", Status.GENERATED)
    record_evaluation(conn, "h1", _result(), "canonical")
    s = conn.execute(
        "SELECT status, fast_evaluated_at, canonical_evaluated_at "
        "FROM strategies WHERE behavioral_hash = ?",
        ("h1",),
    ).fetchone()
    assert s["status"] == "canonical_evaluated"
    assert s["fast_evaluated_at"] is None
    assert s["canonical_evaluated_at"] is not None


def test_record_evaluation_does_not_demote(conn):
    """A late-arriving fast eval on a strategy already at paper_trading must
    NOT rewind status. The eval row is still recorded."""
    _insert_strategy(conn, "h1", Status.PAPER_TRADING)
    eval_id = record_evaluation(conn, "h1", _result(), "fast")
    s = conn.execute(
        "SELECT status FROM strategies WHERE behavioral_hash = ?", ("h1",)
    ).fetchone()
    assert s["status"] == "paper_trading"
    # eval row was still recorded
    assert conn.execute(
        "SELECT 1 FROM evaluations WHERE id = ?", (eval_id,)
    ).fetchone() is not None


def test_record_evaluation_rejects_unknown_eval_type(conn):
    _insert_strategy(conn, "h1")
    with pytest.raises(ValueError, match="unknown eval_type"):
        record_evaluation(conn, "h1", _result(), "garbage")


def test_record_evaluation_rejects_missing_strategy(conn):
    with pytest.raises(ValueError, match="not found"):
        record_evaluation(conn, "nonexistent", _result(), "fast")


# ── transition_status ────────────────────────────────────────────────────────


def test_transition_status_legal_manual_archive(conn):
    _insert_strategy(conn, "h1", Status.HOLDOUT_EVALUATED)
    transition_status(conn, "h1", Status.ARCHIVED, archive_reason="superseded")
    s = conn.execute(
        "SELECT status, archive_reason, archived_at FROM strategies "
        "WHERE behavioral_hash = ?",
        ("h1",),
    ).fetchone()
    assert s["status"] == "archived"
    assert s["archive_reason"] == "superseded"
    assert s["archived_at"] is not None


def test_transition_status_rejects_illegal_jump(conn):
    """The user's named-illegal example: generated → paper_trading."""
    _insert_strategy(conn, "h1", Status.GENERATED)
    with pytest.raises(ValueError, match="illegal transition"):
        transition_status(conn, "h1", Status.PAPER_TRADING)


def test_transition_status_archived_is_terminal(conn):
    _insert_strategy(conn, "h1", Status.ARCHIVED)
    with pytest.raises(ValueError, match="illegal transition"):
        transition_status(conn, "h1", Status.PAPER_CANDIDATE)


def test_transition_status_real_money_requires_paper_outcome_pass(conn):
    _insert_strategy(conn, "h1", Status.PAPER_COMPLETE, paper_outcome="fail")
    with pytest.raises(ValueError, match="paper_outcome='pass'"):
        transition_status(conn, "h1", Status.REAL_MONEY_CANDIDATE)
    # Now flip the outcome and try again:
    conn.execute(
        "UPDATE strategies SET paper_outcome='pass' WHERE behavioral_hash='h1'"
    )
    transition_status(conn, "h1", Status.REAL_MONEY_CANDIDATE)
    assert conn.execute(
        "SELECT status FROM strategies WHERE behavioral_hash='h1'"
    ).fetchone()["status"] == "real_money_candidate"


def test_transition_status_paper_complete_writes_outcome_and_notes(conn):
    _insert_strategy(conn, "h1", Status.PAPER_TRADING)
    transition_status(
        conn, "h1", Status.PAPER_COMPLETE,
        paper_outcome="pass", paper_notes="ran 30 days; PF 1.4",
    )
    s = conn.execute(
        "SELECT status, paper_outcome, paper_notes, paper_ended_at "
        "FROM strategies WHERE behavioral_hash = ?",
        ("h1",),
    ).fetchone()
    assert s["status"] == "paper_complete"
    assert s["paper_outcome"] == "pass"
    assert s["paper_notes"] == "ran 30 days; PF 1.4"
    assert s["paper_ended_at"] is not None


def test_transition_status_no_op_on_same_status(conn):
    """Idempotent: transitioning to the current status is a silent no-op
    that doesn't update *_at columns or raise."""
    _insert_strategy(conn, "h1", Status.PAPER_TRADING)
    # Pin paper_started_at to a known value.
    conn.execute(
        "UPDATE strategies SET paper_started_at='2026-01-01T00:00:00' "
        "WHERE behavioral_hash='h1'"
    )
    transition_status(conn, "h1", Status.PAPER_TRADING)
    s = conn.execute(
        "SELECT paper_started_at FROM strategies WHERE behavioral_hash='h1'"
    ).fetchone()
    assert s["paper_started_at"] == "2026-01-01T00:00:00"
