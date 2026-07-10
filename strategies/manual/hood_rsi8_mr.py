"""RSI(8) mean-reversion with an ATR trailing stop — HOOD daily port.

Requested spec (2026-07-09):
    Entry          RSI(8) < 30  AND  close > SMA(200)          (long only)
    Primary exit   RSI(8) > 89   (crossover above the level)
    Trailing stop  2.0 x ATR(14), ratcheted up, never down
    Backstop       exit after 5 bars if neither of the above fires
    Direction      Long Only
    Timeframe      1d  (see the daily-vs-15min decision below)

This is a close cousin of Rsi2MeanReversion — same long-only, trend-filtered,
RSI-oversold entry with an N-bar backstop — with two deliberate differences:

  1. RSI(8)/30/89 instead of RSI(2)/10/70, and a 5-bar backstop instead of 10.
     Straight parameter changes from the requested alert settings.

  2. The stop is a TRAILING 2x ATR(14) stop, not the fixed-at-entry stop the
     RSI-2 port uses. This is the substantive behavioral difference and the
     reason this is its own file rather than a parameterization of Rsi2.

TIMEFRAME NOTE — why daily, not the 15-min the alert names:
  The spec's "on 15min bars" qualifier can't be honored literally in this
  harness. For intraday timeframes the backtester (a) resets the bar context at
  every session boundary, so SMA(200) — which needs ~8 days of 15-min bars —
  never has enough history to compute, and (b) force-flattens every position at
  the session close, so a multi-day mean-reversion hold and its trailing stop
  can't survive overnight. On daily bars both problems vanish: one continuous
  series (SMA-200 resolves), no EOD flatten (holds span days), and the trailing
  stop ratchets across days. The user chose daily bars with this understood.

FIDELITY NOTES — how the trailing stop is realized on the engine:

  * The engine checks the protective stop intrabar (bar.low <= stop_price for a
    long) at step 3 of each bar, BEFORE on_bar runs at step 5. So the stop that
    protects bar N was the value set at the close of bar N-1. This port ratchets
    the stop inside on_bar using information available through the current
    close; it therefore takes effect on the NEXT bar — the realistic convention
    (a trailing stop can only tighten on already-observed price).

  * Trail anchor is the highest HIGH since entry (chandelier convention):
        stop = max(prev_stop, highest_high_since_entry - mult * ATR(14, now))
    ATR is recomputed each bar (a drifting-ATR trail), and the max() makes the
    stop monotonically non-decreasing. The initial stop, set the bar the
    position opens, is entry_close - mult * ATR at the signal bar (matching the
    RSI-2 port's stop-from-signal-close convention).

  * Exit fill timing / sizing / RSI smoothing all follow the harness-wide
    conventions documented at length in rsi2_mean_reversion.py — signal exits
    fill at the current bar's close, entries fill next-bar open, size=1, and the
    project's simple-average RSI/ATR differ slightly from Pine's Wilder smoothing.
"""

from __future__ import annotations

from typing import Any, Optional

from engine.execution import Order, OrderType
from engine.portfolio import Position
from strategy.base import Strategy
from strategy.context import Bar, Context
from strategy.parameters import Parameter, ParameterSet

from generator.indicators import atr, rsi, sma


