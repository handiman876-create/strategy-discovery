"""Session calendars + bar-timeframe-aware session-reset dispatch.

A Session answers two questions for the engine:
  * "Should this bar be processed at all?" — `is_open(timestamp)`
  * "Is this bar the start/end of a trading session?" — `is_session_start/end(bars, idx)`

Two implementations:
  * `RegularTradingHours` — US stocks, 09:30–16:00 ET, weekdays excluding holidays.
  * `CryptoSession` — 24/7, no boundaries.

Centralized dispatch helpers live at the bottom of this file:
  * `INTRADAY_TIMEFRAMES` — single source of truth for the membership set.
  * `is_intraday_timeframe(bar_timeframe)` — True for {"1m"…"4h"}.
  * `should_reset_session_at_bar(bar_timeframe, session, current_ts, prev_ts)`
    — the decision the backtester and signal-frequency diagnostic share.
    Code outside this module must not call `Session.is_session_start` for
    reset decisions; call this function instead. The contract is enforced
    by tests/unit/test_session_reset_contract.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


# Hardcoded US market full-close holidays 2018–2026.
# Early-close days (e.g. day-after-Thanksgiving 1pm close) are NOT in this list:
# the data feed simply ends earlier on those days, and the engine handles that
# via the session-boundary forced close. See backtester.py.
US_MARKET_HOLIDAYS_2018_2026: frozenset[date] = frozenset(
    {
        # 2018
        date(2018, 1, 1), date(2018, 1, 15), date(2018, 2, 19),
        date(2018, 3, 30), date(2018, 5, 28), date(2018, 7, 4),
        date(2018, 9, 3), date(2018, 11, 22), date(2018, 12, 5),
        date(2018, 12, 25),
        # 2019
        date(2019, 1, 1), date(2019, 1, 21), date(2019, 2, 18),
        date(2019, 4, 19), date(2019, 5, 27), date(2019, 7, 4),
        date(2019, 9, 2), date(2019, 11, 28), date(2019, 12, 25),
        # 2020
        date(2020, 1, 1), date(2020, 1, 20), date(2020, 2, 17),
        date(2020, 4, 10), date(2020, 5, 25), date(2020, 7, 3),
        date(2020, 9, 7), date(2020, 11, 26), date(2020, 12, 25),
        # 2021
        date(2021, 1, 1), date(2021, 1, 18), date(2021, 2, 15),
        date(2021, 4, 2), date(2021, 5, 31), date(2021, 7, 5),
        date(2021, 9, 6), date(2021, 11, 25), date(2021, 12, 24),
        # 2022
        date(2022, 1, 17), date(2022, 2, 21),
        date(2022, 4, 15), date(2022, 5, 30), date(2022, 6, 20),
        date(2022, 7, 4), date(2022, 9, 5), date(2022, 11, 24),
        date(2022, 12, 26),
        # 2023
        date(2023, 1, 2), date(2023, 1, 16), date(2023, 2, 20),
        date(2023, 4, 7), date(2023, 5, 29), date(2023, 6, 19),
        date(2023, 7, 4), date(2023, 9, 4), date(2023, 11, 23),
        date(2023, 12, 25),
        # 2024
        date(2024, 1, 1), date(2024, 1, 15), date(2024, 2, 19),
        date(2024, 3, 29), date(2024, 5, 27), date(2024, 6, 19),
        date(2024, 7, 4), date(2024, 9, 2), date(2024, 11, 28),
        date(2024, 12, 25),
        # 2025
        date(2025, 1, 1), date(2025, 1, 9),  # day of mourning, Carter
        date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
        date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4),
        date(2025, 9, 1), date(2025, 11, 27), date(2025, 12, 25),
        # 2026
        date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
        date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),
        date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26),
        date(2026, 12, 25),
    }
)


class Session(ABC):
    """Abstract calendar."""

    @property
    @abstractmethod
    def timezone(self) -> ZoneInfo:
        ...

    @abstractmethod
    def is_open(self, ts: datetime) -> bool:
        ...

    @abstractmethod
    def is_session_start(self, current_ts: datetime, prev_ts: datetime | None) -> bool:
        ...

    @abstractmethod
    def is_session_end_time(self, ts: datetime) -> bool:
        """True if `ts` is at or past the configured EOD time for the session."""
        ...


class RegularTradingHours(Session):
    """US stocks RTH: 09:30–15:55 (last bar opens 15:55, closes 16:00),
    weekdays excluding US market holidays."""

    def __init__(
        self,
        open_time: time = time(9, 30),
        close_time: time = time(15, 55),
        eod_exit_time: time = time(15, 50),
        holidays: frozenset[date] = US_MARKET_HOLIDAYS_2018_2026,
    ):
        self.open_time = open_time
        self.close_time = close_time
        self.eod_exit_time = eod_exit_time
        self.holidays = holidays

    @property
    def timezone(self) -> ZoneInfo:
        return ET

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def is_open(self, ts: datetime) -> bool:
        local = ts.astimezone(ET) if ts.tzinfo is not None else ts.replace(tzinfo=ET)
        if not self.is_trading_day(local.date()):
            return False
        return self.open_time <= local.time() <= self.close_time

    def is_session_start(self, current_ts: datetime, prev_ts: datetime | None) -> bool:
        cur_local = current_ts.astimezone(ET)
        if prev_ts is None:
            return True
        prev_local = prev_ts.astimezone(ET)
        return cur_local.date() != prev_local.date()

    def is_session_end_time(self, ts: datetime) -> bool:
        local = ts.astimezone(ET) if ts.tzinfo is not None else ts.replace(tzinfo=ET)
        return local.time() >= self.eod_exit_time


class CryptoSession(Session):
    """24/7 crypto session — no boundaries, never end-of-session."""

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo("UTC")

    def is_open(self, ts: datetime) -> bool:
        return True

    def is_session_start(self, current_ts: datetime, prev_ts: datetime | None) -> bool:
        # No daily boundaries — only the very first bar is a "session start".
        return prev_ts is None

    def is_session_end_time(self, ts: datetime) -> bool:
        return False


# ── Centralized session-reset dispatch ───────────────────────────────────────
#
# The backtester and the signal-frequency diagnostic both maintain a
# session_bars list that resets at session boundaries — but only for
# intraday data. For daily-or-coarser bars the whole series is one
# continuous session so daily-period indicators can warm up. Keeping that
# decision in two places drifts (Fix #5 shipped without the gate; the
# diagnostic over-reported cold for daily strategies until this helper
# landed). Single source of truth is the lesson — see
# feedback_centralize_dispatched_logic in the project memory.


INTRADAY_TIMEFRAMES: frozenset[str] = frozenset({"1m", "5m", "15m", "30m", "1h", "4h"})


def is_intraday_timeframe(bar_timeframe: str) -> bool:
    """Single source of truth for the intraday-vs-daily dispatch. Used
    wherever code needs to know whether a timeframe operates within a
    single trading session (intraday) or spans/equals one (daily+)."""
    return bar_timeframe in INTRADAY_TIMEFRAMES


def should_reset_session_at_bar(
    bar_timeframe: str,
    session: Session,
    current_ts: datetime,
    prev_ts: datetime | None,
) -> bool:
    """Decide whether session-scoped state (e.g. session_bars) should be
    reset as we process this bar.

    Returns True at every session boundary for intraday timeframes
    (5m/15m/30m/1h/4h). Returns False for daily-or-coarser timeframes
    unconditionally — the whole bar series is treated as one continuous
    session so daily-period indicators can warm up across bars.

    Code outside src/engine/session.py should not call
    Session.is_session_start directly for reset decisions — call this
    function instead. The contract is enforced by
    tests/unit/test_session_reset_contract.py: any new direct caller
    must either route through this helper or be added to the test's
    explicit allowlist with a justification comment.
    """
    if not is_intraday_timeframe(bar_timeframe):
        return False
    return session.is_session_start(current_ts, prev_ts)
