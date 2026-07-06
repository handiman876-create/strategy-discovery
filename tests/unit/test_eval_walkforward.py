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
    _oos_only,
    _prepend_warmup,
    walk_forward,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "strategies"))
from manual.casper import CasperStrategy
from manual.rsi2_mean_reversion import Rsi2MeanReversion

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


# ── Warmup-prefix behavior (long-lookback indicators in short OOS windows) ─────


def _daily_bars_with_dips(n: int = 420, start: date = date(2022, 1, 1)) -> pd.DataFrame:
    """Deterministic daily bars: steady uptrend (so price stays above a long MA)
    with a 2-bar dip every 15 trading days to drive RSI(2) below 10 and trigger
    the RSI-2 long entry."""
    rows = []
    d = pd.Timestamp(start, tz="America/New_York")
    price = 100.0
    i = 0
    while i < n:
        if d.weekday() < 5:  # weekdays only
            price *= 1.004  # ~0.4%/day drift keeps close well above the 200-SMA
            close = price * 0.95 if (i % 15) in (5, 6) else price  # 2 consecutive dips
            rows.append({
                "timestamp": d,
                "open": close,
                "high": close * 1.005,
                "low": close * 0.995,
                "close": close,
                "volume": 1_000_000.0,
            })
            i += 1
        d += pd.Timedelta(days=1)
    return pd.DataFrame(rows)


def test_prepend_warmup_only_uses_historical_bars():
    bars = _daily_bars_with_dips(300)
    test_start = bars["timestamp"].iloc[200].date()
    test_bars = bars[bars["timestamp"].dt.date >= test_start].reset_index(drop=True)

    ext = _prepend_warmup(bars, test_bars, test_start, warmup_bars=200)
    s = pd.Timestamp(test_start, tz=bars["timestamp"].dt.tz)
    prefix = ext[ext["timestamp"] < s]

    # Every prepended bar strictly precedes the window; nothing from the future.
    assert len(prefix) == 200
    assert (prefix["timestamp"] < s).all()
    # The original test window survives intact at the tail.
    assert len(ext) == len(prefix) + len(test_bars)


def test_prepend_warmup_disabled_or_no_history_returns_unchanged():
    bars = _daily_bars_with_dips(50)
    test_start = bars["timestamp"].iloc[0].date()  # nothing before the first bar
    test_bars = bars.copy()
    # No prior history → unchanged.
    assert len(_prepend_warmup(bars, test_bars, test_start, 200)) == len(test_bars)
    # Warmup disabled → unchanged.
    later = bars["timestamp"].iloc[20].date()
    tb = bars[bars["timestamp"].dt.date >= later].reset_index(drop=True)
    assert len(_prepend_warmup(bars, tb, later, 0)) == len(tb)


def test_oos_only_drops_pre_window_entries():
    from types import SimpleNamespace

    tz = ZoneInfo("America/New_York")
    test_start = date(2023, 6, 1)
    trades = [
        SimpleNamespace(entry_time=datetime(2023, 5, 20, 10, 0, tzinfo=tz)),  # warmup
        SimpleNamespace(entry_time=datetime(2023, 6, 1, 10, 0, tzinfo=tz)),   # boundary: kept
        SimpleNamespace(entry_time=datetime(2023, 7, 15, 10, 0, tzinfo=tz)),  # in-window
    ]
    kept = _oos_only(trades, test_start, tz)
    assert len(kept) == 2
    assert all(pd.Timestamp(t.entry_time) >= pd.Timestamp(test_start, tz=tz) for t in kept)


def test_warmup_enables_long_lookback_oos_trades():
    """A 200-SMA strategy in short (3-month) OOS windows produces ZERO trades
    without warmup (the MA never initializes) and >0 with warmup — and no
    warmup-window trade leaks into the OOS count."""
    bars = _daily_bars_with_dips(420)
    wf = WalkForwardConfig(
        train_window_months=9, test_window_months=3, step_months=3, parameter_grid=None
    )

    # context_lookback=0 disables warmup; the 3-month window can't warm a 200-SMA.
    off = BacktestConfig(session=RegularTradingHours(), bar_timeframe="1d", context_lookback=0)
    res_off = walk_forward("X", bars, Rsi2MeanReversion, off, wf)
    assert len(res_off.all_oos_trades) == 0

    # With warmup, the same windows trade.
    on = BacktestConfig(session=RegularTradingHours(), bar_timeframe="1d", context_lookback=200)
    res_on = walk_forward("X", bars, Rsi2MeanReversion, on, wf)
    assert len(res_on.all_oos_trades) > 0

    # No look-ahead: every OOS trade was entered inside its window.
    tz = bars["timestamp"].dt.tz
    for w in res_on.windows:
        s = pd.Timestamp(w.test_start, tz=tz)
        assert all(pd.Timestamp(t.entry_time) >= s for t in w.test_trades)
