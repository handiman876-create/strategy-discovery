## Archetype: seasonality

**Thesis (broad):** Specific calendar effects (day-of-week, month-end, holidays) create exploitable patterns due to structural flows: dividends, rebalancing, tax-loss harvesting, retirement contributions.

**References:**
- "Sell in May and Go Away" — Bouman & Jacobsen (2002)
- Turn-of-month effect — Lakonishok & Smidt (1988)

**In scope:**
- 1d bars on US stocks.
- Time-of-day-able patterns (less rich than calendar patterns; see notes).

**Constraints / DSL caveat:**
- The DSL currently exposes `time_of_day` (minutes since midnight, ET). Day-of-week and day-of-month gates are NOT yet expressible. Phase 3 limits this archetype to time-of-day-able rules. If your idea genuinely requires day-of-week or day-of-month, decline gracefully — produce a simple time-of-day strategy as a placeholder, with a thesis that names what would actually be desired.

**Counter-examples (do NOT generate):**
- Strategies that only use indicators and have no calendar trigger.
- Random date conditions with no plausible flow-based explanation.
- Anything intraday — those are microstructure.

**Diversity nudge:**
- Different time-of-day windows (open-hour, midday, last-hour).
- Different combinations with broader trend filters (SMA / ROC).
