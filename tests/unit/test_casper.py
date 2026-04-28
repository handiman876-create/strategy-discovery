"""Casper state-machine tests on synthetic bars."""

from __future__ import annotations

import math
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from engine.backtester import BacktestConfig, run_backtest
from engine.session import RegularTradingHours

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "strategies"))
from manual.casper import CasperStrategy

ET = ZoneInfo("America/New_York")


def _make_bars(rows):
    """rows: list of (HH:MM, open, high, low, close)."""
    df_rows = []
    base = datetime(2024, 5, 15, 9, 30, tzinfo=ET)
    for hhmm, o, h, l, c in rows:
        h_, m_ = map(int, hhmm.split(":"))
        ts = base.replace(hour=h_, minute=m_)
        df_rows.append(
            {"timestamp": ts, "open": o, "high": h, "low": l, "close": c, "volume": 1000.0}
        )
    return pd.DataFrame(df_rows)


def _cfg(realistic_fills=False):
    return BacktestConfig(
        starting_capital=10_000,
        commission=0.0,
        slippage=0.01,
        realistic_fills=realistic_fills,
        session=RegularTradingHours(),
    )


# Helpers for crafting scenarios — OR is 100/99 (high/low), size 1.


def _or_bars():
    return [("09:30", 99.5, 100.0, 99.0, 99.5)]


def test_no_breakout_no_trade():
    """Bars stay inside OR all session — no entry."""
    rows = _or_bars()
    for i in range(1, 78):
        h, m = divmod(30 + 5 * i, 60)
        rows.append((f"{9+h:02d}:{m:02d}", 99.5, 99.8, 99.2, 99.5))
    df = _make_bars(rows)
    res = run_backtest("X", df, CasperStrategy(retest_timeout=math.inf), _cfg())
    assert len(res.trades) == 0


def test_long_retest_entry():
    """Two 5-min closes above OR, then a wick-back-and-close-above retest."""
    rows = _or_bars()
    rows += [
        ("09:35", 100.0, 101.0, 100.0, 100.5),  # confirm 1
        ("09:40", 100.5, 101.5, 100.5, 101.0),  # confirm 2 → WAIT_RETEST
        ("09:45", 101.0, 101.5, 99.5, 100.5),   # wicks into OR (99.5 ≤ 100) and closes above (100.5 > 100) → entry
        ("09:50", 100.6, 101.0, 100.4, 100.9),  # next bar — entry fills here at 100.6
        ("09:55", 100.9, 101.5, 100.7, 101.2),  # walking up
        ("10:00", 101.2, 101.7, 100.9, 101.5),
    ]
    df = _make_bars(rows)
    res = run_backtest("X", df, CasperStrategy(retest_timeout=math.inf), _cfg())
    assert len(res.trades) >= 1
    t = res.trades[0]
    assert t.side == "long"
    # Entry signal at 09:45 close (100.5); fills at 09:50 open (100.6) + slippage
    assert t.entry_price == pytest.approx(100.61)


def test_short_retest_entry():
    rows = _or_bars()
    rows += [
        ("09:35", 99.0, 99.0, 98.0, 98.5),
        ("09:40", 98.5, 98.7, 98.0, 98.2),  # confirm 2 short
        ("09:45", 98.5, 99.5, 98.4, 98.5),  # wick into OR (99.5 >= 99) AND close < 99 → entry
        ("09:50", 98.4, 98.5, 98.0, 98.2),  # fills here
    ]
    df = _make_bars(rows)
    res = run_backtest("X", df, CasperStrategy(retest_timeout=math.inf), _cfg())
    assert len(res.trades) >= 1
    assert res.trades[0].side == "short"


