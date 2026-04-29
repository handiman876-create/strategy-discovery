"""StrategySpec / DSL validation tests."""

from __future__ import annotations

import pytest

from generator.spec import (
    IndicatorSpec,
    ParameterSpec,
    StrategySpec,
)


VALID_THESIS = "Buy oversold pullbacks within an established uptrend; revert in 1-3 days."


def _valid_spec_kwargs(**overrides):
    base = dict(
        name="rsi_2_dip",
        archetype="mean_reversion",
        thesis=VALID_THESIS,
        supported_assets=["stocks"],
        timeframes=["1d"],
        parameters=[],
        indicators=[
            IndicatorSpec(name="rsi_2", type="rsi", params={"period": 2}),
        ],
        entry_long={
            "op": "compare",
            "operator": "<",
            "lhs": {"op": "indicator", "name": "rsi_2"},
            "rhs": {"op": "const", "value": 5.0},
        },
    )
    base.update(overrides)
    return base


def test_valid_spec_parses():
    spec = StrategySpec(**_valid_spec_kwargs())
    assert spec.name == "rsi_2_dip"
    assert spec.archetype == "mean_reversion"


def test_name_must_be_snake_case():
    with pytest.raises(ValueError, match="snake_case"):
        StrategySpec(**_valid_spec_kwargs(name="RSI2Dip"))


def test_indicator_type_must_be_in_allowed_set():
    with pytest.raises(ValueError, match="not in allowed set"):
        StrategySpec(
            **_valid_spec_kwargs(
                indicators=[IndicatorSpec(name="x", type="foobar", params={})]
            )
        )


def test_no_entry_signal_raises():
    with pytest.raises(ValueError, match="at least one of"):
        StrategySpec(**_valid_spec_kwargs(entry_long=None, entry_short=None))


def test_multi_timeframe_rejected_in_phase_3():
    """Fix #2 — Phase 3 supports a single declared timeframe per spec.
    Multi-timeframe support is deferred to Phase 4 (a strategy
    subscribing to multiple bar streams needs engine-level dispatch
    that doesn't exist yet). The validator catches this early so the
    generator can never emit a multi-timeframe spec into the pipeline."""
    with pytest.raises(ValueError, match="single declared timeframe"):
        StrategySpec(**_valid_spec_kwargs(timeframes=["5m", "1d"]))


def test_empty_timeframes_rejected():
    """A spec with no timeframes is also invalid — len != 1. Pydantic's
    min_length=1 on the field would catch this first, but the explicit
    check in _validate makes the failure mode unambiguous if either
    constraint is ever relaxed."""
    with pytest.raises(ValueError):
        StrategySpec(**_valid_spec_kwargs(timeframes=[]))


def test_single_timeframe_accepted():
    """Positive control: a single-timeframe spec passes validation."""
    spec = StrategySpec(**_valid_spec_kwargs(timeframes=["1d"]))
    assert spec.timeframes == ["1d"]


def test_indicator_alias_uniqueness():
    with pytest.raises(ValueError, match="unique"):
        StrategySpec(
            **_valid_spec_kwargs(
                indicators=[
                    IndicatorSpec(name="x", type="rsi", params={"period": 2}),
                    IndicatorSpec(name="x", type="sma", params={"period": 50}),
                ],
                entry_long={
                    "op": "compare", "operator": ">",
                    "lhs": {"op": "indicator", "name": "x"},
                    "rhs": {"op": "const", "value": 0.0},
                },
            )
        )


def test_indicator_ref_must_resolve():
    with pytest.raises(ValueError, match="not declared in indicators"):
        StrategySpec(
            **_valid_spec_kwargs(
                entry_long={
                    "op": "compare", "operator": ">",
                    "lhs": {"op": "indicator", "name": "ghost"},
                    "rhs": {"op": "const", "value": 0.0},
                },
            )
        )


def test_param_ref_must_resolve():
    with pytest.raises(ValueError, match="not declared in parameters"):
        StrategySpec(
            **_valid_spec_kwargs(
                parameters=[ParameterSpec(name="p1", type="int", default=5)],
                entry_long={
                    "op": "compare", "operator": ">",
                    "lhs": {"op": "indicator", "name": "rsi_2"},
                    "rhs": {"op": "param", "name": "ghost"},
                },
            )
        )


def test_max_parameters_enforced():
    with pytest.raises(ValueError):
        StrategySpec(
            **_valid_spec_kwargs(
                parameters=[
                    ParameterSpec(name=f"p{i}", type="int", default=1) for i in range(6)
                ]
            )
        )


def test_max_indicators_enforced():
    with pytest.raises(ValueError):
        StrategySpec(
            **_valid_spec_kwargs(
                indicators=[
                    IndicatorSpec(name=f"i{i}", type="sma", params={"period": 10 + i})
                    for i in range(5)
                ]
            )
        )


def test_daily_return_on_intraday_rejected():
    with pytest.raises(ValueError, match="daily-only"):
        StrategySpec(
            **_valid_spec_kwargs(
                timeframes=["5m"],
                indicators=[IndicatorSpec(name="dr", type="daily_return", params={})],
                entry_long={
                    "op": "compare", "operator": ">",
                    "lhs": {"op": "indicator", "name": "dr"},
                    "rhs": {"op": "const", "value": 0.0},
                },
            )
        )


def test_daily_return_on_daily_ok():
    spec = StrategySpec(
        **_valid_spec_kwargs(
            timeframes=["1d"],
            indicators=[IndicatorSpec(name="dr", type="daily_return", params={})],
            entry_long={
                "op": "compare", "operator": ">",
                "lhs": {"op": "indicator", "name": "dr"},
                "rhs": {"op": "const", "value": 0.0},
            },
        )
    )
    assert spec.indicators[0].type == "daily_return"


def test_tool_input_schema_emits():
    schema = StrategySpec.tool_input_schema()
    assert schema["type"] == "object"
    assert "name" in schema["properties"]
    assert "indicators" in schema["properties"]


def test_position_sizing_default():
    spec = StrategySpec(**_valid_spec_kwargs())
    assert spec.position_sizing.rule == "fixed"
    assert spec.position_sizing.size == 1
