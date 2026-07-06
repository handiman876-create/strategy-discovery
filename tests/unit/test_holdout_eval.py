"""Tests for run_holdout_evaluation — the --holdout path wired into evaluate.py.

Uses monkeypatched loaders (synthetic daily bars) so the test doesn't depend on
on-disk data. Verifies: warmup from the train tail enables trades in a short
holdout window that couldn't self-warm a 200-SMA; no look-ahead (only trades
entered on/after the holdout boundary are counted); and eval_type='holdout' is
recorded, advancing status to holdout_evaluated.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

from engine.backtester import BacktestConfig
from engine.session import RegularTradingHours
from evaluation import pipeline
from evaluation.pipeline import run_holdout_evaluation
from evaluation.splits import HOLDOUT_BOUNDARY
from leaderboard.db import initialize_db
from leaderboard.record import record_manual_strategy
from generator.dedup import compute_manual_strategy_hash

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "strategies"))
from manual.rsi2_mean_reversion import Rsi2MeanReversion


def _daily(start: str, end: str) -> pd.DataFrame:
    """Continuous weekday daily uptrend with a 2-bar dip every 15 bars (drives
    RSI(2) below 10 while price stays above its 200-SMA)."""
    rows = []
    d = pd.Timestamp(start, tz="America/New_York")
    end_ts = pd.Timestamp(end, tz="America/New_York")
    price = 100.0
    i = 0
    while d < end_ts:
        if d.weekday() < 5:
            price *= 1.004
            close = price * 0.95 if (i % 15) in (5, 6) else price
            rows.append({"timestamp": d, "open": close, "high": close * 1.005,
                         "low": close * 0.995, "close": close, "volume": 1e6})
            i += 1
        d += pd.Timedelta(days=1)
    return pd.DataFrame(rows)


@pytest.fixture
def split_data():
    # One continuous series split at the holdout boundary so prices are
    # continuous across the join (a discontinuity would break the 200-SMA).
    allbars = _daily("2022-01-03", "2025-06-01")
    boundary = pd.Timestamp(HOLDOUT_BOUNDARY, tz="America/New_York")
    train = allbars[allbars["timestamp"] < boundary].reset_index(drop=True)
    holdout = allbars[allbars["timestamp"] >= boundary].reset_index(drop=True)
    return train, holdout


@pytest.fixture
def patched_loaders(split_data, monkeypatch):
    train, holdout = split_data
    monkeypatch.setattr(pipeline, "train_test_load", lambda sym, target_timeframe="1d": train)
    monkeypatch.setattr(
        pipeline, "holdout_load",
        lambda sym, final_scoring=False, target_timeframe="1d": holdout,
    )
    return train, holdout


def _cfg(context_lookback: int) -> BacktestConfig:
    return BacktestConfig(session=RegularTradingHours(), bar_timeframe="1d",
                          context_lookback=context_lookback)


def test_holdout_warmup_enables_trades_and_no_lookahead(patched_loaders):
    _, holdout = patched_loaders
    assert len(holdout) < 200  # short window: can't self-warm a 200-SMA

    res = run_holdout_evaluation(Rsi2MeanReversion, symbols=["X"],
                                 backtest_config=_cfg(200))
    sym = res.per_symbol[0]
    assert sym.n_oos_trades > 0  # warmup from train tail made the 200-SMA available
    assert res.config["eval"] == "holdout"
    assert res.config["holdout_boundary"] == str(HOLDOUT_BOUNDARY)


def test_holdout_without_warmup_cannot_trade_short_window(patched_loaders):
    # context_lookback=0 → no warmup prefix → the sub-200-bar holdout never warms
    # the 200-SMA → zero trades. Proves the warmup is what enables the eval.
    res = run_holdout_evaluation(Rsi2MeanReversion, symbols=["X"],
                                 backtest_config=_cfg(0))
    assert res.per_symbol[0].n_oos_trades == 0


def test_holdout_records_eval_type_holdout(patched_loaders, tmp_path):
    conn = initialize_db(tmp_path / "lb.db")
    h = compute_manual_strategy_hash(Rsi2MeanReversion)
    record_manual_strategy(conn, Rsi2MeanReversion, h)

    run_holdout_evaluation(Rsi2MeanReversion, symbols=["X"],
                           backtest_config=_cfg(200), conn=conn, strategy_hash=h)

    row = conn.execute(
        "SELECT eval_type FROM evaluations WHERE strategy_hash=?", (h,)
    ).fetchone()
    assert row is not None and row["eval_type"] == "holdout"
    status = conn.execute(
        "SELECT status FROM strategies WHERE strategy_hash=?", (h,)
    ).fetchone()["status"]
    assert status == "holdout_evaluated"
    conn.close()
