"""Slippage policy tests."""

from __future__ import annotations

import pytest

from engine.execution import FillConfig, FillReason, apply_entry_slippage, apply_exit_slippage


def test_entry_slippage_buy():
    cfg = FillConfig(slippage=0.01)
    assert apply_entry_slippage(100.0, "buy", cfg) == pytest.approx(100.01)


def test_entry_slippage_sell_short():
    cfg = FillConfig(slippage=0.01)
    assert apply_entry_slippage(100.0, "sell_short", cfg) == pytest.approx(99.99)


def test_entry_slippage_zero():
    cfg = FillConfig(slippage=0)
    assert apply_entry_slippage(100.0, "buy", cfg) == 100.0


def test_exit_slippage_long_signal_realistic():
    cfg = FillConfig(slippage=0.01, realistic_fills=True)
    assert apply_exit_slippage(100.0, True, FillReason.SIGNAL_EXIT, cfg) == pytest.approx(99.99)


def test_exit_slippage_short_signal_realistic():
    cfg = FillConfig(slippage=0.01, realistic_fills=True)
    assert apply_exit_slippage(100.0, False, FillReason.SIGNAL_EXIT, cfg) == pytest.approx(100.01)


def test_stop_no_slippage_in_regression_mode():
    cfg = FillConfig(slippage=0.01, realistic_fills=False)
    assert apply_exit_slippage(100.0, True, FillReason.STOP, cfg) == 100.0
    assert apply_exit_slippage(100.0, False, FillReason.STOP, cfg) == 100.0


def test_target_no_slippage_in_regression_mode():
    cfg = FillConfig(slippage=0.01, realistic_fills=False)
    assert apply_exit_slippage(100.0, True, FillReason.TARGET, cfg) == 100.0


def test_stop_with_slippage_in_realistic_mode():
    cfg = FillConfig(slippage=0.01, realistic_fills=True)
    assert apply_exit_slippage(100.0, True, FillReason.STOP, cfg) == pytest.approx(99.99)


def test_eod_always_has_slippage():
    cfg = FillConfig(slippage=0.01, realistic_fills=False)
    assert apply_exit_slippage(100.0, True, FillReason.EOD, cfg) == pytest.approx(99.99)