def test_close_inside_resets_confirm():
    rows = _or_bars()
    rows += [
        ("09:35", 100.5, 101.0, 100.0, 100.5),  # confirm 1 long
        ("09:40", 100.0, 100.5, 99.5, 99.7),    # close INSIDE → resets
        ("09:45", 100.0, 100.3, 99.5, 99.5),    # still inside / right at low
        # only one above-close after the reset; never gets to confirm 2 by entry_cutoff
    ]
    for i in range(15):
        rows.append((f"10:{i*5:02d}" if i*5 < 60 else f"11:{(i*5-60):02d}",
                     99.6, 99.8, 99.4, 99.6))
    df = _make_bars(rows)
    res = run_backtest("X", df, CasperStrategy(retest_timeout=math.inf), _cfg())
    assert len(res.trades) == 0


def test_entry_cutoff_kills_signal():
    """Confirm + retest happen AFTER entry_cutoff → no entry."""
    rows = _or_bars()
    # Pad inside-range bars 09:35 through 11:00
    for i in range(1, 18):
        h, m = divmod(30 + 5 * i, 60)
        rows.append((f"{9+h:02d}:{m:02d}", 99.5, 99.8, 99.3, 99.5))
    # Then breakout at 11:05 — too late
    rows.append(("11:05", 100.0, 101.0, 100.0, 100.5))
    rows.append(("11:10", 100.5, 101.5, 100.5, 101.0))
    rows.append(("11:15", 101.0, 101.5, 99.5, 100.5))  # would-be entry
    df = _make_bars(rows)
    res = run_backtest("X", df, CasperStrategy(retest_timeout=math.inf), _cfg())
    assert len(res.trades) == 0


def test_target_hit_long():
    """OR 99-100, entry 100.5 (signal close), stop 99.0 (opposite_bracket),
    target = 100.5 + 1.5 * 2 = 103.5. Next bar opens 100.6, fills at 100.61
    (with slippage). Following bar reaches target 103.5 → fills exactly.
    PnL = (103.5 - 100.61) * 1 = 2.89."""
    rows = _or_bars()
    rows += [
        ("09:35", 100.0, 101.0, 100.0, 100.5),
        ("09:40", 100.5, 101.5, 100.5, 101.0),  # confirm 2
        ("09:45", 101.0, 101.5, 99.5, 100.5),   # retest entry signal
        ("09:50", 100.6, 100.7, 100.5, 100.65), # fill bar
        ("09:55", 100.7, 103.6, 100.6, 103.5),  # target hit
    ]
    df = _make_bars(rows)
    res = run_backtest("X", df, CasperStrategy(retest_timeout=math.inf), _cfg())
    assert len(res.trades) == 1
    t = res.trades[0]
    assert t.exit_reason == "target"
    assert t.exit_price == pytest.approx(103.5)
    assert t.pnl == pytest.approx(103.5 - 100.61)


def test_stop_hit_long():
    """Entry as above. Next bar after fill drops to 98.5 → stop at 99.0 hits."""
    rows = _or_bars()
    rows += [
        ("09:35", 100.0, 101.0, 100.0, 100.5),
        ("09:40", 100.5, 101.5, 100.5, 101.0),
        ("09:45", 101.0, 101.5, 99.5, 100.5),
        ("09:50", 100.6, 100.7, 100.5, 100.65),
        ("09:55", 100.5, 100.6, 98.5, 98.7),  # stop hit at 99.0
    ]
    df = _make_bars(rows)
    res = run_backtest("X", df, CasperStrategy(retest_timeout=math.inf), _cfg())
    t = res.trades[0]
    assert t.exit_reason == "stop"
    assert t.exit_price == pytest.approx(99.0)


