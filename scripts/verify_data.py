"""
Phase 0.5 data verification.

Confirms Polygon (stocks) and Kraken (crypto) can deliver the data we need
before we commit to building the Strategy Discovery Framework on top of them.

Usage:
    cd /root/strategy-discovery
    venv/bin/python scripts/verify_data.py
"""

from __future__ import annotations

import os
import random
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"

ET = "America/New_York"
UTC = timezone.utc

POLYGON_BASE = "https://api.polygon.io"

KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
KRAKEN_CSV_BASE = "https://drive.google.com/drive/folders/1jLG14CGwhzCJuKVDcUjFK8TmS9NRLP82"
KRAKEN_CSV_DOC_URL = "https://support.kraken.com/hc/en-us/articles/360047124832"


@dataclass
class TestResult:
    name: str
    passed: bool
    summary: str
    issues: list[str] = field(default_factory=list)


# ------------------------------------------------------------------------- #
# Test 1: Polygon 5-min stock data                                          #
# ------------------------------------------------------------------------- #

def fetch_polygon_aggs(
    api_key: str, symbol: str, multiplier: int, timespan: str, start_date: str, end_date: str
) -> pd.DataFrame:
    """
    Fetch aggregate bars from Polygon /v2/aggs and follow next_url pagination.
    Returns ET-aware bars filtered to RTH (9:30-16:00 inclusive at the open).
    """
    url = (
        f"{POLYGON_BASE}/v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}/"
        f"{start_date}/{end_date}"
    )
    params = {"adjusted": "true", "sort": "asc", "limit": 50000}
    headers = {"Authorization": f"Bearer {api_key}"}

    rows: list[dict] = []
    page = 0
    while url:
        r = requests.get(url, params=params if page == 0 else None, headers=headers, timeout=30)
        r.raise_for_status()
        payload = r.json()
        if payload.get("status") not in ("OK", "DELAYED"):
            raise RuntimeError(f"Polygon returned status={payload.get('status')!r}: {payload}")
        results = payload.get("results") or []
        rows.extend(results)
        print(f"  fetched {len(results):>6} bars (page {page + 1}, running total {len(rows)})")
        url = payload.get("next_url")
        page += 1
        time.sleep(0.1)

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["t"], unit="ms", utc=True).dt.tz_convert(ET)
    df = df.rename(columns={"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"})
    df = df[["time", "open", "high", "low", "close", "volume"]]

    rth_mask = df["time"].dt.time.between(
        pd.Timestamp("09:30").time(), pd.Timestamp("15:55").time()
    )
    df = df[rth_mask].copy()
    return df.sort_values("time").drop_duplicates(subset=["time"]).reset_index(drop=True)


def check_rth_gaps(df: pd.DataFrame, max_gap_minutes: int = 10) -> list[str]:
    """Check for gaps > max_gap_minutes within RTH (9:30-16:00 ET)."""
    issues: list[str] = []
    rth = df[df["time"].dt.time.between(pd.Timestamp("09:30").time(), pd.Timestamp("16:00").time())].copy()
    rth["date"] = rth["time"].dt.date
    for date, day in rth.groupby("date"):
        diffs = day["time"].diff().dt.total_seconds().div(60)
        big = diffs[diffs > max_gap_minutes]
        if not big.empty:
            for idx, gap in big.items():
                ts = day.loc[idx, "time"]
                issues.append(f"{date}: {gap:.0f}-min gap ending at {ts.time()}")
    return issues


def check_5min_alignment(df: pd.DataFrame, sample_n: int = 3) -> list[str]:
    """Sample N random sessions and confirm bars are on 5-min boundaries."""
    issues: list[str] = []
    sessions = sorted(df["time"].dt.date.unique())
    if len(sessions) < sample_n:
        sample_n = len(sessions)
    rng = random.Random(42)
    picked = rng.sample(list(sessions), sample_n)
    for date in picked:
        day = df[df["time"].dt.date == date]
        bad = day[~((day["time"].dt.minute % 5 == 0) & (day["time"].dt.second == 0))]
        if not bad.empty:
            issues.append(f"{date}: {len(bad)} bars not on 5-min boundary (e.g. {bad.iloc[0]['time']})")
        else:
            print(f"  alignment OK on {date} ({len(day)} bars)")
    return issues


