"""RSI(2) mean-reversion strategy — hand-port of the Pine Script v6
"Mean Reversion Strategy (RSI-based)".

Ported for the default settings requested for the SPY benchmark:
    RSI Length          2
    Oversold Level      10        (long when RSI(2) < 10)
    Overbought Level    90        (unused — Long Only)
    Trend Filter        ON, 200-SMA (long only when close > SMA200)
    Exit Mode           RSI Return (exit when RSI(2) crosses above 70)
    N-Bar Timeout       10 bars    (backstop — see FIDELITY NOTES §1)
    Stop Loss           ATR Multiple, 2.5 × ATR(14)
    Trade Direction     Long Only

This is a Python Strategy subclass (not a generated StrategySpec) because the
ATR stop and the N-bar timeout are stateful, per-position exits that the spec
DSL doesn't express — the same reason CasperStrategy is hand-written. It is
registered on the leaderboard via record_manual_strategy (imported_from=manual).

FIDELITY NOTES — where this port deviates from the literal Pine source, and why:

  1. Exit combination. The Pine `exitMode` is a single-select; "RSI Return"
     alone does NOT arm the N-bar timeout (only "N-Bar Timeout"/"Either" do).
     The requested config asks for RSI-Return *plus* a 10-bar backstop, so this
     port fires an exit on `RSI-return OR timeout` — matching the stated intent,
     not the literal single-select default. The ATR stop is always active
     (Pine attaches strategy.exit independent of exitMode).

  2. ATR stop is fixed at entry. Pine recomputes `longSlPrice =
     position_avg_price - 2.5*ATR` every bar, so its stop drifts with ATR. The
     engine's Order carries one static stop set at entry, so this port fixes the
     stop at `entry_signal_close - 2.5*ATR(14 at signal bar)` — the common
     "ATR stop from entry" intent, and the same convention CasperStrategy uses
     (stop computed from the signal bar's close).

  3. Exit fill timing. Pine (process_orders_on_close=false) fills a
     strategy.close on the NEXT bar's open; this engine fills a signal exit at
     the CURRENT bar's close. This is the harness-wide convention for every
     strategy it evaluates, so the RSI-2 port is measured on the same footing as
     its leaderboard peers. Entries DO match Pine — they fill next-bar open.

  4. Position sizing / costs. Pine uses 10% equity, 0.04% commission, 2-tick
     slippage. This port uses size=1 per trade (the harness convention — profit
     factor is size-invariant per trade) and whatever commission/slippage the
     eval config supplies. To reproduce Pine's cost model exactly, pass
     --commission / --slippage to scripts/evaluate.py.

  5. RSI/ATR smoothing. The project's indicators use simple averages over the
     trailing `period` window (see generator/indicators.py), whereas Pine's
     ta.rsi/ta.atr use Wilder smoothing over all history. Values differ slightly
     at the same bar. This is a library-level difference shared by every
     strategy in the harness, not specific to this port.
"""

from __future__ import annotations

from typing import Any, Optional

from engine.execution import Order, OrderType
from engine.portfolio import Position
from strategy.base import Strategy
from strategy.context import Bar, Context
from strategy.parameters import Parameter, ParameterSet

from generator.indicators import atr, ema, rsi, sma

TREND_MA_TYPES = ("SMA", "EMA")


