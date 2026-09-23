## Archetype: volume

**Thesis (broad):** Volume patterns are more universal than price patterns: institutional accumulation shows up across sectors regardless of price trend. A stock with unusually high up-close volume behaves similarly whether it is a semiconductor or a utility. Volume is the primary signal; price indicators may only filter.

**References:**
- Joseph Granville, *Granville's New Key to Stock Market Profits* (1963) — On-Balance Volume.
- Berkowitz, Logue, Noser (1988), *The Total Cost of Transactions on the NYSE* — VWAP as the institutional execution benchmark.

**In scope:**
- Timeframe: `["1d"]` only. Any other timeframe is rejected.
- Asset class: stocks.
- Primary signal: `obv_zscore(period)` or `vwap_dev(period)`.
  - `obv_zscore` — z-score of On-Balance Volume over `period` bars. `> 1.5` = unusually strong accumulation; `< -1.5` = distribution.
  - `vwap_dev` — `(close − rolling VWAP) / ATR`. `< -1.5` = price well below where volume actually traded; `> 1.5` = well above.

**Hard constraint — at least one of `obv_zscore` / `vwap_dev` must appear in `entry_long` or `entry_short`.** Declaring it in `indicators` and never referencing it in an entry rule is rejected. EMA, SMA, RSI, MACD, ATR and z-score of price are allowed **only as filters** — a secondary clause ANDed onto a volume trigger, never the trigger itself.

**Cross-symbol consistency is the target.** You are scored on `ci_lower` across a diverse basket spanning sectors, not on one ticker. That is the point of this archetype: price-only rules (EMA/RSI crosses) have been generated and evaluated over a thousand times on this basket without the confidence bound clearing 1.0. Volume is a different signal space. Prefer rules whose logic does not depend on a sector's typical volatility or trend — z-scored and ATR-normalized inputs already are.

**Hard constraint — `thesis` must be at most 350 characters.** The schema's hard ceiling is 400 (`src/generator/spec.py:183`); 350 is your budget and the 50-character gap is deliberate slack, not room to spend. Name each indicator once, and write long and short as one symmetric clause rather than two sentences.

**BAD — DO NOT write a spec like this. DO NOT use EMA as the primary signal — it belongs in this family but as a filter only:**
```
thesis: Institutional buying shows up as trend strength. Enter long when
        EMA(9) crosses above EMA(21), confirmed by OBV z-score above zero.
entry_long: ema_9 > ema_21 AND obv_z > 0
```
Why it fails: the EMA cross decides every entry; `obv_z > 0` is true about half the time and only thins the trades. This is momentum wearing a volume costume, and it lands in the same price-only cluster this archetype exists to escape.

**GOOD — obv_zscore as the primary signal, 237 characters:**
```
thesis: Unusually strong up-close volume marks institutional accumulation,
        which persists across sectors. Enter long when OBV z-score(20) exceeds
        1.5 while close holds above SMA(50) as a trend filter; exit when OBV
        z-score falls back below zero.
entry_long: obv_z > 1.5 AND close > sma_50
exit_long:  obv_z < 0
```
Why it works: the volume extreme is the trigger and fires on its own schedule; the SMA only vetoes entries against the trend.

**Counter-examples (do NOT generate):**
- EMA/RSI/MACD as the entry trigger with a volume indicator bolted on.
- Price-only strategies with no volume indicator — rejected outright.
- Intraday VWAP scalps anchored to time-of-day — those are microstructure.

**Diversity nudge:**
- OBV extremes (accumulation / distribution) vs VWAP stretch (price far from where volume traded).
- Agreement vs divergence: price below VWAP while OBV rises is a different thesis from both pushing the same way.
- Different `period` choices (10–60) — shorter catches bursts, longer catches campaigns.
