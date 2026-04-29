"""Kraken public OHLC provider.

The /0/public/OHLC endpoint returns at most ~720 of the most recent bars
regardless of the `since` parameter. For multi-year crypto history use the
CSV bulk-ingest pipeline (kraken_csv.py).
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from pathlib import Path

import pandas as pd
import requests

from .base import NUMERIC_COLUMNS, SCHEMA_COLUMNS, DataProvider
from .cache import load as cache_load, save as cache_save

_KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"

_TIMEFRAME_TO_INTERVAL_MIN = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "1d": 1440,
}


class KrakenRESTProvider(DataProvider):
    name = "kraken"

    def __init__(self, cache_root: Path | None = None):
        self.cache_root = cache_root

    def fetch_bars(
        self, symbol: str, timeframe: str, start: date, end: date
    ) -> pd.DataFrame:
        if timeframe not in _TIMEFRAME_TO_INTERVAL_MIN:
            raise ValueError(
                f"unsupported timeframe {timeframe!r}; "
                f"must be one of {sorted(_TIMEFRAME_TO_INTERVAL_MIN)}"
            )

        params = {
            "pair": symbol.upper(),
            "interval": _TIMEFRAME_TO_INTERVAL_MIN[timeframe],
        }
        r = requests.get(_KRAKEN_OHLC_URL, params=params, timeout=30)
        r.raise_for_status()
        payload = r.json()
        if payload.get("error"):
            raise RuntimeError(f"Kraken API error: {payload['error']}")

        result = payload.get("result", {})
        pair_key = next((k for k in result if k != "last"), None)
        if not pair_key:
            return pd.DataFrame(columns=SCHEMA_COLUMNS)

        rows = result[pair_key]
        df = pd.DataFrame(
            rows, columns=["t", "open", "high", "low", "close", "vwap", "volume", "count"]
        )
        df["timestamp"] = pd.to_datetime(df["t"], unit="s", utc=True)
        for col in NUMERIC_COLUMNS:
            df[col] = df[col].astype(float)
        df = df[SCHEMA_COLUMNS].sort_values("timestamp").reset_index(drop=True)

        df = _trim(df, start, end)

        if self.cache_root is not None and not df.empty:
            cache_save(self.cache_root, self.name, symbol, timeframe, df)

        return df


def _trim(df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    if df.empty:
        return df
    start_ts = pd.Timestamp(datetime.combine(start, time(0, 0), tzinfo=timezone.utc))
    end_ts = pd.Timestamp(datetime.combine(end, time(23, 59, 59), tzinfo=timezone.utc))
    mask = (df["timestamp"] >= start_ts) & (df["timestamp"] <= end_ts)
    return df[mask].reset_index(drop=True)
