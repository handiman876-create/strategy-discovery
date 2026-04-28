"""Resample tests."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from data.resample import resample, to_pandas_freq

ET = ZoneInfo("America/New_York")


def _bars_5min(n: int, start_h: int = 9, start_m: int = 0):
    base = datetime(2024, 5, 15, start_h, start_m, tzinfo=ET)
    rows = []
    for i in range(n):
        ts = base + pd.Timedelta(minutes=5 * i)
        price = 100 + i * 0.1
        rows.append(
            {
                "timestamp": ts,
                "open": price,
                "high": price + 0.5,
                "low": price - 0.5,
                "close": price + 0.2,
                "volume": 1000.0,
            }
        )
    return pd.DataFrame(rows)


def test_to_pandas_freq_known():
    assert to_pandas_freq("5m") == "5min"
    assert to_pandas_freq("1h") == "1h"
    assert to_pandas_freq("1d") == "1D"


def test_to_pandas_freq_unknown():
    with pytest.raises(ValueError):
        to_pandas_freq("3m")


def test_resample_5min_to_15min():
    df = _bars_5min(6, start_h=9, start_m=0)  # 6 bars of 5min == 2 bars of 15min
    out = resample(df, "15m")
    assert len(out) == 2
    # First 15min bar covers minutes 0,5,10
    first = out.iloc[0]
    assert first["open"] == pytest.approx(100.0)
    assert first["close"] == pytest.approx(100 + 0.2 + 2 * 0.1)
    assert first["high"] == pytest.approx(100 + 2 * 0.1 + 0.5)


def test_resample_5min_to_1h():
    df = _bars_5min(12, start_h=9, start_m=0)  # 12 bars from 9:00..9:55 = 1 hour bin
    out = resample(df, "1h")
    assert len(out) == 1
    assert out.iloc[0]["volume"] == 12_000


def test_resample_empty():
    df = pd.DataFrame(
        {"timestamp": pd.DatetimeIndex([], tz=ET), "open": [], "high": [], "low": [], "close": [], "volume": []}
    )
    out = resample(df, "15m")
    assert out.empty
