## Archetype: momentum

**Thesis (broad):** Assets that have outperformed continue to outperform over similar windows. Persistent trends in returns are exploitable on the time horizon of the trend itself.

**References:**
- Jegadeesh & Titman (1993), *Returns to Buying Winners and Selling Losers*
- Asness, Moskowitz, Pedersen (2013), *Value and Momentum Everywhere*

**In scope:**
- Daily-bar (1d) only — momentum on intraday scales is microstructure.
- Persistent-return signals: ROC over multi-month windows, MACD histograms, price-vs-MA structure.
- Asset classes: stocks, crypto.

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
