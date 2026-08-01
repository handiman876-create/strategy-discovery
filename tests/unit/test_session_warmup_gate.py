"""Session warm-up gate: reject specs whose indicators can never warm up.

Regression cover for the 1h zero-trade bug (2026-08-01). A 1h RTH session
holds 7 bars and session_bars resets each session, so any lookback > 7 stayed
None forever and the strategy silently took zero trades — 99/99 generated 1h
strategies, $3.05 of generation spend, with nothing in the logs to show for it.

The defence tested here is generic: the translator rejects over-long lookbacks
on ANY intraday timeframe, so the next variant of this bug (15m/30m/5m, or a
timeframe added later) is caught by the same rule. Rejection feeds the
generation retry loop as `retry_feedback`, so the model self-corrects rather
than emitting a strategy that cannot trade.

Removing 1h from the archetype allow-lists was considered as a second, blunter
defence and deliberately deferred — see the note above the first test.
"""

from __future__ import annotations

import pytest

from engine.session import session_bar_capacity

from generator.indicators import indicator_lookback
from generator.spec import IndicatorSpec, ParameterSpec, StrategySpec
from generator.translator import TranslationError, validate_for_translation


def _spec(*, timeframes, indicators, archetype="microstructure", name="warmup_probe"):
    """Minimal translatable spec. microstructure is the default archetype
    because it is the only one that still allows intraday timeframes, so the
    warm-up gate is reached rather than short-circuited by the timeframe
    allow-list check that runs before it."""
    return StrategySpec(
        name=name,
        archetype=archetype,
        thesis="Probe spec for exercising the session warm-up gate on intraday bars.",
        supported_assets=["stocks"],
        timeframes=timeframes,
        parameters=[ParameterSpec(name="thresh", type="float", default=50.0)],
        indicators=indicators,
        entry_long={
            "op": "compare",
            "operator": ">",
            "lhs": {"op": "price", "field": "close"},
            "rhs": {"op": "indicator", "name": indicators[0].name},
        },
        exit_long={
            "op": "compare",
            "operator": "<",
            "lhs": {"op": "price", "field": "close"},
            "rhs": {"op": "indicator", "name": indicators[0].name},
        },
        position_sizing={"rule": "fixed", "size": 1},
    )


# ── Generic warm-up rejection ────────────────────────────────────────────────
#
# Note: 1h remains an allowed timeframe for mean_reversion and
# volatility_breakout. Removing it from those archetypes was considered and
# deliberately deferred — this gate is meant to make 1h *safe* rather than
# banned, so a future multi-timeframe discovery run can use it with lookbacks
# that fit a 7-bar session.


def test_1h_spec_with_typical_lookback_is_rejected():
    """The exact cohort that died silently: 1h is still generatable, so the
    gate — not the archetype allow-list — is what stops it now."""
    spec = _spec(
        timeframes=["1h"],
        archetype="mean_reversion",
        indicators=[IndicatorSpec(name="rsi_14", type="rsi", params={"period": 14})],
        name="warmup_1h_probe",
    )
    with pytest.raises(TranslationError, match="exceeds session bars"):
        validate_for_translation(spec)


def test_1h_spec_with_short_lookback_is_allowed():
    """1h is not banned outright: a lookback that fits 7 bars passes."""
    spec = _spec(
        timeframes=["1h"],
        archetype="mean_reversion",
        indicators=[IndicatorSpec(name="sma_5", type="sma", params={"period": 5})],
        name="warmup_1h_ok_probe",
    )
    validate_for_translation(spec)  # must not raise


def test_capacity_none_for_daily_and_known_for_intraday():
    assert session_bar_capacity("1d") is None  # no session reset → unbounded
    assert session_bar_capacity("1h") == 7
    assert session_bar_capacity("5m") == 78
    assert session_bar_capacity("nonsense") is None  # refuse to guess


def test_lookback_reads_params_and_defaults():
    assert indicator_lookback("sma", {"period": 50}) == 50
    assert indicator_lookback("rsi", {}) == 14  # falls back to the real default
    # k is a std multiplier, not history — must not inflate the lookback.
    assert indicator_lookback("bb_upper", {"period": 20, "k": 2.5}) == 20
    # MACD warm-up is additive: slow EMA then signal EMA of that series.
    assert indicator_lookback("macd_hist", {"slow": 26, "signal": 9}) == 35
    assert indicator_lookback("daily_return", {}) == 0


def test_rejects_lookback_exceeding_session_bars():
    """The exact shape that produced 99 dead strategies: SMA(50) on 1h."""
    spec = _spec(
        timeframes=["5m"],
        indicators=[IndicatorSpec(name="sma_200", type="sma", params={"period": 200})],
        name="warmup_reject_probe",
    )
    with pytest.raises(TranslationError, match="exceeds session bars"):
        validate_for_translation(spec)


def test_rejection_message_names_periods_and_capacity():
    spec = _spec(
        timeframes=["5m"],
        indicators=[IndicatorSpec(name="ema_100", type="ema", params={"period": 100})],
        name="warmup_message_probe",
    )
    with pytest.raises(TranslationError) as exc:
        validate_for_translation(spec)
    msg = str(exc.value)
    assert "100" in msg and "78" in msg  # needed bars and session capacity
    assert "ema_100" in msg  # which indicator is at fault


def test_valid_intraday_spec_still_passes():
    """A lookback that fits the session must NOT be rejected."""
    spec = _spec(
        timeframes=["5m"],
        indicators=[IndicatorSpec(name="rsi_14", type="rsi", params={"period": 14})],
        name="warmup_ok_probe",
    )
    validate_for_translation(spec)  # must not raise


def test_daily_long_lookback_is_exempt():
    """200-period SMA on 1d is the RSI-2 shape — legal, warms across bars."""
    spec = _spec(
        timeframes=["1d"],
        archetype="mean_reversion",
        indicators=[IndicatorSpec(name="sma_200", type="sma", params={"period": 200})],
        name="warmup_daily_probe",
    )
    validate_for_translation(spec)  # must not raise


def test_boundary_equal_to_capacity_is_allowed():
    """Exactly-capacity warms up on a full session; only > capacity is fatal."""
    ok = _spec(
        timeframes=["15m"],
        indicators=[IndicatorSpec(name="sma_26", type="sma", params={"period": 26})],
        name="warmup_boundary_ok",
    )
    validate_for_translation(ok)

    bad = _spec(
        timeframes=["15m"],
        indicators=[IndicatorSpec(name="sma_27", type="sma", params={"period": 27})],
        name="warmup_boundary_bad",
    )
    with pytest.raises(TranslationError, match="exceeds session bars"):
        validate_for_translation(bad)


def test_gate_records_quirk_counter(tmp_path, monkeypatch):
    """Every safety net needs a counter, else we cannot tell whether it is
    still earning its keep once the generator prompt improves."""
    import json

    import generator.translator as tr

    quirks = tmp_path / "quirks.json"
    monkeypatch.setattr(tr, "_QUIRKS_PATH", quirks)
    spec = _spec(
        timeframes=["5m"],
        indicators=[IndicatorSpec(name="sma_200", type="sma", params={"period": 200})],
        name="warmup_counter_probe",
    )
    with pytest.raises(TranslationError):
        tr.validate_for_translation(spec)

    rec = json.loads(quirks.read_text())["session_warmup"]
    assert rec["total"] == 1
    assert rec["by_timeframe"]["5m"] == 1
    assert rec["examples"][0]["lookback_bars"] == 200
    assert rec["examples"][0]["session_bars"] == 78
