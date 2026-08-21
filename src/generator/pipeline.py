"""Generation orchestrator.

generate_strategy(archetype) → GenerateResult (spec, logs)
generate_and_translate(archetype) → GenerateResult (spec, logs, hash, code_path)

Retry loop: up to 3 attempts. Each retry feeds the prior attempt's failure
reason back to Claude as `retry_feedback`. Validation errors include
StrategySpec parse errors, translator validation errors, and behavioral-dedup
collisions.

Diversity context: scans `results/generations/` for the most recent N
successful generations of the same archetype and includes a short summary
in the prompt under "Already explored — your spec must materially differ".
"""

from __future__ import annotations

import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from leaderboard.adapters import to_generation_metadata
from leaderboard.record import record_generation

from .archetypes import get_archetype
from .claude_client import ClaudeClient, GenerationLog
from .dedup import compute_strategy_hash
from .paths import generations_dir, quirks_path
from .spec import StrategySpec
from .translator import GENERATED_DIR, TranslationError, translate_to_file

logger = logging.getLogger(__name__)

DEFAULT_MAX_RETRIES = 3
DEFAULT_DIVERSITY_N = 5

# Per-attempt reason text is truncated to this many characters in the assembled
# failure_reason. Pydantic validation errors carry a URL and a type tag that add
# ~80 chars of boilerplate after the part that identifies the field, so this has
# to be generous enough to survive them.
_REASON_CHARS = 240

# Strips the "Attempt 2 " that retry-feedback strings already carry, plus the
# bare "failed: " that the API-error path adds, so the assembled line reads
# "attempt 2: <the actual reason>" instead of "attempt 2: failed: <reason>".
_ATTEMPT_PREFIX = re.compile(r"^Attempt\s+\d+\s+(?:failed:\s*)?")



@dataclass
class GenerateResult:
    spec: StrategySpec | None
    logs: list[GenerationLog]
    strategy_hash: str | None = None
    code_path: Path | None = None
    failure_reason: str | None = None


def generate_strategy(
    archetype: str,
    *,
    client: ClaudeClient | None = None,
    diversity_n: int = DEFAULT_DIVERSITY_N,
    max_retries: int = DEFAULT_MAX_RETRIES,
    requested_timeframe: str | None = None,
) -> GenerateResult:
    """Generate a single valid spec for `archetype`. Retries up to `max_retries`
    times with error feedback. Returns the spec + the full attempt log.

    requested_timeframe: when set, the generation prompt is constrained
    to that timeframe and each spec is rejected (counts as a failed
    attempt, increments the timeframe_mismatch quirk counter) if its
    timeframes field doesn't equal [requested_timeframe]."""
    client = client or ClaudeClient()
    arch = get_archetype(archetype)
    diversity = _load_diversity_context(archetype, n=diversity_n)
    prior_hashes = _load_prior_strategy_hashes(archetype)

    logs: list[GenerationLog] = []
    feedback: str | None = None
    last_was_tf_mismatch = False
    attempt_reasons: list[str] = []

    for attempt in range(1, max_retries + 1):
        spec, log, fb, was_tf_mismatch = _generate_spec_with_timeframe_check(
            client=client,
            archetype=archetype,
            diversity_context=diversity,
            retry_feedback=feedback,
            attempt=attempt,
            requested_timeframe=requested_timeframe,
        )
        logs.append(log)

        if spec is None:
            feedback = fb
            attempt_reasons.append(fb or "unknown failure")
            last_was_tf_mismatch = was_tf_mismatch
            continue

        # Translate-level validation (archetype/asset compat, daily_return, pairs).
        try:
            from .translator import validate_for_translation

            validate_for_translation(spec)
        except TranslationError as e:
            feedback = f"Attempt {attempt} translator rejected: {e}"
            attempt_reasons.append(feedback)
            last_was_tf_mismatch = False
            continue

        return GenerateResult(spec=spec, logs=logs)

    if requested_timeframe is not None and last_was_tf_mismatch:
        logger.warning(
            "Generation failed: model could not produce timeframe=%r "
            "after %d attempts. Skipping.",
            requested_timeframe, max_retries,
        )

    return GenerateResult(
        spec=None,
        logs=logs,
        failure_reason=_format_failure_reason(max_retries, attempt_reasons),
    )


