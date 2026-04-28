"""Casper 5-min opening-range scalp strategy.

State machine: WAIT_RANGE → WAIT_CONFIRM → WAIT_RETEST → IN_TRADE → DONE_FOR_DAY.

Rules
-----
* Opening Range (OR): high/low of the first 5-min bar of the session (09:30 ET).
* Confirmation: `min_bars_beyond_or` consecutive 5-min closes beyond the OR
  on the same side. A close BACK INSIDE the OR resets the counter.
* Retest entry: after confirmation, a bar that wicks INTO the OR AND closes
  back outside it on the breakout side enters in the breakout direction.
* Optional momentum fallback: if `momentum_fallback=True`, an entry also fires
  when price extends `momentum_distance × OR_size` past the OR boundary.
* Stop loss: `stop_mode` ∈ {opposite_bracket, midpoint, fixed_dollar, pct_move}.
* Take profit: entry ± risk × `rr_ratio` where risk = |entry - stop|.
* Re-entries: if `allow_multiple_breakouts=True`, after a closed trade the
  state returns to WAIT_RETEST (same OR, same confirmed direction; no new
  confirmation required). Subject to `entry_cutoff` and `retest_timeout`.
* Hard rule: at most one position per symbol at a time.
* `retest_timeout`: after confirmation, give up looking for a retest after
  this many bars (use `math.inf` for "never give up").
"""

from __future__ import annotations

import math
from datetime import datetime, time
from enum import Enum, auto
from typing import Any, Optional

from engine.execution import Order, OrderType
from engine.portfolio import Position, Trade
from strategy.base import Strategy
from strategy.context import Bar, Context
from strategy.parameters import Parameter, ParameterSet

STOP_MODES = ("opposite_bracket", "midpoint", "fixed_dollar", "pct_move")


class _State(Enum):
    WAIT_RANGE = auto()
    WAIT_CONFIRM = auto()
    WAIT_RETEST = auto()
    IN_TRADE = auto()
    DONE_FOR_DAY = auto()


