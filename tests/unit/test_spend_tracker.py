"""SpendTracker tests.

Renamed from test_dedup_and_spend.py during Phase 4 step 10. The two
behavioral_hash tests that used to live here were removed when
behavioral_hash() was deleted; the structural-hash equivalents live in
tests/unit/test_strategy_hash.py.
"""

from __future__ import annotations

import json

import pytest

from generator.spend_tracker import (
    CapExceededError,
    SpendTracker,
    estimate_cost,
)


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
