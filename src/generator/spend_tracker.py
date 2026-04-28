"""Crash-safe API spend tracking.

Storage: results/api_spend.json
Shape:
  {
    "current_month": "2026-04",
    "months": {
      "2026-04": {
        "pending":   [{"call_id", "estimated_cost_usd", "ts", "model", "archetype"}, ...],
        "completed": [{"call_id", "actual_cost_usd", "input_tokens", "output_tokens",
                       "model", "archetype", "ts"}, ...]
      }
    }
  }

Workflow per call:
  1. estimate_and_reserve(estimated_cost_usd, ...) → call_id appended to pending
  2. ... API call runs ...
  3. record_actual(call_id, actual_cost_usd, ...) → moves entry to completed
     OR record_failure(call_id, error) → marks pending entry as failed (still counted)

Cap check uses sum of completed + sum of pending estimates so a crash mid-call
overstates rather than understates.

Calendar-month rollover: when the current month differs from the stored
"current_month", archive the prior month's data to results/monthly_summary.json
(append) and reset the active month.
"""

from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Sonnet 4.6 list pricing as of 2026-04. Override via env if needed.
DEFAULT_INPUT_PRICE_PER_MTOK = 3.0
DEFAULT_OUTPUT_PRICE_PER_MTOK = 15.0

DEFAULT_SPEND_FILE = (
    Path(__file__).resolve().parents[2] / "results" / "api_spend.json"
)
DEFAULT_ARCHIVE_FILE = (
    Path(__file__).resolve().parents[2] / "results" / "monthly_summary.json"
)


class CapExceededError(RuntimeError):
    """Raised when the projected spend (completed + pending + new estimate) exceeds the cap."""


@dataclass
class PendingEntry:
    call_id: str
    ts: str
    estimated_cost_usd: float
    model: str
    archetype: str
    status: str = "pending"  # pending | failed
    error: str | None = None


@dataclass
class CompletedEntry:
    call_id: str
    ts: str
    actual_cost_usd: float
    input_tokens: int
    output_tokens: int
    model: str
    archetype: str


class SpendTracker:
    def __init__(
        self,
        cap_usd: float = 100.0,
        spend_file: Path = DEFAULT_SPEND_FILE,
        archive_file: Path = DEFAULT_ARCHIVE_FILE,
    ):
        self.cap_usd = cap_usd
        self.spend_file = spend_file
        self.archive_file = archive_file
        self._lock = threading.Lock()
        self.spend_file.parent.mkdir(parents=True, exist_ok=True)

    # ── Public API ───────────────────────────────────────────────────────────

    def estimate_and_reserve(
        self,
        estimated_cost_usd: float,
        *,
        model: str,
        archetype: str,
    ) -> str:
        """Reserve `estimated_cost_usd` for an upcoming call. Refuses if cap
        would be exceeded. Returns a call_id used to reconcile later."""
        with self._lock:
            data = self._load_and_rollover()
            month = data["current_month"]
            month_data = data["months"][month]
            projected = (
                self._sum_completed(month_data) + self._sum_pending(month_data) + estimated_cost_usd
            )
            if projected > self.cap_usd:
                raise CapExceededError(
                    f"Projected spend ${projected:.4f} would exceed monthly cap "
                    f"${self.cap_usd:.2f} (already-completed + reserved-pending = "
                    f"${projected - estimated_cost_usd:.4f})"
                )
            entry = PendingEntry(
                call_id=str(uuid.uuid4()),
                ts=_now_iso(),
                estimated_cost_usd=float(estimated_cost_usd),
                model=model,
                archetype=archetype,
            )
            month_data["pending"].append(asdict(entry))
            self._save(data)
            return entry.call_id

    def record_actual(
        self,
        call_id: str,
        *,
        actual_cost_usd: float,
        input_tokens: int,
        output_tokens: int,
        model: str,
        archetype: str,
    ) -> None:
        """Move the pending entry to completed with actual usage."""
        with self._lock:
            data = self._load_and_rollover()
            month_data = data["months"][data["current_month"]]
            month_data["pending"] = [
                p for p in month_data["pending"] if p["call_id"] != call_id
            ]
            month_data["completed"].append(
                asdict(
                    CompletedEntry(
                        call_id=call_id,
                        ts=_now_iso(),
                        actual_cost_usd=float(actual_cost_usd),
                        input_tokens=int(input_tokens),
                        output_tokens=int(output_tokens),
                        model=model,
                        archetype=archetype,
                    )
                )
            )
            self._save(data)

    def record_failure(self, call_id: str, error: str) -> None:
        """Mark a pending entry as failed. The estimated cost stays counted
        against the cap (over-record by design — the API may have charged us
        even for a failed call)."""
        with self._lock:
            data = self._load_and_rollover()
            for p in data["months"][data["current_month"]]["pending"]:
                if p["call_id"] == call_id:
                    p["status"] = "failed"
                    p["error"] = error
            self._save(data)

    def current_month_total(self) -> float:
        """Best-effort current month spend = completed_actual + pending_estimated."""
        with self._lock:
            data = self._load_and_rollover()
            md = data["months"][data["current_month"]]
            return self._sum_completed(md) + self._sum_pending(md)

    # ── Internals ────────────────────────────────────────────────────────────

    def _load_and_rollover(self) -> dict:
        data = self._load()
        cur = _current_month()
        if data["current_month"] != cur:
            self._archive_month(data, data["current_month"])
            data["current_month"] = cur
        if cur not in data["months"]:
            data["months"][cur] = {"pending": [], "completed": []}
        return data

    def _load(self) -> dict:
        if not self.spend_file.exists():
            return {
                "current_month": _current_month(),
                "months": {_current_month(): {"pending": [], "completed": []}},
            }
        return json.loads(self.spend_file.read_text())

    def _save(self, data: dict) -> None:
        # Atomic write: tmp file + rename.
        tmp = self.spend_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.replace(self.spend_file)

    def _archive_month(self, data: dict, month: str) -> None:
        if month not in data["months"]:
            return
        archive = (
            json.loads(self.archive_file.read_text())
            if self.archive_file.exists()
            else {"months": {}}
        )
        archive["months"][month] = data["months"][month]
        self.archive_file.write_text(json.dumps(archive, indent=2, default=str))
        # Reset the archived month's bucket to empty (don't remove — keeps shape).
        del data["months"][month]

    def _sum_pending(self, month_data: dict) -> float:
        return sum(
            float(p.get("estimated_cost_usd", 0))
            for p in month_data.get("pending", [])
        )

    def _sum_completed(self, month_data: dict) -> float:
        return sum(
            float(c.get("actual_cost_usd", 0))
            for c in month_data.get("completed", [])
        )


# ── Helpers ──────────────────────────────────────────────────────────────────


def _current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def estimate_cost(
    input_tokens: int,
    output_tokens: int,
    *,
    input_price: float = DEFAULT_INPUT_PRICE_PER_MTOK,
    output_price: float = DEFAULT_OUTPUT_PRICE_PER_MTOK,
) -> float:
    return (
        input_tokens / 1_000_000 * input_price
        + output_tokens / 1_000_000 * output_price
    )
