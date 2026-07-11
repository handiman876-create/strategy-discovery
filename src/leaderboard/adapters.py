"""Adapters from pipeline-side dataclasses to leaderboard write payloads.

Centralized so the same conversion isn't dispatched from multiple call
sites. Each helper has two consumers:

  * to_evaluation_record:   evaluation.pipeline.run_evaluation and
                            evaluation.fast_pipeline.run_fast_evaluation
  * to_generation_metadata: generator.pipeline.generate_and_translate

Pure functions; no I/O, no DB. Tests construct call-site fixtures
without spinning up a database or an Anthropic client.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .models import EvaluationRecord, GenerationMetadata


def _parse_iso_to_utc(ts: str) -> datetime:
    """ISO-8601 string → tz-aware UTC datetime. Naive strings are treated
    as UTC. Both claude_client._now_iso() and leaderboard.models._utcnow_iso()
    produce tz-aware UTC; this normalization defends against test fixtures
    or older logs written without tzinfo so duration arithmetic doesn't
    blow up with a naive/aware mismatch."""
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def to_generation_metadata(
    logs: list[Any],
    *,
    archetype: str,
    requested_timeframe: str | None = None,
    spec_path: str | None = None,
    now_iso: str | None = None,
) -> GenerationMetadata:
    """Aggregate per-attempt GenerationLogs into one write payload.

    cost_usd and retry_count aggregate across attempts. model_version,
    prompt_hash, raw_response_path come from the LAST log (the call site
    only invokes this on a successful generation's tail). generated_at is
    the FIRST log's timestamp — when generation began.

    duration_seconds is end-to-end wall clock from the first log's
    timestamp to now_iso (defaults to UTC now), so it captures retries
    plus translation/dedup overhead — more meaningful for the leaderboard
    than per-API-call latency.

    Quirk firing counters default to 0; per-generation attribution is
    deferred. See docs/backlog.md "Phase 4.5: leaderboard follow-ups".
    """
    if not logs:
        raise ValueError("to_generation_metadata requires at least one log")
    first = logs[0]
    last = logs[-1]
    end = _parse_iso_to_utc(now_iso) if now_iso else datetime.now(timezone.utc)
    started = _parse_iso_to_utc(first.timestamp)
    duration = (end - started).total_seconds()

    return GenerationMetadata(
        model_version=last.model,
        prompt_hash=last.prompt_hash,
        archetype=archetype,
        cost_usd=sum(l.actual_cost_usd for l in logs),
        retry_count=len(logs),
        duration_seconds=duration,
        raw_response_path=getattr(last, "raw_response_path", None),
        spec_path=spec_path,
        requested_timeframe=requested_timeframe,
        generated_at=first.timestamp,
    )


def to_evaluation_record(
    pipeline_result: Any,
    *,
    eval_type: str,
    config_json: str | None = None,
) -> EvaluationRecord:
    """Convert a pipeline-side eval result into the leaderboard write
    payload. Handles both `evaluation.pipeline.EvaluationResult`
    (canonical) and `evaluation.fast_pipeline.FastEvaluationResult`
    (fast). The fast type exposes n_oos_trades_total directly; the
    canonical type requires summing per_symbol entries.

    config_json defaults to json.dumps(pipeline_result.config); pass an
    explicit value when the caller has already serialized.
    """
    is_fast = getattr(pipeline_result, "is_fast", False)
    if is_fast:
        n_oos = pipeline_result.n_oos_trades_total
    else:
        n_oos = sum(s.n_oos_trades for s in pipeline_result.per_symbol)

    breakdown = pipeline_result.breakdown
    verdict = pipeline_result.verdict
    failed = [c.to_dict() for c in (verdict.failed_conditions or [])]

    output_dir = getattr(pipeline_result, "output_dir", None)
    results_dir = str(output_dir) if output_dir is not None else ""
    cfg = (
        config_json
        if config_json is not None
        else json.dumps(pipeline_result.config, default=str)
    )

    return EvaluationRecord(
        n_oos_trades=n_oos,
        promising=bool(verdict.is_promising),
        results_dir=results_dir,
        config_json=cfg,
        median_pf=breakdown.median_pf,
        score=breakdown.score,
        ci_lower=getattr(pipeline_result, "ci_lower", None),
        duration_seconds=None,
        failed_conditions=failed,
    )
