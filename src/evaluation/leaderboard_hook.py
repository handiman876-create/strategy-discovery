"""Shared leaderboard-write hook for the canonical and fast eval pipelines.

Both `evaluation.pipeline.run_evaluation` and
`evaluation.fast_pipeline.run_fast_evaluation` call
`record_evaluation_to_leaderboard` at the end of a successful run. The
helper lives here (rather than duplicated in each pipeline) so the
log-and-continue policy and the adapter call are written once.

Same log-and-continue rationale as the generator hook
(`generator.pipeline._record_generation_to_leaderboard`): the leaderboard
is observability and audit, not critical path. An eval succeeded, the
on-disk report (`results/eval_*` or `results/fast_eval_*`) already
exists; a DB write failure must not erase that work.
"""

from __future__ import annotations

import logging
from typing import Any

from leaderboard.adapters import to_evaluation_record
from leaderboard.record import record_evaluation

logger = logging.getLogger(__name__)


def record_evaluation_to_leaderboard(
    *,
    pipeline_result: Any,
    conn: Any,
    strategy_hash: str | None,
    eval_type: str,
) -> None:
    """Persist this evaluation to the leaderboard. Three early exits:

      conn is None             — no DB configured (typical for tests /
                                 dry runs / scripts/evaluate.py manual
                                 strategies, see backlog Phase 4.5)
      strategy_hash is None    — caller didn't supply one; DEBUG log so
                                 the absence is observable
      record_evaluation raises — log warning, swallow
    """
    if conn is None:
        return
    if strategy_hash is None:
        logger.debug(
            "skipping leaderboard write, no strategy_hash for %s eval",
            eval_type,
        )
        return
    try:
        record = to_evaluation_record(pipeline_result, eval_type=eval_type)
        record_evaluation(conn, strategy_hash, record, eval_type)
    except Exception as e:
        name = getattr(pipeline_result, "strategy_name", "<unknown>")
        logger.warning(
            "leaderboard %s-eval write failed for %s (hash %s): %s",
            eval_type, name, strategy_hash[:12], e,
        )
