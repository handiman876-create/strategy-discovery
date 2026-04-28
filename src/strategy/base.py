"""Strategy ABC and metadata requirements."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from engine.execution import Order
    from engine.portfolio import Position, Trade

from .context import Bar, Context
from .parameters import ParameterSet


class Strategy(ABC):
    """Base class for all strategies.

    Required class attributes (subclasses must override):
      * archetype          — string from DESIGN.md §4 (e.g. 'microstructure').
      * thesis             — one-sentence justification.
      * supported_assets   — list of {'stocks', 'crypto'}.
      * timeframes         — list of supported bar timeframes (e.g. ['5m']).

    Required methods:
      * on_bar(bar, position, context) -> list[Order]
      * get_parameters() -> dict[str, Any]
    """

    archetype: str = ""
    thesis: str = ""
    supported_assets: list[str] = []
    timeframes: list[str] = []
    parameter_set: ParameterSet = ParameterSet()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # Concrete subclasses must declare all four metadata fields.
        if not getattr(cls, "__abstractmethods__", None):
            for attr in ("archetype", "thesis", "supported_assets", "timeframes"):
                if not getattr(cls, attr, None):
                    raise TypeError(
                        f"{cls.__name__} must define class attribute {attr!r}"
                    )

    @abstractmethod
    def on_bar(
        self,
        bar: Bar,
        position: Optional[Position],
        context: Context,
    ) -> list[Order]:
        ...

    @abstractmethod
    def get_parameters(self) -> dict[str, Any]:
        ...

    def on_session_start(self, session_ts: datetime, context: Context) -> None:
        """Optional hook: called at the first bar of each session."""

    def on_session_end(self, session_ts: datetime, context: Context) -> None:
        """Optional hook: called at the last bar of a session before boundary."""

    def on_trade_closed(self, trade: Trade) -> None:
        """Optional hook: called immediately after a trade closes."""