class CasperStrategy(Strategy):
    archetype = "microstructure"
    thesis = "Opening-range breakout with retest confirmation; classic scalp pattern."
    supported_assets = ["stocks"]
    timeframes = ["5m"]

    parameter_set = (
        ParameterSet()
        .add(Parameter("stop_mode", "opposite_bracket", allowed=STOP_MODES))
        .add(Parameter("stop_value", 0.5, type=float, min_value=0.0))
        .add(Parameter("rr_ratio", 2.0, type=float, min_value=0.1, max_value=10.0))
        .add(Parameter("entry_cutoff", "11:00"))
        .add(Parameter("eod_exit", "15:50"))
        .add(Parameter("min_bars_beyond_or", 2, type=int, min_value=1, max_value=20))
        .add(Parameter("retest_timeout", 12, type=int, min_value=1))
        .add(Parameter("allow_multiple_breakouts", True))
        .add(Parameter("momentum_fallback", False))
        .add(Parameter("momentum_distance", 0.5, type=float, min_value=0.0))
    )

    def __init__(
        self,
        stop_mode: str = "opposite_bracket",
        stop_value: float = 0.5,
        rr_ratio: float = 2.0,
        entry_cutoff: str = "11:00",
        eod_exit: str = "15:50",
        min_bars_beyond_or: int = 2,
        retest_timeout: int | float = 12,
        allow_multiple_breakouts: bool = True,
        momentum_fallback: bool = False,
        momentum_distance: float = 0.5,
    ):
        if stop_mode not in STOP_MODES:
            raise ValueError(f"stop_mode={stop_mode!r} not in {STOP_MODES}")
        self.stop_mode = stop_mode
        self.stop_value = stop_value
        self.rr_ratio = rr_ratio
        self.min_bars_beyond_or = min_bars_beyond_or
        self.retest_timeout = retest_timeout
        self.allow_multiple_breakouts = allow_multiple_breakouts
        self.momentum_fallback = momentum_fallback
        self.momentum_distance = momentum_distance

        self._entry_cutoff = _parse_hhmm(entry_cutoff)
        self._eod_exit = _parse_hhmm(eod_exit)

        self._state = _State.WAIT_RANGE
        self._or_high = 0.0
        self._or_low = 0.0
        self._confirm_dir: Optional[str] = None
        self._confirm_count = 0
        self._bars_since_confirm = 0

    # ── Hooks ────────────────────────────────────────────────────────────────

    def on_session_start(self, session_ts: datetime, context: Context) -> None:
        self._state = _State.WAIT_RANGE
        self._or_high = 0.0
        self._or_low = 0.0
        self._confirm_dir = None
        self._confirm_count = 0
        self._bars_since_confirm = 0

    def on_trade_closed(self, trade: Trade) -> None:
        if self.allow_multiple_breakouts and self._confirm_dir is not None:
            # Re-arm: same OR, same confirmed direction, retest condition resets,
            # bars-since-confirm counter resets. entry_cutoff still applies.
            self._state = _State.WAIT_RETEST
            self._bars_since_confirm = 0
        else:
            self._state = _State.DONE_FOR_DAY

    def on_bar(
        self,
        bar: Bar,
        position: Optional[Position],
        context: Context,
    ) -> list[Order]:
        if self._state == _State.DONE_FOR_DAY:
            return []

        if self._state == _State.WAIT_RANGE:
            if context.bars_since_session_open() == 1:
                self._or_high = bar.high
                self._or_low = bar.low
                self._state = _State.WAIT_CONFIRM
            return []

        if self._state == _State.WAIT_CONFIRM:
            return self._handle_confirm(bar)

        if self._state == _State.WAIT_RETEST:
            return self._handle_retest(bar)

        # IN_TRADE — engine handles stop/target/EOD
        return []

    # ── Internals ────────────────────────────────────────────────────────────

    def _handle_confirm(self, bar: Bar) -> list[Order]:
        if bar.bar_time >= self._entry_cutoff:
            self._state = _State.DONE_FOR_DAY
            return []

        above = bar.close > self._or_high
        below = bar.close < self._or_low

        if above:
            if self._confirm_dir == "long":
                self._confirm_count += 1
            else:
                self._confirm_dir = "long"
                self._confirm_count = 1
        elif below:
            if self._confirm_dir == "short":
                self._confirm_count += 1
            else:
                self._confirm_dir = "short"
                self._confirm_count = 1
        else:
            self._confirm_dir = None
            self._confirm_count = 0

        if self._confirm_count >= self.min_bars_beyond_or:
            self._state = _State.WAIT_RETEST
            self._bars_since_confirm = 0

        return []

    def _handle_retest(self, bar: Bar) -> list[Order]:
        if bar.bar_time >= self._entry_cutoff:
            self._state = _State.DONE_FOR_DAY
            return []

        if self._bars_since_confirm >= self.retest_timeout:
            self._state = _State.DONE_FOR_DAY
            return []
        self._bars_since_confirm += 1

        or_size = self._or_high - self._or_low

        if self._confirm_dir == "long":
            if bar.low <= self._or_high and bar.close > self._or_high:
                return self._build_entry(bar, "long", "retest")
            if self.momentum_fallback:
                threshold = self._or_high + self.momentum_distance * or_size
                if bar.close >= threshold:
                    return self._build_entry(bar, "long", "momentum")
        elif self._confirm_dir == "short":
            if bar.high >= self._or_low and bar.close < self._or_low:
                return self._build_entry(bar, "short", "retest")
            if self.momentum_fallback:
                threshold = self._or_low - self.momentum_distance * or_size
                if bar.close <= threshold:
                    return self._build_entry(bar, "short", "momentum")

        return []

    def _build_entry(self, bar: Bar, direction: str, label: str) -> list[Order]:
        entry_price = bar.close
        stop_price = self._calc_stop(entry_price, direction)
        risk = abs(entry_price - stop_price)
        if risk <= 0:
            self._state = _State.DONE_FOR_DAY
            return []

        if direction == "long":
            target_price = entry_price + risk * self.rr_ratio
            side = "buy"
        else:
            target_price = entry_price - risk * self.rr_ratio
            side = "sell_short"

        self._state = _State.IN_TRADE
        return [
            Order(
                type=OrderType.MARKET,
                side=side,
                size=1,
                stop_price=stop_price,
                target_price=target_price,
                signal_label=f"{direction}_{label}",
            )
        ]

    def _calc_stop(self, entry_price: float, direction: str) -> float:
        if self.stop_mode == "opposite_bracket":
            return self._or_low if direction == "long" else self._or_high
        if self.stop_mode == "midpoint":
            return (self._or_high + self._or_low) / 2
        if self.stop_mode == "fixed_dollar":
            return (
                entry_price - self.stop_value
                if direction == "long"
                else entry_price + self.stop_value
            )
        if self.stop_mode == "pct_move":
            return (
                entry_price * (1 - self.stop_value)
                if direction == "long"
                else entry_price * (1 + self.stop_value)
            )
        raise ValueError(f"unknown stop_mode={self.stop_mode!r}")

    # ── Strategy API ─────────────────────────────────────────────────────────

    def get_parameters(self) -> dict[str, Any]:
        return {
            "stop_mode": self.stop_mode,
            "stop_value": self.stop_value,
            "rr_ratio": self.rr_ratio,
            "entry_cutoff": self._entry_cutoff.strftime("%H:%M"),
            "eod_exit": self._eod_exit.strftime("%H:%M"),
            "min_bars_beyond_or": self.min_bars_beyond_or,
            "retest_timeout": self.retest_timeout,
            "allow_multiple_breakouts": self.allow_multiple_breakouts,
            "momentum_fallback": self.momentum_fallback,
            "momentum_distance": self.momentum_distance,
        }


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))
