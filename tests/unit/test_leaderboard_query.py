"""Tests for the leaderboard query module."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from leaderboard.db import initialize_db
from leaderboard.models import (
    ArchetypeSummary,
    Evaluation,
    Generation,
    GenerationMetadata,
    Status,
    Strategy,
)
from leaderboard.query import (
    get_archetype_summary,
    get_authoritative_result,
    get_generation_history,
    get_promising_candidates,
    get_quirk_trend,
    get_strategy,
    list_strategies,
)


# ── Fixtures + helpers ───────────────────────────────────────────────────────


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "lb.db"
    c = initialize_db(db)
    yield c
    c.close()


def _insert_strategy(
    conn,
    hash_: str,
    *,
    archetype: str = "mean_reversion",
    timeframe: str = "1d",
    status: Status = Status.GENERATED,
    first_generated_at: str = "2026-04-29T00:00:00",
    last_seen_at: str = "2026-04-29T00:00:00",
):
    conn.execute(
        "INSERT INTO strategies (strategy_hash, name, archetype, timeframe, "
        "spec_json, first_generated_at, last_seen_at, status) "
        "VALUES (?, ?, ?, ?, '{}', ?, ?, ?)",
        (hash_, f"name_{hash_}", archetype, timeframe, first_generated_at,
         last_seen_at, status.value),
    )


def _insert_generation(
    conn,
    strategy_hash: str,
    *,
    generated_at: str = "2026-04-29T00:00:00",
    archetype: str = "mean_reversion",
    cost_usd: float = 0.05,
    stringification: int = 0,
    kwarg: int = 0,
    unreachable: int = 0,
):
    conn.execute(
        "INSERT INTO generations (strategy_hash, generated_at, archetype, "
        "model_version, prompt_hash, cost_usd, retry_count, duration_seconds, "
        "stringification_firings, kwarg_validator_firings, "
        "unreachable_default_firings) "
        "VALUES (?, ?, ?, 'm', 'p', ?, 0, 1.0, ?, ?, ?)",
        (strategy_hash, generated_at, archetype, cost_usd,
         stringification, kwarg, unreachable),
    )


def _insert_evaluation(
    conn,
    strategy_hash: str,
    eval_type: str,
    *,
    evaluated_at: str = "2026-04-29T00:00:00",
    n_oos_trades: int = 50,
    score: float = 1.0,
    promising: bool = False,
):
    conn.execute(
        "INSERT INTO evaluations (strategy_hash, eval_type, evaluated_at, "
        "n_oos_trades, score, promising, results_dir, config_json) "
        "VALUES (?, ?, ?, ?, ?, ?, '/tmp', '{}')",
        (strategy_hash, eval_type, evaluated_at, n_oos_trades, score,
         1 if promising else 0),
    )


# ── get_strategy ─────────────────────────────────────────────────────────────


def test_get_strategy_returns_dataclass(conn):
    _insert_strategy(conn, "h1", status=Status.FAST_EVALUATED)
    s = get_strategy(conn, "h1")
    assert isinstance(s, Strategy)
    assert s.strategy_hash == "h1"
    assert s.status is Status.FAST_EVALUATED  # converted from TEXT to enum


def test_get_strategy_returns_none_for_unknown_hash(conn):
    assert get_strategy(conn, "missing") is None


# ── list_strategies ──────────────────────────────────────────────────────────


def test_list_strategies_default_order_is_last_seen_desc(conn):
    _insert_strategy(conn, "old", last_seen_at="2026-01-01T00:00:00")
    _insert_strategy(conn, "new", last_seen_at="2026-04-29T00:00:00")
    _insert_strategy(conn, "mid", last_seen_at="2026-03-01T00:00:00")
    rows = list_strategies(conn)
    assert [s.strategy_hash for s in rows] == ["new", "mid", "old"]


def test_list_strategies_filters_by_archetype_status_and_timeframe(conn):
    _insert_strategy(conn, "a", archetype="mean_reversion", timeframe="1d",
                     status=Status.GENERATED)
    _insert_strategy(conn, "b", archetype="momentum", timeframe="1d",
                     status=Status.GENERATED)
    _insert_strategy(conn, "c", archetype="mean_reversion", timeframe="5m",
                     status=Status.GENERATED)
    _insert_strategy(conn, "d", archetype="mean_reversion", timeframe="1d",
                     status=Status.ARCHIVED)

    only_mr_1d_gen = list_strategies(
        conn, archetype="mean_reversion", timeframe="1d", status=Status.GENERATED
    )
    assert {s.strategy_hash for s in only_mr_1d_gen} == {"a"}


def test_list_strategies_rejects_unknown_order_by(conn):
    with pytest.raises(ValueError, match="unknown order_by"):
        list_strategies(conn, order_by="DROP TABLE strategies")


def test_list_strategies_respects_limit(conn):
    for i in range(5):
        _insert_strategy(conn, f"h{i}", last_seen_at=f"2026-04-2{i}T00:00:00")
    rows = list_strategies(conn, limit=2)
    assert len(rows) == 2


# ── get_authoritative_result ─────────────────────────────────────────────────


def test_get_authoritative_result_priority_holdout_canonical_fast(conn):
    _insert_strategy(conn, "h")
    _insert_evaluation(conn, "h", "fast", evaluated_at="2026-04-30T00:00:00")
    _insert_evaluation(conn, "h", "canonical", evaluated_at="2026-04-29T00:00:00")
    _insert_evaluation(conn, "h", "holdout", evaluated_at="2026-04-28T00:00:00")
    # Even though holdout is the OLDEST, its priority wins.
    res = get_authoritative_result(conn, "h")
    assert res is not None
    assert res.eval_type == "holdout"


def test_get_authoritative_result_most_recent_within_type(conn):
    _insert_strategy(conn, "h")
    _insert_evaluation(conn, "h", "fast", evaluated_at="2026-04-29T00:00:00",
                       score=1.0)
    _insert_evaluation(conn, "h", "fast", evaluated_at="2026-04-30T00:00:00",
                       score=2.0)
    res = get_authoritative_result(conn, "h")
    assert res is not None
    assert res.evaluated_at == "2026-04-30T00:00:00"
    assert res.score == 2.0


def test_get_authoritative_result_none_when_no_evals(conn):
    _insert_strategy(conn, "h")
    assert get_authoritative_result(conn, "h") is None


# ── get_generation_history ───────────────────────────────────────────────────


def test_get_generation_history_orders_oldest_first(conn):
    _insert_strategy(conn, "h")
    _insert_generation(conn, "h", generated_at="2026-04-29T00:00:00")
    _insert_generation(conn, "h", generated_at="2026-04-27T00:00:00")
    _insert_generation(conn, "h", generated_at="2026-04-28T00:00:00")
    gens = get_generation_history(conn, "h")
    assert [g.generated_at for g in gens] == [
        "2026-04-27T00:00:00",
        "2026-04-28T00:00:00",
        "2026-04-29T00:00:00",
    ]
    assert all(isinstance(g, Generation) for g in gens)


# ── get_archetype_summary ────────────────────────────────────────────────────


def test_get_archetype_summary_full_shape(conn):
    # mean_reversion strategies — three at different statuses, one archived.
    _insert_strategy(conn, "m1", archetype="mean_reversion",
                     status=Status.GENERATED)
    _insert_strategy(conn, "m2", archetype="mean_reversion",
                     status=Status.FAST_EVALUATED)
    _insert_strategy(conn, "m3", archetype="mean_reversion",
                     status=Status.ARCHIVED)
    # momentum strategy: must NOT appear in mean_reversion summary.
    _insert_strategy(conn, "x1", archetype="momentum", status=Status.GENERATED)

    _insert_generation(conn, "m1", cost_usd=0.04, stringification=2,
                       kwarg=1, unreachable=0)
    _insert_generation(conn, "m2", cost_usd=0.06, stringification=0,
                       kwarg=0, unreachable=3)
    _insert_generation(conn, "x1", archetype="momentum", cost_usd=0.10)  # excluded

    _insert_evaluation(conn, "m1", "fast", score=0.5, promising=False)
    _insert_evaluation(conn, "m2", "fast", score=2.0, promising=True)
    _insert_evaluation(conn, "m2", "canonical", score=2.5, promising=True)
    _insert_evaluation(conn, "x1", "fast", score=99.0, promising=True)  # excluded

    summary = get_archetype_summary(conn, "mean_reversion")
    assert isinstance(summary, ArchetypeSummary)
    assert summary.archetype == "mean_reversion"
    assert summary.timeframe is None
    assert summary.since is None
    assert summary.n_strategies == 3
    assert summary.n_generations == 2
    assert summary.n_evaluations_by_type == {"fast": 2, "canonical": 1, "holdout": 0}
    assert summary.n_promising_by_type == {"fast": 1, "canonical": 1, "holdout": 0}
    assert summary.by_status == {
        "generated": 1, "fast_evaluated": 1, "archived": 1,
    }
    assert summary.median_score == 2.0  # median of [0.5, 2.0, 2.5]
    assert summary.total_cost_usd == pytest.approx(0.10)
    assert summary.quirk_counts == {
        "stringification": 2, "kwarg_validator": 1, "unreachable_default": 3,
    }


def test_get_archetype_summary_no_data_returns_zero_summary(conn):
    summary = get_archetype_summary(conn, "mean_reversion")
    assert summary.n_strategies == 0
    assert summary.n_generations == 0
    # The three eval_type keys are always present, even when zero.
    assert summary.n_evaluations_by_type == {"fast": 0, "canonical": 0, "holdout": 0}
    assert summary.n_promising_by_type == {"fast": 0, "canonical": 0, "holdout": 0}
    # The three quirk keys are always present, even when zero.
    assert summary.quirk_counts == {
        "stringification": 0, "kwarg_validator": 0, "unreachable_default": 0,
    }
    assert summary.median_score is None
    assert summary.total_cost_usd is None
    assert summary.by_status == {}


def test_get_archetype_summary_filters_by_timeframe(conn):
    _insert_strategy(conn, "d", archetype="mean_reversion", timeframe="1d")
    _insert_strategy(conn, "h", archetype="mean_reversion", timeframe="1h")
    _insert_generation(conn, "d", cost_usd=1.0)
    _insert_generation(conn, "h", cost_usd=2.0)
    summary_1d = get_archetype_summary(conn, "mean_reversion", timeframe="1d")
    assert summary_1d.n_strategies == 1
    assert summary_1d.total_cost_usd == pytest.approx(1.0)


def test_get_archetype_summary_filters_by_since(conn):
    _insert_strategy(conn, "old", archetype="mean_reversion",
                     first_generated_at="2026-01-01T00:00:00")
    _insert_strategy(conn, "new", archetype="mean_reversion",
                     first_generated_at="2026-04-29T00:00:00")
    summary = get_archetype_summary(
        conn, "mean_reversion", since="2026-04-01T00:00:00"
    )
    assert summary.n_strategies == 1


# ── get_quirk_trend ──────────────────────────────────────────────────────────


def test_get_quirk_trend_zero_fills_missing_days(conn):
    """Result must always have window_days entries, even on an empty DB."""
    trend = get_quirk_trend(conn, "stringification", window_days=7)
    assert len(trend) == 7
    assert all(count == 0 for _date, count in trend)
    # Dates are sorted ASC.
    dates = [d for d, _ in trend]
    assert dates == sorted(dates)


def test_get_quirk_trend_aggregates_per_day(conn):
    _insert_strategy(conn, "h")
    today = datetime.now(timezone.utc).date()
    # Two generations today with stringification firings 3 and 4 → 7 total.
    today_iso = today.isoformat() + "T12:00:00"
    yesterday_iso = (today - timedelta(days=1)).isoformat() + "T12:00:00"
    _insert_generation(conn, "h", generated_at=today_iso, stringification=3)
    _insert_generation(conn, "h", generated_at=today_iso, stringification=4)
    _insert_generation(conn, "h", generated_at=yesterday_iso, stringification=2)

    trend = dict(get_quirk_trend(conn, "stringification", window_days=7))
    assert trend[today.isoformat()] == 7
    assert trend[(today - timedelta(days=1)).isoformat()] == 2
    # Day 3 ago should have zero.
    assert trend[(today - timedelta(days=3)).isoformat()] == 0


def test_get_quirk_trend_rejects_unknown_quirk_name(conn):
    with pytest.raises(ValueError, match="unknown quirk_name"):
        get_quirk_trend(conn, "garbage")


def test_get_quirk_trend_rejects_zero_window(conn):
    with pytest.raises(ValueError, match="window_days must be >= 1"):
        get_quirk_trend(conn, "stringification", window_days=0)


# ── get_promising_candidates ─────────────────────────────────────────────────


def test_get_promising_candidates_returns_promising_only(conn):
    _insert_strategy(conn, "good")
    _insert_strategy(conn, "bad")
    _insert_evaluation(conn, "good", "fast", promising=True, score=2.0)
    _insert_evaluation(conn, "bad", "fast", promising=False, score=10.0)
    rows = get_promising_candidates(conn, eval_type="fast")
    assert [s.strategy_hash for s in rows] == ["good"]


def test_get_promising_candidates_orders_by_most_recent_score_desc(conn):
    """Order is by the *most recent* promising eval's score, not the highest
    score across all promising evals."""
    _insert_strategy(conn, "a")
    _insert_strategy(conn, "b")
    # a: old high score, then new low score → most-recent score = 0.5
    _insert_evaluation(conn, "a", "canonical", promising=True, score=10.0,
                       evaluated_at="2026-01-01T00:00:00")
    _insert_evaluation(conn, "a", "canonical", promising=True, score=0.5,
                       evaluated_at="2026-04-29T00:00:00")
    # b: single high promising eval → most-recent score = 5.0
    _insert_evaluation(conn, "b", "canonical", promising=True, score=5.0,
                       evaluated_at="2026-04-28T00:00:00")
    rows = get_promising_candidates(conn, eval_type="canonical")
    assert [s.strategy_hash for s in rows] == ["b", "a"]


def test_get_promising_candidates_filters_by_eval_type(conn):
    _insert_strategy(conn, "h")
    _insert_evaluation(conn, "h", "fast", promising=True, score=2.0)
    # Same strategy, but no canonical eval → not in canonical candidates.
    assert get_promising_candidates(conn, eval_type="canonical") == []
    assert len(get_promising_candidates(conn, eval_type="fast")) == 1


def test_get_promising_candidates_rejects_unknown_eval_type(conn):
    with pytest.raises(ValueError, match="unknown eval_type"):
        get_promising_candidates(conn, eval_type="garbage")
