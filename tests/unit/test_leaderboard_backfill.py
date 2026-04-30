"""Tests for leaderboard.backfill — idempotency, schema-drift tolerance,
and missing-strategy handling.

Each test builds a synthetic results/ tree under tmp_path so we don't
depend on the actual repo state. The synthetic generation logs use the
same shape as `_save_log` produces.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from leaderboard.backfill import (
    BackfillSummary,
    _snake_from_camel,
    backfill_all,
    backfill_evaluations,
    backfill_generations,
    recover_strategy_hash,
)
from leaderboard.db import initialize_db


# ── Fixtures ─────────────────────────────────────────────────────────────────


def _valid_spec_dict(name: str = "test_mr_strat") -> dict:
    """A minimal valid mean_reversion spec — uses 1d which is allowed by
    that archetype's allowed_timeframes."""
    return {
        "name": name,
        "archetype": "mean_reversion",
        "thesis": "Buy oversold dips in established uptrends; mean revert in 1-3 days.",
        "supported_assets": ["stocks"],
        "timeframes": ["1d"],
        "parameters": [
            {"name": "rsi_threshold", "type": "float", "default": 5.0,
             "range_min": 1.0, "range_max": 30.0},
        ],
        "indicators": [
            {"name": "rsi_2", "type": "rsi", "params": {"period": 2}},
            {"name": "sma_200", "type": "sma", "params": {"period": 200}},
        ],
        "entry_long": {
            "op": "and",
            "args": [
                {"op": "compare", "operator": ">",
                 "lhs": {"op": "price", "field": "close"},
                 "rhs": {"op": "indicator", "name": "sma_200"}},
                {"op": "compare", "operator": "<",
                 "lhs": {"op": "indicator", "name": "rsi_2"},
                 "rhs": {"op": "param", "name": "rsi_threshold"}},
            ],
        },
        "exit_long": {
            "op": "compare", "operator": ">",
            "lhs": {"op": "indicator", "name": "rsi_2"},
            "rhs": {"op": "const", "value": 70.0},
        },
        "position_sizing": {"rule": "fixed", "size": 1},
    }


def _gen_log(spec: dict | None, *, timestamp: str, archetype: str = "mean_reversion") -> dict:
    """Mirror of `_save_log`'s on-disk shape."""
    return {
        "timestamp": timestamp,
        "archetype": archetype,
        "model": "claude-sonnet-4-6",
        "prompt_hash": f"ph_{timestamp}",
        "system_prompt": "...",
        "user_prompt": "...",
        "raw_tool_input": spec,
        "spec": spec,
        "error": None if spec else "validation failed",
        "input_tokens": 5000,
        "output_tokens": 500,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "actual_cost_usd": 0.0225,
        "call_id": f"call_{timestamp}",
        "attempt": 1,
    }


def _write_gen_log(gen_dir: Path, log: dict) -> Path:
    """Write a generation log using `_save_log`'s filename pattern so the
    eval-side lookup (which depends on this pattern) works."""
    arch = log["archetype"]
    slug = log["spec"]["name"] if log["spec"] else "failed"
    safe_ts = log["timestamp"].replace(":", "-")
    p = gen_dir / f"{safe_ts}_{arch}_{slug}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(log, indent=2))
    return p


def _write_fast_eval(
    results_dir: Path, ts: str, strategy_class: str, *, payload_extra: dict | None = None
) -> Path:
    """Mirror `evaluation.fast_pipeline._write_fast_report` shape."""
    outer = results_dir / f"fast_eval_{ts}"
    inner = outer / strategy_class
    inner.mkdir(parents=True, exist_ok=True)
    payload = {
        "label": "FAST: NON-CANONICAL",
        "warning": "...",
        "is_fast": True,
        "strategy": strategy_class,
        "symbols": ["AMD", "NFLX", "SPY", "QQQ", "NVDA"],
        "median_pf": 0.0,
        "n_oos_trades_total": 0,
        "breakdown": {
            "median_pf": 0.0, "consistency_factor": 0.0,
            "parameter_penalty": 0.0, "significance_factor": 0.0,
            "score": 0.0,
        },
        "verdict": {
            "is_promising": False, "failed_conditions": [],
            "breakdown": {
                "median_pf": 0.0, "consistency_factor": 0.0,
                "parameter_penalty": 0.0, "significance_factor": 0.0,
                "score": 0.0,
            },
        },
        "config": {"strategy": strategy_class, "symbols": []},
    }
    if payload_extra:
        payload.update(payload_extra)
    (inner / "fast_summary.json").write_text(json.dumps(payload, indent=2))
    return inner


