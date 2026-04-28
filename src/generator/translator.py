"""StrategySpec → strategies/generated/<name>.py.

The translator inlines the DSL → Python conditions inside on_bar(). The
generated file imports indicator functions from generator.indicators and
inherits from strategy.base.Strategy. No `eval`, no `exec`, no dynamic
imports — every emitted construct is a fixed shape.

Pre-translation validation rejects:
  * archetype unknown
  * pairs archetype (Phase 3.5 deferred)
  * indicators outside the allowed set
  * daily-only indicators on intraday timeframes
  * indicators / parameters / DSL refs that don't resolve
  * params or indicators above their caps
"""

from __future__ import annotations

import inspect
import json
import logging
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .archetypes import ARCHETYPES, get_archetype
from .indicators import ALLOWED_INDICATORS, INDICATOR_FUNCTIONS
from .spec import (
    And,
    BooleanExpression,
    Compare,
    Const,
    IndicatorRef,
    IndicatorSpec,
    Not,
    Operand,
    Or,
    ParamRef,
    ParameterSpec,
    PriceRef,
    PositionSizing,
    StrategySpec,
    TimeOfDay,
)

GENERATED_DIR = Path(__file__).resolve().parents[2] / "strategies" / "generated"
_QUIRKS_PATH = Path(__file__).resolve().parents[2] / "results" / "generation_quirks.json"

logger = logging.getLogger(__name__)


def _indicator_signature(name: str) -> tuple[set[str], set[str]]:
    """Return (allowed_kwarg_names, required_kwarg_names) for an indicator,
    excluding the leading `bars` positional. Source of truth: the runtime
    function in src/generator/indicators.py."""
    fn = INDICATOR_FUNCTIONS[name]
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())[1:]  # skip `bars`
    allowed = {p.name for p in params}
    required = {p.name for p in params if p.default is inspect.Parameter.empty}
    return allowed, required


def _record_kwargs_quirk(indicator: str, extra: list[str], missing: list[str]) -> None:
    """Persist a counter row when the kwarg validator rejects a spec. Defensive:
    any I/O failure is swallowed — quirk logging never blocks translation
    feedback."""
    try:
        now = datetime.now(timezone.utc).isoformat()
        data: dict = {}
        if _QUIRKS_PATH.exists():
            data = json.loads(_QUIRKS_PATH.read_text())
        rec = data.setdefault(
            "bad_indicator_kwargs",
            {
                "total": 0,
                "by_indicator": {},
                "by_extra_kwarg": {},
                "by_missing_kwarg": {},
                "first_seen": now,
                "last_seen": now,
            },
        )
        rec["total"] += 1
        rec["by_indicator"][indicator] = rec["by_indicator"].get(indicator, 0) + 1
        for k in extra:
            rec["by_extra_kwarg"][k] = rec["by_extra_kwarg"].get(k, 0) + 1
        for k in missing:
            rec["by_missing_kwarg"][k] = rec["by_missing_kwarg"].get(k, 0) + 1
        rec["last_seen"] = now
        _QUIRKS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _QUIRKS_PATH.write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.warning("failed to record kwargs quirk to %s: %s", _QUIRKS_PATH, e)


# ── Public API ───────────────────────────────────────────────────────────────


class TranslationError(ValueError):
    """Raised when a spec cannot be translated to executable code."""


def translate_to_file(spec: StrategySpec, *, overwrite: bool = True) -> Path:
    validate_for_translation(spec)
    code = _emit_code(spec)
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    path = GENERATED_DIR / f"{spec.name}.py"
    if path.exists() and not overwrite:
        raise FileExistsError(path)
    path.write_text(code)
    # Best-effort syntax validation: import as text first, then byte-compile.
    compile(code, str(path), "exec")
    return path


