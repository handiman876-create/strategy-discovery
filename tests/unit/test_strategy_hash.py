"""Tests for compute_strategy_hash — the structural hash that's
replacing behavioral_hash in step 10.

Coverage:
  identity / canonicalization (1-6, 16) — same logic, different ordering → same hash
  field-level decisions (7-13)         — what changes the hash, what doesn't
  format / determinism (14-15)         — sha256 hex shape, repeated calls stable
  strict KNOWN_FIELDS (17-20)          — unknown fields raise UnknownFieldError

Tests use raw spec dicts rather than StrategySpec instances so the strict
canonicalizer is exercised directly. compute_strategy_hash routes
StrategySpec inputs through `model_dump(mode="json")`, so the test
material here is what callers like backfill see (raw dicts from JSON).
"""

from __future__ import annotations

import copy

import pytest

from generator.dedup import UnknownFieldError, compute_strategy_hash


def _spec_dict() -> dict:
    """A minimal valid spec dict in the shape `_save_log` produces.
    mean_reversion + 1d so it's compatible with current archetype rules."""
    return {
        "name": "test_mr_strat",
        "archetype": "mean_reversion",
        "thesis": "Buy oversold dips in established uptrends; mean revert in 1-3 days.",
        "supported_assets": ["stocks"],
        "timeframes": ["1d"],
        "parameters": [
            {"name": "rsi_threshold", "type": "float", "default": 5.0,
             "range_min": 1.0, "range_max": 30.0, "description": ""},
        ],
        "indicators": [
            {"name": "rsi_2", "type": "rsi", "params": {"period": 2}},
            {"name": "sma_200", "type": "sma", "params": {"period": 200}},
        ],
        "entry_long": {
            "op": "and",
            "args": [
                {"op": "compare", "operator": ">",
                 "lhs": {"op": "price", "field": "close"},
                 "rhs": {"op": "indicator", "name": "sma_200"}},
                {"op": "compare", "operator": "<",
                 "lhs": {"op": "indicator", "name": "rsi_2"},
                 "rhs": {"op": "param", "name": "rsi_threshold"}},
            ],
        },
        "entry_short": None,
        "exit_long": {
            "op": "compare", "operator": ">",
            "lhs": {"op": "indicator", "name": "rsi_2"},
            "rhs": {"op": "const", "value": 70.0},
        },
        "exit_short": None,
        "position_sizing": {"rule": "fixed", "size": 1},
    }


# ── 1-6, 16: canonicalization (same logic → same hash) ──────────────────────


def test_1_identical_dict_twice_same_hash():
    s = _spec_dict()
    assert compute_strategy_hash(s) == compute_strategy_hash(_spec_dict())


def test_2_reorder_indicators_same_hash():
    a = _spec_dict()
    b = _spec_dict()
    b["indicators"] = list(reversed(b["indicators"]))
    assert compute_strategy_hash(a) == compute_strategy_hash(b)


def test_3_reorder_parameters_same_hash():
    a = _spec_dict()
    a["parameters"] = [
        {"name": "rsi_threshold", "type": "float", "default": 5.0,
         "range_min": 1.0, "range_max": 30.0, "description": ""},
        {"name": "exit_threshold", "type": "float", "default": 70.0,
         "range_min": 50.0, "range_max": 90.0, "description": ""},
    ]
    b = copy.deepcopy(a)
    b["parameters"] = list(reversed(b["parameters"]))
    assert compute_strategy_hash(a) == compute_strategy_hash(b)


def test_4_reorder_and_args_same_hash():
    a = _spec_dict()
    b = copy.deepcopy(a)
    b["entry_long"]["args"] = list(reversed(b["entry_long"]["args"]))
    assert compute_strategy_hash(a) == compute_strategy_hash(b)


def test_5_reorder_or_args_same_hash():
    a = _spec_dict()
    a["entry_long"] = {
        "op": "or",
        "args": [
            {"op": "compare", "operator": ">",
             "lhs": {"op": "price", "field": "close"},
             "rhs": {"op": "indicator", "name": "sma_200"}},
            {"op": "compare", "operator": "<",
             "lhs": {"op": "indicator", "name": "rsi_2"},
             "rhs": {"op": "const", "value": 30.0}},
            {"op": "compare", "operator": ">",
             "lhs": {"op": "indicator", "name": "rsi_2"},
             "rhs": {"op": "const", "value": 70.0}},
        ],
    }
    b = copy.deepcopy(a)
    b["entry_long"]["args"] = list(reversed(b["entry_long"]["args"]))
    assert compute_strategy_hash(a) == compute_strategy_hash(b)


