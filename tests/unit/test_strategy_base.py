"""Strategy ABC metadata-enforcement tests."""

from __future__ import annotations

from typing import Any, Optional

import pytest

from engine.execution import Order
from engine.portfolio import Position
from strategy.base import Strategy
from strategy.context import Bar, Context


def test_subclass_missing_archetype_raises():
    with pytest.raises(TypeError, match="archetype"):

        class Bad(Strategy):
            thesis = "hi"
            supported_assets = ["stocks"]
            timeframes = ["5m"]

            def on_bar(self, bar, position, context):
                return []

            def get_parameters(self):
                return {}


def test_subclass_missing_thesis_raises():
    with pytest.raises(TypeError, match="thesis"):

        class Bad(Strategy):
            archetype = "x"
            supported_assets = ["stocks"]
            timeframes = ["5m"]

            def on_bar(self, bar, position, context):
                return []

            def get_parameters(self):
                return {}


def test_concrete_strategy_constructs():
    class OK(Strategy):
        archetype = "x"
        thesis = "y"
        supported_assets = ["stocks"]
        timeframes = ["5m"]

        def on_bar(self, bar, position, context):
            return []

        def get_parameters(self):
            return {"a": 1}

    inst = OK()
    assert inst.get_parameters() == {"a": 1}