@pytest.fixture
def conn(tmp_path):
    c = initialize_db(tmp_path / "lb.db")
    yield c
    c.close()


# ── Helpers ──────────────────────────────────────────────────────────────────


def test_snake_from_camel_round_trips_translator_convention():
    # The translator emits CamelCase from snake_case via:
    #   "".join(p.capitalize() for p in name.split("_"))
    # _snake_from_camel reverses that.
    assert _snake_from_camel("ZscoreBbReversion") == "zscore_bb_reversion"
    assert _snake_from_camel("LastHourMomentumSeasonality") == "last_hour_momentum_seasonality"
    assert _snake_from_camel("Casper") == "casper"


def test_recover_strategy_hash_returns_hash_for_valid_spec():
    bh, err = recover_strategy_hash(_valid_spec_dict(), archetype="mean_reversion")
    assert err is None
    assert bh is not None
    assert len(bh) == 64  # sha256 hex


def test_recover_strategy_hash_rejects_invalid_archetype_timeframe():
    """mean_reversion + 5m is unsatisfiable per current archetype rules
    (5m only allowed for microstructure). Hash recovery must fail with a
    translator_validate reason — the same drift class we observed in the
    audit on real seasonality + 5m logs."""
    spec = _valid_spec_dict()
    spec["timeframes"] = ["5m"]
    bh, err = recover_strategy_hash(spec, archetype="mean_reversion")
    assert bh is None
    assert err is not None
    assert err.startswith("translator_validate")


def test_recover_strategy_hash_rejects_malformed_spec():
    bh, err = recover_strategy_hash({"clearly": "not a spec"})
    assert bh is None
    assert err is not None
    assert err.startswith("spec_validation")


# ── backfill_generations ─────────────────────────────────────────────────────


