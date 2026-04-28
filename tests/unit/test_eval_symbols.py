"""Symbol roster tests."""

from __future__ import annotations

import json

import pytest

from evaluation.symbols import (
    SP500_SUBSET,
    sp500_random_subset,
    sp500_with_required,
    save_symbol_list,
    load_symbol_list,
    top_crypto,
    CRYPTO_TOP_15,
)


def test_sp500_subset_size():
    assert len(SP500_SUBSET) >= 50
    assert len(SP500_SUBSET) == len(set(SP500_SUBSET))


def test_random_subset_seeded_reproducible():
    a = sp500_random_subset(n=10, seed=1)
    b = sp500_random_subset(n=10, seed=1)
    assert a == b


def test_random_subset_different_seeds_differ():
    a = sp500_random_subset(n=10, seed=1)
    b = sp500_random_subset(n=10, seed=2)
    assert a != b


def test_sp500_with_required_includes_required():
    picks = sp500_with_required(required=["AMD", "NVDA"], n=10, seed=42)
    assert "AMD" in picks
    assert "NVDA" in picks
    assert len(picks) == 10
    assert len(set(picks)) == 10


def test_sp500_with_required_rejects_unknown():
    with pytest.raises(ValueError, match="not in roster"):
        sp500_with_required(required=["NOTREAL"], n=10, seed=0)


def test_top_crypto_excludes_stablecoins():
    syms = top_crypto(n=10)
    for s in syms:
        assert "USDT" not in s and "USDC" not in s
    assert len(syms) == 10


def test_save_load_roundtrip(tmp_path):
    path = tmp_path / "list.json"
    save_symbol_list(["AMD", "NVDA"], path, seed=42, source="test")
    assert load_symbol_list(path) == ["AMD", "NVDA"]
    payload = json.loads(path.read_text())
    assert payload["seed"] == 42
    assert payload["source"] == "test"
