"""Tests for autodiscover's terminal DONE line, health canaries, and the
per-attempt GEN-FAIL text.

WHY THIS FILE EXISTS: 2026-08-19 → 08-21 the nightly run produced zero usable
candidates three nights running (the model began stringifying `position_sizing`;
see test_spec_recovery_fields.py) and reported:

    CAND 0..10  GEN-FAIL all 3 attempts failed      <- no reason, 33x/night
    DONE reason=cost_ceiling spent=$0.6431
    DONE reason=batch_exhausted n=11 hits=0 ...     <- false: stopped at 11 of 20
    exit 0                                         <- timer green

Three separate observability failures, one per test class below.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _ROOT / "scripts" / "autodiscover.py"


@pytest.fixture(scope="module")
def ad():
    spec = importlib.util.spec_from_file_location("autodiscover_script", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeLog:
    def __init__(self, cost: float):
        self.actual_cost_usd = cost


class _FakeGen:
    """A generation that failed, carrying a realistic multi-attempt reason."""
    def __init__(self, cost: float, reason: str | None = None):
        self.spec = None
        self.logs = [_FakeLog(cost)] if cost else []
        self.failure_reason = reason or (
            "all 3 attempts failed:\n"
            "  attempt 1: StrategySpec validation: position_sizing Input should be "
            "a valid dictionary or instance of PositionSizing\n"
            "  attempt 2: StrategySpec validation: position_sizing Input should be "
            "a valid dictionary or instance of PositionSizing\n"
            "  attempt 3: StrategySpec validation: position_sizing Input should be "
            "a valid dictionary or instance of PositionSizing"
        )


class _FakeConn:
    def close(self):
        pass


def _run(ad, monkeypatch, tmp_path, *, argv_extra: list[str],
         cost_per_candidate: float = 0.02) -> tuple[int, Path]:
    summary = tmp_path / "summary.json"
    monkeypatch.setattr(ad, "initialize_db", lambda *a, **k: _FakeConn())
    monkeypatch.setattr(ad, "load_symbol_list", lambda *a, **k: ["SPY"])
    monkeypatch.setattr(ad, "generate_and_translate",
                        lambda *a, **k: _FakeGen(cost_per_candidate))
    monkeypatch.setattr(sys, "argv", [
        "autodiscover.py", "--fast-only", "--summary", str(summary), *argv_extra,
    ])
    return ad.main(), summary


# ── Item 3: exactly one DONE line, carrying the real stop reason ─────────────


class TestTerminalDoneLine:
    def test_cost_ceiling_stop_prints_one_done_with_the_real_reason(
            self, ad, monkeypatch, tmp_path, capsys):
        """The 08-21 bug: the run stopped on budget at candidate 11 of 20 but the
        LAST DONE line — the one a human or a grep reads — said
        batch_exhausted."""
        _run(ad, monkeypatch, tmp_path,
             argv_extra=["--n", "20", "--cost-ceiling", "0.10"])
        done = [l for l in capsys.readouterr().out.splitlines() if l.startswith("DONE ")]
        assert len(done) == 1, f"expected exactly one DONE line, got {done}"
        assert "reason=cost_ceiling" in done[0]
        assert "batch_exhausted" not in done[0]

    def test_full_batch_reports_batch_exhausted(self, ad, monkeypatch, tmp_path, capsys):
        """The control: when every candidate really did run, the reason is
        batch_exhausted and must NOT be reported as a budget stop."""
        _run(ad, monkeypatch, tmp_path,
             argv_extra=["--n", "3", "--cost-ceiling", "100.0"])
        done = [l for l in capsys.readouterr().out.splitlines() if l.startswith("DONE ")]
        assert len(done) == 1
        assert "reason=batch_exhausted" in done[0]

    def test_done_line_carries_the_usable_count(self, ad, monkeypatch, tmp_path, capsys):
        """`usable=` is the number that distinguishes a bad batch from a broken
        one, so it belongs on the line that summarises the run."""
        _run(ad, monkeypatch, tmp_path,
             argv_extra=["--n", "3", "--cost-ceiling", "100.0"])
        done = [l for l in capsys.readouterr().out.splitlines() if l.startswith("DONE ")]
        assert "usable=0" in done[0]


# ── Item 4: canaries ─────────────────────────────────────────────────────────


class TestHealthCanaries:
    def test_low_yield_and_short_run_both_warn_on_a_nightly_batch(
            self, ad, monkeypatch, tmp_path, capsys):
        _run(ad, monkeypatch, tmp_path,
             argv_extra=["--n", "20", "--cost-ceiling", "100.0"])
        out = capsys.readouterr().out
        assert "WARN low_yield:" in out
        assert "WARN short_run:" in out

    def test_canaries_stay_silent_on_a_small_spot_check(
            self, ad, monkeypatch, tmp_path, capsys):
        """A `--n 3` manual run is expected to be small and fast. If the canaries
        fired here they would be noise, and noise gets ignored — which is how the
        original failure survived three nights."""
        _run(ad, monkeypatch, tmp_path,
             argv_extra=["--n", "3", "--cost-ceiling", "100.0"])
        out = capsys.readouterr().out
        assert "WARN low_yield:" not in out
        assert "WARN short_run:" not in out

    def test_warnings_are_persisted_to_the_summary(self, ad, monkeypatch, tmp_path):
        """The dated summary is the artifact a morning health check reads. Before
        this, failure was only inferable from the file being unusually small."""
        _, summary = _run(ad, monkeypatch, tmp_path,
                          argv_extra=["--n", "20", "--cost-ceiling", "100.0"])
        data = json.loads(summary.read_text())
        assert data["usable_candidates"] == 0
        assert "elapsed_minutes" in data
        assert any(w.startswith("low_yield:") for w in data["warnings"])

    def test_default_exit_code_is_unchanged_by_the_canaries(
            self, ad, monkeypatch, tmp_path):
        """The canaries WARN by default. The exit-code contract documented in
        test_autodiscover_exit_code.py (paid-for batch that finds nothing == 0)
        is deliberately left alone unless --fail-on-low-yield is set."""
        rc, _ = _run(ad, monkeypatch, tmp_path,
                     argv_extra=["--n", "20", "--cost-ceiling", "100.0"])
        assert rc == 0

    def test_fail_on_low_yield_opts_into_a_red_timer(self, ad, monkeypatch, tmp_path, capsys):
        rc, _ = _run(ad, monkeypatch, tmp_path,
                     argv_extra=["--n", "20", "--cost-ceiling", "100.0",
                                 "--fail-on-low-yield"])
        assert rc == 1
        assert "FAIL reason=low_yield_canary" in capsys.readouterr().out

    def test_fail_on_low_yield_is_quiet_on_a_healthy_run(self, ad, monkeypatch, tmp_path):
        """The flag must not turn every run red — with no canary warnings it has
        no effect."""
        rc, _ = _run(ad, monkeypatch, tmp_path,
                     argv_extra=["--n", "3", "--cost-ceiling", "100.0",
                                 "--fail-on-low-yield"])
        assert rc == 0


# ── Items 2 & 6: the GEN-FAIL line explains itself ───────────────────────────


class TestGenFailText:
    def test_per_attempt_reasons_reach_the_log(self, ad, monkeypatch, tmp_path, capsys):
        _run(ad, monkeypatch, tmp_path,
             argv_extra=["--n", "2", "--cost-ceiling", "100.0"])
        out = capsys.readouterr().out
        assert "GEN-FAIL all 3 attempts failed:" in out
        assert "attempt 1:" in out and "attempt 3:" in out
        assert "position_sizing" in out, "the reason must be in the log, not only in results/"

    def test_reason_is_persisted_to_the_summary(self, ad, monkeypatch, tmp_path):
        _, summary = _run(ad, monkeypatch, tmp_path,
                          argv_extra=["--n", "2", "--cost-ceiling", "100.0"])
        data = json.loads(summary.read_text())
        assert "position_sizing" in data["candidates"][0]["failed"]


class TestFailureReasonFormatter:
    """Unit-level checks on the formatter itself."""

    def test_legacy_prefix_is_preserved(self):
        """Existing greps and test_autodiscover_exit_code.py match on this exact
        string; the reasons are appended after it, not substituted for it."""
        from generator.pipeline import _format_failure_reason
        assert _format_failure_reason(3, []).startswith("all 3 attempts failed")
        assert _format_failure_reason(3, ["boom"]).startswith("all 3 attempts failed:")

    def test_attempt_numbers_are_not_duplicated(self):
        from generator.pipeline import _format_failure_reason
        out = _format_failure_reason(2, ["Attempt 1 translator rejected: nope",
                                         "Attempt 2 failed: also nope"])
        assert "attempt 1: translator rejected: nope" in out
        assert "Attempt 1" not in out
        assert "attempt 2: also nope" in out, "the bare 'failed: ' prefix is stripped too"

    def test_multiline_pydantic_errors_are_flattened(self):
        """One reason per line, so `grep -A3 GEN-FAIL` shows all three attempts
        rather than one attempt's stack."""
        from generator.pipeline import _format_failure_reason
        out = _format_failure_reason(1, ["Attempt 1 failed: line one\n  line two\n  line three"])
        assert len(out.splitlines()) == 2
        assert "line one line two line three" in out

    def test_long_reasons_are_truncated(self):
        from generator.pipeline import _format_failure_reason
        out = _format_failure_reason(1, ["Attempt 1 failed: " + "x" * 5000])
        assert len(out.splitlines()[1]) < 400
