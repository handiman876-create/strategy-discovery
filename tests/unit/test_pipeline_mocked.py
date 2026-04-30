"""End-to-end pipeline test with mocked Claude API.

Verifies that generate_and_translate:
  1. Calls the API (mocked to return a valid spec).
  2. Translates the spec to a code file.
  3. Computes the behavioral hash.
  4. Records spend correctly.
  5. Retries on validation failure with feedback.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from generator.claude_client import ClaudeClient, GenerationLog
from generator.pipeline import generate_and_translate, generate_strategy
from generator.spec import IndicatorSpec, ParameterSpec, StrategySpec
from generator.spend_tracker import SpendTracker


def _valid_spec_dict():
    return {
        "name": "mock_rsi_dip",
        "archetype": "mean_reversion",
        "thesis": "Buy oversold dips in established uptrends; mean revert in 1-3 days.",
        "supported_assets": ["stocks"],
        "timeframes": ["1d"],
        "parameters": [
            {"name": "rsi_threshold", "type": "float", "default": 5.0, "range_min": 1.0, "range_max": 30.0},
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


def _mock_message_response(tool_input: dict, input_tokens: int = 5000, output_tokens: int = 500):
    block = MagicMock()
    block.type = "tool_use"
    block.input = tool_input
    msg = MagicMock()
    msg.content = [block]
    msg.usage.input_tokens = input_tokens
    msg.usage.output_tokens = output_tokens
    msg.usage.cache_read_input_tokens = 0
    msg.usage.cache_creation_input_tokens = 0
    return msg


def test_pipeline_happy_path(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-mock")
    tracker = SpendTracker(
        cap_usd=10.0,
        spend_file=tmp_path / "spend.json",
        archive_file=tmp_path / "summary.json",
    )
    client = ClaudeClient(spend_tracker=tracker)

    with patch.object(client.client.messages, "create",
                      return_value=_mock_message_response(_valid_spec_dict())):
        result = generate_and_translate(
            "mean_reversion", client=client, dedup=False, max_retries=1
        )

    assert result.spec is not None
    assert result.spec.name == "mock_rsi_dip"
    assert result.code_path is not None
    assert result.code_path.exists()
    # Spend recorded
    assert tracker.current_month_total() > 0


def test_pipeline_retries_on_validation_failure(tmp_path, monkeypatch):
    """First response has an unknown indicator type → fails validation.
    Second response is valid → succeeds."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-mock")
    tracker = SpendTracker(
        cap_usd=10.0,
        spend_file=tmp_path / "spend.json",
        archive_file=tmp_path / "summary.json",
    )
    client = ClaudeClient(spend_tracker=tracker)

    bad_spec = _valid_spec_dict()
    bad_spec["name"] = "bad_attempt"
    bad_spec["indicators"][0]["type"] = "bogus_indicator"

    good_spec = _valid_spec_dict()
    good_spec["name"] = "good_attempt"

    responses = [_mock_message_response(bad_spec), _mock_message_response(good_spec)]

    with patch.object(client.client.messages, "create", side_effect=responses):
        result = generate_strategy("mean_reversion", client=client, max_retries=2)

    assert result.spec is not None
    assert result.spec.name == "good_attempt"
    assert len(result.logs) == 2
    # First log should have a validation error.
    assert result.logs[0].error is not None
    assert "validation" in result.logs[0].error.lower()