def generate_and_translate(
    archetype: str,
    *,
    client: ClaudeClient | None = None,
    diversity_n: int = DEFAULT_DIVERSITY_N,
    max_retries: int = DEFAULT_MAX_RETRIES,
    dedup: bool = True,
    conn: Any = None,
    requested_timeframe: str | None = None,
) -> GenerateResult:
    """Generate + translate. Adds behavioral-dedup retry on top of generate_strategy.

    conn: optional sqlite3.Connection to a leaderboard DB. When set, a
    successful generation is recorded via record_generation before
    returning. Failures are logged at WARNING and swallowed — the
    leaderboard is observability, not critical path. dedup=False
    (strategy_hash unavailable) skips the write with a DEBUG log
    because strategy_hash is the strategies-table primary key.

    requested_timeframe: when set, the generation prompt is constrained
    to that timeframe and each spec is rejected (counts as a failed
    attempt, increments the timeframe_mismatch quirk counter) if its
    timeframes field doesn't equal [requested_timeframe]."""
    client = client or ClaudeClient()
    prior_hashes = _load_prior_strategy_hashes(archetype) if dedup else set()

    feedback: str | None = None
    diversity = _load_diversity_context(archetype, n=diversity_n)
    logs: list[GenerationLog] = []
    last_was_tf_mismatch = False
    attempt_reasons: list[str] = []

    for attempt in range(1, max_retries + 1):
        spec, log, fb, was_tf_mismatch = _generate_spec_with_timeframe_check(
            client=client,
            archetype=archetype,
            diversity_context=diversity,
            retry_feedback=feedback,
            attempt=attempt,
            requested_timeframe=requested_timeframe,
        )
        logs.append(log)

        if spec is None:
            feedback = fb
            attempt_reasons.append(fb or "unknown failure")
            last_was_tf_mismatch = was_tf_mismatch
            continue

        # Dedup BEFORE translate: compute_strategy_hash works directly from
        # the spec, so we can reject duplicates without paying the
        # translate_to_file cost (file write + scan_unreachable_defaults +
        # quirk-counter side effect). Side effect: if a duplicate spec
        # would also have failed validate_for_translation, we now miss
        # that signal — but the spec was a duplicate anyway, so the
        # validation failure was redundant information.
        if dedup:
            try:
                bh = compute_strategy_hash(spec)
            except Exception as e:
                feedback = f"Attempt {attempt} strategy hash failed: {e}"
                attempt_reasons.append(feedback)
                last_was_tf_mismatch = False
                continue

            if bh in prior_hashes:
                feedback = (
                    f"Attempt {attempt} duplicate of an existing strategy "
                    f"(structural hash {bh[:12]}). Choose materially different "
                    f"parameters or logic."
                )
                attempt_reasons.append(feedback)
                last_was_tf_mismatch = False
                continue
        else:
            bh = None

        try:
            path = translate_to_file(spec, overwrite=True)
        except TranslationError as e:
            feedback = f"Attempt {attempt} translator rejected: {e}"
            attempt_reasons.append(feedback)
            last_was_tf_mismatch = False
            continue
        except Exception as e:
            feedback = f"Attempt {attempt} translator crashed: {e}"
            attempt_reasons.append(feedback)
            last_was_tf_mismatch = False
            continue

        _record_generation_to_leaderboard(
            spec=spec,
            bh=bh,
            logs=logs,
            conn=conn,
            archetype=archetype,
            code_path=path,
            requested_timeframe=requested_timeframe,
        )

        return GenerateResult(spec=spec, logs=logs, strategy_hash=bh, code_path=path)

    if requested_timeframe is not None and last_was_tf_mismatch:
        logger.warning(
            "Generation failed: model could not produce timeframe=%r "
            "after %d attempts. Skipping.",
            requested_timeframe, max_retries,
        )

    return GenerateResult(
        spec=None,
        logs=logs,
        failure_reason=_format_failure_reason(max_retries, attempt_reasons),
    )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _format_failure_reason(max_retries: int, attempt_reasons: list[str]) -> str:
    """Assemble the GEN-FAIL text from the per-attempt reasons.

    WHY: until 2026-08-21 this was the bare string "all N attempts failed" and
    the retry loop threw away the reason it already held in `feedback`. When the
    model started stringifying `position_sizing` on 2026-08-19, three nightly
    runs logged 33 identical contentless GEN-FAILs a night; diagnosing it meant
    grepping 2,008 archived generation records for the error text that had been
    in this function's hands all along.

    The first line keeps the exact legacy prefix ("all N attempts failed") so
    existing log greps and tests still match; the reasons follow indented.
    """
    header = f"all {max_retries} attempts failed"
    if not attempt_reasons:
        return header
    lines = [f"{header}:"]
    for n, reason in enumerate(attempt_reasons, start=1):
        # Reasons arrive as "Attempt 2 translator rejected: ..." — strip the
        # embedded attempt number so it is not printed twice, and flatten the
        # multi-line pydantic errors onto one line each.
        flat = " ".join(_ATTEMPT_PREFIX.sub("", str(reason)).split())
        lines.append(f"  attempt {n}: {flat[:_REASON_CHARS]}")
    return "\n".join(lines)


