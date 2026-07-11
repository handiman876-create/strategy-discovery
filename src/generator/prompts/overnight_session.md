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
- **Over-gated overnights that rarely trigger** (e.g. a BB-squeeze or multi-condition regime gate on top of the overnight rule). Stacking a rare condition onto the close→open move produces too few trades, a wide bootstrap CI, and `ci_lower` below 1.0. Historic failure: `overnight_bb_squeeze_reversion`.

**Robustness note (read the global objective first):** the gate is `ci_lower > 1.0 across many trades`, not high average PF. The overnight move can fire ~daily — keep it that way. Use at most ONE light conditional filter (sign/magnitude of prior-day return, or a slow trend filter); do not stack several rare conditions. Aim to keep the strategy trading frequently enough for a tight, above-1.0 CI lower bound.

**Diversity nudge:**
- Lightly conditional overnights (a single gate on prior-day return sign or magnitude).
- Trend-filtered overnights (only when SMA(50) > SMA(200)) — one filter, not several.
- Reversion-based overnights (gate on RSI of daily closes).
