"""Performance metrics, trade-log CSV writing, and equity-curve plotting."""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

import pandas as pd

from .backtester import BacktestResult


def compute_metrics(result: BacktestResult) -> dict:
    trades = result.trades
    equity = result.equity_curve
    starting = result.config.starting_capital

    if not trades:
        return {
            "symbol": result.symbol,
            "total_trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "total_pnl": 0.0,
            "gross_win": 0.0,
            "gross_loss": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "avg_trade": 0.0,
            "max_drawdown_dollar": 0.0,
            "max_drawdown_pct": 0.0,
            "avg_duration_mins": 0.0,
            "sharpe_ratio": 0.0,
            "starting_capital": starting,
            "ending_capital": starting,
        }

    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins) if wins else 0.0
    gross_loss = abs(sum(losses)) if losses else 0.0
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    max_dd_dollar, max_dd_pct = _max_drawdown(equity)
    durations = [t.duration_mins for t in trades]
    sharpe = _sharpe_ratio(equity)

    return {
        "symbol": result.symbol,
        "total_trades": len(trades),
        "win_rate": len(wins) / len(trades),
        "profit_factor": pf,
        "total_pnl": sum(pnls),
        "gross_win": gross_win,
        "gross_loss": -gross_loss,
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "avg_trade": sum(pnls) / len(pnls),
        "max_drawdown_dollar": max_dd_dollar,
        "max_drawdown_pct": max_dd_pct,
        "avg_duration_mins": sum(durations) / len(durations),
        "sharpe_ratio": sharpe,
        "starting_capital": starting,
        "ending_capital": equity.iloc[-1] if not equity.empty else starting,
    }


def _max_drawdown(equity: pd.Series) -> tuple[float, float]:
    if equity.empty:
        return 0.0, 0.0
    roll_max = equity.cummax()
    dd_dollar = (equity - roll_max).min()
    dd_pct = ((equity - roll_max) / roll_max).min()
    return float(dd_dollar), float(dd_pct)


def _sharpe_ratio(equity: pd.Series, periods_per_year: int = 252) -> float:
    if equity.empty or len(equity) < 2:
        return 0.0
    daily = equity.resample("D").last().dropna()
    rets = daily.pct_change().dropna()
    if rets.std() == 0:
        return 0.0
    return float(rets.mean() / rets.std() * math.sqrt(periods_per_year))


def print_metrics(m: dict) -> None:
    w = 28
    print(f"\n{'─'*45}")
    print(f"  {m['symbol']}  Backtest Results")
    print(f"{'─'*45}")
    print(f"  {'Total trades':<{w}} {m['total_trades']}")
    print(f"  {'Win rate':<{w}} {m['win_rate']*100:.1f}%")
    print(f"  {'Profit factor':<{w}} {m['profit_factor']:.2f}")
    print(f"  {'Total P&L':<{w}} ${m['total_pnl']:,.2f}")
    print(f"  {'Avg trade':<{w}} ${m['avg_trade']:,.4f}")
    print(f"  {'Avg win':<{w}} ${m['avg_win']:,.2f}")
    print(f"  {'Avg loss':<{w}} ${m['avg_loss']:,.2f}")
    print(f"  {'Max drawdown ($)':<{w}} ${m['max_drawdown_dollar']:,.2f}")
    print(f"  {'Max drawdown (%)':<{w}} {m['max_drawdown_pct']*100:.2f}%")
    print(f"  {'Avg trade duration':<{w}} {m['avg_duration_mins']:.0f} min")
    print(f"  {'Sharpe ratio':<{w}} {m['sharpe_ratio']:.2f}")
    print(f"  {'Starting capital':<{w}} ${m['starting_capital']:,.2f}")
    print(f"  {'Ending capital':<{w}} ${m['ending_capital']:,.2f}")
    print(f"{'─'*45}")


def print_aggregate_metrics(all_metrics: list[dict]) -> None:
    if not all_metrics:
        return
    total_trades = sum(m["total_trades"] for m in all_metrics)
    total_wins = sum(m["total_trades"] * m["win_rate"] for m in all_metrics)
    total_pnl = sum(m["total_pnl"] for m in all_metrics)
    gross_win = sum(m["gross_win"] for m in all_metrics)
    gross_loss = abs(sum(m["gross_loss"] for m in all_metrics))
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    print(f"\n{'═'*45}")
    print(f"  AGGREGATE  ({len(all_metrics)} symbols)")
    print(f"{'═'*45}")
    print(f"  {'Total trades':<28} {total_trades}")
    if total_trades:
        print(f"  {'Win rate':<28} {total_wins/total_trades*100:.1f}%")
    print(f"  {'Profit factor':<28} {pf:.2f}")
    print(f"  {'Total P&L':<28} ${total_pnl:,.2f}")
    print(f"{'═'*45}")


def save_trade_log(result: BacktestResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{result.symbol}_trades.csv"
    if not result.trades:
        pd.DataFrame().to_csv(path, index=False)
        return path
    rows = []
    for t in result.trades:
        rows.append(
            {
                "symbol": t.symbol,
                "side": t.side,
                "size": t.size,
                "entry_time": t.entry_time.strftime("%Y-%m-%d %H:%M %Z"),
                "entry_price": round(t.entry_price, 4),
                "exit_time": t.exit_time.strftime("%Y-%m-%d %H:%M %Z"),
                "exit_price": round(t.exit_price, 4),
                "exit_reason": t.exit_reason,
                "signal_label": t.signal_label,
                "pnl": round(t.pnl, 4),
                "pnl_pct": round(t.pnl_pct * 100, 4),
                "duration_mins": t.duration_mins,
            }
        )
    pd.DataFrame(rows).to_csv(path, index=False)
    return path


def plot_equity_curve(result: BacktestResult, out_dir: Path) -> Path | None:
    """Two-panel PNG: equity curve (top) + drawdown % (bottom)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    equity = result.equity_curve
    if equity.empty:
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{result.symbol}_equity.png"

    roll_max = equity.cummax()
    dd_pct = (equity - roll_max) / roll_max * 100

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(12, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )
    ax1.plot(equity.index, equity.values, linewidth=1.0, color="#1f77b4")
    ax1.axhline(
        result.config.starting_capital, color="gray", linewidth=0.8, linestyle="--", alpha=0.6
    )
    ax1.set_title(f"{result.symbol} Equity Curve")
    ax1.set_ylabel("Portfolio Value ($)")
    ax1.grid(True, alpha=0.3)

    ax2.fill_between(dd_pct.index, dd_pct.values, 0, color="#d62728", alpha=0.4)
    ax2.set_ylabel("Drawdown (%)")
    ax2.set_xlabel("Date")
    ax2.grid(True, alpha=0.3)

    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax2.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
    fig.autofmt_xdate()
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    return path
