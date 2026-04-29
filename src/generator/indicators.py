"""Allowed indicator library for generated strategies.

Each indicator is a pure function over a list[Bar] (newest last) and returns
the most recent value, or None when the buffer is too short to compute.

The set of allowed indicators is FIXED. Generated strategy specs reference
indicators by name from `ALLOWED_INDICATORS`; the translator emits calls to
the corresponding function in this module.

Naming convention: each indicator function is named the same as its DSL key.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Optional, Sequence

if TYPE_CHECKING:
    from strategy.context import Bar

ALLOWED_INDICATORS: tuple[str, ...] = (
    "sma",
    "ema",
    "rsi",
    "atr",
    "bb_mid",
    "bb_upper",
    "bb_lower",
    "roc",
    "macd",
    "macd_signal",
    "macd_hist",
    "daily_return",
    "percent_rank",
    "zscore",
)

# Indicators that only make sense on daily-bar strategies.
DAILY_ONLY_INDICATORS: frozenset[str] = frozenset({"daily_return"})

# Closed-form ranges for indicators that are mathematically bounded. Used by
# the translator's unreachable-default detector to flag clauses like
# `rsi > 110` whose RHS lies outside the indicator's possible range. Indicators
# absent from this map are treated as unbounded (-inf, inf). Bounds are
# inclusive on the bounded side; use math.inf for the unbounded side.
INDICATOR_RANGES: dict[str, tuple[float, float]] = {
    "rsi": (0.0, 100.0),
    "atr": (0.0, math.inf),
    "percent_rank": (0.0, 1.0),
}


# ── Helpers ──────────────────────────────────────────────────────────────────


def _closes(bars: Sequence["Bar"]) -> list[float]:
    return [b.close for b in bars]


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _std(xs: Sequence[float]) -> float:
    m = _mean(xs)
    return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5


# ── Moving averages ──────────────────────────────────────────────────────────


def sma(bars: Sequence["Bar"], period: int) -> Optional[float]:
    if len(bars) < period:
        return None
    return _mean(_closes(bars[-period:]))


def ema(bars: Sequence["Bar"], period: int) -> Optional[float]:
    if len(bars) < period:
        return None
    closes = _closes(bars)
    k = 2.0 / (period + 1)
    e = _mean(closes[:period])
    for c in closes[period:]:
        e = c * k + e * (1 - k)
    return e


# ── Oscillators ──────────────────────────────────────────────────────────────


def rsi(bars: Sequence["Bar"], period: int = 14) -> Optional[float]:
    if len(bars) < period + 1:
        return None
    closes = _closes(bars)
    gains = []
    losses = []
    for i in range(1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = _mean(gains[-period:])
    avg_loss = _mean(losses[-period:])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def roc(bars: Sequence["Bar"], period: int = 10) -> Optional[float]:
    """Rate of change in percent: (close_now / close_period_ago - 1) * 100."""
    if len(bars) < period + 1:
        return None
    now = bars[-1].close
    then = bars[-1 - period].close
    if then == 0:
        return None
    return (now / then - 1.0) * 100.0


# ── Volatility ───────────────────────────────────────────────────────────────


def atr(bars: Sequence["Bar"], period: int = 14) -> Optional[float]:
    if len(bars) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(bars)):
        h, l, prev_c = bars[i].high, bars[i].low, bars[i - 1].close
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    return _mean(trs[-period:])


def _bb(bars: Sequence["Bar"], period: int, k: float) -> Optional[tuple[float, float, float]]:
    if len(bars) < period:
        return None
    closes = _closes(bars[-period:])
    m = _mean(closes)
    s = _std(closes)
    return m, m + k * s, m - k * s


def bb_mid(bars: Sequence["Bar"], period: int = 20, k: float = 2.0) -> Optional[float]:
    res = _bb(bars, period, k)
    return res[0] if res else None


def bb_upper(bars: Sequence["Bar"], period: int = 20, k: float = 2.0) -> Optional[float]:
    res = _bb(bars, period, k)
    return res[1] if res else None


def bb_lower(bars: Sequence["Bar"], period: int = 20, k: float = 2.0) -> Optional[float]:
    res = _bb(bars, period, k)
    return res[2] if res else None


# ── MACD (12/26/9 default) ───────────────────────────────────────────────────


def _macd_components(
    bars: Sequence["Bar"], fast: int, slow: int, signal: int
) -> Optional[tuple[float, float, float]]:
    if len(bars) < slow + signal:
        return None
    closes = _closes(bars)
    # Compute MACD series (full), then signal line as EMA over the MACD series.
    k_fast = 2.0 / (fast + 1)
    k_slow = 2.0 / (slow + 1)
    e_fast = _mean(closes[:fast])
    e_slow = _mean(closes[:slow])
    macd_series: list[float] = []
    for i, c in enumerate(closes):
        if i >= fast:
            e_fast = c * k_fast + e_fast * (1 - k_fast)
        if i >= slow:
            e_slow = c * k_slow + e_slow * (1 - k_slow)
        if i >= slow:
            macd_series.append(e_fast - e_slow)
    if len(macd_series) < signal:
        return None
    k_sig = 2.0 / (signal + 1)
    e_sig = _mean(macd_series[:signal])
    for v in macd_series[signal:]:
        e_sig = v * k_sig + e_sig * (1 - k_sig)
    macd_now = macd_series[-1]
    return macd_now, e_sig, macd_now - e_sig


def macd(
    bars: Sequence["Bar"], fast: int = 12, slow: int = 26, signal: int = 9
) -> Optional[float]:
    res = _macd_components(bars, fast, slow, signal)
    return res[0] if res else None


def macd_signal(
    bars: Sequence["Bar"], fast: int = 12, slow: int = 26, signal: int = 9
) -> Optional[float]:
    res = _macd_components(bars, fast, slow, signal)
    return res[1] if res else None


def macd_hist(
    bars: Sequence["Bar"], fast: int = 12, slow: int = 26, signal: int = 9
) -> Optional[float]:
    res = _macd_components(bars, fast, slow, signal)
    return res[2] if res else None


# ── Returns / statistics ────────────────────────────────────────────────────


def daily_return(bars: Sequence["Bar"]) -> Optional[float]:
    """Most recent bar's close / previous bar's close - 1.

    DAILY-ONLY: spec validator rejects this on intraday timeframes. On 1d
    bars, this is the day's return.
    """
    if len(bars) < 2:
        return None
    prev_c = bars[-2].close
    if prev_c == 0:
        return None
    return bars[-1].close / prev_c - 1.0


def percent_rank(bars: Sequence["Bar"], period: int = 252) -> Optional[float]:
    """Percent rank of the most recent close within the last `period` closes,
    in [0, 1]. 1.0 = highest in the window."""
    if len(bars) < period:
        return None
    closes = _closes(bars[-period:])
    cur = closes[-1]
    return sum(1 for c in closes if c <= cur) / len(closes)


def zscore(bars: Sequence["Bar"], period: int = 20) -> Optional[float]:
    if len(bars) < period:
        return None
    closes = _closes(bars[-period:])
    s = _std(closes)
    if s == 0:
        return None
    return (closes[-1] - _mean(closes)) / s


INDICATOR_FUNCTIONS = {
    "sma": sma,
    "ema": ema,
    "rsi": rsi,
    "atr": atr,
    "bb_mid": bb_mid,
    "bb_upper": bb_upper,
    "bb_lower": bb_lower,
    "roc": roc,
    "macd": macd,
    "macd_signal": macd_signal,
    "macd_hist": macd_hist,
    "daily_return": daily_return,
    "percent_rank": percent_rank,
    "zscore": zscore,
}
