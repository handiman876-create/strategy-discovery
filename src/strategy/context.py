"""Bar dataclass and the Context object passed to strategies each bar.

The Context exposes a rolling window of recent bars and convenience helpers
for session-relative reasoning (e.g. "is this the first bar of the session?").
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from engine.session import Session


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def bar_time(self) -> time:
        return self.timestamp.time()

    @property
    def bar_date(self) -> date:
        return self.timestamp.date()


class Context:
    """Read-only view passed to `Strategy.on_bar`.

    Holds a rolling window of recent bars (default 200, configurable). The
    newest bar is always the last item.
    """

    def __init__(self, bars: list[Bar], lookback: int, session: "Session"):
        self._all_bars = bars
        self._lookback = lookback
        self._session = session

    @property
    def session(self) -> "Session":
        return self._session

    @property
    def now(self) -> Optional[datetime]:
        return self._all_bars[-1].timestamp if self._all_bars else None

    @property
    def now_time(self) -> Optional[time]:
        ts = self.now
        return ts.time() if ts is not None else None

    @property
    def now_date(self) -> Optional[date]:
        ts = self.now
        return ts.date() if ts is not None else None

    def recent(self, n: Optional[int] = None) -> list[Bar]:
        n = n if n is not None else self._lookback
        return self._all_bars[-n:] if len(self._all_bars) >= n else list(self._all_bars)

    def bars_in_session(self) -> list[Bar]:
        """All bars in the current session, newest last."""
        return list(self._all_bars)

    def bars_since_session_open(self) -> int:
        return len(self._all_bars)

    def is_session_start(self) -> bool:
        return len(self._all_bars) == 1

    def is_session_end(self) -> bool:
        ts = self.now
        if ts is None:
            return False
        return self._session.is_session_end_time(ts)

    def highest_high(self, n: int) -> float:
        bars = self.recent(n)
        return max(b.high for b in bars) if bars else float("nan")

    def lowest_low(self, n: int) -> float:
        bars = self.recent(n)
        return min(b.low for b in bars) if bars else float("nan")
