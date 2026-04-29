# Strategy Discovery Framework — Design Document

**Project:** `/root/strategy-discovery`
**Status:** Phase 0 — Design (v2)
**Last updated:** April 26, 2026

---

## Changelog

- **v4 (Apr 27 PM):** Phase-2 build surfaced a second data-tier surprise: Polygon Stocks Starter caps history at exactly 5 years (rolling), not "5+ years" as v3 stated. Direct fetch of 2018-01 returned `NOT_AUTHORIZED`. Updated Section 3 and added Section 12.3. Section 5.1 / 5.3 rewritten: walk-forward IS the train/test mechanism, sliding (24mo train, 6mo test, 6mo step) windows over the available 2021-04 → 2024-12 span; holdout (2025+) remains sealed until final scoring. Three windows fit, meeting the §5 minimum.
- **v3 (Apr 27):** Resolved the v2.1 Tradier-window finding by switching primary stocks data to **Polygon Stocks Starter ($29/mo)**. Tradier demoted to live/recent-only backup, retained for the brokerage integration needed for paper/live trading later. Updated Section 3 (data layer), Risk 5 (cost — now ~$130/mo), and closed Risk 6. The empirical evidence behind this decision is preserved in `docs/data-provider-notes.md` and Section 12.
- **v2.1 (Apr 27):** Phase 0.5 verification surfaced a Tradier-tier limitation: intraday timesales is restricted to a rolling ~60-day window, not multi-year as assumed in v2. Added Section 12 (Known constraints) and `docs/data-provider-notes.md`. Pending decision on whether to upgrade Tradier, restore Polygon as primary intraday provider, or narrow scope.
- **v2 (Apr 26):** Switched primary data providers to Tradier (stocks) + Kraken (crypto). Polygon and Alpaca demoted to fallback options. Updated cost projections. Added explicit data verification step (Phase 0.5) before Phase 1 begins. Added Section 11 on user discipline checkpoints.
- **v1 (Apr 25):** Initial design with Polygon as primary stocks provider.

---

## 1. Purpose and non-goals

### Purpose

Build infrastructure to systematically generate, evaluate, and track candidate trading strategies across asset classes (stocks, ETFs, crypto) and timeframes. Surface strategies that demonstrate robust, generalizable edge through honest out-of-sample testing. Provide an audit trail so we can distinguish real edges from luck over time.

### Non-goals

The following things this framework will **not** do, and we will resist scope creep toward them:

- Predict the market or generate signals in real time
- Auto-deploy strategies to live trading
- Optimize a single "best" strategy through exhaustive parameter search
- Provide investment advice or financial recommendations
- Guarantee any strategy's future performance

The framework is a **research tool**. It surfaces candidates. Humans decide what to paper trade. Real-money deployment requires separate infrastructure (broker integration, position sizing, risk management) that is out of scope for this project.

### Commitments

The user has committed to:

1. Honoring train/test discipline even when results look tempting
2. Paper-trading any candidate for minimum 3 months before risking real money
3. Building this slowly and correctly rather than rushing to deployment

These commitments are the foundation. If they erode, the framework's value erodes with them.

---

## 2. System architecture

Six layers, each with a clear interface to the layer above. Layers can be developed and tested independently.

