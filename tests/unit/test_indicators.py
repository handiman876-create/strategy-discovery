"""Indicator math tests."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from generator import indicators
from strategy.context import Bar

ET = ZoneInfo("America/New_York")


def _bars(closes: list[float], highs: list[float] | None = None, lows: list[float] | None = None):
    base = datetime(2024, 1, 1, tzinfo=ET)
    out = []
    for i, c in enumerate(closes):
        h = highs[i] if highs else c + 0.5
        l = lows[i] if lows else c - 0.5
        out.append(Bar(base + timedelta(days=i), c, h, l, c, 1000.0))
    return out


def test_sma_known_value():
    bars = _bars([100, 101, 102, 103, 104])
    assert indicators.sma(bars, 5) == pytest.approx(102.0)


def test_sma_insufficient_returns_none():
    assert indicators.sma(_bars([100, 101]), 5) is None


def test_ema_returns_finite():
    bars = _bars(list(range(100, 130)))
    val = indicators.ema(bars, 10)
    assert val is not None and 100 < val < 130


def test_rsi_all_up_returns_100():
    bars = _bars([100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115])
    assert indicators.rsi(bars, 14) == 100.0


def test_rsi_all_down_returns_0():
    bars = _bars(list(range(150, 130, -1)))
    assert indicators.rsi(bars, 14) == 0.0


def test_rsi_default_is_simple():
    # The default smoothing must stay "simple" so existing callers/specs are
    # byte-for-byte unchanged by the addition of the Wilder switch.
    bars = _bars([100, 99, 101, 98, 102, 97, 103, 96, 104, 95, 105, 94, 106])
    assert indicators.rsi(bars, 6) == indicators.rsi(bars, 6, smoothing="simple")


def test_rsi_wilder_differs_from_simple():
    # On a mixed short-period series the two smoothings must produce different
    # values (both valid 0-100) — this is the divergence the switch exists for.
    bars = _bars([100, 99, 101, 98, 102, 97, 103, 96, 104, 95, 105, 94, 106])
    simple = indicators.rsi(bars, 6, smoothing="simple")
    wilder = indicators.rsi(bars, 6, smoothing="wilder")
    assert 0.0 <= wilder <= 100.0
    assert wilder != simple


def test_rsi_wilder_extremes_match_simple():
    # Degenerate all-up / all-down series pin both smoothings to 100 / 0.
    up = _bars(list(range(100, 116)))
    down = _bars(list(range(150, 130, -1)))
    assert indicators.rsi(up, 14, smoothing="wilder") == 100.0
    assert indicators.rsi(down, 14, smoothing="wilder") == 0.0


def test_rsi_unknown_smoothing_raises():
    bars = _bars(list(range(100, 116)))
    with pytest.raises(ValueError):
        indicators.rsi(bars, 14, smoothing="ema")


def test_atr_computes():
    bars = _bars([100] * 20, highs=[101]*20, lows=[99]*20)
    val = indicators.atr(bars, 14)
    assert val is not None and val > 0


def test_bb_bands_ordered():
    bars = _bars(list(range(100, 130)))
    mid = indicators.bb_mid(bars, 20)
    upper = indicators.bb_upper(bars, 20)
    lower = indicators.bb_lower(bars, 20)
    assert lower < mid < upper


def test_roc_known_value():
    bars = _bars([100, 100, 100, 100, 100, 110])
    # ROC(period=5): (110/100 - 1)*100 = 10
    assert indicators.roc(bars, 5) == pytest.approx(10.0)


def test_macd_components_all_finite_on_long_series():
    bars = _bars(list(range(100, 200)))
    m = indicators.macd(bars)
    s = indicators.macd_signal(bars)
    h = indicators.macd_hist(bars)
    assert m is not None and s is not None and h is not None


def test_daily_return():
    bars = _bars([100, 110])
    assert indicators.daily_return(bars) == pytest.approx(0.1)


def test_percent_rank_top():
    bars = _bars([1, 2, 3, 4, 5, 6, 7, 8, 9, 10] * 30)  # 300 bars
    val = indicators.percent_rank(bars, 252)
    assert val == pytest.approx(1.0)


def test_zscore_positive_on_extending_series():
    bars = _bars(list(range(100, 130)))
    val = indicators.zscore(bars, 20)
    assert val is not None and val > 0


def test_indicator_function_registry_complete():
    for name in indicators.ALLOWED_INDICATORS:
        assert name in indicators.INDICATOR_FUNCTIONS
