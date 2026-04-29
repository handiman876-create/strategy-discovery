"""Data layer — pluggable providers, parquet cache, resampling utilities."""

from .base import DataProvider, SCHEMA_COLUMNS, validate_schema
from .cache import cache_path, load, save, merge_into_cache
from .resample import resample
from .polygon import PolygonProvider
from .kraken import KrakenRESTProvider
from .kraken_csv import aggregate_csv_to_bars, download_quarter
from .alpaca import AlpacaProvider

__all__ = [
    "DataProvider",
    "SCHEMA_COLUMNS",
    "validate_schema",
    "cache_path",
    "load",
    "save",
    "merge_into_cache",
    "resample",
    "PolygonProvider",
    "KrakenRESTProvider",
    "aggregate_csv_to_bars",
    "download_quarter",
    "AlpacaProvider",
]
