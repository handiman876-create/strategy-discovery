"""Regression tests for evaluation.diagnostics.

The diagnostic loads `raw_tool_input` from results/generations/, which is
the model's pre-validation payload. Sonnet 4.6 routinely returns the four
discriminated-union DSL fields (entry_long/short, exit_long/short) as
JSON-encoded strings instead of nested objects. The production validator
unpacks these via StrategySpec._parse_stringified_dsl, but the diagnostic
was a separate consumer that didn't share the same protection — leading
to `TypeError: string indices must be integers, not 'str'` when the DSL
walker indexed a string with `entry["op"]`.

Repro path baked into this test: build a generation file whose
raw_tool_input has stringified entry/exit fields, then call
_load_spec_for and assert the result has them parsed back to dicts.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from evaluation import diagnostics
from strategy.base import Strategy


class StringifiedDslStrategy(Strategy):
    """Marker class — the diagnostic uses .__name__ to glob the generations
    dir, so we only need this to expose a class name to convert via
    _camel_to_snake. on_bar / get_parameters aren't invoked by _load_spec_for."""

    archetype = "mean_reversion"
    thesis = "Test fixture for stringified DSL recovery in diagnostic."
    supported_assets = ["stocks"]
    timeframes = ["5m"]

    def on_bar(self, bar, position, context):  # pragma: no cover
        return []

    def get_parameters(self):  # pragma: no cover
        return {}


def _write_generation_file(dir: Path, snake_name: str, raw_tool_input: dict) -> Path:
    """Mirror the generation-file layout that _load_spec_for globs against."""
    payload = {
        "timestamp": "2026-04-28T00:00:00+00:00",
        "archetype": "mean_reversion",
        "model": "claude-sonnet-4-6",
        "raw_tool_input": raw_tool_input,
    }
    path = dir / f"2026-04-28T00-00-00.000000+00-00_{snake_name}.json"
    path.write_text(json.dumps(payload))
    return path


def _stringified_spec_payload() -> dict:
    """A raw_tool_input where entry_short and exit_short are JSON strings,
    not nested objects — the exact shape Sonnet 4.6 emits under the
    Optional[BooleanExpression] union."""
    entry_short_obj = {
        "op": "and",
        "args": [
            {"op": "compare", "operator": ">",
             "lhs": {"op": "indicator", "name": "rsi_7"},
             "rhs": {"op": "const", "value": 70.0}},
        ],
    }
    return {
        "name": "stringified_dsl_strategy",
        "archetype": "mean_reversion",
        "thesis": "Test fixture for stringified DSL recovery in diagnostic.",
        "supported_assets": ["stocks"],
        "timeframes": ["5m"],
        "parameters": [],
        "indicators": [
            {"name": "rsi_7", "type": "rsi", "params": {"period": 7}},
        ],
        "entry_long": None,
        "entry_short": json.dumps(entry_short_obj),
        "exit_long": None,
        "exit_short": json.dumps({"op": "compare", "operator": "<",
                                  "lhs": {"op": "indicator", "name": "rsi_7"},
                                  "rhs": {"op": "const", "value": 30.0}}),
    }


def test_load_spec_for_unpacks_stringified_dsl_fields(tmp_path):
    """Without the safety net, _load_spec_for returns the spec with
    entry_short still a str. Downstream walkers then do entry["op"] on
    a string and raise TypeError. With the safety net, the field is a
    dict by the time _load_spec_for returns."""
    gen_dir = tmp_path / "generations"
    gen_dir.mkdir()
    _write_generation_file(gen_dir, "stringified_dsl_strategy", _stringified_spec_payload())

    with patch.object(diagnostics, "_GENERATIONS_DIR", gen_dir):
        spec = diagnostics._load_spec_for(StringifiedDslStrategy)

    # The whole point of the safety net: stringified fields come back as dicts.
    assert isinstance(spec["entry_short"], dict), (
        f"entry_short should be unpacked to dict, got {type(spec['entry_short']).__name__}"
    )
    assert spec["entry_short"]["op"] == "and"
    assert isinstance(spec["exit_short"], dict)
    assert spec["exit_short"]["op"] == "compare"
    # Null fields stay null.
    assert spec["entry_long"] is None
    assert spec["exit_long"] is None


def test_split_top_level_would_fail_on_string_without_safety_net():
    """Pin the failure mode: indexing a string with a string key raises
    TypeError. This is what _split_top_level (and the rest of the DSL
    walker) does when handed an unparsed stringified field. If this test
    ever stops raising, the failure mode itself has changed and the
    safety net's contract should be re-examined."""
    stringified = json.dumps({"op": "and", "args": []})
    with pytest.raises(TypeError, match="string indices must be integers"):
        diagnostics._split_top_level(stringified)  # type: ignore[arg-type]
