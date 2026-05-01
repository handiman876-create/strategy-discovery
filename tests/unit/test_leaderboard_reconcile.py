"""Tests for the leaderboard reconcile module."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from leaderboard.db import initialize_db
from leaderboard.reconcile import (
    ReconcileChange,
    ReconcileSummary,
    reconcile_evaluations,
)


# ── Fixtures + helpers ───────────────────────────────────────────────────────


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "lb.db"
    c = initialize_db(db)
    yield c
    c.close()


def _insert_strategy(conn, hash_: str = "h"):
    conn.execute(
        "INSERT INTO strategies (strategy_hash, name, archetype, timeframe, "
        "spec_json, first_generated_at, last_seen_at, status) "
        "VALUES (?, 'n', 'mean_reversion', '1d', '{}', "
        "'2026-04-29T00:00:00', '2026-04-29T00:00:00', 'fast_evaluated')",
        (hash_,),
    )


def _insert_eval_row(
    conn,
    *,
    strategy_hash: str = "h",
    eval_type: str = "fast",
    n_oos_trades: int = 1,
    score: float = 100.0,
    promising: int = 1,
    failed_gates: str | None = None,
    results_dir: str = "results/fast_eval_x/Strat",
) -> int:
    cur = conn.execute(
        "INSERT INTO evaluations (strategy_hash, eval_type, evaluated_at, "
        "n_oos_trades, score, promising, failed_gates, results_dir, "
        "config_json) "
        "VALUES (?, ?, '2026-04-29T00:00:00', ?, ?, ?, ?, ?, '{}')",
        (
            strategy_hash, eval_type, n_oos_trades, score,
            promising, failed_gates, results_dir,
        ),
    )
    return cur.lastrowid


def _write_fast_summary(
    project_root: Path,
    rel_results_dir: str,
    *,
    score: float,
    median_pf: float,
    n_oos_trades_total: int,
    consistency_factor: float = 1.0,
    parameter_penalty: float = 1.0,
    significance_factor: float = 1.0,
    is_promising: bool = True,
    failed_conditions: list[dict] | None = None,
) -> None:
    """Write a fast_summary.json mirroring fast_pipeline._write_fast_report."""
    summary_dir = project_root / rel_results_dir
    summary_dir.mkdir(parents=True, exist_ok=True)
    breakdown = {
        "median_pf": median_pf,
        "consistency_factor": consistency_factor,
        "parameter_penalty": parameter_penalty,
        "significance_factor": significance_factor,
        "score": score,
    }
    payload = {
        "is_fast": True,
        "strategy": "Strat",
        "median_pf": median_pf,
        "n_oos_trades_total": n_oos_trades_total,
        "breakdown": breakdown,
        "verdict": {
            "is_promising": is_promising,
            "failed_conditions": failed_conditions or [],
            "breakdown": breakdown,
        },
        "config": {},
        "diagnostics": None,
    }
    (summary_dir / "fast_summary.json").write_text(json.dumps(payload))


# ── Tests ────────────────────────────────────────────────────────────────────


def test_reconcile_flips_pre_p2_promising_to_unpromising(conn, tmp_path):
    """A stored row marked promising=1 with n_oos_trades_total=3 was written
    before the MIN_TRADES_FOR_PROMISING gate existed. Current logic must
    flip it: promising=0 with an n_oos_trades_total failed condition."""
    _insert_strategy(conn)
    rel = "results/fast_eval_pre_p2/Strat"
    _write_fast_summary(
        tmp_path, rel,
        score=50.0, median_pf=10.0, n_oos_trades_total=3,
        is_promising=True, failed_conditions=[],
    )
    eval_id = _insert_eval_row(
        conn, n_oos_trades=3, score=50.0, promising=1,
        failed_gates=None, results_dir=rel,
    )

    summary = reconcile_evaluations(conn, project_root=tmp_path)

    assert summary.n_reconciled == 1
    assert summary.n_unchanged == 0
    assert summary.n_skipped == 0
    assert len(summary.changes) == 1
    ch = summary.changes[0]
    assert ch.eval_id == eval_id
    assert ch.old_promising is True
    assert ch.new_promising is False
    assert ch.old_failed_gates is None
    new_gates = json.loads(ch.new_failed_gates)
    assert any(c["name"] == "n_oos_trades_total" for c in new_gates)

    # DB was actually updated.
    row = conn.execute(
        "SELECT promising, failed_gates FROM evaluations WHERE id = ?",
        (eval_id,),
    ).fetchone()
    assert row["promising"] == 0
    assert any(
        c["name"] == "n_oos_trades_total" for c in json.loads(row["failed_gates"])
    )


def test_reconcile_leaves_already_current_row_alone(conn, tmp_path):
    """A row whose stored verdict matches the current classifier output
    should be reported as unchanged with no DB write."""
    _insert_strategy(conn)
    rel = "results/fast_eval_current/Strat"
    # Score below 1.5 → score gate fails. n=200 → trades gate passes.
    # No ci_lower in old failed_conditions → reconcile uses passing
    # sentinel → ci_lower gate passes too. Exactly one failed gate (score).
    failed = [{
        "name": "score", "required": ">1.5",
        "actual": 0.5, "deficit": 1.0,
    }]
    _write_fast_summary(
        tmp_path, rel,
        score=0.5, median_pf=2.0, n_oos_trades_total=200,
        is_promising=False, failed_conditions=failed,
    )
    eval_id = _insert_eval_row(
        conn, n_oos_trades=200, score=0.5, promising=0,
        failed_gates=json.dumps(failed), results_dir=rel,
    )

    summary = reconcile_evaluations(conn, project_root=tmp_path)

    assert summary.n_reconciled == 0
    assert summary.n_unchanged == 1
    assert summary.changes == []


def test_reconcile_is_idempotent(conn, tmp_path):
    """Run twice on the same DB: the second run reports zero changes."""
    _insert_strategy(conn)
    rel = "results/fast_eval_idem/Strat"
    _write_fast_summary(
        tmp_path, rel,
        score=50.0, median_pf=10.0, n_oos_trades_total=3,
        is_promising=True, failed_conditions=[],
    )
    _insert_eval_row(
        conn, n_oos_trades=3, score=50.0, promising=1,
        failed_gates=None, results_dir=rel,
    )

    first = reconcile_evaluations(conn, project_root=tmp_path)
    second = reconcile_evaluations(conn, project_root=tmp_path)

    assert first.n_reconciled == 1
    assert second.n_reconciled == 0
    assert second.n_unchanged == 1


def test_reconcile_skips_missing_results_dir(conn, tmp_path):
    """Missing on-disk summary → row is skipped, run continues."""
    _insert_strategy(conn)
    eval_id = _insert_eval_row(
        conn, results_dir="results/nonexistent_dir/Strat",
    )

    summary = reconcile_evaluations(conn, project_root=tmp_path)

    assert summary.n_skipped == 1
    assert summary.skipped[0][0] == eval_id
    # DB row untouched.
    row = conn.execute(
        "SELECT promising FROM evaluations WHERE id = ?", (eval_id,)
    ).fetchone()
    assert row["promising"] == 1


def test_reconcile_skips_canonical_and_holdout(conn, tmp_path):
    """Only fast evaluations are reconciled today; canonical and holdout
    rows must be left untouched (and unreported)."""
    _insert_strategy(conn)
    rel = "results/fast_eval_only/Strat"
    _write_fast_summary(
        tmp_path, rel,
        score=50.0, median_pf=10.0, n_oos_trades_total=3,
        is_promising=True, failed_conditions=[],
    )
    fast_id = _insert_eval_row(conn, eval_type="fast", results_dir=rel)
    canon_id = _insert_eval_row(conn, eval_type="canonical")
    holdout_id = _insert_eval_row(conn, eval_type="holdout")

    summary = reconcile_evaluations(conn, project_root=tmp_path)

    # Only the fast row counts toward reconciled/unchanged/skipped totals.
    assert summary.n_reconciled + summary.n_unchanged + summary.n_skipped == 1
    # Canonical and holdout rows unchanged in DB.
    for eid in (canon_id, holdout_id):
        row = conn.execute(
            "SELECT promising FROM evaluations WHERE id = ?", (eid,)
        ).fetchone()
        assert row["promising"] == 1


def test_reconcile_preserves_ci_lower_from_old_failed_conditions(conn, tmp_path):
    """When the stored verdict had ci_lower in failed_conditions, reconcile
    must use that exact value — not the passing sentinel — so the new
    failed_gates output continues to include ci_lower with the same actual."""
    _insert_strategy(conn)
    rel = "results/fast_eval_cilow/Strat"
    # Pick `actual` and `deficit` that round-trip losslessly through
    # IEEE 754 (0.5 and 0.5 do; 0.7/0.3 do not). On real data this is
    # never an issue: stored deficits were computed from the same
    # breakdown floats classify_promise will see, so they're bit-identical.
    failed = [{
        "name": "ci_lower", "required": ">1.0",
        "actual": 0.5, "deficit": 0.5,
    }]
    _write_fast_summary(
        tmp_path, rel,
        score=2.0, median_pf=2.0, n_oos_trades_total=200,
        is_promising=False, failed_conditions=failed,
    )
    eval_id = _insert_eval_row(
        conn, n_oos_trades=200, score=2.0, promising=0,
        failed_gates=json.dumps(failed), results_dir=rel,
    )

    summary = reconcile_evaluations(conn, project_root=tmp_path)

    assert summary.n_unchanged == 1
    assert summary.n_reconciled == 0
    row = conn.execute(
        "SELECT failed_gates FROM evaluations WHERE id = ?", (eval_id,)
    ).fetchone()
    gates = json.loads(row["failed_gates"])
    ci = next(c for c in gates if c["name"] == "ci_lower")
    assert ci["actual"] == 0.5


def test_reconcile_writes_log_file(conn, tmp_path):
    """Log file lands in the requested log_dir and contains a line per
    change + skip."""
    _insert_strategy(conn)
    rel = "results/fast_eval_log/Strat"
    _write_fast_summary(
        tmp_path, rel,
        score=50.0, median_pf=10.0, n_oos_trades_total=3,
        is_promising=True, failed_conditions=[],
    )
    _insert_eval_row(conn, n_oos_trades=3, results_dir=rel)
    log_dir = tmp_path / "logs"

    summary = reconcile_evaluations(
        conn, project_root=tmp_path, log_dir=log_dir
    )

    assert summary.log_path is not None
    assert summary.log_path.parent == log_dir
    contents = summary.log_path.read_text()
    assert "reconciled: 1" in contents
    assert "promising=1->0" in contents
