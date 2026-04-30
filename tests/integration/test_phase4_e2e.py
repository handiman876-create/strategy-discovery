"""End-to-end integration tests for the Phase 4 leaderboard.

Two tests live here:

* `test_e2e_real_generate_eval_record_query`
    Marked `@pytest.mark.integration`. Hits the real Anthropic API and
    runs the real fast-eval backtest pipeline. Cost ~$0.02-0.05 +
    30-60s. Skipped if `ANTHROPIC_API_KEY` is unset. Default `pytest`
    deselects this test (see pyproject.toml addopts); opt in with
    `pytest -m integration`.

* `test_backfill_all_idempotency_with_generations_and_evals`
    Unmarked. Synthetic results/ tree, no API. Verifies that
    `backfill_all` is idempotent over both generations and evaluations
    as a composition. The unit tests in
    tests/unit/test_leaderboard_backfill.py cover the building blocks
    separately; this one verifies the full pipeline. Runs by default.

Both tests use a tmp_path-scoped DB and the `real_db_unchanged` fixture
to fail loudly if a future refactor accidentally writes to the live
db/leaderboard.db.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from pathlib import Path

import pytest

from engine.backtester import BacktestConfig
from engine.session import RegularTradingHours
from evaluation.fast_pipeline import run_fast_evaluation
from evaluation.walkforward import WalkForwardConfig
from generator.pipeline import generate_and_translate
from leaderboard.backfill import backfill_all
from leaderboard.db import DEFAULT_DB_PATH, initialize_db


# ── real-DB scope-check fixture ──────────────────────────────────────────────


@pytest.fixture
def real_db_unchanged():
    """Capture row counts of the live db/leaderboard.db before yielding;
    re-check after. Fails the test if anything under this fixture
    accidentally wrote to the real DB instead of its tmp_path-scoped one.

    If db/leaderboard.db doesn't exist (or hasn't had migrations applied),
    pre/post counts are zeros and the assertion passes trivially. The
    fixture's job is to catch a test-scoping regression, not to require a
    populated production DB."""

    def _counts(path: Path) -> dict[str, int]:
        if not path.exists():
            return {"strategies": 0, "generations": 0, "evaluations": 0}
        c = sqlite3.connect(str(path))
        try:
            return {
                "strategies": c.execute("SELECT COUNT(*) FROM strategies").fetchone()[0],
                "generations": c.execute("SELECT COUNT(*) FROM generations").fetchone()[0],
                "evaluations": c.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0],
            }
        except sqlite3.OperationalError:
            # DB exists but doesn't have the expected schema (e.g. partial
            # migration). Treat as zeros — the post-test re-check will
            # surface any actual writes via the same path.
            return {"strategies": 0, "generations": 0, "evaluations": 0}
        finally:
            c.close()

    before = _counts(DEFAULT_DB_PATH)
    yield
    after = _counts(DEFAULT_DB_PATH)
    assert before == after, (
        f"Real db/leaderboard.db row counts changed during this test! "
        f"before={before} after={after}. A test under the "
        f"`real_db_unchanged` fixture wrote to the real DB instead of "
        f"its tmp_path scope. Check that conn=initialize_db(tmp_path / "
        f"'lb.db') is being threaded into every record_* / backfill_* "
        f"call site within the test body."
    )


# ── helper: import a generated strategy class ───────────────────────────────


def _import_generated(snake_name: str, code_path: Path):
    """Mirror of scripts/discover.py:127-132 — load the generated module
    and return the Strategy class. Future drift between the two becomes
    findable via grep on `_import_generated`; if you change one, search
    for the other and reconcile."""
    spec_mod = importlib.util.spec_from_file_location(snake_name, code_path)
    mod = importlib.util.module_from_spec(spec_mod)
    spec_mod.loader.exec_module(mod)
    class_name = "".join(p.capitalize() for p in snake_name.split("_"))
    return getattr(mod, class_name)


# ── synthetic-backfill helpers ───────────────────────────────────────────────


def _synthetic_spec_dict(name: str, archetype: str = "mean_reversion") -> dict:
    return {
        "name": name,
        "archetype": archetype,
        "thesis": "Buy oversold dips in established uptrends; mean revert in 1-3 days.",
        "supported_assets": ["stocks"],
        "timeframes": ["1d"],
        "parameters": [
            {"name": "rsi_threshold", "type": "float", "default": 5.0,
             "range_min": 1.0, "range_max": 30.0, "description": ""},
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


def _write_synthetic_generation(gen_dir: Path, spec_name: str, timestamp: str):
    gen_dir.mkdir(parents=True, exist_ok=True)
    spec = _synthetic_spec_dict(spec_name)
    payload = {
        "timestamp": timestamp,
        "archetype": spec["archetype"],
        "model": "claude-sonnet-4-6",
        "prompt_hash": f"ph_{timestamp}",
        "system_prompt": "...",
        "user_prompt": "...",
        "raw_tool_input": spec,
        "spec": spec,
        "error": None,
        "input_tokens": 5000,
        "output_tokens": 500,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "actual_cost_usd": 0.0225,
        "call_id": f"call_{timestamp}",
        "attempt": 1,
    }
    safe_ts = timestamp.replace(":", "-")
    filename = f"{safe_ts}_{spec['archetype']}_{spec_name}.json"
    (gen_dir / filename).write_text(json.dumps(payload))


def _write_synthetic_fast_eval(results_dir: Path, ts: str, strategy_class: str):
    inner = results_dir / f"fast_eval_{ts}" / strategy_class
    inner.mkdir(parents=True, exist_ok=True)
    payload = {
        "label": "FAST: NON-CANONICAL",
        "warning": "...",
        "is_fast": True,
        "strategy": strategy_class,
        "symbols": ["AMD"],
        "median_pf": 0.0,
        "n_oos_trades_total": 0,
        "breakdown": {"median_pf": 0.0, "consistency_factor": 0.0,
                      "parameter_penalty": 0.0, "significance_factor": 0.0,
                      "score": 0.0},
        "verdict": {"is_promising": False, "failed_conditions": [],
                    "breakdown": {"median_pf": 0.0, "consistency_factor": 0.0,
                                  "parameter_penalty": 0.0,
                                  "significance_factor": 0.0, "score": 0.0}},
        "config": {"strategy": strategy_class, "symbols": []},
    }
    (inner / "fast_summary.json").write_text(json.dumps(payload))


# ── tests ────────────────────────────────────────────────────────────────────


def test_backfill_all_idempotency_with_generations_and_evals(
    tmp_path, real_db_unchanged
):
    """Composition-level idempotency for `backfill_all`: run twice on the
    same synthetic results/ tree and assert the second run imports zero
    new rows in any table.

    Complements the unit tests in tests/unit/test_leaderboard_backfill.py
    which cover backfill_generations and backfill_evaluations
    independently — this one verifies they compose correctly under a
    repeat invocation."""
    results_dir = tmp_path / "results"
    _write_synthetic_generation(
        results_dir / "generations",
        spec_name="test_strat",
        timestamp="2026-04-30T10:00:00+00:00",
    )
    _write_synthetic_fast_eval(results_dir, "20260430_120000", "TestStrat")

    db_path = tmp_path / "lb.db"

    conn = initialize_db(db_path)
    try:
        s1 = backfill_all(conn, results_dir)
    finally:
        conn.close()

    conn = initialize_db(db_path)
    try:
        s2 = backfill_all(conn, results_dir)
    finally:
        conn.close()

    assert s1.imported_strategies == 1
    assert s1.imported_generations == 1
    assert s1.imported_evaluations == 1
    assert s2.imported_strategies == 0
    assert s2.imported_generations == 0
    assert s2.imported_evaluations == 0


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)
def test_e2e_real_generate_eval_record_query(tmp_path, real_db_unchanged):
    """End-to-end with the real Anthropic API and the real fast-eval
    pipeline. Cost ~$0.02-0.05 + 30-60s.

    Programmatic equivalent of `discover.py --archetype mean_reversion
    --timeframe 1h --fast`, bypassing the argparse layer so we can scope
    the DB to tmp_path. (scripts/discover.py doesn't accept a --db flag,
    and adding one is scope creep for step 11; calling library functions
    directly mirrors what discover.py:_run does internally.)"""
    db_path = tmp_path / "lb.db"
    conn = initialize_db(db_path)
    try:
        # 1. Generate + translate (real API call).
        result = generate_and_translate(
            "mean_reversion",
            max_retries=3,
            dedup=True,
            conn=conn,
            requested_timeframe="1h",
        )
        assert result.spec is not None, (
            f"generation failed: {result.failure_reason}; "
            f"see results/generations/ for per-attempt logs"
        )
        assert result.strategy_hash is not None
        assert result.spec.timeframes == ["1h"]
        assert result.spec.archetype == "mean_reversion"

        # 2. Import the generated strategy class.
        StrategyClass = _import_generated(result.spec.name, result.code_path)

        # 3. Run fast evaluation (real backtest, 5 symbols).
        cfg = BacktestConfig(
            starting_capital=10_000,
            commission=0.0,
            slippage=0.01,
            realistic_fills=True,
            session=RegularTradingHours(),
        )
        wf = WalkForwardConfig(
            train_window_months=24,
            test_window_months=6,
            step_months=6,
            parameter_grid=None,
        )
        eval_result = run_fast_evaluation(
            StrategyClass,
            backtest_config=cfg,
            walk_config=wf,
            output_root=tmp_path / "results",
            conn=conn,
            strategy_hash=result.strategy_hash,
        )
        assert eval_result.is_fast is True

        # 4. Query the leaderboard and verify structure.

        # 4a. Exactly 1 strategies row, fields match.
        strategies = conn.execute(
            "SELECT * FROM strategies WHERE strategy_hash = ?",
            (result.strategy_hash,),
        ).fetchall()
        assert len(strategies) == 1, (
            f"Expected 1 strategies row for hash {result.strategy_hash}, "
            f"got {len(strategies)}"
        )
        s = strategies[0]
        assert s["archetype"] == "mean_reversion"
        assert s["timeframe"] == "1h"
        assert s["name"] == result.spec.name
        assert s["status"] == "fast_evaluated", (
            f"expected status='fast_evaluated' (auto-promoted by "
            f"record_evaluation), got {s['status']!r}"
        )
        assert s["imported_from"] is None  # not a backfill row

        # 4b. ≥1 generations rows linked via FK.
        gens = conn.execute(
            "SELECT * FROM generations WHERE strategy_hash = ?",
            (result.strategy_hash,),
        ).fetchall()
        assert len(gens) >= 1, (
            f"Expected ≥1 generation row for hash {result.strategy_hash}, "
            f"got {len(gens)}"
        )
        g = gens[0]
        assert g["archetype"] == "mean_reversion"
        assert g["requested_timeframe"] == "1h"
        assert g["model_version"]  # non-empty

        # 4c. Exactly 1 evaluations row, well-typed.
        evals = conn.execute(
            "SELECT * FROM evaluations WHERE strategy_hash = ?",
            (result.strategy_hash,),
        ).fetchall()
        assert len(evals) == 1, (
            f"Expected 1 evaluations row for hash {result.strategy_hash}, "
            f"got {len(evals)}"
        )
        e = evals[0]
        assert e["eval_type"] == "fast"
        # n_oos_trades: non-negative int.
        assert isinstance(e["n_oos_trades"], int)
        assert e["n_oos_trades"] >= 0
        # median_pf: float — the live record_evaluation hook always populates
        # it from breakdown.median_pf (which is 0.0 for zero-trade specs);
        # only backfill emits NULL there.
        assert isinstance(e["median_pf"], float)
        # score: float.
        assert isinstance(e["score"], float)
        # promising: stored as INTEGER 0/1; sqlite3.Row returns int.
        assert e["promising"] in (0, 1)
        # failed_gates: NULL when no failures, else parses as JSON list of
        # FailedCondition-shaped dicts.
        if e["failed_gates"]:
            decoded = json.loads(e["failed_gates"])
            assert isinstance(decoded, list)
            for cond in decoded:
                assert {"name", "required", "actual", "deficit"}.issubset(cond.keys())
        # results_dir: non-empty (fast_pipeline writes output_dir before recording).
        assert e["results_dir"]
    finally:
        conn.close()
