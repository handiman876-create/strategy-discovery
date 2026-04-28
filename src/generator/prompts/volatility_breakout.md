## Archetype: volatility_breakout

**Thesis (broad):** Periods of compressed volatility precede directional breakouts. Enter on range expansion, scaled by ATR; exit on opposite signal or stop.

**References:**
- Donchian channels (Richard Donchian, 1960s)
- Turtle Traders (Dennis & Eckhardt, 1983)
- John Bollinger, *Bollinger on Bollinger Bands* (2001)

**In scope:**
- 1h or 1d bars.
- ATR-driven entries; close vs Bollinger upper/lower; close > recent high (Donchian-style — but the DSL doesn't yet have rolling-high; approximate via percent_rank near 1.0).
- Asset classes: stocks, crypto.

**Counter-examples (do NOT generate):**
- Mean reversion against the band (e.g. buy when close < BB_lower) — that's reversion, not breakout.
- Pure momentum without an ATR / volatility trigger.
- Microstructure scalps using intraday session structure (separate archetype).

**Diversity nudge:**
- Vary ATR period (10, 14, 20).
- Vary BB k (1.5, 2.0, 2.5).
- Combine a volatility trigger (ATR rising or band-width expanding) with a directional trigger (close above band).

Suggested entry/exit shapes:
- Long: close > BB_upper AND ATR rising → long; exit when close < BB_mid.
- Long: `percent_rank` with `period=60` > 0.95 (close near 60-bar high) AND ATR rising → long.