```
┌─────────────────────────────────────────────────────────────┐
│ Layer 6: Leaderboard & Review                               │
│   - Database of all generated strategies + their results    │
│   - CLI tools for filtering, sorting, surfacing candidates  │
└─────────────────────────────────────────────────────────────┘
                             ▲
┌─────────────────────────────────────────────────────────────┐
│ Layer 5: Generator Pipeline                                 │
│   - Archetype-driven strategy generation via Claude         │
│   - Spec → executable code translation                      │
│   - Reproducibility metadata logging                        │
└─────────────────────────────────────────────────────────────┘
                             ▲
┌─────────────────────────────────────────────────────────────┐
│ Layer 4: Evaluation Harness                                 │
│   - Train/test split enforcement                            │
│   - Walk-forward analysis                                   │
│   - Multi-symbol robustness scoring                         │
│   - Statistical significance testing                        │
└─────────────────────────────────────────────────────────────┘
                             ▲
┌─────────────────────────────────────────────────────────────┐
│ Layer 3: Strategy Framework                                 │
│   - Strategy base class with required metadata              │
│   - Parameter declarations with allowed ranges              │
│   - Asset-class and timeframe abstractions                  │
└─────────────────────────────────────────────────────────────┘
                             ▲
┌─────────────────────────────────────────────────────────────┐
│ Layer 2: Backtest Engine                                    │
│   - Multi-timeframe bar processing                          │
│   - Realistic execution (slippage, commissions, sessions)   │
│   - Asset-class-aware (stocks RTH, crypto 24/7)             │
└─────────────────────────────────────────────────────────────┘
                             ▲
┌─────────────────────────────────────────────────────────────┐
│ Layer 1: Data Layer                                         │
│   - Pluggable providers (Tradier stocks, Kraken crypto)     │
│   - Unified bar schema                                      │
│   - Local cache (parquet, organized by symbol/tf/provider)  │
│   - Resampling utilities                                    │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Technology choices

### Language and runtime

- **Python 3.11+** — modern asyncio, type hints, performance improvements
- **uv** for package management (faster than pip, better lockfile handling)
- **Virtual environment** at `.venv/` in the project root

### Data layer (revised in v3)

**Primary providers:**

- **Polygon (stocks)** — Stocks Starter plan ($29/mo) (rebranded to Massive in 2026)
  - **Exactly 5 years of intraday history** on a rolling basis (NOT "5+", contrary to v3 — confirmed 2026-04-27 PM, see §12.3). Empirical floor today: 2021-04-28.
  - Unlimited API calls on this tier; pagination via `next_url`
  - REST aggregates endpoint: `/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}`
  - Timestamps returned as UTC ms; we convert to ET for stocks at ingest

- **Kraken (crypto)** — free, public market data
  - REST `/0/public/OHLC` endpoint for recent bars (last 720 candles per request)
  - **Quarterly CSV trade history downloads** for deep historical depth (years of data)
  - We'll write a one-time CSV ingest pipeline that aggregates trades into bars at our target timeframes
  - Free for public market data; account exists but no API key needed for backtest data

**Backup / live-only providers:**

- **Tradier** — retained for two specific roles, not historical research:
  1. **Brokerage integration** for paper trading (Phase 2+) and live deployment (post-checkpoint 4). Same broker the user trades through, so execution mapping is direct.
  2. **Recent intraday cross-checks** within the rolling ~60-day window (see Section 12.1) when we want a second source against Polygon for live or near-live data.
  - Production token + market data add-on already paid (~$10/mo); kept on the stack.

**Fallback providers (not built in Phase 1, code in stubs only):**

- **Alpaca** — free tier, useful for stocks redundancy if Polygon ever has an outage

**Why Polygon over Tradier as primary stocks source (v3):** Phase 0.5 verification on 2026-04-27 showed Tradier production + market data add-on caps the `markets/timesales` `start` parameter to a rolling window (observed: 59 days). Multi-year intraday backtests — required by Section 4 archetypes 4.3, 4.6, 4.7 and the rigor in Section 5 — are not deliverable at that tier. Full empirical detail in `docs/data-provider-notes.md`; constraint summary in Section 12.1.

**Storage:** parquet files organized as `data/{provider}/{symbol}/{timeframe}.parquet`

**Schema:** unified across providers
- `timestamp` — pandas DatetimeIndex, ET-aware for stocks, UTC for crypto
- `open`, `high`, `low`, `close` — float64
- `volume` — float64 (crypto can be fractional)
- Optional: `vwap`, `trades`

### Backtest engine

- **Pure Python** for clarity and ease of debugging. Vectorize hot paths only if profiling demands it
- **No third-party backtesting library** — backtesting.py, vectorbt, zipline all have issues for our use case (limited multi-timeframe support, opinionated about data structures, abandoned, etc.). We'll write what we need
- **Event-driven simulation** — bars arrive in chronological order, strategy receives each bar, places orders, orders fill on next bar's price (no lookahead)

### Strategy framework

- **Subclass-based**, not config-based. Strategies are real Python code, easier to debug, more expressive
- **Metadata required** — every strategy must declare archetype, thesis, asset class compatibility, timeframe(s), parameter ranges
- **Decorator-based parameter declaration** for type safety and automatic CLI integration

### Evaluation harness

- **Train/test/holdout split** enforced at framework level. Strategy code cannot see holdout data
- **Walk-forward** with configurable window sizes
- **Robustness metrics** — not just profit factor on one symbol, but consistency across multiple symbols and time windows
- **Statistical tests** — bootstrap confidence intervals on profit factor, comparison against random entry baseline

### Database

- **SQLite** for the leaderboard. No need for Postgres complexity at this scale. Single file at `db/leaderboard.db`
- **Schema versioning** via lightweight Alembic-style migrations

### Generator pipeline

- **Claude API** via the Anthropic Python SDK, NOT Claude Code
- Reason: Claude Code is for interactive development; the generator needs programmatic, reproducible calls with logged prompts and responses
- Each generation logged with: prompt, raw response, parsed strategy spec, generation timestamp, model version
- **Strict spec format** — pydantic schema — so generated strategies are deterministic to translate into code

### Testing

- **pytest** for unit tests
- **Coverage requirement: 80%+ on engine and evaluation harness**, lower on data layer (mostly external API)
- **Regression tests** — known strategies (Casper port) produce known results

---

## 4. Strategy archetypes

The generator pipeline doesn't generate from a blank slate — it generates within defined archetypes. This forces diversity and grounds each strategy in some theoretical basis. Initial archetypes:

### 4.1 Mean reversion
- **Thesis:** Asset prices that move strongly in one direction over a short window tend to partially revert.
- **References:** Larry Connors' research, Jegadeesh (1990) "Evidence of predictable behavior of security returns"
- **Typical timeframes:** Daily to weekly
- **Example signals:** RSI(2) < 5 + price > 200-day MA → buy

### 4.2 Momentum / trend following
- **Thesis:** Assets that have outperformed continue to outperform over similar windows.
- **References:** Jegadeesh & Titman (1993), AQR's "Value and Momentum Everywhere"
- **Typical timeframes:** Monthly rebalancing on daily data
- **Example signals:** Top decile of 6-month returns, hold 1 month, rebalance

### 4.3 Volatility breakout
- **Thesis:** Periods of low volatility precede directional breakouts; ATR-scaled entries capture moves.
- **References:** Bollinger, Donchian channels, turtle traders
- **Typical timeframes:** Daily, sometimes intraday
- **Example signals:** Close above N-day high + ATR > recent average

### 4.4 Seasonality / calendar
- **Thesis:** Specific calendar effects (day-of-week, month-end, holidays) create exploitable patterns.
- **References:** "Sell in May," turn-of-month effect, end-of-quarter window dressing
- **Typical timeframes:** Daily, with date-based filtering
- **Example signals:** Long SPY last 5 trading days of month

### 4.5 Pairs / statistical arbitrage
- **Thesis:** Cointegrated pairs revert to their mean spread.
- **References:** Gatev, Goetzmann, Rouwenhorst (2006) "Pairs Trading"
- **Typical timeframes:** Daily
- **Example signals:** Long underperformer / short outperformer when spread > N standard deviations

### 4.6 Microstructure / intraday patterns
- **Thesis:** Institutional order flow creates predictable intraday patterns (open auction, close auction, lunch hour).
- **References:** Various market microstructure literature
- **Typical timeframes:** Intraday (5-min, 15-min)
- **Example signals:** Opening range breakout (Casper-style) is one example, with mostly negative empirical evidence

### 4.7 Overnight / session edge
- **Thesis:** Overnight returns differ systematically from intraday returns due to news, foreign markets, retail flow patterns.
- **References:** Lou, Polk, Skouras (2019) "A tug of war"
- **Typical timeframes:** Daily
- **Example signals:** Buy at close, sell at open

### 4.8 Crypto-specific archetypes (future)
- **Funding rate / basis trades** — Persistent positive funding rates on perpetual swaps indicate crowded long positioning that often mean-reverts. Requires perpetual futures data; not initially in scope.

The generator can be extended with new archetypes over time, but each must come with a written thesis and at least one academic or practitioner reference.

---

## 5. Evaluation rubric

How strategies are scored. This is the most important part of the system — get it wrong and we'll fool ourselves.

### 5.1 Data discipline

**Walk-forward IS the train/test mechanism** (revised v4). Rather than a single fixed train/test boundary, sliding (train_window, test_window) pairs are walked across the non-holdout span. Each step produces its own internal optimization-train and OOS-test partition. Aggregating OOS trades across all walk-forward steps gives the test signal.

- **Walk-forward span (stocks):** 2021-04-28 to 2024-12-31. Hard floor at 2021-04-28 because Polygon Stocks Starter has a 5-year rolling history cap (Section 12.3).
- **Walk-forward span (crypto):** to be defined per archetype based on Kraken CSV bulk-ingest depth.
- **Holdout period:** 2025-01-01 onward. Sacred — touched only at final scoring after walk-forward optimization is complete and parameters are frozen. Code-enforced via `evaluation.splits.optimization_mode()` (a thread-local context manager): inside `optimization_mode()`, calls to `holdout_load()` raise `HoldoutAccessError`.

The original v1 design (Train 2018-22 / Test 2023-24) is no longer applicable — the data tier we have access to does not include 2018. The walk-forward sliding window subsumes both the train and the test roles; the "test period" line is preserved here for orientation only.

### 5.2 Multi-symbol requirement

A strategy must be tested on at least **10 symbols** chosen by the framework (not the strategy author). For stocks, this is a seeded random subset of a hardcoded S&P 500 list (current membership, not point-in-time — see Section 12.4). For crypto, top-15 by market cap excluding stablecoins.

The strategy is graded on **median** profit factor across symbols, not the best. This prevents cherry-picking.

### 5.3 Walk-forward validation

Window sizes are configured in **months** (not years) to fit the constrained span: defaults are `train=24mo`, `test=6mo`, `step=6mo`, yielding 3 windows over 2021-04 → 2024-12. The minimum-3-windows requirement is met.

For strategies with parameters, each walk-forward step:

1. Grid-searches over the parameter space on the train window (objective: profit factor if num_trades ≥ 30 else `-inf`; ties broken by total PnL). The search runs inside `optimization_mode()` so any code path that mistakenly tries to read holdout raises immediately.
2. Applies the best parameters to the next test_window period (out of sample for that optimization).
3. Records OOS trades.

Steps 1-3 repeat as the window slides forward by `step_months`. Strategies that only work for one parameter set get penalized — the OOS aggregation captures parameter brittleness directly.

Strategies with no `parameter_grid` skip the optimization step but still run the same window-by-window OOS backtests with default parameters, so the downstream aggregation is uniform.

### 5.4 Statistical significance

For each strategy, compute:

- **Bootstrap confidence interval** on profit factor (5000 resamples). If the 5th percentile is below 1.0, edge is not statistically distinguishable from break-even.
- **Comparison against random entry baseline** with same trade frequency and exit rules. Strategy must beat the random baseline at p < 0.05.
- **Number of independent trades** — strategies with fewer than 100 trades across all symbols and walk-forward windows are flagged as under-sampled.

### 5.5 Robustness scoring

A strategy's score is a composite:

```
robustness_score = median_pf_across_symbols
                 × consistency_factor      # std dev of PF across symbols
                 × parameter_penalty       # 0.95^(num_parameters)
                 × significance_factor     # 1.0 if p<0.05, else 0.5
