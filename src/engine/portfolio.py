"""Position, Trade, and per-symbol Portfolio bookkeeping."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import pandas as pd


@dataclass
class Position:
    symbol: str
    side: str  # 'long' | 'short'
    size: int
    entry_price: float
    entry_time: datetime
    stop_price: float
    target_price: float
    signal_label: str = ""

    @property
    def is_long(self) -> bool:
        return self.side == "long"

    def unrealized_pnl(self, current_price: float, commission: float = 0.0) -> float:
        direction = 1 if self.is_long else -1
        gross = (current_price - self.entry_price) * self.size * direction
        return gross - commission


@dataclass
class Trade:
    symbol: str
    side: str
    size: int
    entry_price: float
    entry_time: datetime
    exit_price: float
    exit_time: datetime
    exit_reason: str  # 'stop' | 'target' | 'eod' | 'signal'
    commission: float
    slippage: float
    signal_label: str = ""

    @property
    def pnl(self) -> float:
        direction = 1 if self.side == "long" else -1
        gross = (self.exit_price - self.entry_price) * self.size * direction
        return gross - 2 * self.commission

    @property
    def pnl_pct(self) -> float:
        direction = 1 if self.side == "long" else -1
        return (self.exit_price - self.entry_price) / self.entry_price * direction

    @property
    def duration_mins(self) -> int:
        return int((self.exit_time - self.entry_time).total_seconds() / 60)

    @property
    def is_winner(self) -> bool:
        return self.pnl > 0


@dataclass
class Portfolio:
    """Per-symbol portfolio: at most one open position; cash + equity tracking."""

    starting_capital: float
    cash: float = field(init=False)
    position: Optional[Position] = field(default=None, init=False)
    trades: list[Trade] = field(default_factory=list, init=False)
    equity_points: list[tuple[datetime, float]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self.cash = self.starting_capital

    def open_position(self, position: Position) -> None:
        if self.position is not None:
            raise RuntimeError("Cannot open: position already open")
        self.position = position

    def close_position(
        self,
        exit_price: float,
        exit_time: datetime,
        reason: str,
        commission: float,
        slippage: float,
    ) -> Trade:
        if self.position is None:
            raise RuntimeError("Cannot close: no open position")
        pos = self.position
        trade = Trade(
            symbol=pos.symbol,
            side=pos.side,
            size=pos.size,
            entry_price=pos.entry_price,
            entry_time=pos.entry_time,
            exit_price=exit_price,
            exit_time=exit_time,
            exit_reason=reason,
            commission=commission,
            slippage=slippage,
            signal_label=pos.signal_label,
        )
        self.cash += trade.pnl
        self.trades.append(trade)
        self.position = None
        return trade

    def mark_to_market(self, ts: datetime, price: float, commission: float = 0.0) -> None:
        unrealized = (
            self.position.unrealized_pnl(price, commission) if self.position is not None else 0.0
        )
        self.equity_points.append((ts, self.cash + unrealized))

    def equity_curve(self) -> pd.Series:
        if not self.equity_points:
            return pd.Series([], dtype=float, name="equity")
        return pd.Series(
            [e for _, e in self.equity_points],
            index=pd.DatetimeIndex([t for t, _ in self.equity_points], name="timestamp"),
            name="equity",
        )
