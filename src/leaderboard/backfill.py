"""Backfill historical results/ artifacts into the leaderboard DB.

The leaderboard's write hooks were added in Phase 4 step 8 — anything
generated or evaluated before that point exists only as on-disk artifacts
under `results/`. This module walks those artifacts and writes equivalent
rows, marked `imported_from='backfill'`, so the leaderboard reflects the
project's full history.

Three operations:
  * `recover_strategy_hash`  — recompute the hash from a stored spec dict
                                  (the hash was never persisted in the
                                  generation log, so backfill has to
                                  reconstruct it via the current translator)
  * `backfill_generations`     — iterate `results/generations/*.json`
  * `backfill_evaluations`     — iterate `results/{eval,fast_eval}_<ts>/`

Two policies that govern this module:

  log-and-continue  Per-directory failures (malformed JSON, missing field,
                    unrecognized shape, hash recomputation error) are logged
                    and skipped, never raised. The end-of-run summary
                    reports counts and the first 10 reasons; full detail
                    goes to results/backfill_<ts>.log.

  idempotent on natural keys
                    `strategies`  — `strategy_hash` is the PK; ON CONFLICT
                                    in record_generation already handles it
                    `generations` — (strategy_hash, generated_at, prompt_hash)
                                    SELECT-then-INSERT (no UNIQUE in schema)
                    `evaluations` — (strategy_hash, eval_type, evaluated_at)
                                    SELECT-then-INSERT (no UNIQUE in schema)

NOTE: The fast_eval → generations linkage relies on `_save_log`'s filename
pattern `results/generations/<ts>_<archetype>_<snake_name>.json`, plus the
`spec.name → CamelCase` convention used by both the translator and the
fast-eval report writer. If those patterns change, this lookup breaks.
Backfill is a one-time historical operation, so this dispatched contract
is acceptable here. **Do not add new code that depends on this pattern** —
generations should be linked via the leaderboard's `strategy_hash` going
forward.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from generator.dedup import compute_strategy_hash
from generator.spec import StrategySpec
from generator.translator import TranslationError, validate_for_translation

from .models import EvaluationRecord, GenerationMetadata
from .record import record_evaluation, record_generation

logger = logging.getLogger(__name__)


# ── Public summary ───────────────────────────────────────────────────────────


@dataclass
class BackfillSummary:
    imported_strategies: int = 0
    imported_generations: int = 0
    imported_evaluations: int = 0
    skipped_generations: list[tuple[str, str]] = field(default_factory=list)
    skipped_evaluations: list[tuple[str, str]] = field(default_factory=list)
    log_path: Optional[Path] = None

    def render(self) -> str:
        lines = [
            "Backfill summary:",
            f"  imported_strategies   : {self.imported_strategies}",
            f"  imported_generations  : {self.imported_generations}",
            f"  imported_evaluations  : {self.imported_evaluations}",
            f"  skipped (generations) : {len(self.skipped_generations)}",
            f"  skipped (evaluations) : {len(self.skipped_evaluations)}",
        ]
        if self.skipped_generations:
            lines.append("  generation skip reasons (first 10):")
            for path, reason in self.skipped_generations[:10]:
                lines.append(f"    {Path(path).name}: {reason}")
        if self.skipped_evaluations:
            lines.append("  evaluation skip reasons (first 10):")
            for path, reason in self.skipped_evaluations[:10]:
                lines.append(f"    {Path(path).name}: {reason}")
        if self.log_path is not None:
            lines.append(f"  full skip log: {self.log_path}")
        return "\n".join(lines)


# ── Hash recovery ────────────────────────────────────────────────────────────


def recover_strategy_hash(
    spec_dict: dict,
    *,
    archetype: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Recompute strategy_hash from a stored spec dict.

    With Phase 4 step 10's structural hashing, this is two steps:

      1. StrategySpec.model_validate(spec_dict)  — gates malformed specs
      2. validate_for_translation(spec)          — gates unsatisfiable specs
      3. compute_strategy_hash(spec)             — pure dict canonicalization

    Returns (hash, error). On any failure: (None, reason).

    The previous behavioral-hash version had to translate + import + run
    the strategy on a fixture; that's gone, replaced by step 3 above.
    The validate_for_translation gate is preserved deliberately — a spec
    that doesn't translate isn't a usable strategy, even if it now has a
    well-defined structural hash. Documented "real product losses" from
    schema drift (pre-step-9 archetype-timeframe combos, pre-Phase-3
    multi-timeframe, stale indicator kwargs) stay as documented losses
    with the same reasons.

    Side-effect-free: no I/O, no file writes, no quirk-counter writes.
    """
    try:
        spec = StrategySpec.model_validate(spec_dict)
    except Exception as e:
        return None, f"spec_validation: {type(e).__name__}: {e}"

    try:
        validate_for_translation(spec)
    except TranslationError as e:
        return None, f"translator_validate: {e}"
    except Exception as e:
        return None, f"translator_validate_unexpected: {type(e).__name__}: {e}"

    try:
        return compute_strategy_hash(spec), None
    except Exception as e:
        return None, f"compute_strategy_hash: {type(e).__name__}: {e}"


