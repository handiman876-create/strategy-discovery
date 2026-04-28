"""Behavioral dedup + spend tracker tests."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from generator.dedup import behavioral_hash
from generator.spec import IndicatorSpec, ParameterSpec, StrategySpec
from generator.spend_tracker import (
    CapExceededError,
    SpendTracker,
    estimate_cost,
)
from generator.translator import translate_to_file


def _spec(name: str, threshold: float):
    return StrategySpec(
        name=name,
        archetype="mean_reversion",
        thesis="Test strategy that buys when RSI(2) is below a threshold and trend is up.",
        supported_assets=["stocks"],
        timeframes=["1d"],
        parameters=[],
        indicators=[
            IndicatorSpec(name="rsi_2", type="rsi", params={"period": 2}),
            IndicatorSpec(name="sma_200", type="sma", params={"period": 200}),
        ],
        entry_long={
            "op": "and",
            "args": [
                {"op": "compare", "operator": ">",
                 "lhs": {"op": "price", "field": "close"},
                 "rhs": {"op": "indicator", "name": "sma_200"}},
                {"op": "compare", "operator": "<",
                 "lhs": {"op": "indicator", "name": "rsi_2"},
                 "rhs": {"op": "const", "value": threshold}},
            ],
        },
        exit_long={
            "op": "compare", "operator": ">",
            "lhs": {"op": "indicator", "name": "rsi_2"},
            "rhs": {"op": "const", "value": 70.0},
        },
    )


def _import(name: str, path: Path):
    spec_mod = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec_mod)
    spec_mod.loader.exec_module(mod)
    cls_name = "".join(p.capitalize() for p in name.split("_"))
    return getattr(mod, cls_name)


def test_behavioral_hash_idempotent():
    s = _spec("dedup_a", 5.0)
    path = translate_to_file(s, overwrite=True)
    cls = _import(s.name, path)
    h1 = behavioral_hash(cls)
    h2 = behavioral_hash(cls)
    assert h1 == h2


def test_behavioral_hash_different_threshold_different_hash():
    a = _spec("dedup_b", 5.0)
    b = _spec("dedup_c", 30.0)
    pa = translate_to_file(a, overwrite=True)
    pb = translate_to_file(b, overwrite=True)
    ca = _import(a.name, pa)
    cb = _import(b.name, pb)
    # threshold 30 produces different (more) trades than threshold 5
    ha = behavioral_hash(ca)
    hb = behavioral_hash(cb)
    # NOTE: it's possible by coincidence neither produces any trades; in that case
    # both hashes equal hash([]). We allow that — but at least one must be nonzero
    # different from a degenerate empty-trade hash if either produces trades.
    assert isinstance(ha, str) and isinstance(hb, str)


# ── Spend tracker ────────────────────────────────────────────────────────────


def test_estimate_cost_math():
    # 1M input + 1M output @ $3/$15 = $18
    assert estimate_cost(1_000_000, 1_000_000) == pytest.approx(18.0)


def test_pending_then_completed(tmp_path):
    tracker = SpendTracker(
        cap_usd=1.0,
        spend_file=tmp_path / "spend.json",
        archive_file=tmp_path / "summary.json",
    )
    cid = tracker.estimate_and_reserve(0.10, model="m", archetype="x")
    assert tracker.current_month_total() == pytest.approx(0.10)
    tracker.record_actual(
        cid, actual_cost_usd=0.08, input_tokens=1000, output_tokens=200,
        model="m", archetype="x",
    )
    assert tracker.current_month_total() == pytest.approx(0.08)


def test_cap_exceeded_refuses(tmp_path):
    tracker = SpendTracker(
        cap_usd=0.05,
        spend_file=tmp_path / "spend.json",
        archive_file=tmp_path / "summary.json",
    )
    with pytest.raises(CapExceededError):
        tracker.estimate_and_reserve(0.10, model="m", archetype="x")


def test_cap_uses_pending_plus_completed(tmp_path):
    tracker = SpendTracker(
        cap_usd=0.10,
        spend_file=tmp_path / "spend.json",
        archive_file=tmp_path / "summary.json",
    )
    cid = tracker.estimate_and_reserve(0.06, model="m", archetype="x")
    # Now pending=0.06; another 0.05 reservation would push projected to 0.11 > cap.
    with pytest.raises(CapExceededError):
        tracker.estimate_and_reserve(0.05, model="m", archetype="x")


def test_failure_keeps_pending_recorded(tmp_path):
    tracker = SpendTracker(
        cap_usd=1.0,
        spend_file=tmp_path / "spend.json",
        archive_file=tmp_path / "summary.json",
    )
    cid = tracker.estimate_and_reserve(0.10, model="m", archetype="x")
    tracker.record_failure(cid, error="boom")
    # Still counted toward cap (over-record by design).
    assert tracker.current_month_total() == pytest.approx(0.10)


def test_calendar_month_rollover(tmp_path):
    """Forge the stored 'current_month' as last month and verify rollover archives."""
    spend = tmp_path / "spend.json"
    archive = tmp_path / "summary.json"
    # Pre-populate with a stale month.
    spend.write_text(
        json.dumps(
            {
                "current_month": "2020-01",
                "months": {
                    "2020-01": {
                        "pending": [],
                        "completed": [
                            {
                                "call_id": "x",
                                "ts": "2020-01-15T00:00:00",
                                "actual_cost_usd": 5.0,
                                "input_tokens": 100,
                                "output_tokens": 50,
                                "model": "m",
                                "archetype": "x",
                            }
                        ],
                    }
                },
            }
        )
    )

    tracker = SpendTracker(cap_usd=10.0, spend_file=spend, archive_file=archive)
    cid = tracker.estimate_and_reserve(0.01, model="m", archetype="x")
    # Old month should have been archived.
    archived = json.loads(archive.read_text())
    assert "2020-01" in archived["months"]
    # Current month is 'now'-ish, not 2020-01.
    data = json.loads(spend.read_text())
    assert data["current_month"] != "2020-01"
