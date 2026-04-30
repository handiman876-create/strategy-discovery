"""Strategy hashing.

Two functions live here during the step-10 transition:

* `compute_strategy_hash(spec)` — STRUCTURAL hash. SHA-256 over a
  canonicalized JSON representation of the spec. Two textually different
  specs that describe the same logic produce the same hash. Differences
  in operators, thresholds, alias names, or archetype produce different
  hashes. This is the active dedup primitive going forward.

* `behavioral_hash(strategy_class)` — BEHAVIORAL hash (legacy, deprecated).
  Runs the strategy on a small fixture and hashes the trade list. The
  step-10 audit revealed this collapses unrelated specs to the same hash
  whenever they produce zero trades on the fixture (`sha256("[]")`),
  so it is being replaced. Kept here through commit 2 of the migration
  to support a clean cutover; removed in commit 3.

# Canonicalization rules (compute_strategy_hash)

Each canonicalizer enforces a strict KNOWN_FIELDS allowlist. If an input
dict carries a key not in KNOWN_FIELDS, we raise UnknownFieldError with
a message instructing the maintainer to add the field and decide whether
it affects strategy identity (hash inclusion) or not (hash exclusion).

This loud-fail-on-mismatch policy is the project's defense against
silent-drop drift. The behavioral_hash collapse this module is replacing
is itself an example of silent drift — the drift was in fixture-vs-spec
diversity rather than fields, but the failure mode (unrelated things
hashing the same) is the same shape.

Field-level decisions:

* `name`, `thesis`, parameter `description` — EXCLUDED. Free metadata,
  not strategy logic.
* `archetype` — INCLUDED. Categorical label, stable. Excluding would
  re-create cross-archetype identity collisions for logically-identical
  specs prompted under different archetype labels.
* indicator alias names — SIGNIFICANT. Two specs with identical logic
  but different alias names (e.g. `rsi_short` vs `rsi_2`) hash
  DIFFERENTLY. Alias normalization (renaming aliases to a canonical
  form derived from `(type, params)` and rewriting all DSL refs) is
  not implemented — it's strictly more complex and we can layer it on
  later if observability shows alias variation creates significant
  noise. Do not "fix" this without first reading the original
  step-10-pivot discussion.
* AND/OR `args` — SORTED (commutative). Reordered branches hash same.
* Compare `operator`/`lhs`/`rhs` — POSITIONAL. `a > b` and `b < a`
  hash differently. This is canonicalization, not normalization;
  semantic operator-rewrites are out of scope for now.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Type

from engine.backtester import BacktestConfig, run_backtest
from engine.session import CryptoSession, RegularTradingHours
from strategy.base import Strategy

from .fixture import fixture_for_timeframe
from .spec import StrategySpec


# ── Strict KNOWN_FIELDS allowlists ───────────────────────────────────────────


class UnknownFieldError(ValueError):
    """Raised when a canonicalizer sees a field not in its KNOWN_FIELDS.
    Loud-fail-on-mismatch over silent-drop. The fix is to add the field
    to the appropriate KNOWN_FIELDS frozenset in this module and decide
    whether the field affects strategy identity (hash inclusion) or not
    (hash exclusion)."""


_KNOWN_SPEC_FIELDS = frozenset({
    "name", "archetype", "thesis", "supported_assets", "timeframes",
    "parameters", "indicators",
    "entry_long", "entry_short", "exit_long", "exit_short",
    "position_sizing",
})

_KNOWN_PARAM_FIELDS = frozenset({
    "name", "type", "default", "range_min", "range_max", "description",
})

_KNOWN_INDICATOR_FIELDS = frozenset({"name", "type", "params"})

_KNOWN_POSITION_SIZING_FIELDS = frozenset({"rule", "size"})

_KNOWN_COMPARE_FIELDS = frozenset({"op", "operator", "lhs", "rhs"})
_KNOWN_AND_OR_FIELDS = frozenset({"op", "args"})
_KNOWN_NOT_FIELDS = frozenset({"op", "arg"})

_KNOWN_INDICATOR_REF_FIELDS = frozenset({"op", "name"})
_KNOWN_PARAM_REF_FIELDS = frozenset({"op", "name"})
_KNOWN_PRICE_REF_FIELDS = frozenset({"op", "field"})
_KNOWN_TIME_OF_DAY_FIELDS = frozenset({"op"})
_KNOWN_CONST_FIELDS = frozenset({"op", "value"})


def _check_keys(d: dict, known: frozenset, what: str) -> None:
    extra = set(d.keys()) - known
    if extra:
        raise UnknownFieldError(
            f"Unknown field(s) {sorted(extra)} in {what} "
            f"(known: {sorted(known)}). Add to KNOWN_FIELDS in "
            f"src/generator/dedup.py and decide whether the field "
            f"affects strategy identity (hash inclusion) or not "
            f"(hash exclusion)."
        )


# ── compute_strategy_hash + canonicalizers ───────────────────────────────────


def compute_strategy_hash(spec: "StrategySpec | dict") -> str:
    """SHA-256 over the canonicalized spec. See module docstring for
    canonicalization rules and field-level decisions.

    Accepts a StrategySpec or a raw dict (e.g. from a generation log).
    StrategySpec inputs are routed through `model_dump(mode="json")`
    so numeric types are normalized via Pydantic's JSON-mode coercion.
    Raw-dict callers are responsible for passing already-normalized
    types — the canonicalizer does not coerce.

    Raises UnknownFieldError if any input dict carries a field not in
    its KNOWN_FIELDS (see _check_keys).
    """
    if isinstance(spec, StrategySpec):
        spec_dict = spec.model_dump(mode="json")
    else:
        spec_dict = spec
    canonical = _canonicalize_spec(spec_dict)
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _canonicalize_spec(spec_dict: dict) -> dict:
    _check_keys(spec_dict, _KNOWN_SPEC_FIELDS, "StrategySpec")
    out: dict[str, Any] = {
        "archetype": spec_dict["archetype"],
        "supported_assets": sorted(spec_dict.get("supported_assets", [])),
        "timeframes": sorted(spec_dict.get("timeframes", [])),
        "parameters": sorted(
            [_canonical_param(p) for p in spec_dict.get("parameters", [])],
            key=lambda p: p["name"],
        ),
        "indicators": sorted(
            [_canonical_indicator(i) for i in spec_dict.get("indicators", [])],
            key=lambda i: i["name"],
        ),
        "position_sizing": _canonical_position_sizing(
            spec_dict.get("position_sizing", {"rule": "fixed", "size": 1})
        ),
    }
    for slot in ("entry_long", "entry_short", "exit_long", "exit_short"):
        v = spec_dict.get(slot)
        out[slot] = _canonical_expr(v) if v is not None else None
    return out


def _canonical_param(p: dict) -> dict:
    _check_keys(p, _KNOWN_PARAM_FIELDS, "ParameterSpec")
    return {
        "name": p["name"],
        "type": p["type"],
        "default": p["default"],
        "range_min": p.get("range_min"),
        "range_max": p.get("range_max"),
        # description excluded (free text, not logic)
    }


def _canonical_indicator(i: dict) -> dict:
    _check_keys(i, _KNOWN_INDICATOR_FIELDS, "IndicatorSpec")
    return {
        "name": i["name"],
        "type": i["type"],
        "params": dict(sorted((i.get("params") or {}).items())),
    }


def _canonical_position_sizing(ps: dict) -> dict:
    _check_keys(ps, _KNOWN_POSITION_SIZING_FIELDS, "PositionSizing")
    return {"rule": ps["rule"], "size": ps["size"]}


def _canonical_expr(node: dict | None) -> dict | None:
    if node is None:
        return None
    op = node.get("op")
    if op == "compare":
        _check_keys(node, _KNOWN_COMPARE_FIELDS, "Compare")
        return {
            "op": "compare",
            "operator": node["operator"],
            "lhs": _canonical_operand(node["lhs"]),
            "rhs": _canonical_operand(node["rhs"]),
        }
    if op in ("and", "or"):
        _check_keys(node, _KNOWN_AND_OR_FIELDS, op.capitalize())
        args = [_canonical_expr(a) for a in node["args"]]
        # AND/OR are commutative — sort args by their canonicalized JSON
        # serialization so reordered branches hash the same.
        args.sort(key=lambda a: json.dumps(a, sort_keys=True))
        return {"op": op, "args": args}
    if op == "not":
        _check_keys(node, _KNOWN_NOT_FIELDS, "Not")
        return {"op": "not", "arg": _canonical_expr(node["arg"])}
    raise UnknownFieldError(
        f"Unknown BooleanExpression op {op!r} (expected 'compare' / 'and' "
        f"/ 'or' / 'not'). Update _canonical_expr in src/generator/dedup.py "
        f"if this is a new node type."
    )


def _canonical_operand(node: dict) -> dict:
    op = node.get("op")
    if op == "indicator":
        _check_keys(node, _KNOWN_INDICATOR_REF_FIELDS, "IndicatorRef")
        return {"op": "indicator", "name": node["name"]}
    if op == "param":
        _check_keys(node, _KNOWN_PARAM_REF_FIELDS, "ParamRef")
        return {"op": "param", "name": node["name"]}
    if op == "price":
        _check_keys(node, _KNOWN_PRICE_REF_FIELDS, "PriceRef")
        return {"op": "price", "field": node["field"]}
    if op == "time_of_day":
        _check_keys(node, _KNOWN_TIME_OF_DAY_FIELDS, "TimeOfDay")
        return {"op": "time_of_day"}
    if op == "const":
        _check_keys(node, _KNOWN_CONST_FIELDS, "Const")
        return {"op": "const", "value": node["value"]}
    raise UnknownFieldError(
        f"Unknown Operand op {op!r} (expected 'indicator' / 'param' / "
        f"'price' / 'time_of_day' / 'const'). Update _canonical_operand "
        f"in src/generator/dedup.py if this is a new operand type."
    )


# ── Behavioral hash (legacy; removed in step-10 commit 3) ─────────────────────


def behavioral_hash(
    strategy_class: Type[Strategy],
    *,
    timeframe: str | None = None,
    starting_capital: float = 10_000.0,
    slippage: float = 0.01,
) -> str:
    """Run `strategy_class` on the fixture and return SHA-256 of its trade
    fingerprint. Returns the same hash for any spec that produces the same
    trades on the same fixture."""
    tf = timeframe or _infer_timeframe(strategy_class)
    bars = fixture_for_timeframe(tf)
    asset_class = "stocks" if "stocks" in getattr(strategy_class, "supported_assets", []) else "crypto"
    cfg = BacktestConfig(
        starting_capital=starting_capital,
        commission=0.0,
        slippage=slippage,
        realistic_fills=True,
        session=RegularTradingHours() if asset_class == "stocks" else CryptoSession(),
    )
    result = run_backtest(strategy_class.__name__, bars, strategy_class(), cfg)

    fingerprint: list[tuple[str, str, str, float]] = []
    for t in result.trades:
        fingerprint.append(
            (
                t.entry_time.isoformat(),
                t.side,
                t.exit_reason,
                round(t.pnl, 4),
            )
        )
    fingerprint.sort()
    payload = json.dumps(fingerprint, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def _infer_timeframe(strategy_class: Type[Strategy]) -> str:
    tfs = getattr(strategy_class, "timeframes", None) or []
    if not tfs:
        raise ValueError(f"{strategy_class.__name__} has no timeframes declared")
    # Pick the lowest-frequency one for the fixture (we have 5m raw → resample up).
    priority = {"1d": 4, "1h": 3, "15m": 2, "5m": 1}
    return sorted(tfs, key=lambda t: priority.get(t, 0))[-1]
