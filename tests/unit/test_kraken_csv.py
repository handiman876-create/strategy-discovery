"""Kraken CSV bulk-aggregation tests against a synthetic fixture."""

from __future__ import annotations

import random
from pathlib import Path

import pandas as pd
import pytest

from data.kraken_csv import aggregate_csv_to_bars, download_quarter


def _write_synthetic_csv(path: Path, n_trades: int = 1000) -> None:
    """Write a synthetic CSV: timestamp_unix, price, volume."""
    rng = random.Random(7)
    base_ts = 1_704_067_200  # 2024-01-01 00:00:00 UTC
    rows = []
    price = 42_000.0
    for i in range(n_trades):
        # ~3.6 seconds between trades → ~1 hour total span
        ts = base_ts + int(i * 3.6)
        price *= 1 + rng.gauss(0, 0.0005)
        vol = round(rng.uniform(0.001, 0.1), 6)
        rows.append((ts, round(price, 2), vol))
    with open(path, "w") as f:
        for ts, p, v in rows:
            f.write(f"{ts},{p},{v}\n")


def test_aggregate_synthetic_to_5min(tmp_path):
    csv_path = tmp_path / "XBTUSD_test.csv"
    _write_synthetic_csv(csv_path, n_trades=1000)
    bars = aggregate_csv_to_bars(csv_path, "XBTUSD", "5m", cache_root=tmp_path / "cache")
    assert not bars.empty
    # 1000 trades × 3.6s ≈ 3600s ≈ 60 min → ~12 5-min bars
    assert 10 <= len(bars) <= 14
    # OHLCV invariants
    for _, r in bars.iterrows():
        assert r["high"] >= max(r["open"], r["close"])
        assert r["low"] <= min(r["open"], r["close"])
        assert r["volume"] > 0
    # Timestamps must be UTC and monotonic
    assert bars["timestamp"].dt.tz is not None
    assert bars["timestamp"].is_monotonic_increasing


def test_aggregate_idempotent_via_cache(tmp_path):
    csv_path = tmp_path / "XBTUSD_test.csv"
    _write_synthetic_csv(csv_path, n_trades=300)
    cache = tmp_path / "cache"
    first = aggregate_csv_to_bars(csv_path, "XBTUSD", "1h", cache_root=cache)
    csv_path.unlink()  # If aggregate re-reads CSV, it'll fail
    second = aggregate_csv_to_bars(csv_path, "XBTUSD", "1h", cache_root=cache)
    assert len(first) == len(second)
    pd.testing.assert_series_equal(
        first["timestamp"].dt.tz_convert("UTC").reset_index(drop=True),
        second["timestamp"].dt.tz_convert("UTC").reset_index(drop=True),
        check_dtype=False,
    )
    for col in ("open", "high", "low", "close", "volume"):
        pd.testing.assert_series_equal(
            first[col].reset_index(drop=True),
            second[col].reset_index(drop=True),
            check_dtype=False,
        )


def test_aggregate_5min_volumes_sum_correctly(tmp_path):
    csv_path = tmp_path / "x.csv"
    # Two 5-min bars with known volumes
    base = 1_704_067_200  # 2024-01-01 00:00 UTC
    with open(csv_path, "w") as f:
        # bar 1: 0–5min, three trades, total vol=10
        f.write(f"{base + 60},100.0,3.0\n")
        f.write(f"{base + 120},101.0,4.0\n")
        f.write(f"{base + 240},99.0,3.0\n")
        # bar 2: 5–10min, two trades, total vol=5
        f.write(f"{base + 320},102.0,2.0\n")
        f.write(f"{base + 480},103.0,3.0\n")
    bars = aggregate_csv_to_bars(csv_path, "X", "5m")
    assert len(bars) == 2
    assert bars.iloc[0]["volume"] == pytest.approx(10.0)
    assert bars.iloc[1]["volume"] == pytest.approx(5.0)
    assert bars.iloc[0]["high"] == 101.0
    assert bars.iloc[0]["low"] == 99.0
    assert bars.iloc[0]["open"] == 100.0
    assert bars.iloc[0]["close"] == 99.0


def test_download_raises_with_helpful_message(tmp_path):
    with pytest.raises(NotImplementedError, match="(?i)manually download"):
        download_quarter("XBTUSD", "2023Q1", tmp_path)
