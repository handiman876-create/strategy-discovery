"""Per-symbol backtester. Single-symbol, single-position.

Bar processing order (mirrors the legacy backtester for regression parity):
  1. Detect session boundary; if new session and position open, force-close at
     PREVIOUS bar's close as 'eod'. Reset session state.
  2. Fill any pending market entry at THIS bar's open ± slippage.
  3. Check intrabar stop, then target, on the open position.
  4. Check EOD forced exit (bar.time >= session.eod_exit_time).
  5. Append bar to context; call strategy.on_bar(); enqueue any new orders.
  6. Mark-to-market and append to equity curve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

import pandas as pd

from strategy.base import Strategy
from strategy.context import Bar, Context

from .execution import (
    FillConfig,
    FillReason,
    Order,
    OrderType,
    apply_entry_slippage,
    apply_exit_slippage,
)
from .portfolio import Portfolio, Position
from .session import RegularTradingHours, Session


_INTRADAY_TIMEFRAMES = frozenset({"1m", "5m", "15m", "30m", "1h", "4h"})


@dataclass
class BacktestConfig:
    starting_capital: float = 10_000.0
    commission: float = 0.0
    slippage: float = 0.01
    realistic_fills: bool = True
    session: Session = field(default_factory=RegularTradingHours)
    context_lookback: int = 200
    # Bar timeframe drives session-handling. For intraday data (5m/15m/1h/...)
    # session_bars resets at every session boundary and EOD force-close fires.
    # For daily-or-coarser data, session_bars is never reset (treat the whole
    # series as one continuous session so daily-period indicators can warm up)
    # and EOD force-close is suppressed (no intra-bar EOD on daily bars).
    # Default "5m" preserves prior behavior for tests that don't set it.
    #
    # This is the simple version: a single boolean dispatch on intraday vs
    # daily-or-coarser. A future architectural improvement (Phase 4+) would
    # be timeframe-aware session calendars with custom reset rules per
    # timeframe — not needed for Phase 3.
    bar_timeframe: str = "5m"

    @property
    def is_intraday(self) -> bool:
        return self.bar_timeframe in _INTRADAY_TIMEFRAMES


@dataclass
class BacktestResult:
    symbol: str
    portfolio: Portfolio
    config: BacktestConfig

    @property
    def trades(self):
        return self.portfolio.trades

    @property
    def equity_curve(self) -> pd.Series:
        return self.portfolio.equity_curve()


def run_backtest(
    symbol: str,
    bars_df: pd.DataFrame,
    strategy: Strategy,
    config: Optional[BacktestConfig] = None,
) -> BacktestResult:
    if config is None:
        config = BacktestConfig()

    fill_cfg = FillConfig(
        commission=config.commission,
        slippage=config.slippage,
        realistic_fills=config.realistic_fills,
    )

    bars = _materialize_bars(bars_df)
    portfolio = Portfolio(starting_capital=config.starting_capital)
    pending_market: Optional[Order] = None
    session_bars: list[Bar] = []
    prev_ts: Optional[datetime] = None
    last_bar: Optional[Bar] = None
    is_intraday = config.is_intraday

    for i, bar in enumerate(bars):
        # 1. Session boundary
        # Intraday: per-session reset, force-close at boundary, on_session_start hook.
        # Daily-or-coarser: skip — the whole series is one continuous session.
        # on_session_start fires once at the very first bar so strategies that
        # need init-time setup still get the hook.
        if is_intraday:
            if config.session.is_session_start(bar.timestamp, prev_ts):
                if portfolio.position is not None and last_bar is not None:
                    _close(portfolio, last_bar.close, last_bar.timestamp, FillReason.EOD, fill_cfg)
                    strategy.on_trade_closed(portfolio.trades[-1])
                    pending_market = None
                session_bars = []
                ctx = _make_context(session_bars, config.context_lookback, config.session)
                strategy.on_session_start(bar.timestamp, ctx)
        elif prev_ts is None:
            ctx = _make_context(session_bars, config.context_lookback, config.session)
            strategy.on_session_start(bar.timestamp, ctx)

        # 2. Fill pending market entry at this bar's open
        if pending_market is not None and portfolio.position is None:
            _fill_market_entry(symbol, portfolio, pending_market, bar, fill_cfg)
            pending_market = None

        # 3. Intrabar stop / target
        if portfolio.position is not None:
            _check_stop_target(portfolio, bar, fill_cfg, strategy)

        # 4. EOD forced exit (intraday only — daily bars have no intra-bar EOD)
        if is_intraday and portfolio.position is not None and config.session.is_session_end_time(
            bar.timestamp
        ):
            _close(portfolio, bar.close, bar.timestamp, FillReason.EOD, fill_cfg)
            strategy.on_trade_closed(portfolio.trades[-1])
            pending_market = None

        # 5. Strategy on_bar
        session_bars.append(bar)
        ctx = _make_context(session_bars, config.context_lookback, config.session)
        orders = strategy.on_bar(bar, portfolio.position, ctx)
        for order in orders or []:
            pending_market = _process_order(
                portfolio, order, bar, fill_cfg, pending_market, strategy
            )

        # 6. Mark-to-market
        portfolio.mark_to_market(bar.timestamp, bar.close, fill_cfg.commission)
        prev_ts = bar.timestamp
        last_bar = bar

    # Close any lingering position at end
    if portfolio.position is not None and last_bar is not None:
        _close(portfolio, last_bar.close, last_bar.timestamp, FillReason.EOD, fill_cfg)
        strategy.on_trade_closed(portfolio.trades[-1])

    return BacktestResult(symbol=symbol, portfolio=portfolio, config=config)


# ── helpers ──────────────────────────────────────────────────────────────────


def _materialize_bars(df: pd.DataFrame) -> list[Bar]:
    return [
        Bar(
            timestamp=row.timestamp,
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
        )
        for row in df.itertuples()
    ]


def _make_context(bars: list[Bar], lookback: int, session: Session) -> Context:
    return Context(bars=bars, lookback=lookback, session=session)


def _fill_market_entry(
    symbol: str, portfolio: Portfolio, order: Order, bar: Bar, cfg: FillConfig
) -> None:
    fill_price = apply_entry_slippage(bar.open, order.side, cfg)
    side = "long" if order.side == "buy" else "short"
    stop = order.stop_price if order.stop_price is not None else (
        0.0 if side == "long" else float("inf")
    )
    target = order.target_price if order.target_price is not None else (
        float("inf") if side == "long" else 0.0
    )
    portfolio.open_position(
        Position(
            symbol=symbol,
            side=side,
            size=order.size,
            entry_price=fill_price,
            entry_time=bar.timestamp,
            stop_price=stop,
            target_price=target,
            signal_label=order.signal_label,
        )
    )


def _check_stop_target(
    portfolio: Portfolio,
    bar: Bar,
    cfg: FillConfig,
    strategy: Strategy,
) -> None:
    pos = portfolio.position
    assert pos is not None
    if pos.is_long:
        if bar.low <= pos.stop_price:
            _close(portfolio, pos.stop_price, bar.timestamp, FillReason.STOP, cfg)
            strategy.on_trade_closed(portfolio.trades[-1])
        elif bar.high >= pos.target_price:
            _close(portfolio, pos.target_price, bar.timestamp, FillReason.TARGET, cfg)
            strategy.on_trade_closed(portfolio.trades[-1])
    else:
        if bar.high >= pos.stop_price:
            _close(portfolio, pos.stop_price, bar.timestamp, FillReason.STOP, cfg)
            strategy.on_trade_closed(portfolio.trades[-1])
        elif bar.low <= pos.target_price:
            _close(portfolio, pos.target_price, bar.timestamp, FillReason.TARGET, cfg)
            strategy.on_trade_closed(portfolio.trades[-1])


def _close(
    portfolio: Portfolio,
    raw_price: float,
    ts: datetime,
    reason: FillReason,
    cfg: FillConfig,
) -> None:
    pos = portfolio.position
    assert pos is not None
    fill_price = apply_exit_slippage(raw_price, pos.is_long, reason, cfg)
    portfolio.close_position(
        exit_price=fill_price,
        exit_time=ts,
        reason=_reason_label(reason),
        commission=cfg.commission,
        slippage=cfg.slippage,
    )


def _reason_label(reason: FillReason) -> str:
    return {
        FillReason.STOP: "stop",
        FillReason.TARGET: "target",
        FillReason.EOD: "eod",
        FillReason.SIGNAL_EXIT: "signal",
        FillReason.ENTRY: "entry",
    }[reason]


def _process_order(
    portfolio: Portfolio,
    order: Order,
    bar: Bar,
    cfg: FillConfig,
    pending_market: Optional[Order],
    strategy: Strategy,
) -> Optional[Order]:
    if order.type != OrderType.MARKET:
        # LIMIT/STOP not used by Casper; reserved for future strategies.
        # They would be filed on the next bar's range. We stash them as pending
        # in the same slot; for Phase 1 we only support market entries.
        raise NotImplementedError(
            f"order type {order.type.name} not supported in Phase 1 engine"
        )

    if order.side in ("buy", "sell_short"):
        if portfolio.position is None:
            return order  # queued; fills next bar open
        # already in a position — ignore (one-position-per-symbol rule)
        return pending_market

    if order.side in ("sell", "buy_to_cover"):
        if portfolio.position is not None:
            _close(portfolio, bar.close, bar.timestamp, FillReason.SIGNAL_EXIT, cfg)
            strategy.on_trade_closed(portfolio.trades[-1])
        return pending_market

    raise ValueError(f"unknown order side: {order.side}")
