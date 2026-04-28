"""Order types and fill simulation.

Three order types:
  * MARKET — entry fills at NEXT bar's open ± slippage; explicit-close fills at
    current bar's close ± slippage.
  * LIMIT — fills at limit price if next bar's range crosses it (no fill
    optimization assumed; we assume worst-case touch fills).
  * STOP  — triggers when next bar's range crosses stop, fills at stop price
    ± slippage. Acknowledged optimistic on real-world gap fills.

Slippage policy is governed by `realistic_fills`:
  * False (regression mode): slippage on market entry/exit only; stops/targets
    fill at exact price. Mirrors the legacy backtester semantics.
  * True (default): slippage on all fills.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional


class OrderType(Enum):
    MARKET = auto()
    LIMIT = auto()
    STOP = auto()


class FillReason(Enum):
    ENTRY = auto()
    SIGNAL_EXIT = auto()
    STOP = auto()
    TARGET = auto()
    EOD = auto()


@dataclass
class Order:
    type: OrderType
    side: str  # 'buy' | 'sell' | 'sell_short' | 'buy_to_cover'
    size: int
    price: Optional[float] = None  # required for LIMIT/STOP orders
    stop_price: Optional[float] = None  # bracket: protective stop
    target_price: Optional[float] = None  # bracket: take-profit
    signal_label: str = ""


@dataclass
class FillConfig:
    commission: float = 0.0
    slippage: float = 0.01
    realistic_fills: bool = True


def apply_entry_slippage(price: float, side: str, cfg: FillConfig) -> float:
    """Slippage on market entry. Buy fills above mid, short-sell below mid."""
    if cfg.slippage == 0:
        return price
    if side == "buy":
        return price + cfg.slippage
    if side == "sell_short":
        return price - cfg.slippage
    raise ValueError(f"unknown entry side: {side}")


def apply_exit_slippage(
    price: float, position_is_long: bool, reason: FillReason, cfg: FillConfig
) -> float:
    """Slippage on exit. Long sells below mid; short covers above mid.

    When realistic_fills is False, stop/target fills get NO slippage (legacy
    semantics — needed for regression parity with the old backtester)."""
    if cfg.slippage == 0:
        return price
    if not cfg.realistic_fills and reason in (FillReason.STOP, FillReason.TARGET):
        return price
    if position_is_long:
        return price - cfg.slippage
    return price + cfg.slippage
