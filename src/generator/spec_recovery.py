"""Canonical recovery for raw_tool_input from Anthropic tool-use.

Sonnet 4.6 JSON-encodes *structured* StrategySpec fields as strings — it
conservatively stringifies slots whose schema is a nested object, a list,
or a discriminated union, even though the schema does not permit strings
there. The set of affected fields is NOT stable over time:

  * 2026-04-28 → the four Optional DSL slots (entry_long, entry_short,
    exit_long, exit_short). The original theory was that the
    `anyOf[oneOf-discriminated-union, null]` shape of
    `Optional[BooleanExpression]` was what triggered it.
  * 2026-08-19 → `position_sizing`, which is a plain required-with-default
    nested model: no union, no null. That falsified the theory above and
    cost three consecutive nightly runs (~$1.93, 99 wasted generations,
    zero evaluated candidates) because the safety net enumerated four
    field names by hand and this was not one of them.

So the field list is no longer hand-written. `_structured_fields()`
derives it from `StrategySpec.model_fields`: any field whose declared
type is a model, list, or dict is eligible for recovery, and any field
whose declared type is a scalar (str / Literal-of-str / int / float /
bool) is not. A new structured field added to the spec is covered the
day it lands; a new field the model decides to stringify next month is
covered without a code change.

Any consumer of `raw_tool_input` MUST route through
`recover_stringified_fields` so the safety net + counter stay in one
place. Today that means:

  * StrategySpec validator (model_validator mode="before")
  * evaluation.diagnostics._load_spec_for
  * scripts/recover_stranded_generations.py

Future consumers (e.g. retry-feedback formatters that re-show the spec
to the model, archival tools that re-evaluate old generations) MUST
also call this helper before walking the DSL nodes. Otherwise they
will index strings with `node["op"]` and raise:

    TypeError: string indices must be integers, not 'str'
"""

from __future__ import annotations

import json
import logging
import types
import typing
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel
from .paths import quirks_path

logger = logging.getLogger(__name__)

# Retained for reference and for the counter's historical continuity: every
# quirk row recorded before 2026-08-21 came from one of these four. The
# recovery pass no longer reads this tuple — see _structured_fields().
_LEGACY_DSL_FIELDS = ("entry_long", "entry_short", "exit_long", "exit_short")


_NONE_TYPE = type(None)


def _leaf_types(tp: typing.Any) -> list[typing.Any]:
    """Flatten a type annotation to its leaves, stripping Annotated wrappers
    and Union/Optional membership.

    `Optional[Annotated[Union[And, Or, Not, Compare], Field(...)]]`
        -> [And, Or, Not, Compare, NoneType]
    `list[Literal['1m', '5m']]`
        -> [list[Literal['1m', '5m']]]   (origin inspected by the caller)
    """
    if hasattr(tp, "__metadata__"):  # Annotated[X, ...] -> X
        return _leaf_types(tp.__origin__)
    origin = typing.get_origin(tp)
    if origin is typing.Union or origin is types.UnionType:
        out: list[typing.Any] = []
        for arg in typing.get_args(tp):
            out.extend(_leaf_types(arg))
        return out
    return [tp]


def _is_structured(tp: typing.Any) -> bool:
    """True when `tp` is a nested model, list, dict, or tuple — i.e. a slot the
    model might JSON-encode. False for scalars, including Literal-of-scalars.

    Deliberately structural rather than a name list: `position_sizing` was
    missed for three nights precisely because the old check asked "is this
    field one of these four names" instead of "is this field an object".
    """
    if typing.get_origin(tp) in (list, dict, tuple):
        return True
    return isinstance(tp, type) and issubclass(tp, BaseModel)


@lru_cache(maxsize=1)
def _structured_fields() -> dict[str, bool]:
    """Map field name -> is_nullable for every structured StrategySpec field.

    Imported lazily: spec.py imports this module at module scope, so a
    top-level `from .spec import StrategySpec` would be circular. By the
    time a validator calls in, spec.py is fully loaded. Cached because the
    answer is fixed for the life of the process.
    """
    from .spec import StrategySpec

    fields: dict[str, bool] = {}
    for name, field in StrategySpec.model_fields.items():
        leaves = _leaf_types(field.annotation)
        nullable = _NONE_TYPE in leaves
        concrete = [leaf for leaf in leaves if leaf is not _NONE_TYPE]
        if any(_is_structured(leaf) for leaf in concrete):
            fields[name] = nullable
    return fields


def recover_stringified_fields(
    spec_dict: dict,
    *,
    model: str = "unknown",
    archetype: str | None = None,
    record: bool = True,
) -> dict:
    """Walk every structured StrategySpec slot; for each that arrived as a
    JSON-encoded string, json.loads it back into an object. Mutates and
    returns spec_dict.

    Each successful unpack increments the persistent counter at
    results/generation_quirks.json, broken down by field, so we can tell
    over time both whether the safety net is still earning its keep and
    *which* slots the model is stringifying this month (see
    feedback_observability_in_validators in the project memory). The
    2026-08-19 regression would have been a one-glance diagnosis if
    `position_sizing` had been able to appear in that breakdown.

    A field that is already an object is left alone. A string that fails
    json.loads is handled by nullability:

      * nullable field  -> set to None, so the validator's downstream
        "at least one entry side" / type checks surface the real problem
        rather than a confusing TypeError deep in the DSL walker.
      * non-nullable field -> left as the raw string, because None on a
        required slot produces a misleading "Input should be a valid
        dictionary [input_value=None]" that hides what the model sent.
        Pydantic's own error on the string is the clearer message.

    record=False suppresses the counter write. Set it when REPLAYING archived
    generations (the recovery script, tests, diagnostics re-runs): those are not
    fresh observations of the model's behaviour, and counting them inflates the
    series that is supposed to answer "is this net still earning its keep". A
    single dry run over the 08-19..08-21 archive added 94 rows that no live API
    call produced.
    """
    arch = archetype if archetype is not None else spec_dict.get("archetype", "unknown")
    for fld, nullable in _structured_fields().items():
        v = spec_dict.get(fld)
        if not isinstance(v, str):
            continue
        try:
            spec_dict[fld] = json.loads(v)
        except json.JSONDecodeError:
            if nullable:
                spec_dict[fld] = None
            continue
        logger.warning(
            "stringified spec quirk auto-parsed: field=%s model=%s archetype=%s",
            fld, model, arch,
        )
        if record:
            _record_string_dsl_quirk(fld, model, arch)
    return spec_dict


# Back-compat alias: the old name is referenced in commit messages, docs, and
# the project memory. Keep it resolvable so nothing silently loses the net.
recover_stringified_dsl_fields = recover_stringified_fields


def _record_string_dsl_quirk(field_name: str, model: str, archetype: str) -> None:
    """Persist a counter row when the safety net unpacks a stringified field.

    The counter key stays `string_dsl_field` even though the net is no longer
    DSL-specific: 6,838 rows of history from 2026-04-28 onward live under that
    key, and the `by_field` breakdown already distinguishes which slot fired.
    Renaming the key would fragment the series for no gain.

    Defensive: any I/O failure is swallowed — quirk logging must never break
    validation or diagnostic flow."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        data: dict = {}
        if quirks_path().exists():
            data = json.loads(quirks_path().read_text())
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
        quirks_path().parent.mkdir(parents=True, exist_ok=True)
        quirks_path().write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.warning("failed to record quirk to %s: %s", quirks_path(), e)
