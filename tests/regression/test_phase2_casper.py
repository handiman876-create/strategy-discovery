"""Phase 2 acceptance: the evaluation harness must classify Casper as
'not promising'.

Per DESIGN.md §7 Phase 2 done-criteria: 'Casper strategy scored through full
evaluation pipeline ... Spoiler: it's expected to score poorly. That's the
point — confirms the evaluation pipeline correctly identifies non-edges.'

This test runs the pipeline at small-but-real scale (subset of cached
symbols, smaller bootstrap) so it completes in reasonable time during CI.
The actual Phase-2 acceptance numbers come from a separate full run via
scripts/evaluate.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from engine.backtester import BacktestConfig
from engine.session import RegularTradingHours
from evaluation import WalkForwardConfig, run_evaluation

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "strategies"))
from manual.casper import CasperStrategy


def test_casper_classified_not_promising():
    """Casper across the 5 Phase-1 cached symbols must NOT pass the promising
    gate. We use a small grid + small bootstrap to keep test runtime <5 min."""
    backtest_cfg = BacktestConfig(
        starting_capital=10_000,
        commission=0.0,
        slippage=0.01,
        realistic_fills=True,
        session=RegularTradingHours(),
    )
    walk_cfg = WalkForwardConfig(
        train_window_months=24,
        test_window_months=6,
        step_months=6,
        parameter_grid={
            "rr_ratio": [1.5, 2.0, 2.5],
            "min_bars_beyond_or": [1, 2, 3],
        },
    )

    result = run_evaluation(
        CasperStrategy,
        symbols=["AMD", "NFLX", "SPY", "QQQ", "NVDA"],
        backtest_config=backtest_cfg,
        walk_config=walk_cfg,
        n_bootstrap=1000,
        m_baseline=50,
    )

    assert not result.verdict.is_promising, (
        f"Casper unexpectedly classified as promising. "
        f"Score breakdown: {result.breakdown}"
    )
    assert result.verdict.failed_conditions, (
        "Verdict is 'not promising' but no failed conditions logged — bug."
    )
    # Sanity: at least one of the gate conditions failed substantially.
    deficits = [c.deficit for c in result.verdict.failed_conditions]
    assert any(d > 0.05 for d in deficits), (
        "All deficits trivially small; framework may have falsely-failed."
    )
