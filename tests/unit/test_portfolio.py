"""Portfolio bookkeeping tests."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from engine.portfolio import Portfolio, Position, Trade

ET = ZoneInfo("America/New_York")


def _ts(h=10, m=0):
    return datetime(2024, 5, 15, h, m, tzinfo=ET)


def test_initial_state():
    p = Portfolio(starting_capital=10_000)
    assert p.cash == 10_000
    assert p.position is None
    assert p.trades == []


def test_open_close_long_profit():
    p = Portfolio(starting_capital=10_000)
    p.open_position(
        Position("AMD", "long", 1, 100.0, _ts(10, 0), 99.0, 102.0)
    )
    trade = p.close_position(101.5, _ts(10, 30), "target", commission=0.0, slippage=0.01)
    assert trade.pnl == pytest.approx(1.5)
    assert p.cash == pytest.approx(10_001.5)
    assert p.position is None
    assert len(p.trades) == 1


def test_open_close_short_profit():
    p = Portfolio(starting_capital=10_000)
    p.open_position(Position("AMD", "short", 1, 100.0, _ts(10, 0), 101.0, 98.0))
    trade = p.close_position(99.0, _ts(10, 30), "target", commission=0.0, slippage=0.01)
    assert trade.pnl == pytest.approx(1.0)
    assert p.cash == pytest.approx(10_001.0)


def test_close_with_no_position_raises():
    p = Portfolio(starting_capital=10_000)
    with pytest.raises(RuntimeError):
        p.close_position(100.0, _ts(), "stop", 0, 0.01)


def test_double_open_raises():
    p = Portfolio(starting_capital=10_000)
    p.open_position(Position("X", "long", 1, 100.0, _ts(), 99.0, 102.0))
    with pytest.raises(RuntimeError):
        p.open_position(Position("X", "long", 1, 100.0, _ts(), 99.0, 102.0))


def test_mark_to_market_with_position():
    p = Portfolio(starting_capital=10_000)
    p.open_position(Position("AMD", "long", 2, 100.0, _ts(10, 0), 99.0, 102.0))
    p.mark_to_market(_ts(10, 5), 101.0)
    eq = p.equity_curve()
    assert eq.iloc[0] == pytest.approx(10_002.0)


def test_mark_to_market_no_position():
    p = Portfolio(starting_capital=10_000)
    p.mark_to_market(_ts(), 100.0)
    assert p.equity_curve().iloc[0] == 10_000


def test_trade_pnl_pct_long():
    t = Trade("X", "long", 1, 100.0, _ts(10, 0), 110.0, _ts(10, 30), "target", 0, 0.01)
    assert t.pnl_pct == pytest.approx(0.10)


def test_trade_pnl_pct_short():
    t = Trade("X", "short", 1, 100.0, _ts(10, 0), 90.0, _ts(10, 30), "target", 0, 0.01)
    assert t.pnl_pct == pytest.approx(0.10)


def test_trade_duration():
    t = Trade("X", "long", 1, 100.0, _ts(10, 0), 110.0, _ts(10, 45), "target", 0, 0.01)
    assert t.duration_mins == 45
