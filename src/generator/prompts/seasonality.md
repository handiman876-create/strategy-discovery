## Archetype: seasonality

**Thesis (broad):** Specific calendar effects (day-of-week, month-end, holidays) create exploitable patterns due to structural flows: dividends, rebalancing, tax-loss harvesting, retirement contributions.

**References:**
- "Sell in May and Go Away" — Bouman & Jacobsen (2002)
- Turn-of-month effect — Lakonishok & Smidt (1988)

**In scope:**
- 1d bars on US stocks.
- Time-of-day-able patterns (less rich than calendar patterns; see notes).

**Hard constraints for this archetype — a spec that violates either one is rejected:**

- **`timeframes` must be exactly `["1d"]`.** Nothing else. Not `["1h"]`, not `["5m"]`, not `["1d", "1h"]` — the registry allows a single timeframe for seasonality and the translator rejects anything else outright, so an intraday-timeframe seasonality spec is a wasted retry, not a near miss.
  - ✓ `"timeframes": ["1d"]`
  - ✗ `"timeframes": ["15m"]` — DO NOT do this; rejected before backtest
  - ✗ `"timeframes": ["1d", "15m"]` — DO NOT do this; one timeframe only
- **`thesis` must be at most 350 characters.** The schema's hard ceiling is 400, and this archetype has repeatedly blown it — 350 is your budget, and the 50-character gap is deliberate slack, not room to spend. Two or three sentences. State the flow, the window, and the filter; do not narrate what the DSL cannot express (see the caveat below).

**Example thesis (good — 293 characters, copy this shape):**
```
Institutional rebalancing flow concentrates in the afternoon, so the back half of
the session carries a directional drift. Trade the whole afternoon window rather
than a single hour, gated by a broad ROC(20) trend filter, so the edge is filtered
and still fires often enough to tighten the CI.
```
Why it works: names the flow (rebalancing), the window (whole afternoon), and the filter (ROC(20)) in three clauses, and stops.

**BAD — DO NOT write a thesis like this (it is what actually gets rejected here):**
```
Placeholder seasonality strategy. The desired edge is a day-of-week effect in
which Monday returns following an up Friday are systematically positive because
retirement-contribution inflows settle over the weekend, and the ideal formulation
would additionally gate on the turn-of-month window to capture pension rebalancing,
and would exclude holiday-shortened weeks, and ... [runs past 400 chars]
```
Why it fails: it spends its whole budget describing a rule the DSL cannot express, trips `max_length=400`, and burns a retry without ever having proposed something backtestable.

**Constraints / DSL caveat:**
- The DSL currently exposes `time_of_day` (minutes since midnight, ET). Day-of-week and day-of-month gates are NOT yet expressible. Phase 3 limits this archetype to time-of-day-able rules. If your idea genuinely requires day-of-week or day-of-month, decline gracefully — produce a simple time-of-day strategy as a placeholder. Name the missing gate in **one short clause** ("day-of-week gate not yet expressible; approximated by time-of-day") and spend the rest of the 350 characters on the strategy you are actually submitting. Do not describe the ideal rule at length — that is the single most common way this archetype exceeds the thesis limit.

**Counter-examples (do NOT generate):**
- Strategies that only use indicators and have no calendar trigger.
- Random date conditions with no plausible flow-based explanation.
- Anything intraday — those are microstructure.
- **Ultra-narrow single-hour windows** (e.g. "only the last hour" / "only the power hour") with no broader filter. These fire too rarely, so their bootstrap CI is wide and `ci_lower` lands below 1.0 — the single most common way this archetype fails canonical. Historic failures: `last_hour_momentum_seasonality`, `power_hour_momentum_seasonality`.

**Robustness note (read the global objective first):** the gate is `ci_lower > 1.0 across many trades`, not high average PF. A time-of-day rule must be paired with a broad trend/mean-reversion filter and use a wide enough window to clear ~50 trades/year/symbol. If your seasonality thesis can only express itself as a rare narrow trigger, prefer a broader formulation.

**Diversity nudge:**
- Prefer WIDER windows (e.g. whole morning or whole afternoon session) over single-hour slices.
- Combine the calendar/time trigger with broader trend filters (SMA / ROC) so the edge is filtered, not just windowed.
