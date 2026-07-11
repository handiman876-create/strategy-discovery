"""Fast evaluation path — strictly NON-CANONICAL.

This module exists for the Phase-3 demo and for "is this strategy obviously
broken?" sanity checks. Per the user's Q3 spec, the fast path is code-level
separated from the canonical evaluation: it returns a DISTINCT type
(`FastEvaluationResult`, not `EvaluationResult`) so downstream code (e.g.
leaderboard promotion) cannot accidentally accept fast results.

Fast path uses:
  * 5 symbols (not 10)
  * Trivial parameter "grid" (a single combo)
  * n_bootstrap=500, m_baseline=20

Real promotion decisions must use `evaluation.pipeline.run_evaluation()`.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Type

import pandas as pd

from engine.backtester import BacktestConfig
from strategy.base import Strategy

from .leaderboard_hook import record_evaluation_to_leaderboard
from .pipeline import run_evaluation
from .scoring import MIN_TRADES_FOR_PROMISING, PromiseVerdict, ScoreBreakdown
from .walkforward import WalkForwardConfig

logger = logging.getLogger(__name__)

FAST_LABEL = "FAST: NON-CANONICAL"

# Minimum OOS trades for a fast score to be trustworthy. Below this, profit
# factor is dominated by a handful of trades and the bootstrap CI is
# uninformative: a lucky 1-3 trade spec posts a capped PF=100 -> score=100 and
# tops a score ranking (observed 2026-07-06, when the three highest-scoring fast
# rows were all 1-3 trade artifacts). We floor such a spec's fast score to 0 so
# under-sampled candidates sink instead of leading.
# NOTE: this floors the *noise* only. Genuinely-sampled specs whose PF later
# collapses under the 10-symbol canonical walk-forward are canonical's job to
# reject, not this floor's.
#
# DECOUPLED from the canonical promise floor (scoring.MIN_TRADES_FOR_PROMISING,
# = 30) and raised to 50 (Fix 2b, 2026-07-11). CI width scales ~1/sqrt(n), so
# 30 trades across the 5 FAST_SYMBOLS (~6/symbol) is too thin to yield a stable
# ci_lower — the metric the fast screen now promotes on. 50 (~10/symbol) is a
# stricter sample-size bar at the fast tier; canonical keeps its own 30 floor.
FAST_MIN_TRADES = 50
FAST_SYMBOLS = ["AMD", "NFLX", "SPY", "QQQ", "NVDA"]


DIAGNOSE_BELOW_TRADES = 10  # if total OOS trades < this, run signal-frequency diag


@dataclass
class FastEvaluationResult:
    """Distinct type from EvaluationResult on purpose. Do not pass this to
    code that expects EvaluationResult — the type system enforces the rule
    that fast results are demo/sanity-check only."""

    is_fast: bool  # always True
    label: str  # always FAST_LABEL
    strategy_name: str
    symbols: list[str]
    median_pf: float
    n_oos_trades_total: int
    breakdown: ScoreBreakdown
    verdict: PromiseVerdict
    ci_lower: float  # aggregate bootstrap CI lower bound (fast screen's gate metric)
    config: dict
    output_dir: Path | None = None
    diagnostics: dict | None = None


def run_fast_evaluation(
    strategy_class: Type[Strategy],
    *,
    backtest_config: BacktestConfig,
    walk_config: WalkForwardConfig | None = None,
    output_root: Path | None = None,
    conn: Any = None,
    strategy_hash: str | None = None,
) -> FastEvaluationResult:
    """Fast/sanity evaluation. Always uses 5-symbol subset and small bootstrap/
    baseline. Returns FastEvaluationResult.

    conn / strategy_hash: optional leaderboard-DB connection and the
    strategy's behavioral hash. When both are set, the fast result is
    recorded via record_evaluation(eval_type='fast') before returning.
    The inner run_evaluation call is intentionally NOT given conn — the
    fast path records exactly one row (eval_type='fast'), not two."""
    if walk_config is None:
        walk_config = WalkForwardConfig(
            train_window_months=24,
            test_window_months=6,
            step_months=6,
            parameter_grid=None,  # no optimization
        )

    canonical = run_evaluation(
        strategy_class,
        symbols=FAST_SYMBOLS,
        backtest_config=backtest_config,
        walk_config=walk_config,
        n_bootstrap=500,
        m_baseline=20,
        output_root=None,  # we'll write our own scoped report
    )

    n_total = sum(s.n_oos_trades for s in canonical.per_symbol)

    # Trade floor: an under-sampled fast eval can post a capped/degenerate PF
    # (e.g. a single winning trade -> PF=100 -> score=100) and rank above
    # genuinely-sampled candidates. classify_promise has already marked it
    # not-promising via the same 30-trade gate; here we additionally floor the
    # score to 0 so the artifact can't lead a score ranking. Firing is logged
    # for observability (per the "every safety net is observable" norm).
    breakdown = canonical.breakdown
    if n_total < FAST_MIN_TRADES:
        logger.warning(
            "fast-screen trade floor fired for %s: n_oos=%d < %d; "
            "flooring score %.3f -> 0.0 (under-sampled, PF not trustworthy)",
            canonical.strategy_name, n_total, FAST_MIN_TRADES, breakdown.score,
        )
        breakdown = replace(breakdown, score=0.0)

    fast = FastEvaluationResult(
        is_fast=True,
        label=FAST_LABEL,
        strategy_name=canonical.strategy_name,
        symbols=canonical.symbols,
        median_pf=canonical.breakdown.median_pf,
        n_oos_trades_total=n_total,
        breakdown=breakdown,
        verdict=canonical.verdict,
        ci_lower=canonical.ci_lower,
        config=canonical.config,
    )

    if n_total < DIAGNOSE_BELOW_TRADES:
        fast.diagnostics = _run_signal_frequency_diag(strategy_class, canonical.symbols)

    if output_root is not None:
        fast.output_dir = _write_fast_report(fast, output_root, canonical)

    record_evaluation_to_leaderboard(
        pipeline_result=fast,
        conn=conn,
        strategy_hash=strategy_hash,
        eval_type="fast",
    )

    return fast


def _run_signal_frequency_diag(strategy_class: Type[Strategy], symbols: list[str]) -> dict:
    """Per-symbol signal-frequency diagnostic. Errors are captured per symbol
    so a single failure can't blank the whole report."""
    from .diagnostics import diagnose_signal_frequency

    out: dict = {
        "reason": f"n_oos_trades_total < {DIAGNOSE_BELOW_TRADES}",
        "per_symbol": {},
    }
    for sym in symbols:
        try:
            out["per_symbol"][sym] = diagnose_signal_frequency(strategy_class, sym)
        except Exception as e:
            out["per_symbol"][sym] = {"error": f"{type(e).__name__}: {e}"}
    return out


def _write_fast_report(
    fast: FastEvaluationResult, output_root: Path, canonical
) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = output_root / f"fast_eval_{stamp}" / fast.strategy_name
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "label": FAST_LABEL,
        "warning": (
            "These results are NOT canonical. They use a reduced symbol set "
            "(5/10), small bootstrap (n=500), and small baseline (m=20). "
            "Do NOT use for promotion or leaderboard inclusion. Run "
            "scripts/evaluate.py for the canonical pipeline."
        ),
        "is_fast": True,
        "strategy": fast.strategy_name,
        "symbols": fast.symbols,
        "median_pf": fast.median_pf,
        "n_oos_trades_total": fast.n_oos_trades_total,
        "breakdown": asdict(fast.breakdown),
        "verdict": fast.verdict.to_dict(),
        "config": fast.config,
        "diagnostics": fast.diagnostics,
    }
    import json
    (out_dir / "fast_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    return out_dir
