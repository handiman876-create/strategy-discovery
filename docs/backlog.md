# Backlog

Deferred work that's been observed but isn't actively prioritized. Each entry: what was seen, where, and the proposed direction.

## Seasonality archetype prompt produces theses > 400 chars

**Observed:** During the 2nd 6-archetype `--fast` sweep on 2026-04-29, the `seasonality` archetype was rejected by the spec validator on all 3 generation attempts because the model wrote a thesis longer than the 400-char cap. The 3rd sweep (post-centralization, same date) produced a valid 2-attempt seasonality run, suggesting the failure is intermittent rather than deterministic.

**Direction:** Either tighten the seasonality prompt to constrain thesis length, or raise the cap (e.g. to 600 chars) if the longer theses are genuinely more useful. Decide after one more data point.

## Phase 4.5: leaderboard follow-ups

A bundle of decisions deferred during the Phase 4 leaderboard integration (commits a–d on 2026-04-30) so the core write path could ship cleanly. None block Phase 4 closure; each is a small, independent commit when prioritized.

### Manual strategies are out of leaderboard scope

**Decision (2026-04-30):** `scripts/evaluate.py` does not record to the leaderboard. The leaderboard tracks generated strategies (those with a behavioral_hash from the dedup pipeline); manual strategies like `CasperStrategy` live outside that flow.

**Why:** (a) manual strategies are typically one-offs, not part of discovery sweeps; (b) `behavioral_hash` semantics for hand-written code need separate design thought — the hash is currently defined over the compiled spec/translator output, not arbitrary `Strategy` subclasses; (c) Casper's results are already documented in `docs/phase3-closure.md` and don't need a leaderboard row to be discoverable.

**Direction when prioritized:** Decide a hash policy for hand-written strategies (probably hash the source code of the class), add an `imported_from='manual'` marker to `record_generation`, and call it from `scripts/evaluate.py` after wiring `initialize_db()` + a connection at startup.

### Per-generation quirk attribution

**Current state:** Safety nets in `src/generator/spec_recovery.py:75` and `src/generator/translator.py:67,260` write to a single lifetime JSON file at `results/generation_quirks.json` (total + by_model + by_field + by_archetype aggregates). The file works — it's the existing mechanism for "is this safety net still earning its keep" per the `feedback_observability_in_validators` memory.

**Schema mismatch:** The leaderboard `generations` table has per-row columns `stringification_firings`, `kwarg_validator_firings`, and `unreachable_default_firings`. They were designed in for per-call attribution but no producer exists; commit (a)'s `to_generation_metadata` adapter defaults all three to 0.

**Why deferred:** The lifetime JSON file already serves the validator-maturity tracking role (memory: `feedback_safety_nets_have_lifecycles`). Per-generation attribution is "nice to have" for queries like "did this specific strategy hit the stringification quirk during generation," but it isn't required for the leaderboard's main use cases (filter, query, backfill, status promotion).

**Recommended implementation path** (Option II from the commit (a) diagnosis): thread a per-call `QuirkCounter` context object through the three safety-net call sites:

  * `recover_stringified_dsl_fields(..., counter=)`
  * `validate_for_translation(..., counter=)` (kwarg validator path)
  * `translate_to_file(..., counter=)` (unreachable-default path)

The counter accumulates firings during this generation and is read by `to_generation_metadata` before the leaderboard write. The existing file-write side effects stay — they're complementary.

**Natural integration point:** Step 9's `--timeframe` flag already plumbs per-generation context through the generator pipeline (for retry-on-mismatch). The `QuirkCounter` rides along on the same context object.

### Per-archetype timeframe compatibility

**Decision (2026-04-30):** `discover.py --timeframe X` validates that `X is in get_archetype(args.archetype).allowed_timeframes` before any API call. Unsatisfiable combos (e.g. `--archetype mean_reversion --timeframe 5m`) exit with code 2 and a stderr message listing the archetype's allowed timeframes.

**Why:** the spec validator (`src/generator/spec.py:35` `TIMEFRAMES`) allows the union of all timeframes any archetype uses, but each archetype further restricts via `src/generator/archetypes.py:allowed_timeframes`. The translator enforces the per-archetype restriction; without CLI validation, the user would burn 3 API calls on a guaranteed-to-fail retry loop where the model alternates between satisfying the timeframe constraint (rejected by translator) and satisfying the archetype constraint (rejected as a timeframe mismatch).

