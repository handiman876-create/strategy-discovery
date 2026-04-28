"""Robustness scoring + 'promising' classifier.

Per DESIGN.md §5.5:

  robustness_score = median_pf_across_symbols
                   * consistency_factor (1 / (1 + std_dev_of_pf))
                   * parameter_penalty (0.95 ** num_parameters)
                   * significance_factor (1.0 if p_value < 0.05 else 0.5)

A strategy is "promising" iff ALL of:
  * score > 1.5
  * median_pf > 1.2
  * CI_lower (5th percentile of bootstrapped per-symbol PFs) > 1.0
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass


@dataclass
class ScoreBreakdown:
    median_pf: float
    consistency_factor: float
    parameter_penalty: float
    significance_factor: float
    score: float


@dataclass
class FailedCondition:
    name: str
    required: str
    actual: float
    deficit: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PromiseVerdict:
    is_promising: bool
    failed_conditions: list[FailedCondition]
    breakdown: ScoreBreakdown

    def to_dict(self) -> dict:
        return {
            "is_promising": self.is_promising,
            "failed_conditions": [c.to_dict() for c in self.failed_conditions],
            "breakdown": asdict(self.breakdown),
        }


def compute_robustness_score(
    per_symbol_pfs: list[float],
    num_parameters: int,
    p_value: float,
) -> ScoreBreakdown:
    if not per_symbol_pfs:
        return ScoreBreakdown(0.0, 0.0, 0.0, 0.0, 0.0)
    median_pf = float(statistics.median(per_symbol_pfs))
    std_pf = float(statistics.pstdev(per_symbol_pfs)) if len(per_symbol_pfs) > 1 else 0.0
    consistency = 1.0 / (1.0 + std_pf)
    param_penalty = 0.95 ** max(num_parameters, 0)
    sig = 1.0 if p_value < 0.05 else 0.5
    score = median_pf * consistency * param_penalty * sig
    return ScoreBreakdown(
        median_pf=median_pf,
        consistency_factor=consistency,
        parameter_penalty=param_penalty,
        significance_factor=sig,
        score=score,
    )


def classify_promise(
    breakdown: ScoreBreakdown,
    ci_lower: float,
    *,
    score_threshold: float = 1.5,
    median_pf_threshold: float = 1.2,
    ci_lower_threshold: float = 1.0,
) -> PromiseVerdict:
    failed: list[FailedCondition] = []

    if breakdown.score <= score_threshold:
        failed.append(
            FailedCondition(
                name="score",
                required=f">{score_threshold}",
                actual=breakdown.score,
                deficit=score_threshold - breakdown.score,
            )
        )
    if breakdown.median_pf <= median_pf_threshold:
        failed.append(
            FailedCondition(
                name="median_pf",
                required=f">{median_pf_threshold}",
                actual=breakdown.median_pf,
                deficit=median_pf_threshold - breakdown.median_pf,
            )
        )
    if ci_lower <= ci_lower_threshold:
        failed.append(
            FailedCondition(
                name="ci_lower",
                required=f">{ci_lower_threshold}",
                actual=ci_lower,
                deficit=ci_lower_threshold - ci_lower,
            )
        )

    return PromiseVerdict(
        is_promising=not failed,
        failed_conditions=failed,
        breakdown=breakdown,
    )
