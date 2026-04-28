#!/usr/bin/env python3
"""Phase-2 evaluation CLI.

Usage:
    python scripts/evaluate.py --strategy casper --asset-class stocks

Loads the strategy, runs walk-forward + multi-symbol + bootstrap + random
baseline, prints summary, saves report to results/eval_<timestamp>/<strategy>/.

Holdout (data/holdout/) is sealed: walk-forward optimization wraps in
optimization_mode() so any accidental holdout access raises.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Type

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "strategies"))

from dotenv import load_dotenv

from engine.backtester import BacktestConfig
from engine.session import RegularTradingHours
from evaluation import (
    EvaluationResult,
    WalkForwardConfig,
    load_symbol_list,
    run_evaluation,
    sp500_with_required,
)
from manual.casper import CasperStrategy
from strategy.base import Strategy

STRATEGY_REGISTRY: dict[str, Type[Strategy]] = {
    "casper": CasperStrategy,
}

# Named parameter grids per strategy. Keep these here so the eval CLI can
# reference them without each strategy needing to expose grid config.
GRIDS: dict[str, dict[str, list[Any]]] = {
    "casper_full": {
        "rr_ratio": [1.5, 2.0, 2.5, 3.0],
        "min_bars_beyond_or": [1, 2, 3],
        "momentum_fallback": [False, True],
    },
    "casper_default": {
        "rr_ratio": [1.5, 2.0, 2.5, 3.0],
        "min_bars_beyond_or": [1, 2, 3],
    },
    "casper_smoke": {
        "rr_ratio": [2.0],
        "min_bars_beyond_or": [2],
    },
    "none": None,
}


def main() -> int:
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--strategy", required=True, choices=list(STRATEGY_REGISTRY))
    parser.add_argument("--asset-class", default="stocks", choices=["stocks", "crypto"])
    parser.add_argument("--symbols", default=None, help="Comma list; overrides --symbol-list")
    parser.add_argument("--symbol-list", default="data/symbol_lists/sp500_phase2_seed42.json")
    parser.add_argument("--grid", default="casper_default", choices=list(GRIDS))
    parser.add_argument("--n-bootstrap", type=int, default=5000)
    parser.add_argument("--m-baseline", type=int, default=200)
    parser.add_argument("--bootstrap-seed", type=int, default=0)
    parser.add_argument("--baseline-seed", type=int, default=0)
    parser.add_argument("--output-root", default=str(_ROOT / "results"))
    parser.add_argument("--commission", type=float, default=0.0)
    parser.add_argument("--slippage", type=float, default=0.01)
    parser.add_argument("--starting-capital", type=float, default=10_000.0)
    parser.add_argument("--train-months", type=int, default=24)
    parser.add_argument("--test-months", type=int, default=6)
    parser.add_argument("--step-months", type=int, default=6)
    args = parser.parse_args()

    load_dotenv(_ROOT / ".env", override=True)

    if args.asset_class != "stocks":
        print("crypto evaluation pipeline is not implemented in Phase 2 (Polygon stocks only)")
        return 2

    # Symbols
    if args.symbols:
        symbols = sorted(s.strip().upper() for s in args.symbols.split(","))
    else:
        path = Path(args.symbol_list)
        if not path.exists():
            print(f"symbol list {path} missing — run scripts/fetch_data.py first")
            return 2
        symbols = load_symbol_list(path)

    backtest_cfg = BacktestConfig(
        starting_capital=args.starting_capital,
        commission=args.commission,
        slippage=args.slippage,
        realistic_fills=True,
        session=RegularTradingHours(),
    )
    walk_cfg = WalkForwardConfig(
        train_window_months=args.train_months,
        test_window_months=args.test_months,
        step_months=args.step_months,
        parameter_grid=GRIDS[args.grid],
    )

    print(f"\n{'='*60}")
    print(f"  Strategy   : {args.strategy}")
    print(f"  Asset      : {args.asset_class}")
    print(f"  Symbols    : {len(symbols)} → {', '.join(symbols)}")
    print(f"  Grid       : {args.grid}")
    print(f"  WF window  : train={args.train_months}mo  test={args.test_months}mo  step={args.step_months}mo")
    print(f"  Bootstrap  : n={args.n_bootstrap} seed={args.bootstrap_seed}")
    print(f"  Baseline   : m={args.m_baseline} seed={args.baseline_seed}")
    print(f"{'='*60}\n")

    result = run_evaluation(
        STRATEGY_REGISTRY[args.strategy],
        symbols=symbols,
        backtest_config=backtest_cfg,
        walk_config=walk_cfg,
        n_bootstrap=args.n_bootstrap,
        m_baseline=args.m_baseline,
        bootstrap_seed=args.bootstrap_seed,
        baseline_seed=args.baseline_seed,
        output_root=Path(args.output_root),
    )

    _print_summary(result)
    return 0


def _print_summary(r: EvaluationResult) -> None:
    print(f"\n{'─'*60}")
    print(f"  Per-symbol OOS results")
    print(f"{'─'*60}")
    print(f"  {'symbol':<6} {'trades':>6} {'PF':>6} {'CI_low':>7} {'CI_high':>7} {'base_p':>7}")
    for s in r.per_symbol:
        print(
            f"  {s.symbol:<6} {s.n_oos_trades:>6} {s.pf:>6.3f} "
            f"{s.bootstrap.ci_lower:>7.3f} {s.bootstrap.ci_upper:>7.3f} {s.baseline.p_value:>7.3f}"
        )
        for w in s.warnings:
            print(f"         ! {w}")

    print(f"\n{'═'*60}")
    print(f"  Aggregate / Verdict")
    print(f"{'═'*60}")
    bd = r.breakdown
    print(f"  Median PF across symbols  : {bd.median_pf:.3f}")
    print(f"  Consistency factor        : {bd.consistency_factor:.3f}")
    print(f"  Parameter penalty         : {bd.parameter_penalty:.3f}")
    print(f"  Significance factor       : {bd.significance_factor:.3f} (agg p={r.aggregate_p_value:.3f})")
    print(f"  Robustness score          : {bd.score:.3f}")
    print()
    print(f"  PROMISING: {'YES ✓' if r.verdict.is_promising else 'NO  ✗'}")
    if r.verdict.failed_conditions:
        print(f"  Failed conditions:")
        for c in r.verdict.failed_conditions:
            print(f"    - {c.name} {c.required}; actual={c.actual:.3f}  deficit={c.deficit:.3f}")
    if r.output_dir is not None:
        print(f"\n  Report → {r.output_dir}")


if __name__ == "__main__":
    sys.exit(main())