def test_eod_exit():
    """Position open at 15:50 closes at that bar's close."""
    rows = _or_bars()
    rows += [
        ("09:35", 100.0, 101.0, 100.0, 100.5),
        ("09:40", 100.5, 101.5, 100.5, 101.0),
        ("09:45", 101.0, 101.5, 99.5, 100.5),  # entry
        ("09:50", 100.6, 100.7, 100.5, 100.65),  # fills
    ]
    # Pad until 15:50 with flat prices well within stop/target.
    cur = datetime(2024, 5, 15, 9, 55, tzinfo=ET)
    while cur.time() <= __import__("datetime").time(15, 55):
        rows.append((cur.strftime("%H:%M"), 100.7, 100.9, 100.5, 100.7))
        cur = cur + pd.Timedelta(minutes=5)
    df = _make_bars(rows)
    res = run_backtest("X", df, CasperStrategy(retest_timeout=math.inf), _cfg())
    t = res.trades[0]
    assert t.exit_reason == "eod"


def test_one_trade_per_day_when_disallow_multi():
    rows = _or_bars()
    rows += [
        ("09:35", 100.0, 101.0, 100.0, 100.5),
        ("09:40", 100.5, 101.5, 100.5, 101.0),
        ("09:45", 101.0, 101.5, 99.5, 100.5),
        ("09:50", 100.6, 102.0, 100.5, 102.0),  # quick win path; EOD will close
    ]
    # Add bars producing another retest later in the day; should NOT create a 2nd trade.
    for i in range(40):
        ts = datetime(2024, 5, 15, 10, 0, tzinfo=ET) + pd.Timedelta(minutes=5 * i)
        rows.append((ts.strftime("%H:%M"), 100.0, 101.0, 99.5, 100.5))
    df = _make_bars(rows)
    res = run_backtest(
        "X", df, CasperStrategy(retest_timeout=math.inf, allow_multiple_breakouts=False), _cfg()
    )
    assert len(res.trades) == 1


def test_multiple_breakouts_can_create_multiple_trades():
    rows = _or_bars()
    rows += [
        ("09:35", 100.0, 101.0, 100.0, 100.5),
        ("09:40", 100.5, 101.5, 100.5, 101.0),
        ("09:45", 101.0, 101.5, 99.5, 100.5),  # entry 1
        ("09:50", 100.6, 100.7, 100.5, 100.65),
        ("09:55", 100.6, 100.6, 98.5, 98.7),   # stop 1; re-arm to WAIT_RETEST
        ("10:00", 99.0, 100.5, 98.5, 100.3),
        ("10:05", 100.3, 100.5, 99.6, 100.5),  # wick into OR (99.6 ≤ 100) AND close > 100 → entry 2
        ("10:10", 100.6, 100.7, 100.5, 100.65),
    ]
    df = _make_bars(rows)
    res = run_backtest(
        "X",
        df,
        CasperStrategy(retest_timeout=math.inf, allow_multiple_breakouts=True),
        _cfg(),
    )
    assert len(res.trades) >= 2


def test_retest_timeout_kills_signal():
    rows = _or_bars()
    rows += [
        ("09:35", 100.0, 101.0, 100.0, 100.5),
        ("09:40", 100.5, 101.5, 100.5, 101.0),  # confirmed long → WAIT_RETEST
    ]
    # 5 bars that don't produce a retest condition (price stays above OR, no wick back)
    for i in range(5):
        ts = datetime(2024, 5, 15, 9, 45, tzinfo=ET) + pd.Timedelta(minutes=5 * i)
        rows.append((ts.strftime("%H:%M"), 100.5, 100.7, 100.3, 100.6))
    # After timeout=3, a perfect retest at 10:10 should still NOT trigger.
    rows.append(("10:10", 100.5, 100.6, 99.5, 100.5))
    df = _make_bars(rows)
    res = run_backtest("X", df, CasperStrategy(retest_timeout=3), _cfg())
    assert len(res.trades) == 0


def test_get_parameters_round_trip():
    p = CasperStrategy().get_parameters()
    for k in (
        "stop_mode",
        "rr_ratio",
        "min_bars_beyond_or",
        "retest_timeout",
        "allow_multiple_breakouts",
        "momentum_fallback",
    ):
        assert k in p
