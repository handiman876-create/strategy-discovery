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