def test_6_reorder_nested_and_inside_or_same_hash():
    a = _spec_dict()
    a["entry_long"] = {
        "op": "or",
        "args": [
            {"op": "and", "args": [
                {"op": "compare", "operator": ">",
                 "lhs": {"op": "indicator", "name": "rsi_2"},
                 "rhs": {"op": "const", "value": 70.0}},
                {"op": "compare", "operator": "<",
                 "lhs": {"op": "indicator", "name": "sma_200"},
                 "rhs": {"op": "const", "value": 100.0}},
            ]},
            {"op": "compare", "operator": ">",
             "lhs": {"op": "price", "field": "close"},
             "rhs": {"op": "const", "value": 50.0}},
        ],
    }
    b = copy.deepcopy(a)
    # Reverse the outer OR's args.
    b["entry_long"]["args"] = list(reversed(b["entry_long"]["args"]))
    # Reverse the inner AND's args (now at position [-1] in b after the
    # outer reversal).
    inner = next(arg for arg in b["entry_long"]["args"] if arg["op"] == "and")
    inner["args"] = list(reversed(inner["args"]))
    assert compute_strategy_hash(a) == compute_strategy_hash(b)


def test_16_swap_commutative_tree_at_depth_3():
    """AND inside OR inside top-level AND. Reorder the deepest AND's
    args. Hash must remain stable — recursion correctness check."""
    a = _spec_dict()
    a["entry_long"] = {
        "op": "and",
        "args": [
            {"op": "or", "args": [
                {"op": "and", "args": [
                    {"op": "compare", "operator": ">",
                     "lhs": {"op": "indicator", "name": "rsi_2"},
                     "rhs": {"op": "const", "value": 50.0}},
                    {"op": "compare", "operator": "<",
                     "lhs": {"op": "indicator", "name": "sma_200"},
                     "rhs": {"op": "const", "value": 200.0}},
                ]},
                {"op": "compare", "operator": "==",
                 "lhs": {"op": "price", "field": "close"},
                 "rhs": {"op": "const", "value": 100.0}},
            ]},
            {"op": "compare", "operator": ">",
             "lhs": {"op": "price", "field": "high"},
             "rhs": {"op": "indicator", "name": "rsi_2"}},
        ],
    }
    b = copy.deepcopy(a)
    inner_and = b["entry_long"]["args"][0]["args"][0]
    inner_and["args"] = list(reversed(inner_and["args"]))
    assert compute_strategy_hash(a) == compute_strategy_hash(b)


# ── 7-9: logic differences → different hashes ───────────────────────────────


def test_7_change_compare_operator_differs():
    a = _spec_dict()
    b = copy.deepcopy(a)
    b["exit_long"]["operator"] = "<"  # was ">"
    assert compute_strategy_hash(a) != compute_strategy_hash(b)


def test_8_change_parameter_default_differs():
    a = _spec_dict()
    b = copy.deepcopy(a)
    b["parameters"][0]["default"] = 6.0  # was 5.0
    assert compute_strategy_hash(a) != compute_strategy_hash(b)


def test_9_change_rsi_period_differs():
    a = _spec_dict()
    b = copy.deepcopy(a)
    b["indicators"][0]["params"]["period"] = 4  # was 2
    assert compute_strategy_hash(a) != compute_strategy_hash(b)


# ── 10-13: alias / metadata field decisions ─────────────────────────────────


def test_10_change_indicator_alias_differs():
    """Alias names are SIGNIFICANT (per design choice). Two specs with
    identical logic but different alias names hash differently. See
    src/generator/dedup.py module docstring for the trade-off."""
    a = _spec_dict()
    b = copy.deepcopy(a)
    # Rename rsi_2 → rsi_short throughout (alias + DSL refs).
    b["indicators"][0]["name"] = "rsi_short"
    b["entry_long"]["args"][1]["lhs"]["name"] = "rsi_short"
    b["exit_long"]["lhs"]["name"] = "rsi_short"
    assert compute_strategy_hash(a) != compute_strategy_hash(b)


