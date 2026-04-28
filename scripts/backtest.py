#!/usr/bin/env python3
"""Phase-1 backtest CLI.

Usage:
    python scripts/backtest.py --strategy casper \
        --symbols AMD,NFLX,SPY,QQQ,NVDA \
        --start 2023-01-01 --end 2026-01-01

Optional flags:
    --data-source       polygon (default) | alpaca-cache (read old project's
                        Alpaca parquets at /root/archive/...)
    --commission        per-trade (default 0.0)
    --slippage          per-share (default 0.01)
    --realistic-fills   apply slippage to all fills (default True)
    --no-realistic-fills  use legacy regression-mode (no slippage on stop/target)
    --starting-capital  per-symbol (default 10000)
    --output            output directory (default results/<timestamp>/)
    --rr-ratio          Casper RR (default 2.0)
    --stop-mode         Casper stop mode (default opposite_bracket)
    --entry-cutoff      Casper entry cutoff HH:MM (default 11:00)
    --eod-exit          Casper EOD exit HH:MM (default 15:50)
    --min-bars-beyond-or
    --retest-timeout    int or 'inf' (default 12)
    --allow-multiple-breakouts / --no-multiple-breakouts (default ON)
    --momentum-fallback (default OFF)
    --no-plot           skip PNG output
"""

from __future__ import annotations

import argparse
import math
import sys
from datetime import date, datetime
from pathlib import Path

# Make src/ and strategies/ importable when run directly.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

import pandas as pd
from dotenv import load_dotenv

from data import PolygonProvider, SCHEMA_COLUMNS, validate_schema
from engine.backtester import BacktestConfig, run_backtest
from engine.metrics import (
    compute_metrics,
    plot_equity_curve,
    print_aggregate_metrics,
    print_metrics,
    save_trade_log,
)
from engine.session import RegularTradingHours

sys.path.insert(0, str(_ROOT / "strategies"))
from manual.casper import CasperStrategy
from manual.buy_and_hold import BuyAndHold

OLD_ALPACA_DIR = Path("/root/archive/trading-backtester-2026-04/data/alpaca")
DATA_DIR = _ROOT / "data"


def _build_strategy(args: argparse.Namespace):
    if args.strategy == "casper":
        return CasperStrategy(
            stop_mode=args.stop_mode,
            stop_value=args.stop_value,
            rr_ratio=args.rr_ratio,
            entry_cutoff=args.entry_cutoff,
            eod_exit=args.eod_exit,
            min_bars_beyond_or=args.min_bars_beyond_or,
            retest_timeout=(math.inf if args.retest_timeout == "inf" else int(args.retest_timeout)),
            allow_multiple_breakouts=args.allow_multiple_breakouts,
            momentum_fallback=args.momentum_fallback,
            momentum_distance=args.momentum_distance,
        )
    if args.strategy == "buy_and_hold":
        return BuyAndHold()
    raise ValueError(f"unknown strategy: {args.strategy!r}")


