"""Canonical recovery for raw_tool_input from Anthropic tool-use.

Sonnet 4.6 reliably JSON-encodes the four optional discriminated-union
DSL fields (entry_long, entry_short, exit_long, exit_short) as strings
under the `anyOf[oneOf-discriminated-union, null]` schema shape that
`Optional[BooleanExpression]` produces. The model is conservatively
stringifying complex nested-union slots; the schema does not permit
strings for these fields.

Any consumer of `raw_tool_input` MUST route through
`recover_stringified_dsl_fields` so the safety net + counter stay in
one place. Today that means:

  * StrategySpec validator (model_validator mode="before")
  * evaluation.diagnostics._load_spec_for

Future consumers (e.g. retry-feedback formatters that re-show the spec
to the model, archival tools that re-evaluate old generations) MUST
also call this helper before walking the DSL nodes. Otherwise they
will index strings with `node["op"]` and raise:

    TypeError: string indices must be integers, not 'str'
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_DSL_FIELDS = ("entry_long", "entry_short", "exit_long", "exit_short")
_QUIRKS_PATH = Path(__file__).resolve().parents[2] / "results" / "generation_quirks.json"


def recover_stringified_dsl_fields(
    spec_dict: dict,
    *,
    model: str = "unknown",
    archetype: str | None = None,
) -> dict:
    """Walk the four DSL slots; for each that's a JSON-encoded string,
    json.loads it back into a dict. Mutates and returns spec_dict.

    Each successful unpack increments the persistent counter at
    results/generation_quirks.json so we can tell over time whether the
    safety net is still earning its keep (see
    feedback_observability_in_validators in the project memory).

    A field that's already a dict is left alone. A string that fails
    json.loads is replaced with None — the validator's downstream type
    checks will surface the real problem with a clear error rather than
    a confusing TypeError deep in the DSL walker.
    """
    arch = archetype if archetype is not None else spec_dict.get("archetype", "unknown")
    for fld in _DSL_FIELDS:
        v = spec_dict.get(fld)
        if not isinstance(v, str):
            continue
        try:
            spec_dict[fld] = json.loads(v)
        except json.JSONDecodeError:
            spec_dict[fld] = None
            continue
        logger.warning(
            "stringified DSL quirk auto-parsed: field=%s model=%s archetype=%s",
            fld, model, arch,
        )
        _record_string_dsl_quirk(fld, model, arch)
    return spec_dict


def _record_string_dsl_quirk(field_name: str, model: str, archetype: str) -> None:
    """Persist a counter row when the safety net unpacks a stringified DSL
    field. Defensive: any I/O failure is swallowed — quirk logging must
    never break validation or diagnostic flow."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        data: dict = {}
        if _QUIRKS_PATH.exists():
            data = json.loads(_QUIRKS_PATH.read_text())
        rec = data.setdefault(
            "string_dsl_field",
            {
                "total": 0,
                "by_model": {},
                "by_field": {},
                "by_archetype": {},
                "first_seen": now,
                "last_seen": now,
            },
        )
        rec["total"] += 1
        rec["by_model"][model] = rec["by_model"].get(model, 0) + 1
        rec["by_field"][field_name] = rec["by_field"].get(field_name, 0) + 1
        rec["by_archetype"][archetype] = rec["by_archetype"].get(archetype, 0) + 1
        rec["last_seen"] = now
        _QUIRKS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _QUIRKS_PATH.write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.warning("failed to record quirk to %s: %s", _QUIRKS_PATH, e)
