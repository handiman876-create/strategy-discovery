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