```

This is one possible formulation; we'll iterate. The point is that no single metric is enough, and over-parameterized or single-symbol strategies are penalized.

### 5.6 What "promising" means

A strategy is flagged as worth paper-trading if:

- Robustness score > some threshold (TBD, calibrated as we generate strategies)
- Median profit factor > 1.2 across the multi-symbol test
- Bootstrap 5th percentile of profit factor > 1.0
- **Total OOS trade count ≥ 30** across all symbols and walk-forward windows. Below 30 trades the score, CI, and median PF are dominated by sampling noise — a single lucky trade can satisfy every other condition (see `evaluation.scoring.MIN_TRADES_FOR_PROMISING`). This is a hard floor; §5.4 separately flags <100 trades as under-sampled.
- Consistent edge across walk-forward windows
- Drawdowns are tolerable (max DD < 25% of equity)

Even a "promising" strategy goes to paper trading, not live. **No strategy is ever auto-deployed.**

---

## 6. Project structure

```
/root/strategy-discovery/
├── DESIGN.md                 # This document
├── README.md                 # Setup and usage
├── pyproject.toml            # uv-managed dependencies
├── .env.example
├── .gitignore
├── .venv/                    # gitignored
├── data/                     # gitignored, parquet cache
│   ├── tradier/
│   ├── kraken/
│   └── ...
├── db/                       # gitignored, SQLite leaderboard
│   └── leaderboard.db
├── results/                  # gitignored, backtest outputs
│
├── src/
│   ├── data/
│   │   ├── __init__.py
│   │   ├── base.py           # DataProvider ABC
│   │   ├── tradier.py
│   │   ├── kraken.py
│   │   ├── kraken_csv.py     # CSV bulk ingest pipeline
│   │   ├── alpaca.py         # stub for fallback
│   │   ├── cache.py
│   │   └── resample.py
│   │
│   ├── engine/
│   │   ├── __init__.py
│   │   ├── backtester.py     # Core engine
│   │   ├── execution.py      # Order/fill simulation
│   │   ├── session.py        # Session calendars (RTH, 24/7)
│   │   └── portfolio.py
│   │
│   ├── strategy/
│   │   ├── __init__.py
│   │   ├── base.py           # Strategy ABC + metadata
│   │   ├── parameters.py     # Parameter declarations
│   │   └── context.py        # What strategies receive each bar
│   │
│   ├── evaluation/
│   │   ├── __init__.py
│   │   ├── splits.py         # Train/test/holdout enforcement
│   │   ├── walkforward.py
│   │   ├── metrics.py
│   │   ├── significance.py   # Bootstrap, baselines
│   │   └── scoring.py        # Robustness score
│   │
│   ├── generator/
│   │   ├── __init__.py
│   │   ├── archetypes.py     # Archetype definitions
│   │   ├── prompts.py        # Claude prompts per archetype
│   │   ├── claude_client.py
│   │   ├── spec.py           # Strategy spec schema
│   │   └── translator.py     # Spec → executable strategy
│   │
│   └── leaderboard/
│       ├── __init__.py
│       ├── db.py             # SQLite schema and ORM
│       ├── record.py         # Recording strategies + results
│       └── query.py          # CLI query tools
│
├── strategies/
│   ├── manual/               # Hand-written (e.g., Casper port)
│   └── generated/            # Output of generator pipeline
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── regression/           # Casper port produces known results
│
├── scripts/
│   ├── discover.py           # Run generator pipeline
│   ├── evaluate.py           # Evaluate a strategy
│   ├── backtest.py           # Manual backtest of one strategy
│   ├── leaderboard.py        # Query leaderboard
│   ├── verify_data.py        # Pre-Phase-1 data verification
│   └── archive_old_project.sh
│
└── docs/
    ├── archetype-references.md
    ├── adding-an-archetype.md
    └── adding-a-data-provider.md
