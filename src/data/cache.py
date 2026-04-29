"""Generic parquet cache for bar data.

Layout: {root}/{provider}/{SYMBOL}/{timeframe}.parquet

A single parquet file per (provider, symbol, timeframe). Callers are responsible
for deciding whether the cached range covers their request — this module simply
loads/saves and answers "do you have anything for this key?".
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from .base import SCHEMA_COLUMNS, validate_schema


def cache_path(root: Path, provider: str, symbol: str, timeframe: str) -> Path:
    return root / provider / symbol.upper() / f"{timeframe}.parquet"


def load(root: Path, provider: str, symbol: str, timeframe: str) -> pd.DataFrame | None:
    path = cache_path(root, provider, symbol, timeframe)
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    validate_schema(df)
    return df[SCHEMA_COLUMNS]


def save(
    root: Path, provider: str, symbol: str, timeframe: str, df: pd.DataFrame
) -> Path:
    validate_schema(df)
    path = cache_path(root, provider, symbol, timeframe)
    path.parent.mkdir(parents=True, exist_ok=True)
    df[SCHEMA_COLUMNS].to_parquet(path, index=False)
    return path


def merge_into_cache(
    root: Path, provider: str, symbol: str, timeframe: str, new_df: pd.DataFrame
) -> pd.DataFrame:
    """Union the new bars with anything already on disk; persist; return merged."""
    validate_schema(new_df)
    existing = load(root, provider, symbol, timeframe)
    if existing is None or existing.empty:
        merged = new_df
    else:
        merged = (
            pd.concat([existing, new_df], ignore_index=True)
            .drop_duplicates("timestamp")
            .sort_values("timestamp")
            .reset_index(drop=True)
        )
    save(root, provider, symbol, timeframe, merged)
    return merged
