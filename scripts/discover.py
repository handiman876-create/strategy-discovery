#!/usr/bin/env python3
"""Phase-3 generator CLI.

Usage:
    python scripts/discover.py --archetype mean_reversion
    python scripts/discover.py --archetype momentum --evaluate --fast

Pipeline:
  1. Generate a StrategySpec via Claude API (tool-use, retries on failure).
  2. Translate to strategies/generated/<name>.py.
  3. Compute behavioral hash (dedup against prior generations).
  4. Optionally evaluate (canonical or fast).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "strategies"))

from dotenv import load_dotenv

from engine.backtester import BacktestConfig
from engine.session import RegularTradingHours
from evaluation import (
    WalkForwardConfig,
    run_evaluation,
    run_fast_evaluation,
)
from generator.archetypes import all_archetype_names, get_archetype
from generator.pipeline import generate_and_translate, generate_strategy
from leaderboard.db import initialize_db


def _check_timeframe_archetype_compat(
    archetype: str, timeframe: str | None
) -> str | None:
    """Validate that the requested timeframe is supported by the archetype.
    Returns None on a valid combo; returns a stderr-ready error message
    when the combo is unsatisfiable.

    Each archetype declares `allowed_timeframes` (`src/generator/archetypes.py`)
    which is a subset of the spec validator's TIMEFRAMES. Without this CLI-
    layer check, an unsatisfiable combo (e.g. mean_reversion + 5m) would
    burn 3 API calls in a futile retry loop — the model could comply with
    one constraint or the other, never both."""
    if timeframe is None:
        return None
    arch = get_archetype(archetype)
    if timeframe not in arch.allowed_timeframes:
        return (
            f"archetype {archetype!r} does not support timeframe "
            f"{timeframe!r}. Allowed for {archetype}: "
            f"{arch.allowed_timeframes}"
        )
    return None


def main() -> int:
    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--archetype", required=True, choices=all_archetype_names())
    parser.add_argument("--asset-class", default=None,
                        help="Override default asset class for the archetype")
    parser.add_argument("--evaluate", dest="evaluate", action="store_true", default=True,
                        help="Run the new strategy through the eval pipeline")
    parser.add_argument("--no-evaluate", dest="evaluate", action="store_false")
    parser.add_argument("--fast", action="store_true",
                        help="WARNING: Fast eval is NOT canonical. Use only for "
                             "demo / sanity checks. Real promotion decisions MUST "
                             "use the full eval pipeline.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Generate spec only, don't translate or evaluate")
    parser.add_argument("--diversity-n", type=int, default=5)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--no-dedup", action="store_true")
    parser.add_argument(
        "--timeframe",
        default=None,
        choices=["5m", "15m", "1h", "1d"],
        help="Constrain the model to generate a strategy at this timeframe. "
             "Choices match src/generator/spec.py TIMEFRAMES; 30m / 4h are "
             "deferred (see docs/backlog.md). Up to 3 retries on noncompliance, "
             "then the generation is skipped.",
    )
    args = parser.parse_args()

    err = _check_timeframe_archetype_compat(args.archetype, args.timeframe)
    if err is not None:
        print(f"Error: {err}", file=sys.stderr)
        return 2

    load_dotenv(_ROOT / ".env", override=True)

    # Phase 4 step 8b plumbing: open the leaderboard DB once at startup.
    # Functions called below accept conn as a no-op kwarg for now; commits
    # 8c (generator hook) and 8d (eval hooks) will use it to record rows.
    conn = initialize_db()
    try:
        return _run(args, conn)
    finally:
        conn.close()


def _run(args, conn) -> int:
    print(f"\n{'='*60}")
    print(f"  Archetype : {args.archetype}")
    print(f"  Mode      : {'DRY-RUN' if args.dry_run else 'GENERATE+TRANSLATE'}")
    if args.timeframe is not None:
        print(f"  Timeframe : {args.timeframe} (constrained; up to 3 retries on noncompliance)")
    if args.evaluate and not args.dry_run:
        print(f"  Evaluate  : {'FAST (NON-CANONICAL)' if args.fast else 'CANONICAL'}")
        if args.fast:
            print(f"              ⚠ Fast eval: 5 symbols, no grid, n_bootstrap=500. "
                  f"Do not promote based on these numbers.")
    print(f"{'='*60}\n")

    if args.dry_run:
        result = generate_strategy(
            args.archetype,
            diversity_n=args.diversity_n,
            max_retries=args.max_retries,
            requested_timeframe=args.timeframe,
        )
        if result.spec is None:
            print(f"GENERATION FAILED: {result.failure_reason}")
            for log in result.logs:
                print(f"  attempt {log.attempt}: {log.error}")
            return 1
        print(f"✓ Spec generated: {result.spec.name}")
        print(f"  thesis     : {result.spec.thesis}")
        print(f"  indicators : {[i.type for i in result.spec.indicators]}")
        print(f"  parameters : {[p.name for p in result.spec.parameters]}")
        print(f"  timeframes : {result.spec.timeframes}")
        print(f"\n  attempts   : {len(result.logs)}")
        for log in result.logs:
            print(
                f"    {log.attempt}. {log.input_tokens + log.output_tokens} toks  "
                f"(read={log.cache_read_input_tokens}, "
                f"write={log.cache_creation_input_tokens})  "
                f"${log.actual_cost_usd:.4f}"
            )
        return 0

    result = generate_and_translate(
        args.archetype,
        diversity_n=args.diversity_n,
        max_retries=args.max_retries,
        dedup=not args.no_dedup,
        conn=conn,
        requested_timeframe=args.timeframe,
    )
    if result.spec is None:
        print(f"GENERATION FAILED: {result.failure_reason}")
        for log in result.logs:
            print(f"  attempt {log.attempt}: {log.error}")
        return 1

    print(f"✓ Spec       : {result.spec.name}")
    print(f"✓ Code       : {result.code_path}")
    print(f"✓ Hash       : {result.strategy_hash}")
    cost_total = sum(l.actual_cost_usd for l in result.logs)
    print(f"  Cost       : ${cost_total:.4f}  ({len(result.logs)} attempt(s))")

    if not args.evaluate:
        return 0

    # Evaluate
    cfg = BacktestConfig(
        starting_capital=10_000,
        commission=0.0,
        slippage=0.01,
        realistic_fills=True,
        session=RegularTradingHours(),
    )

    # Lazily import the generated class
    import importlib.util
    spec_mod = importlib.util.spec_from_file_location(result.spec.name, result.code_path)
    mod = importlib.util.module_from_spec(spec_mod)
    spec_mod.loader.exec_module(mod)
    class_name = "".join(p.capitalize() for p in result.spec.name.split("_"))
    StrategyClass = getattr(mod, class_name)

    if args.fast:
        wf = WalkForwardConfig(
            train_window_months=24, test_window_months=6, step_months=6, parameter_grid=None
        )
        eval_result = run_fast_evaluation(
            StrategyClass,
            backtest_config=cfg,
            walk_config=wf,
            output_root=_ROOT / "results",
            conn=conn,
            strategy_hash=result.strategy_hash,
        )
        print(f"\n[FAST EVAL — NON-CANONICAL]")
        print(f"  median_pf = {eval_result.median_pf:.3f}")
        print(f"  n_oos     = {eval_result.n_oos_trades_total}")
        print(f"  promising : {'YES' if eval_result.verdict.is_promising else 'NO'}")
        if eval_result.verdict.failed_conditions:
            for c in eval_result.verdict.failed_conditions:
                print(f"    - {c.name} {c.required}; actual={c.actual:.3f}")
        if eval_result.output_dir:
            print(f"  report → {eval_result.output_dir}")
    else:
        # Canonical evaluation — use full 10-symbol pipeline.
        from evaluation.symbols import load_symbol_list
        symlist_path = _ROOT / "data/symbol_lists/sp500_phase2_seed42.json"
        symbols = load_symbol_list(symlist_path)
        wf = WalkForwardConfig(
            train_window_months=24, test_window_months=6, step_months=6, parameter_grid=None
        )
        eval_result = run_evaluation(
            StrategyClass,
            symbols=symbols,
            backtest_config=cfg,
            walk_config=wf,
            output_root=_ROOT / "results",
            conn=conn,
            strategy_hash=result.strategy_hash,
        )
        print(f"\n[CANONICAL EVAL]")
        print(f"  median_pf = {eval_result.breakdown.median_pf:.3f}")
        print(f"  score     = {eval_result.breakdown.score:.3f}")
        print(f"  promising : {'YES' if eval_result.verdict.is_promising else 'NO'}")
        if eval_result.verdict.failed_conditions:
            for c in eval_result.verdict.failed_conditions:
                print(f"    - {c.name} {c.required}; actual={c.actual:.3f}")
        if eval_result.output_dir:
            print(f"  report → {eval_result.output_dir}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