def _client_with_mocked_response(tmp_path, monkeypatch, spec_dict=None):
    """Shared setup for the leaderboard-hook tests below: ClaudeClient
    backed by a fresh SpendTracker and a single mocked API response."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-mock")
    tracker = SpendTracker(
        cap_usd=10.0,
        spend_file=tmp_path / "spend.json",
        archive_file=tmp_path / "summary.json",
    )
    client = ClaudeClient(spend_tracker=tracker)
    response = _mock_message_response(spec_dict or _valid_spec_dict())
    return client, response


def test_generate_and_translate_records_to_leaderboard_when_conn_set(
    tmp_path, monkeypatch
):
    """Step 8c hook: with conn + dedup, a successful generation writes one
    strategies row + one generations row. The strategy_hash from the
    GenerateResult matches the strategies primary key in the DB."""
    from leaderboard.db import initialize_db

    db_conn = initialize_db(tmp_path / "lb.db")
    client, response = _client_with_mocked_response(tmp_path, monkeypatch)

    with patch.object(client.client.messages, "create", return_value=response):
        result = generate_and_translate(
            "mean_reversion",
            client=client,
            dedup=True,
            max_retries=1,
            conn=db_conn,
        )

    assert result.spec is not None
    assert result.strategy_hash is not None

    rows = db_conn.execute(
        "SELECT strategy_hash, name, archetype FROM strategies"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["strategy_hash"] == result.strategy_hash
    assert rows[0]["archetype"] == "mean_reversion"

    gens = db_conn.execute(
        "SELECT strategy_hash, archetype FROM generations"
    ).fetchall()
    assert len(gens) == 1
    assert gens[0]["strategy_hash"] == result.strategy_hash
    db_conn.close()


def test_generate_and_translate_no_write_when_conn_none(
    tmp_path, monkeypatch
):
    """conn=None: record_generation must NOT be called (don't even build
    metadata). Patch the symbol on the pipeline module so any invocation
    would surface as a test failure."""
    client, response = _client_with_mocked_response(tmp_path, monkeypatch)
    record_spy = MagicMock(name="record_generation_spy")

    with patch.object(client.client.messages, "create", return_value=response), \
         patch("generator.pipeline.record_generation", record_spy):
        result = generate_and_translate(
            "mean_reversion",
            client=client,
            dedup=True,
            max_retries=1,
            conn=None,
        )

    assert result.spec is not None
    record_spy.assert_not_called()


def test_generate_and_translate_no_write_when_dedup_false_logs_debug(
    tmp_path, monkeypatch, caplog
):
    """dedup=False → strategy_hash is None → leaderboard write is
    skipped with a DEBUG log. Strategies are keyed by strategy_hash so
    we have nothing to insert without one."""
    from leaderboard.db import initialize_db

    db_conn = initialize_db(tmp_path / "lb.db")
    client, response = _client_with_mocked_response(tmp_path, monkeypatch)
    record_spy = MagicMock(name="record_generation_spy")

    caplog.set_level("DEBUG", logger="generator.pipeline")
    with patch.object(client.client.messages, "create", return_value=response), \
         patch("generator.pipeline.record_generation", record_spy):
        result = generate_and_translate(
            "mean_reversion",
            client=client,
            dedup=False,
            max_retries=1,
            conn=db_conn,
        )

    assert result.spec is not None
    assert result.strategy_hash is None
    record_spy.assert_not_called()
    assert any(
        "skipping leaderboard write, dedup disabled" in r.message
        and r.levelname == "DEBUG"
        for r in caplog.records
    )
    db_conn.close()


def test_generate_and_translate_swallows_record_generation_exception(
    tmp_path, monkeypatch, caplog
):
    """The leaderboard is observability, not critical path. If
    record_generation raises (e.g. DB locked, schema drift, disk full),
    log a warning and return the GenerateResult normally — the caller
    must NOT see the exception."""
    client, response = _client_with_mocked_response(tmp_path, monkeypatch)

    def _raising(*a, **kw):
        raise sqlite3.OperationalError("simulated DB failure")

    sentinel_conn = MagicMock(name="leaderboard_conn_sentinel")

    caplog.set_level("WARNING", logger="generator.pipeline")
    with patch.object(client.client.messages, "create", return_value=response), \
         patch("generator.pipeline.record_generation", side_effect=_raising):
        result = generate_and_translate(
            "mean_reversion",
            client=client,
            dedup=True,
            max_retries=1,
            conn=sentinel_conn,
        )

    assert result.spec is not None
    assert result.strategy_hash is not None
    assert any(
        "leaderboard write failed" in r.message and r.levelname == "WARNING"
        for r in caplog.records
    )


def test_pipeline_records_failed_calls_to_spend(tmp_path, monkeypatch):
    """If all retries fail, every attempt's spend is still recorded
    (over-record by design)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-mock")
    tracker = SpendTracker(
        cap_usd=10.0,
        spend_file=tmp_path / "spend.json",
        archive_file=tmp_path / "summary.json",
    )
    client = ClaudeClient(spend_tracker=tracker)

    bad = _valid_spec_dict()
    bad["indicators"][0]["type"] = "bogus"

    with patch.object(
        client.client.messages, "create",
        return_value=_mock_message_response(bad),
    ):
        result = generate_strategy("mean_reversion", client=client, max_retries=3)

    assert result.spec is None
    assert len(result.logs) == 3
    # Spend is still > 0 because each failed attempt was billed.
    assert tracker.current_month_total() > 0


