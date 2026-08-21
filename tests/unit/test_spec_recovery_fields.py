"""Tests for the generalized stringified-field recovery.

WHY THIS FILE EXISTS: on 2026-08-19 Sonnet 4.6 started JSON-encoding the
`position_sizing` field. spec_recovery enumerated four field names by hand
(entry_long, entry_short, exit_long, exit_short), so `position_sizing` went
straight to pydantic as a string and every generation failed validation. Three
nightly runs — 99 generations, ~$1.93 — produced zero evaluated candidates, and
the timer stayed green throughout.

The fix is structural: the eligible field set is derived from
StrategySpec.model_fields, so any field declared as a model/list/dict is covered
whether or not anyone anticipated the model stringifying it. These tests pin
that property rather than the specific field, so they keep holding when the
model picks a different slot next month.
"""

from __future__ import annotations

import json

import pytest

from generator.spec import StrategySpec
from generator.spec_recovery import (
    _LEGACY_DSL_FIELDS,
    _structured_fields,
    recover_stringified_dsl_fields,
    recover_stringified_fields,
)


def _valid_spec_dict() -> dict:
    """A minimal spec that validates, as nested objects (not strings)."""
    return {
        "name": "recovery_probe",
        "archetype": "mean_reversion",
        "thesis": "A deliberately simple oversold bounce used only as a test fixture.",
        "supported_assets": ["stocks"],
        "timeframes": ["1d"],
        "indicators": [{"name": "rsi_5", "type": "rsi", "params": {"period": 5}}],
        "parameters": [
            {"name": "rsi_lo", "type": "float", "default": 30.0,
             "range_min": 20.0, "range_max": 40.0, "description": "oversold level"},
        ],
        "entry_long": {
            "op": "compare",
            "operator": "<",
            "lhs": {"op": "indicator", "name": "rsi_5"},
            "rhs": {"op": "param", "name": "rsi_lo"},
        },
        "position_sizing": {"rule": "fixed", "size": 1},
    }


# ── The field set is derived, not hand-written ────────────────────────────────


def test_every_legacy_dsl_field_is_still_covered():
    """The four fields the old hand-written tuple listed must remain eligible —
    the generalization is a superset, not a replacement."""
    eligible = _structured_fields()
    for fld in _LEGACY_DSL_FIELDS:
        assert fld in eligible, f"{fld} lost its safety net"


def test_position_sizing_is_covered():
    """The 2026-08-19 regression, pinned directly."""
    assert "position_sizing" in _structured_fields()


def test_scalar_fields_are_never_eligible():
    """name/thesis are genuine strings. If recovery ever tried to json.loads
    them, a thesis that happens to start with '{' would be silently mangled."""
    eligible = _structured_fields()
    for fld in ("name", "thesis", "archetype"):
        assert fld not in eligible, f"{fld} is a scalar and must not be parsed"


def test_every_structured_spec_field_is_eligible():
    """The property that makes this fix structural: no model/list/dict field on
    StrategySpec may be left out of the recovery set. A new nested field added
    to the spec is covered on the day it lands."""
    from pydantic import BaseModel

    eligible = _structured_fields()
    for name, field in StrategySpec.model_fields.items():
        ann = field.annotation
        origin = getattr(ann, "__origin__", None)
        is_obj = (isinstance(ann, type) and issubclass(ann, BaseModel)) or origin in (list, dict)
        if is_obj:
            assert name in eligible, f"structured field {name} is not covered"


def test_nullability_is_recorded_correctly():
    eligible = _structured_fields()
    assert eligible["entry_long"] is True, "Optional[BooleanExpression] is nullable"
    assert eligible["position_sizing"] is False, "PositionSizing has no None variant"


# ── End-to-end recovery through the validator ────────────────────────────────


def test_stringified_position_sizing_validates():
    """The exact 08-19 payload shape: position_sizing arrives JSON-encoded."""
    d = _valid_spec_dict()
    d["position_sizing"] = json.dumps(d["position_sizing"])
    spec = StrategySpec.model_validate(d, context={"model": "test", "record_quirks": False})
    assert spec.position_sizing.rule == "fixed"
    assert spec.position_sizing.size == 1


