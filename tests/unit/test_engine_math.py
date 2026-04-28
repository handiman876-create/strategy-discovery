"""Engine math smoke test using BuyAndHold on synthetic bars."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from engine.backtester import BacktestConfig, run_backtest
from engine.session import CryptoSession

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "strategies"))
from manual.buy_and_hold import BuyAndHold

UTC = ZoneInfo("UTC")


def _bars(prices: list[float]):
    base = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    rows = []
    for i, p in enumerate(prices):
        rows.append(
            {
                "timestamp": base + pd.Timedelta(hours=i),
                "open": p,
                "high": p + 0.5,
                "low": p - 0.5,
                "close": p,
                "volume": 1000.0,
            }
        )
    return pd.DataFrame(rows)


def test_buy_and_hold_round_trip_pnl():
    """
    BuyAndHold submits a market buy on bar 0; fills at bar 1's open + slippage.
    Lingering position at end of run is closed at last bar's close - slippage.
    Round-trip P&L = (last_close - bar_1_open) - 2*slippage - 2*commission.
    """
    prices = [100, 101, 102, 103, 104, 105]
    df = _bars(prices)

    cfg = BacktestConfig(
        starting_capital=10_000,
        commission=0.0,
        slippage=0.01,
        realistic_fills=True,
        session=CryptoSession(),
    )
    result = run_backtest("BTC", df, BuyAndHold(), cfg)

    assert len(result.trades) == 1
    t = result.trades[0]
    assert t.entry_price == pytest.approx(101 + 0.01)  # bar 1 open + slippage
    assert t.exit_price == pytest.approx(105 - 0.01)   # last close - slippage
    assert t.pnl == pytest.approx((105 - 0.01) - (101 + 0.01))


def test_buy_and_hold_with_commission():
    prices = [100, 101, 102]
    df = _bars(prices)
    cfg = BacktestConfig(
        starting_capital=10_000,
        commission=0.5,
        slippage=0.0,
        realistic_fills=True,
        session=CryptoSession(),
    )
    result = run_backtest("X", df, BuyAndHold(), cfg)
    t = result.trades[0]
    # Expected pnl: (102 - 101) - 2 * 0.5 commission = 0.0
    assert t.pnl == pytest.approx(0.0)


def test_equity_curve_length_matches_bars():
    df = _bars([100, 101, 102, 103])
    cfg = BacktestConfig(session=CryptoSession())
    result = run_backtest("X", df, BuyAndHold(), cfg)
    assert len(result.equity_curve) == 4


def test_starting_capital_respected():
    df = _bars([100, 101])
    cfg = BacktestConfig(starting_capital=50_000, slippage=0, session=CryptoSession())
    result = run_backtest("X", df, BuyAndHold(), cfg)
    # With slippage=0 and price unchanged at exit, PnL is 0; cash stays at 50k.
    assert result.portfolio.cash == 50_000
