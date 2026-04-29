"""Resample bars to coarser timeframes."""

from __future__ import annotations

import pandas as pd

from .base import SCHEMA_COLUMNS, validate_schema

_PANDAS_FREQ = {
    "1m": "1min",
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1h",
    "4h": "4h",
    "1d": "1D",
}


def to_pandas_freq(timeframe: str) -> str:
    if timeframe not in _PANDAS_FREQ:
        raise ValueError(f"unsupported timeframe: {timeframe!r}")
    return _PANDAS_FREQ[timeframe]


def resample(df: pd.DataFrame, target_timeframe: str) -> pd.DataFrame:
    """Resample bars to `target_timeframe`. Bar timestamps are left edges
    (the bar opens at the timestamp). Drops empty bins."""
    validate_schema(df)
    if df.empty:
        return df.copy()

    freq = to_pandas_freq(target_timeframe)
    indexed = df.set_index("timestamp")
    agg = indexed.resample(freq, label="left", closed="left").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    )
    agg = agg.dropna(subset=["open"]).reset_index()
    return agg[SCHEMA_COLUMNS]
