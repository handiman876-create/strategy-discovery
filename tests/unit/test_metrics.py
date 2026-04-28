"""Metrics computation tests."""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from engine.backtester import BacktestConfig, run_backtest
from engine.metrics import (
    compute_metrics,
    plot_equity_curve,
    print_aggregate_metrics,
    print_metrics,
    save_trade_log,
)
from engine.session import CryptoSession

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "strategies"))
from manual.buy_and_hold import BuyAndHold

UTC = ZoneInfo("UTC")


def _bars(prices):
    base = datetime(2024, 1, 1, tzinfo=UTC)
    rows = []
    for i, p in enumerate(prices):
        rows.append({
            "timestamp": base + pd.Timedelta(hours=i),
            "open": p, "high": p + 0.5, "low": p - 0.5, "close": p, "volume": 1000.0,
        })
    return pd.DataFrame(rows)


@pytest.fixture
def winning_result():
    df = _bars([100, 101, 102, 103, 104, 105])
    cfg = BacktestConfig(
        starting_capital=10_000, commission=0.0, slippage=0.0, session=CryptoSession()
    )
    return run_backtest("X", df, BuyAndHold(), cfg)


def test_compute_metrics_basic(winning_result):
    m = compute_metrics(winning_result)
    assert m["symbol"] == "X"
    assert m["total_trades"] == 1
    assert m["win_rate"] == 1.0
    assert m["total_pnl"] == pytest.approx(4.0)
    assert m["max_drawdown_dollar"] <= 0
    assert m["max_drawdown_pct"] <= 0


def test_compute_metrics_empty():
    df = _bars([100])  # one bar, no trade can complete
    cfg = BacktestConfig(
        starting_capital=10_000, slippage=0, commission=0, session=CryptoSession()
    )
    result = run_backtest("X", df, BuyAndHold(), cfg)
    m = compute_metrics(result)
    assert m["total_trades"] == 0
    assert m["win_rate"] == 0.0


def test_print_metrics_runs(winning_result, capsys):
    print_metrics(compute_metrics(winning_result))
    captured = capsys.readouterr()
    assert "Backtest Results" in captured.out
    assert "Sharpe ratio" in captured.out


def test_print_aggregate_runs(winning_result, capsys):
    m = compute_metrics(winning_result)
    print_aggregate_metrics([m, m])
    captured = capsys.readouterr()
    assert "AGGREGATE" in captured.out


def test_save_trade_log(winning_result, tmp_path):
    path = save_trade_log(winning_result, tmp_path)
    assert path.exists()
    df = pd.read_csv(path)
    assert "entry_time" in df.columns
    assert "exit_reason" in df.columns


def test_save_trade_log_no_trades(tmp_path):
    df = _bars([100])
    cfg = BacktestConfig(slippage=0, commission=0, session=CryptoSession())
    result = run_backtest("X", df, BuyAndHold(), cfg)
    path = save_trade_log(result, tmp_path)
    assert path.exists()


def test_plot_equity_curve_writes_png(winning_result, tmp_path):
    path = plot_equity_curve(winning_result, tmp_path)
    assert path is not None
    assert path.exists()
    assert path.suffix == ".png"
