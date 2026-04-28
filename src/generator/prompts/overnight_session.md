## Archetype: overnight_session

**Thesis (broad):** Overnight returns differ systematically from intraday returns due to news, foreign markets, retail flow patterns. Buy near close / sell near open, or the reverse, based on which side carries the historical edge.

**References:**
- Lou, Polk, Skouras (2019), *A Tug of War: Overnight versus Intraday*

**In scope:**
- 1d bars on US stocks.
- Conditional overnight rules (e.g. only enter overnight long if today's intraday return < 0).

**Engine caveat:**
- Phase-3 engine processes daily bars; overnight strategies are approximated as "enter long on today's close, hold one bar, exit at next bar's open" via spec entry rules + a 1-bar exit. Use `daily_return` to gate.

**Counter-examples (do NOT generate):**
- Intraday-only strategies — those are microstructure.
- Multi-day holding strategies — those are momentum or seasonality.

**Diversity nudge:**
- Conditional overnights (gate on prior-day return, sign, magnitude).
- Trend-filtered overnights (only when SMA(50) > SMA(200)).
- Reversion-based overnights (gate on RSI of daily closes).