def test_1_polygon() -> TestResult:
    print("\n=== Test 1: Polygon 5-min AMD bars (2023-01-01 to 2024-01-01) ===")
    api_key = os.environ.get("POLYGON_API_KEY")
    if not api_key or api_key == "your_polygon_api_key_here":
        return TestResult(
            "Polygon 5-min AMD",
            False,
            "POLYGON_API_KEY not set in .env",
            ["set POLYGON_API_KEY before running"],
        )

    df = fetch_polygon_aggs(api_key, "AMD", 5, "minute", "2023-01-01", "2024-01-01")
    if df.empty:
        return TestResult("Polygon 5-min AMD", False, "no data returned", ["empty result"])

    issues: list[str] = []
    n = len(df)
    if not (18_000 <= n <= 22_000):
        issues.append(f"bar count {n} outside 18,000-22,000")

    if df["time"].dt.tz is None:
        issues.append("timestamps are timezone-naive")
    elif str(df["time"].dt.tz) not in ("America/New_York", "US/Eastern"):
        issues.append(f"timestamps not in ET: {df['time'].dt.tz}")

    issues.extend(check_5min_alignment(df))
    gap_issues = check_rth_gaps(df, max_gap_minutes=10)
    if gap_issues:
        issues.append(f"{len(gap_issues)} RTH gap(s) > 10min (first: {gap_issues[0]})")

    print("\n  First 10 timestamps:")
    for ts in df["time"].head(10):
        print(f"    {ts}")
    print("\n  Last 10 timestamps:")
    for ts in df["time"].tail(10):
        print(f"    {ts}")

    out_path = DATA_DIR / "polygon" / "AMD" / "5min.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)
    print(f"\n  saved {n} bars -> {out_path}")

    passed = not issues
    summary = f"{n} bars; first {df['time'].iloc[0]}, last {df['time'].iloc[-1]}"
    return TestResult("Polygon 5-min AMD", passed, summary, issues)


# ------------------------------------------------------------------------- #
# Test 2: Kraken recent crypto data                                         #
# ------------------------------------------------------------------------- #

def test_2_kraken() -> TestResult:
    print("\n=== Test 2: Kraken BTC/USD 1-hour OHLC ===")
    params = {"pair": "XBTUSD", "interval": 60}
    r = requests.get(KRAKEN_OHLC_URL, params=params, timeout=30)
    r.raise_for_status()
    payload = r.json()
    if payload.get("error"):
        return TestResult("Kraken 1h BTCUSD", False, f"API error: {payload['error']}", [str(payload['error'])])

    result = payload.get("result", {})
    pair_key = next((k for k in result if k != "last"), None)
    if not pair_key:
        return TestResult("Kraken 1h BTCUSD", False, "no pair data in response", ["missing pair key"])

    rows = result[pair_key]
    df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "vwap", "volume", "count"])
    df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
    for c in ["open", "high", "low", "close", "vwap", "volume"]:
        df[c] = df[c].astype(float)

    issues: list[str] = []
    n = len(df)
    if not (650 <= n <= 800):
        issues.append(f"bar count {n} outside expected ~720 (650-800)")

    if df["time"].dt.tz != UTC:
        issues.append(f"timestamps not UTC: {df['time'].dt.tz}")

    bad_align = df[~((df["time"].dt.minute == 0) & (df["time"].dt.second == 0))]
    if not bad_align.empty:
        issues.append(f"{len(bad_align)} bars not 1-hour aligned")

    last_ts = df["time"].iloc[-1]
    age = datetime.now(UTC) - last_ts
    if age > timedelta(hours=2):
        issues.append(f"last bar is {age} old (> 2h)")

    out_path = DATA_DIR / "kraken" / "BTCUSD" / "1h.parquet"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)
    print(f"  saved {n} bars -> {out_path}")
    print(f"  last bar: {last_ts} (age: {age})")

    passed = not issues
    summary = f"{n} bars; last {last_ts}"
    return TestResult("Kraken 1h BTCUSD", passed, summary, issues)


