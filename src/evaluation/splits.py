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

# Timeframes stored natively on disk as their own parquet per symbol. Anything
# else is resampled UP from _NATIVE_TIMEFRAME at load time.
#
# "1m" was added 2026-08-02 with its OWN source files. It is deliberately in
# _STORED_TIMEFRAMES rather than served by the resampler: resampling 5m -> 1m
# does not fail, it maps each 5m bar into a single 1-minute bin and hands back
# wrong-frequency data wearing a "1m" label. Finer-than-native timeframes must
# always read their own file — see `_source_timeframe`.
_NATIVE_TIMEFRAME = "5m"
_STORED_TIMEFRAMES = frozenset({"1m", "5m"})
_SERVABLE_TIMEFRAMES = frozenset({"1m", "5m", "15m", "30m", "1h", "4h", "1d"})

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

    `target_timeframe` is read from its own parquet when natively stored
    (see `_source_timeframe`), otherwise resampled from 5m source data to the
    requested coarser bar size. Defaults to "5m" (no-op)."""
    if provider != "polygon":
        raise ValueError(f"only polygon supported in Phase 2; got {provider!r}")
    _resolve_timeframe(target_timeframe)
    source_tf = _source_timeframe(target_timeframe)
    path = TRAIN_TEST_ROOT / symbol.upper() / f"{source_tf}.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"no train_test {source_tf} data for {symbol} at {path}; "
            f"run scripts/fetch_data.py --timeframe {source_tf} first"
        )
    df = pd.read_parquet(path)
    df = _normalize(df)
    boundary = pd.Timestamp(HOLDOUT_BOUNDARY, tz="America/New_York")
    df = df[df["timestamp"] < boundary].reset_index(drop=True)
    validate_schema(df)
    if target_timeframe != source_tf:
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
    source_tf = _source_timeframe(target_timeframe)
    path = HOLDOUT_ROOT / symbol.upper() / f"{source_tf}.parquet"
    if not path.exists():
        raise FileNotFoundError(
            f"no holdout {source_tf} data for {symbol} at {path}; "
            f"run scripts/fetch_data.py --timeframe {source_tf} first"
        )
    df = pd.read_parquet(path)
    df = _normalize(df)
    boundary = pd.Timestamp(HOLDOUT_BOUNDARY, tz="America/New_York")
    df = df[df["timestamp"] >= boundary].reset_index(drop=True)
    validate_schema(df)
    if target_timeframe != source_tf:
        df = _resample_bars(df, target_timeframe)
    return df


def slice_window(df: pd.DataFrame, start: date, end_exclusive: date) -> pd.DataFrame:
    """Return rows in [start, end_exclusive). Used by walk-forward windows."""
    tz = df["timestamp"].dt.tz
    s = pd.Timestamp(start, tz=tz)
    e = pd.Timestamp(end_exclusive, tz=tz)
    return df[(df["timestamp"] >= s) & (df["timestamp"] < e)].reset_index(drop=True)


def _source_timeframe(target_timeframe: str) -> str:
    """Which on-disk parquet backs `target_timeframe`.

    Natively-stored timeframes read their own file; everything coarser is
    resampled from `_NATIVE_TIMEFRAME`. Centralized in one place because every
    reader must agree: a caller that reads 5m and resamples to a FINER target
    gets wrong-frequency bars back with no error, so the source choice can
    never be re-derived ad hoc at each call site."""
    if target_timeframe in _STORED_TIMEFRAMES:
        return target_timeframe
    return _NATIVE_TIMEFRAME


def _resolve_timeframe(target_timeframe: str) -> None:
    """Per Additional ask C: keep this check as a stub even though every
    entry in the spec's TIMEFRAMES literal is now either natively stored or
    servable from 5m via resampling. Future additions (e.g. "tick") would be
    servable from neither and must explicitly fail here rather than silently
    returning the wrong-frequency native bars."""
    if target_timeframe not in _SERVABLE_TIMEFRAMES:
        raise ValueError(
            f"timeframe {target_timeframe!r} is not servable. Servable: "
            f"{sorted(_SERVABLE_TIMEFRAMES)} (natively stored: "
            f"{sorted(_STORED_TIMEFRAMES)}; the rest resampled from "
            f"{_NATIVE_TIMEFRAME}). To support a new timeframe, either add "
            f"source data for it and list it in _STORED_TIMEFRAMES, or extend "
            f"_SERVABLE_TIMEFRAMES if the resampler can reach it from "
            f"{_NATIVE_TIMEFRAME}."
        )


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df[SCHEMA_COLUMNS].copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_convert("America/New_York")
    df["volume"] = df["volume"].astype(float)
    return df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