def test_all_structured_fields_stringified_at_once():
    """The failure mode was never one field — the model stringifies whatever it
    considers complex, and the count grows. Every eligible slot at once must
    still validate."""
    d = _valid_spec_dict()
    for fld in _structured_fields():
        if fld in d:
            d[fld] = json.dumps(d[fld])
    spec = StrategySpec.model_validate(d, context={"model": "test", "record_quirks": False})
    assert spec.name == "recovery_probe"
    assert spec.timeframes == ["1d"]
    assert len(spec.indicators) == 1
    assert spec.entry_long is not None
    assert spec.position_sizing.size == 1


def test_thesis_starting_with_a_brace_is_left_alone():
    """The negative case — DO NOT parse scalar fields. A thesis is free text and
    may legitimately begin with '{'; parsing it would replace the string with a
    dict and produce a baffling type error."""
    d = _valid_spec_dict()
    d["thesis"] = '{"not": "json"} — this is prose that happens to start with a brace.'
    spec = StrategySpec.model_validate(d, context={"model": "test", "record_quirks": False})
    assert spec.thesis.startswith('{"not": "json"}')


def test_unparseable_nullable_field_becomes_none():
    """Documented behavior for nullable slots: blank it so the downstream
    'at least one entry side' check reports the real problem."""
    d = _valid_spec_dict()
    d["exit_long"] = "{this is not valid json"
    spec = StrategySpec.model_validate(d, context={"model": "test", "record_quirks": False})
    assert spec.exit_long is None


def test_unparseable_non_nullable_field_keeps_the_raw_string():
    """A required slot must NOT be blanked to None — pydantic's error on the
    string names what the model actually sent, which None hides."""
    d = _valid_spec_dict()
    d["position_sizing"] = "{this is not valid json"
    with pytest.raises(Exception) as exc:
        StrategySpec.model_validate(d, context={"model": "test", "record_quirks": False})
    assert "position_sizing" in str(exc.value)


def test_already_parsed_objects_are_untouched():
    d = _valid_spec_dict()
    out = recover_stringified_fields(dict(d), model="test", record=False)
    assert out["position_sizing"] == {"rule": "fixed", "size": 1}
    assert out["entry_long"]["op"] == "compare"


def test_record_false_suppresses_the_counter_write(monkeypatch):
    """Replaying archived generations must not inflate the live quirk counter.

    Without this, one dry run of scripts/recover_stranded_generations.py over
    the 08-19..08-21 window added 94 rows that no API call produced — the series
    that exists to tell us whether the safety net still earns its keep would be
    measuring our own recovery tooling."""
    import generator.spec_recovery as sr

    calls = []
    monkeypatch.setattr(sr, "_record_string_dsl_quirk",
                        lambda *a: calls.append(a))

    d = _valid_spec_dict()
    d["position_sizing"] = json.dumps(d["position_sizing"])
    sr.recover_stringified_fields(dict(d), model="test", record=False)
    assert calls == [], "record=False must not touch the counter"

    sr.recover_stringified_fields(dict(d), model="test", record=True)
    assert len(calls) == 1, "a live recovery must still be counted"
    assert calls[0][0] == "position_sizing"


def test_validator_honours_record_quirks_context(monkeypatch):
    """The suppression has to survive the trip through pydantic's context, since
    that is how the recovery script and diagnostics reach the validator."""
    import generator.spec_recovery as sr

    calls = []
    monkeypatch.setattr(sr, "_record_string_dsl_quirk",
                        lambda *a: calls.append(a))

    def stringified() -> dict:
        # A FRESH dict per call: recovery unpacks in place, so reusing one dict
        # would leave the second validate with nothing left to parse.
        d = _valid_spec_dict()
        d["position_sizing"] = json.dumps(d["position_sizing"])
        return d

    StrategySpec.model_validate(stringified(), context={"model": "t", "record_quirks": False})
    assert calls == []

    StrategySpec.model_validate(stringified(), context={"model": "t"})
    assert len(calls) == 1, "counting is the default; only replays opt out"


def test_legacy_alias_still_resolves():
    """The old name appears in commit messages, docs, and project memory. Keep
    it working so nothing silently loses the safety net."""
    assert recover_stringified_dsl_fields is recover_stringified_fields
