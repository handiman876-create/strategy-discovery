"""Backtester timeframe-dispatch tests (Fix #1 + session_bars param).

Two regression dimensions:

  * Daily-strategy-on-resampled data: when BacktestConfig.bar_timeframe is
    "1d", session_bars must NOT reset at session boundaries — the whole
    series is one continuous session — so daily-period indicators warm up.
    This is the "Additional ask B" smoke test from the Fix #1 review.

  * Intraday default unchanged: when bar_timeframe is "5m" (the default),
    session_bars resets at each RTH session start, EOD force-close fires,
    on_session_start hook fires per session. Pinning prior behavior so
    the dispatch doesn't accidentally regress intraday strategies.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd

from data.resample import resample
from engine.backtester import BacktestConfig, run_backtest
from engine.execution import Order
from engine.session import RegularTradingHours
from generator.indicators import sma
from strategy.base import Strategy
from strategy.context import Bar, Context


_ET = ZoneInfo("America/New_York")


def _synthesize_5m_rth_bars(n_sessions: int) -> pd.DataFrame:
    """78 RTH 5-min bars per session over n_sessions consecutive weekdays
    starting Mon 2024-01-08 (chosen to dodge 2024 Q1 US holidays for the
    short ranges these tests use)."""
    rows = []
    day = datetime(2024, 1, 8, tzinfo=_ET)  # Monday
    for s in range(n_sessions):
        # Skip weekends if we wandered into one (1 Jan 2024 alignment).
        while day.weekday() >= 5:
            day += timedelta(days=1)
        ts = day.replace(hour=9, minute=30)
        for b in range(78):
            price = 100.0 + s * 1.0 + b * 0.05
            rows.append({
                "timestamp": ts,
                "open": price, "high": price + 0.1, "low": price - 0.1,
                "close": price + 0.05, "volume": 1000.0,
            })
            ts += timedelta(minutes=5)
        day += timedelta(days=1)
    return pd.DataFrame(rows)


class _ProbeStrategyDaily(Strategy):
    """Captures every on_bar invocation: timestamp, sma_100 value, position
    state. Emits no orders. Lets the test inspect what the engine fed in."""

    archetype = "mean_reversion"
    thesis = "Probe strategy with sma_100 — used by the daily-bar smoke test for Fix #1."
    supported_assets = ["stocks"]
    timeframes = ["1d"]

    def __init__(self):
        self.calls: list[dict] = []
        self.session_starts: list[datetime] = []

    def on_session_start(self, ts, ctx):
        self.session_starts.append(ts)

    def on_bar(self, bar: Bar, position: Optional[object], ctx: Context) -> list[Order]:
        recent = ctx.recent(150)
        s = sma(recent, period=100)
        self.calls.append({
            "ts": bar.timestamp,
            "n_recent": len(recent),
            "sma_100": s,
        })
        return []

    def get_parameters(self):
        return {}


class _ProbeStrategyIntraday(Strategy):
    """Same shape as _ProbeStrategyDaily but timeframes=['5m']. Used to
    confirm intraday session-reset behavior is unchanged when
    bar_timeframe='5m' (the default)."""

    archetype = "mean_reversion"
    thesis = "Probe strategy with sma_20 — used by the intraday session-reset preservation test."
    supported_assets = ["stocks"]
    timeframes = ["5m"]

    def __init__(self):
        self.calls: list[dict] = []
        self.session_starts: list[datetime] = []

    def on_session_start(self, ts, ctx):
        self.session_starts.append(ts)

    def on_bar(self, bar, position, ctx):
        recent = ctx.recent(150)
        self.calls.append({
            "ts": bar.timestamp,
            "n_recent": len(recent),
        })
        return []

    def get_parameters(self):
        return {}


def test_daily_strategy_on_resampled_data_warms_indicators():
    """The Additional-ask-B smoke test. Daily strategy + bar_timeframe='1d'
    on resampled bars: the engine must NOT reset session_bars at session
    boundaries, so sma_100 warms up after the 100th daily bar."""
    bars_5m = _synthesize_5m_rth_bars(n_sessions=120)
    bars_1d = resample(bars_5m, "1d")

    # 120 sessions in, 120 daily bars out (one per session).
    assert len(bars_1d) == 120, f"resample 5m→1d should yield one bar per session; got {len(bars_1d)}"

    strat = _ProbeStrategyDaily()
    cfg = BacktestConfig(
        starting_capital=10_000,
        slippage=0.0,
        session=RegularTradingHours(),
        bar_timeframe="1d",
    )
    run_backtest("AMD", bars_1d, strat, cfg)

    # 1. Strategy was invoked once per daily bar.
    assert len(strat.calls) == 120

    # 2. on_session_start fired exactly once at the very first bar
    #    (continuous-session semantics for daily-or-coarser timeframes).
    assert len(strat.session_starts) == 1, (
        f"Daily strategy should get on_session_start once at series start, not "
        f"per-bar; got {len(strat.session_starts)} calls"
    )

    # 3. session_bars accumulates across bars — n_recent grows from 1 to 120
    #    instead of resetting to 1 each bar.
    assert strat.calls[0]["n_recent"] == 1
    assert strat.calls[10]["n_recent"] == 11
    assert strat.calls[99]["n_recent"] == 100
    assert strat.calls[119]["n_recent"] == 120

    # 4. sma_100 is None until 100 bars accumulate, then non-None onward.
    #    This is the smoking-gun assertion: with the broken (session-reset
    #    every bar) behavior, sma_100 would be None on every single call.
    assert strat.calls[98]["sma_100"] is None, (
        "sma_100 should still be None at bar 99 (only 99 bars in window)"
    )
    assert strat.calls[99]["sma_100"] is not None, (
        f"sma_100 should warm up at bar 100; got None — daily session reset is firing"
    )
    # Spot-check a later bar.
    assert strat.calls[119]["sma_100"] is not None


def test_intraday_default_preserves_session_reset():
    """Pin prior behavior. With bar_timeframe='5m' (default), session_bars
    must reset at every RTH session start: the strategy sees session 1
    bars, then a fresh window for session 2. on_session_start fires once
    per session. Catches accidental regression of intraday flow."""
    bars_5m = _synthesize_5m_rth_bars(n_sessions=3)  # 234 5-min bars

    strat = _ProbeStrategyIntraday()
    cfg = BacktestConfig(
        starting_capital=10_000,
        slippage=0.0,
        session=RegularTradingHours(),
        # bar_timeframe defaults to "5m" — explicit here for readability.
        bar_timeframe="5m",
    )
    run_backtest("AMD", bars_5m, strat, cfg)

    assert len(strat.calls) == 234

    # on_session_start fires once per session = 3 times.
    assert len(strat.session_starts) == 3, (
        f"Intraday default should fire on_session_start per RTH session; got "
        f"{len(strat.session_starts)}"
    )

    # Each session resets session_bars: bar 78 (last of session 1) sees 78
    # bars; bar 79 (first of session 2) sees 1 bar.
    assert strat.calls[77]["n_recent"] == 78, "last bar of session 1 should see all 78"
    assert strat.calls[78]["n_recent"] == 1, (
        f"first bar of session 2 should see fresh session_bars (1 bar); "
        f"got {strat.calls[78]['n_recent']}"
    )
    assert strat.calls[155]["n_recent"] == 78, "last bar of session 2 should see all 78"
    assert strat.calls[156]["n_recent"] == 1, "first bar of session 3 should see 1"
