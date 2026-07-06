"""Tests for the manual-strategy leaderboard path (Phase 4.5 prerequisite):
compute_manual_strategy_hash + record_manual_strategy, and that a manual
strategy's evaluation records against the row it creates.

Covers the two gaps that blocked scripts/evaluate.py from recording:
  1. hand-written Strategy subclasses have no StrategySpec → source-based hash
  2. record_evaluation needs a pre-existing strategies row → record_manual_strategy
"""

from __future__ import annotations

import hashlib
import json

import pytest

from generator.dedup import compute_manual_strategy_hash, compute_strategy_hash
from leaderboard.db import initialize_db
from leaderboard.models import EvaluationRecord, Status
from leaderboard.record import record_evaluation, record_manual_strategy


# ── Sample manual strategies (module-level so inspect.getsource works) ─────────


class SampleManual:
    archetype = "microstructure"
    timeframes = ["5m"]

    def entries(self):
        return "a distinctive body so the source hash is unique"


class SampleManualEdited:
    archetype = "microstructure"
    timeframes = ["5m"]

    def entries(self):
        return "a DIFFERENT body — an edit must change the identity hash"


class NoArchetype:
    timeframes = ["1d"]


class NoTimeframes:
    archetype = "momentum"


@pytest.fixture
def conn(tmp_path):
    c = initialize_db(tmp_path / "lb.db")
    yield c
    c.close()


# ── compute_manual_strategy_hash ──────────────────────────────────────────────


def test_manual_hash_is_stable_and_hex():
    h1 = compute_manual_strategy_hash(SampleManual)
    h2 = compute_manual_strategy_hash(SampleManual)
    assert h1 == h2
    assert len(h1) == 64
    int(h1, 16)  # valid hex, raises if not


def test_manual_hash_changes_when_source_changes():
    assert compute_manual_strategy_hash(SampleManual) != compute_manual_strategy_hash(
        SampleManualEdited
    )


def test_manual_hash_is_domain_separated():
    """The domain prefix must be mixed in, so a manual hash can never equal a
    bare sha256 of the same source — that's what keeps it disjoint from the
    spec-hash preimage space."""
    import inspect

    bare = hashlib.sha256(inspect.getsource(SampleManual).encode()).hexdigest()
    assert compute_manual_strategy_hash(SampleManual) != bare


def test_manual_hash_raises_without_source():
    """A dynamically-built class has no retrievable source → TypeError, not a
    fabricated identity."""
    dynamic = type("Dynamic", (), {"archetype": "x", "timeframes": ["1d"]})
    with pytest.raises(TypeError):
        compute_manual_strategy_hash(dynamic)


# ── record_manual_strategy ────────────────────────────────────────────────────


def _get_strategy(conn, h):
    return conn.execute(
        "SELECT * FROM strategies WHERE strategy_hash = ?", (h,)
    ).fetchone()


def test_record_manual_creates_strategy_and_generation_rows(conn):
    h = compute_manual_strategy_hash(SampleManual)
    gen_id = record_manual_strategy(conn, SampleManual, h)

    row = _get_strategy(conn, h)
    assert row is not None
    assert row["name"] == "SampleManual"
    assert row["archetype"] == "microstructure"
    assert row["timeframe"] == "5m"
    assert row["status"] == Status.GENERATED.value
    assert row["imported_from"] == "manual"
    assert row["generation_count"] == 1
    # spec_json is a manual marker, not a fabricated spec
    marker = json.loads(row["spec_json"])
    assert marker["manual"] is True
    assert marker["source_hash"] == h

    gen = conn.execute(
        "SELECT * FROM generations WHERE id = ?", (gen_id,)
    ).fetchone()
    assert gen["model_version"] == "manual"
    assert gen["prompt_hash"] == h
    assert gen["imported_from"] == "manual"


def test_record_manual_is_idempotent(conn):
    h = compute_manual_strategy_hash(SampleManual)
    record_manual_strategy(conn, SampleManual, h)
    record_manual_strategy(conn, SampleManual, h)

    rows = conn.execute(
        "SELECT generation_count FROM strategies WHERE strategy_hash = ?", (h,)
    ).fetchall()
    assert len(rows) == 1  # still one strategy
    assert rows[0]["generation_count"] == 2  # re-registration bumped the count
    # two generation events recorded
    n_gen = conn.execute(
        "SELECT COUNT(*) AS n FROM generations WHERE strategy_hash = ?", (h,)
    ).fetchone()["n"]
    assert n_gen == 2


def test_record_manual_requires_archetype(conn):
    with pytest.raises(AttributeError, match="archetype"):
        record_manual_strategy(conn, NoArchetype, "deadbeef")


def test_record_manual_requires_timeframes(conn):
    with pytest.raises(AttributeError, match="timeframes"):
        record_manual_strategy(conn, NoTimeframes, "deadbeef")


# ── The point of the whole exercise: eval records against the manual row ──────


def test_evaluation_records_against_manual_strategy(conn):
    h = compute_manual_strategy_hash(SampleManual)
    record_manual_strategy(conn, SampleManual, h)

    record = EvaluationRecord(
        n_oos_trades=30,
        promising=True,
        results_dir="/tmp/eval/manual",
        config_json="{}",
        median_pf=1.4,
        score=1.8,
    )
    # Would raise "strategy not found" (the pre-fix failure) if the row were absent.
    record_evaluation(conn, h, record, "canonical")

    row = _get_strategy(conn, h)
    assert row["status"] == Status.CANONICAL_EVALUATED.value
    n_eval = conn.execute(
        "SELECT COUNT(*) AS n FROM evaluations WHERE strategy_hash = ?", (h,)
    ).fetchone()["n"]
    assert n_eval == 1


def test_manual_hash_disjoint_from_spec_hash_domain(conn):
    """A manual hash and a spec hash for unrelated things simply differ; this
    documents that the two hashing functions coexist in one column without a
    format clash (both 64-hex, different preimage domains)."""
    manual_h = compute_manual_strategy_hash(SampleManual)
    assert len(manual_h) == 64
    # sanity: compute_strategy_hash still works and yields a same-width hash
    from generator.spec import StrategySpec, ParameterSpec, IndicatorSpec

    spec = StrategySpec(
        name="s",
        archetype="mean_reversion",
        thesis="A minimal spec used only to confirm hash width parity in tests.",
        supported_assets=["stocks"],
        timeframes=["1d"],
        parameters=[ParameterSpec(name="x", type="float", default=1.0)],
        indicators=[IndicatorSpec(name="rsi_14", type="rsi", params={"period": 14})],
        entry_long={
            "op": "compare", "operator": "<",
            "lhs": {"op": "indicator", "name": "rsi_14"},
            "rhs": {"op": "param", "name": "x"},
        },
    )
    assert len(compute_strategy_hash(spec)) == len(manual_h)
