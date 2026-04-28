## Archetype: mean_reversion

**Thesis (broad):** Asset prices that move strongly in one direction over a short window tend to partially revert. Buy weakness in established uptrends; sell strength in established downtrends.

**References:**
- Larry Connors, *Short Term Trading Strategies That Work* (2008)
- Jegadeesh (1990), *Evidence of predictable behavior of security returns*

**In scope:**
- Lower-frequency strategies (1h, 1d) where reversion has time to materialize.
- Conditions that combine an "extended" signal (RSI low/high, z-score, BB band) with a "trend filter" (price vs SMA, ROC).
- Asset classes: stocks, crypto.

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
