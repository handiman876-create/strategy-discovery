"""Walk-forward analysis with parameter optimization on each train window.

The walk-forward IS the train/test mechanism in our framework. We don't have
a single fixed train/test boundary; instead, we slide a (train_window,
test_window) pair across the non-holdout span. For each step:

  1. Optimize parameters on the train window (grid search; objective:
     PF if trades >= 30 else -inf).
  2. Apply best params to the next test_window period (out of sample).
  3. Record the OOS results.

Aggregating OOS trades across all walk-forward steps gives our test signal
without ever touching the holdout slice.

Window sizes are configured in MONTHS, not years. Reason: Polygon Stocks
Starter has a hard 5-year rolling history cap, so our train_test span is
about 44 months. Year-based 3-year-train / 1-year-test windows can't fit
the minimum-3-windows requirement; month-based config can.

For strategies with no `parameter_grid`, the optimization step is skipped:
each test window is backtested with default constructor parameters. The
window-slicing structure stays the same so that downstream aggregation is
uniform.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable

import pandas as pd

from engine.backtester import BacktestConfig
from engine.backtester import run_backtest
from engine.portfolio import Trade

from .splits import optimization_mode, slice_window


@dataclass
class WalkForwardConfig:
    """Sliding-window walk-forward parameters.

    All windows are in MONTHS. parameter_grid is a dict of name → list of
    values; the cartesian product is searched. Pass `None` to skip
    optimization entirely.
    """

    train_window_months: int = 24
    test_window_months: int = 6
    step_months: int = 6
    parameter_grid: dict[str, list[Any]] | None = None
    min_trades_for_objective: int = 30


@dataclass
class WindowResult:
    train_start: date
    train_end_exclusive: date
    test_start: date
    test_end_exclusive: date
    best_params: dict[str, Any]
    train_pf: float
    train_n_trades: int
    test_trades: list[Trade]
    test_pf: float
    test_n_trades: int


@dataclass
class WalkForwardResult:
    symbol: str
    config: WalkForwardConfig
    windows: list[WindowResult] = field(default_factory=list)

    @property
    def all_oos_trades(self) -> list[Trade]:
        out: list[Trade] = []
        for w in self.windows:
            out.extend(w.test_trades)
        return out


# ── Public API ───────────────────────────────────────────────────────────────


def walk_forward(
    symbol: str,
    bars: pd.DataFrame,
    strategy_factory: Callable[..., Any],
    backtest_config: BacktestConfig,
    config: WalkForwardConfig,
) -> WalkForwardResult:
    """Run walk-forward analysis on `bars` (already trimmed to non-holdout span).

    `strategy_factory(**params) -> Strategy` constructs a fresh instance per
    backtest. `bars` must NOT contain holdout data; the caller is expected to
    have loaded via `splits.train_test_load`.
    """
    if bars.empty:
        return WalkForwardResult(symbol=symbol, config=config)

    span_start = bars["timestamp"].iloc[0].date()
    span_end_excl = bars["timestamp"].iloc[-1].date()
    windows = _enumerate_windows(span_start, span_end_excl, config)
    grid = _expand_grid(config.parameter_grid)

    # Indicator warmup for the OOS backtest. The context retains at most
    # context_lookback bars, so prepending that many strictly-historical bars
    # before each test window fully initializes any indicator the context can
    # serve (e.g. a 200-bar MA) at the window's first OOS bar.
    warmup_bars = getattr(backtest_config, "context_lookback", 0) or 0

    result = WalkForwardResult(symbol=symbol, config=config)

    for train_start, train_end, test_start, test_end in windows:
        train_bars = slice_window(bars, train_start, train_end)
        test_bars = slice_window(bars, test_start, test_end)
        if train_bars.empty or test_bars.empty:
            continue

        if grid is None:
            best_params: dict[str, Any] = {}
            with optimization_mode():  # still mark as optimization for safety
                train_strat = strategy_factory()
                train_res = run_backtest(symbol, train_bars, train_strat, backtest_config)
            train_pf = _pf(train_res.trades)
            train_n = len(train_res.trades)
        else:
            best_params, train_pf, train_n = _optimize(
                symbol, train_bars, strategy_factory, grid, backtest_config, config
            )

        test_strat = strategy_factory(**best_params)
        # Prepend indicator warmup so long-lookback indicators are initialized
        # at the first OOS bar rather than returning None for the whole window.
        # Warmup bars feed the context only; trades entered before test_start
        # are dropped by _oos_only, so this adds no look-ahead to the OOS signal.
        test_input = _prepend_warmup(bars, test_bars, test_start, warmup_bars)
        test_res = run_backtest(symbol, test_input, test_strat, backtest_config)
        oos_trades = _oos_only(test_res.trades, test_start, bars["timestamp"].dt.tz)

        result.windows.append(
            WindowResult(
                train_start=train_start,
                train_end_exclusive=train_end,
                test_start=test_start,
                test_end_exclusive=test_end,
                best_params=best_params,
                train_pf=train_pf,
                train_n_trades=train_n,
                test_trades=oos_trades,
                test_pf=_pf(oos_trades),
                test_n_trades=len(oos_trades),
            )
        )

    return result


# ── Helpers ──────────────────────────────────────────────────────────────────


def _prepend_warmup(
    all_bars: pd.DataFrame,
    test_bars: pd.DataFrame,
    test_start: date,
    warmup_bars: int,
) -> pd.DataFrame:
    """Return `test_bars` with up to `warmup_bars` strictly-historical bars
    (timestamp < test_start) prepended, for indicator warmup. No look-ahead:
    every prepended bar predates the OOS window, and trades opened during warmup
    are excluded downstream by `_oos_only`. Falls back to `test_bars` unchanged
    when warmup is disabled or no prior history exists (early windows)."""
    if warmup_bars <= 0:
        return test_bars
    tz = all_bars["timestamp"].dt.tz
    s = pd.Timestamp(test_start, tz=tz)
    hist = all_bars[all_bars["timestamp"] < s].tail(warmup_bars)
    if hist.empty:
        return test_bars
    return pd.concat([hist, test_bars], ignore_index=True)


def _oos_only(trades: list[Trade], test_start: date, tz: Any) -> list[Trade]:
    """Keep only trades entered on/after test_start, mirroring slice_window's
    [test_start, test_end) lower bound. Drops warmup-window entries so the OOS
    signal contains exactly the in-window trades."""
    s = pd.Timestamp(test_start, tz=tz)
    return [t for t in trades if pd.Timestamp(t.entry_time) >= s]


def _add_months(d: date, months: int) -> date:
    """date arithmetic in calendar months (no day-of-month overflow worries
    because we always anchor windows at day=1)."""
    total = d.year * 12 + (d.month - 1) + months
    return date(total // 12, total % 12 + 1, 1)


def _enumerate_windows(
    span_start: date, span_last_bar: date, config: WalkForwardConfig
) -> list[tuple[date, date, date, date]]:
    """Yield (train_start, train_end_excl, test_start, test_end_excl).
    Anchors at month-start = 1st of the month after span_start."""
    out: list[tuple[date, date, date, date]] = []
    anchor = (
        span_start
        if span_start.day == 1
        else _add_months(date(span_start.year, span_start.month, 1), 1)
    )
    span_end = _add_months(date(span_last_bar.year, span_last_bar.month, 1), 1)

    train_start = anchor
    while True:
        train_end = _add_months(train_start, config.train_window_months)
        test_start = train_end
        test_end = _add_months(test_start, config.test_window_months)
        if test_end > span_end:
            break
        out.append((train_start, train_end, test_start, test_end))
        train_start = _add_months(train_start, config.step_months)
    return out


def _expand_grid(
    grid: dict[str, list[Any]] | None,
) -> list[dict[str, Any]] | None:
    if grid is None:
        return None
    if not grid:
        return [{}]
    keys = list(grid.keys())
    values = [grid[k] for k in keys]
    return [dict(zip(keys, combo)) for combo in itertools.product(*values)]


def _optimize(
    symbol: str,
    train_bars: pd.DataFrame,
    strategy_factory: Callable[..., Any],
    grid: list[dict[str, Any]],
    backtest_config: BacktestConfig,
    config: WalkForwardConfig,
) -> tuple[dict[str, Any], float, int]:
    """Grid-search on `train_bars`. Wraps the search in `optimization_mode()`
    so any accidental holdout access raises."""
    best_params: dict[str, Any] = {}
    best_obj = -math.inf
    best_pnl = -math.inf
    best_pf = 0.0
    best_n = 0
    with optimization_mode():
        for params in grid:
            strat = strategy_factory(**params)
            res = run_backtest(symbol, train_bars, strat, backtest_config)
            n = len(res.trades)
            pf = _pf(res.trades)
            obj = pf if n >= config.min_trades_for_objective else -math.inf
            pnl = sum(t.pnl for t in res.trades)
            if (obj > best_obj) or (
                obj == best_obj and obj > -math.inf and pnl > best_pnl
            ):
                best_obj = obj
                best_pnl = pnl
                best_params = dict(params)
                best_pf = pf
                best_n = n
    return best_params, best_pf, best_n


def _pf(trades: list[Trade], cap: float = 100.0) -> float:
    if not trades:
        return 0.0
    wins = sum(t.pnl for t in trades if t.pnl > 0)
    losses = -sum(t.pnl for t in trades if t.pnl < 0)
    if losses <= 0:
        return cap if wins > 0 else 0.0
    return wins / losses