**Why CLI-layer (not in `generate_strategy` / `generate_and_translate`):** the library functions stay archetype-agnostic — they accept any `requested_timeframe` per their contract, and the retry loop handles model noncompliance. Putting the cross-validation in the CLI keeps the library functions composable (a future caller might intentionally test out-of-spec combos) and the entry-point sane-defaulted.

**Per-archetype allowed timeframes (current):**

| archetype | allowed timeframes |
|---|---|
| `mean_reversion` | `1h`, `1d` |
| `momentum` | `1d` |
| `volatility_breakout` | `1h`, `1d` |
| `seasonality` | `1d` |
| `pairs` | `1d` |
| `microstructure` | `5m`, `15m` |
| `overnight_session` | `1d` |

**To expand for a single archetype:** modify `allowed_timeframes` in the archetype definition in `src/generator/archetypes.py` (lines ~33–200). Likely also needs an archetype-prompt update so the model produces appropriate strategies at the new timeframe (intraday strategies need different indicators, lookbacks, and exit logic than daily ones).

## Consider expanding TIMEFRAMES to include 30m and 4h

**Current state:** `src/generator/spec.py:35` defines `TIMEFRAMES = ("5m", "15m", "1h", "1d")`. The `discover.py --timeframe` flag (added in step 9) restricts to these 4 values.

**Question for future review:** should `30m` and `4h` be added as supported timeframes?

**Scope of work if added:**

  * Verify `src/data/resample.py` handles 5m → 30m and 5m → 4h (probably already does, since it uses generic OHLC aggregation)
  * Verify `src/engine/session.py` / `should_reset_session_at_bar` treats 30m and 4h correctly (intraday for 30m, almost-certainly intraday for 4h since 6.5h RTH session > 4h means 1–2 bars per session)
  * Verify `src/evaluation/walkforward.py` window sizing makes sense at these timeframes
  * Update at least one archetype prompt that mentions specific timeframe ranges
  * Add to `TIMEFRAMES` literal in `spec.py`
  * Add to `discover.py --timeframe` choices

**Trigger for revisit:** if generator output suggests value in these timeframes (e.g., model wants to use `30m` for some archetype), or if a user wants to test specifically at `30m` / `4h`.

**Decision deferred to:** post-Phase 4, when leaderboard data shows whether 5m/15m/1h/1d coverage is sufficient or if there's a gap.

## Centralize the quirk-counter pattern

**Current state:** Four duplicated implementations of the read-bump-write JSON pattern at `results/generation_quirks.json`:

  * `src/generator/spec_recovery.py:_record_string_dsl_quirk` — stringification
  * `src/generator/translator.py:_record_kwargs_quirk` — kwarg validator
  * `src/generator/translator.py:_record_unreachable_quirk` — unreachable defaults
  * `src/generator/pipeline.py:_record_timeframe_mismatch_quirk` — timeframe noncompliance (added step 9)

Each helper independently re-implements: read JSON, set/get a top-level dict, bump `total`, bump per-dimension subkeys, update `last_seen`, write back. Each also independently defines `_QUIRKS_PATH` and the same defensive try/except.

**Recommended shape:** `src/generator/quirk_counters.py` exposing a single

```python
def record_quirk(name: str, **breakdowns: str) -> None:
    """Bump the counter for `name` by 1; bump each `key`-by-`value`
    dimension under it; refresh last_seen. Idempotent on shape — first
    call for a name initializes the row."""
```

**Migration:** all 4 call sites in one focused commit; remove the per-counter helpers + their `_QUIRKS_PATH` constants after. The four sites pass slightly different breakdowns today (`by_field`, `by_model`, `by_archetype`, `by_requested_timeframe`, etc.) — `**breakdowns` accommodates that without forcing a uniform schema.

**Why deferred:** bundling the refactor into step 9 would have hidden it inside a feature commit. Centralizations deserve their own focused diffs (per `feedback_centralize_dispatched_logic` memory).

## Pre-push housekeeping

### Rewrite git author email across all commits

