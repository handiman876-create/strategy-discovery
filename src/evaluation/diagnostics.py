"""Per-clause signal-frequency diagnostic for generated strategies.

When a fast eval produces fewer than 10 OOS trades total, the fast pipeline
calls `diagnose_signal_frequency` to answer "why didn't this strategy trade?"
without manual inspection. The diagnostic walks the entry-condition DSL,
splits any top-level AND into its sub-clauses, and reports per-clause hit
counts plus the full-condition hit count over the bar window.

The strategy's StrategySpec is recovered from `results/generations/` by
matching against the class name. The function depends only on:
  * the saved spec JSON (DSL + indicator/parameter declarations),
  * `evaluation.splits.train_test_load` for bar history,
  * `generator.indicators.INDICATOR_FUNCTIONS` for indicator math.

It does not run the strategy's compiled `on_bar`. We re-evaluate the DSL
directly so each sub-clause is observable in isolation.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any, Type

import pandas as pd

from generator.indicators import INDICATOR_FUNCTIONS
from strategy.base import Strategy
from strategy.context import Bar

from .splits import train_test_load

logger = logging.getLogger(__name__)

_GENERATIONS_DIR = Path(__file__).resolve().parents[2] / "results" / "generations"
_MIN_TRADES_FOR_SKIP = 10  # mirrored in fast_pipeline; below this we diagnose


# ── Spec lookup ──────────────────────────────────────────────────────────────


def _camel_to_snake(name: str) -> str:
    out: list[str] = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def _load_spec_for(strategy_class: Type[Strategy]) -> dict:
    """Find the most recent generation file whose spec name matches the class.
    The class name is the CamelCase form of `spec.name`; we glob by suffix.

    The generation file stores `raw_tool_input` — the model's pre-validation
    output. Sonnet 4.6 routinely stringifies entry_long/entry_short/exit_long/
    exit_short (the documented safety-net case). At validation time the
    safety net json.loads them; we mirror that here so the diagnostic walks
    plain dicts."""
    snake = _camel_to_snake(strategy_class.__name__)
    candidates = sorted(_GENERATIONS_DIR.glob(f"*_{snake}.json"))
    if not candidates:
        raise FileNotFoundError(
            f"no generation file found for strategy {strategy_class.__name__!r} "
            f"(expected *_{snake}.json under {_GENERATIONS_DIR})"
        )
    payload = json.loads(candidates[-1].read_text())
    spec = payload.get("raw_tool_input")
    if not isinstance(spec, dict):
        raise ValueError(f"generation file {candidates[-1]} missing raw_tool_input dict")
    for fld in ("entry_long", "entry_short", "exit_long", "exit_short"):
        v = spec.get(fld)
        if isinstance(v, str):
            try:
                spec[fld] = json.loads(v)
            except json.JSONDecodeError:
                spec[fld] = None
    return spec


# ── DSL evaluation ───────────────────────────────────────────────────────────


_OPS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
}


def _eval_operand(node: dict, ind_values: dict, params: dict, bar: Bar) -> Any:
    op = node["op"]
    if op == "indicator":
        return ind_values[node["name"]]
    if op == "price":
        return getattr(bar, node["field"])
    if op == "time_of_day":
        ts = bar.timestamp
        return ts.hour * 60 + ts.minute
    if op == "const":
        return float(node["value"])
    if op == "param":
        return params[node["name"]]
    raise ValueError(f"unknown operand op {op!r}")


def _eval_dsl(node: dict, ind_values: dict, params: dict, bar: Bar) -> bool:
    op = node["op"]
    if op == "compare":
        lhs = _eval_operand(node["lhs"], ind_values, params, bar)
        rhs = _eval_operand(node["rhs"], ind_values, params, bar)
        return _OPS[node["operator"]](lhs, rhs)
    if op == "and":
        return all(_eval_dsl(a, ind_values, params, bar) for a in node["args"])
    if op == "or":
        return any(_eval_dsl(a, ind_values, params, bar) for a in node["args"])
    if op == "not":
        return not _eval_dsl(node["arg"], ind_values, params, bar)
    raise ValueError(f"unknown boolean op {op!r}")


def _split_top_level(entry: dict) -> list[dict]:
    """If the entry condition is a top-level AND, return its args; otherwise
    return the whole node as a single-element list."""
    if entry["op"] == "and":
        return list(entry["args"])
    return [entry]


def _render_clause(node: dict) -> str:
    op = node["op"]
    if op == "compare":
        return f"{_render_operand(node['lhs'])} {node['operator']} {_render_operand(node['rhs'])}"
    if op == "and":
        return "(" + " AND ".join(_render_clause(a) for a in node["args"]) + ")"
    if op == "or":
        return "(" + " OR ".join(_render_clause(a) for a in node["args"]) + ")"
    if op == "not":
        return f"NOT {_render_clause(node['arg'])}"
    return f"<{op}>"


def _render_operand(node: dict) -> str:
    op = node["op"]
    if op == "indicator":
        return node["name"]
    if op == "price":
        return f"price.{node['field']}"
    if op == "time_of_day":
        return "time_of_day"
    if op == "const":
        return repr(float(node["value"]))
    if op == "param":
        return f"param.{node['name']}"
    return f"<{op}>"


# ── Bar plumbing ─────────────────────────────────────────────────────────────


def _df_to_bars(df: pd.DataFrame) -> list[Bar]:
    out: list[Bar] = []
    for row in df.itertuples(index=False):
        out.append(
            Bar(
                timestamp=row.timestamp.to_pydatetime() if hasattr(row.timestamp, "to_pydatetime") else row.timestamp,
                open=float(row.open),
                high=float(row.high),
                low=float(row.low),
                close=float(row.close),
                volume=float(row.volume),
            )
        )
    return out


def _slice_to_window(df: pd.DataFrame, start: datetime | date | None, end: datetime | date | None) -> pd.DataFrame:
    if start is not None:
        ts = pd.Timestamp(start, tz=df["timestamp"].dt.tz) if df["timestamp"].dt.tz is not None else pd.Timestamp(start)
        df = df[df["timestamp"] >= ts]
    if end is not None:
        ts = pd.Timestamp(end, tz=df["timestamp"].dt.tz) if df["timestamp"].dt.tz is not None else pd.Timestamp(end)
        df = df[df["timestamp"] < ts]
    return df.reset_index(drop=True)


def _required_lookback(spec: dict) -> int:
    candidates = [50]
    for ind in spec.get("indicators", []):
        for k in ("period", "slow", "lookback"):
            v = ind.get("params", {}).get(k)
            if isinstance(v, int) and v > 0:
                candidates.append(v + 10)
    return max(candidates)


# ── Public API ───────────────────────────────────────────────────────────────


def diagnose_signal_frequency(
    strategy_class: Type[Strategy],
    symbol: str,
    start: datetime | date | None = None,
    end: datetime | date | None = None,
) -> dict:
    """Report per-sub-clause and full-condition entry hit counts.

    Returns a dict with one entry per side ("long"/"short") that has a
    non-null entry condition. Each side reports:
      * `clauses`: human-readable rendering of each top-level sub-clause
      * `clause_hits`: bars satisfying each sub-clause individually
      * `full_hits`: bars satisfying the full entry condition
      * `n_evaluable_bars`: bars where all indicators were warmed up
      * `ratio_full_to_min_clause`: full_hits / min(clause_hits) — close to
        1.0 means the AND adds little; close to 0 means clauses rarely
        co-occur (this is the diagnostic we care about most).
    """
    spec = _load_spec_for(strategy_class)
    df = train_test_load(symbol)
    df = _slice_to_window(df, start, end)
    bars = _df_to_bars(df)

    if not bars:
        return {"error": f"no bars for {symbol} in window [{start}, {end})"}

    lookback = _required_lookback(spec)
    indicator_specs = spec.get("indicators", [])
    params = {p["name"]: p["default"] for p in spec.get("parameters", [])}

    sides: dict[str, Any] = {}
    for side_key in ("entry_long", "entry_short"):
        entry = spec.get(side_key)
        if entry is None:
            continue
        sub_clauses = _split_top_level(entry)
        clause_hits = [0] * len(sub_clauses)
        full_hits = 0
        evaluable = 0

        for i in range(len(bars)):
            recent = bars[max(0, i - lookback) : i + 1]
            ind_values: dict[str, Any] = {}
            ok = True
            for ind in indicator_specs:
                fn = INDICATOR_FUNCTIONS[ind["type"]]
                v = fn(recent, **ind.get("params", {}))
                if v is None:
                    ok = False
                    break
                ind_values[ind["name"]] = v
            if not ok:
                continue
            evaluable += 1
            try:
                if _eval_dsl(entry, ind_values, params, bars[i]):
                    full_hits += 1
            except Exception as e:  # defensive — never break the diag
                logger.debug("diag full-eval error on bar %d: %s", i, e)
            for j, clause in enumerate(sub_clauses):
                try:
                    if _eval_dsl(clause, ind_values, params, bars[i]):
                        clause_hits[j] += 1
                except Exception as e:
                    logger.debug("diag clause-eval error clause=%d bar=%d: %s", j, i, e)

        min_clause = min(clause_hits) if clause_hits else 0
        ratio = (full_hits / min_clause) if min_clause > 0 else None
        sides[side_key.removeprefix("entry_")] = {
            "clauses": [_render_clause(c) for c in sub_clauses],
            "clause_hits": clause_hits,
            "full_hits": full_hits,
            "n_evaluable_bars": evaluable,
            "ratio_full_to_min_clause": ratio,
        }

    return {
        "symbol": symbol,
        "n_bars": len(bars),
        "lookback": lookback,
        "sides": sides,
    }
