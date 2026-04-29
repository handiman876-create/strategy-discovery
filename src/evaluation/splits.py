"""Three-way data split with code-level holdout enforcement.

Layout
------
data/polygon/<SYM>/5m.parquet              ← train + test (≤ 2024-12-31)
data/holdout/polygon/<SYM>/5m.parquet      ← holdout      (≥ 2025-01-01)

Enforcement
-----------
The holdout loader checks a `ContextVar` that walk-forward optimization sets
to True. If the flag is True at the time `holdout_load` is called, OR if
`final_scoring=True` is not explicitly passed, the loader raises
`HoldoutAccessError`.

This is enforced at the LOADING level. Strategies receive bars via the
engine; they have no path to load holdout themselves. The protection
catches framework code that would mistakenly read holdout during
optimization or evaluation.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime
from pathlib import Path
from typing import Iterator

import pandas as pd

from data.base import SCHEMA_COLUMNS, validate_schema
from data.resample import resample as _resample_bars

# Available timeframes per the spec's TIMEFRAMES literal. Source data is 5m,
# everything else is resampled on load. If new finer timeframes are added
# (e.g. "1m"), they'll need a separate data source — `_resolve_timeframe`
# raises rather than silently returning the wrong-frequency 5m bars.
_NATIVE_TIMEFRAME = "5m"
_SERVABLE_TIMEFRAMES = frozenset({"5m", "15m", "30m", "1h", "4h", "1d"})

_ROOT = Path(__file__).resolve().parents[2]
TRAIN_TEST_ROOT = _ROOT / "data" / "polygon"
HOLDOUT_ROOT = _ROOT / "data" / "holdout" / "polygon"
HOLDOUT_BOUNDARY = date(2025, 1, 1)

# True while the framework is running optimization / walk-forward.
# `holdout_load()` raises if this is set, regardless of `final_scoring`.
_OPT_MODE: ContextVar[bool] = ContextVar("optimization_mode", default=False)


class HoldoutAccessError(RuntimeError):
    """Raised when code attempts to read holdout data during optimization,
    or without explicit final-scoring authorization."""


@contextmanager
def optimization_mode() -> Iterator[None]:
    """Mark all enclosed code as 'optimization' — `holdout_load` will refuse
    to return data while this is active."""
    token = _OPT_MODE.set(True)
    try:
        yield
    finally:
        _OPT_MODE.reset(token)


def is_in_optimization_mode() -> bool:
    return _OPT_MODE.get()


def train_test_load(
    symbol: str,
    *,
    provider: str = "polygon",
    target_timeframe: str = _NATIVE_TIMEFRAME,
) -> pd.DataFrame:
    """Load the train+test slice (everything before HOLDOUT_BOUNDARY).

    `target_timeframe` resamples 5m source data to a coarser bar size if
    requested. Defaults to "5m" (no-op). Raises if the requested timeframe
    is finer than the native data — we cannot synthesize sub-5m bars."""
    if provider != "polygon":
        raise ValueError(f"only polygon supported in Phase 2; got {provider!r}")
    _resolve_timeframe(target_timeframe)
    path = TRAIN_TEST_ROOT / symbol.upper() / "5m.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"no train_test data for {symbol} at {path}; "
            f"run scripts/fetch_data.py first"
        )
    df = pd.read_parquet(path)
    df = _normalize(df)
    boundary = pd.Timestamp(HOLDOUT_BOUNDARY, tz="America/New_York")
    df = df[df["timestamp"] < boundary].reset_index(drop=True)
    validate_schema(df)
    if target_timeframe != _NATIVE_TIMEFRAME:
        df = _resample_bars(df, target_timeframe)
    return df


def holdout_load(
    symbol: str,
    *,
    provider: str = "polygon",
    final_scoring: bool = False,
    target_timeframe: str = _NATIVE_TIMEFRAME,
) -> pd.DataFrame:
    """Load the holdout slice. Refuses to return data:
      * while `optimization_mode()` is active, or
      * unless the caller explicitly passes `final_scoring=True`.
    """
    if is_in_optimization_mode():
        raise HoldoutAccessError(
            "holdout data cannot be loaded inside optimization_mode(). "
            "Holdout is reserved for final scoring after walk-forward "
            "optimization is complete."
        )
    if not final_scoring:
        raise HoldoutAccessError(
            "holdout_load requires final_scoring=True. This is a deliberate "
            "speed bump: holdout data is touched only at the very end of an "
            "evaluation, never during optimization or development."
        )
    if provider != "polygon":
        raise ValueError(f"only polygon supported in Phase 2; got {provider!r}")
    _resolve_timeframe(target_timeframe)
    path = HOLDOUT_ROOT / symbol.upper() / "5m.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"no holdout data for {symbol} at {path}; "
            f"run scripts/fetch_data.py first"
        )
    df = pd.read_parquet(path)
    df = _normalize(df)
    boundary = pd.Timestamp(HOLDOUT_BOUNDARY, tz="America/New_York")
    df = df[df["timestamp"] >= boundary].reset_index(drop=True)
    validate_schema(df)
    if target_timeframe != _NATIVE_TIMEFRAME:
        df = _resample_bars(df, target_timeframe)
    return df


def slice_window(df: pd.DataFrame, start: date, end_exclusive: date) -> pd.DataFrame:
    """Return rows in [start, end_exclusive). Used by walk-forward windows."""
    tz = df["timestamp"].dt.tz
    s = pd.Timestamp(start, tz=tz)
    e = pd.Timestamp(end_exclusive, tz=tz)
    return df[(df["timestamp"] >= s) & (df["timestamp"] < e)].reset_index(drop=True)


def _resolve_timeframe(target_timeframe: str) -> None:
    """Per Additional ask C: keep this check as a stub even though every
    entry in the spec's TIMEFRAMES literal is currently servable from 5m
    via resampling. Future additions (e.g. "1m" or "tick") would not be
    servable from 5m source data and must explicitly fail here rather
    than silently returning the wrong-frequency native bars."""
    if target_timeframe not in _SERVABLE_TIMEFRAMES:
        raise ValueError(
            f"timeframe {target_timeframe!r} is not servable from native 5m data. "
            f"Servable timeframes: {sorted(_SERVABLE_TIMEFRAMES)}. "
            f"To support a new timeframe, either add finer source data or "
            f"extend _SERVABLE_TIMEFRAMES if the resampler can handle it."
        )


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df[SCHEMA_COLUMNS].copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_convert("America/New_York")
    df["volume"] = df["volume"].astype(float)
    return df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