**Current state:** every commit in `/root/strategy-discovery` and the memory repo at `/root/.claude/projects/-root/memory` bears author email `handiman876@gmail.com`. Going forward (post-Phase 4 step 9), new commits use the GitHub noreply alias `236492174+handiman876-create@users.noreply.github.com` via local git config.

**Trigger:** before any push to a public host (GitHub or similar).

**Procedure:**

  * `git filter-repo --email-callback 'return new_email if old_email == b"handiman876@gmail.com" else old_email'`
  * Or use `git rebase --root --exec 'git commit --amend --reset-author --no-edit'` if `filter-repo` isn't installed
  * Apply to **both** repos (`strategy-discovery` and the memory repo)
  * Verify: `git log --format='%ae' | sort -u` shows only the noreply alias

**Why deferred:** rewriting now adds risk without current benefit (no exposure on private VPS). The right time is the moment before publishing, when the rewrite strategy can be comprehensive across all commits, not just in-session ones.

## Phase 5: dedup hash review

### Fixture-induced behavioral hash collapse — PARTIALLY RESOLVED in step 10

**Original finding (Phase 4 step 10 audit):** `behavioral_hash` (Phase 2/3) collapsed many specs to identical hashes because the dedup fixture (AMD 2024-Q3) produced zero trades for most generated strategies — 127 distinct mean_reversion specs hashed to 1, 6 momentum to 1, 5 overnight_session to 1. The hash effectively encoded "did this produce zero trades on the fixture" rather than "is this behaviorally distinct."

**Resolved (Phase 4 step 10, commits 1–4, replacing `behavioral_hash` with `compute_strategy_hash`):** Structural hashing over a canonicalized spec representation now differentiates specs by content rather than by trade fingerprint. Cross-archetype collapse is gone. Smoke test on the same repo state (post-migration):

| archetype | distinct hashes (behavioral) | distinct hashes (structural) |
|---|---|---|
| mean_reversion | 1 | 10 |
| microstructure | 5 | 6 |
| momentum | 1 | 6 |
| overnight_session | 1 | 5 |
| seasonality | 1 | 5 |
| volatility_breakout | 1 | 4 |
| **total** | **5** | **36** |

A single eval-class label (e.g. `mock_strat_1d` × 174 generations) still collapses to one strategy row — that's the desired identity behavior for re-runs of the same fixture, not a bug.

### Residual concerns (NOT resolved — Phase 5 work)

Structural hashing has its own properties worth tracking:

  * **Alias-name sensitivity.** Two specs with identical logic but different indicator alias names (`rsi_short` vs `rsi_2`) hash differently. Documented as a deliberate choice in `src/generator/dedup.py` — alias normalization (renaming aliases to a canonical form derived from `(type, params)` and rewriting all DSL refs) is more complex and was deferred. Revisit if observability shows alias variation creates significant noise.
  * **No operator normalization.** `a > b` and `b < a` hash differently. Same for `a == b` vs `b == a`. Could add semantic operator normalization, but the trade-off is more complex canonicalization for marginal collapse benefit.
  * **No behavioral equivalence.** Two structurally different specs that happen to produce identical trade lists are now distinct in the leaderboard. With behavioral hashing they would have collapsed; with structural hashing they don't. This may be the correct answer (different specs are different strategies) or a regression in equivalence-detection — it depends on the use case. Worth observing in practice before deciding whether to add a complementary trade-fingerprint hash as a secondary dedup signal.

**Trigger for action:** if alias-variation noise or operator-asymmetry collisions become observable in leaderboard queries, prioritize a Phase 5 follow-up. Otherwise this section can be retired after a stretch of clean operation under the new hash.

## Random baseline is intraday-specific — degenerate on daily timeframes

**Observed (2026-07-06):** During the SPY canonical eval of the hand-ported
`Rsi2MeanReversion` (a 1d strategy), `random_baseline` returned PF=0.0 for all
200 trials (`baseline_pf` column uniformly 0.0), giving `baseline_p_value=0.0`
and `median_baseline_pf=0.0`. The p-value technically reads "beats 100% of
random trials," but it is **uninformative** — the baseline never generated a
profitable random trade because it can't generate a valid trade at all on daily
bars.

