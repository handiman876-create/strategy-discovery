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


def train_test_load(symbol: str, *, provider: str = "polygon") -> pd.DataFrame:
    """Load the train+test slice (everything before HOLDOUT_BOUNDARY)."""
    if provider != "polygon":
        raise ValueError(f"only polygon supported in Phase 2; got {provider!r}")
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
    return df


def holdout_load(
    symbol: str,
    *,
    provider: str = "polygon",
    final_scoring: bool = False,
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
    return df


def slice_window(df: pd.DataFrame, start: date, end_exclusive: date) -> pd.DataFrame:
    """Return rows in [start, end_exclusive). Used by walk-forward windows."""
    tz = df["timestamp"].dt.tz
    s = pd.Timestamp(start, tz=tz)
    e = pd.Timestamp(end_exclusive, tz=tz)
    return df[(df["timestamp"] >= s) & (df["timestamp"] < e)].reset_index(drop=True)


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df[SCHEMA_COLUMNS].copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_convert("America/New_York")
    df["volume"] = df["volume"].astype(float)
    return df.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)
