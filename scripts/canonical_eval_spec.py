#!/usr/bin/env python3
"""Canonical-eval a STORED generated spec by leaderboard hash.

evaluate.py only knows the manual strategy classes (casper, rsi2_mr); the
generated specs live as spec_json in the leaderboard `strategies` table. This
runner rebuilds the StrategySpec from that JSON, translates it to a strategy
module, and runs the SAME canonical pipeline discover.py uses (10-symbol
sp500_phase2_seed42 list, walk-forward 24/6/6, no grid), recording
eval_type='canonical' so the strategy advances to canonical_evaluated.

Usage: python scripts/canonical_eval_spec.py <hash-prefix>
One hash per invocation — so the operator can stop and read between runs.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from engine.backtester import BacktestConfig
from engine.session import RegularTradingHours
from evaluation import WalkForwardConfig, run_evaluation
from evaluation.symbols import load_symbol_list
from generator.spec import StrategySpec
from generator.translator import translate_to_file
from leaderboard.db import initialize_db


def _class_name(spec_name: str) -> str:
    return "".join(p.capitalize() for p in spec_name.split("_"))


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: canonical_eval_spec.py <hash-prefix>")
        return 2
    hash_prefix = sys.argv[1]

    db_path = _ROOT / "db" / "leaderboard.db"
    conn = initialize_db(str(db_path))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT strategy_hash, name, spec_json FROM strategies "
        "WHERE strategy_hash LIKE ?",
        (hash_prefix + "%",),
    ).fetchone()
    if row is None:
        print(f"no strategy matches hash prefix {hash_prefix!r}")
        return 1
    full_hash = row["strategy_hash"]
    spec = StrategySpec.model_validate(json.loads(row["spec_json"]))

    print(f"=== CANONICAL EVAL: {spec.name} ({full_hash[:12]}) ===")
    print(f"  archetype={spec.archetype}  timeframes={spec.timeframes}")

    code_path = translate_to_file(spec)
    spec_mod = importlib.util.spec_from_file_location(spec.name, code_path)
    mod = importlib.util.module_from_spec(spec_mod)
    spec_mod.loader.exec_module(mod)
    StrategyClass = getattr(mod, _class_name(spec.name))

    symbols = load_symbol_list(_ROOT / "data/symbol_lists/sp500_phase2_seed42.json")
    cfg = BacktestConfig(
        starting_capital=10_000,
        commission=0.0,
        slippage=0.01,
        realistic_fills=True,
        session=RegularTradingHours(),
    )
    wf = WalkForwardConfig(
        train_window_months=24, test_window_months=6, step_months=6, parameter_grid=None
    )

    result = run_evaluation(
        StrategyClass,
        symbols=symbols,
        backtest_config=cfg,
        walk_config=wf,
        output_root=_ROOT / "results",
        conn=conn,
        strategy_hash=full_hash,
    )

    # Aggregate CI (median of per-symbol bootstrap ci_lower where trades exist),
    # matching how classify_promise weights it inside run_evaluation.
    import statistics
    active = [s for s in result.per_symbol if s.n_oos_trades > 0]
    ci_lows = [s.bootstrap.ci_lower for s in active]
    ci_lower_agg = statistics.median(ci_lows) if ci_lows else 0.0
    n_oos_total = sum(s.n_oos_trades for s in result.per_symbol)

    # Win rate across all OOS trades.
    all_trades = [t for s in result.per_symbol for t in s.walk_forward.all_oos_trades]
    wins = sum(1 for t in all_trades if t.pnl > 0)
    win_rate = wins / len(all_trades) if all_trades else 0.0

    b = result.breakdown
    print("\n--- PER SYMBOL ---")
    print(f'{"sym":5s} {"oos":>5s} {"pf":>7s} {"ci_low":>7s} {"ci_hi":>7s}')
    for s in result.per_symbol:
        print(f"{s.symbol:5s} {s.n_oos_trades:>5d} {s.pf:>7.2f} "
              f"{s.bootstrap.ci_lower:>7.2f} {s.bootstrap.ci_upper:>7.2f}")

    print("\n--- AGGREGATE (canonical) ---")
    print(f"  median_pf        : {b.median_pf:.3f}")
    print(f"  score            : {b.score:.3f}   (canonical gate: > 1.5)")
    print(f"  ci_lower (agg)   : {ci_lower_agg:.3f}   (gate: > 1.0)")
    print(f"  win_rate         : {win_rate*100:.1f}%  ({wins}/{len(all_trades)})")
    print(f"  n_oos_trades     : {n_oos_total}")
    print(f"  aggregate_p      : {result.aggregate_p_value:.4f}")
    print(f"  PROMISING        : {'YES' if result.verdict.is_promising else 'NO'}")
    for c in result.verdict.failed_conditions:
        print(f"    FAILED {c.name} {c.required}; actual={c.actual:.3f} (deficit {c.deficit:.3f})")
    if result.output_dir:
        print(f"  report → {result.output_dir}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
