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

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Type

import pandas as pd

from engine.backtester import BacktestConfig
from strategy.base import Strategy

from .pipeline import run_evaluation
from .scoring import PromiseVerdict, ScoreBreakdown
from .walkforward import WalkForwardConfig

FAST_LABEL = "FAST: NON-CANONICAL"
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
    strategy's behavioral hash. Phase 4 step 8b plumbing only — accepted
    but not yet used. Step 8d calls record_evaluation when both are set.
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

    fast = FastEvaluationResult(
        is_fast=True,
        label=FAST_LABEL,
        strategy_name=canonical.strategy_name,
        symbols=canonical.symbols,
        median_pf=canonical.breakdown.median_pf,
        n_oos_trades_total=n_total,
        breakdown=canonical.breakdown,
        verdict=canonical.verdict,
        config=canonical.config,
    )

    if n_total < DIAGNOSE_BELOW_TRADES:
        fast.diagnostics = _run_signal_frequency_diag(strategy_class, canonical.symbols)

    if output_root is not None:
        fast.output_dir = _write_fast_report(fast, output_root, canonical)

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
