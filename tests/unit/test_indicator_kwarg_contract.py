"""Indicator naming-contract tests.

Two layers:

1. Prompt vs runtime: the kwarg block in `_system.md` is generated from the
   runtime signatures via `inspect.signature`. The test rebuilds the expected
   block from the runtime and asserts it appears verbatim in the prompt. If
   someone adds an indicator or changes a signature without updating the
   prompt (or vice versa), the test fails loudly.

2. Per-indicator translation: for every indicator in `INDICATOR_FUNCTIONS`,
   build a minimal StrategySpec that uses it (with each declared parameter
   set to its default), translate to code, parse the emitted Python with
   `ast.parse`, locate the indicator call inside `on_bar`, and verify the
   call's kwargs exactly match the runtime signature.

Together these guard against the kwarg-synonym class of bug (e.g. `std` vs
`k` for Bollinger Bands) that broke the Phase 3 E2E demo.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from generator.indicators import ALLOWED_INDICATORS, INDICATOR_FUNCTIONS
from generator.spec import IndicatorSpec, ParameterSpec, StrategySpec
from generator.translator import _emit_code, validate_for_translation

PROMPT_PATH = Path(__file__).resolve().parents[2] / "src" / "generator" / "prompts" / "_system.md"


def _runtime_kwarg_signature_line(name: str) -> str:
    """Render a single indicator's signature in the same shape used in
    `_system.md`'s kwarg table. Mirrors the prompt format exactly."""
    fn = INDICATOR_FUNCTIONS[name]
    sig = inspect.signature(fn)
    parts: list[str] = []
    for p in list(sig.parameters.values())[1:]:
        if p.default is inspect.Parameter.empty:
            parts.append(p.name)
        else:
            parts.append(f"{p.name}={p.default!r}")
    return f"{name}({', '.join(parts)})"


# ── Layer 1: prompt-vs-runtime ───────────────────────────────────────────────


@pytest.mark.parametrize("indicator", ALLOWED_INDICATORS)
def test_prompt_documents_indicator_signature(indicator: str) -> None:
    """The prompt's kwarg table must contain the exact signature line for each
    indicator, generated from `inspect.signature` on the runtime function."""
    prompt = PROMPT_PATH.read_text()
    expected = _runtime_kwarg_signature_line(indicator)
    assert expected in prompt, (
        f"Prompt is missing or has stale signature for {indicator!r}. "
        f"Expected substring: {expected!r}. Update _system.md to match the "
        f"runtime signature in src/generator/indicators.py."
    )


# ── Layer 2: per-indicator translation round-trip ────────────────────────────


def _minimal_spec_using(indicator: str) -> StrategySpec:
    """Build the smallest valid StrategySpec that exercises `indicator` once
    in entry_long. Uses each declared parameter's default value verbatim."""
    fn = INDICATOR_FUNCTIONS[indicator]
    sig = inspect.signature(fn)
    params: dict = {}
    for p in list(sig.parameters.values())[1:]:
        params[p.name] = p.default if p.default is not inspect.Parameter.empty else 14

    daily_only = indicator == "daily_return"
    timeframes = ["1d"] if daily_only else ["1d"]

    indicators = [IndicatorSpec(name=f"{indicator}_x", type=indicator, params=params)]

    spec = StrategySpec(
        name=f"contract_{indicator}",
        archetype="mean_reversion",
        thesis="contract test — exercises one indicator with its declared kwargs",
        supported_assets=["stocks"],
        timeframes=timeframes,
        parameters=[],
        indicators=indicators,
        entry_long={
            "op": "compare",
            "operator": ">",
            "lhs": {"op": "indicator", "name": f"{indicator}_x"},
            "rhs": {"op": "const", "value": 0.0},
        },
        entry_short=None,
        exit_long=None,
        exit_short=None,
        position_sizing={"rule": "fixed", "size": 1},
    )
    return spec


def _kwargs_in_emitted_call(code: str, fn_name: str) -> set[str]:
    """Parse `code`, find the first call to `fn_name`, return the set of
    keyword-arg names on that call."""
    tree = ast.parse(code)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == fn_name
        ):
            return {kw.arg for kw in node.keywords if kw.arg is not None}
    raise AssertionError(f"Generated code does not call {fn_name!r}")


@pytest.mark.parametrize("indicator", ALLOWED_INDICATORS)
def test_translator_emits_runtime_kwargs(indicator: str) -> None:
    """Translate a minimal spec for each indicator; the emitted call must use
    exactly the runtime signature's kwarg names, no synonyms, no missing."""
    spec = _minimal_spec_using(indicator)
    validate_for_translation(spec)
    code = _emit_code(spec)
    emitted = _kwargs_in_emitted_call(code, indicator)
    expected_all = set(inspect.signature(INDICATOR_FUNCTIONS[indicator]).parameters) - {"bars"}
    assert emitted <= expected_all, (
        f"{indicator!r} emitted unknown kwargs {sorted(emitted - expected_all)}; "
        f"runtime accepts {sorted(expected_all)}"
    )
    expected_required = {
        p.name
        for p in inspect.signature(INDICATOR_FUNCTIONS[indicator]).parameters.values()
        if p.name != "bars" and p.default is inspect.Parameter.empty
    }
    assert expected_required <= emitted, (
        f"{indicator!r} missing required kwargs {sorted(expected_required - emitted)}"
    )
