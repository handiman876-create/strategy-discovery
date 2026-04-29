# Phase 3 closure

**Status:** Closed 2026-04-29.

## Deliverable

A generator pipeline that produces strategy specs via Claude API, translates them into executable strategies, and runs them through the Phase 2 evaluation harness end-to-end.

## Achieved

- **Diversity across runs.** Six archetypes — `mean_reversion`, `momentum`, `volatility_breakout`, `seasonality`, `microstructure`, `overnight_session` — generate distinct strategies. `pairs` is rejected by the translator and deferred to Phase 3.5.
- **End-to-end pipeline runs without structural failure.** Four classes of mid-Phase-3 structural failure surfaced and were resolved:
  - Zero-trade bug from session-reset dispatched on the wrong timeframe — Fix #1 (`523efb3`), Fix #5 (`48cc030`), centralized in `ca96b81` with an AST-level contract test that fails CI on bypass.
  - Translator `UnboundLocalError` from indicator alias colliding with the imported function name — fixed via `_val` suffix in `4bc3511`.
  - Spec corruption from Sonnet 4.6 stringifying nested DSL fields — hot-fix `f84fbdf`, regression test `9b2bfc7`, centralized helper `ae453b2` (`generator/spec_recovery.recover_stringified_dsl_fields`).
  - Eight production files in `src/data/` silently untracked because of an unanchored gitignore rule — `d480202`.
- **6/6 strategies correctly classified `is_promising=False` in the most recent sweep** (2026-04-29 21:05–21:10 UTC). Two new verdict gates verified:
  - **Minimum trade count (`n_oos_trades_total >= 30`)** — `894c170`. Catches the n=1 / PF=100 false-positive class. Fired in the wild on `VolatilityBreakoutEmaRsiAtr` (n=0) and `OvernightPercentRankGap` (n=15).
  - **Unreachable-default detector at translation time** — `7563b44`. Warn-only static analysis. Caught a real bug on first sweep: the model wrote `percent_rank > 55.0` on `OvernightPercentRankGap.exit_long`, treating `percent_rank` as 0–100 rather than its actual range of 0–1.
- **Four bug classes structurally prevented from recurrence**, each with a centralized helper, a contract test, and a counter in `results/generation_quirks.json`. The counters' lifecycle policy is documented in user memory under `feedback_safety_nets_have_lifecycles.md`: removal of any safety net requires an explicit structural-prevention argument, not just a low counter.
- **214 tests passing**, 86%+ coverage on `engine/` and `strategy/`.

## Open items

Deferred to Phase 4 or the backlog:

- **Seasonality thesis-length issue** — intermittent specs with `thesis > 400` chars rejected by the validator. Tracked in [`docs/backlog.md`](backlog.md) (`93498a8`).
- **Multi-timeframe strategy support** — Phase 3 spec validator rejects multi-timeframe specs (`0b2897f`); the engine architecture for one strategy subscribing to multiple bar streams is deferred to Phase 4.
- **Survivorship bias in the S&P 500 symbol roster** — known limitation, documented in `DESIGN.md`.
- **Position sizing rules beyond `fixed`** — `fixed_dollar`, `atr_scaled`, `vol_scaled` are listed in `PLANNED_SIZING_RULES` in `generator/spec.py` and slated for Phase 3.5.

## Phase 4 outlook

Phase 4 starts in a separate session. Realistic scope:

- **Leaderboard infrastructure.** Persistent ranking of generated strategies across *canonical* evaluations (10-symbol, full bootstrap), distinct from the per-run `--fast` eval used during discovery.
- **Longer-running discovery.** Many more strategies generated and evaluated over time, beyond the 1-strategy-per-archetype-per-run cadence Phase 3 settled on.
- **Strategy lifecycle tracking.** Discovery → fast eval → canonical eval → leaderboard → paper trade → retire.

Phase 3's gates and safety nets are the foundation. Any strategy that surfaces in Phase 4 has already been screened against the four documented failure modes, with counters in `generation_quirks.json` providing the regression signal.
