"""Volume archetype: obv_zscore / vwap_dev math, registration, and the
volume-as-primary-signal gate."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from generator import indicators
from generator.archetypes import ARCHETYPES, get_archetype
from generator.spec import ARCHETYPE_NAMES, IndicatorSpec, StrategySpec
from generator.translator import TranslationError, validate_for_translation
from strategy.context import Bar

ET = ZoneInfo("America/New_York")
PROMPT_PATH = (
    Path(__file__).resolve().parents[2] / "src" / "generator" / "prompts" / "volume.md"
)


def _bars(closes: list[float], volumes: list[float]) -> list[Bar]:
    base = datetime(2024, 1, 1, tzinfo=ET)
    return [
        Bar(base + timedelta(days=i), c, c + 0.5, c - 0.5, c, v)
        for i, (c, v) in enumerate(zip(closes, volumes))
    ]


# ── obv_zscore ───────────────────────────────────────────────────────────────


def test_obv_zscore_known_value():
    # OBV over the last 3 diffs: +100, -50, +200 → [100, 50, 250].
    # mean 133.33, population std 84.984 → z = 116.67 / 84.984.
    bars = _bars([10, 11, 10, 12], [999, 100, 50, 200])
    assert indicators.obv_zscore(bars, 3) == pytest.approx(1.37281, rel=1e-4)


def test_obv_zscore_invariant_to_buffer_start():
    """The z-score must not depend on how much history precedes the window —
    that is what justifies summing OBV from the window start."""
    closes = [10, 12, 11, 13, 12, 14, 13, 15, 16]
    vols = [500, 100, 300, 200, 400, 150, 250, 350, 120]
    full = _bars(closes, vols)
    assert indicators.obv_zscore(full, 4) == pytest.approx(
        indicators.obv_zscore(full[-5:], 4)
    )


def test_obv_zscore_zero_volume_bar_contributes_nothing():
    # Middle bar has zero volume: OBV [100, 100, 300] → still computable.
    bars = _bars([10, 11, 12, 13], [999, 100, 0, 200])
    # Deviations from mean 166.67 are (-1, -1, +2) × 66.67 → z = 2/√2 = √2.
    assert indicators.obv_zscore(bars, 3) == pytest.approx(2 ** 0.5)


def test_obv_zscore_all_zero_volume_returns_none():
    bars = _bars([10, 11, 12, 13], [0, 0, 0, 0])
    assert indicators.obv_zscore(bars, 3) is None


def test_obv_zscore_lookback_is_period_plus_one():
    bars = _bars([10, 11, 10, 12], [1, 100, 50, 200])
    assert indicators.obv_zscore(bars[:3], 3) is None
    assert indicators.obv_zscore(bars, 3) is not None


# ── vwap_dev ─────────────────────────────────────────────────────────────────


def test_vwap_dev_known_value():
    # VWAP(2) = (10*100 + 12*300) / 400 = 11.5.
    # TRs: 1.0, max(1.0, 2.5, 1.5) = 2.5 → ATR(2) = 1.75.
    bars = _bars([10, 10, 12], [999, 100, 300])
    assert indicators.vwap_dev(bars, 2) == pytest.approx(0.5 / 1.75)


def test_vwap_dev_zero_volume_bar_excluded_from_vwap():
    # Zero-volume bar at 20 carries no weight: VWAP = 10 over the window.
    bars = _bars([10, 10, 20, 10], [999, 100, 0, 100])
    atr = indicators.atr(bars, 3)
    assert indicators.vwap_dev(bars, 3) == pytest.approx((10 - 10) / atr)


def test_vwap_dev_all_zero_volume_returns_none():
    bars = _bars([10, 11, 12], [0, 0, 0])
    assert indicators.vwap_dev(bars, 2) is None


def test_vwap_dev_lookback_is_period_plus_one():
    bars = _bars([10, 10, 12], [1, 100, 300])
    assert indicators.vwap_dev(bars[:2], 2) is None
    assert indicators.vwap_dev(bars, 2) is not None


@pytest.mark.parametrize("name", ["obv_zscore", "vwap_dev"])
def test_volume_indicators_registered(name: str):
    assert name in indicators.ALLOWED_INDICATORS
    assert name in indicators.INDICATOR_FUNCTIONS
    assert name in indicators.VOLUME_INDICATORS
    assert indicators.indicator_lookback(name, {"period": 30}) == 30


def test_unknown_volume_indicator_rejected():
    with pytest.raises(ValidationError, match="not in allowed set"):
        IndicatorSpec(name="vd", type="volume_delta", params={})


# ── Archetype registration ───────────────────────────────────────────────────


def test_volume_archetype_registered_in_both_places():
    assert "volume" in ARCHETYPE_NAMES
    arch = get_archetype("volume")
    assert arch.allowed_timeframes == ["1d"]
    assert set(arch.required_indicators) == {"obv_zscore", "vwap_dev"}


def test_archetype_registries_agree():
    """spec.ARCHETYPE_NAMES and archetypes.ARCHETYPES are kept by hand; a name
    in one but not the other either fails validation or get_archetype()."""
    assert set(ARCHETYPE_NAMES) == set(ARCHETYPES)


def test_autodiscover_schedules_volume_at_weight_2():
    """autodiscover keeps its own hand-written archetype list; a new archetype
    missing from it is never generated nightly."""
    import importlib.util

    script = Path(__file__).resolve().parents[2] / "scripts" / "autodiscover.py"
    spec = importlib.util.spec_from_file_location("autodiscover_script", script)
    ad = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ad)
    assert ad.ARCHETYPE_WEIGHTS["volume"] == 2
    assert "volume" in ad.ARCHETYPES
    assert set(ad.ARCHETYPE_WEIGHTS) <= set(ARCHETYPES)


def test_prompt_states_350_char_budget_and_1d():
    text = PROMPT_PATH.read_text()
    assert "at most 350 characters" in text
    assert '`["1d"]` only' in text
    assert "DO NOT use EMA as the primary signal" in text


# ── Spec-level gates ─────────────────────────────────────────────────────────


def _cmp(alias: str, op: str, value: float) -> dict:
    return {
        "op": "compare",
        "operator": op,
        "lhs": {"op": "indicator", "name": alias},
        "rhs": {"op": "const", "value": value},
    }


def _volume_spec(*, indicators_, entry_long, thesis: str | None = None) -> StrategySpec:
    return StrategySpec(
        name="vol_probe",
        archetype="volume",
        thesis=thesis or "Unusual up-close volume marks accumulation across sectors.",
        supported_assets=["stocks"],
        timeframes=["1d"],
        indicators=indicators_,
        entry_long=entry_long,
    )


def test_schema_rejects_thesis_over_400_chars():
    with pytest.raises(ValidationError, match="at most 400 characters"):
        _volume_spec(
            indicators_=[IndicatorSpec(name="obv_z", type="obv_zscore", params={"period": 20})],
            entry_long=_cmp("obv_z", ">", 1.5),
            thesis="x" * 401,
        )


def test_volume_spec_with_obv_entry_passes():
    spec = _volume_spec(
        indicators_=[
            IndicatorSpec(name="obv_z", type="obv_zscore", params={"period": 20}),
            IndicatorSpec(name="sma_50", type="sma", params={"period": 50}),
        ],
        entry_long={"op": "and", "args": [
            _cmp("obv_z", ">", 1.5),
            {"op": "compare", "operator": ">",
             "lhs": {"op": "price", "field": "close"},
             "rhs": {"op": "indicator", "name": "sma_50"}},
        ]},
    )
    validate_for_translation(spec)


def test_volume_spec_with_no_volume_indicator_flagged():
    spec = _volume_spec(
        indicators_=[IndicatorSpec(name="ema_9", type="ema", params={"period": 9})],
        entry_long=_cmp("ema_9", ">", 100.0),
    )
    with pytest.raises(TranslationError, match="requires at least one of"):
        validate_for_translation(spec)


def test_volume_indicator_declared_but_not_in_entry_flagged():
    spec = _volume_spec(
        indicators_=[
            IndicatorSpec(name="ema_9", type="ema", params={"period": 9}),
            IndicatorSpec(name="vwap_d", type="vwap_dev", params={"period": 20}),
        ],
        entry_long=_cmp("ema_9", ">", 100.0),
    )
    with pytest.raises(TranslationError, match="requires at least one of"):
        validate_for_translation(spec)


def test_volume_archetype_rejects_intraday():
    spec = StrategySpec(
        name="vol_probe",
        archetype="volume",
        thesis="Unusual up-close volume marks accumulation across sectors.",
        supported_assets=["stocks"],
        timeframes=["5m"],
        indicators=[IndicatorSpec(name="obv_z", type="obv_zscore", params={"period": 20})],
        entry_long=_cmp("obv_z", ">", 1.5),
    )
    with pytest.raises(TranslationError, match="disallows timeframes"):
        validate_for_translation(spec)


def test_other_archetypes_unaffected_by_volume_requirement():
    spec = StrategySpec(
        name="rsi_dip",
        archetype="mean_reversion",
        thesis="Short-term oversold dips in uptrends partially revert.",
        supported_assets=["stocks"],
        timeframes=["1d"],
        indicators=[IndicatorSpec(name="rsi_2", type="rsi", params={"period": 2})],
        entry_long=_cmp("rsi_2", "<", 5.0),
    )
    validate_for_translation(spec)


def test_discover_cli_accepts_note():
    """--note was missing on the first volume test run (2026-09-23) and the
    run label was lost; pin that the flag exists."""
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    out = subprocess.run(
        [sys.executable, str(root / "scripts" / "discover.py"), "--help"],
        capture_output=True, text=True, cwd=root, timeout=120,
    )
    assert out.returncode == 0, out.stderr
    assert "--note" in out.stdout
