## Archetype: momentum

**Thesis (broad):** Assets that have outperformed continue to outperform over similar windows. Persistent trends in returns are exploitable on the time horizon of the trend itself.

**References:**
- Jegadeesh & Titman (1993), *Returns to Buying Winners and Selling Losers*
- Asness, Moskowitz, Pedersen (2013), *Value and Momentum Everywhere*

**In scope:**
- Daily-bar (1d) only — momentum on intraday scales is microstructure.
- Persistent-return signals: ROC over multi-month windows, MACD histograms, price-vs-MA structure.
- Asset classes: stocks, crypto.

**Hard constraint — `thesis` must be at most 350 characters.** The schema's hard ceiling is 400 (`src/generator/spec.py:183`); 350 is your budget and the 50-character gap is deliberate slack, not room to spend. This archetype has a good record — 2 rejections in 346 attempts (0.6%) — and the rule exists to keep it that way.

The failure shape specific to momentum theses: **an appended generalization boast.** Claims like "this works across trending and range-bound names by relying on relative rate-of-change rather than a long-term trend gate" run ~120 characters and assert exactly what the diverse-basket evaluation exists to test. State the mechanism; let `ci_lower` make the claim.

**BAD — DO NOT write a thesis like this (411 chars, actually rejected 2026-07-20):**
```
Assets showing positive 21-day rate-of-change with a rising MACD histogram
exhibit persistent short-to-medium-term momentum. The MACD histogram crossing
above zero confirms trend acceleration, while ROC(21) filters for genuine recent
strength. Mirror conditions capture downside momentum. This works across
trending and range-bound names by relying on relative rate-of-change rather than
a long-term trend gate.
```
Why it fails: the middle sentence restates the first in indicator terms, and the last is an unverifiable robustness claim. Together they are ~200 characters of overflow.

**GOOD — the same idea in 208 characters:**
```
Recent relative strength persists over similar horizons. Enter when ROC(21) is
positive and the MACD histogram crosses up, confirming acceleration rather than
a single strong bar, with mirrored short entries.
```

**Counter-examples (do NOT generate):**
- Mean reversion: "buy when RSI < 30" is the OPPOSITE of momentum.
- Volatility breakout based on price levels — those have their own archetype.
- Intraday strategies (5m/15m/1h) — those are microstructure.

**Diversity nudge:**
- Use different lookback horizons (ROC 21 vs ROC 63 vs ROC 126).
- MA-based variants: SMA(50)/SMA(200) cross vs price-vs-SMA.
- MACD signal-line crossovers.

Suggested entry/exit shapes:
- Long: long-window return > threshold AND shorter-window return positive → long; exit on cross-down.
- Short: mirror conditions for downtrends.
