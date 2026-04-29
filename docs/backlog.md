# Backlog

Deferred work that's been observed but isn't actively prioritized. Each entry: what was seen, where, and the proposed direction.

## Seasonality archetype prompt produces theses > 400 chars

**Observed:** During the 2nd 6-archetype `--fast` sweep on 2026-04-29, the `seasonality` archetype was rejected by the spec validator on all 3 generation attempts because the model wrote a thesis longer than the 400-char cap. The 3rd sweep (post-centralization, same date) produced a valid 2-attempt seasonality run, suggesting the failure is intermittent rather than deterministic.

**Direction:** Either tighten the seasonality prompt to constrain thesis length, or raise the cap (e.g. to 600 chars) if the longer theses are genuinely more useful. Decide after one more data point.