**Root cause:** `src/evaluation/significance.py` (`random_baseline` /
`_simulate_random_trade`, see module docstring lines ~16–17) is hard-wired to
Casper's *intraday* mechanics: it places random entries with an
"opposite-bracket stop on that session's opening range (first 5-min bar's
high/low), target at risk × rr_ratio." On a daily strategy there is no
intraday opening range / first-5-min bar, so the OR-derived stop is degenerate
(risk ≤ 0 → no valid bracket → `profit_factor([]) == 0.0`), for every trial.

**Impact:** the "vs random baseline" significance leg is meaningless for any
non-intraday strategy. The bootstrap CI is unaffected and remains the trustworthy
significance signal (RSI-2 SPY: PF 4.45, CI lower bound 2.23 > 1). But the
significance_factor / aggregate_p_value derived from the baseline should not be
trusted for daily strategies, and any future daily strategy will hit this.

**Direction when prioritized:** make the random baseline timeframe-aware. Options:
  * A daily-appropriate random-entry model: random entry on a bar, exit after a
    holding period drawn from the strategy's realized hold distribution (or a
    fixed N-bar hold), with an ATR- or percent-based stop rather than an
    opening-range bracket.
  * Or dispatch on `backtest_config.is_intraday`: keep the OR-bracket baseline
    for intraday strategies, use the holding-period model for daily-or-coarser.
  * Guard: if the baseline produces zero valid trades across all trials, surface
    a warning and drop the significance leg rather than reporting a spurious
    p=0.000 (loud-fail-on-degenerate, per the observability convention).

## Fast-screen trade floor fires to a log line — no persistent counter yet

**Added (2026-07-06):** `run_fast_evaluation` now floors an under-sampled fast
eval's robustness score to 0.0 when total OOS trades < `FAST_MIN_TRADES`
(= `MIN_TRADES_FOR_PROMISING`, 30). This stops 1-3 trade artifacts (capped
PF=100 → score ~100) from topping a score ranking. See
`src/evaluation/fast_pipeline.py` and `tests/unit/test_fast_trade_floor.py`.

**Gap vs. the observability convention:** every other safety net in this project
carries a *persistent firing counter* (e.g. `stringification_firings`,
`kwarg_validator_firings`, `unreachable_default_firings` on the `generations`
table) so we can tell whether it is still earning its keep and drive its
detection → maturity → decision lifecycle. The trade floor currently only emits
a WARNING log when it fires — greppable, but not queryable and not durable.

**Direction when prioritized:** add a persistent counter for floor firings.
Likely a `fast_trade_floor_firings` column on the `evaluations` table (small
schema migration + wire-through in the leaderboard record path), mirroring the
generation-side counters. Then the floor can be reviewed like the other
counter-bearing validators: if it rarely fires once the fast screen stops
surfacing degenerate specs, decide whether to keep, retune the threshold, or
retire it. Deferred deliberately on 2026-07-06 to avoid a schema migration
mid-session; the log line is the interim signal.

## percent_rank scale confusion — model emits unsatisfiable thresholds

**Observed (2026-07-06, autodiscover run 1):** roughly 1/3 of an 18-candidate
batch produced non-firing or near-non-firing strategies. Two tripped explicit
unreachable-default warnings — `percent_rank(prank_126) > 75.0` (momentum) and
`percent_rank(prank) > 65.0` (microstructure) — and several more produced 0
trades from the same root cause, wasting the generation + fast eval.

**Root cause:** `percent_rank` outputs a fraction in **[0, 1]**
(`INDICATOR_RANGES`, `src/generator/indicators.py`), but the model treats it like
a 0-100 oscillator (RSI-style) and emits thresholds such as 65/75. Any
`percent_rank(x) > 65` is always false → the entry never fires → dead strategy.
The `_system.md` indicator catalog lists `percent_rank(period=252)` with **no
range annotation**, so archetypes whose own prompt lacks a correct [0,1] example
(momentum, microstructure) get it wrong; `volatility_breakout.md`, which shows
`> 0.95`, gets it right.

**Existing safety net is warn-only:** `scan_unreachable_defaults`
(`src/generator/translator.py`) already DETECTS these clauses and records a quirk
counter, but only logs a WARNING — it does not reject or repair, so the dead spec
is still translated and evaluated.