class Rsi2MeanReversion(Strategy):
    archetype = "mean_reversion"
    thesis = (
        "Buy short-term RSI(2) oversold stretches while price holds above its "
        "200-day trend; exit on reversion (RSI back above 70), a bar-count "
        "backstop, or an ATR stop. Long-only, daily."
    )
    supported_assets = ["stocks"]
    timeframes = ["1d"]

    parameter_set = (
        ParameterSet()
        .add(Parameter("rsi_len", 2, type=int, min_value=1, max_value=50))
        .add(Parameter("oversold", 10.0, type=float, min_value=1.0, max_value=50.0))
        .add(Parameter("rsi_exit", 70.0, type=float, min_value=20.0, max_value=99.0))
        .add(Parameter("use_trend", True))
        .add(Parameter("trend_ma_len", 200, type=int, min_value=10, max_value=400))
        .add(Parameter("trend_ma_type", "SMA", allowed=TREND_MA_TYPES))
        .add(Parameter("sl_atr_mult", 2.5, type=float, min_value=0.1, max_value=10.0))
        .add(Parameter("atr_len", 14, type=int, min_value=1, max_value=100))
        .add(Parameter("max_bars", 10, type=int, min_value=1, max_value=100))
    )

    def __init__(
        self,
        rsi_len: int = 2,
        oversold: float = 10.0,
        rsi_exit: float = 70.0,
        use_trend: bool = True,
        trend_ma_len: int = 200,
        trend_ma_type: str = "SMA",
        sl_atr_mult: float = 2.5,
        atr_len: int = 14,
        max_bars: int = 10,
    ) -> None:
        if trend_ma_type not in TREND_MA_TYPES:
            raise ValueError(
                f"trend_ma_type={trend_ma_type!r} not in {TREND_MA_TYPES}"
            )
        self.rsi_len = rsi_len
        self.oversold = oversold
        self.rsi_exit = rsi_exit
        self.use_trend = use_trend
        self.trend_ma_len = trend_ma_len
        self.trend_ma_type = trend_ma_type
        self.sl_atr_mult = sl_atr_mult
        self.atr_len = atr_len
        self.max_bars = max_bars

        # Bars-in-trade counter, mirroring Pine's `barsInTrade`. Reset to 0
        # whenever flat; incremented once per bar while a position is open.
        self._bars_in_trade = 0

    # ── Strategy API ──────────────────────────────────────────────────────────

    def on_bar(
        self,
        bar: Bar,
        position: Optional[Position],
        context: Context,
    ) -> list[Order]:
        # Enough history for the longest lookback (the trend MA), plus a couple
        # of spare bars so RSI/ATR and their one-bar-lagged values resolve.
        need = max(self.trend_ma_len, self.atr_len + 1, self.rsi_len + 1) + 5
        bars = context.recent(need)

        if position is None:
            self._bars_in_trade = 0
            return self._maybe_enter(bar, bars)

        self._bars_in_trade += 1
        return self._maybe_exit(position, bars)

    # ── Entry ─────────────────────────────────────────────────────────────────

    def _maybe_enter(self, bar: Bar, bars: list[Bar]) -> list[Order]:
        rsi_now = rsi(bars, period=self.rsi_len)
        atr_now = atr(bars, period=self.atr_len)
        trend_ma = self._trend_ma(bars) if self.use_trend else None
        if rsi_now is None or atr_now is None or (self.use_trend and trend_ma is None):
            return []  # insufficient warmup

        oversold_stretch = rsi_now < self.oversold
        trend_ok = (not self.use_trend) or (bar.close > trend_ma)
        if not (oversold_stretch and trend_ok):
            return []

        # Static ATR stop fixed at entry (FIDELITY NOTE §2): reference the signal
        # bar's close, matching CasperStrategy's stop-from-signal-close convention.
        stop_price = bar.close - self.sl_atr_mult * atr_now
        if stop_price <= 0:
            return []

        return [
            Order(
                type=OrderType.MARKET,
                side="buy",
                size=1,
                stop_price=stop_price,
                signal_label="mr_long_entry",
            )
        ]

    # ── Exit ──────────────────────────────────────────────────────────────────

    def _maybe_exit(self, position: Position, bars: list[Bar]) -> list[Order]:
        # ATR stop is enforced intrabar by the engine (set on the Position at
        # entry) before on_bar runs, so it is not re-checked here. This method
        # covers the discretionary exits: RSI-return crossover and the timeout.
        if not position.is_long:
            return []  # long-only port; no short handling

        rsi_now = rsi(bars, period=self.rsi_len)
        rsi_prev = rsi(bars[:-1], period=self.rsi_len) if len(bars) > 1 else None

        # ta.crossover(rsi, rsi_exit): now above the level, previous bar at/below.
        rsi_return = (
            rsi_now is not None
            and rsi_now > self.rsi_exit
            and (rsi_prev is None or rsi_prev <= self.rsi_exit)
        )
        timeout = self._bars_in_trade >= self.max_bars

        if rsi_return or timeout:
            label = "mr_exit_rsi" if rsi_return else "mr_exit_timeout"
            return [
                Order(
                    type=OrderType.MARKET,
                    side="sell",
                    size=position.size,
                    signal_label=label,
                )
            ]
        return []

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _trend_ma(self, bars: list[Bar]) -> Optional[float]:
        if self.trend_ma_type == "EMA":
            return ema(bars, period=self.trend_ma_len)
        return sma(bars, period=self.trend_ma_len)

    def get_parameters(self) -> dict[str, Any]:
        return {
            "rsi_len": self.rsi_len,
            "oversold": self.oversold,
            "rsi_exit": self.rsi_exit,
            "use_trend": self.use_trend,
            "trend_ma_len": self.trend_ma_len,
            "trend_ma_type": self.trend_ma_type,
            "sl_atr_mult": self.sl_atr_mult,
            "atr_len": self.atr_len,
            "max_bars": self.max_bars,
        }
