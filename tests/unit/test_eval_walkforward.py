"""Walk-forward enumeration + optimization tests."""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from engine.backtester import BacktestConfig
from engine.session import RegularTradingHours
from evaluation.splits import is_in_optimization_mode
from evaluation.walkforward import (
    WalkForwardConfig,
    _enumerate_windows,
    _expand_grid,
    walk_forward,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "strategies"))
from manual.casper import CasperStrategy

ET = ZoneInfo("America/New_York")


def test_enumerate_windows_3_year_span():
    cfg = WalkForwardConfig(
        train_window_months=24, test_window_months=6, step_months=6
    )
    windows = _enumerate_windows(date(2021, 4, 28), date(2024, 12, 31), cfg)
    assert len(windows) == 3
    # First window: train 2021-05 → 2023-05, test 2023-05 → 2023-11
    assert windows[0][0] == date(2021, 5, 1)
    assert windows[0][1] == date(2023, 5, 1)
    assert windows[0][2] == date(2023, 5, 1)
    assert windows[0][3] == date(2023, 11, 1)


def test_enumerate_windows_short_span_yields_nothing():
    cfg = WalkForwardConfig(train_window_months=24, test_window_months=6)
    # Span too short for even one window
    windows = _enumerate_windows(date(2024, 1, 1), date(2024, 6, 30), cfg)
    assert windows == []


def test_expand_grid_cartesian():
    grid = {"a": [1, 2], "b": ["x", "y", "z"]}
    out = _expand_grid(grid)
    assert out is not None and len(out) == 6
    assert {"a": 1, "b": "x"} in out


def test_expand_grid_none():
    assert _expand_grid(None) is None


def test_expand_grid_empty_dict():
    assert _expand_grid({}) == [{}]


def _synth_bars(start: date, n_days: int = 30):
    """Generate synthetic 5-min bars across n_days RTH sessions."""
    rows = []
    for d in range(n_days):
        day = start + pd.Timedelta(days=d)
        # Skip weekends
        if day.weekday() >= 5:
            continue
        for i in range(78):
            ts = pd.Timestamp(day, tz="America/New_York") + pd.Timedelta(hours=9, minutes=30 + 5 * i)
            price = 100 + (d + i / 78) * 0.5
            rows.append({"timestamp": ts, "open": price, "high": price + 0.3, "low": price - 0.3, "close": price + 0.1, "volume": 1000.0})
    return pd.DataFrame(rows)


def test_walk_forward_no_grid_skips_optimization():
    """When parameter_grid is None, the walk-forward should still produce
    windows, just running default-param backtests."""
    bars = _synth_bars(date(2024, 1, 1), n_days=200)
    cfg = WalkForwardConfig(
        train_window_months=2, test_window_months=1, step_months=1, parameter_grid=None
    )
    bt_cfg = BacktestConfig(session=RegularTradingHours())
    res = walk_forward("X", bars, CasperStrategy, bt_cfg, cfg)
    # All windows should have empty best_params dict.
    for w in res.windows:
        assert w.best_params == {}


def test_walk_forward_optimization_mode_active_during_grid_search():
    """During the grid-search step, is_in_optimization_mode() must be True.
    Verify by patching the strategy_factory to assert that flag."""
    seen_flags: list[bool] = []

    def factory(**params):
        seen_flags.append(is_in_optimization_mode())
        return CasperStrategy(**params)

    bars = _synth_bars(date(2024, 1, 1), n_days=200)
    cfg = WalkForwardConfig(
        train_window_months=2, test_window_months=1, step_months=1,
        parameter_grid={"rr_ratio": [1.5, 2.0]},
    )
    bt_cfg = BacktestConfig(session=RegularTradingHours())
    walk_forward("X", bars, factory, bt_cfg, cfg)

    # At least some calls were inside optimization_mode (the in-window grid search),
    # and at least some were outside (the OOS test backtest).
    assert any(seen_flags), "expected some calls inside optimization_mode"
    assert not all(seen_flags), "expected OOS calls outside optimization_mode"
