"""Typed parameter declarations.

A strategy's parameters double as backtest config and as the schema the
evaluation harness uses for parameter sweeps. Each Parameter records its
type, default, and (for numeric) allowed range.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Parameter:
    name: str
    default: Any
    description: str = ""
    type: type | None = None  # if None, inferred from default
    min_value: float | None = None
    max_value: float | None = None
    allowed: tuple | None = None  # for categorical/enum-like params

    def validate(self, value: Any) -> None:
        expected = self.type or type(self.default)
        if not isinstance(value, expected) and not (
            isinstance(value, int) and expected is float
        ):
            raise ValueError(
                f"parameter {self.name!r}: expected {expected.__name__}, got {type(value).__name__}"
            )
        if self.allowed is not None and value not in self.allowed:
            raise ValueError(
                f"parameter {self.name!r}={value!r} not in allowed: {self.allowed}"
            )
        if isinstance(value, (int, float)):
            if self.min_value is not None and value < self.min_value:
                raise ValueError(f"parameter {self.name!r}={value} < min {self.min_value}")
            if self.max_value is not None and value > self.max_value:
                raise ValueError(f"parameter {self.name!r}={value} > max {self.max_value}")


@dataclass
class ParameterSet:
    """Ordered collection of Parameter declarations."""

    params: list[Parameter] = field(default_factory=list)

    def add(self, p: Parameter) -> "ParameterSet":
        self.params.append(p)
        return self

    def defaults(self) -> dict[str, Any]:
        return {p.name: p.default for p in self.params}

    def validate(self, values: dict[str, Any]) -> None:
        by_name = {p.name: p for p in self.params}
        for name, value in values.items():
            if name not in by_name:
                continue
            by_name[name].validate(value)

    def to_dict(self) -> list[dict[str, Any]]:
        return [
            {
                "name": p.name,
                "default": p.default,
                "type": (p.type or type(p.default)).__name__,
                "min": p.min_value,
                "max": p.max_value,
                "allowed": p.allowed,
                "description": p.description,
            }
            for p in self.params
        ]