def test_11_change_top_level_name_same_hash():
    """`name` is metadata (filename / class derivation), not logic."""
    a = _spec_dict()
    b = copy.deepcopy(a)
    b["name"] = "renamed_strategy"
    assert compute_strategy_hash(a) == compute_strategy_hash(b)


def test_12_change_thesis_same_hash():
    """`thesis` is free-form documentation, not logic."""
    a = _spec_dict()
    b = copy.deepcopy(a)
    b["thesis"] = "Different thesis text but same logic underneath."
    assert compute_strategy_hash(a) == compute_strategy_hash(b)


def test_13_change_archetype_differs():
    """Archetype IS hashed (categorical label, prevents cross-archetype
    identity collisions for logically-identical specs)."""
    a = _spec_dict()
    b = copy.deepcopy(a)
    b["archetype"] = "momentum"
    assert compute_strategy_hash(a) != compute_strategy_hash(b)


def test_change_param_description_same_hash():
    """description on ParameterSpec is free text, excluded from hash."""
    a = _spec_dict()
    b = copy.deepcopy(a)
    b["parameters"][0]["description"] = "any text whatsoever"
    assert compute_strategy_hash(a) == compute_strategy_hash(b)


# ── 14-15: format and determinism ───────────────────────────────────────────


def test_14_hash_is_64_char_sha256_hex():
    h = compute_strategy_hash(_spec_dict())
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_15_hash_deterministic_across_calls():
    h1 = compute_strategy_hash(_spec_dict())
    h2 = compute_strategy_hash(_spec_dict())
    h3 = compute_strategy_hash(_spec_dict())
    assert h1 == h2 == h3


# ── Strict KNOWN_FIELDS contract ────────────────────────────────────────────


def test_17_unknown_top_level_field_raises():
    s = _spec_dict()
    s["new_unhandled_field"] = "whatever"
    with pytest.raises(UnknownFieldError) as exc:
        compute_strategy_hash(s)
    assert "StrategySpec" in str(exc.value)
    assert "new_unhandled_field" in str(exc.value)
    assert "src/generator/dedup.py" in str(exc.value)


def test_18_unknown_parameter_field_raises():
    s = _spec_dict()
    s["parameters"][0]["new_param_attribute"] = "x"
    with pytest.raises(UnknownFieldError) as exc:
        compute_strategy_hash(s)
    assert "ParameterSpec" in str(exc.value)
    assert "new_param_attribute" in str(exc.value)


def test_19_unknown_indicator_field_raises():
    s = _spec_dict()
    s["indicators"][0]["new_indicator_attr"] = True
    with pytest.raises(UnknownFieldError) as exc:
        compute_strategy_hash(s)
    assert "IndicatorSpec" in str(exc.value)
    assert "new_indicator_attr" in str(exc.value)


def test_20_unknown_boolean_expression_op_raises():
    s = _spec_dict()
    s["entry_long"] = {"op": "xor", "args": []}  # invented op
    with pytest.raises(UnknownFieldError) as exc:
        compute_strategy_hash(s)
    assert "xor" in str(exc.value)
    assert "BooleanExpression" in str(exc.value)


def test_unknown_operand_op_raises():
    s = _spec_dict()
    s["exit_long"]["lhs"] = {"op": "volume", "field": "v"}  # invented operand
    with pytest.raises(UnknownFieldError) as exc:
        compute_strategy_hash(s)
    assert "volume" in str(exc.value)
    assert "Operand" in str(exc.value)


def test_unknown_compare_field_raises():
    s = _spec_dict()
    s["exit_long"]["new_compare_attr"] = "x"
    with pytest.raises(UnknownFieldError) as exc:
        compute_strategy_hash(s)
    assert "Compare" in str(exc.value)
    assert "new_compare_attr" in str(exc.value)


# ── Sanity: StrategySpec input also works ───────────────────────────────────


def test_accepts_strategy_spec_instance():
    """compute_strategy_hash accepts a StrategySpec; round-trips through
    model_dump(mode='json') so types are normalized."""
    from generator.spec import StrategySpec

    spec = StrategySpec.model_validate(_spec_dict())
    h_from_spec = compute_strategy_hash(spec)
    h_from_dict = compute_strategy_hash(spec.model_dump(mode="json"))
    assert h_from_spec == h_from_dict
