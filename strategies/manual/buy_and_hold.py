"""BuyAndHold smoke strategy.

Buys 1 share at the first bar of the run and holds. The engine will close the
position at the last bar (lingering-position cleanup).

Used to validate engine math on synthetic bars: round-trip P&L should equal
(last_bar.close - first_bar.open) - 2*slippage - 2*commission.
"""

from __future__ import annotations

from typing import Any, Optional

from engine.execution import Order, OrderType
from engine.portfolio import Position
from strategy.base import Strategy
from strategy.context import Bar, Context


class BuyAndHold(Strategy):
    archetype = "smoke_test"
    thesis = "Hold 1 share for the entire backtest; used to validate engine math."
    supported_assets = ["stocks", "crypto"]
    timeframes = ["1m", "5m", "15m", "1h", "1d"]

    def __init__(self) -> None:
        self._has_entered = False

    def on_bar(
        self,
        bar: Bar,
        position: Optional[Position],
        context: Context,
    ) -> list[Order]:
        if self._has_entered or position is not None:
            return []
        self._has_entered = True
        return [Order(type=OrderType.MARKET, side="buy", size=1, signal_label="bnh_entry")]

    def get_parameters(self) -> dict[str, Any]:
        return {}
