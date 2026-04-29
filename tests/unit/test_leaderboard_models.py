"""Tests pinning the Status enum, transition matrices, and dataclass defaults.

These are constant-shape tests; the actual transition behavior (record_evaluation
auto-advancing status, transition_status rejecting illegal moves) is exercised
by the record.py tests."""

from __future__ import annotations

from leaderboard.models import (
    EVAL_TYPE_AUTO_TRANSITIONS,
    LEGAL_MANUAL_TRANSITIONS,
    Evaluation,
    Generation,
    Status,
    Strategy,
)


def test_status_enum_values_match_schema_check_constraint():
    """Status enum must mirror the schema CHECK exactly. Drift here would
    silently accept a value at the dataclass level that the DB then rejects."""
    expected = {
        "generated",
        "fast_evaluated",
        "canonical_evaluated",
        "holdout_evaluated",
        "paper_candidate",
        "paper_trading",
        "paper_complete",
        "real_money_candidate",
        "archived",
    }
    assert {s.value for s in Status} == expected


def test_status_compares_equal_to_its_string_value():
    """The (str, Enum) hybrid is the whole reason for using `class Status(str, Enum)`
    — it lets row['status'] == Status.X work without conversion at every site."""
    assert Status.GENERATED == "generated"
    assert Status.PAPER_TRADING == "paper_trading"
    assert Status.ARCHIVED == "archived"


def test_eval_type_auto_transitions_cover_three_eval_types():
    assert set(EVAL_TYPE_AUTO_TRANSITIONS.keys()) == {"fast", "canonical", "holdout"}
    for eval_type, (target, predecessors) in EVAL_TYPE_AUTO_TRANSITIONS.items():
        assert isinstance(target, Status)
        assert all(isinstance(p, Status) for p in predecessors)
        # The target itself must not appear in its own predecessor set —
        # that would be a self-loop and obscure whether the transition fired.
        assert target not in predecessors


def test_legal_manual_transitions_cover_every_status():
    """Every Status must appear as a key (so transition_status doesn't have
    to handle missing-key as a special case). ARCHIVED is the only terminal
    state."""
    assert set(LEGAL_MANUAL_TRANSITIONS.keys()) == set(Status)
    assert LEGAL_MANUAL_TRANSITIONS[Status.ARCHIVED] == frozenset()
    for state, successors in LEGAL_MANUAL_TRANSITIONS.items():
        if state is Status.ARCHIVED:
            continue
        assert Status.ARCHIVED in successors, (
            f"{state} must be archivable; manual archive is the universal escape hatch"
        )


def test_dataclass_defaults_are_sensible():
    """Smoke test: default-construct each model with required fields only."""
    s = Strategy(
        behavioral_hash="h1",
        name="n",
        archetype="mean_reversion",
        timeframe="1d",
        spec_json="{}",
        first_generated_at="2026-04-29T00:00:00",
        last_seen_at="2026-04-29T00:00:00",
    )
    assert s.status == Status.GENERATED
    assert s.generation_count == 1
    assert s.imported_from is None

    g = Generation(
        id=None,
        strategy_hash="h1",
        generated_at="2026-04-29T00:00:00",
        archetype="mean_reversion",
        model_version="claude-sonnet-4-6",
        prompt_hash="abcd",
    )
    assert g.retry_count == 0
    assert g.requested_timeframe is None
    assert g.stringification_firings == 0

    e = Evaluation(
        id=None,
        strategy_hash="h1",
        eval_type="fast",
        evaluated_at="2026-04-29T00:00:00",
        n_oos_trades=10,
        promising=True,
        results_dir="/tmp",
        config_json="{}",
    )
    assert e.duration_seconds is None
    assert e.failed_gates is None
