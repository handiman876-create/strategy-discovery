#!/usr/bin/env python3
"""Autonomous discovery loop: generate -> fast-screen -> conditional canonical.

For each generated candidate:
  1. generate_and_translate (records eval-less generation to the leaderboard)
  2. run_fast_evaluation (records eval_type='fast'; the new FAST_MIN_TRADES floor
     zeroes the score for under-sampled specs)
  3. IF fast n_oos_trades > TRADES_MIN AND fast score > SCORE_MIN:
        run_evaluation (canonical, 10-symbol; records eval_type='canonical')
        IF canonical promising -> record HIT and (default) STOP for a decision.

NEVER runs holdout — that gate requires explicit human approval.

Usage:
  autodiscover.py --n 18 [--score-min 1.5] [--trades-min 30]
                  [--cost-ceiling 1.00] [--no-stop-on-pass]
Prints structured CAND/HIT/DONE lines and writes a JSON summary to
--summary (default scratchpad). Safe to run in the background.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv

from engine.backtester import BacktestConfig
from engine.session import RegularTradingHours
from evaluation import WalkForwardConfig, run_evaluation
from evaluation.fast_pipeline import run_fast_evaluation
from evaluation.symbols import load_symbol_list
from generator.pipeline import generate_and_translate
from evaluation.baskets import FAST_BASKET, KNOWN_BASKETS, basket_identity
from leaderboard.db import initialize_db

ARCHETYPES = [
    "mean_reversion", "microstructure", "momentum",
    "overnight_session", "seasonality", "volatility_breakout",
]  # 'pairs' excluded — translator defers it (multi-symbol position mgmt).

# Weighted round-robin (Fix 2a, 2026-07-11). Discovery's only canonical pass to
# date is a high-frequency mean-reversion strategy; seasonality / microstructure /
# overnight_session are narrow-window, few-trade archetypes that repeatedly fail
# canonical with high PF but ci_lower < 1.0. Bias generation toward the families
# that produce separable edges. Weights are deterministic (no RNG) so runs stay
# reproducible.
ARCHETYPE_WEIGHTS = {
    "mean_reversion": 3,       # up   — only family that has passed canonical
    "momentum": 3,            # up
    "volatility_breakout": 2,  # mid  — unchanged
    "seasonality": 1,         # down — narrow-window, thin-edge
    "microstructure": 1,      # down
    "overnight_session": 1,   # down
}
# Expanded to a flat schedule so `i % len` draws in the weighted proportion.
_WEIGHTED_ARCHETYPES = [a for a, w in ARCHETYPE_WEIGHTS.items() for _ in range(w)]


def _class_name(spec_name: str) -> str:
    return "".join(p.capitalize() for p in spec_name.split("_"))


def _load_class(code_path, spec_name):
    m = importlib.util.spec_from_file_location(spec_name, code_path)
    mod = importlib.util.module_from_spec(m)
    m.loader.exec_module(mod)
    return getattr(mod, _class_name(spec_name))


def _cfg():
    return BacktestConfig(
        starting_capital=10_000, commission=0.0, slippage=0.01,
        realistic_fills=True, session=RegularTradingHours(),
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=18)
    ap.add_argument("--score-min", type=float, default=1.5,
                    help="Fast-screen promotion gate: the candidate's fast score "
                         "must exceed this. Mirrors the canonical score gate, so "
                         "a candidate the canonical tier would reject on score is "
                         "never promoted to it (Fix 3, 2026-07-17).")
    ap.add_argument("--ci-lower-min", type=float, default=1.0,
                    help="Fast-screen promotion gate: promote to canonical only "
                         "when the aggregate bootstrap CI lower bound exceeds this "
                         "(edge separable from breakeven).")
    ap.add_argument("--trades-min", type=int, default=50)
    ap.add_argument("--basket", default=None, choices=sorted(KNOWN_BASKETS),
                    help="Symbol basket for the fast screen. Defaults to "
                         "evaluation.baskets.FAST_BASKET (diverse8_v1). Every eval "
                         "row records the basket it ran, since ci_lower is only "
                         "comparable within one.")
    ap.add_argument("--cost-ceiling", type=float, default=1.00)
    ap.add_argument("--no-stop-on-pass", action="store_true")
    ap.add_argument("--fast-only", action="store_true",
                    help="Generation + fast eval only; never run the expensive "
                         "canonical stage (for unattended/nightly runs).")
    ap.add_argument("--summary",
                    default=str(_ROOT / "logs" / "autodiscover_summary.json"))
    args = ap.parse_args()

    load_dotenv(_ROOT / ".env", override=True)
    conn = initialize_db(str(_ROOT / "db" / "leaderboard.db"))
    canon_symbols = load_symbol_list(_ROOT / "data/symbol_lists/sp500_phase2_seed42.json")
    wf = WalkForwardConfig(train_window_months=24, test_window_months=6,
                           step_months=6, parameter_grid=None)

    fast_basket = KNOWN_BASKETS[args.basket] if args.basket else FAST_BASKET
    basket_label, basket_h = basket_identity(fast_basket)
    print(f"BASKET fast={basket_label} ({basket_h}) symbols={fast_basket}", flush=True)

    candidates, hits = [], []
    spent = 0.0
    stop_on_pass = not args.no_stop_on_pass

    def flush():
        # basket_version rides on the summary too, not just the DB rows: a
        # summary's ci_lower numbers are meaningless without knowing which
        # roster produced them, and the summary is what a human reads first.
        Path(args.summary).write_text(json.dumps(
            {"basket_version": basket_label, "basket_hash": basket_h,
             "fast_symbols": fast_basket,
             "candidates": candidates, "hits": hits,
             "spent_usd": round(spent, 4)}, indent=2, default=str))

    for i in range(args.n):
        if spent >= args.cost_ceiling:
            print(f"DONE reason=cost_ceiling spent=${spent:.4f}", flush=True)
            break
        arch = _WEIGHTED_ARCHETYPES[i % len(_WEIGHTED_ARCHETYPES)]
        rec = {"i": i, "archetype": arch}
        try:
            gen = generate_and_translate(arch, dedup=True, conn=conn)
        except Exception as e:
            rec.update(stage="generate", error=str(e)[:200]); candidates.append(rec); flush()
            print(f"CAND {i} {arch} GEN-ERROR {str(e)[:80]}", flush=True); continue
        cost = sum(l.actual_cost_usd for l in gen.logs)
        spent += cost
        if gen.spec is None:
            rec.update(stage="generate", failed=gen.failure_reason); candidates.append(rec); flush()
            print(f"CAND {i} {arch} GEN-FAIL {gen.failure_reason}", flush=True); continue

        h = gen.strategy_hash
        rec.update(name=gen.spec.name, hash=h[:12], timeframe=list(gen.spec.timeframes), cost=round(cost, 4))
        try:
            cls = _load_class(gen.code_path, gen.spec.name)
            fast = run_fast_evaluation(cls, backtest_config=_cfg(), conn=conn,
                                       strategy_hash=h, symbols=fast_basket)
        except Exception as e:
            rec.update(stage="fast", error=str(e)[:200]); candidates.append(rec); flush()
            print(f"CAND {i} {gen.spec.name} FAST-ERROR {str(e)[:80]}", flush=True); continue

        fscore, ftr = fast.breakdown.score, fast.n_oos_trades_total
        rec.update(fast_score=round(fscore, 3), fast_trades=ftr,
                   fast_pf=round(fast.median_pf, 3),
                   fast_ci_lower=round(fast.ci_lower, 3))
        # Promote on edge separability (ci_lower) AND a trade floor AND the
        # canonical score gate.
        #
        # Fix 1 (2026-07-11) dropped score from this gate on the reasoning that
        # ci_lower > 1.0 subsumes it. It does not: score and ci_lower measure
        # different things, and canonical still rejects on BOTH. The 2026-07-16
        # run promoted two candidates whose fast rows already recorded a score
        # failure (fdc88ceb54fd score=0.340, 01325c323e43 score=1.194, gate 1.5)
        # — the pipeline knew they would fail canonical and promoted them anyway.
        # fdc88ceb54fd went on to fail canonical on score exactly as its fast row
        # predicted. Screening on score here costs nothing and is strictly
        # information we already have (Fix 3, 2026-07-17).
        gate = (
            ftr >= args.trades_min
            and fast.ci_lower > args.ci_lower_min
            and fscore > args.score_min
        )
        print(f"CAND {i} {gen.spec.name} tf={list(gen.spec.timeframes)} "
              f"ci_lower={fast.ci_lower:.3f} fast_pf={fast.median_pf:.2f} "
              f"fast_score={fscore:.2f} trades={ftr} "
              f"{'-> CANONICAL' if gate else 'screened-out'}", flush=True)
        if not gate:
            rec.update(result="screened_out"); candidates.append(rec); flush(); continue

        if args.fast_only:
            rec.update(result="fast_pass_canonical_skipped")
            candidates.append(rec); flush()
            print(f"CAND {i} {gen.spec.name} FAST-PASS (canonical skipped: --fast-only)",
                  flush=True)
            continue

        try:
            canon = run_evaluation(cls, symbols=canon_symbols, backtest_config=_cfg(),
                                   walk_config=wf, output_root=_ROOT / "results",
                                   conn=conn, strategy_hash=h)
        except Exception as e:
            rec.update(stage="canonical", error=str(e)[:200]); candidates.append(rec); flush()
            print(f"CAND {i} {gen.spec.name} CANON-ERROR {str(e)[:80]}", flush=True); continue

        n_oos = sum(s.n_oos_trades for s in canon.per_symbol)
        promising = canon.verdict.is_promising
        rec.update(result="canonical_promising" if promising else "canonical_fail",
                   canon_score=round(canon.breakdown.score, 3),
                   canon_pf=round(canon.breakdown.median_pf, 3), canon_oos=n_oos,
                   canon_failed=[c.name for c in canon.verdict.failed_conditions])
        candidates.append(rec); flush()
        print(f"CAND {i} {gen.spec.name} CANONICAL score={canon.breakdown.score:.3f} "
              f"pf={canon.breakdown.median_pf:.3f} oos={n_oos} promising={promising}", flush=True)
        if promising:
            hits.append(rec); flush()
            print(f"HIT {gen.spec.name} hash={h[:12]} score={canon.breakdown.score:.3f} "
                  f"CANONICAL PASS", flush=True)
            if stop_on_pass:
                print(f"DONE reason=canonical_pass spent=${spent:.4f}", flush=True)
                conn.close(); return 0

    flush()
    print(f"DONE reason=batch_exhausted n={len(candidates)} hits={len(hits)} "
          f"spent=${spent:.4f}", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
