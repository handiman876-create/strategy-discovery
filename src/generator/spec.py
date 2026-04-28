"""Strategy spec schema + restricted boolean-expression DSL.

The DSL is a JSON tree. The set of node types is fixed; the spec validator
rejects anything not matching this shape — there is no path from a generated
spec to arbitrary Python execution. The translator emits `if`-conditions
straight from the validated tree.

Spec → tool input_schema is exposed via `StrategySpec.tool_input_schema()`
for the Anthropic SDK tool-use call.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .indicators import ALLOWED_INDICATORS, DAILY_ONLY_INDICATORS

logger = logging.getLogger(__name__)

_QUIRKS_PATH = Path(__file__).resolve().parents[2] / "results" / "generation_quirks.json"


def _record_string_dsl_quirk(field_name: str, model: str, archetype: str) -> None:
    """Persist a counter row when the safety-net validator parses a stringified
    DSL field. Defensive: any I/O failure is swallowed — we never want quirk
    logging to break validation itself."""
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

ARCHETYPE_NAMES = (
    "mean_reversion",
    "momentum",
    "volatility_breakout",
    "seasonality",
    "pairs",
    "microstructure",
    "overnight_session",
)
ASSET_CLASSES = ("stocks", "crypto")
TIMEFRAMES = ("5m", "15m", "1h", "1d")

# Position-sizing rules currently implemented in the engine.
ALLOWED_SIZING_RULES = ("fixed",)

# Sizing rules planned but not yet implemented. The validator references this
# list so error messages are concrete and forward-pointing.
PLANNED_SIZING_RULES = ("fixed_dollar", "atr_scaled", "vol_scaled")

MAX_PARAMETERS = 5
MAX_INDICATORS = 4

_SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")


# ── Parameter and indicator specs ────────────────────────────────────────────


class ParameterSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="snake_case identifier")
    type: Literal["int", "float", "bool"]
    default: Union[int, float, bool]
    range_min: Union[int, float, None] = None
    range_max: Union[int, float, None] = None
    description: str = ""

    @field_validator("name")
    @classmethod
    def _snake(cls, v: str) -> str:
        if not _SNAKE_CASE.match(v):
            raise ValueError(f"parameter name must be snake_case: {v!r}")
        return v


class IndicatorSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., description="snake_case alias used in DSL refs (e.g. 'rsi_2')")
    type: str = Field(..., description="One of ALLOWED_INDICATORS")
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _snake(cls, v: str) -> str:
        if not _SNAKE_CASE.match(v):
            raise ValueError(f"indicator alias must be snake_case: {v!r}")
        return v

    @field_validator("type")
    @classmethod
    def _allowed(cls, v: str) -> str:
        if v not in ALLOWED_INDICATORS:
            raise ValueError(
                f"indicator type {v!r} not in allowed set {sorted(ALLOWED_INDICATORS)}"
            )
        return v


# ── Boolean-expression DSL ───────────────────────────────────────────────────


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IndicatorRef(_Base):
    op: Literal["indicator"] = "indicator"
    name: str = Field(..., description="Alias of an IndicatorSpec entry")


class PriceRef(_Base):
    op: Literal["price"] = "price"
    field: Literal["open", "high", "low", "close"] = "close"


class TimeOfDay(_Base):
    op: Literal["time_of_day"] = "time_of_day"
    # Returns minutes since midnight (ET for stocks).


class Const(_Base):
    op: Literal["const"] = "const"
    value: float


class ParamRef(_Base):
    op: Literal["param"] = "param"
    name: str  # references a ParameterSpec


# Operands (right-hand sides of comparisons): refs + constants.
Operand = Annotated[
    Union[IndicatorRef, PriceRef, TimeOfDay, Const, ParamRef],
    Field(discriminator="op"),
]


class Compare(_Base):
    op: Literal["compare"] = "compare"
    operator: Literal["<", "<=", ">", ">=", "==", "!="]
    lhs: Operand
    rhs: Operand


class And(_Base):
    op: Literal["and"] = "and"
    args: list["BooleanExpression"]


class Or(_Base):
    op: Literal["or"] = "or"
    args: list["BooleanExpression"]


class Not(_Base):
    op: Literal["not"] = "not"
    arg: "BooleanExpression"


BooleanExpression = Annotated[
    Union[And, Or, Not, Compare],
    Field(discriminator="op"),
]

And.model_rebuild()
Or.model_rebuild()
Not.model_rebuild()


# ── Position sizing ──────────────────────────────────────────────────────────


class PositionSizing(_Base):
    """Phase-3 supports `fixed` only. See PLANNED_SIZING_RULES for upcoming
    additions (fractional-share / ATR-scaled etc., Phase 3.5)."""

    rule: Literal["fixed"] = "fixed"
    size: int = Field(default=1, ge=1, le=100)


# ── StrategySpec (root) ──────────────────────────────────────────────────────


class StrategySpec(_Base):
    name: str = Field(..., description="snake_case unique identifier")
    archetype: Literal[ARCHETYPE_NAMES] = Field(...)  # type: ignore[valid-type]
    thesis: str = Field(..., min_length=20, max_length=400)
    supported_assets: list[Literal[ASSET_CLASSES]] = Field(..., min_length=1)  # type: ignore[valid-type]
    timeframes: list[Literal[TIMEFRAMES]] = Field(..., min_length=1)  # type: ignore[valid-type]
    parameters: list[ParameterSpec] = Field(default_factory=list, max_length=MAX_PARAMETERS)
    indicators: list[IndicatorSpec] = Field(default_factory=list, max_length=MAX_INDICATORS)
    entry_long: BooleanExpression | None = None
    entry_short: BooleanExpression | None = None
    exit_long: BooleanExpression | None = None
    exit_short: BooleanExpression | None = None
    position_sizing: PositionSizing = Field(default_factory=PositionSizing)

    @field_validator("name")
    @classmethod
    def _snake(cls, v: str) -> str:
        if not _SNAKE_CASE.match(v):
            raise ValueError(f"strategy name must be snake_case: {v!r}")
        return v

    @field_validator(
        "entry_long", "entry_short", "exit_long", "exit_short", mode="before"
    )
    @classmethod
    def _parse_stringified_dsl(cls, v: Any, info) -> Any:
        # Safety net: the anyOf[discriminated-oneOf, null] schema for these
        # optional DSL fields can cause Claude to JSON-encode them as strings
        # rather than nest them as objects. Detect, parse, count, continue.
        # If this counter stays at 0 after the prompt fix, this branch is dead
        # weight and can be removed. See docs/coding-conventions.md.
        if not isinstance(v, str):
            return v
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError:
            return v  # let normal validation surface the real type error
        ctx = info.context or {}
        model = ctx.get("model", "unknown")
        archetype = (info.data or {}).get("archetype", "unknown")
        logger.warning(
            "stringified DSL quirk auto-parsed: field=%s model=%s archetype=%s",
            info.field_name, model, archetype,
        )
        _record_string_dsl_quirk(info.field_name, model, archetype)
        return parsed

    @model_validator(mode="after")
    def _validate(self) -> "StrategySpec":
        # At least one entry side must be defined.
        if self.entry_long is None and self.entry_short is None:
            raise ValueError("StrategySpec must define at least one of entry_long or entry_short")

        # Indicator alias uniqueness.
        names = [i.name for i in self.indicators]
        if len(names) != len(set(names)):
            raise ValueError("indicator aliases must be unique")

        # Parameter alias uniqueness.
        pnames = [p.name for p in self.parameters]
        if len(pnames) != len(set(pnames)):
            raise ValueError("parameter names must be unique")

        # Daily-only indicators cannot appear on intraday strategies.
        intraday = any(t in {"5m", "15m", "1h"} for t in self.timeframes)
        if intraday:
            for ind in self.indicators:
                if ind.type in DAILY_ONLY_INDICATORS:
                    raise ValueError(
                        f"indicator {ind.type!r} (alias {ind.name!r}) is daily-only "
                        f"but spec timeframes include intraday: {self.timeframes}"
                    )

        # Every IndicatorRef in expressions must resolve to a declared alias.
        # Every ParamRef must resolve to a declared parameter.
        ind_names = set(names)
        param_names = set(pnames)
        for label, expr in (
            ("entry_long", self.entry_long),
            ("entry_short", self.entry_short),
            ("exit_long", self.exit_long),
            ("exit_short", self.exit_short),
        ):
            if expr is None:
                continue
            self._check_refs(expr, ind_names, param_names, label)

        return self

    @classmethod
    def _check_refs(
        cls,
        node: Any,
        ind_names: set[str],
        param_names: set[str],
        label: str,
    ) -> None:
        if isinstance(node, IndicatorRef):
            if node.name not in ind_names:
                raise ValueError(
                    f"{label}: IndicatorRef {node.name!r} not declared in indicators"
                )
        elif isinstance(node, ParamRef):
            if node.name not in param_names:
                raise ValueError(
                    f"{label}: ParamRef {node.name!r} not declared in parameters"
                )
        elif isinstance(node, Compare):
            cls._check_refs(node.lhs, ind_names, param_names, label)
            cls._check_refs(node.rhs, ind_names, param_names, label)
        elif isinstance(node, And) or isinstance(node, Or):
            for a in node.args:
                cls._check_refs(a, ind_names, param_names, label)
        elif isinstance(node, Not):
            cls._check_refs(node.arg, ind_names, param_names, label)

    @classmethod
    def tool_input_schema(cls) -> dict:
        """JSON schema usable as Anthropic tool-use input_schema."""
        schema = cls.model_json_schema()
        # Anthropic tool-use expects "type": "object" at top level.
        return schema