# ------------------------------------------------------------------------- #
# Test 3: Kraken CSV bulk pipeline (proof of concept)                       #
# ------------------------------------------------------------------------- #

def download_and_aggregate_kraken_csv(pair: str, quarter: str, interval_min: int) -> pd.DataFrame:
    """
    STUB — do not run yet.

    Kraken publishes quarterly trade-history CSV bundles (per Google Drive folder
    linked from their support article). Each CSV row is a single trade:
        timestamp_unix, price, volume

    Real implementation would:
      1. Resolve the per-pair, per-quarter file (e.g. 'XBTUSD_2023Q1.csv')
      2. Stream-download (files can be multiple GB) into a local cache
      3. Load with pandas in chunks (chunksize=1_000_000) to avoid OOM
      4. Resample trades into OHLCV bars at `interval_min` using groupby(pd.Grouper)
      5. Persist to parquet partitioned by year/month

    Args:
        pair: Kraken pair code, e.g. 'XBTUSD'
        quarter: e.g. '2023Q1'
        interval_min: target bar size in minutes

    Returns:
        Empty DataFrame; this is a stub.
    """
    raise NotImplementedError("Bulk CSV ingest is a Phase 1+ task")


def test_3_kraken_csv() -> TestResult:
    print("\n=== Test 3: Kraken CSV bulk pipeline (URL reachability only) ===")
    issues: list[str] = []
    try:
        r = requests.head(KRAKEN_CSV_DOC_URL, allow_redirects=True, timeout=15)
        ok = r.status_code < 400
        print(f"  doc URL: {KRAKEN_CSV_DOC_URL} -> HTTP {r.status_code}")
        if not ok:
            issues.append(f"doc URL returned HTTP {r.status_code}")
    except requests.RequestException as ex:
        issues.append(f"doc URL unreachable: {ex}")
        ok = False

    print(f"  download landing folder (Google Drive): {KRAKEN_CSV_BASE}")
    print(
        "\n  TODO (Phase 1+): implement real bulk ingest.\n"
        "    - Files are quarterly per pair (e.g. XBTUSD_2023Q1.csv); each row is a single trade.\n"
        "    - Total size per pair-year is multi-GB; download to local cache, never load whole file.\n"
        "    - Stream via pandas.read_csv(chunksize=1_000_000), groupby Grouper(freq=...) to OHLCV.\n"
        "    - Persist parquet partitioned by year/month under data/kraken/<PAIR>/<interval>/.\n"
        "    - Then concatenate with API top-up (Test 2 endpoint) for current quarter."
    )

    summary = "doc URL reachable" if ok else "doc URL not reachable"
    return TestResult("Kraken CSV docs reachable", ok, summary, issues)


# ------------------------------------------------------------------------- #
# Driver                                                                    #
# ------------------------------------------------------------------------- #

def print_report(results: list[TestResult]) -> bool:
    print("\n" + "=" * 60)
    print("PHASE 0.5 VERIFICATION REPORT")
    print("=" * 60)
    for i, r in enumerate(results, 1):
        status = "PASS" if r.passed else "FAIL"
        print(f"Test {i}: [{status}] {r.name}")
        print(f"        {r.summary}")
        for issue in r.issues:
            print(f"        - {issue}")
    overall = all(r.passed for r in results)
    print("-" * 60)
    print(f"Overall: {'GO' if overall else 'NO GO'} for Phase 1")
    print("=" * 60)
    return overall


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=True)

    results: list[TestResult] = []

    r1 = test_1_polygon()
    results.append(r1)
    if not r1.passed:
        print_report(results)
        print("\nTest 1 failed — stopping before running remaining tests.")
        return 1

    r2 = test_2_kraken()
    results.append(r2)
    if not r2.passed:
        print_report(results)
        print("\nTest 2 failed — stopping before running Test 3.")
        return 1

    r3 = test_3_kraken_csv()
    results.append(r3)

    overall = print_report(results)
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
