"""Translator emits compilable code that runs on the fixture."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from engine.backtester import BacktestConfig, run_backtest
from engine.session import RegularTradingHours
from generator.spec import IndicatorSpec, ParameterSpec, StrategySpec
from generator.translator import (
    GENERATED_DIR,
    TranslationError,
    scan_unreachable_defaults,
    translate_to_file,
)


def _import(name: str, path: Path):
    spec_mod = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec_mod)
    spec_mod.loader.exec_module(mod)
    cls_name = "".join(p.capitalize() for p in name.split("_"))
    return getattr(mod, cls_name)


def _mean_reversion_spec():
    return StrategySpec(
        name="test_rsi_dip",
        archetype="mean_reversion",
        thesis="Buy oversold dips in uptrending stocks; mean revert in 1-3 days.",
        supported_assets=["stocks"],
        timeframes=["1d"],
        parameters=[
            ParameterSpec(name="rsi_threshold", type="float", default=5.0),
        ],
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
                 "rhs": {"op": "param", "name": "rsi_threshold"}},
            ],
        },
        exit_long={
            "op": "compare", "operator": ">",
            "lhs": {"op": "indicator", "name": "rsi_2"},
            "rhs": {"op": "const", "value": 70.0},
        },
    )


def test_translate_compiles(tmp_path):
    spec = _mean_reversion_spec()
    path = translate_to_file(spec, overwrite=True)
    assert path.exists()
    cls = _import(spec.name, path)
    assert cls.archetype == "mean_reversion"
    assert "stocks" in cls.supported_assets


def test_translate_runs_on_fixture():
    from generator.fixture import fixture_1d
    spec = _mean_reversion_spec()
    path = translate_to_file(spec, overwrite=True)
    cls = _import(spec.name, path)

    bars = fixture_1d()
    cfg = BacktestConfig(starting_capital=10_000, slippage=0.01, session=RegularTradingHours())
    result = run_backtest("AMD", bars, cls(), cfg)
    # The fixture is small; the strategy may make 0 trades — what matters is
    # that the run completes without exceptions.
    assert isinstance(result.trades, list)


def test_pairs_archetype_rejected_by_translator():
    spec = _mean_reversion_spec()
    spec_dict = spec.model_dump()
    spec_dict["archetype"] = "pairs"
    spec_dict["name"] = "pairs_demo"
    bad_spec = StrategySpec(**spec_dict)
    with pytest.raises(TranslationError, match="pairs archetype"):
        translate_to_file(bad_spec, overwrite=True)


def test_archetype_asset_compatibility_enforced():
    spec_dict = _mean_reversion_spec().model_dump()
    spec_dict["archetype"] = "microstructure"
    spec_dict["name"] = "microstructure_misfit"
    spec_dict["supported_assets"] = ["stocks"]
    spec_dict["timeframes"] = ["1d"]  # microstructure only allows 5m/15m
    bad = StrategySpec(**spec_dict)
    with pytest.raises(TranslationError, match="disallows timeframes"):
        translate_to_file(bad, overwrite=True)


def test_translate_indicator_alias_equal_to_type_does_not_shadow():
    """Regression: when an indicator alias equals its imported function name
    (e.g. name='bb_upper', type='bb_upper'), the translator must not emit
    `bb_upper = bb_upper(bars, ...)` — that triggers UnboundLocalError because
    Python treats the LHS as a local declaration for the entire function and
    the RHS reference to the imported function fails."""
    spec = StrategySpec(
        name="bb_upper_collision_demo",
        archetype="volatility_breakout",
        thesis="Demo: alias collides with imported function — must not shadow.",
        supported_assets=["stocks"],
        timeframes=["1d"],
        indicators=[
            IndicatorSpec(name="bb_upper", type="bb_upper", params={"period": 20, "k": 2.0}),
        ],
        entry_long={
            "op": "compare", "operator": ">",
            "lhs": {"op": "price", "field": "close"},
            "rhs": {"op": "indicator", "name": "bb_upper"},
        },
    )
    path = translate_to_file(spec, overwrite=True)
    cls = _import(spec.name, path)

    from generator.fixture import fixture_1d
    from engine.backtester import run_backtest

    bars = fixture_1d()
    cfg = BacktestConfig(starting_capital=10_000, slippage=0.01, session=RegularTradingHours())
    # The on_bar path must execute without UnboundLocalError. The strategy
    # may make 0 trades on this fixture; the regression is about scope, not
    # signal frequency.
    result = run_backtest("AMD", bars, cls(), cfg)
    assert isinstance(result.trades, list)


# ── Unreachable-default detector (warn-only) ────────────────────────────────


def _spec_with_entry_long(*, name: str, parameters, indicators, entry_long):
    """Construct a minimal-but-valid mean_reversion spec with a custom
    entry_long. Forces a single 1d timeframe and a stocks asset class."""
    return StrategySpec(
        name=name,
        archetype="mean_reversion",
        thesis="Detector test fixture; the entry expression is the unit under test.",
        supported_assets=["stocks"],
        timeframes=["1d"],
        parameters=parameters,
        indicators=indicators,
        entry_long=entry_long,
    )


def test_unreachable_detector_flags_rsi_above_max():
    spec = _spec_with_entry_long(
        name="unreachable_rsi_above_max",
        parameters=[ParameterSpec(name="rsi_threshold", type="float", default=110.0)],
        indicators=[IndicatorSpec(name="rsi_14", type="rsi", params={"period": 14})],
        entry_long={
            "op": "compare", "operator": ">",
            "lhs": {"op": "indicator", "name": "rsi_14"},
            "rhs": {"op": "param", "name": "rsi_threshold"},
        },
    )
    findings = scan_unreachable_defaults(spec)
    assert len(findings) == 1
    f = findings[0]
    assert f.label == "entry_long"
    assert f.indicator_type == "rsi"
    assert f.operator == ">"
    assert f.param_name == "rsi_threshold"
    assert f.param_default == 110.0
    assert f.indicator_range == (0.0, 100.0)


def test_unreachable_detector_no_finding_when_default_in_range():
    spec = _spec_with_entry_long(
        name="reachable_rsi_in_range",
        parameters=[ParameterSpec(name="rsi_threshold", type="float", default=70.0)],
        indicators=[IndicatorSpec(name="rsi_14", type="rsi", params={"period": 14})],
        entry_long={
            "op": "compare", "operator": ">",
            "lhs": {"op": "indicator", "name": "rsi_14"},
            "rhs": {"op": "param", "name": "rsi_threshold"},
        },
    )
    assert scan_unreachable_defaults(spec) == []


def test_unreachable_detector_boundary_at_max_is_reachable():
    """rsi >= 100 with default=100: max=100, value=100 → max < value is False
    → reachable (rsi=100 satisfies). Boundary equality must NOT trip."""
    spec = _spec_with_entry_long(
        name="reachable_rsi_boundary",
        parameters=[ParameterSpec(name="rsi_threshold", type="float", default=100.0)],
        indicators=[IndicatorSpec(name="rsi_14", type="rsi", params={"period": 14})],
        entry_long={
            "op": "compare", "operator": ">=",
            "lhs": {"op": "indicator", "name": "rsi_14"},
            "rhs": {"op": "param", "name": "rsi_threshold"},
        },
    )
    assert scan_unreachable_defaults(spec) == []


def test_unreachable_detector_handles_swapped_lhs_rhs():
    """`110 < rsi` (param on LHS, indicator on RHS) must flip to `rsi > 110`
    and still be flagged."""
    spec = _spec_with_entry_long(
        name="unreachable_rsi_flipped",
        parameters=[ParameterSpec(name="rsi_threshold", type="float", default=110.0)],
        indicators=[IndicatorSpec(name="rsi_14", type="rsi", params={"period": 14})],
        entry_long={
            "op": "compare", "operator": "<",
            "lhs": {"op": "param", "name": "rsi_threshold"},
            "rhs": {"op": "indicator", "name": "rsi_14"},
        },
    )
    findings = scan_unreachable_defaults(spec)
    assert len(findings) == 1
    assert findings[0].operator == ">"  # post-flip
    assert findings[0].param_default == 110.0


def test_unreachable_detector_percent_rank_above_one():
    spec = _spec_with_entry_long(
        name="unreachable_percent_rank_above_one",
        parameters=[ParameterSpec(name="prank_ceiling", type="float", default=1.5)],
        indicators=[
            IndicatorSpec(name="prank_252", type="percent_rank", params={"period": 252})
        ],
        entry_long={
            "op": "compare", "operator": ">",
            "lhs": {"op": "indicator", "name": "prank_252"},
            "rhs": {"op": "param", "name": "prank_ceiling"},
        },
    )
    findings = scan_unreachable_defaults(spec)
    assert len(findings) == 1
    assert findings[0].indicator_type == "percent_rank"


def test_unreachable_detector_atr_below_zero():
    """ATR ∈ [0, inf). `atr < 0` (default=0) is unsatisfiable since min=0."""
    spec = _spec_with_entry_long(
        name="unreachable_atr_below_zero",
        parameters=[ParameterSpec(name="atr_floor", type="float", default=0.0)],
        indicators=[IndicatorSpec(name="atr_14", type="atr", params={"period": 14})],
        entry_long={
            "op": "compare", "operator": "<",
            "lhs": {"op": "indicator", "name": "atr_14"},
            "rhs": {"op": "param", "name": "atr_floor"},
        },
    )
    findings = scan_unreachable_defaults(spec)
    assert len(findings) == 1
    assert findings[0].indicator_type == "atr"


def test_unreachable_detector_unbounded_indicator_never_flags():
    """SMA has no entry in INDICATOR_RANGES → unbounded. `sma > 999999` is
    extreme but not provably unsatisfiable, so the detector must stay quiet."""
    spec = _spec_with_entry_long(
        name="reachable_sma_extreme",
        parameters=[ParameterSpec(name="sma_threshold", type="float", default=999999.0)],
        indicators=[IndicatorSpec(name="sma_50", type="sma", params={"period": 50})],
        entry_long={
            "op": "compare", "operator": ">",
            "lhs": {"op": "indicator", "name": "sma_50"},
            "rhs": {"op": "param", "name": "sma_threshold"},
        },
    )
    assert scan_unreachable_defaults(spec) == []


def test_unreachable_detector_const_rhs_is_out_of_scope():
    """Detector targets parameter-default vs indicator. A Const RHS is a
    different (broader) issue and is intentionally out of scope here."""
    spec = _spec_with_entry_long(
        name="const_rhs_out_of_scope",
        parameters=[ParameterSpec(name="dummy", type="float", default=1.0)],
        indicators=[IndicatorSpec(name="rsi_14", type="rsi", params={"period": 14})],
        entry_long={
            "op": "compare", "operator": ">",
            "lhs": {"op": "indicator", "name": "rsi_14"},
            "rhs": {"op": "const", "value": 110.0},
        },
    )
    assert scan_unreachable_defaults(spec) == []


def test_unreachable_detector_finds_clause_inside_and():
    """The unreachable clause is one conjunct in an AND tree — must still be
    detected by recursion."""
    spec = _spec_with_entry_long(
        name="unreachable_inside_and",
        parameters=[
            ParameterSpec(name="rsi_threshold", type="float", default=110.0),
            ParameterSpec(name="sma_threshold", type="float", default=10.0),
        ],
        indicators=[
            IndicatorSpec(name="rsi_14", type="rsi", params={"period": 14}),
            IndicatorSpec(name="sma_50", type="sma", params={"period": 50}),
        ],
        entry_long={
            "op": "and",
            "args": [
                {"op": "compare", "operator": ">",
                 "lhs": {"op": "indicator", "name": "sma_50"},
                 "rhs": {"op": "param", "name": "sma_threshold"}},
                {"op": "compare", "operator": ">",
                 "lhs": {"op": "indicator", "name": "rsi_14"},
                 "rhs": {"op": "param", "name": "rsi_threshold"}},
            ],
        },
    )
    findings = scan_unreachable_defaults(spec)
    assert len(findings) == 1
    assert findings[0].indicator_alias == "rsi_14"


def test_unreachable_detector_skips_under_not():
    """Conservative: a clause negated by NOT becomes always-true under the
    inversion, which is a different quirk class. The detector intentionally
    does not report it. This pins that intent so future refactors don't
    silently start emitting confusing warnings."""
    spec = _spec_with_entry_long(
        name="reachable_under_not",
        parameters=[ParameterSpec(name="rsi_threshold", type="float", default=110.0)],
        indicators=[IndicatorSpec(name="rsi_14", type="rsi", params={"period": 14})],
        entry_long={
            "op": "not",
            "arg": {"op": "compare", "operator": ">",
                    "lhs": {"op": "indicator", "name": "rsi_14"},
                    "rhs": {"op": "param", "name": "rsi_threshold"}},
        },
    )
    assert scan_unreachable_defaults(spec) == []


def test_translate_to_file_warns_but_does_not_raise(monkeypatch, tmp_path, caplog):
    """End-to-end: an unreachable clause causes a logger.warning + a quirks
    file row, but translate_to_file must NOT raise. Quirk file is redirected
    to tmp_path to keep the test hermetic."""
    import generator.translator as tr

    quirks_path = tmp_path / "generation_quirks.json"
    monkeypatch.setattr(tr, "_QUIRKS_PATH", quirks_path)

    spec = _spec_with_entry_long(
        name="warn_only_no_raise",
        parameters=[ParameterSpec(name="rsi_threshold", type="float", default=110.0)],
        indicators=[IndicatorSpec(name="rsi_14", type="rsi", params={"period": 14})],
        entry_long={
            "op": "compare", "operator": ">",
            "lhs": {"op": "indicator", "name": "rsi_14"},
            "rhs": {"op": "param", "name": "rsi_threshold"},
        },
    )

    import logging
    with caplog.at_level(logging.WARNING, logger="generator.translator"):
        path = translate_to_file(spec, overwrite=True)

    assert path.exists()
    assert any("unreachable-default" in r.message for r in caplog.records)

    # Quirks file shape: a single 'unreachable_default' record with one example.
    data = json.loads(quirks_path.read_text())
    assert "unreachable_default" in data
    rec = data["unreachable_default"]
    assert rec["total"] == 1
    assert rec["by_strategy"]["warn_only_no_raise"] == 1
    assert rec["by_indicator"]["rsi"] == 1
    assert len(rec["examples"]) == 1
    ex = rec["examples"][0]
    assert ex["param_name"] == "rsi_threshold"
    assert ex["param_default"] == 110.0
    assert ex["indicator_range"] == [0.0, 100.0]
