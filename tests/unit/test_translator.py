"""Translator emits compilable code that runs on the fixture."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from engine.backtester import BacktestConfig, run_backtest
from engine.session import RegularTradingHours
from generator.spec import IndicatorSpec, ParameterSpec, StrategySpec
from generator.translator import GENERATED_DIR, TranslationError, translate_to_file


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
