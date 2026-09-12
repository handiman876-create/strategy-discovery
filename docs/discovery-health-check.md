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

## `ci_lower` is not comparable across baskets

**Don't compare `tech5_v1` and `diverse8_v1` `ci_lower` values directly.** Grouped by `basket_version` over fast evals with `n_oos_trades >= 50` (verified 2026-09-12):

| basket | symbols | n≥50 fast evals | max `ci_lower` | count ≥ 1.0 |
|---|---|---|---|---|
| `tech5_v1` | 5 | 44 | **1.145** | **3** |
| `diverse8_v1` | 8 | 867 | **0.995** | **0** |

Every fast-gate clearance in the project's history is a July `tech5_v1` eval — `rsi_ema_reversion_1d` (1.145), `overnight_rsi_trend_filter` (1.063), `daily_macd_hist_roc_momentum` (1.042). The current 8-symbol basket has **never cleared 1.0 in 867 evals**; its all-time ceiling is 0.995 (`close_auction_rsi_bb_reversion_scalp`, 08-22 — and see the multiple-comparisons section above, that 0.995 is a max-of-sweep).

The narrower basket wins by construction: `tech5_v1` was 3/5 high-beta tech and **over-promoted beta as signal** (`src/evaluation/baskets.py:11`). A `tech5_v1` 1.145 and a `diverse8_v1` 0.995 are not 0.15 apart on a common scale — they are two different measurements. **"All-time best `ci_lower`" is two questions, not one.** Always group by `basket_version` before claiming a record.

### DO NOT lower the 1.0 threshold to "restore" the old pass rate

The 0-for-867 above is the single most misreadable number in this doc. It looks like evidence the gate is too hard. It is not — **the `tech5_v1` clearances were the false positives, and screening them out is why the basket was switched on 2026-07-17.** From `baskets.py:11-22`:

```
                          d0cc300e5c07      fdc88ceb54fd
    tech5_v1 (fast)         ci 1.054  ->      ci 1.145      both promoted
    diverse8_v1 (fast)      ci 0.218  ->      ci 0.634      both screened out
    sp500_phase2_seed42     ci 0.288  ->      ci 0.963      both FAILED canonical
```

`diverse8_v1` reproduces the canonical verdict; `tech5_v1` contradicted it twice and burned canonical compute both times. `diverse8_v1` is a strict subset of the canonical roster, which is why it *predicts* rather than merely differs.

**Measured size of the correction.** `reeval_basket.py` re-ran 11 strategies on both baskets on 2026-07-17. 10 of 11 dropped; mean delta **−0.388**:

| name | tech5_v1 | diverse8_v1 | Δ |
|---|---|---|---|
| `daily_sma_cross_roc_momentum` | 0.833 | 0.148 | −0.685 |
| `daily_macd_hist_roc_momentum` | 1.042 | 0.463 | −0.579 |
| `rsi_ema_reversion_1d` | 1.145 | 0.634 | −0.511 |
| `rsi_macd_reversion_1d` | 0.880 | 0.434 | −0.446 |
| `rsi_sma_reversion_1d` | 0.937 | 0.527 | −0.410 |
| `rsi_sma_cross_reversion_1d` | 0.920 | 0.524 | −0.396 |
| `daily_macd_hist_roc126_momentum` | 0.965 | 0.569 | −0.396 |
| `overnight_rsi_trend_filter` | 1.063 | 0.746 | −0.317 |
| `roc_ema_rsi_reversion_1d` | 0.825 | 0.549 | −0.276 |
| `roc_sma_reversion_1d` | 0.899 | 0.632 | −0.267 |
| `late_morning_rsi_reversion_scalp` | 0.806 | 0.823 | **+0.017** |

`n_oos_trades` went *up* on `diverse8_v1` (more symbols) while `ci_lower` went *down*, so this is cross-sectional dispersion, not sampling. Per `baskets.py:42`, PG and QCOM are the names doing the work: both failed canonical at PF 0.54/0.56 on the spec `tech5_v1` rated 1.145.