def _generate_spec_with_timeframe_check(
    *,
    client: ClaudeClient,
    archetype: str,
    diversity_context: list[dict] | None,
    retry_feedback: str | None,
    attempt: int,
    requested_timeframe: str | None,
) -> tuple[StrategySpec | None, GenerationLog, str | None, bool]:
    """Run one API attempt and validate timeframe compliance.

    Returns (spec, log, retry_feedback, was_timeframe_mismatch):
      * (None,  log, fb,   False) — API parse failed; fb describes the parse error
      * (None,  log, fb,   True ) — spec parsed but timeframes mismatched; counter incremented
      * (spec,  log, None, False) — success

    The 4th element lets the outer retry loop track whether the LAST
    failure was specifically a timeframe mismatch, so the end-of-loop
    "could not produce timeframe X" WARNING fires only when warranted
    (not for parse failures, translator rejections, or dedup hits — each
    of which has its own retry feedback message)."""
    spec, log = client.generate_spec(
        archetype,
        diversity_context=diversity_context,
        retry_feedback=retry_feedback,
        attempt=attempt,
        requested_timeframe=requested_timeframe,
    )
    if spec is None:
        return None, log, f"Attempt {attempt} failed: {log.error}", False
    if requested_timeframe is not None and spec.timeframes != [requested_timeframe]:
        _record_timeframe_mismatch_quirk(
            requested=requested_timeframe,
            actual=list(spec.timeframes),
            model=log.model,
            archetype=archetype,
        )
        return None, log, (
            f"Attempt {attempt} timeframe_mismatch: "
            f"requested={requested_timeframe!r}, got={list(spec.timeframes)!r}. "
            f"The timeframes field of your spec MUST be [{requested_timeframe!r}]."
        ), True
    return spec, log, None, False


def _record_timeframe_mismatch_quirk(
    *,
    requested: str,
    actual: list[str],
    model: str,
    archetype: str,
) -> None:
    """Persist a counter row when the timeframe-compliance check fires.
    Mirrors the shape of `_record_string_dsl_quirk` and friends — see
    docs/backlog.md "Centralize the quirk-counter pattern" for the
    refactor plan that will collapse all four into one helper. Defensive:
    any I/O failure is swallowed — quirk logging must never break the
    generation flow."""
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        data: dict = {}
        if quirks_path().exists():
            data = json.loads(quirks_path().read_text())
        rec = data.setdefault(
            "timeframe_mismatch",
            {
                "total": 0,
                "by_model": {},
                "by_archetype": {},
                "by_requested_timeframe": {},
                "first_seen": now,
                "last_seen": now,
            },
        )
        rec["total"] += 1
        rec["by_model"][model] = rec["by_model"].get(model, 0) + 1
        rec["by_archetype"][archetype] = rec["by_archetype"].get(archetype, 0) + 1
        rec["by_requested_timeframe"][requested] = (
            rec["by_requested_timeframe"].get(requested, 0) + 1
        )
        rec["last_seen"] = now
        quirks_path().parent.mkdir(parents=True, exist_ok=True)
        quirks_path().write_text(json.dumps(data, indent=2))
    except Exception as e:
        logger.warning(
            "failed to record timeframe_mismatch quirk to %s: %s", quirks_path(), e
        )


def _record_generation_to_leaderboard(
    *,
    spec: StrategySpec,
    bh: str | None,
    logs: list[GenerationLog],
    conn: Any,
    archetype: str,
    code_path: Path,
    requested_timeframe: str | None = None,
) -> None:
    """Persist this generation to the leaderboard. Three early exits:

      conn is None       — no DB configured (typical for tests / dry runs)
      bh   is None       — dedup=False, no primary key for the strategies row
      record_generation raises — log warning, swallow

    The third case is the load-bearing one: the leaderboard is an
    observability/audit surface, not a critical-path output. Generation
    succeeded and the on-disk artifacts already exist; a DB write failure
    must not lose the strategy that was just produced.
    """
    if conn is None:
        return
    if bh is None:
        logger.debug("skipping leaderboard write, dedup disabled")
        return
    try:
        metadata = to_generation_metadata(
            logs,
            archetype=archetype,
            spec_path=str(code_path),
            requested_timeframe=requested_timeframe,
        )
        record_generation(conn, spec, bh, metadata)
    except Exception as e:
        logger.warning(
            "leaderboard write failed for strategy %s (hash %s): %s",
            spec.name, bh[:12], e,
        )


def _load_diversity_context(archetype: str, n: int) -> list[dict]:
    """Scan results/generations/ for recent successful generations of this
    archetype, return short summaries for the prompt."""
    if not generations_dir().exists():
        return []
    matches: list[tuple[str, dict]] = []
    for path in sorted(generations_dir().glob(f"*_{archetype}_*.json"), reverse=True):
        try:
            payload = json.loads(path.read_text())
        except Exception:
            continue
        if payload.get("spec") is None:
            continue
        spec = payload["spec"]
        matches.append(
            (
                payload["timestamp"],
                {
                    "name": spec.get("name"),
                    "thesis": spec.get("thesis", ""),
                    "indicators": [i["type"] for i in spec.get("indicators", [])],
                },
            )
        )
        if len(matches) >= n:
            break
    return [m[1] for m in matches]


def _load_prior_strategy_hashes(archetype: str) -> set[str]:
    if not generations_dir().exists():
        return set()
    out: set[str] = set()
    for path in generations_dir().glob(f"*_{archetype}_*.json"):
        try:
            payload = json.loads(path.read_text())
        except Exception:
            continue
        h = payload.get("strategy_hash")
        if h:
            out.add(h)
    return out


