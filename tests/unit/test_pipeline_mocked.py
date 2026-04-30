"""End-to-end pipeline test with mocked Claude API.

Verifies that generate_and_translate:
  1. Calls the API (mocked to return a valid spec).
  2. Translates the spec to a code file.
  3. Computes the behavioral hash.
  4. Records spend correctly.
  5. Retries on validation failure with feedback.
"""

from __future__ import annotations

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
    strategies row + one generations row. The behavioral_hash from the
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
    assert result.behavioral_hash is not None

    rows = db_conn.execute(
        "SELECT behavioral_hash, name, archetype FROM strategies"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["behavioral_hash"] == result.behavioral_hash
    assert rows[0]["archetype"] == "mean_reversion"

    gens = db_conn.execute(
        "SELECT strategy_hash, archetype FROM generations"
    ).fetchall()
    assert len(gens) == 1
    assert gens[0]["strategy_hash"] == result.behavioral_hash
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
    """dedup=False → behavioral_hash is None → leaderboard write is
    skipped with a DEBUG log. Strategies are keyed by behavioral_hash so
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
    assert result.behavioral_hash is None
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
    assert result.behavioral_hash is not None
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
