#!/usr/bin/env python3
"""Re-run the fast screen for a cohort of strategies on the CURRENT fast basket.

Why: ci_lower is only comparable within a basket. When FAST_BASKET changes,
existing fast rows do not become wrong — they become incomparable to new ones.
Rather than backfill 221 rows (most of which were nowhere near the gate), this
re-evaluates only the near-misses worth a second look, writing NEW rows tagged
with the new basket_version. Old rows are left exactly as they are.

Selection: strategies whose BEST fast ci_lower on a DIFFERENT basket exceeds
--ci-min, that have >= --trades-min OOS trades, and that have not already been
re-evaluated on the target basket. Ordered by ci_lower descending.

--basket names the basket to RUN on (default: the current FAST_BASKET). It can
name a retired basket, which is what makes this usable as a paired
basket-sensitivity probe rather than only a forward migration: pointing it at
tech5_v1 with --timeframe 5m re-runs today's 5m cohort on the old roster and
yields a paired delta per spec. Pair it with --note so the resulting rows are
distinguishable from that basket's original era in the leaderboard.

Cost: $0 API — no generation, no LLM calls. Local compute over cached parquet.
Usage: reeval_basket.py [--ci-min 0.80] [--trades-min 50] [--dry-run]
                        [--basket tech5_v1] [--timeframe 5m] [--limit N]
                        [--note probe-label]
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import random
import sqlite3
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from engine.backtester import BacktestConfig
from engine.session import RegularTradingHours
from evaluation.baskets import FAST_BASKET, KNOWN_BASKETS, basket_identity
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
    ap.add_argument("--basket", default=None, choices=sorted(KNOWN_BASKETS),
                    help="Basket to RUN on. Defaults to FAST_BASKET "
                         "(diverse8_v1). Naming a retired basket turns this "
                         "into a paired sensitivity probe — see module docstring.")
    ap.add_argument("--timeframe", default=None,
                    help="Restrict the cohort to one timeframe (e.g. 5m). "
                         "ci_lower is confounded with trade count, so a mixed-"
                         "timeframe cohort mixes two delta distributions.")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap cohort size (highest ci_lower first). Use to time "
                         "a few evals before committing to the full cohort.")
    ap.add_argument("--stratify", default=None,
                    help="Comma-separated ci_lower bucket edges, e.g. "
                         "'0.0,0.5,0.7,0.85,1.0'. Samples --per-bucket specs "
                         "from each bucket instead of taking the top N. A top-N "
                         "cohort is selected on its home basket and so regresses "
                         "downward on re-eval; stratifying spreads the cohort "
                         "over the range so the delta is not confounded with "
                         "selection. Overrides --limit.")
    ap.add_argument("--per-bucket", type=int, default=5)
    ap.add_argument("--seed", type=int, default=0,
                    help="Seed for stratified sampling. Fixed so a probe is "
                         "reproducible — the cohort must be re-derivable from "
                         "the command line alone.")
    ap.add_argument("--note", default=None,
                    help="Provenance marker written to each row's "
                         "imported_from. Set this for probe runs so the rows "
                         "are separable from that basket's original era.")
    args = ap.parse_args()

    target_basket = KNOWN_BASKETS[args.basket] if args.basket else FAST_BASKET
    label, bhash = basket_identity(target_basket)
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
           AND (? IS NULL OR s.timeframe = ?)
           AND s.strategy_hash NOT IN (
                 SELECT strategy_hash FROM evaluations
                  WHERE eval_type = 'fast' AND basket_version = ?
               )
         GROUP BY s.strategy_hash
        HAVING best_ci > ? AND best_n >= ?
         ORDER BY best_ci DESC
        """,
        (label, args.timeframe, args.timeframe, label,
         args.ci_min, args.trades_min),
    ).fetchall()
    strata: list[tuple[str, int, int]] = []   # (label, available, sampled)
    if args.stratify:
        edges = [float(x) for x in args.stratify.split(",")]
        rng = random.Random(args.seed)
        picked = []
        for lo, hi in zip(edges, edges[1:]):
            bucket = [r for r in rows if lo <= r["best_ci"] < hi]
            take = bucket if len(bucket) <= args.per_bucket else rng.sample(
                bucket, args.per_bucket)
            strata.append((f"[{lo}, {hi})", len(bucket), len(take)))
            picked.extend(take)
        rows = sorted(picked, key=lambda r: r["best_ci"], reverse=True)
    elif args.limit:
        rows = rows[:args.limit]

    print(f"=== RE-EVAL COHORT: basket {label} ({bhash}) ===")
    print(f"  symbols: {target_basket}")
    print(f"  criteria: other-basket ci_lower > {args.ci_min}, n_oos >= {args.trades_min}"
          + (f", timeframe={args.timeframe}" if args.timeframe else ""))
    if args.note:
        print(f"  provenance: imported_from={args.note}")
    if strata:
        print(f"  stratified: per-bucket={args.per_bucket} seed={args.seed}")
        for lbl, avail, took in strata:
            print(f"    {lbl:<14} available={avail:<4} sampled={took}")
    print(f"  cohort size: {len(rows)}\n")
    if args.dry_run:
        for r in rows:
            print(f"  {r['strategy_hash'][:12]}  ci={r['best_ci']:.3f}  "
                  f"n={r['best_n']:<5} {r['name'][:34]:<34} {r['archetype']}")
        return 0

    # flush=True on the per-spec line: a 5m cohort runs minutes per spec, so an
    # unflushed run is indistinguishable from a hung one for hours.
    print(f"{'hash':<14}{'name':<34}{'old_ci':>8}{'new_ci':>8}{'delta':>8}"
          f"{'new_pf':>8}{'score':>8}{'n':>6}  verdict", flush=True)
    print("-" * 104, flush=True)
    survivors, failures = [], []
    deltas: list[float] = []
    by_ci: list[tuple[float, float]] = []   # (old_ci, delta) for per-bucket means
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
                strategy_hash=r["strategy_hash"], symbols=target_basket,
                imported_from=args.note,
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
        delta = fast.ci_lower - r["best_ci"]
        deltas.append(delta)
        by_ci.append((r["best_ci"], delta))
        print(f"{r['strategy_hash'][:12]:<14}{r['name'][:34]:<34}"
              f"{r['best_ci']:>8.3f}{fast.ci_lower:>8.3f}{delta:>+8.3f}"
              f"{fast.median_pf:>8.2f}"
              f"{fast.breakdown.score:>8.3f}{fast.n_oos_trades_total:>6}"
              f"  {'SURVIVED' if survived else 'screened-out'}", flush=True)

    print(f"\n--- SUMMARY ---")
    print(f"  re-evaluated : {len(survivors) + len(failures)}")
    print(f"  survived     : {len(survivors)}")
    print(f"  screened out : {len(failures)}")

    # Paired delta distribution. This is the probe's actual output when --basket
    # names a retired roster: per-spec ci_lower change, same spec, same data
    # window, only the symbol roster differing.
    if deltas:
        deltas_sorted = sorted(deltas)
        mean = sum(deltas) / len(deltas)
        mid = len(deltas_sorted) // 2
        median = (deltas_sorted[mid] if len(deltas_sorted) % 2
                  else (deltas_sorted[mid - 1] + deltas_sorted[mid]) / 2)
        print(f"\n--- PAIRED DELTA (new_ci - old_ci) ---")
        print(f"  n        : {len(deltas)}")
        print(f"  dropped  : {sum(1 for d in deltas if d < 0)}")
        print(f"  rose     : {sum(1 for d in deltas if d > 0)}")
        print(f"  mean     : {mean:+.3f}")
        print(f"  median   : {median:+.3f}")
        print(f"  min/max  : {deltas_sorted[0]:+.3f} / {deltas_sorted[-1]:+.3f}")

        # Mean delta per old-basket ci_lower bucket. The question this answers:
        # does the effect hold across the range, or only where the old basket
        # already scored high (which would make it regression to the mean)?
        edges = ([float(x) for x in args.stratify.split(",")]
                 if args.stratify else [0.0, 0.5, 0.7, 0.85, 1.0])
        print(f"\n  mean delta by old ci_lower bucket:")
        for lo, hi in zip(edges, edges[1:]):
            b = [d for c, d in by_ci if lo <= c < hi]
            if b:
                print(f"    [{lo}, {hi})   n={len(b):<3} mean={sum(b)/len(b):+.3f}"
                      f"  dropped={sum(1 for d in b if d < 0)}/{len(b)}")
            else:
                print(f"    [{lo}, {hi})   n=0   —")
    if survivors:
        print(f"\n  canonical candidates:")
        for r, f in survivors:
            print(f"    {r['strategy_hash'][:12]}  {r['name']}  "
                  f"ci={f.ci_lower:.3f} score={f.breakdown.score:.3f} n={f.n_oos_trades_total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
