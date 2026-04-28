"""Significance test checks: bootstrap CI + random baseline construction."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from engine.portfolio import Trade
from evaluation.significance import (
    bootstrap_profit_factor,
    profit_factor,
    random_baseline,
    trade_count_warning,
    PF_CAP,
)

ET = ZoneInfo("America/New_York")


def _trade(pnl: float):
    ts = datetime(2024, 5, 15, 10, 0, tzinfo=ET)
    side = "long" if pnl >= 0 else "long"
    return Trade("X", side, 1, 100, ts, 100 + pnl, ts, "target", 0, 0.01)


def test_profit_factor_basic():
    # 2 wins of $1, 1 loss of $1 → PF = 2/1 = 2
    pnls = [1.0, 1.0, -1.0]
    assert profit_factor(pnls) == pytest.approx(2.0)


def test_profit_factor_only_wins_caps_at_pf_cap():
    pnls = [1.0, 2.0, 3.0]
    assert profit_factor(pnls) == PF_CAP


def test_profit_factor_only_losses_zero():
    pnls = [-1.0, -2.0]
    assert profit_factor(pnls) == 0.0


def test_bootstrap_ci_around_point_estimate():
    # Build a clearly-positive PF: 100 wins of $1, 50 losses of $1 → PF=2
    trades = [_trade(1.0)] * 100 + [_trade(-1.0)] * 50
    boot = bootstrap_profit_factor(trades, n_resamples=2000, seed=42)
    assert boot.point_estimate == pytest.approx(2.0)
    # CI should bracket the point.
    assert boot.ci_lower < boot.point_estimate < boot.ci_upper
    # Lower bound should be > 1.0 for this strong sample.
    assert boot.ci_lower > 1.0


def test_bootstrap_empty_trades():
    boot = bootstrap_profit_factor([], n_resamples=100)
    assert boot.point_estimate == 0.0


def test_bootstrap_seed_reproducible():
    trades = [_trade(1.0), _trade(-1.0), _trade(2.0)] * 30
    a = bootstrap_profit_factor(trades, n_resamples=500, seed=7)
    b = bootstrap_profit_factor(trades, n_resamples=500, seed=7)
    assert a.ci_lower == b.ci_lower
    assert a.ci_upper == b.ci_upper


def test_trade_count_warning_under_threshold():
    msg = trade_count_warning(50)
    assert msg is not None and "Under-sampled" in msg


def test_trade_count_warning_at_threshold():
    assert trade_count_warning(100) is None
    assert trade_count_warning(150) is None


def _synth_session_bars(date_str: str, n_bars: int = 78):
    """One session of fake 5-min RTH bars starting at 09:30 ET."""
    base = pd.Timestamp(f"{date_str} 09:30", tz="America/New_York")
    rows = []
    price = 100.0
    for i in range(n_bars):
        ts = base + pd.Timedelta(minutes=5 * i)
        # OR bar (i=0): set initial range
        if i == 0:
            rows.append({"timestamp": ts, "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1000.0})
        else:
            price = 100 + (i - n_bars / 2) * 0.05
            rows.append({"timestamp": ts, "open": price, "high": price + 0.3, "low": price - 0.3, "close": price + 0.1, "volume": 1000.0})
    return pd.DataFrame(rows)


def test_random_baseline_returns_valid_distribution():
    bars = _synth_session_bars("2024-05-15", n_bars=78)
    # Pretend the strategy entered twice in this session.
    fake_trades = [
        Trade("X", "long", 1, 100, bars.iloc[10]["timestamp"], 101, bars.iloc[20]["timestamp"], "target", 0, 0.01),
        Trade("X", "short", 1, 100, bars.iloc[30]["timestamp"], 99, bars.iloc[40]["timestamp"], "target", 0, 0.01),
    ]
    res = random_baseline(bars, fake_trades, m_trials=20, seed=42)
    assert res.n_trials == 20
    assert len(res.baseline_pfs) == 20
    assert 0.0 <= res.p_value <= 1.0


def test_random_baseline_empty_strategy_trades():
    bars = _synth_session_bars("2024-05-15")
    res = random_baseline(bars, [], m_trials=5, seed=1)
    # No strategy trades → no baseline trades; PFs all 0; p-value 1.0
    assert all(pf == 0.0 for pf in res.baseline_pfs)
    assert res.p_value == 1.0
