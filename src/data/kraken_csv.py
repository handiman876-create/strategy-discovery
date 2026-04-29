"""Kraken bulk CSV trade-history pipeline.

Kraken publishes per-pair quarterly trade-history CSV files (each row is one
trade: timestamp_unix, price, volume) in a public Google Drive folder. Files
are multi-GB; we stream-read them in chunks and aggregate into OHLCV bars at
the requested timeframe.

Phase 1 implementation:
  * `aggregate_csv_to_bars` — full streaming aggregation, idempotent (uses cache).
  * `download_quarter` — raises NotImplementedError (manual one-time download).
"""

from __future__ import annotations

from datetime import timezone
from pathlib import Path
from typing import Iterable

import pandas as pd

from .base import NUMERIC_COLUMNS, SCHEMA_COLUMNS
from .cache import cache_path, load as cache_load, save as cache_save
from .resample import to_pandas_freq

_CSV_BASE_URL = "https://drive.google.com/drive/folders/1jLG14CGwhzCJuKVDcUjFK8TmS9NRLP82"

_TRADE_COLUMNS = ["timestamp", "price", "volume"]


def download_quarter(pair: str, quarter: str, dest_dir: Path) -> Path:
    """Resolve and download the per-pair, per-quarter trade CSV.

    Not yet implemented — the Google Drive folder requires either manual download
    or OAuth-mediated access. For Phase 1, callers should download files manually
    from {url} and pass the local path to `aggregate_csv_to_bars`.
    """
    raise NotImplementedError(
        f"Kraken CSV download is not implemented. Manually download "
        f"{pair}_{quarter}.csv from {_CSV_BASE_URL} and place it in {dest_dir}."
    )


def aggregate_csv_to_bars(
    csv_path: Path,
    pair: str,
    timeframe: str,
    *,
    cache_root: Path | None = None,
    chunk_rows: int = 1_000_000,
) -> pd.DataFrame:
    """Aggregate a Kraken trade-history CSV into OHLCV bars.

    The CSV format is: `timestamp_unix,price,volume` with no header.
    Streams the file in chunks of `chunk_rows` rows so a multi-GB CSV does not
    require multi-GB of RAM. Idempotent: if the resulting parquet is already
    cached, returns it without re-reading the CSV.
    """
    if cache_root is not None:
        cached = cache_load(cache_root, "kraken_csv", pair, timeframe)
        if cached is not None and not cached.empty:
            return cached

    if not csv_path.exists():
        raise FileNotFoundError(f"Kraken CSV not found: {csv_path}")

    freq = to_pandas_freq(timeframe)
    chunks: Iterable[pd.DataFrame] = pd.read_csv(
        csv_path,
        names=_TRADE_COLUMNS,
        header=None,
        chunksize=chunk_rows,
        dtype={"timestamp": "int64", "price": "float64", "volume": "float64"},
    )

    partials: list[pd.DataFrame] = []
    for chunk in chunks:
        chunk["timestamp"] = pd.to_datetime(chunk["timestamp"], unit="s", utc=True)
        chunk = chunk.set_index("timestamp")
        agg = chunk["price"].resample(freq, label="left", closed="left").ohlc()
        agg["volume"] = chunk["volume"].resample(freq, label="left", closed="left").sum()
        agg = agg.dropna(subset=["open"])
        partials.append(agg.reset_index())

    if not partials:
        return pd.DataFrame(columns=SCHEMA_COLUMNS)

    combined = pd.concat(partials, ignore_index=True)
    combined = (
        combined.groupby("timestamp", as_index=False)
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    for col in NUMERIC_COLUMNS:
        combined[col] = combined[col].astype(float)
    combined = combined[SCHEMA_COLUMNS]

    if cache_root is not None:
        cache_save(cache_root, "kraken_csv", pair, timeframe, combined)

    return combined
