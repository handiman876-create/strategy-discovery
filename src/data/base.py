"""DataProvider ABC and the canonical bar schema.

All providers must return a DataFrame with the columns:
    timestamp  datetime64[ns, tz]   tz-aware (ET for stocks, UTC for crypto)
    open       float64
    high       float64
    low        float64
    close      float64
    volume     float64

Sorted ascending by timestamp, no duplicate timestamps.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

import pandas as pd

SCHEMA_COLUMNS: list[str] = ["timestamp", "open", "high", "low", "close", "volume"]
NUMERIC_COLUMNS: list[str] = ["open", "high", "low", "close", "volume"]


class DataProvider(ABC):
    """Abstract base class for all market-data providers."""

    name: str = "abstract"

    @abstractmethod
    def fetch_bars(
        self,
        symbol: str,
        timeframe: str,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """Return bars for symbol/timeframe in [start, end] (inclusive).

        timeframe is one of: 1m, 5m, 15m, 1h, 1d.
        Returned frame must conform to SCHEMA_COLUMNS in that order.
        """
        ...


def validate_schema(df: pd.DataFrame, *, allow_empty: bool = True) -> None:
    """Raise ValueError if df does not conform to the canonical schema."""
    if df.empty:
        if allow_empty:
            return
        raise ValueError("frame is empty")
    missing = [c for c in SCHEMA_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"missing columns: {missing}")
    if df["timestamp"].dt.tz is None:
        raise ValueError("timestamp column must be tz-aware")
    if not df["timestamp"].is_monotonic_increasing:
        raise ValueError("timestamp column must be sorted ascending")
    if df["timestamp"].duplicated().any():
        raise ValueError("timestamp column has duplicates")
