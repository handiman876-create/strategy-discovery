"""Reproducible fixture used by translator smoke tests.

Used by `tests/unit/test_translator.py`. Previously also used by
`behavioral_hash()` in `dedup.py` until structural hashing
(`compute_strategy_hash`) replaced it in Phase 4 step 10 — that hash
no longer needs a fixture-derived trade list, but the fixture itself
remains the cleanest way to exercise the translator end-to-end (DSL →
emitted code → import → run).

The fixture is AMD 2024-Q3 (3 months of 5-min bars), loaded from the
train_test cache. Resampled to 1d on demand for daily strategies.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from data.resample import resample
from evaluation.splits import slice_window, train_test_load

FIXTURE_SYMBOL = "AMD"
FIXTURE_START = date(2024, 7, 1)
FIXTURE_END_EXCL = date(2024, 10, 1)


def fixture_5m() -> pd.DataFrame:
    """5-min RTH bars, AMD 2024-07 → 2024-09 inclusive."""
    df = train_test_load(FIXTURE_SYMBOL)
    return slice_window(df, FIXTURE_START, FIXTURE_END_EXCL)


def fixture_1d() -> pd.DataFrame:
    """Daily bars, AMD 2024-07 → 2024-09 (resampled from 5m)."""
    return resample(fixture_5m(), "1d")


def fixture_for_timeframe(timeframe: str) -> pd.DataFrame:
    if timeframe == "5m":
        return fixture_5m()
    if timeframe == "1d":
        return fixture_1d()
    if timeframe in ("15m", "1h"):
        return resample(fixture_5m(), timeframe)
    raise ValueError(f"unsupported fixture timeframe {timeframe!r}")
