"""Session calendar tests."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest

from engine.session import (
    CryptoSession,
    RegularTradingHours,
    US_MARKET_HOLIDAYS_2018_2026,
)

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def _et(year, month, day, h=10, m=0):
    return datetime(year, month, day, h, m, tzinfo=ET)


class TestRegularTradingHours:
    def test_weekday_open(self):
        s = RegularTradingHours()
        assert s.is_open(_et(2024, 5, 15, 10, 0))

    def test_weekday_pre_open(self):
        s = RegularTradingHours()
        assert not s.is_open(_et(2024, 5, 15, 9, 25))

    def test_weekday_after_close(self):
        s = RegularTradingHours()
        assert not s.is_open(_et(2024, 5, 15, 16, 0))

    def test_saturday_closed(self):
        s = RegularTradingHours()
        assert not s.is_open(_et(2024, 5, 18, 10, 0))

    def test_july_4_closed(self):
        s = RegularTradingHours()
        assert not s.is_open(_et(2024, 7, 4, 10, 0))

    def test_session_start_first_bar(self):
        s = RegularTradingHours()
        assert s.is_session_start(_et(2024, 5, 15, 9, 30), prev_ts=None)

    def test_session_start_new_day(self):
        s = RegularTradingHours()
        prev = _et(2024, 5, 14, 15, 55)
        cur = _et(2024, 5, 15, 9, 30)
        assert s.is_session_start(cur, prev)

    def test_session_start_same_day(self):
        s = RegularTradingHours()
        prev = _et(2024, 5, 15, 9, 30)
        cur = _et(2024, 5, 15, 9, 35)
        assert not s.is_session_start(cur, prev)

    def test_eod_threshold(self):
        s = RegularTradingHours()
        assert s.is_session_end_time(_et(2024, 5, 15, 15, 50))
        assert s.is_session_end_time(_et(2024, 5, 15, 15, 55))
        assert not s.is_session_end_time(_et(2024, 5, 15, 15, 45))

    def test_holiday_set_nonempty(self):
        assert len(US_MARKET_HOLIDAYS_2018_2026) > 50


class TestCryptoSession:
    def test_always_open(self):
        s = CryptoSession()
        ts = datetime(2024, 1, 1, 3, 30, tzinfo=UTC)
        assert s.is_open(ts)
        assert s.is_open(datetime(2024, 6, 30, 23, 59, tzinfo=UTC))

    def test_only_first_bar_is_session_start(self):
        s = CryptoSession()
        ts1 = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
        ts2 = datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
        assert s.is_session_start(ts1, prev_ts=None)
        assert not s.is_session_start(ts2, prev_ts=ts1)

    def test_never_session_end(self):
        s = CryptoSession()
        assert not s.is_session_end_time(datetime(2024, 1, 1, 23, 59, tzinfo=UTC))
