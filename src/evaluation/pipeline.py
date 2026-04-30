"""End-to-end evaluation pipeline.

run_evaluation orchestrates:
  1. For each symbol → walk-forward → concat OOS trades.
  2. Per-symbol PF, bootstrap CI, random-baseline p-value.
  3. Aggregate: median PF, consistency factor (cross-symbol std), score.
  4. Classify "promising" or not, with per-condition deficit log.
  5. Write a structured JSON + CSV report to results/eval_<timestamp>/<strategy>/.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Type

import pandas as pd

from engine.backtester import BacktestConfig
from engine.portfolio import Trade
from strategy.base import Strategy

from .leaderboard_hook import record_evaluation_to_leaderboard
from .scoring import (
    PromiseVerdict,
    ScoreBreakdown,
    classify_promise,
    compute_robustness_score,
)
from .significance import (
    BaselineResult,
    BootstrapResult,
    bootstrap_profit_factor,
    profit_factor,
    random_baseline,
    trade_count_warning,
)
from .splits import train_test_load
from .walkforward import (
    WalkForwardConfig,
    WalkForwardResult,
    walk_forward,
)


@dataclass
class SymbolEvaluation:
    symbol: str
    n_oos_trades: int
    pf: float
    bootstrap: BootstrapResult
    baseline: BaselineResult
    walk_forward: WalkForwardResult
    warnings: list[str] = field(default_factory=list)


@dataclass
class EvaluationResult:
    strategy_name: str
    symbols: list[str]
    per_symbol: list[SymbolEvaluation]
    breakdown: ScoreBreakdown
    verdict: PromiseVerdict
    config: dict[str, Any]
    aggregate_p_value: float
    output_dir: Path | None = None


def run_evaluation(
    strategy_class: Type[Strategy],
    *,
    symbols: list[str],
    backtest_config: BacktestConfig,
    walk_config: WalkForwardConfig,
    n_bootstrap: int = 5000,
    bootstrap_seed: int = 0,
    m_baseline: int = 200,
    baseline_seed: int = 0,
    output_root: Path | None = None,
    strategy_factory: Callable[..., Strategy] | None = None,
    conn: Any = None,
    strategy_hash: str | None = None,
) -> EvaluationResult:
    """Run the full evaluation harness for a strategy across `symbols`.

    conn / strategy_hash: optional leaderboard-DB connection and the
    strategy's behavioral hash. When both are set, the result is recorded
    via record_evaluation(eval_type='canonical') before returning.
    Failures are logged at WARNING and swallowed — see
    leaderboard_hook.record_evaluation_to_leaderboard for the policy."""
    strategy_factory = strategy_factory or strategy_class
    strategy_name = strategy_class.__name__

    # Derive the bar timeframe from the strategy's declared timeframes.
    # Phase 3 requires single-timeframe strategies (Fix #2 enforces this in
    # the spec validator); existing in-tree generated strategies pre-date
    # that check, so we still defensively assert here.
    declared = list(getattr(strategy_class, "timeframes", None) or [])
    if len(declared) != 1:
        raise ValueError(
            f"{strategy_name}: Phase 3 supports a single declared timeframe per "
            f"strategy; got timeframes={declared}. Multi-timeframe support is "
            f"deferred to Phase 4."
        )
    bar_timeframe = declared[0]

    # Stamp the resolved timeframe onto the per-symbol BacktestConfig used in
    # this run so the backtester gates session-reset correctly. We don't
    # mutate the caller's config; we replace bar_timeframe via dataclass repl.
    from dataclasses import replace as _dc_replace

    backtest_config = _dc_replace(backtest_config, bar_timeframe=bar_timeframe)

    per_symbol: list[SymbolEvaluation] = []
    for sym in symbols:
        bars = train_test_load(sym, target_timeframe=bar_timeframe)
        wf = walk_forward(sym, bars, strategy_factory, backtest_config, walk_config)
        oos_trades = wf.all_oos_trades

        boot = bootstrap_profit_factor(
            oos_trades, n_resamples=n_bootstrap, seed=bootstrap_seed
        )
        baseline = random_baseline(
            bars,
            oos_trades,
            rr_ratio=_extract_rr(strategy_factory),
            m_trials=m_baseline,
            seed=baseline_seed,
            slippage=backtest_config.slippage,
            realistic_fills=backtest_config.realistic_fills,
        )
        warnings = []
        warn = trade_count_warning(len(oos_trades))
        if warn:
            warnings.append(warn)

        per_symbol.append(
            SymbolEvaluation(
                symbol=sym,
                n_oos_trades=len(oos_trades),
                pf=profit_factor([t.pnl for t in oos_trades]),
                bootstrap=boot,
                baseline=baseline,
                walk_forward=wf,
                warnings=warnings,
            )
        )

    # Aggregate metrics for scoring.
    n_oos_total = sum(s.n_oos_trades for s in per_symbol)
    pfs = [s.pf for s in per_symbol if s.n_oos_trades > 0]
    if not pfs:
        # No symbol produced any trades → trivially not promising.
        breakdown = ScoreBreakdown(0.0, 0.0, 0.0, 0.0, 0.0)
        verdict = classify_promise(breakdown, ci_lower=0.0, n_oos_trades_total=n_oos_total)
        agg_p = 1.0
    else:
        # Significance: combine per-symbol p-values via Fisher's method
        # → just take the median for simplicity in Phase 2.
        ps = [s.baseline.p_value for s in per_symbol if s.n_oos_trades > 0]
        agg_p = float(pd.Series(ps).median()) if ps else 1.0
        num_parameters = _count_grid_dims(walk_config.parameter_grid)
        breakdown = compute_robustness_score(pfs, num_parameters, agg_p)

        # Aggregate CI: median of per-symbol CI_lower values (conservative).
        ci_lows = [s.bootstrap.ci_lower for s in per_symbol if s.n_oos_trades > 0]
        ci_lower_agg = float(pd.Series(ci_lows).median()) if ci_lows else 0.0
        verdict = classify_promise(
            breakdown, ci_lower=ci_lower_agg, n_oos_trades_total=n_oos_total
        )

    cfg_dict = {
        "strategy": strategy_name,
        "symbols": symbols,
        "walk_config": dataclasses.asdict(walk_config),
        "backtest_config": _backtest_cfg_to_dict(backtest_config),
        "n_bootstrap": n_bootstrap,
        "m_baseline": m_baseline,
    }

    result = EvaluationResult(
        strategy_name=strategy_name,
        symbols=symbols,
        per_symbol=per_symbol,
        breakdown=breakdown,
        verdict=verdict,
        config=cfg_dict,
        aggregate_p_value=agg_p,
    )

    if output_root is not None:
        result.output_dir = _write_report(result, output_root)

    record_evaluation_to_leaderboard(
        pipeline_result=result,
        conn=conn,
        strategy_hash=strategy_hash,
        eval_type="canonical",
    )

    return result


# ── helpers ──────────────────────────────────────────────────────────────────


def _extract_rr(strategy_factory: Callable) -> float:
    try:
        s = strategy_factory()
        return float(getattr(s, "rr_ratio", 2.0))
    except Exception:
        return 2.0


def _count_grid_dims(grid: dict[str, list[Any]] | None) -> int:
    return len(grid) if grid else 0


def _backtest_cfg_to_dict(cfg: BacktestConfig) -> dict:
    return {
        "starting_capital": cfg.starting_capital,
        "commission": cfg.commission,
        "slippage": cfg.slippage,
        "realistic_fills": cfg.realistic_fills,
        "session_class": type(cfg.session).__name__,
    }


def _write_report(result: EvaluationResult, output_root: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = output_root / f"eval_{stamp}" / result.strategy_name
    out_dir.mkdir(parents=True, exist_ok=True)

    summary = {
        "strategy": result.strategy_name,
        "symbols": result.symbols,
        "verdict": result.verdict.to_dict(),
        "aggregate_p_value": result.aggregate_p_value,
        "per_symbol": [
            {
                "symbol": s.symbol,
                "n_oos_trades": s.n_oos_trades,
                "pf": s.pf,
                "bootstrap_ci_lower": s.bootstrap.ci_lower,
                "bootstrap_ci_upper": s.bootstrap.ci_upper,
                "bootstrap_fraction_capped": s.bootstrap.fraction_capped,
                "baseline_p_value": s.baseline.p_value,
                "baseline_median_pf": s.baseline.median_baseline_pf,
                "warnings": s.warnings,
            }
            for s in result.per_symbol
        ],
        "config": result.config,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    # Per-symbol metrics CSV
    pd.DataFrame(summary["per_symbol"]).to_csv(out_dir / "per_symbol.csv", index=False)

    # Walk-forward windows CSV
    wf_rows = []
    for s in result.per_symbol:
        for w in s.walk_forward.windows:
            wf_rows.append(
                {
                    "symbol": s.symbol,
                    "train_start": w.train_start,
                    "train_end": w.train_end_exclusive,
                    "test_start": w.test_start,
                    "test_end": w.test_end_exclusive,
                    "best_params": json.dumps(w.best_params, default=str),
                    "train_pf": w.train_pf,
                    "train_n_trades": w.train_n_trades,
                    "test_pf": w.test_pf,
                    "test_n_trades": w.test_n_trades,
                }
            )
    pd.DataFrame(wf_rows).to_csv(out_dir / "walkforward.csv", index=False)

    # Baseline distribution CSV
    base_rows = []
    for s in result.per_symbol:
        for i, pf in enumerate(s.baseline.baseline_pfs):
            base_rows.append({"symbol": s.symbol, "trial": i, "baseline_pf": pf})
    pd.DataFrame(base_rows).to_csv(out_dir / "baseline_distribution.csv", index=False)

    return out_dir
