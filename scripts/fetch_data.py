#!/usr/bin/env python3
"""Idempotent Polygon backfill for Phase-2 evaluation.

Fetches 5-min bars for the Phase-2 symbol roster from start_date through
today, then splits the result into:

  data/polygon/<SYM>/5min.parquet            ←  through HOLDOUT_BOUNDARY
  data/holdout/polygon/<SYM>/5min.parquet    ←  HOLDOUT_BOUNDARY onwards

The roster = required cached symbols (Phase 1) + N seeded picks from
SP500_SUBSET. Roster is logged to:

  data/symbol_lists/sp500_phase2_seed<N>.json

Idempotency: Provider gap-fills cache, so re-running this script after a
partial fetch resumes from where it left off.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))

import pandas as pd
from dotenv import load_dotenv

from data.base import SCHEMA_COLUMNS, validate_schema
from data.cache import cache_path, save as cache_save
from data.polygon import PolygonProvider
from evaluation.symbols import save_symbol_list, sp500_with_required

CACHED_REQUIRED = ["AMD", "NFLX", "SPY", "QQQ", "NVDA"]
HOLDOUT_BOUNDARY = date(2025, 1, 1)
SYMBOL_LIST_DIR = _ROOT / "data" / "symbol_lists"
TRAIN_TEST_DATA = _ROOT / "data"
HOLDOUT_DATA = _ROOT / "data" / "holdout"


def main() -> int:
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--start", default="2018-01-01", help="Earliest date to backfill")
    parser.add_argument("--end", default=None, help="Latest date (default: today)")
    parser.add_argument("--n-symbols", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--symbols", default=None,
                        help="Override: comma-separated symbol list (skips seeded pick)")
    args = parser.parse_args()

    load_dotenv(_ROOT / ".env", override=True)

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end) if args.end else date.today()

    if args.symbols:
        symbols = sorted(s.strip().upper() for s in args.symbols.split(","))
        roster_label = "manual"
    else:
        symbols = sp500_with_required(
            required=CACHED_REQUIRED, n=args.n_symbols, seed=args.seed
        )
        roster_label = f"sp500_phase2_seed{args.seed}"
        save_symbol_list(
            symbols,
            SYMBOL_LIST_DIR / f"{roster_label}.json",
            seed=args.seed,
            source="SP500_SUBSET + required Phase-1 cached",
        )

    print(f"\n{'='*55}")
    print(f"  Roster: {roster_label}  ({len(symbols)} symbols)")
    print(f"  Range : {start} → {end}")
    print(f"  Holdout boundary: {HOLDOUT_BOUNDARY}")
    print(f"  Symbols: {', '.join(symbols)}")
    print(f"{'='*55}\n")

    provider = PolygonProvider(cache_root=TRAIN_TEST_DATA)

    for i, sym in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}] {sym}")
        df = provider.fetch_bars(sym, "5m", start, end)
        if df.empty:
            print(f"  ! no data returned")
            continue
        n = len(df)
        first = df["timestamp"].iloc[0]
        last = df["timestamp"].iloc[-1]
        print(f"  fetched {n} bars; first {first}; last {last}")
        _split_holdout(sym, df)

    print("\nDone.")
    return 0


def _split_holdout(symbol: str, df: pd.DataFrame) -> None:
    """Re-write data/polygon/<sym>/5min.parquet to contain only train+test bars
    (< HOLDOUT_BOUNDARY) and write data/holdout/polygon/<sym>/5min.parquet
    with the rest."""
    boundary = pd.Timestamp(HOLDOUT_BOUNDARY, tz="America/New_York")
    train_test = df[df["timestamp"] < boundary].reset_index(drop=True)
    holdout = df[df["timestamp"] >= boundary].reset_index(drop=True)

    if not train_test.empty:
        validate_schema(train_test)
        cache_save(TRAIN_TEST_DATA, "polygon", symbol, "5m", train_test)
        path = cache_path(TRAIN_TEST_DATA, "polygon", symbol, "5m")
        print(f"  train_test → {path}  ({len(train_test)} bars)")

    if not holdout.empty:
        validate_schema(holdout)
        cache_save(HOLDOUT_DATA, "polygon", symbol, "5m", holdout)
        path = cache_path(HOLDOUT_DATA, "polygon", symbol, "5m")
        print(f"  holdout    → {path}  ({len(holdout)} bars)")


if __name__ == "__main__":
    sys.exit(main())
