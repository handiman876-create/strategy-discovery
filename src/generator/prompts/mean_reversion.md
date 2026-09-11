## Archetype: mean_reversion

**Thesis (broad):** Asset prices that move strongly in one direction over a short window tend to partially revert. Buy weakness in established uptrends; sell strength in established downtrends.

**References:**
- Larry Connors, *Short Term Trading Strategies That Work* (2008)
- Jegadeesh (1990), *Evidence of predictable behavior of security returns*

**In scope:**
- Lower-frequency strategies (1h, 1d) where reversion has time to materialize.
- Conditions that combine an "extended" signal (RSI low/high, z-score, BB band) with a "trend filter" (price vs SMA, ROC).
- Asset classes: stocks, crypto.

**Hard constraint — `thesis` must be at most 350 characters.** The schema's hard ceiling is 400 (`src/generator/spec.py:183`); 350 is your budget and the 50-character gap is deliberate slack, not room to spend. This archetype has never been rejected for thesis length — 0 in 1184 attempts — and the rule is here to keep it that way, not because it has a problem.

It has come closer than the record suggests. This thesis was **accepted at exactly 400 characters** on 2026-08-09 — one character more and it would have been a wasted retry:

**BAD — DO NOT write to the ceiling like this (400 chars, passed with zero slack):**
```
When a stock's 60-bar percent rank drops below 0.15 (price near its 60-day low)
AND the EMA(10) is still above EMA(50) (medium-term trend intact), the short-term
weakness is statistically overextended and likely to revert. Mirror logic:
percent rank above 0.85 AND EMA(10) below EMA(50) signals overbought short-term,
sell short. Exit on mean-reversion signal from percent rank crossing the midpoint.
```
Why it is a problem even though it validated: every threshold is transcribed from the spec, each indicator is glossed in parentheses, and the short side is written out in full. There is no margin — the same thesis with one more qualifying clause overflows.

**GOOD — the same idea in 241 characters:**
```
Short-term weakness inside an intact medium-term uptrend is overextended rather
than a trend change, so it reverts. Enter on a percent_rank(60) extreme while
the EMA structure still favours the trend, mirrored short, exiting at the
midpoint.
```

**Counter-examples (do NOT generate):**
- Trend-following: "buy when 50-day MA crosses above 200-day MA" — that's momentum, not mean reversion.
- Volatility breakouts: "buy when close exceeds 20-day high" — that's breakout, not reversion.
- Time-of-day rules without reversion logic — that's seasonality or microstructure.

**Diversity nudge:**
- Vary the "extended" signal (RSI vs z-score vs BB-distance vs ROC).
- Vary the trend filter (SMA periods, ROC sign, percent_rank position).
- Vary the exit condition (revert to mean, RSI cross, time-stop via param).

Suggested entry/exit shapes:
- Long: trend-filter bullish AND short-term oversold → enter long; exit when oversold-ness mean-reverts.
- Short: trend-filter bearish AND short-term overbought → enter short; exit on revert.
