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
- **Ultra-narrow single-hour windows** (e.g. "only the last hour" / "only the power hour") with no broader filter. These fire too rarely, so their bootstrap CI is wide and `ci_lower` lands below 1.0 — the single most common way this archetype fails canonical. Historic failures: `last_hour_momentum_seasonality`, `power_hour_momentum_seasonality`.

**Robustness note (read the global objective first):** the gate is `ci_lower > 1.0 across many trades`, not high average PF. A time-of-day rule must be paired with a broad trend/mean-reversion filter and use a wide enough window to clear ~50 trades/year/symbol. If your seasonality thesis can only express itself as a rare narrow trigger, prefer a broader formulation.

**Diversity nudge:**
- Prefer WIDER windows (e.g. whole morning or whole afternoon session) over single-hour slices.
- Combine the calendar/time trigger with broader trend filters (SMA / ROC) so the edge is filtered, not just windowed.