**Direction — two complementary layers:**
  * **Prompt (prevention):** annotate `percent_rank`'s [0,1] range in the
    `_system.md` catalog so every archetype learns it at the source.
  * **Validator (enforcement):** call `scan_unreachable_defaults` inside
    `validate_for_translation` so an unsatisfiable clause raises
    `TranslationError`, feeding the existing `generate_strategy` retry loop
    (`retry_feedback`) so the model self-corrects instead of emitting a dead
    spec. Weigh AND/OR context so a benign unreachable OR-branch doesn't
    over-reject an otherwise-valid spec.

## Generation-time signal-frequency floor — reject empirically-zero-trade specs

**Observed (2026-07-06/07, autodiscover runs 1 & 2):** ~23.5% (4/17 both runs)
of generated specs produce **empirically zero OOS trades**. After the percent_rank
fix (commit 8a7256c) removed the statically-unsatisfiable cases, this became the
dominant yield leak — each dead spec still costs a full 5-symbol fast eval and
leaves a dead leaderboard row. Examples: run2 CAND 6/11/16/17 (0 trades, no
warnings — the static unreachable-detector cannot see them).

**Root cause:** the entry condition is *empirically* almost-never satisfied,
though not mathematically unsatisfiable. Two sub-causes, both already surfaced by
`diagnose_signal_frequency` (`src/evaluation/diagnostics.py`):
  * `warm_ratio ≈ 0` — timeframe/session mismatch (e.g. a 200-period indicator on
    intraday bars never warms within a session), so indicators are never ready.
  * `ratio_full_to_min_clause ≈ 0` — an over-restrictive AND whose clauses rarely
    co-occur, so the full entry (`full_hits`) fires ~0 times.
These pass `validate_for_translation` (spec-only, static) and fast eval, wasting
compute.

**Proposed fix — wire `diagnose_signal_frequency` into the generation gate:**
`diagnose_signal_frequency(strategy_class, symbol)` already returns per-side
`full_hits`, `n_evaluable_bars`, `warm_ratio`, `ratio_full_to_min_clause` and is
cheap (single-symbol indicator walk, no backtest/bootstrap). Currently it only
runs *post-hoc* in `fast_pipeline._run_signal_frequency_diag` when
`n_oos_trades_total < DIAGNOSE_BELOW_TRADES` (10). Instead, run it as a **gate in
`generate_and_translate`** (after `translate_to_file` + class import, before
returning): probe ONE symbol (e.g. SPY, or the first canonical symbol) and if the
declared long/short side's `full_hits` is below a small floor (e.g. `< 3`, tune
at impl) OR `warm_ratio == 0`, raise so the existing `generate_strategy` retry
loop feeds a specific reason back as `retry_feedback` ("entry fires 0× on SPY
over <window>: clauses rarely co-occur / indicators never warm — loosen the
entry"). The model self-corrects instead of shipping a dead spec.

NOTE: unlike the percent_rank check, this needs the translated CLASS + data, so
it lives in `pipeline.py` (post-translation), NOT in `validate_for_translation`
(pre-translation, spec-only).

**Expected yield improvement:** converts the ~1/4-of-batch zero-trade specs into
either a self-corrected firing spec (via retry) or an honest gen-fail — either
way no wasted fast eval and no dead leaderboard row. Should push the
zero-trade-reaching-fast-eval rate from ~23% toward ~0 and modestly increase the
count of candidates clearing the fast gate (trades>30 & score>1.5). Does NOT
change the base rate of edges surviving canonical — it removes dead weight, not a
magic source of alpha.

**Files to change:**
  * `src/generator/pipeline.py` — add the probe+gate in `generate_and_translate`
    (raise a retry-triggering error on failure; thread the reason into
    `retry_feedback`). This is the primary change.
  * `src/evaluation/diagnostics.py` — reuse `diagnose_signal_frequency` as-is, or
    add a thin `entry_fires_enough(cls, symbol) -> (bool, reason)` wrapper.
  * Config constant for the `full_hits` floor + probe symbol (near the other
    generation constants).
  * Observability: a firing counter (mirror `unreachable_default_firings` etc. on
    the `generations` table) so we can track how often the floor fires — per the
    "every safety net is observable" norm.
  * Tests: `tests/unit/` — a spec whose entry never fires triggers the gate/retry;
    a normally-firing spec passes untouched.
