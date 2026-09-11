## Archetype: overnight_session

**Thesis (broad):** Overnight returns differ systematically from intraday returns due to news, foreign markets, retail flow patterns. Buy near close / sell near open, or the reverse, based on which side carries the historical edge.

**References:**
- Lou, Polk, Skouras (2019), *A Tug of War: Overnight versus Intraday*

**In scope:**
- 1d bars on US stocks.
- Conditional overnight rules (e.g. only enter overnight long if today's intraday return < 0).

**Engine caveat:**
- Phase-3 engine processes daily bars; overnight strategies are approximated as "enter long on today's close, hold one bar, exit at next bar's open" via spec entry rules + a 1-bar exit. Use `daily_return` to gate.

**Hard constraint — `thesis` must be at most 350 characters.** The schema's hard ceiling is 400 (`src/generator/spec.py:183`); 350 is your budget and the 50-character gap is deliberate slack, not room to spend. **This archetype is currently the worst offender: 9.1% of post-fix attempts (3 of 33) were rejected for exceeding 400 characters**, each one a wasted retry.

Two failure shapes specific to overnight theses:

1. **Restating both directions symmetrically.** The long and short cases are mirror images — say the rule once and note it is symmetric.
2. **Appending a literature justification.** "This exploits the well-documented asymmetry between overnight and intraday returns" costs ~95 characters and tells the evaluator nothing; the archetype already assumes it.

**BAD — DO NOT write a thesis like this (409 chars, actually rejected 2026-09-08):**
```
When a stock's 20-bar z-score of closing prices is significantly negative (price
statistically stretched below its recent mean), institutional rebalancing and
overseas markets tend to push the price higher overnight. When z-score is
significantly positive, the overnight return tends to be negative as elevated
prices revert. This exploits the well-documented asymmetry between overnight and
intraday returns.
```
Why it fails: the second sentence is the first one inverted, and the third is a citation. Both directions and the justification can go in one clause.

**GOOD — the same idea in 205 characters:**
```
Closes stretched from their 20-bar mean revert overnight as overseas flow and
rebalancing hit the open. Enter against a z-score(20) extreme, symmetrically
long and short, and hold one bar to the next open.
```

**Counter-examples (do NOT generate):**
- Intraday-only strategies — those are microstructure.
- Multi-day holding strategies — those are momentum or seasonality.
- **Over-gated overnights that rarely trigger** (e.g. a BB-squeeze or multi-condition regime gate on top of the overnight rule). Stacking a rare condition onto the close→open move produces too few trades, a wide bootstrap CI, and `ci_lower` below 1.0. Historic failure: `overnight_bb_squeeze_reversion`.

**Robustness note (read the global objective first):** the gate is `ci_lower > 1.0 across many trades`, not high average PF. The overnight move can fire ~daily — keep it that way. Use at most ONE light conditional filter (sign/magnitude of prior-day return, or a slow trend filter); do not stack several rare conditions. Aim to keep the strategy trading frequently enough for a tight, above-1.0 CI lower bound.

**Diversity nudge:**
- Lightly conditional overnights (a single gate on prior-day return sign or magnitude).
- Trend-filtered overnights (only when SMA(50) > SMA(200)) — one filter, not several.
- Reversion-based overnights (gate on RSI of daily closes).