def _load_polygon_bars(symbols: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
    load_dotenv(_ROOT / ".env", override=True)
    provider = PolygonProvider(cache_root=DATA_DIR)
    out = {}
    for sym in symbols:
        print(f"  fetching {sym} via polygon ({start} → {end})...")
        df = provider.fetch_bars(sym, "5m", start, end)
        out[sym] = df
        print(f"  {sym}: {len(df)} bars")
    return out


def _load_alpaca_cache(symbols: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
    out = {}
    start_ts = pd.Timestamp(start, tz="America/New_York")
    end_ts = pd.Timestamp(end, tz="America/New_York") + pd.Timedelta(days=1)
    for sym in symbols:
        sym_dir = OLD_ALPACA_DIR / sym.upper()
        files = sorted(sym_dir.glob("*.parquet"))
        if not files:
            print(f"  {sym}: no Alpaca cache found at {sym_dir} — skipping")
            out[sym] = pd.DataFrame(columns=SCHEMA_COLUMNS)
            continue
        df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        df["timestamp"] = pd.to_datetime(df["timestamp"]).dt.tz_convert("America/New_York")
        df["volume"] = df["volume"].astype(float)
        df = df[SCHEMA_COLUMNS].drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
        df = df[(df["timestamp"] >= start_ts) & (df["timestamp"] < end_ts)].reset_index(drop=True)
        validate_schema(df)
        out[sym] = df
        print(f"  {sym}: {len(df)} bars (alpaca-cache)")
    return out


def _make_output_dir(arg: str | None) -> Path:
    if arg:
        return Path(arg)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return _ROOT / "results" / stamp


def main() -> int:
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("--strategy", required=True, choices=["casper", "buy_and_hold"])
    parser.add_argument("--symbols", required=True, help="Comma-separated, e.g. AMD,NVDA")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--data-source", default="polygon", choices=["polygon", "alpaca-cache"])
    parser.add_argument("--commission", type=float, default=0.0)
    parser.add_argument("--slippage", type=float, default=0.01)
    parser.add_argument("--realistic-fills", dest="realistic_fills", action="store_true", default=True)
    parser.add_argument("--no-realistic-fills", dest="realistic_fills", action="store_false")
    parser.add_argument("--starting-capital", type=float, default=10_000.0)
    parser.add_argument("--output", default=None)
    parser.add_argument("--no-plot", action="store_true")

    # Casper params
    parser.add_argument("--stop-mode", default="opposite_bracket")
    parser.add_argument("--stop-value", type=float, default=0.5)
    parser.add_argument("--rr-ratio", type=float, default=2.0)
    parser.add_argument("--entry-cutoff", default="11:00")
    parser.add_argument("--eod-exit", default="15:50")
    parser.add_argument("--min-bars-beyond-or", type=int, default=2)
    parser.add_argument("--retest-timeout", default="12", help="int or 'inf'")
    parser.add_argument("--allow-multiple-breakouts", dest="allow_multiple_breakouts",
                        action="store_true", default=True)
    parser.add_argument("--no-multiple-breakouts", dest="allow_multiple_breakouts",
                        action="store_false")
    parser.add_argument("--momentum-fallback", action="store_true", default=False)
    parser.add_argument("--momentum-distance", type=float, default=0.5)

    args = parser.parse_args()
    symbols = [s.strip().upper() for s in args.symbols.split(",")]
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    out_dir = _make_output_dir(args.output)

    print(f"\n{'='*55}")
    print(f"  Strategy        : {args.strategy.upper()}")
    print(f"  Data source     : {args.data_source}")
    print(f"  Symbols         : {', '.join(symbols)}")
    print(f"  Period          : {start} → {end}")
    print(f"  Capital/symbol  : ${args.starting_capital:,.0f}")
    print(f"  Slippage        : ${args.slippage}/share  (realistic_fills={args.realistic_fills})")
    print(f"  Commission      : ${args.commission}/trade")
    if args.strategy == "casper":
        print(f"  Casper          : RR={args.rr_ratio}  stop={args.stop_mode}  "
              f"min_bars_beyond_or={args.min_bars_beyond_or}  "
              f"retest_timeout={args.retest_timeout}  "
              f"multi_breakouts={args.allow_multiple_breakouts}  "
              f"fallback={args.momentum_fallback}")
    print(f"  Output          : {out_dir}")
    print(f"{'='*55}\n")

    # Fetch
    print("[data] loading bars...")
    if args.data_source == "polygon":
        bar_data = _load_polygon_bars(symbols, start, end)
    else:
        bar_data = _load_alpaca_cache(symbols, start, end)

    cfg = BacktestConfig(
        starting_capital=args.starting_capital,
        commission=args.commission,
        slippage=args.slippage,
        realistic_fills=args.realistic_fills,
        session=RegularTradingHours(),
    )

    all_metrics = []
    for sym in symbols:
        df = bar_data.get(sym)
        if df is None or df.empty:
            print(f"  {sym}: no data — skipping")
            continue
        result = run_backtest(sym, df, _build_strategy(args), cfg)
        m = compute_metrics(result)
        all_metrics.append(m)
        print_metrics(m)
        log_path = save_trade_log(result, out_dir)
        print(f"  trade log → {log_path}")
        if not args.no_plot:
            png = plot_equity_curve(result, out_dir)
            if png:
                print(f"  equity plot → {png}")

    if len(all_metrics) > 1:
        print_aggregate_metrics(all_metrics)
    return 0


if __name__ == "__main__":
    sys.exit(main())
