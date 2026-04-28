# Data Provider Notes

Empirical observations about what each provider actually returns, recorded as we hit edges. Update when behavior changes or when we move to a new account tier.

---

## Tradier

**Account tier observed:** production API token + market data add-on (~$10/mo).
**Date of observation:** 2026-04-27.
**Token env var:** `TRADIER_PRODUCTION_API_TOKEN` (28 chars, production endpoint `api.tradier.com`).

### Intraday timesales — rolling window only

**Endpoint:** `GET https://api.tradier.com/v1/markets/timesales`
**Auth:** `Authorization: Bearer <token>`, `Accept: application/json`

**Request that failed (Phase 0.5 Test 1, repeated for ~20 chunks across 2023):**
```
params:
  symbol=AMD
  interval=5min
  start=2023-01-01 00:00
  end=2023-01-19 00:00
  session_filter=all
```

**Response:**
```
HTTP 400
body: Invalid parameter, start: must be on or after 2026-02-27 00:00:00.
```

The cutoff date in the error (`2026-02-27`) is 59 days before the request date (`2026-04-27`). We have not tested whether the window is exactly 60 calendar days, "current + previous month," or something else — only that on 2026-04-27 anything before 2026-02-27 was rejected.

**Request that succeeded (recent intraday):**
```
params:
  symbol=AMD
  interval=5min
  start=2026-04-21 09:30
  end=2026-04-21 16:00
  session_filter=all
→ HTTP 200, 79 bars (full RTH session, 9:30 → 16:00 ET).
```

**Implication:** The `timesales` endpoint at this account tier cannot serve multi-year intraday history. The DESIGN.md v2 assumption — Tradier as primary stocks provider for the strategy framework — does not hold for any intraday-bar strategy that needs a year+ of history. Logged as project memory.

### Daily history — works for years back

**Endpoint:** `GET https://api.tradier.com/v1/markets/history`

**Request:**
```
params:
  symbol=AMD
  interval=daily
  start=2023-01-01
  end=2024-01-01
→ HTTP 200, 250 trading days (2023-01-03 → 2023-12-29).
```

Daily bars appear unaffected by the rolling window. We have not probed how far back daily history goes; 2023 was sufficient for the verification.

### Open questions (unresolved as of 2026-04-27)

- Is the intraday window exactly 60 days, or some other rule (e.g. "current + previous calendar month")? Probe with a few boundary dates if it matters.
- Does Tradier offer a higher tier with multi-year intraday? Their docs reference a separate "historical pricing" endpoint — unclear if it's bundled with the data add-on we have.
- Does the 1-minute interval have a *shorter* window than 5-minute? Not tested.

---

## Kraken

**Account tier:** public/unauthenticated REST endpoints (no key required for OHLC).
**Date of observation:** 2026-04-27.

### REST OHLC — recent only, free

**Endpoint:** `GET https://api.kraken.com/0/public/OHLC`

**Request:**
```
params:
  pair=XBTUSD
  interval=60   (minutes, so 1h bars)
→ HTTP 200, 721 bars; last bar 2026-04-27 01:00:00 UTC (~54 min old).
```

The endpoint returns only the most recent ~720 bars regardless of how far back you ask (per Kraken docs). For any deeper history, the bulk CSV pipeline below is required.

### Bulk CSV trade history

**Doc URL (reachable, HTTP 200 on 2026-04-27):** https://support.kraken.com/hc/en-us/articles/360047124832
**Drive folder:** https://drive.google.com/drive/folders/1jLG14CGwhzCJuKVDcUjFK8TmS9NRLP82

Quarterly per-pair files of individual trades (timestamp, price, volume). Multi-GB per pair-year. Not yet ingested — stub function in `scripts/verify_data.py:download_and_aggregate_kraken_csv`. Phase 1+ work.

---

## Polygon (not currently used)

Demoted to fallback in DESIGN.md v2 on the assumption Tradier would cover intraday. Given the Tradier intraday-window finding above, Polygon is back in the conversation as a candidate primary stocks provider. Plan tier and pricing not yet re-checked as of this note.