class HoodRsi8MeanReversion(Strategy):
    archetype = "mean_reversion"
    thesis = (
        "Buy RSI(8) oversold dips while price holds above its 200-day trend; "
        "exit on reversion (RSI(8) back above 89), a 5-bar backstop, or a 2xATR "
        "trailing stop. Long-only, daily."
    )
    supported_assets = ["stocks"]
    timeframes = ["1d"]

    parameter_set = (
        ParameterSet()
        .add(Parameter("rsi_len", 8, type=int, min_value=1, max_value=50))
        .add(Parameter("oversold", 30.0, type=float, min_value=1.0, max_value=50.0))
        .add(Parameter("rsi_exit", 89.0, type=float, min_value=20.0, max_value=99.0))
        .add(Parameter("trend_ma_len", 200, type=int, min_value=10, max_value=400))
        .add(Parameter("sl_atr_mult", 2.0, type=float, min_value=0.1, max_value=10.0))
        .add(Parameter("atr_len", 14, type=int, min_value=1, max_value=100))
        .add(Parameter("max_bars", 5, type=int, min_value=1, max_value=100))
    )

    def __init__(
        self,
        rsi_len: int = 8,
        oversold: float = 30.0,
        rsi_exit: float = 89.0,
        trend_ma_len: int = 200,
        sl_atr_mult: float = 2.0,
        atr_len: int = 14,
        max_bars: int = 5,
    ) -> None:
        self.rsi_len = rsi_len
        self.oversold = oversold
        self.rsi_exit = rsi_exit
        self.trend_ma_len = trend_ma_len
        self.sl_atr_mult = sl_atr_mult
        self.atr_len = atr_len
        self.max_bars = max_bars

        # Per-position state, reset whenever flat.
        self._bars_in_trade = 0
        self._highest_high = 0.0

    # ── Strategy API ──────────────────────────────────────────────────────────

    def on_bar(
        self,
        bar: Bar,
        position: Optional[Position],
        context: Context,
    ) -> list[Order]:
        need = max(self.trend_ma_len, self.atr_len + 1, self.rsi_len + 1) + 5
        bars = context.recent(need)

        if position is None:
            self._bars_in_trade = 0
            self._highest_high = 0.0
            return self._maybe_enter(bar, bars)

        self._bars_in_trade += 1
        self._update_trailing_stop(bar, position, bars)
        return self._maybe_exit(position, bars)

    # ── Entry ─────────────────────────────────────────────────────────────────

    def _maybe_enter(self, bar: Bar, bars: list[Bar]) -> list[Order]:
        rsi_now = rsi(bars, period=self.rsi_len)
        atr_now = atr(bars, period=self.atr_len)
        trend_ma = sma(bars, period=self.trend_ma_len)
        if rsi_now is None or atr_now is None or trend_ma is None:
            return []  # insufficient warmup

        if not (rsi_now < self.oversold and bar.close > trend_ma):
            return []

        stop_price = bar.close - self.sl_atr_mult * atr_now
        if stop_price <= 0:
            return []

        # Seed the trailing-stop state for the life of this position.
        self._highest_high = bar.high

        return [
            Order(
                type=OrderType.MARKET,
                side="buy",
                size=1,
                stop_price=stop_price,
                signal_label="hood_mr_long_entry",
            )
        ]

    # ── Trailing stop ─────────────────────────────────────────────────────────

    def _update_trailing_stop(
        self, bar: Bar, position: Position, bars: list[Bar]
    ) -> None:
        """Ratchet the protective stop up to highest_high - mult*ATR. Never
        lowers it. Mutates position.stop_price so the engine enforces the new
        level on the next bar."""
        self._highest_high = max(self._highest_high, bar.high)
        atr_now = atr(bars, period=self.atr_len)
        if atr_now is None:
            return
        trail = self._highest_high - self.sl_atr_mult * atr_now
        if trail > position.stop_price:
            position.stop_price = trail

    # ── Exit ──────────────────────────────────────────────────────────────────

    def _maybe_exit(self, position: Position, bars: list[Bar]) -> list[Order]:
        # The ATR trailing stop is enforced intrabar by the engine off
        # position.stop_price (updated above); this covers the discretionary
        # RSI-return exit and the bar-count backstop.
        if not position.is_long:
            return []

        rsi_now = rsi(bars, period=self.rsi_len)
        rsi_prev = rsi(bars[:-1], period=self.rsi_len) if len(bars) > 1 else None

        # ta.crossover(rsi, rsi_exit): now above the level, previous at/below.
        rsi_return = (
            rsi_now is not None
            and rsi_now > self.rsi_exit
            and (rsi_prev is None or rsi_prev <= self.rsi_exit)
        )
        timeout = self._bars_in_trade >= self.max_bars

        if rsi_return or timeout:
            label = "hood_mr_exit_rsi" if rsi_return else "hood_mr_exit_timeout"
            return [
                Order(
                    type=OrderType.MARKET,
                    side="sell",
                    size=position.size,
                    signal_label=label,
                )
            ]
        return []

    def get_parameters(self) -> dict[str, Any]:
        return {
            "rsi_len": self.rsi_len,
            "oversold": self.oversold,
            "rsi_exit": self.rsi_exit,
            "trend_ma_len": self.trend_ma_len,
            "sl_atr_mult": self.sl_atr_mult,
            "atr_len": self.atr_len,
            "max_bars": self.max_bars,
        }
