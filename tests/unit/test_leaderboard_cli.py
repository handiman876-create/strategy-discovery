"""Tests for scripts/leaderboard.py.

Helpers (format_table, _resolve_hash_prefix, _parse_since) are unit-tested
directly via importlib.util; the script itself is smoke-tested via subprocess
on a populated DB. Argparse wiring is intentionally not unit-tested — the
expensive surface is the formatters and the prefix resolver."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from leaderboard.db import initialize_db


_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "leaderboard.py"


@pytest.fixture(scope="module")
def cli():
    """Load scripts/leaderboard.py as a module so we can call its helpers
    directly without spawning a subprocess for every assertion."""
    spec = importlib.util.spec_from_file_location("leaderboard_cli", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── format_table ─────────────────────────────────────────────────────────────


def test_format_table_basic(cli):
    out = cli.format_table(
        ["a", "longheader"],
        [["x", "y"], ["foo", "barbaz"]],
    )
    lines = out.split("\n")
    # Header + separator + 2 data rows
    assert len(lines) == 4
    # Column widths: max(len('a'), len('x'), len('foo')) = 3
    #                max(len('longheader'), len('y'), len('barbaz')) = 10
    assert lines[0] == "a    longheader"
    assert lines[1] == "---  ----------"
    assert "foo" in lines[3] and "barbaz" in lines[3]


def test_format_table_empty(cli):
    assert cli.format_table(["a", "b"], []) == "(no rows)"


# ── _resolve_hash_prefix ─────────────────────────────────────────────────────


@pytest.fixture
def populated_conn(tmp_path):
    """A DB with three strategies: two whose hashes share a prefix and one
    distinct. Used by prefix-resolver tests."""
    db = tmp_path / "lb.db"
    conn = initialize_db(db)
    for h, name in [
        ("aaaaaa1111deadbeef", "alpha"),  # shares 'aaaaaa' (6 chars) with beta
        ("aaaaaa2222deadbeef", "beta"),
        ("bbbbcccc11112222", "gamma"),
    ]:
        conn.execute(
            "INSERT INTO strategies (behavioral_hash, name, archetype, timeframe, "
            "spec_json, first_generated_at, last_seen_at, status) "
            "VALUES (?, ?, 'mean_reversion', '1d', '{}', "
            "datetime('now'), datetime('now'), 'generated')",
            (h, name),
        )
    yield conn
    conn.close()


def test_resolve_hash_prefix_unique(cli, populated_conn):
    full = cli._resolve_hash_prefix(populated_conn, "bbbbcc")
    assert full == "bbbbcccc11112222"


def test_resolve_hash_prefix_no_match(cli, populated_conn):
    with pytest.raises(cli.CliError, match="no strategy matches"):
        cli._resolve_hash_prefix(populated_conn, "ffffff")


def test_resolve_hash_prefix_ambiguous(cli, populated_conn):
    """Two hashes share the 'aaaaaa' prefix; resolver must list both and
    refuse to choose."""
    with pytest.raises(cli.CliError, match=r"matches 2 strategies"):
        cli._resolve_hash_prefix(populated_conn, "aaaaaa")


def test_resolve_hash_prefix_too_short(cli, populated_conn):
    with pytest.raises(cli.CliError, match="at least 6 characters"):
        cli._resolve_hash_prefix(populated_conn, "aaa")


# ── _parse_since ─────────────────────────────────────────────────────────────


def test_parse_since_iso_date(cli):
    out = cli._parse_since("2026-04-29")
    parsed = datetime.fromisoformat(out)
    assert parsed.year == 2026 and parsed.month == 4 and parsed.day == 29
    assert parsed.tzinfo is not None  # UTC-stamped


def test_parse_since_relative_days(cli):
    out = cli._parse_since("7d")
    parsed = datetime.fromisoformat(out)
    delta = datetime.now(timezone.utc) - parsed
    # Allow a small window for clock drift between the function and the test.
    assert timedelta(days=6, hours=23) < delta < timedelta(days=7, hours=1)


def test_parse_since_full_iso(cli):
    full = "2026-04-29T15:30:00+00:00"
    out = cli._parse_since(full)
    assert datetime.fromisoformat(out) == datetime.fromisoformat(full)


def test_parse_since_invalid(cli):
    with pytest.raises(cli.CliError, match="invalid --since"):
        cli._parse_since("yesterday")


# ── Subprocess smoke tests ───────────────────────────────────────────────────


def _populate(db: Path) -> str:
    """Write one strategy with two evals (one promising) and one generation.
    Returns the strategy's behavioral_hash for show/promote testing."""
    conn = initialize_db(db)
    h = "feedf00d" * 8  # 64 chars
    conn.execute(
        "INSERT INTO strategies (behavioral_hash, name, archetype, timeframe, "
        "spec_json, first_generated_at, last_seen_at, status) "
        "VALUES (?, 'demo_smoke', 'mean_reversion', '1d', '{}', "
        "'2026-04-29T00:00:00', '2026-04-29T00:00:00', 'generated')",
        (h,),
    )
    conn.execute(
        "INSERT INTO generations (strategy_hash, generated_at, archetype, "
        "model_version, prompt_hash, cost_usd, retry_count, duration_seconds) "
        "VALUES (?, '2026-04-29T00:00:00', 'mean_reversion', 'm', 'p', 0.05, 0, 1.0)",
        (h,),
    )
    conn.execute(
        "INSERT INTO evaluations (strategy_hash, eval_type, evaluated_at, "
        "n_oos_trades, score, promising, results_dir, config_json) "
        "VALUES (?, 'fast', '2026-04-29T00:00:00', 50, 2.0, 1, '/tmp', '{}')",
        (h,),
    )
    conn.close()
    return h


