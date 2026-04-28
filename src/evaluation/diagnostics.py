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

from engine.session import RegularTradingHours, Session
from generator.indicators import INDICATOR_FUNCTIONS
from generator.spec_recovery import recover_stringified_dsl_fields
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
    output, which may contain stringified DSL fields. We route through
    `recover_stringified_dsl_fields` (the canonical helper used by the spec
    validator too) so the safety net + counter live in exactly one place."""
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
    return recover_stringified_dsl_fields(
        spec,
        model=payload.get("model", "unknown"),
        archetype=payload.get("archetype"),
    )


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


# ── Public API ───────────────────────────────────────────────────────────────


def diagnose_signal_frequency(
    strategy_class: Type[Strategy],
    symbol: str,
    start: datetime | date | None = None,
    end: datetime | date | None = None,
    session: Session | None = None,
) -> dict:
    """Report per-sub-clause and full-condition entry hit counts.

    The bar walk mirrors the backtester's session-reset behavior: the
    indicator window resets at every session boundary (RegularTradingHours
    by default). Indicators are computed on the same per-session window the
    strategy actually sees in production, so a daily-period indicator on
    intraday data correctly reports as "never warm" instead of warming up
    after the first 100 bars of a 4-year continuous slice.

    Returns a top-level dict with:
      * `bars_with_warm_indicators`: count of bars where every declared
        indicator returned a value within the current session's window
      * `warm_ratio`: bars_with_warm_indicators / n_bars — quick read on
        timeframe/session-length mismatch (a value of 0.0 with a non-zero
        period indicator is the smoking gun of a daily-on-intraday issue)
      * `sides`: one entry per side ("long"/"short") with non-null entry
        condition. Each reports clauses, clause_hits, full_hits,
        n_evaluable_bars (== bars_with_warm_indicators), and
        ratio_full_to_min_clause (close to 1 = AND adds little; close to
        0 = clauses rarely co-occur — the diagnostic we care about most).
    """
    if session is None:
        session = RegularTradingHours()
    spec = _load_spec_for(strategy_class)
    df = train_test_load(symbol)
    df = _slice_to_window(df, start, end)
    bars = _df_to_bars(df)

    if not bars:
        return {"error": f"no bars for {symbol} in window [{start}, {end})"}

    indicator_specs = spec.get("indicators", [])
    params = {p["name"]: p["default"] for p in spec.get("parameters", [])}

    # Single pass: walk per-session, mirroring backtester's session_bars
    # reset. Per bar, record whether all indicators warmed up within the
    # current session's window, and cache their values for the side eval.
    warm_per_bar: list[bool] = []
    ind_values_per_bar: list[dict | None] = []
    session_bars: list[Bar] = []
    prev_ts: datetime | None = None
    for bar in bars:
        if session.is_session_start(bar.timestamp, prev_ts):
            session_bars = []
        session_bars.append(bar)
        ind_values: dict[str, Any] = {}
        ok = True
        for ind in indicator_specs:
            fn = INDICATOR_FUNCTIONS[ind["type"]]
            v = fn(session_bars, **ind.get("params", {}))
            if v is None:
                ok = False
                break
            ind_values[ind["name"]] = v
        warm_per_bar.append(ok)
        ind_values_per_bar.append(ind_values if ok else None)
        prev_ts = bar.timestamp

    n_warm = sum(warm_per_bar)

    sides: dict[str, Any] = {}
    for side_key in ("entry_long", "entry_short"):
        entry = spec.get(side_key)
        if entry is None:
            continue
        sub_clauses = _split_top_level(entry)
        clause_hits = [0] * len(sub_clauses)
        full_hits = 0

        for i, bar in enumerate(bars):
            if not warm_per_bar[i]:
                continue
            ind_values = ind_values_per_bar[i]
            try:
                if _eval_dsl(entry, ind_values, params, bar):
                    full_hits += 1
            except Exception as e:  # defensive — never break the diag
                logger.debug("diag full-eval error on bar %d: %s", i, e)
            for j, clause in enumerate(sub_clauses):
                try:
                    if _eval_dsl(clause, ind_values, params, bar):
                        clause_hits[j] += 1
                except Exception as e:
                    logger.debug("diag clause-eval error clause=%d bar=%d: %s", j, i, e)

        min_clause = min(clause_hits) if clause_hits else 0
        ratio = (full_hits / min_clause) if min_clause > 0 else None
        sides[side_key.removeprefix("entry_")] = {
            "clauses": [_render_clause(c) for c in sub_clauses],
            "clause_hits": clause_hits,
            "full_hits": full_hits,
            "n_evaluable_bars": n_warm,
            "ratio_full_to_min_clause": ratio,
        }

    return {
        "symbol": symbol,
        "n_bars": len(bars),
        "bars_with_warm_indicators": n_warm,
        "warm_ratio": (n_warm / len(bars)) if bars else 0.0,
        "sides": sides,
    }