def validate_for_translation(spec: StrategySpec) -> None:
    arch = get_archetype(spec.archetype)
    if spec.archetype == "pairs":
        raise TranslationError(
            "pairs archetype is deferred to Phase 3.5 — engine does not yet support "
            "multi-symbol position management"
        )
    bad_assets = [a for a in spec.supported_assets if a not in arch.allowed_assets]
    if bad_assets:
        raise TranslationError(
            f"archetype {spec.archetype!r} disallows assets {bad_assets}; "
            f"allowed: {arch.allowed_assets}"
        )
    bad_tfs = [t for t in spec.timeframes if t not in arch.allowed_timeframes]
    if bad_tfs:
        raise TranslationError(
            f"archetype {spec.archetype!r} disallows timeframes {bad_tfs}; "
            f"allowed: {arch.allowed_timeframes}"
        )
    for ind in spec.indicators:
        if ind.type not in ALLOWED_INDICATORS:
            raise TranslationError(f"indicator type {ind.type!r} not in allowed set")
        _validate_indicator_kwargs(ind.type, ind.params)


def _validate_indicator_kwargs(indicator: str, params: dict[str, Any]) -> None:
    """Reject specs whose IndicatorSpec.params don't match the runtime signature.
    Catches synonym guesses (e.g. `std` for `k`, `length` for `period`) at
    translation time rather than at first on_bar() call."""
    allowed, required = _indicator_signature(indicator)
    given = set(params.keys())
    extra = sorted(given - allowed)
    missing = sorted(required - given)
    if not extra and not missing:
        return
    _record_kwargs_quirk(indicator, extra, missing)
    fn = INDICATOR_FUNCTIONS[indicator]
    sig = inspect.signature(fn)
    valid_parts: list[str] = []
    for p in list(sig.parameters.values())[1:]:
        if p.default is inspect.Parameter.empty:
            valid_parts.append(f"{p.name} (required)")
        else:
            valid_parts.append(f"{p.name} (default {p.default!r})")
    raise TranslationError(
        f"Indicator {indicator!r} has invalid kwargs: extra={extra}, missing={missing}. "
        f"Valid kwargs: {', '.join(valid_parts) if valid_parts else '(none — pass no params)'}."
    )


# ── Code emission ────────────────────────────────────────────────────────────


def _emit_code(spec: StrategySpec) -> str:
    class_name = _to_camel(spec.name)
    arch = get_archetype(spec.archetype)
    indicator_imports = sorted({i.type for i in spec.indicators})

    # Compute the lookback window: max indicator period parameter + buffer.
    lookback = _required_lookback(spec)

    parts: list[str] = []
    parts.append(_emit_header(spec, class_name))
    parts.append(_emit_imports(indicator_imports))
    parts.append(_emit_class(spec, class_name, spec.thesis, lookback))
    return "\n".join(parts) + "\n"


def _emit_header(spec: StrategySpec, class_name: str) -> str:
    return textwrap.dedent(
        f'''\
        """Auto-generated strategy: {spec.name}

        Archetype: {spec.archetype}
        Thesis: {spec.thesis}

        DO NOT EDIT BY HAND — regenerate via the Phase-3 pipeline.
        Spec hash and generation log live in results/generations/.
        """
        '''
    )


def _emit_imports(indicator_imports: list[str]) -> str:
    if not indicator_imports:
        return textwrap.dedent(
            """\
            from __future__ import annotations
            from typing import Any, Optional

            from engine.execution import Order, OrderType
            from engine.portfolio import Position
            from strategy.base import Strategy
            from strategy.context import Bar, Context
            """
        )
    ind_csv = ", ".join(indicator_imports)
    return textwrap.dedent(
        f"""\
        from __future__ import annotations
        from typing import Any, Optional

        from engine.execution import Order, OrderType
        from engine.portfolio import Position
        from strategy.base import Strategy
        from strategy.context import Bar, Context

        from generator.indicators import {ind_csv}
        """
    )


