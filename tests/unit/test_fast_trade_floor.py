"""Regression tests for the fast-screen minimum-trade floor.

Motivation (2026-07-06): the three highest-*scoring* rows in the fast-eval
leaderboard were 1-3 trade artifacts whose profit factor was capped at 100 and
whose robustness score was therefore ~100 — they topped a score ranking despite
being pure noise. run_fast_evaluation now floors the score to 0 when total OOS
trades < FAST_MIN_TRADES so under-sampled specs sink instead of leading.

These tests isolate the floor by monkeypatching the inner run_evaluation, so
they exercise the floor logic without loading any market data.
"""
from __future__ import annotations

from engine.backtester import BacktestConfig
from engine.session import RegularTradingHours
from evaluation import fast_pipeline
from evaluation.fast_pipeline import FAST_MIN_TRADES, run_fast_evaluation
from evaluation.pipeline import EvaluationResult, SymbolEvaluation
from evaluation.scoring import PromiseVerdict, ScoreBreakdown


class _DummyStrategy:  # never instantiated — run_evaluation is patched out
    timeframes = ["1d"]


def _fake_result(n_total: int, score: float) -> EvaluationResult:
    """A canonical EvaluationResult with a chosen total trade count and score.
    Only per_symbol[i].n_oos_trades and breakdown.score are read downstream in
    the no-DB / no-report path, so the other fields are minimal stand-ins."""
    breakdown = ScoreBreakdown(
        median_pf=100.0 if n_total < FAST_MIN_TRADES else 2.0,
        consistency_factor=1.0,
        parameter_penalty=1.0,
        significance_factor=1.0,
        score=score,
    )
    per_symbol = [
        SymbolEvaluation(
            symbol="SPY",
            n_oos_trades=n_total,
            pf=breakdown.median_pf,
            bootstrap=None,
            baseline=None,
            walk_forward=None,
        )
    ]
    verdict = PromiseVerdict(is_promising=False, failed_conditions=[], breakdown=breakdown)
    return EvaluationResult(
        strategy_name="DummyStrategy",
        symbols=["SPY"],
        per_symbol=per_symbol,
        breakdown=breakdown,
        verdict=verdict,
        config={"strategy": "dummy"},
        aggregate_p_value=0.0,
    )


def _cfg() -> BacktestConfig:
    return BacktestConfig(
        starting_capital=10_000, commission=0.0, slippage=0.01,
        realistic_fills=True, session=RegularTradingHours(),
    )


def test_score_floored_when_undersampled(monkeypatch):
    # 20 trades < FAST_MIN_TRADES (30); pick 20 so we also stay above the
    # DIAGNOSE_BELOW_TRADES (10) branch and don't trip signal-frequency diag.
    n = FAST_MIN_TRADES - 10
    assert n > fast_pipeline.DIAGNOSE_BELOW_TRADES
    monkeypatch.setattr(fast_pipeline, "run_evaluation",
                        lambda *a, **k: _fake_result(n, score=100.0))
    fast = run_fast_evaluation(_DummyStrategy, backtest_config=_cfg())
    assert fast.n_oos_trades_total == n
    assert fast.breakdown.score == 0.0  # floored


def test_score_preserved_when_enough_trades(monkeypatch):
    n = FAST_MIN_TRADES + 20  # comfortably above the floor
    monkeypatch.setattr(fast_pipeline, "run_evaluation",
                        lambda *a, **k: _fake_result(n, score=2.0))
    fast = run_fast_evaluation(_DummyStrategy, backtest_config=_cfg())
    assert fast.n_oos_trades_total == n
    assert fast.breakdown.score == 2.0  # untouched


def test_floor_at_exact_threshold_not_fired(monkeypatch):
    # Boundary: exactly FAST_MIN_TRADES trades is enough — floor uses strict <.
    monkeypatch.setattr(fast_pipeline, "run_evaluation",
                        lambda *a, **k: _fake_result(FAST_MIN_TRADES, score=1.7))
    fast = run_fast_evaluation(_DummyStrategy, backtest_config=_cfg())
    assert fast.breakdown.score == 1.7
