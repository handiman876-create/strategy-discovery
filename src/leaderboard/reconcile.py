"""Re-classify stored evaluations against current scoring logic.

Use case: gate definitions in `evaluation.scoring.classify_promise` change
between releases — for example, P2 added MIN_TRADES_FOR_PROMISING. Rows
written under the OLD logic carry stale `promising` and `failed_gates`
columns. Reconcile re-runs `classify_promise` against the stored
breakdown + n_oos_trades + ci_lower (extracted from the on-disk
`fast_summary.json`), overwrites the two derived columns, and logs every
change.

Scope:
  * Only `fast` evaluations are reconciled today. canonical/holdout are
    skipped because the DB has none yet — extending here means writing
    against an untested code path. Future enhancement: reconcile all
    eval_types once we have rows of those types and their on-disk shape
    is settled.
  * Touches only `promising` and `failed_gates`. eval_type, evaluated_at,
    n_oos_trades, score reflect what the pipeline actually produced and
    must not move under reconciliation.

Idempotent: a second run on a reconciled DB produces zero updates.

ci_lower extraction: `fast_summary.json` doesn't store ci_lower as a
top-level field — it's only visible in `verdict.failed_conditions`
when the gate failed. When absent, the gate previously passed; a
sentinel value above the current threshold is used so re-classification
keeps the same direction. If the ci_lower threshold is ever tightened,
a previously-passing eval whose actual ci_lower is below the new
threshold can't be detected here — re-run the eval against the canonical
pipeline rather than reconciling.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from evaluation.scoring import ScoreBreakdown, classify_promise

logger = logging.getLogger(__name__)

# Sentinel ci_lower for evals whose stored verdict didn't record a ci_lower
# failed_condition. Must be strictly greater than the current default
# `ci_lower_threshold` (1.0) so the gate continues to pass under
# re-classification — we know it passed under the old logic, and current
# logic uses the same threshold.
_CI_LOWER_PASSING_SENTINEL = 2.0


# ── Public dataclasses ───────────────────────────────────────────────────────


@dataclass
class ReconcileChange:
    eval_id: int
    strategy_hash: str
    old_promising: bool
    new_promising: bool
    old_failed_gates: Optional[str]
    new_failed_gates: Optional[str]


@dataclass
class ReconcileSummary:
    n_reconciled: int = 0
    n_unchanged: int = 0
    n_skipped: int = 0
    skipped: list[tuple[int, str]] = field(default_factory=list)
    changes: list[ReconcileChange] = field(default_factory=list)
    log_path: Optional[Path] = None

    def render(self) -> str:
        lines = [
            "Reconcile summary:",
            f"  reconciled : {self.n_reconciled}",
            f"  unchanged  : {self.n_unchanged}",
            f"  skipped    : {self.n_skipped}",
        ]
        if self.changes:
            lines.append(f"  changes (first 10):")
            for ch in self.changes[:10]:
                lines.append(
                    f"    eval_id={ch.eval_id} hash={ch.strategy_hash[:12]} "
                    f"promising {int(ch.old_promising)}→{int(ch.new_promising)}"
                )
        if self.skipped:
            lines.append(f"  skip reasons (first 10):")
            for eid, reason in self.skipped[:10]:
                lines.append(f"    eval_id={eid}: {reason}")
        if self.log_path is not None:
            lines.append(f"  full log: {self.log_path}")
        return "\n".join(lines)


# ── Public API ───────────────────────────────────────────────────────────────


def reconcile_evaluations(
    conn: sqlite3.Connection,
    *,
    project_root: Path,
    log_dir: Optional[Path] = None,
) -> ReconcileSummary:
    """Re-derive promising + failed_gates for every fast evaluation in the
    DB. Returns a ReconcileSummary; per-row failures (missing results dir,
    malformed summary file) are recorded as `skipped` and don't abort the
    run — same log-and-continue policy as backfill.

    `project_root` is the base for relative `results_dir` paths stored on
    evaluation rows. Absolute paths are honored as-is.

    `log_dir` defaults to `project_root / "results"`. A timestamped log of
    every change + skip is written to `<log_dir>/reconcile_<utc_ts>.log`."""
    summary = ReconcileSummary()
    rows = conn.execute(
        "SELECT id, strategy_hash, eval_type, n_oos_trades, promising, "
        "failed_gates, results_dir "
        "FROM evaluations WHERE eval_type = 'fast' ORDER BY id"
    ).fetchall()

    for row in rows:
        try:
            change = _reconcile_one(conn, row, project_root)
        except _SkipRow as e:
            summary.n_skipped += 1
            summary.skipped.append((int(row["id"]), str(e)))
            continue
        if change is None:
            summary.n_unchanged += 1
        else:
            summary.n_reconciled += 1
            summary.changes.append(change)

    log_dir = log_dir or (project_root / "results")
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_dir / f"reconcile_{stamp}.log"
    with log_path.open("w") as f:
        f.write("Reconcile log\n")
        f.write(f"timestamp:    {datetime.now(timezone.utc).isoformat()}\n")
        f.write(f"project_root: {project_root}\n\n")
        f.write(f"reconciled: {summary.n_reconciled}\n")
        f.write(f"unchanged:  {summary.n_unchanged}\n")
        f.write(f"skipped:    {summary.n_skipped}\n\n")
        f.write(f"-- changes ({len(summary.changes)}) --\n")
        for ch in summary.changes:
            f.write(
                f"eval_id={ch.eval_id}\thash={ch.strategy_hash}\t"
                f"promising={int(ch.old_promising)}->{int(ch.new_promising)}\t"
                f"old_failed_gates={ch.old_failed_gates}\t"
                f"new_failed_gates={ch.new_failed_gates}\n"
            )
        f.write(f"\n-- skipped ({len(summary.skipped)}) --\n")
        for eid, reason in summary.skipped:
            f.write(f"eval_id={eid}\t{reason}\n")
    summary.log_path = log_path
    return summary


# ── Internals ────────────────────────────────────────────────────────────────


class _SkipRow(Exception):
    """Sentinel raised by _reconcile_one to short-circuit a row into the
    skipped list without aborting the whole reconcile run."""


def _reconcile_one(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    project_root: Path,
) -> Optional[ReconcileChange]:
    """Reconcile a single evaluation row. Returns the ReconcileChange when
    promising or failed_gates moved; None when the row was already current.
    Raises _SkipRow on any I/O or parse failure."""
    summary_path = _resolve_summary_path(row["results_dir"], project_root)
    if not summary_path.exists():
        raise _SkipRow(f"summary file not found: {summary_path}")
    try:
        data = json.loads(summary_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise _SkipRow(f"read/parse failed: {type(e).__name__}: {e}") from e

    try:
        breakdown = _breakdown_from_summary(data)
        n_oos = int(data["n_oos_trades_total"])
    except (KeyError, TypeError, ValueError) as e:
        raise _SkipRow(f"summary missing fields: {type(e).__name__}: {e}") from e

    ci_lower = _ci_lower_from_summary(data)
    verdict = classify_promise(
        breakdown, ci_lower=ci_lower, n_oos_trades_total=n_oos
    )

    new_promising = bool(verdict.is_promising)
    new_failed_gates = (
        json.dumps([c.to_dict() for c in verdict.failed_conditions])
        if verdict.failed_conditions
        else None
    )
    old_promising = bool(row["promising"])
    old_failed_gates: Optional[str] = row["failed_gates"]

    if old_promising == new_promising and _gates_equal(
        old_failed_gates, new_failed_gates
    ):
        return None

    conn.execute(
        "UPDATE evaluations SET promising = ?, failed_gates = ? WHERE id = ?",
        (1 if new_promising else 0, new_failed_gates, int(row["id"])),
    )
    return ReconcileChange(
        eval_id=int(row["id"]),
        strategy_hash=row["strategy_hash"],
        old_promising=old_promising,
        new_promising=new_promising,
        old_failed_gates=old_failed_gates,
        new_failed_gates=new_failed_gates,
    )


def _resolve_summary_path(results_dir: str, project_root: Path) -> Path:
    p = Path(results_dir)
    if not p.is_absolute():
        p = project_root / p
    return p / "fast_summary.json"


def _breakdown_from_summary(data: dict) -> ScoreBreakdown:
    b = data["breakdown"]
    return ScoreBreakdown(
        median_pf=float(b["median_pf"]),
        consistency_factor=float(b["consistency_factor"]),
        parameter_penalty=float(b["parameter_penalty"]),
        significance_factor=float(b["significance_factor"]),
        score=float(b["score"]),
    )


def _ci_lower_from_summary(data: dict) -> float:
    """Extract ci_lower from the stored verdict's failed_conditions when
    present (the old gate failed → we know the actual value). When absent,
    the gate previously passed; return a sentinel that still passes the
    current threshold."""
    verdict = data.get("verdict") or {}
    for cond in verdict.get("failed_conditions") or []:
        if cond.get("name") == "ci_lower":
            return float(cond["actual"])
    return _CI_LOWER_PASSING_SENTINEL


def _gates_equal(a: Optional[str], b: Optional[str]) -> bool:
    """Compare two failed_gates JSON strings as Python objects so whitespace
    and dict-key ordering don't produce false diffs. NULL == NULL."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return json.loads(a) == json.loads(b)
    except json.JSONDecodeError:
        return a == b