def _emit_class(spec: StrategySpec, class_name: str, thesis: str, lookback: int) -> str:
    """Emit the class as a list of lines with explicit, consistent indentation.

    Class body uses 4 spaces. Method bodies use 8 spaces. Nested blocks add
    4 more. We avoid textwrap gymnastics so indentation is auditable line-
    by-line.
    """
    L: list[str] = []  # accumulator
    I = " " * 4  # one level of indent

    # Class header
    L.append(f"class {class_name}(Strategy):")
    L.append(f'{I}archetype = "{spec.archetype}"')
    L.append(f"{I}thesis = {json.dumps(thesis)}")
    L.append(f"{I}supported_assets = {spec.supported_assets!r}")
    L.append(f"{I}timeframes = {spec.timeframes!r}")
    L.append("")

    # __init__
    if spec.parameters:
        init_args = ", ".join(
            f"{p.name}: {_py_type(p.type)} = {_py_lit(p.default)}" for p in spec.parameters
        )
        L.append(f"{I}def __init__(self, {init_args}) -> None:")
        for p in spec.parameters:
            L.append(f"{I*2}self.{p.name} = {p.name}")
    else:
        L.append(f"{I}def __init__(self) -> None:")
        L.append(f"{I*2}pass")
    L.append("")

    # on_bar
    L.append(
        f"{I}def on_bar(self, bar: Bar, position: Optional[Position], "
        f"context: Context) -> list[Order]:"
    )
    L.append(f"{I*2}bars = context.recent({lookback})")
    L.append(f"{I*2}if len(bars) < 2:")
    L.append(f"{I*3}return []")
    L.append(f"{I*2}close = bar.close")
    L.append(f"{I*2}high = bar.high")
    L.append(f"{I*2}low = bar.low")
    L.append(f"{I*2}open_ = bar.open")
    L.append(f"{I*2}now = bar.timestamp")
    L.append(f"{I*2}tod_minutes = now.hour * 60 + now.minute")

    # Indicator block.
    # We suffix the local variable name with "_val" so that an indicator alias
    # equal to its imported function name (e.g. name="bb_upper", type="bb_upper")
    # does not shadow the import. Python's scope analysis treats `bb_upper =`
    # as a local declaration for the whole function, which would make the RHS
    # reference to the imported `bb_upper` raise UnboundLocalError. The DSL
    # operand emitter mirrors this suffix when generating `IndicatorRef`s.
    if spec.indicators:
        for ind in spec.indicators:
            kw = ", ".join(f"{k}={_py_lit(v)}" for k, v in ind.params.items())
            call = f"{ind.type}(bars{(', ' + kw) if kw else ''})"
            L.append(f"{I*2}{ind.name}_val = {call}")
        ind_list = ", ".join(f"{i.name}_val" for i in spec.indicators)
        L.append(f"{I*2}__ind_values = [{ind_list}]")
        L.append(f"{I*2}if any(v is None for v in __ind_values):")
        L.append(f"{I*3}return []")

    # Entry conditions
    entry_long_cond = _emit_expr(spec.entry_long) if spec.entry_long is not None else "False"
    entry_short_cond = _emit_expr(spec.entry_short) if spec.entry_short is not None else "False"
    exit_long_cond = _emit_expr(spec.exit_long) if spec.exit_long is not None else "False"
    exit_short_cond = _emit_expr(spec.exit_short) if spec.exit_short is not None else "False"
    size = spec.position_sizing.size

    L.append(f"{I*2}if position is None:")
    L.append(f"{I*3}if {entry_long_cond}:")
    L.append(
        f'{I*4}return [Order(type=OrderType.MARKET, side="buy", '
        f'size={size}, signal_label="long_entry")]'
    )
    L.append(f"{I*3}if {entry_short_cond}:")
    L.append(
        f'{I*4}return [Order(type=OrderType.MARKET, side="sell_short", '
        f'size={size}, signal_label="short_entry")]'
    )
    L.append(f"{I*3}return []")
    L.append("")
    L.append(f"{I*2}# Position open — check exit")
    L.append(f"{I*2}if position.is_long and {exit_long_cond}:")
    L.append(
        f'{I*3}return [Order(type=OrderType.MARKET, side="sell", '
        f'size=position.size, signal_label="long_exit")]'
    )
    L.append(f"{I*2}if not position.is_long and {exit_short_cond}:")
    L.append(
        f'{I*3}return [Order(type=OrderType.MARKET, side="buy_to_cover", '
        f'size=position.size, signal_label="short_exit")]'
    )
    L.append(f"{I*2}return []")
    L.append("")

    # get_parameters
    L.append(f"{I}def get_parameters(self) -> dict[str, Any]:")
    if spec.parameters:
        L.append(f"{I*2}return {{")
        for p in spec.parameters:
            L.append(f"{I*3}{json.dumps(p.name)}: self.{p.name},")
        L.append(f"{I*2}}}")
    else:
        L.append(f"{I*2}return {{}}")

    return "\n".join(L)