```

---

## 7. Phase plan

### Phase 0.5 — Data verification (before Phase 1, ~30 min)

Before we commit to building Phase 1 on top of Tradier and Kraken, we verify both can deliver what we need.

Deliverables:
- A standalone script `scripts/verify_data.py` that uses minimal code (not the full framework yet) to:
  - Pull AMD 5-min bars from Tradier for 2023-01-01 through 2024-01-01
  - Confirm bar count is reasonable (~250 trading days × 78 bars = ~19,500)
  - Confirm timestamps are 5-min aligned and ET-aware
  - Pull BTC/USD 1-hour bars from Kraken for the most recent 720 hours
  - Confirm the CSV download pipeline works for one quarter of historical BTC/USD trades

**Done criteria:** Both data sources deliver clean data. If either fails, we revise the plan before Phase 1 starts.

### Phase 1 — Foundation (week 1-2)

**Goal:** Working data layer, working engine, regression test passes.

Deliverables:
- Project scaffold, uv setup, virtual environment
- Tradier data provider (production for historical pulls)
- Kraken data provider (REST + CSV bulk ingest)
- Multi-timeframe support in engine
- Session calendars for RTH stocks and 24/7 crypto
- Strategy base class with metadata requirements
- Casper strategy ported as regression test (must produce same results as old backtester within rounding tolerance)
- Tests passing: 50+ unit tests, regression test for Casper

**Done criteria:** Can run `python scripts/backtest.py --strategy casper --symbols AMD,NFLX,SPY,QQQ,NVDA --start 2023-01-01 --end 2026-01-01` and get the same results as the old project.

### Phase 2 — Evaluation rigor (week 2-3)

**Goal:** Honest evaluation harness in place.

Deliverables:
- Train/test/holdout split enforcement (code-level data isolation)
- Walk-forward analysis
- Bootstrap confidence intervals
- Random baseline comparison
- Robustness scoring
- Multi-symbol selection (random S&P 500 picks, top-N crypto)

**Done criteria:** Casper strategy scored through full evaluation pipeline, including walk-forward and significance testing. Result documented as baseline. Spoiler: it's expected to score poorly. That's the point — confirms the evaluation pipeline correctly identifies non-edges.

### Phase 3 — Generator pipeline (week 3-4)

**Goal:** Claude can generate strategies that run end-to-end.

Deliverables:
- Archetype definitions in code (initial: mean reversion, momentum, volatility breakout)
- Prompts for each archetype
- Anthropic API client with logging
- Strategy spec schema (pydantic)
- Spec-to-code translator
- Reproducibility metadata for every generation

**Done criteria:** `python scripts/discover.py --archetype mean_reversion` produces a complete strategy file in `strategies/generated/` that can be evaluated through the harness.

### Phase 4 — Leaderboard and operations (week 4-5)

**Goal:** Long-term tracking of strategies.

Deliverables:
- SQLite schema for strategies and results
- Recording every evaluated strategy with full metadata
- CLI for querying leaderboard
- Promotion workflow (mark a strategy for paper trading consideration)

**Done criteria:** After running discovery 10 times across different archetypes, leaderboard shows all 10 strategies with their scores, and the top 1-2 can be inspected in detail.

---

## 8. Risks and mitigations

### Risk 1: Overfitting at scale
Generating many strategies and picking the best inevitably finds strategies that look good by chance.
**Mitigation:** Holdout period that no strategy ever sees during optimization. Bootstrap confidence intervals. Robustness score penalizes single-symbol or single-window wins.

### Risk 2: User abandons discipline
If a strategy "looks really good," there's pressure to skip the holdout, deploy early, or override the framework.
**Mitigation:** Discipline is technical, not just procedural. Holdout data lives in a separate directory. Evaluation harness will refuse to run if asked to optimize on holdout data. See Section 11 for ongoing checkpoints.

### Risk 3: Data leakage
Future information sneaking into "past" data — survivorship bias in symbol lists, look-ahead in indicator calculations, wrong timezone handling.
**Mitigation:** Use point-in-time symbol membership where available. Audit indicator calculations for lookahead. Timezone handling tested explicitly.

### Risk 4: Generator produces nonsense
Claude generates strategies that don't make sense, don't compile, or violate the archetype.
**Mitigation:** Strict spec format with validation. Translator rejects invalid specs. Generated strategies must compile and pass smoke tests before being evaluated.

### Risk 5: Cost spiral
Subscriptions plus Claude API calls plus development time add up.
**Mitigation (v3):** Stack is ~$130/month — Polygon Stocks Starter $29 + Tradier data add-on ~$10 (kept for brokerage and recent cross-checks) + Claude API for generator pipeline capped at ~$100. Kraken data is free. The Claude cap is enforced at the application level by the generator pipeline, not just by Anthropic billing limits.

### Risk 6: Tradier data turns out to be insufficient — **CLOSED 2026-04-27**
We bet in v2 that Tradier production + market data add-on covered multi-year intraday needs. It did not: Phase 0.5 verification on 2026-04-27 showed `markets/timesales` is restricted to a rolling ~60-day window at this account tier (observed cutoff: 59 days; full request/response in `docs/data-provider-notes.md`). The mitigation worked exactly as designed — Phase 0.5 caught the gap before we built infrastructure on top of it.

**Resolution:** Switched primary stocks provider to Polygon Stocks Starter ($29/mo) in v3. Tradier remains on the stack for brokerage integration and short-window cross-checks (Section 3). The lesson — *empirically verify provider claims against the specific endpoints and date ranges your design actually requires, before committing the architecture* — is preserved in Section 12.1 and the provider notes doc.

### Risk 7: We discover the strategy is to not have a strategy
After all this work, the data may say "no archetype generates reliable edge for retail-accessible markets and instruments." That's a valid result.
**Mitigation:** Accept it. Don't manufacture a positive result.

---

## 9. Out of scope (do not build)

To prevent scope creep, these things are explicitly excluded from the project:

- Live trading execution
- Real-time data feeds (only historical for backtesting)
- Order management beyond simulated fills
- Portfolio-level position sizing across multiple strategies
- Risk management beyond per-strategy stops
- Web UI or dashboard (CLI only for now)
- Multi-user support
- Cloud deployment beyond the existing single VPS
- Alerting integrations (Discord, Telegram, etc.)
- Options strategies (spot/cash markets only)
- High-frequency strategies (sub-minute timeframes)
- Anything requiring exchange-specific market making rebates or co-location
- Interactive Brokers integration (Tradier is sufficient for backtesting; IBKR can come later if execution layer is ever built)

If any of these become genuinely needed, they get their own design document and explicit decision to expand scope.

---

## 10. Approval and next steps

This document represents a 4-5 week build commitment. Before any code is written:

- [ ] User reads and approves this design
- [ ] User confirms commitments from Section 1.3 (discipline, paper trading)
- [ ] User confirms Tradier production account has market data add-on active
- [ ] User confirms okay with Claude API spend cap (~$100/mo)
- [ ] Old `/root/trading-backtester/` archived to `/root/archive/trading-backtester-2026-04/`
- [ ] Phase 0.5 data verification passes

After approval AND Phase 0.5 passes, Claude Code begins Phase 1.

---

## 11. Discipline checkpoints

Because the user has acknowledged that staying disciplined is the hard part of this project, here are explicit checkpoints to revisit periodically. These are commitments to your future self.

### Checkpoint 1: After Phase 2 (evaluation harness done)

When Casper goes through the new evaluation pipeline and gets a low score, the temptation will be to "fix" the strategy. Don't. The point of this checkpoint is to confirm the framework correctly identifies non-edges. Casper scoring poorly is the validation we want.

### Checkpoint 2: First "promising" strategy from generator

When the first generated strategy crosses the "promising" threshold, the temptation will be to skip the holdout test and start paper trading immediately. Don't. Run it through the full harness including holdout. If it still passes, paper trade. If holdout kills it, accept the result.

### Checkpoint 3: 3 months of paper trading

When a paper-traded strategy shows good performance for 1-2 months, the temptation will be to deploy real money early. Don't. The minimum is 3 months. The reason: short-term variance can be massive. A losing strategy can show 2 winning months by luck.

### Checkpoint 4: First real-money deployment

When a strategy passes paper trading, the temptation will be to size up quickly. Start with the smallest position size that's economically meaningful (e.g., $500 per trade max). Run for at least 2 more months at that size. Only then consider scaling.

### Checkpoint 5: After a profitable run

When you make money on a real strategy, the temptation will be to add leverage, add more strategies running simultaneously, or revisit the framework's discipline assuming "I've figured this out." Don't. Markets regime-shift. The same discipline that found the edge is what protects against losing it.

---

## 12. Known constraints

Empirically observed limits of our chosen providers and tooling. Each entry should record what we *saw*, not what we *assume*. Update when we change tiers or learn more. Detailed request/response specifics live in `docs/data-provider-notes.md`.

### 12.1 Tradier intraday history is a rolling window, not multi-year

**Status:** confirmed 2026-04-27 during Phase 0.5 verification.

The production `markets/timesales` endpoint, with our current account tier (production token + market data add-on, ~$10/mo), restricts the `start` parameter to a rolling window. On 2026-04-27 the cutoff was `2026-02-27` — a 59-day window. Anything earlier returned `HTTP 400: Invalid parameter, start: must be on or after 2026-02-27 00:00:00.`

**What still works at this tier:**
- `markets/timesales` for dates inside the rolling window (verified with a 5-min AMD session on 2026-04-21).
- `markets/history` for daily bars, going back at least to 2023-01-03 (verified one full year, 250 trading days).

**What this invalidates:** The v2 design treated Tradier as the primary stocks provider for the framework. That is fine for daily-bar strategies, but the framework as designed in Section 4 (intraday archetypes 4.3, 4.6, 4.7) and the multi-year evaluation rigor in Section 5 cannot be served by Tradier intraday at this tier. This realizes the scenario described in Section 8, Risk 6 — Phase 0.5 caught it as designed.

**Open questions before next decision:**
- Is the window exactly 60 calendar days, or something like "current + previous calendar month"? Affects whether a hypothetical paid-data top-of-funnel is even safe.
- Does Tradier offer a higher tier with multi-year intraday, and at what price?
- Does the 1-minute interval have a different (likely shorter) window than 5-minute?

**Decision pending.** Three live options: (a) upgrade Tradier — verify the window actually changes before committing; (b) restore Polygon as primary intraday provider, keep Tradier for daily + recent intraday; (c) narrow scope to daily-bar stock strategies + crypto-only intraday. Choice will inform a v2.2 design revision.

### 12.2 Kraken REST OHLC returns only the most recent ~720 bars

**Status:** confirmed 2026-04-27.

The public `OHLC` endpoint returns at most the most recent ~720 bars per call regardless of how far back you query (observed: 721 1-hour BTCUSD bars). Multi-year crypto history requires the bulk CSV trade-history pipeline (per-pair quarterly files in Google Drive, multi-GB each, requires resampling). This was already understood in v2 design (Phase 1+ task) and is not a surprise — recorded here for completeness.

### 12.3 Polygon Stocks Starter caps history at exactly 5 years

**Status:** confirmed 2026-04-27 PM during Phase 2 build.

Direct `/v2/aggs/ticker/AMD/range/5/minute/2018-01-02/2018-01-15` returned `HTTP 403 NOT_AUTHORIZED: "Your plan doesn't include this data timeframe"`. Empirical floor on data-fetched-without-error is **2021-04-28** as of 2026-04-27, exactly 5 years before today. v3 of this design read Polygon docs as "5+ years"; the actual cap is exactly 5, on a rolling basis.

**What this invalidates:** the v3 / earlier reading of §5.1 ("Train 2018-22, Test 2023-24") cannot be served. Section 5 has been rewritten in v4 around walk-forward over the available 2021-04 → 2024-12 span. Holdout (2025+) remains intact and sealed.

**Lesson (carried forward from §12.1):** *empirically verify provider claims against the specific endpoints and date ranges your design actually requires, before committing the architecture* — a Phase-0.5 fetch at 2018-01 would have surfaced this in advance. Add to a future Phase 0.5 v2 checklist: a multi-year backfill probe, not just a single-year RTH test.

### 12.4 S&P 500 roster is current membership, not point-in-time

**Status:** acknowledged 2026-04-27 PM during Phase 2 build.

`evaluation.symbols.SP500_SUBSET` is a hand-curated list of 50 current S&P 500 names spanning sectors. It is NOT point-in-time membership. Backtests across this roster therefore exhibit survivorship bias — companies that were once members but have since been delisted or removed are not included. This is an explicit Phase-2 trade-off; point-in-time membership is on the Phase-5+ roadmap.

The seeded random_subset over this list is reproducible via `data/symbol_lists/sp500_phase2_seed<N>.json`.

---

## Appendix A: Existing assets to leverage

From `/root/trading-backtester`, port:

- Tradier data provider patterns (auth, pagination, caching)
- Casper strategy logic (as regression test reference)
- Parquet caching pattern
- Performance metrics calculations (most can be reused)

Do NOT port:

- Existing engine code (rewrite for multi-timeframe and asset-class awareness)
- Existing CLI structure (redesign for the larger scope)
- Casper-specific session handling (generalize to session calendar abstraction)
- Alpaca integration (drop to stub, not actively used)

