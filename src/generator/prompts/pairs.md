## Archetype: pairs

**STATUS: DEFERRED to Phase 3.5.**

The Phase-3 engine is single-symbol. Pair trading needs multi-symbol position management; that lands in Phase 3.5. The translator rejects pairs specs.

If asked to generate a pairs strategy, do NOT produce a spec — instead, return a tool call indicating you cannot satisfy the request. The pipeline will catch this gracefully.
