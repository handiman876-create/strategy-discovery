"""Tests for leaderboard.adapters: pipeline → write-payload conversions.

We don't import GenerationLog / FailedCondition / EvaluationResult from
across the codebase here — the adapters duck-type on attribute access,
so minimal local fakes keep these tests fast and independent of changes
to those upstream dataclasses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from leaderboard.adapters import (
    _parse_iso_to_utc,
    to_evaluation_record,
    to_generation_metadata,
)


# ── Fakes ────────────────────────────────────────────────────────────────────


@dataclass
class _FakeLog:
    timestamp: str
    model: str = "claude-sonnet-4-6"
    prompt_hash: str = "ph_001"
    actual_cost_usd: float = 0.05
    raw_response_path: str | None = None


@dataclass
class _FakeFailed:
    name: str
    required: str
    actual: float
    deficit: float

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "required": self.required,
            "actual": self.actual,
            "deficit": self.deficit,
        }


@dataclass
class _FakeBreakdown:
    median_pf: float = 1.5
    consistency_factor: float = 0.7
    parameter_penalty: float = 0.95
    significance_factor: float = 1.0
    score: float = 2.0


@dataclass
class _FakeVerdict:
    is_promising: bool = True
    failed_conditions: list[Any] = field(default_factory=list)


@dataclass
class _FakePerSymbol:
    n_oos_trades: int


@dataclass
class _FakeCanonical:
    breakdown: _FakeBreakdown = field(default_factory=_FakeBreakdown)
    verdict: _FakeVerdict = field(default_factory=_FakeVerdict)
    config: dict = field(default_factory=lambda: {"k": "v"})
    output_dir: Any = None
    per_symbol: list[_FakePerSymbol] = field(default_factory=list)


@dataclass
class _FakeFast:
    is_fast: bool = True
    breakdown: _FakeBreakdown = field(default_factory=_FakeBreakdown)
    verdict: _FakeVerdict = field(default_factory=_FakeVerdict)
    config: dict = field(default_factory=lambda: {"k": "v"})
    output_dir: Any = None
    n_oos_trades_total: int = 50


# ── _parse_iso_to_utc ────────────────────────────────────────────────────────


def test_parse_iso_to_utc_aware_passes_through():
    dt = _parse_iso_to_utc("2026-04-30T12:00:00+00:00")
    assert dt == datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)


def test_parse_iso_to_utc_naive_treated_as_utc():
    dt = _parse_iso_to_utc("2026-04-30T12:00:00")
    assert dt.tzinfo is timezone.utc
    assert dt == datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc)


# ── to_generation_metadata ───────────────────────────────────────────────────


def test_to_generation_metadata_single_log():
    log = _FakeLog(
        timestamp="2026-04-30T12:00:00+00:00",
        actual_cost_usd=0.10,
        raw_response_path="/tmp/raw.json",
    )
    md = to_generation_metadata(
        [log],
        archetype="momentum",
        spec_path="/tmp/strategies/foo.py",
        now_iso="2026-04-30T12:00:30+00:00",
    )
    assert md.archetype == "momentum"
    assert md.model_version == "claude-sonnet-4-6"
    assert md.prompt_hash == "ph_001"
    assert md.cost_usd == pytest.approx(0.10)
    assert md.retry_count == 1
    assert md.duration_seconds == pytest.approx(30.0)
    assert md.raw_response_path == "/tmp/raw.json"
    assert md.spec_path == "/tmp/strategies/foo.py"
    assert md.requested_timeframe is None
    assert md.generated_at == "2026-04-30T12:00:00+00:00"
    assert md.stringification_firings == 0
    assert md.kwarg_validator_firings == 0
    assert md.unreachable_default_firings == 0


def test_to_generation_metadata_aggregates_across_attempts():
    logs = [
        _FakeLog(
            timestamp="2026-04-30T12:00:00+00:00",
            actual_cost_usd=0.05,
            raw_response_path="/tmp/a.json",
        ),
        _FakeLog(
            timestamp="2026-04-30T12:00:10+00:00",
            actual_cost_usd=0.06,
            raw_response_path="/tmp/b.json",
        ),
        _FakeLog(
            timestamp="2026-04-30T12:00:20+00:00",
            actual_cost_usd=0.04,
            raw_response_path="/tmp/c.json",
        ),
    ]
    md = to_generation_metadata(
        logs,
        archetype="trend_following",
        now_iso="2026-04-30T12:00:30+00:00",
    )
    assert md.cost_usd == pytest.approx(0.15)
    assert md.retry_count == 3
    assert md.generated_at == "2026-04-30T12:00:00+00:00"  # first log
    assert md.raw_response_path == "/tmp/c.json"  # last log
    assert md.duration_seconds == pytest.approx(30.0)


def test_to_generation_metadata_naive_timestamps_normalized():
    logs = [_FakeLog(timestamp="2026-04-30T12:00:00", actual_cost_usd=0.01)]
    md = to_generation_metadata(
        logs, archetype="x", now_iso="2026-04-30T12:00:05"
    )
    assert md.duration_seconds == pytest.approx(5.0)


def test_to_generation_metadata_passes_requested_timeframe():
    logs = [_FakeLog(timestamp="2026-04-30T12:00:00+00:00")]
    md = to_generation_metadata(
        logs,
        archetype="x",
        requested_timeframe="15m",
        now_iso="2026-04-30T12:00:01+00:00",
    )
    assert md.requested_timeframe == "15m"


def test_to_generation_metadata_missing_raw_response_path_defaults_none():
    @dataclass
    class _LogNoPath:
        timestamp: str = "2026-04-30T12:00:00+00:00"
        model: str = "m"
        prompt_hash: str = "h"
        actual_cost_usd: float = 0.0

    md = to_generation_metadata(
        [_LogNoPath()], archetype="x", now_iso="2026-04-30T12:00:01+00:00"
    )
    assert md.raw_response_path is None


def test_to_generation_metadata_empty_logs_raises():
    with pytest.raises(ValueError, match="at least one log"):
        to_generation_metadata([], archetype="x")


# ── to_evaluation_record ─────────────────────────────────────────────────────


def test_to_evaluation_record_from_canonical():
    res = _FakeCanonical(
        per_symbol=[
            _FakePerSymbol(n_oos_trades=20),
            _FakePerSymbol(n_oos_trades=22),
        ],
        verdict=_FakeVerdict(is_promising=True, failed_conditions=[]),
        output_dir="/tmp/results/eval_x",
    )
    rec = to_evaluation_record(res, eval_type="canonical")
    assert rec.n_oos_trades == 42
    assert rec.promising is True
    assert rec.median_pf == 1.5
    assert rec.score == 2.0
    assert rec.results_dir == "/tmp/results/eval_x"
    assert rec.failed_conditions == []
    assert json.loads(rec.config_json) == {"k": "v"}


def test_to_evaluation_record_from_fast():
    res = _FakeFast(n_oos_trades_total=33, output_dir="/tmp/fast")
    rec = to_evaluation_record(res, eval_type="fast")
    assert rec.n_oos_trades == 33
    assert rec.results_dir == "/tmp/fast"
    assert rec.median_pf == 1.5  # from breakdown


def test_to_evaluation_record_failed_conditions_converted_to_dicts():
    res = _FakeCanonical(
        verdict=_FakeVerdict(
            is_promising=False,
            failed_conditions=[
                _FakeFailed("median_pf", ">1.2", 1.0, 0.2),
                _FakeFailed("score", ">1.5", 0.8, 0.7),
            ],
        ),
        per_symbol=[_FakePerSymbol(n_oos_trades=5)],
    )
    rec = to_evaluation_record(res, eval_type="canonical")
    assert rec.promising is False
    assert rec.failed_conditions == [
        {"name": "median_pf", "required": ">1.2", "actual": 1.0, "deficit": 0.2},
        {"name": "score", "required": ">1.5", "actual": 0.8, "deficit": 0.7},
    ]


def test_to_evaluation_record_no_output_dir_emits_empty_string():
    res = _FakeCanonical(
        per_symbol=[_FakePerSymbol(n_oos_trades=10)], output_dir=None
    )
    rec = to_evaluation_record(res, eval_type="canonical")
    assert rec.results_dir == ""


def test_to_evaluation_record_explicit_config_json_overrides_default():
    res = _FakeCanonical(
        per_symbol=[_FakePerSymbol(n_oos_trades=10)],
        config={"will": "be ignored"},
    )
    rec = to_evaluation_record(
        res, eval_type="canonical", config_json='{"explicit": true}'
    )
    assert rec.config_json == '{"explicit": true}'