def test_backfill_generations_imports_one_strategy_one_generation(conn, tmp_path):
    gen_dir = tmp_path / "results" / "generations"
    _write_gen_log(gen_dir, _gen_log(_valid_spec_dict(), timestamp="2026-04-30T10:00:00+00:00"))

    summary = BackfillSummary()
    backfill_generations(conn, tmp_path / "results", summary)

    assert summary.imported_strategies == 1
    assert summary.imported_generations == 1
    assert summary.skipped_generations == []

    rows = conn.execute(
        "SELECT strategy_hash, name, imported_from FROM strategies"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["imported_from"] == "backfill"
    assert rows[0]["name"] == "test_mr_strat"


def test_backfill_generations_skips_failed_logs(conn, tmp_path):
    gen_dir = tmp_path / "results" / "generations"
    # 2 successful, 3 failed.
    _write_gen_log(gen_dir, _gen_log(_valid_spec_dict("a"), timestamp="2026-04-30T10:00:00+00:00"))
    _write_gen_log(gen_dir, _gen_log(_valid_spec_dict("b"), timestamp="2026-04-30T10:01:00+00:00"))
    for i in range(3):
        _write_gen_log(gen_dir, _gen_log(None, timestamp=f"2026-04-30T10:0{i+2}:00+00:00"))

    summary = BackfillSummary()
    backfill_generations(conn, tmp_path / "results", summary)

    assert summary.imported_generations == 2
    # 3 failed logs in skip list with reason 'spec_is_none'.
    spec_none_skips = [s for s in summary.skipped_generations if s[1] == "spec_is_none"]
    assert len(spec_none_skips) == 3


def test_backfill_generations_skips_unrecoverable_specs(conn, tmp_path):
    """Specs that pass JSON parse but fail the current pipeline (archetype-
    timeframe drift, stale kwargs) are skipped with a reason matching the
    audit's failure-class taxonomy."""
    gen_dir = tmp_path / "results" / "generations"
    bad_spec = _valid_spec_dict()
    bad_spec["timeframes"] = ["5m"]  # mean_reversion doesn't allow 5m
    _write_gen_log(gen_dir, _gen_log(bad_spec, timestamp="2026-04-30T10:00:00+00:00"))

    summary = BackfillSummary()
    backfill_generations(conn, tmp_path / "results", summary)

    assert summary.imported_generations == 0
    assert len(summary.skipped_generations) == 1
    _, reason = summary.skipped_generations[0]
    assert reason.startswith("translator_validate")


def test_backfill_generations_is_idempotent(conn, tmp_path):
    """Running twice over the same on-disk state must produce identical row
    counts. Idempotency is enforced by SELECT-before-INSERT on the natural
    key (strategy_hash, generated_at, prompt_hash) since the schema lacks a
    UNIQUE constraint there."""
    gen_dir = tmp_path / "results" / "generations"
    _write_gen_log(gen_dir, _gen_log(_valid_spec_dict("a"), timestamp="2026-04-30T10:00:00+00:00"))
    _write_gen_log(gen_dir, _gen_log(_valid_spec_dict("b"), timestamp="2026-04-30T10:01:00+00:00"))

    s1 = BackfillSummary()
    backfill_generations(conn, tmp_path / "results", s1)

    s2 = BackfillSummary()
    backfill_generations(conn, tmp_path / "results", s2)

    # First run imported them; second run should skip both as duplicates.
    assert s1.imported_generations == 2
    assert s2.imported_generations == 0
    assert all(reason == "duplicate" for _, reason in s2.skipped_generations)

    # Total rows in DB stay at the first-run count.
    n = conn.execute("SELECT COUNT(*) AS n FROM generations").fetchone()["n"]
    assert n == 2


def test_backfill_generations_handles_malformed_json(conn, tmp_path):
    gen_dir = tmp_path / "results" / "generations"
    gen_dir.mkdir(parents=True)
    # 1 valid + 1 garbage file.
    _write_gen_log(gen_dir, _gen_log(_valid_spec_dict(), timestamp="2026-04-30T10:00:00+00:00"))
    (gen_dir / "garbage.json").write_text("{ this is not valid json")

    summary = BackfillSummary()
    backfill_generations(conn, tmp_path / "results", summary)

    assert summary.imported_generations == 1  # the valid one
    parse_errors = [s for s in summary.skipped_generations if s[1].startswith("json_parse")]
    assert len(parse_errors) == 1


# ── backfill_evaluations ─────────────────────────────────────────────────────


def test_backfill_evaluations_links_to_strategy_via_class_name(conn, tmp_path):
    gen_dir = tmp_path / "results" / "generations"
    _write_gen_log(
        gen_dir,
        _gen_log(_valid_spec_dict("test_mr_strat"), timestamp="2026-04-30T10:00:00+00:00"),
    )
    # Fast eval for "TestMrStrat" (CamelCase of test_mr_strat).
    _write_fast_eval(tmp_path / "results", "20260430_120000", "TestMrStrat")

    summary = BackfillSummary()
    backfill_generations(conn, tmp_path / "results", summary)
    backfill_evaluations(conn, tmp_path / "results", summary)

    assert summary.imported_evaluations == 1
    rows = conn.execute(
        "SELECT eval_type, imported_from, evaluated_at FROM evaluations"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["eval_type"] == "fast"
    assert rows[0]["imported_from"] == "backfill"
    # Timestamp parsed from dirname.
    assert rows[0]["evaluated_at"].startswith("2026-04-30T12:00:00")


def test_backfill_evaluations_skips_when_no_matching_generation_log(conn, tmp_path):
    """An eval whose CamelCase strategy name doesn't have a matching
    generation log (e.g. CasperStrategy from a manual run) is skipped —
    the leaderboard's strategies table is keyed by strategy_hash and
    we have no way to derive that without the spec."""
    (tmp_path / "results" / "generations").mkdir(parents=True)  # empty dir
    _write_fast_eval(tmp_path / "results", "20260430_120000", "MysteryStrat")

    summary = BackfillSummary()
    backfill_evaluations(conn, tmp_path / "results", summary)

    assert summary.imported_evaluations == 0
    assert len(summary.skipped_evaluations) == 1
    _, reason = summary.skipped_evaluations[0]
    assert "no_generation_log_for" in reason or "no_recoverable_hash_in_logs" in reason


def test_backfill_evaluations_handles_old_fast_summary_shape(conn, tmp_path):
    """Older fast_summary.json files (per the audit, 6 of 35 dirs) lack
    the `diagnostics` key. Backfill must use .get('diagnostics', None)
    via the duck-typed pipeline_result it builds — verify it handles the
    older shape without raising."""
    gen_dir = tmp_path / "results" / "generations"
    _write_gen_log(
        gen_dir,
        _gen_log(_valid_spec_dict("test_mr_strat"), timestamp="2026-04-30T10:00:00+00:00"),
    )
    # Write a fast_summary that omits 'diagnostics' (older shape).
    inner = _write_fast_eval(tmp_path / "results", "20260430_120000", "TestMrStrat")
    payload = json.loads((inner / "fast_summary.json").read_text())
    payload.pop("diagnostics", None)  # ensure it's not there
    (inner / "fast_summary.json").write_text(json.dumps(payload))

    summary = BackfillSummary()
    backfill_generations(conn, tmp_path / "results", summary)
    backfill_evaluations(conn, tmp_path / "results", summary)
    assert summary.imported_evaluations == 1


def test_backfill_evaluations_is_idempotent(conn, tmp_path):
    gen_dir = tmp_path / "results" / "generations"
    _write_gen_log(
        gen_dir,
        _gen_log(_valid_spec_dict("test_mr_strat"), timestamp="2026-04-30T10:00:00+00:00"),
    )
    _write_fast_eval(tmp_path / "results", "20260430_120000", "TestMrStrat")

    s1 = BackfillSummary()
    backfill_generations(conn, tmp_path / "results", s1)
    backfill_evaluations(conn, tmp_path / "results", s1)

    s2 = BackfillSummary()
    backfill_generations(conn, tmp_path / "results", s2)
    backfill_evaluations(conn, tmp_path / "results", s2)

    assert s1.imported_evaluations == 1
    assert s2.imported_evaluations == 0
    assert any(r == "duplicate" for _, r in s2.skipped_evaluations)

    n = conn.execute("SELECT COUNT(*) AS n FROM evaluations").fetchone()["n"]
    assert n == 1


def test_backfill_evaluations_handles_malformed_summary_json(conn, tmp_path):
    gen_dir = tmp_path / "results" / "generations"
    _write_gen_log(
        gen_dir,
        _gen_log(_valid_spec_dict("test_mr_strat"), timestamp="2026-04-30T10:00:00+00:00"),
    )
    # Valid eval dir.
    _write_fast_eval(tmp_path / "results", "20260430_120000", "TestMrStrat")
    # Malformed eval dir.
    bad_outer = tmp_path / "results" / "fast_eval_20260430_130000" / "BadStrat"
    bad_outer.mkdir(parents=True)
    (bad_outer / "fast_summary.json").write_text("{ malformed")

    summary = BackfillSummary()
    backfill_generations(conn, tmp_path / "results", summary)
    backfill_evaluations(conn, tmp_path / "results", summary)

    assert summary.imported_evaluations == 1
    parse_errors = [s for s in summary.skipped_evaluations if s[1].startswith("json_parse")]
    assert len(parse_errors) == 1


# ── backfill_all + summary log ───────────────────────────────────────────────


def test_backfill_all_writes_log_and_returns_summary(conn, tmp_path):
    gen_dir = tmp_path / "results" / "generations"
    _write_gen_log(
        gen_dir,
        _gen_log(_valid_spec_dict("test_mr_strat"), timestamp="2026-04-30T10:00:00+00:00"),
    )
    _write_fast_eval(tmp_path / "results", "20260430_120000", "TestMrStrat")

    summary = backfill_all(conn, tmp_path / "results")

    assert summary.imported_strategies == 1
    assert summary.imported_generations == 1
    assert summary.imported_evaluations == 1
    assert summary.log_path is not None
    assert summary.log_path.exists()

    log_text = summary.log_path.read_text()
    assert "imported_strategies:  1" in log_text
    assert "imported_generations: 1" in log_text
    assert "imported_evaluations: 1" in log_text


def test_backfill_summary_render_lists_skip_reasons(tmp_path):
    s = BackfillSummary(
        imported_strategies=2,
        imported_generations=5,
        imported_evaluations=3,
        skipped_generations=[(f"/path/to/log_{i}.json", f"reason_{i}") for i in range(15)],
        skipped_evaluations=[("/path/to/eval", "duplicate")],
        log_path=tmp_path / "backfill.log",
    )
    rendered = s.render()
    assert "imported_strategies   : 2" in rendered
    # render shows first 10 skip reasons.
    assert "log_2.json: reason_2" in rendered
    assert "log_9.json: reason_9" in rendered
    assert "log_10.json: reason_10" not in rendered  # truncated
    assert str(s.log_path) in rendered
