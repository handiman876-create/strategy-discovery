# Discovery health check

How to read the output of a nightly `autodiscover` run or a `recover_stranded_generations` replay without drawing the wrong conclusion. Each entry: the trap, why it bites, and what to do instead.

## Reading the summary JSON: candidate metrics are `fast_`-prefixed

Each entry in `candidates` in `logs/autodiscover_summary_*.json` uses `fast_score`, `fast_trades`, `fast_pf`, `fast_ci_lower` — **not** `score` / `trades` / `pf` / `ci_lower`. Top-level keys (`usable_candidates`, `elapsed_minutes`, `spent_usd`, `hits`, `warnings`) are unprefixed.

Reading the unprefixed name inside `candidates` returns the `.get()` default, so a filter like `c.get('ci_lower', 0) > 0` empties the list and the script exits 0 printing nothing — indistinguishable from "N candidates, none scored". Prefer `is not None` over `> 0` when filtering scores: a `ci_lower` of 0.0 or negative is a real measurement, not a missing one.

`hits` is the promotion list (`ci_lower > 1.0`) and is normally empty. `usable_candidates: 20` does **not** mean 20 promotable strategies.

## Checking recovery results: filter by date window, not `imported_from`

For runs **before 2026-08-22**, filter recovered evals by the `evaluated_at` window, not by `imported_from`.

The recovery script tagged the generation row (`strategies.imported_from LIKE 'recovered:%'`, 73 rows) but the eval row was written on a separate path that never forwarded the marker, leaving `evaluations.imported_from` NULL — identical to a nightly eval. A query filtering *evals* on `imported_from LIKE 'recovered:%'` returns zero rows and exits 0, which reads as "nothing was recovered" rather than "wrong table".

Fixed in `fe92637` (threads `imported_from` through `run_fast_evaluation` → `record_evaluation_to_leaderboard` → `record_evaluation`). The fix is **not retroactive**: the 18 evals from the 2026-08-22 08:30 run keep NULL. So:

- evals from runs **after** `fe92637` — filter on `e.imported_from LIKE 'recovered:%'`
- evals from runs **before** it — filter on the `evaluated_at` window, or join to `strategies` and filter on `s.imported_from`
- the date-window approach breaks as soon as a replay overlaps a nightly run; prefer the tag once it is available

## Multiple comparisons: a family of near-identical specs is ONE finding

A family of 18 similar specs showing max `ci_lower = 0.995` is **one finding sampled 18 times, not 18 near-misses.**

**Observed 2026-08-22:** the 5m recovery batch screened 18 specs, every one a close/closing-auction BB-reversion scalp — the same edge re-parameterized. Their `ci_lower` clustered at 0.77–0.995. The 0.995 top is the *maximum of 18 correlated draws*, which is roughly where you'd expect the max to land even if the family's true `ci_lower` sits below the gate. Ranked naively it reads as "one spec agonizingly close"; it is really "we sampled one edge 18 times and took the best."

Before reacting to a near-miss, check archetype diversity in the batch. If the top N rows share an archetype and timeframe, collapse them to one finding and treat the max as optimistically biased. This is the multiple-comparisons sibling of the high-PF / low-CI structural trap: promote on `ci_lower`, and discount the max of a correlated family further still.

Corollary: `score` and `ci_lower` can disagree on the winner. In the same batch the only `score > 1.0` row (1.024) ranked *third* by `ci_lower`. `ci_lower` is the gate.
