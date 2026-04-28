"""Splits + holdout-enforcement tests."""

from __future__ import annotations

import pytest

from evaluation.splits import (
    HoldoutAccessError,
    holdout_load,
    is_in_optimization_mode,
    optimization_mode,
    train_test_load,
)


def test_optimization_mode_default_false():
    assert not is_in_optimization_mode()


def test_optimization_mode_sets_and_resets():
    assert not is_in_optimization_mode()
    with optimization_mode():
        assert is_in_optimization_mode()
    assert not is_in_optimization_mode()


def test_holdout_load_without_final_scoring_raises():
    with pytest.raises(HoldoutAccessError, match="final_scoring=True"):
        holdout_load("AMD")


def test_holdout_load_inside_optimization_mode_raises():
    """The 'evil strategy' test: any code path that ends up calling
    holdout_load while walk-forward optimization is active must raise,
    even if final_scoring=True is naively passed."""
    with optimization_mode():
        with pytest.raises(HoldoutAccessError, match="optimization_mode"):
            holdout_load("AMD", final_scoring=True)


def test_train_test_load_smoke():
    df = train_test_load("AMD")
    assert not df.empty
    # Must NOT contain holdout dates.
    boundary = df["timestamp"].max()
    assert boundary.year < 2025, f"train_test_load returned rows in holdout: max={boundary}"


def test_holdout_load_works_with_final_scoring_outside_opt():
    df = holdout_load("AMD", final_scoring=True)
    assert not df.empty
    # Must NOT contain pre-2025 dates.
    earliest = df["timestamp"].min()
    assert earliest.year >= 2025, f"holdout_load returned pre-2025 rows: min={earliest}"


def test_evil_strategy_pattern():
    """Simulate a framework code path that is supposed to be in optimization
    mode but mistakenly tries to peek at holdout. The check must catch it
    even if the caller passes final_scoring=True."""
    leaked = []

    def evil_optimizer():
        with optimization_mode():
            # Suppose the developer accidentally writes this:
            try:
                leaked.append(holdout_load("AMD", final_scoring=True))
            except HoldoutAccessError:
                leaked.append("blocked")

    evil_optimizer()
    assert leaked == ["blocked"]
