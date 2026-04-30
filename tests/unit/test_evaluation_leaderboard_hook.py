"""Tests for evaluation.leaderboard_hook.record_evaluation_to_leaderboard.

The hook itself is small and mostly delegates to the adapter +
record_evaluation. These tests cover the four documented behaviors:
write-on-success (canonical and fast), conn=None skip, strategy_hash=None
skip + DEBUG log, and exception swallowing + WARNING log.

Local fakes mirror the adapter test fakes — minimal duck-typed objects so
we don't need a real evaluation pipeline to exercise the helper.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

from evaluation.leaderboard_hook import record_evaluation_to_leaderboard
from leaderboard.db import initialize_db


# ── Fakes ────────────────────────────────────────────────────────────────────


@dataclass
class _FakeBreakdown:
    median_pf: float = 1.5
    consistency_factor: float = 0.7
    parameter_penalty: float = 0.95
    significance_factor: float = 1.0
    score: float = 2.0


@dataclass
class _FakeVerdict:
    is_promising: bool = True
    failed_conditions: list[Any] = field(default_factory=list)


@dataclass
class _FakePerSymbol:
    n_oos_trades: int


@dataclass
class _FakeCanonical:
    strategy_name: str = "TestStrat"
    breakdown: _FakeBreakdown = field(default_factory=_FakeBreakdown)
    verdict: _FakeVerdict = field(default_factory=_FakeVerdict)
    config: dict = field(default_factory=lambda: {"k": "v"})
    output_dir: Any = None
    per_symbol: list[_FakePerSymbol] = field(default_factory=list)


@dataclass
class _FakeFast:
    is_fast: bool = True
    strategy_name: str = "TestStrat"
    breakdown: _FakeBreakdown = field(default_factory=_FakeBreakdown)
    verdict: _FakeVerdict = field(default_factory=_FakeVerdict)
    config: dict = field(default_factory=lambda: {"k": "v"})
    output_dir: Any = None
    n_oos_trades_total: int = 50


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def conn(tmp_path):
    c = initialize_db(tmp_path / "lb.db")
    yield c
    c.close()


def _seed_strategy(conn, hash_="h1"):
    """record_evaluation expects the parent strategies row to exist
    (FK-enforced); a generation hook would normally have inserted it
    already in real flows."""
    conn.execute(
        "INSERT INTO strategies (strategy_hash, name, archetype, "
        "timeframe, spec_json, first_generated_at, last_seen_at, status) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'), 'generated')",
        (hash_, "TestStrat", "mean_reversion", "1d", "{}"),
    )


# ── Write paths ──────────────────────────────────────────────────────────────


def test_canonical_write_inserts_evaluation_row(conn):
    _seed_strategy(conn, "h1")
    res = _FakeCanonical(
        per_symbol=[_FakePerSymbol(n_oos_trades=20), _FakePerSymbol(n_oos_trades=22)],
    )
    record_evaluation_to_leaderboard(
        pipeline_result=res, conn=conn, strategy_hash="h1", eval_type="canonical"
    )
    rows = conn.execute(
        "SELECT strategy_hash, eval_type, n_oos_trades FROM evaluations"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["strategy_hash"] == "h1"
    assert rows[0]["eval_type"] == "canonical"
    assert rows[0]["n_oos_trades"] == 42


def test_fast_write_inserts_with_eval_type_fast(conn):
    _seed_strategy(conn, "h1")
    res = _FakeFast(n_oos_trades_total=15)
    record_evaluation_to_leaderboard(
        pipeline_result=res, conn=conn, strategy_hash="h1", eval_type="fast"
    )
    rows = conn.execute(
        "SELECT eval_type, n_oos_trades FROM evaluations"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["eval_type"] == "fast"
    assert rows[0]["n_oos_trades"] == 15


# ── Skip paths ──────────────────────────────────────────────────────────────


def test_skip_when_conn_none():
    """conn=None must short-circuit before any work — adapter is not even
    invoked. Patch the helper's references so any call would surface."""
    res = _FakeCanonical(per_symbol=[_FakePerSymbol(n_oos_trades=5)])
    with patch("evaluation.leaderboard_hook.to_evaluation_record") as adapter, \
         patch("evaluation.leaderboard_hook.record_evaluation") as recorder:
        record_evaluation_to_leaderboard(
            pipeline_result=res,
            conn=None,
            strategy_hash="h1",
            eval_type="canonical",
        )
    adapter.assert_not_called()
    recorder.assert_not_called()


def test_skip_when_strategy_hash_none_logs_debug(conn, caplog):
    res = _FakeCanonical(per_symbol=[_FakePerSymbol(n_oos_trades=5)])
    caplog.set_level("DEBUG", logger="evaluation.leaderboard_hook")
    with patch("evaluation.leaderboard_hook.record_evaluation") as recorder:
        record_evaluation_to_leaderboard(
            pipeline_result=res,
            conn=conn,
            strategy_hash=None,
            eval_type="canonical",
        )
    recorder.assert_not_called()
    assert any(
        "skipping leaderboard write" in r.message
        and "no strategy_hash" in r.message
        and r.levelname == "DEBUG"
        for r in caplog.records
    )


# ── Exception swallowing ────────────────────────────────────────────────────


def test_record_evaluation_exception_swallowed_with_warning(conn, caplog):
    """Same log-and-continue policy as the generator hook: a DB failure
    must not propagate out and break the eval pipeline's return path."""
    _seed_strategy(conn, "h1")
    res = _FakeCanonical(
        strategy_name="ExplodingStrat",
        per_symbol=[_FakePerSymbol(n_oos_trades=10)],
    )

    def _raising(*a, **kw):
        raise sqlite3.OperationalError("simulated DB failure")

    caplog.set_level("WARNING", logger="evaluation.leaderboard_hook")
    with patch(
        "evaluation.leaderboard_hook.record_evaluation", side_effect=_raising
    ):
        # Must NOT raise.
        record_evaluation_to_leaderboard(
            pipeline_result=res,
            conn=conn,
            strategy_hash="h1",
            eval_type="canonical",
        )

    assert any(
        "leaderboard canonical-eval write failed" in r.message
        and "ExplodingStrat" in r.message
        and r.levelname == "WARNING"
        for r in caplog.records
    )


def test_to_evaluation_record_exception_also_swallowed(conn, caplog):
    """Adapter failures (not just record_evaluation) are also covered by
    the try/except. Confirms the exception boundary wraps both calls."""
    _seed_strategy(conn, "h1")
    res = _FakeCanonical(per_symbol=[_FakePerSymbol(n_oos_trades=10)])

    def _raising(*a, **kw):
        raise ValueError("adapter blew up")

    caplog.set_level("WARNING", logger="evaluation.leaderboard_hook")
    with patch(
        "evaluation.leaderboard_hook.to_evaluation_record", side_effect=_raising
    ):
        record_evaluation_to_leaderboard(
            pipeline_result=res,
            conn=conn,
            strategy_hash="h1",
            eval_type="canonical",
        )

    rows = conn.execute("SELECT COUNT(*) AS n FROM evaluations").fetchone()
    assert rows["n"] == 0
    assert any(
        "leaderboard canonical-eval write failed" in r.message
        and r.levelname == "WARNING"
        for r in caplog.records
    )