def _snake_from_camel(camel: str) -> str:
    """Convert CamelCase → snake_case. Mirror of the convention used by
    the translator (`"".join(p.capitalize() for p in name.split("_"))`)
    in reverse — needed because eval reports persist the CamelCase class
    name and backfill needs the snake_case spec name to find the matching
    generation log."""
    # Insert _ before capitals (except the first), then lowercase.
    s1 = re.sub(r"(?<!^)(?=[A-Z])", "_", camel)
    return s1.lower()


# ── Generations backfill ─────────────────────────────────────────────────────


def backfill_generations(
    conn: sqlite3.Connection,
    results_dir: Path,
    summary: BackfillSummary,
) -> None:
    """Walk results/generations/*.json, recover hash for each successful
    spec, and insert via record_generation with imported_from='backfill'.
    Failed-generation logs (spec=None) are skipped — the leaderboard's
    domain is strategies, and failed-generation telemetry already lives
    in spend tracker + quirk counters."""
    gen_dir = results_dir / "generations"
    if not gen_dir.exists():
        logger.warning("no generations/ dir at %s; skipping", gen_dir)
        return

    seen_strategy_hashes: set[str] = set()
    for log_path in sorted(gen_dir.glob("*.json")):
        try:
            payload = json.loads(log_path.read_text())
        except Exception as e:
            summary.skipped_generations.append(
                (str(log_path), f"json_parse: {type(e).__name__}: {e}")
            )
            continue

        if payload.get("spec") is None:
            # Failed generation — out of leaderboard scope.
            summary.skipped_generations.append((str(log_path), "spec_is_none"))
            continue

        spec_dict = dict(payload["spec"])
        archetype = payload.get("archetype")
        bh, err = recover_strategy_hash(spec_dict, archetype=archetype)
        if bh is None:
            summary.skipped_generations.append((str(log_path), err or "unknown"))
            continue

        # Idempotency: SELECT before INSERT on (strategy_hash, generated_at,
        # prompt_hash). Schema has no UNIQUE here, so we check explicitly.
        existing = conn.execute(
            "SELECT 1 FROM generations WHERE strategy_hash = ? "
            "AND generated_at = ? AND prompt_hash = ?",
            (bh, payload["timestamp"], payload.get("prompt_hash", "")),
        ).fetchone()
        if existing:
            summary.skipped_generations.append((str(log_path), "duplicate"))
            continue

        try:
            spec = StrategySpec.model_validate(spec_dict)
            metadata = GenerationMetadata(
                model_version=payload.get("model", "unknown"),
                prompt_hash=payload.get("prompt_hash", ""),
                archetype=archetype or "unknown",
                cost_usd=payload.get("actual_cost_usd", 0.0) or 0.0,
                retry_count=int(payload.get("attempt", 1)),
                duration_seconds=0.0,  # not tracked in legacy logs
                raw_response_path=str(log_path),
                spec_path=None,
                requested_timeframe=None,
                generated_at=payload["timestamp"],
            )
            record_generation(
                conn, spec, bh, metadata, imported_from="backfill"
            )
            summary.imported_generations += 1
            if bh not in seen_strategy_hashes:
                seen_strategy_hashes.add(bh)
                summary.imported_strategies += 1
        except Exception as e:
            summary.skipped_generations.append(
                (str(log_path), f"db_write: {type(e).__name__}: {e}")
            )


