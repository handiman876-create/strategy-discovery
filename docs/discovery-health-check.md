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

## Seasonality prompt fix: working since 2026-09-04 (7 clean runs)

2026-09-11: `usable_candidates=20`, `hits=0`, `spent=$0.4881`. Both seasonality candidates generated at `tf=['1d']` with thesis under 400 chars:

- `atr_zscore_reversion_seasonality` — score=0.907, median_pf=1.342, ci_lower=0.815, n=573
- `macd_percentrank_reversion_seasonality` — score=0.314, median_pf=0.886, ci_lower=0.194, n=126

`ed38090` landed **2026-09-04 21:12 UTC**; the last seasonality GEN-FAIL was the 2026-09-04 07:0x run, i.e. pre-fix. Runs 09-05 through 09-11 have each generated both seasonality slots cleanly — today is the **7th consecutive clean run, not the first confirmation.**

### 0.907 is NOT the highest-ever score for the family

It ranks **3rd** of 80 seasonality evals with full metrics in `db/leaderboard.db`:

| name | score | median_pf | ci_lower | n | date |
|---|---|---|---|---|---|
| `morning_open_momentum_seasonality` | 1.175 | 1.729 | 0.589 | 82 | 2026-07-16 |
| `afternoon_momentum_seasonality` | 0.932 | 1.604 | 0.602 | 165 | 2026-07-18 |
| `atr_zscore_reversion_seasonality` | 0.907 | 1.342 | 0.815 | 573 | 2026-09-11 |

The July records predate the dated summaries (which start 2026-08-05) and exist only in the DB. **Rank a family from `leaderboard.db`, never from `logs/autodiscover_summary_*.json`** — the summary era is shorter than the eval history and makes stale figures look like records.

What *is* a family record: **ci_lower 0.815**, beating 0.685 (`midday_ema_momentum_seasonality`, 08-15). That is the promotion metric, so it is the record worth having.

### Gate status: score is the gate furthest out, not ci_lower

`median_pf` and the trade floor both **pass**. Two gates fail (`src/evaluation/scoring.py:91-93`):

| gate | required | actual | deficit |
|---|---|---|---|
| `score` | > 1.5 | 0.907 | **0.593** — furthest out |
| `ci_lower` | > 1.0 | 0.815 | 0.185 — nearest |

So "ci_lower is still the binding gate" is only half true for this spec: it is the *nearest* gate, while `score` is the one holding it back hardest. Consistent with backlog `32f285e`.

### Not a sample-size problem

`midday_ema_momentum_seasonality` (08-15) reached ci_lower 0.685 at n=572; today's spec reaches 0.815 at n=573. **Matched sample size, +0.13** — so the improvement is real and not an n artifact. But 19 seasonality specs have now cleared `median_pf>1.2` with n>=50, and 0.815 is the all-time maximum ci_lower across every one of them. Same high-PF / `ci_lower<1.0` signature as the last-hour/power-hour graveyard: treat as guilty until proven.

### Still open — NOT fixed by ed38090

The thesis<400 budget was added to the **seasonality prompt only**. On 2026-09-11 two generation attempts still died on `thesis String should have at most 400 characters`:

- `microstructure` 07:03:41
- `overnight_session` 07:41:55

Retries covered both (`usable=20/20`), so the cost was API spend, not lost slots. **Generalize the thesis budget to every archetype prompt.**
