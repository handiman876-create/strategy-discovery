"""Tests for scripts/autodiscover.py's process exit code.

WHY THIS FILE EXISTS: on 2026-08-03 the nightly run produced
    CAND 0..19  GEN-FAIL all 3 attempts failed
    DONE reason=batch_exhausted n=20 hits=0 spent=$0.0000
and exited 0. The Anthropic credit balance had run out, so every generate call
failed before it cost anything — yet `systemctl list-timers` showed the job green
because a swallowed infrastructure failure and a quiet night are indistinguishable
at the exit-code level.

The distinction the exit code now draws:
  - spent $0 AND generated nothing  -> outage (bad key, no credits, provider down) -> 1
  - spent real money, found nothing -> a bad batch, which is the job's normal output -> 0

hits=0 is deliberately NOT part of the condition. The generator screens on
ci_lower > 1.0 and is expected to return zero hits most nights; making an empty
batch "fail" would train everyone to ignore the signal.

Only the generation stage is exercised — every candidate here returns spec=None,
which `continue`s before any evaluation runs, so no backtest or API call happens.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _ROOT / "scripts" / "autodiscover.py"


@pytest.fixture(scope="module")
def ad():
    """Load scripts/autodiscover.py as a module (same convention as
    test_leaderboard_cli.py) so main() can be driven in-process."""
    spec = importlib.util.spec_from_file_location("autodiscover_script", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeLog:
    """Stands in for a generation log row; only the cost field is read."""
    def __init__(self, cost: float):
        self.actual_cost_usd = cost


class _FakeGen:
    """A generation result that failed to produce a spec. `logs` carries the
    cost, which is what separates 'we paid and it failed' from 'we never got
    off the ground'."""
    def __init__(self, cost: float):
        self.spec = None
        self.failure_reason = "all 3 attempts failed"
        self.logs = [_FakeLog(cost)] if cost else []


class _FakeConn:
    def close(self):
        pass


def _run(ad, monkeypatch, tmp_path, *, n: int, cost_per_candidate: float) -> int:
    """Drive main() with generation stubbed out. Returns the exit code."""
    monkeypatch.setattr(ad, "initialize_db", lambda *a, **k: _FakeConn())
    monkeypatch.setattr(ad, "load_symbol_list", lambda *a, **k: ["SPY"])
    monkeypatch.setattr(ad, "generate_and_translate",
                        lambda *a, **k: _FakeGen(cost_per_candidate))
    monkeypatch.setattr(sys, "argv", [
        "autodiscover.py", "--n", str(n), "--fast-only",
        "--cost-ceiling", "10.0",
        "--summary", str(tmp_path / "summary.json"),
    ])
    return ad.main()


def test_exits_1_when_nothing_generated_and_nothing_spent(ad, monkeypatch, tmp_path):
    """The 2026-08-03 signature: 20 GEN-FAILs, spent=$0.0000."""
    rc = _run(ad, monkeypatch, tmp_path, n=20, cost_per_candidate=0.0)
    assert rc == 1, f"total generation failure at $0 spend must exit 1, got {rc}"


def test_exits_0_when_money_was_spent_and_all_candidates_failed(ad, monkeypatch, tmp_path):
    """The control that keeps this signal meaningful. Real generations that all
    fail validation are a BAD BATCH — the ordinary result this job exists to
    produce — not an outage. If this ever returns 1 the alarm becomes noise."""
    rc = _run(ad, monkeypatch, tmp_path, n=5, cost_per_candidate=0.02)
    assert rc == 0, f"a paid-for batch that found nothing is normal, got {rc}"


def test_exits_0_when_no_candidates_were_attempted(ad, monkeypatch, tmp_path):
    """--n 0 spends $0 and generates nothing, but nothing was ever tried, so it is
    not evidence of an outage. This is why the condition is `candidates and ...`
    rather than a bare `spent == 0`."""
    rc = _run(ad, monkeypatch, tmp_path, n=0, cost_per_candidate=0.0)
    assert rc == 0, f"an empty run is not a failure, got {rc}"


def test_summary_is_still_written_on_the_failing_path(ad, monkeypatch, tmp_path):
    """Exit 1 reports the failure; it must not suppress the artifact a human
    reads first."""
    summary = tmp_path / "summary.json"
    monkeypatch.setattr(ad, "initialize_db", lambda *a, **k: _FakeConn())
    monkeypatch.setattr(ad, "load_symbol_list", lambda *a, **k: ["SPY"])
    monkeypatch.setattr(ad, "generate_and_translate", lambda *a, **k: _FakeGen(0.0))
    monkeypatch.setattr(sys, "argv", [
        "autodiscover.py", "--n", "3", "--fast-only",
        "--cost-ceiling", "10.0", "--summary", str(summary),
    ])
    assert ad.main() == 1
    assert summary.exists(), "summary must survive the non-zero exit"


def test_failure_line_is_printed_for_the_log(ad, monkeypatch, tmp_path, capsys):
    """The exit code alerts; the log line has to explain. Whoever opens
    autodiscover.log after a FAILED timer needs the reason in the file."""
    _run(ad, monkeypatch, tmp_path, n=4, cost_per_candidate=0.0)
    out = capsys.readouterr().out
    assert "FAIL reason=no_generation_no_spend" in out
    assert "outage" in out