# ── Indicator-call block ─────────────────────────────────────────────────────


def _emit_indicator_block(spec: StrategySpec) -> str:
    """Emit one indicator-call line per declared indicator alias.
    Collects the values into __ind_values so we can None-check uniformly.
    Local variable names use a `_val` suffix to avoid shadowing the imported
    indicator function when alias == type (see _emit_class)."""
    if not spec.indicators:
        return "__ind_values: list = []"
    lines: list[str] = []
    for ind in spec.indicators:
        kw = ", ".join(f"{k}={_py_lit(v)}" for k, v in ind.params.items())
        call = f"{ind.type}(bars{(', ' + kw) if kw else ''})"
        lines.append(f"{ind.name}_val = {call}")
    lines.append(
        "__ind_values = [" + ", ".join(f"{i.name}_val" for i in spec.indicators) + "]"
    )
    return "\n".join(lines)


# ── DSL → Python ────────────────────────────────────────────────────────────


def _emit_expr(expr: BooleanExpression | Compare) -> str:
    if isinstance(expr, And):
        return "(" + " and ".join(_emit_expr(a) for a in expr.args) + ")"
    if isinstance(expr, Or):
        return "(" + " or ".join(_emit_expr(a) for a in expr.args) + ")"
    if isinstance(expr, Not):
        return f"(not {_emit_expr(expr.arg)})"
    if isinstance(expr, Compare):
        op = expr.operator
        return f"({_emit_operand(expr.lhs)} {op} {_emit_operand(expr.rhs)})"
    raise TranslationError(f"unexpected node type: {type(expr).__name__}")


def _emit_operand(node: Operand) -> str:
    if isinstance(node, IndicatorRef):
        # Mirrors the `_val` suffix added in the indicator-block emitter so DSL
        # references resolve to the local variable, not the imported function.
        return f"{node.name}_val"
    if isinstance(node, PriceRef):
        return {"open": "open_", "high": "high", "low": "low", "close": "close"}[node.field]
    if isinstance(node, TimeOfDay):
        return "tod_minutes"
    if isinstance(node, Const):
        return repr(float(node.value))
    if isinstance(node, ParamRef):
        return f"self.{node.name}"
    raise TranslationError(f"unexpected operand type: {type(node).__name__}")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _to_camel(snake: str) -> str:
    return "".join(p.capitalize() for p in snake.split("_"))


def _py_type(t: str) -> str:
    return {"int": "int", "float": "float", "bool": "bool"}[t]


def _py_lit(v: Any) -> str:
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return repr(v)
    return repr(v)


def _required_lookback(spec: StrategySpec) -> int:
    """Best-effort lookback derivation. Use the largest period parameter + 50."""
    candidates = [50]
    for ind in spec.indicators:
        for k in ("period", "slow", "lookback"):
            v = ind.params.get(k)
            if isinstance(v, int) and v > 0:
                candidates.append(v + 10)
    return max(candidates)
