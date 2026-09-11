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

**Hard constraint — `thesis` must be at most 350 characters.** The schema's hard ceiling is 400 (`src/generator/spec.py:183`); 350 is your budget and the 50-character gap is deliberate slack, not room to spend. **3.3% of post-fix attempts here (1 of 30) were rejected for exceeding 400 characters**, and 8 of the last 28 accepted theses landed in the 351–400 band with no slack left.

Two failure shapes specific to microstructure theses:

1. **Glossing the session window twice.** `"(780–870 min ET, ~13:00–14:30)"` states the same window in two units. The spec already carries the minute values in `time_of_day`; name the window once, in words.
2. **Spelling out long and short as separate sentences.** They are mirror images. One clause covers both.

**BAD — DO NOT write a thesis like this (409 chars, actually rejected 2026-09-11):**
```
In the early-afternoon window (780–870 min ET, ~13:00–14:30), post-lunch
institutional repositioning creates short-lived directional divergences
detectable by MACD histogram momentum and z-score mean-reversion. Enter long
when price is z-score depressed and MACD histogram turns positive; enter short
when z-score elevated and MACD histogram turns negative. Exit on z-score
normalization or at end of session.
```
Why it fails: the window is stated twice, then both entry directions are written out in full, leaving nothing for the exit but overflow.

**GOOD — the same idea in 271 characters:**
```
Post-lunch institutional repositioning drives short-lived divergences in the
early afternoon. Enter against a z-score(20) extreme confirmed by MACD
histogram turning in the reversion's direction, symmetrically both ways, and
exit on z-score normalization or at the close.
```

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