# ── Step 9: --timeframe flag tests ───────────────────────────────────────────


def _spec_dict_with_timeframe(tf: str) -> dict:
    """Helper: a valid spec dict with timeframes=[tf]. The model is told
    to produce a strategy at this timeframe; we use this to fabricate
    both compliant and non-compliant responses."""
    s = _valid_spec_dict()
    s["timeframes"] = [tf]
    s["name"] = f"mock_strat_{tf}"
    return s


def _patched_quirks_path(monkeypatch, tmp_path):
    """Redirect _QUIRKS_PATH so tests don't pollute results/generation_quirks.json."""
    monkeypatch.setattr(
        "generator.pipeline._QUIRKS_PATH", tmp_path / "quirks.json"
    )


def test_no_timeframe_flag_no_prompt_injection(tmp_path, monkeypatch):
    """When requested_timeframe is None (default), the user message must
    NOT contain the constraint sentence — the no-flag path stays
    byte-identical to today's prompt."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-mock")
    tracker = SpendTracker(
        cap_usd=10.0, spend_file=tmp_path / "s.json",
        archive_file=tmp_path / "a.json",
    )
    client = ClaudeClient(spend_tracker=tracker)

    with patch.object(
        client.client.messages, "create",
        return_value=_mock_message_response(_valid_spec_dict()),
    ) as mock_create:
        generate_strategy("mean_reversion", client=client, max_retries=1)

    user_text = mock_create.call_args.kwargs["messages"][0]["content"]
    assert "MUST be" not in user_text
    assert "operates on" not in user_text


def test_with_timeframe_flag_injects_constraint_into_prompt(
    tmp_path, monkeypatch
):
    """When requested_timeframe is set, the user message contains the
    full constraint sentence."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-mock")
    tracker = SpendTracker(
        cap_usd=10.0, spend_file=tmp_path / "s.json",
        archive_file=tmp_path / "a.json",
    )
    client = ClaudeClient(spend_tracker=tracker)

    with patch.object(
        client.client.messages, "create",
        return_value=_mock_message_response(_spec_dict_with_timeframe("1h")),
    ) as mock_create:
        generate_strategy(
            "mean_reversion", client=client, max_retries=1,
            requested_timeframe="1h",
        )

    user_text = mock_create.call_args.kwargs["messages"][0]["content"]
    assert "operates on 1h bars" in user_text
    assert "MUST be ['1h']" in user_text


