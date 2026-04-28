"""Scoring + classify_promise tests."""

from __future__ import annotations

import pytest

from evaluation.scoring import (
    ScoreBreakdown,
    classify_promise,
    compute_robustness_score,
)


def test_robustness_score_basic():
    # Median PF 1.5; std 0.1 → consistency = 1/(1+0.1) ≈ 0.909
    # 2 params → penalty 0.95**2 ≈ 0.9025
    # significant → 1.0
    bd = compute_robustness_score([1.4, 1.5, 1.6], num_parameters=2, p_value=0.01)
    assert bd.median_pf == pytest.approx(1.5)
    assert bd.consistency_factor == pytest.approx(1 / (1 + 0.0816497), rel=0.01)
    assert bd.parameter_penalty == pytest.approx(0.9025)
    assert bd.significance_factor == 1.0
    assert bd.score > 0


def test_robustness_score_insignificant_halves():
    bd = compute_robustness_score([1.5], num_parameters=0, p_value=0.5)
    assert bd.significance_factor == 0.5


def test_robustness_score_empty():
    bd = compute_robustness_score([], num_parameters=0, p_value=1.0)
    assert bd.score == 0.0


def test_classify_promise_passes_when_all_thresholds_met():
    bd = ScoreBreakdown(median_pf=1.4, consistency_factor=1.0, parameter_penalty=0.9, significance_factor=1.0, score=1.6)
    v = classify_promise(bd, ci_lower=1.1)
    assert v.is_promising
    assert v.failed_conditions == []


def test_classify_promise_fails_when_score_low():
    bd = ScoreBreakdown(median_pf=1.4, consistency_factor=1.0, parameter_penalty=0.9, significance_factor=1.0, score=1.0)
    v = classify_promise(bd, ci_lower=1.1)
    assert not v.is_promising
    assert any(c.name == "score" for c in v.failed_conditions)


def test_classify_promise_fails_when_median_pf_low():
    bd = ScoreBreakdown(median_pf=1.0, consistency_factor=1.0, parameter_penalty=1.0, significance_factor=1.0, score=2.0)
    v = classify_promise(bd, ci_lower=1.1)
    assert not v.is_promising
    assert any(c.name == "median_pf" for c in v.failed_conditions)


def test_classify_promise_fails_when_ci_lower_low():
    bd = ScoreBreakdown(median_pf=1.4, consistency_factor=1.0, parameter_penalty=1.0, significance_factor=1.0, score=2.0)
    v = classify_promise(bd, ci_lower=0.9)
    assert not v.is_promising
    assert any(c.name == "ci_lower" for c in v.failed_conditions)


def test_failed_condition_records_deficit():
    bd = ScoreBreakdown(median_pf=1.0, consistency_factor=1.0, parameter_penalty=1.0, significance_factor=1.0, score=1.0)
    v = classify_promise(bd, ci_lower=0.5)
    score_cond = [c for c in v.failed_conditions if c.name == "score"][0]
    assert score_cond.deficit == pytest.approx(0.5)
    median_cond = [c for c in v.failed_conditions if c.name == "median_pf"][0]
    assert median_cond.deficit == pytest.approx(0.2)
    ci_cond = [c for c in v.failed_conditions if c.name == "ci_lower"][0]
    assert ci_cond.deficit == pytest.approx(0.5)