Shifting the threshold by −0.388 to recover the old pass rate would recover the old false-positive rate exactly. Read 0-for-867 alongside **zero strategies having ever passed canonical** (11 canonical evals, 2 `promising`, both superseded partials): the consistent reading is that the generator has not yet produced a real edge. The gate is doing its job.

Note the one riser: `late_morning_rsi_reversion_scalp`, a 5m scalp at n=3322. High-n intraday scalps appear basket-insensitive while every 1d strategy collapsed — worth keeping in view, but it is one case. See the next section before acting on it.

### Do NOT turn the riser into a generator target: `ci_lower` rises with n

**Candidate direction considered 2026-09-12 and NOT adopted:** "target high-N intraday 5m scalps with cross-symbol consistency — `late_morning_rsi_reversion_scalp` was the only strategy to rise on `diverse8_v1` (0.806 → 0.823, n=3322); hypothesis: intraday mechanics are more basket-insensitive than 1d signals."

The paired delta is real (same `strategy_hash` `0746c98b`, both baskets). Two problems make it unsafe to steer the generator on.

**1. The evidence is n=1 and the cross-sectional support is confounded by sample size.** Only 1 of the 11 paired specs is 5m, so basket sensitivity by timeframe is *untested*, not established, and +0.017 is well inside noise. The tempting confirmation — 5m specs average a much higher `ci_lower` on `diverse8_v1` than 1d specs — is an n effect. `ci_lower` rises monotonically with trade count, and the gradient holds **within 5m alone**, so it is not about the timeframe:

| `n_oos_trades` | all specs, avg `ci_lower` | 5m only, avg `ci_lower` |
|---|---|---|
| 50–200 | 0.287 (n=551) | 0.403 (n=3) |
| 200–500 | 0.429 (n=158) | — |
| 500–1,500 | 0.652 (n=60) | 0.606 (n=16) |
| 1,500–3,000 | 0.789 (n=30) | 0.789 (n=30) |
| 3,000+ | 0.744 (n=68) | 0.744 (n=68) |

5m specs average **n=4,242**; 1d specs average **n=192**. That alone accounts for the 5m/1d split (avg `ci_lower` 0.728 vs 0.339).

**2. The 5m population has worse profitability, not better.** Average `median_pf` on `diverse8_v1` is **0.949 for 5m** — below break-even — vs **1.096 for 1d**. In the 3,000+ trade bucket average PF is **0.912**. So high-n scalps earn their `ci_lower` by having a narrow interval around a *worse* point estimate, not a stronger edge.

`ci_lower` ≈ point estimate − interval half-width, and n shrinks the half-width. **"Maximize `ci_lower` by maximizing n" is therefore a way to game the gate rather than pass it**, and it aims the generator squarely at the close-auction / power-hour 5m family documented above: 14 variants, every one below 1.0, and 0-for-2 at canonical (`last_hour_momentum_seasonality`, `power_hour_momentum_seasonality`, both `promising=0`, 2026-07-06). A same-named sibling of the riser itself, `06e3b21cb58b` (08-09), came in at `ci_lower` 0.767 with **PF 0.959**.

**What would actually test the hypothesis:** use `reeval_basket.py` to re-run a batch of existing 5m specs on `tech5_v1` and build a paired 5m delta distribution to compare against the 1d deltas above. That is fast-tier compute only, and it turns an n=1 anecdote into a real measurement. Until then, any timeframe preference should be matched on n — compare 5m and 1d specs inside the same trade-count bucket, never across the pooled populations.

### Corollary: a fast clearance does not survive canonical either

Only two specs have ever had both a fast and a canonical `ci_lower` recorded, and both fell hard:

| name | fast `ci_lower` | canonical `ci_lower` | promising |
|---|---|---|---|
| `rsi_ema_reversion_1d` | 1.145 | **0.967** | 0 |
| `daily_price_sma_zscore_momentum` | 1.054 | **0.314** | 0 |

Both of those fast numbers are `tech5_v1` rows, i.e. the two known false positives — which is the point: clearing 1.0 at fast is necessary, not sufficient, and a spec **below** 1.0 at fast has no realistic canonical path. Do not spend canonical compute on a near-miss to "see if it gets over the line"; the fast gate exists to protect that budget. 11 canonical evals have ever run; zero strategies have survived one.

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