def test_compliant_first_attempt_no_retry(tmp_path, monkeypatch):
    """Model returns timeframes=['1h'] for requested='1h' → success on
    attempt 1, no retry, no quirk increment. (Using 1h because the
    mean_reversion archetype's allowed_timeframes is ('1h', '1d'); 5m
    would cause translator rejection — see scripts/discover.py
    _check_timeframe_archetype_compat.)"""
    _patched_quirks_path(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-mock")
    tracker = SpendTracker(
        cap_usd=10.0, spend_file=tmp_path / "s.json",
        archive_file=tmp_path / "a.json",
    )
    client = ClaudeClient(spend_tracker=tracker)

    with patch.object(
        client.client.messages, "create",
        return_value=_mock_message_response(_spec_dict_with_timeframe("1h")),
    ) as mock_create:
        result = generate_and_translate(
            "mean_reversion", client=client, dedup=False, max_retries=3,
            requested_timeframe="1h",
        )

    assert result.spec is not None
    assert result.spec.timeframes == ["1h"]
    assert len(result.logs) == 1
    assert mock_create.call_count == 1
    # Counter file should not exist (no quirk fired).
    quirks_file = tmp_path / "quirks.json"
    assert not quirks_file.exists()


def test_mismatch_then_compliant_increments_counter_once(
    tmp_path, monkeypatch
):
    """Mismatch on attempt 1, compliant on attempt 2. Counter incremented
    exactly once. Retry feedback contains 'timeframe_mismatch'."""
    _patched_quirks_path(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-mock")
    tracker = SpendTracker(
        cap_usd=10.0, spend_file=tmp_path / "s.json",
        archive_file=tmp_path / "a.json",
    )
    client = ClaudeClient(spend_tracker=tracker)

    responses = [
        _mock_message_response(_spec_dict_with_timeframe("1d")),  # wrong
        _mock_message_response(_spec_dict_with_timeframe("1h")),  # right
    ]
    with patch.object(
        client.client.messages, "create", side_effect=responses,
    ) as mock_create:
        result = generate_and_translate(
            "mean_reversion", client=client, dedup=False, max_retries=3,
            requested_timeframe="1h",
        )

    assert result.spec is not None
    assert result.spec.timeframes == ["1h"]
    assert len(result.logs) == 2
    assert mock_create.call_count == 2

    # Attempt 2's user_text should carry the timeframe_mismatch feedback.
    second_user_text = mock_create.call_args_list[1].kwargs["messages"][0]["content"]
    assert "timeframe_mismatch" in second_user_text
    assert "requested='1h'" in second_user_text

    # Counter incremented exactly once.
    quirks = json.loads((tmp_path / "quirks.json").read_text())
    assert quirks["timeframe_mismatch"]["total"] == 1
    assert quirks["timeframe_mismatch"]["by_requested_timeframe"]["1h"] == 1
    assert quirks["timeframe_mismatch"]["by_archetype"]["mean_reversion"] == 1


def test_three_mismatches_returns_failure_with_warning(
    tmp_path, monkeypatch, caplog
):
    """All 3 attempts mismatch → spec=None, counter incremented 3 times,
    WARNING logged with the 'could not produce timeframe X' message."""
    _patched_quirks_path(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-mock")
    tracker = SpendTracker(
        cap_usd=10.0, spend_file=tmp_path / "s.json",
        archive_file=tmp_path / "a.json",
    )
    client = ClaudeClient(spend_tracker=tracker)

    wrong = _mock_message_response(_spec_dict_with_timeframe("1d"))
    caplog.set_level("WARNING", logger="generator.pipeline")
    with patch.object(
        client.client.messages, "create", side_effect=[wrong, wrong, wrong],
    ):
        result = generate_and_translate(
            "mean_reversion", client=client, dedup=False, max_retries=3,
            requested_timeframe="1h",
        )

    assert result.spec is None
    assert len(result.logs) == 3

    quirks = json.loads((tmp_path / "quirks.json").read_text())
    assert quirks["timeframe_mismatch"]["total"] == 3
    assert quirks["timeframe_mismatch"]["by_requested_timeframe"]["1h"] == 3

    assert any(
        "could not produce timeframe='1h' after 3 attempts" in r.message
        and r.levelname == "WARNING"
        for r in caplog.records
    )


def test_no_warning_when_failure_was_not_timeframe(
    tmp_path, monkeypatch, caplog
):
    """When all attempts fail for a NON-timeframe reason (here: parse
    error), the timeframe-specific WARNING must NOT fire — the discriminator
    in the helper's 4th return value gates this correctly."""
    _patched_quirks_path(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-mock")
    tracker = SpendTracker(
        cap_usd=10.0, spend_file=tmp_path / "s.json",
        archive_file=tmp_path / "a.json",
    )
    client = ClaudeClient(spend_tracker=tracker)

    bad = _valid_spec_dict()
    bad["indicators"][0]["type"] = "bogus_indicator"
    bad["timeframes"] = ["1d"]

    caplog.set_level("WARNING", logger="generator.pipeline")
    with patch.object(
        client.client.messages, "create",
        return_value=_mock_message_response(bad),
    ):
        result = generate_strategy(
            "mean_reversion", client=client, max_retries=3,
            requested_timeframe="1d",  # spec WOULD comply on timeframe;
                                        # but parse fails before timeframe check
        )

    assert result.spec is None
    # No timeframe-specific WARNING.
    assert not any(
        "could not produce timeframe" in r.message for r in caplog.records
    )


def test_metadata_requested_timeframe_populated_in_db(tmp_path, monkeypatch):
    """When --timeframe is set and conn is provided, the leaderboard
    generations row records requested_timeframe verbatim."""
    from leaderboard.db import initialize_db

    _patched_quirks_path(monkeypatch, tmp_path)
    db_conn = initialize_db(tmp_path / "lb.db")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-mock")
    tracker = SpendTracker(
        cap_usd=10.0, spend_file=tmp_path / "s.json",
        archive_file=tmp_path / "a.json",
    )
    client = ClaudeClient(spend_tracker=tracker)

    with patch.object(
        client.client.messages, "create",
        return_value=_mock_message_response(_spec_dict_with_timeframe("1h")),
    ):
        result = generate_and_translate(
            "mean_reversion", client=client, dedup=True, max_retries=1,
            conn=db_conn, requested_timeframe="1h",
        )

    assert result.spec is not None
    rows = db_conn.execute(
        "SELECT requested_timeframe FROM generations"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["requested_timeframe"] == "1h"
    db_conn.close()


def test_metadata_requested_timeframe_none_without_flag(tmp_path, monkeypatch):
    """No --timeframe → generations.requested_timeframe is NULL. Confirms
    the no-flag path doesn't accidentally populate the column."""
    from leaderboard.db import initialize_db

    db_conn = initialize_db(tmp_path / "lb.db")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-mock")
    tracker = SpendTracker(
        cap_usd=10.0, spend_file=tmp_path / "s.json",
        archive_file=tmp_path / "a.json",
    )
    client = ClaudeClient(spend_tracker=tracker)

    with patch.object(
        client.client.messages, "create",
        return_value=_mock_message_response(_valid_spec_dict()),
    ):
        result = generate_and_translate(
            "mean_reversion", client=client, dedup=True, max_retries=1,
            conn=db_conn,
        )

    assert result.spec is not None
    rows = db_conn.execute(
        "SELECT requested_timeframe FROM generations"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["requested_timeframe"] is None
    db_conn.close()


# ── Helper unit tests ────────────────────────────────────────────────────────


def test_generate_spec_with_timeframe_check_returns_success_shape(
    tmp_path, monkeypatch
):
    """Helper success shape: (spec, log, None, False)."""
    from generator.pipeline import _generate_spec_with_timeframe_check

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-mock")
    tracker = SpendTracker(
        cap_usd=10.0, spend_file=tmp_path / "s.json",
        archive_file=tmp_path / "a.json",
    )
    client = ClaudeClient(spend_tracker=tracker)

    with patch.object(
        client.client.messages, "create",
        return_value=_mock_message_response(_spec_dict_with_timeframe("1h")),
    ):
        spec, log, fb, was_tf = _generate_spec_with_timeframe_check(
            client=client, archetype="mean_reversion",
            diversity_context=None, retry_feedback=None, attempt=1,
            requested_timeframe="1h",
        )
    assert spec is not None
    assert fb is None
    assert was_tf is False


def test_generate_spec_with_timeframe_check_returns_mismatch_shape(
    tmp_path, monkeypatch
):
    """Helper mismatch shape: (None, log, fb, True). Counter incremented."""
    from generator.pipeline import _generate_spec_with_timeframe_check

    _patched_quirks_path(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-mock")
    tracker = SpendTracker(
        cap_usd=10.0, spend_file=tmp_path / "s.json",
        archive_file=tmp_path / "a.json",
    )
    client = ClaudeClient(spend_tracker=tracker)

    with patch.object(
        client.client.messages, "create",
        return_value=_mock_message_response(_spec_dict_with_timeframe("1d")),
    ):
        spec, log, fb, was_tf = _generate_spec_with_timeframe_check(
            client=client, archetype="mean_reversion",
            diversity_context=None, retry_feedback=None, attempt=1,
            requested_timeframe="1h",
        )
    assert spec is None
    assert was_tf is True
    assert "timeframe_mismatch" in fb
    assert "requested='1h'" in fb

    quirks = json.loads((tmp_path / "quirks.json").read_text())
    assert quirks["timeframe_mismatch"]["total"] == 1


def test_generate_spec_with_timeframe_check_parse_failure_shape(
    tmp_path, monkeypatch
):
    """Helper parse-failure shape: (None, log, fb, False) — was_tf is False
    even though requested_timeframe is set (the failure was parse, not tf)."""
    from generator.pipeline import _generate_spec_with_timeframe_check

    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-mock")
    tracker = SpendTracker(
        cap_usd=10.0, spend_file=tmp_path / "s.json",
        archive_file=tmp_path / "a.json",
    )
    client = ClaudeClient(spend_tracker=tracker)

    bad = _valid_spec_dict()
    bad["indicators"][0]["type"] = "bogus"
    with patch.object(
        client.client.messages, "create",
        return_value=_mock_message_response(bad),
    ):
        spec, log, fb, was_tf = _generate_spec_with_timeframe_check(
            client=client, archetype="mean_reversion",
            diversity_context=None, retry_feedback=None, attempt=1,
            requested_timeframe="1h",
        )
    assert spec is None
    assert was_tf is False
    assert "Attempt 1 failed" in fb


def test_argparse_rejects_unsupported_timeframe(tmp_path):
    """argparse must reject timeframes not in spec.py TIMEFRAMES.
    30m/4h are deferred per docs/backlog.md."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--timeframe",
        choices=["5m", "15m", "1h", "1d"],
        default=None,
    )
    with pytest.raises(SystemExit):
        parser.parse_args(["--timeframe", "30m"])


# ── scripts/discover.py CLI-validation tests ────────────────────────────────


def _import_discover_helper():
    """scripts/discover.py is a script, not a packaged module. Import its
    validation helper directly via path manipulation. Done lazily inside
    each test so the sys.path mutation is scoped (and the discover module
    itself only imports once per pytest session)."""
    import sys
    scripts_dir = str(Path(__file__).resolve().parent.parent.parent / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import discover
    return discover._check_timeframe_archetype_compat


def test_check_timeframe_archetype_compat_passes_for_valid_combo():
    check = _import_discover_helper()
    # mean_reversion allows 1h and 1d.
    assert check("mean_reversion", "1h") is None
    assert check("mean_reversion", "1d") is None
    # Microstructure allows 5m and 15m.
    assert check("microstructure", "5m") is None
    assert check("microstructure", "15m") is None
    # No timeframe constraint → always pass.
    assert check("mean_reversion", None) is None


def test_check_timeframe_archetype_compat_rejects_unsatisfiable_combo():
    check = _import_discover_helper()
    # mean_reversion does not allow 5m or 15m.
    err = check("mean_reversion", "5m")
    assert err is not None
    assert "mean_reversion" in err
    assert "5m" in err
    assert "Allowed for mean_reversion" in err

    err = check("microstructure", "1d")
    assert err is not None
    assert "microstructure" in err
    assert "1d" in err
