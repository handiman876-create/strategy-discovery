## Archetype: microstructure

**Thesis (broad):** Institutional order flow creates predictable intraday patterns (open auction imbalance, opening-range breakouts, closing imbalance, lunch-hour drift). Strategies fit a single trading day.

**References:**
- O'Hara, *Market Microstructure Theory* (1995)
- Casper-style opening-range scalping is in this family.

**In scope:**
- 5m or 15m bars on US stocks only.
- Time-of-day-anchored entries.
- ATR-scaled stop / target via parameters.
- Asset class: stocks.

**Counter-examples (do NOT generate):**
- Daily-bar momentum or reversion — those have their own archetypes.
- Strategies that hold overnight — microstructure typically exits intraday.
- Strategies that don't use time-of-day at all.

**Casper as positive example:**
The user's hand-written reference strategy:
1. Captures the high/low of the first 5-min bar (opening range).
2. After 2 consecutive closes outside, enters on a wick-back retest.
3. Stop at opposite OR boundary; target at risk × 2.
4. Exits at 15:50 ET if neither stop nor target has hit.

**Diversity nudge:**
- Different time-of-day windows (open vs midday vs close).
- Different volatility filters (ATR-based).
- Different price-vs-band entries — but anchored to a session-relative time.