# ── Evaluations backfill ─────────────────────────────────────────────────────


def backfill_evaluations(
    conn: sqlite3.Connection,
    results_dir: Path,
    summary: BackfillSummary,
) -> None:
    """Walk results/{eval,fast_eval}_<ts>/<StrategyName>/*summary.json and
    insert via record_evaluation with imported_from='backfill'.

    Each eval row's strategy_hash is recovered by:
      1. CamelCase → snake_case (eval report's "strategy" field → spec name)
      2. Find the most recent generations/*.json matching that spec name
      3. Recompute strategy_hash from that log's spec
    """
    if not results_dir.exists():
        return

    # Ordered chronologically (oldest first) so auto-promote status
    # transitions in record_evaluation land monotonically.
    eval_dirs: list[tuple[str, Path]] = []
    for entry in sorted(results_dir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("fast_eval_"):
            eval_dirs.append(("fast", entry))
        elif entry.name.startswith("eval_"):
            eval_dirs.append(("canonical", entry))

    for eval_type, outer in eval_dirs:
        summary_filename = (
            "fast_summary.json" if eval_type == "fast" else "summary.json"
        )
        for child in sorted(outer.iterdir()):
            if not child.is_dir():
                continue
            sf = child / summary_filename
            if not sf.exists():
                summary.skipped_evaluations.append(
                    (str(child), f"missing_{summary_filename}")
                )
                continue
            _try_backfill_one_evaluation(
                conn, sf, eval_type, results_dir, summary
            )


def _try_backfill_one_evaluation(
    conn: sqlite3.Connection,
    summary_path: Path,
    eval_type: str,
    results_dir: Path,
    summary: BackfillSummary,
) -> None:
    try:
        payload = json.loads(summary_path.read_text())
    except Exception as e:
        summary.skipped_evaluations.append(
            (str(summary_path), f"json_parse: {type(e).__name__}: {e}")
        )
        return

    strategy_class = payload.get("strategy")
    if not strategy_class:
        summary.skipped_evaluations.append(
            (str(summary_path), "missing_strategy_field")
        )
        return

    bh, err = _hash_for_class(strategy_class, results_dir)
    if bh is None:
        summary.skipped_evaluations.append((str(summary_path), err or "no_hash"))
        return

    # Build EvaluationRecord. Both fast and canonical summaries carry
    # `verdict` and `breakdown`, but the failed_conditions are nested
    # differently and n_oos comes from different fields per shape.
    try:
        verdict = payload.get("verdict") or {}
        breakdown = payload.get("breakdown") or verdict.get("breakdown") or {}
        if eval_type == "fast":
            n_oos = int(payload.get("n_oos_trades_total", 0))
        else:
            per_symbol = payload.get("per_symbol", []) or []
            n_oos = sum(int(s.get("n_oos_trades", 0)) for s in per_symbol)

        evaluated_at = _evaluated_at_from_dirname(summary_path.parent.parent.name)
        record = EvaluationRecord(
            n_oos_trades=n_oos,
            promising=bool(verdict.get("is_promising", False)),
            results_dir=str(summary_path.parent),
            config_json=json.dumps(payload.get("config", {}), default=str),
            median_pf=breakdown.get("median_pf"),
            score=breakdown.get("score"),
            duration_seconds=None,
            failed_conditions=verdict.get("failed_conditions", []) or [],
            evaluated_at=evaluated_at,
        )
    except Exception as e:
        summary.skipped_evaluations.append(
            (str(summary_path), f"build_record: {type(e).__name__}: {e}")
        )
        return

    # Idempotency on (strategy_hash, eval_type, evaluated_at).
    existing = conn.execute(
        "SELECT 1 FROM evaluations WHERE strategy_hash = ? "
        "AND eval_type = ? AND evaluated_at = ?",
        (bh, eval_type, evaluated_at),
    ).fetchone()
    if existing:
        summary.skipped_evaluations.append((str(summary_path), "duplicate"))
        return

    try:
        record_evaluation(
            conn, bh, record, eval_type, imported_from="backfill"
        )
        summary.imported_evaluations += 1
    except Exception as e:
        summary.skipped_evaluations.append(
            (str(summary_path), f"db_write: {type(e).__name__}: {e}")
        )


def _hash_for_class(
    camel_name: str, results_dir: Path
) -> tuple[Optional[str], Optional[str]]:
    """Find the most recent generation log whose spec.name matches
    snake_case(camel_name), recompute its hash, return (hash, None) on
    success or (None, error_reason) on any failure."""
    snake = _snake_from_camel(camel_name)
    gen_dir = results_dir / "generations"
    if not gen_dir.exists():
        return None, f"no_generations_dir for {camel_name}"
    matches = sorted(gen_dir.glob(f"*_{snake}.json"), reverse=True)
    if not matches:
        return None, f"no_generation_log_for {camel_name}"
    for log_path in matches:
        try:
            payload = json.loads(log_path.read_text())
        except Exception:
            continue
        if payload.get("spec") is None:
            continue
        bh, err = recover_strategy_hash(
            dict(payload["spec"]), archetype=payload.get("archetype")
        )
        if bh is not None:
            return bh, None
    return None, f"no_recoverable_hash_in_logs for {camel_name}"


def _evaluated_at_from_dirname(dirname: str) -> str:
    """`fast_eval_20260428_015353` → `2026-04-28T01:53:53+00:00`. Best-
    effort: if the suffix doesn't parse, fall back to UTC now (so the row
    still lands; the dirname goes into results_dir for traceability)."""
    m = re.match(r"^(?:fast_)?eval_(\d{8})_(\d{6})$", dirname)
    if not m:
        return datetime.now(timezone.utc).isoformat()
    date_part, time_part = m.group(1), m.group(2)
    try:
        dt = datetime.strptime(date_part + time_part, "%Y%m%d%H%M%S")
        return dt.replace(tzinfo=timezone.utc).isoformat()
    except ValueError:
        return datetime.now(timezone.utc).isoformat()


# ── Composition ──────────────────────────────────────────────────────────────


def backfill_all(
    conn: sqlite3.Connection,
    results_dir: Path,
    log_dir: Optional[Path] = None,
) -> BackfillSummary:
    """Run the full backfill: generations first (to seed strategies +
    generations rows), then evaluations (which FK-reference strategies).

    `log_dir` defaults to `results_dir`. The full skip log is written to
    `<log_dir>/backfill_<utc_ts>.log` so `BackfillSummary.log_path` points
    callers at the detailed rejection reasons."""
    summary = BackfillSummary()
    backfill_generations(conn, results_dir, summary)
    backfill_evaluations(conn, results_dir, summary)

    log_dir = log_dir or results_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_dir / f"backfill_{stamp}.log"
    with log_path.open("w") as f:
        f.write("Backfill log\n")
        f.write(f"results_dir: {results_dir}\n")
        f.write(f"timestamp:   {datetime.now(timezone.utc).isoformat()}\n\n")
        f.write(f"imported_strategies:  {summary.imported_strategies}\n")
        f.write(f"imported_generations: {summary.imported_generations}\n")
        f.write(f"imported_evaluations: {summary.imported_evaluations}\n\n")
        f.write(f"-- generation skips ({len(summary.skipped_generations)}) --\n")
        for path, reason in summary.skipped_generations:
            f.write(f"{path}\t{reason}\n")
        f.write(f"\n-- evaluation skips ({len(summary.skipped_evaluations)}) --\n")
        for path, reason in summary.skipped_evaluations:
            f.write(f"{path}\t{reason}\n")
    summary.log_path = log_path
    return summary
