"""Backtest engine — execution, portfolio, sessions, event loop."""

from .backtester import BacktestConfig, BacktestResult, run_backtest
from .execution import FillConfig, FillReason, Order, OrderType
from .portfolio import Portfolio, Position, Trade
from .session import (
    CryptoSession,
    RegularTradingHours,
    Session,
    US_MARKET_HOLIDAYS_2018_2026,
)

__all__ = [
    "BacktestConfig",
    "BacktestResult",
    "run_backtest",
    "FillConfig",
    "FillReason",
    "Order",
    "OrderType",
    "Portfolio",
    "Position",
    "Trade",
    "Session",
    "RegularTradingHours",
    "CryptoSession",
    "US_MARKET_HOLIDAYS_2018_2026",
]