def _run(args: list[str], db: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "--db", str(db), *args],
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_cli_list_smoke(tmp_path):
    db = tmp_path / "lb.db"
    _populate(db)
    p = _run(["list"], db)
    assert p.returncode == 0, p.stderr
    assert "demo_smoke" in p.stdout
    assert "mean_reversion" in p.stdout
    assert "1d" in p.stdout


def test_cli_list_json_smoke(tmp_path):
    db = tmp_path / "lb.db"
    _populate(db)
    p = _run(["--json", "list"], db)
    assert p.returncode == 0, p.stderr
    parsed = json.loads(p.stdout)
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["name"] == "demo_smoke"
    assert parsed[0]["status"] == "generated"


def test_cli_global_flags_work_after_subcommand(tmp_path):
    """Regression: --json (and --db) must work in either position relative to
    the subcommand. Argparse's default behavior puts global flags only on the
    top-level parser; a hand-test caught that `lb list --json` silently
    emitted tabular output. The fix uses default=argparse.SUPPRESS on the
    subparser side so it doesn't overwrite the top-level value."""
    db = tmp_path / "lb.db"
    _populate(db)
    p_before = _run(["--json", "list"], db)
    p_after = _run(["list", "--json"], db)
    assert p_before.returncode == 0
    assert p_after.returncode == 0
    # Both positions must produce the same JSON output.
    assert json.loads(p_before.stdout) == json.loads(p_after.stdout)


def test_cli_show_smoke(tmp_path):
    db = tmp_path / "lb.db"
    h = _populate(db)
    p = _run(["show", h[:10]], db)
    assert p.returncode == 0, p.stderr
    assert "demo_smoke" in p.stdout
    assert "Authoritative result" in p.stdout
    assert "promising: True" in p.stdout


def test_cli_promote_smoke_routes_through_transition_status(tmp_path):
    """generated → paper_trading is unreachable via either matrix (paper_trading
    requires paper_candidate as predecessor, and paper_candidate requires
    holdout_evaluated). The error must surface from the state machine, not
    be swallowed by the CLI."""
    db = tmp_path / "lb.db"
    h = _populate(db)
    p = _run(["promote", h[:10], "--to", "paper_trading"], db)
    assert p.returncode == 1
    assert "illegal transition" in p.stderr


def test_cli_archive_smoke(tmp_path):
    db = tmp_path / "lb.db"
    h = _populate(db)
    p = _run(["archive", h[:10], "--reason", "stale"], db)
    assert p.returncode == 0, p.stderr
    # State actually changed.
    p2 = _run(["show", h[:10]], db)
    assert "status:           archived" in p2.stdout
    assert "archive_reason:   stale" in p2.stdout
