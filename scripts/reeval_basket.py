#!/usr/bin/env python3
"""Re-run the fast screen for a cohort of strategies on the CURRENT fast basket.

Why: ci_lower is only comparable within a basket. When FAST_BASKET changes,
existing fast rows do not become wrong — they become incomparable to new ones.
Rather than backfill 221 rows (most of which were nowhere near the gate), this
re-evaluates only the near-misses worth a second look, writing NEW rows tagged
with the new basket_version. Old rows are left exactly as they are.

Selection: strategies whose BEST fast ci_lower on the old basket exceeds
--ci-min, that have >= --trades-min OOS trades, and that have not already been
re-evaluated on the current basket. Ordered by ci_lower descending.

Cost: $0 API — no generation, no LLM calls. Local compute over cached parquet.
Usage: reeval_basket.py [--ci-min 0.80] [--trades-min 50] [--dry-run]
"""
from __future__ import annotations

import argparse
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
from evaluation.baskets import FAST_BASKET, basket_identity
from evaluation.fast_pipeline import run_fast_evaluation
from generator.spec import StrategySpec
from generator.translator import translate_to_file
from leaderboard.db import initialize_db


def _class_name(spec_name: str) -> str:
    return "".join(p.capitalize() for p in spec_name.split("_"))


def _cfg() -> BacktestConfig:
    # Must match autodiscover._cfg() — a re-eval under different backtest
    # assumptions would compare the basket change against a moving baseline.
    return BacktestConfig(
        starting_capital=10_000, commission=0.0, slippage=0.01,
        realistic_fills=True, session=RegularTradingHours(),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ci-min", type=float, default=0.80,
                    help="Re-evaluate strategies whose best old-basket fast "
                         "ci_lower exceeds this. Below it, a candidate was not "
                         "a near-miss and a basket swap will not rescue it.")
    ap.add_argument("--trades-min", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true",
                    help="List the cohort and exit; write nothing.")
    args = ap.parse_args()

    label, bhash = basket_identity(FAST_BASKET)
    conn = initialize_db(str(_ROOT / "db" / "leaderboard.db"))
    conn.row_factory = sqlite3.Row

    # MAX(ci_lower) per strategy: a strategy may hold several fast rows. Use its
    # best-ever showing so the cohort is generous — the point is to avoid
    # discarding a real edge because of one weak run.
    rows = conn.execute(
        """
        SELECT s.strategy_hash, s.name, s.archetype, s.spec_json,
               MAX(e.ci_lower) AS best_ci,
               MAX(e.n_oos_trades) AS best_n
          FROM evaluations e
          JOIN strategies s USING (strategy_hash)
         WHERE e.eval_type = 'fast'
           AND e.ci_lower IS NOT NULL
           AND e.basket_version != ?
           AND s.strategy_hash NOT IN (
                 SELECT strategy_hash FROM evaluations
                  WHERE eval_type = 'fast' AND basket_version = ?
               )
         GROUP BY s.strategy_hash
        HAVING best_ci > ? AND best_n >= ?
         ORDER BY best_ci DESC
        """,
        (label, label, args.ci_min, args.trades_min),
    ).fetchall()

    print(f"=== RE-EVAL COHORT: basket {label} ({bhash}) ===")
    print(f"  symbols: {FAST_BASKET}")
    print(f"  criteria: old-basket ci_lower > {args.ci_min}, n_oos >= {args.trades_min}")
    print(f"  cohort size: {len(rows)}\n")
    if args.dry_run:
        for r in rows:
            print(f"  {r['strategy_hash'][:12]}  ci={r['best_ci']:.3f}  "
                  f"n={r['best_n']:<5} {r['name'][:34]:<34} {r['archetype']}")
        return 0

    print(f"{'hash':<14}{'name':<34}{'old_ci':>8}{'new_ci':>8}{'new_pf':>8}"
          f"{'score':>8}{'n':>6}  verdict")
    print("-" * 96)
    survivors, failures = [], []
    for r in rows:
        spec = StrategySpec.model_validate(json.loads(r["spec_json"]))
        try:
            code_path = translate_to_file(spec)
            m = importlib.util.spec_from_file_location(spec.name, code_path)
            mod = importlib.util.module_from_spec(m)
            m.loader.exec_module(mod)
            cls = getattr(mod, _class_name(spec.name))
            fast = run_fast_evaluation(
                cls, backtest_config=_cfg(), conn=conn,
                strategy_hash=r["strategy_hash"], symbols=FAST_BASKET,
            )
        except Exception as e:
            print(f"{r['strategy_hash'][:12]:<14}{r['name'][:34]:<34}  ERROR {str(e)[:40]}")
            continue

        # Same gate autodiscover applies: ci_lower AND trade floor AND score.
        survived = (
            fast.ci_lower > 1.0
            and fast.n_oos_trades_total >= args.trades_min
            and fast.breakdown.score > 1.5
        )
        (survivors if survived else failures).append((r, fast))
        print(f"{r['strategy_hash'][:12]:<14}{r['name'][:34]:<34}"
              f"{r['best_ci']:>8.3f}{fast.ci_lower:>8.3f}{fast.median_pf:>8.2f}"
              f"{fast.breakdown.score:>8.3f}{fast.n_oos_trades_total:>6}"
              f"  {'SURVIVED' if survived else 'screened-out'}")

    print(f"\n--- SUMMARY ---")
    print(f"  re-evaluated : {len(survivors) + len(failures)}")
    print(f"  survived     : {len(survivors)}")
    print(f"  screened out : {len(failures)}")
    if survivors:
        print(f"\n  canonical candidates:")
        for r, f in survivors:
            print(f"    {r['strategy_hash'][:12]}  {r['name']}  "
                  f"ci={f.ci_lower:.3f} score={f.breakdown.score:.3f} n={f.n_oos_trades_total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
