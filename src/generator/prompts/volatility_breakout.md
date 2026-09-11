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

**Hard constraint — `thesis` must be at most 350 characters.** The schema's hard ceiling is 400 (`src/generator/spec.py:183`); 350 is your budget and the 50-character gap is deliberate slack, not room to spend. **12 attempts in this archetype have been rejected for exceeding 400 characters (5.0% pre-fix).** Post-fix attempts are clean so far, but only 26 of them exist — do not read that as headroom.

The failure shape specific to breakout theses: **restating indicator parameters in prose.** `"the upper Bollinger Band (period=20, k=1.5)"` and `"ATR(10) is rising above its own SMA(10)"` duplicate values the spec already carries in `indicators` and `parameters`. The thesis explains *why* the trigger should work; the JSON says *what* it is.

**BAD — DO NOT write a thesis like this (407 chars, actually rejected 2026-08-31):**
```
On daily bars, when price closes above the upper Bollinger Band (period=20,
k=1.5) signaling a breakout from compression AND ATR(10) is rising above its own
SMA(10) confirming expanding volatility, enter long. Enter short when close
breaks below the lower band with ATR expanding. Exit on a cross back through the
midline. This fires frequently on both sides and works across trending and
range-bound names.
```
Why it fails: it is the spec transcribed into English — timeframe, every period, every `k` — and then adds a robustness claim the backtest will decide anyway.

**GOOD — the same idea in 228 characters:**
```
Compressed volatility resolves directionally, so a close outside the Bollinger
band while ATR is expanding marks a breakout rather than noise. Trade the break
symmetrically both ways and exit on a cross back through the midline.
```

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
