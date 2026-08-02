#!/usr/bin/env python3
"""Idempotent Polygon backfill for Phase-2 evaluation.

Fetches bars at --timeframe for the chosen symbol roster from start_date
through today, then splits the result into:

  data/polygon/<SYM>/<TF>.parquet            ←  through HOLDOUT_BOUNDARY
  data/holdout/polygon/<SYM>/<TF>.parquet    ←  HOLDOUT_BOUNDARY onwards

The roster is, in precedence order: --symbols, then --basket (a name from
KNOWN_BASKETS), else required cached symbols (Phase 1) + N seeded picks from
SP500_SUBSET, logged to:

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
# _TIMEFRAME_TO_AGG is the provider's own map of what Polygon can serve; import
# it rather than restating the list here so the two can never drift apart.
from data.polygon import _TIMEFRAME_TO_AGG, PolygonProvider
from evaluation.baskets import KNOWN_BASKETS
from evaluation.symbols import save_symbol_list, sp500_with_required

CACHED_REQUIRED = ["AMD", "NFLX", "SPY", "QQQ", "NVDA"]
HOLDOUT_BOUNDARY = date(2025, 1, 1)

# Default backfill start for daily bars.
DAILY_START = date(2018, 1, 1)

# Polygon serves a ROLLING 5-year window and it applies to every granularity,
# not just 1m (measured 2026-08-02: 5m at 2021-04-28 -> 403, same as 1m;
# 2021-08-02 -> 403, 2021-08-03 -> OK). Requests that STRADDLE the floor are
# clamped silently to what's available rather than erroring, so a value here
# that has aged below the current floor stays safe — it just asks for more than
# the API will serve. A range entirely below the floor 403s. Intraday defaults
# to the floor so the common case never issues a doomed request.
INTRADAY_FLOOR = date(2021, 8, 3)
SYMBOL_LIST_DIR = _ROOT / "data" / "symbol_lists"
TRAIN_TEST_DATA = _ROOT / "data"
HOLDOUT_DATA = _ROOT / "data" / "holdout"


def main() -> int:
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--timeframe", default="5m", choices=sorted(_TIMEFRAME_TO_AGG),
                        help="Bar timeframe to fetch and store")
    parser.add_argument("--start", default=None,
                        help=f"Earliest date to backfill (default: {DAILY_START} for 1d, "
                             f"{INTRADAY_FLOOR} for intraday — Polygon's rolling 5y floor)")
    parser.add_argument("--end", default=None, help="Latest date (default: today)")
    parser.add_argument("--n-symbols", type=int, default=10)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--symbols", default=None,
                        help="Override: comma-separated symbol list (skips seeded pick)")
    parser.add_argument("--basket", default=None,
                        help=f"Named basket to fetch, one of: {sorted(KNOWN_BASKETS)}")
    args = parser.parse_args()

    load_dotenv(_ROOT / ".env", override=True)

    if args.start:
        start = date.fromisoformat(args.start)
    else:
        start = DAILY_START if args.timeframe == "1d" else INTRADAY_FLOOR
    end = date.fromisoformat(args.end) if args.end else date.today()

    if args.symbols:
        symbols = sorted(s.strip().upper() for s in args.symbols.split(","))
        roster_label = "manual"
    elif args.basket:
        if args.basket not in KNOWN_BASKETS:
            parser.error(
                f"unknown basket {args.basket!r}; known: {sorted(KNOWN_BASKETS)}"
            )
        symbols = sorted(KNOWN_BASKETS[args.basket])
        roster_label = args.basket
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
    print(f"  Timeframe: {args.timeframe}")
    print(f"  Range : {start} → {end}")
    print(f"  Holdout boundary: {HOLDOUT_BOUNDARY}")
    print(f"  Symbols: {', '.join(symbols)}")
    print(f"{'='*55}\n")

    provider = PolygonProvider(cache_root=TRAIN_TEST_DATA)

    for i, sym in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}] {sym}")
        df = provider.fetch_bars(sym, args.timeframe, start, end)
        if df.empty:
            print(f"  ! no data returned")
            continue
        n = len(df)
        first = df["timestamp"].iloc[0]
        last = df["timestamp"].iloc[-1]
        print(f"  fetched {n} bars; first {first}; last {last}")
        _split_holdout(sym, df, args.timeframe)

    print("\nDone.")
    return 0


def _split_holdout(symbol: str, df: pd.DataFrame, timeframe: str) -> None:
    """Re-write data/polygon/<sym>/<TF>.parquet to contain only train+test bars
    (< HOLDOUT_BOUNDARY) and write data/holdout/polygon/<sym>/<TF>.parquet
    with the rest."""
    boundary = pd.Timestamp(HOLDOUT_BOUNDARY, tz="America/New_York")
    train_test = df[df["timestamp"] < boundary].reset_index(drop=True)
    holdout = df[df["timestamp"] >= boundary].reset_index(drop=True)

    if not train_test.empty:
        validate_schema(train_test)
        cache_save(TRAIN_TEST_DATA, "polygon", symbol, timeframe, train_test)
        path = cache_path(TRAIN_TEST_DATA, "polygon", symbol, timeframe)
        print(f"  train_test → {path}  ({len(train_test)} bars)")

    if not holdout.empty:
        validate_schema(holdout)
        cache_save(HOLDOUT_DATA, "polygon", symbol, timeframe, holdout)
        path = cache_path(HOLDOUT_DATA, "polygon", symbol, timeframe)
        print(f"  holdout    → {path}  ({len(holdout)} bars)")


if __name__ == "__main__":
    sys.exit(main())
