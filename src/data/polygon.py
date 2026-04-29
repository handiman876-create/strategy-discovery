"""Polygon Stocks Starter data provider.

Ports the working logic from scripts/verify_data.py. Returns ET-aware bars
filtered to RTH (09:30 ≤ t ≤ 15:55 inclusive at the open).
"""

from __future__ import annotations

import os
import time
from datetime import date
from pathlib import Path

import pandas as pd
import requests

from .base import NUMERIC_COLUMNS, SCHEMA_COLUMNS, DataProvider
from .cache import load as cache_load, save as cache_save, merge_into_cache

_POLYGON_BASE = "https://api.polygon.io"
_ET = "America/New_York"

_TIMEFRAME_TO_AGG = {
    "1m": (1, "minute"),
    "5m": (5, "minute"),
    "15m": (15, "minute"),
    "30m": (30, "minute"),
    "1h": (1, "hour"),
    "1d": (1, "day"),
}

# Timeframes for which fetched bars get post-filtered to RTH-only.
# Distinct from engine.session.INTRADAY_TIMEFRAMES (which drives session-reset
# in the backtester). Same membership today minus "4h"; renamed to make the
# semantic difference explicit and prevent the next "we forgot to keep them
# in sync" bug. The two sets answer different questions.
_RTH_FILTERED_TIMEFRAMES = {"1m", "5m", "15m", "30m", "1h"}


class PolygonProvider(DataProvider):
    name = "polygon"

    def __init__(
        self,
        api_key: str | None = None,
        cache_root: Path | None = None,
        request_pause_s: float = 0.1,
    ):
        self.api_key = api_key or os.environ.get("POLYGON_API_KEY")
        if not self.api_key or self.api_key == "your_polygon_api_key_here":
            raise RuntimeError(
                "POLYGON_API_KEY not set. Add it to .env or pass api_key=... explicitly."
            )
        self.cache_root = cache_root
        self.request_pause_s = request_pause_s

    def fetch_bars(
        self, symbol: str, timeframe: str, start: date, end: date
    ) -> pd.DataFrame:
        if timeframe not in _TIMEFRAME_TO_AGG:
            raise ValueError(
                f"unsupported timeframe {timeframe!r}; "
                f"must be one of {sorted(_TIMEFRAME_TO_AGG)}"
            )

        if self.cache_root is not None:
            cached = cache_load(self.cache_root, self.name, symbol, timeframe)
            if cached is not None and _covers(cached, start, end):
                return _trim(cached, start, end)

        # Gap-fill: only fetch the missing date ranges, then merge with existing cache.
        existing = (
            cache_load(self.cache_root, self.name, symbol, timeframe)
            if self.cache_root is not None
            else None
        )
        gaps = _missing_ranges(existing, start, end)
        new_frames: list[pd.DataFrame] = []
        for gap_start, gap_end in gaps:
            df_gap = self._fetch_aggs(symbol, timeframe, gap_start, gap_end)
            if timeframe in _RTH_FILTERED_TIMEFRAMES:
                df_gap = _filter_rth(df_gap)
            new_frames.append(df_gap)

        if new_frames:
            new_df = (
                pd.concat(new_frames, ignore_index=True)
                .drop_duplicates("timestamp")
                .sort_values("timestamp")
                .reset_index(drop=True)
            )
        else:
            new_df = pd.DataFrame(columns=SCHEMA_COLUMNS)

        if self.cache_root is not None and not new_df.empty:
            merged = merge_into_cache(self.cache_root, self.name, symbol, timeframe, new_df)
        else:
            merged = (
                pd.concat([existing, new_df], ignore_index=True)
                if existing is not None
                else new_df
            ).drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)

        return _trim(merged, start, end)

    def _fetch_aggs(
        self, symbol: str, timeframe: str, start: date, end: date
    ) -> pd.DataFrame:
        multiplier, timespan = _TIMEFRAME_TO_AGG[timeframe]
        url = (
            f"{_POLYGON_BASE}/v2/aggs/ticker/{symbol.upper()}"
            f"/range/{multiplier}/{timespan}/{start.isoformat()}/{end.isoformat()}"
        )
        params: dict[str, str | int] | None = {
            "adjusted": "true",
            "sort": "asc",
            "limit": 50000,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        rows: list[dict] = []
        while url:
            payload = self._get_with_retry(url, params, headers)
            status = payload.get("status")
            if status not in ("OK", "DELAYED"):
                raise RuntimeError(f"Polygon status={status!r}: {payload}")
            rows.extend(payload.get("results") or [])
            url = payload.get("next_url")
            params = None
            if url:
                time.sleep(self.request_pause_s)

        if not rows:
            return pd.DataFrame(columns=SCHEMA_COLUMNS)
        df = self._rows_to_frame(rows)
        return df

    def _get_with_retry(self, url, params, headers, attempts: int = 4) -> dict:
        last: Exception | None = None
        for i in range(attempts):
            try:
                r = requests.get(url, params=params, headers=headers, timeout=60)
                r.raise_for_status()
                return r.json()
            except (requests.RequestException, ValueError) as exc:
                last = exc
                wait = 1.5 * (2 ** i)
                print(f"  polygon transient error ({type(exc).__name__}): {exc} — retrying in {wait:.1f}s")
                time.sleep(wait)
        raise RuntimeError(f"Polygon failed after {attempts} attempts: {last}") from last

    def _rows_to_frame(self, rows: list[dict]) -> pd.DataFrame:
        df = pd.DataFrame(rows)
        df["timestamp"] = (
            pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert(_ET)
        )
        df = df.rename(
            columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
        )
        for col in NUMERIC_COLUMNS:
            df[col] = df[col].astype(float)
        return df[SCHEMA_COLUMNS]



def _filter_rth(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    t = df["timestamp"].dt.time
    mask = (t >= pd.Timestamp("09:30").time()) & (t <= pd.Timestamp("15:55").time())
    return df[mask].copy()


def _covers(df: pd.DataFrame, start: date, end: date) -> bool:
    if df.empty:
        return False
    first = df["timestamp"].iloc[0].date()
    last = df["timestamp"].iloc[-1].date()
    return first <= start and last >= end


def _missing_ranges(
    existing: pd.DataFrame | None, start: date, end: date
) -> list[tuple[date, date]]:
    """Return the date sub-ranges of [start, end] not covered by `existing`.
    Coarse-grained: if existing covers a contiguous span, we only fill prefix
    and suffix gaps."""
    if existing is None or existing.empty:
        return [(start, end)]
    first = existing["timestamp"].iloc[0].date()
    last = existing["timestamp"].iloc[-1].date()
    from datetime import timedelta as _td

    gaps: list[tuple[date, date]] = []
    if start < first:
        gaps.append((start, min(end, first - _td(days=1))))
    if end > last:
        gaps.append((max(start, last + _td(days=1)), end))
    return gaps


def _trim(df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    if df.empty:
        return df
    tz = df["timestamp"].dt.tz
    start_ts = pd.Timestamp(start).tz_localize(tz)
    end_ts = pd.Timestamp(end).tz_localize(tz) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
    mask = (df["timestamp"] >= start_ts) & (df["timestamp"] <= end_ts)
    return df[mask].reset_index(drop=True)
