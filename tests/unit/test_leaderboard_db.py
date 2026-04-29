"""Tests for the leaderboard SQLite connection + migration runner."""

from __future__ import annotations

import sqlite3

import pytest

from leaderboard.db import applied_versions, connect, initialize_db


def test_initialize_db_creates_expected_schema(tmp_path):
    db = tmp_path / "lb.db"
    conn = initialize_db(db)
    try:
        # The four primary tables + paper_trading_sessions stub +
        # schema_version (created by the runner). sqlite_% are internal
        # SQLite tables (e.g. sqlite_sequence for AUTOINCREMENT) and are
        # filtered out.
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
        tables = sorted(r[0] for r in rows)
        assert tables == [
            "evaluations",
            "generations",
            "paper_trading_sessions",
            "schema_version",
            "strategies",
        ]

        # connect() must turn FKs on and switch to WAL journaling.
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

        # Row factory: queries return sqlite3.Row with dict-style access.
        conn.execute(
            "INSERT INTO strategies (behavioral_hash, name, archetype, timeframe, "
            "spec_json, first_generated_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
            ("h1", "demo", "mean_reversion", "1d", "{}"),
        )
        row = conn.execute(
            "SELECT behavioral_hash, name FROM strategies"
        ).fetchone()
        assert isinstance(row, sqlite3.Row)
        assert row["behavioral_hash"] == "h1"
        assert row["name"] == "demo"
    finally:
        conn.close()


def test_initialize_db_is_idempotent(tmp_path):
    db = tmp_path / "lb.db"
    conn1 = initialize_db(db)
    try:
        conn1.execute(
            "INSERT INTO strategies (behavioral_hash, name, archetype, timeframe, "
            "spec_json, first_generated_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'))",
            ("h1", "demo", "mean_reversion", "1d", "{}"),
        )
        versions_first = applied_versions(db)
    finally:
        conn1.close()

    # Second init on the same path must be a no-op: same applied versions,
    # same data.
    conn2 = initialize_db(db)
    try:
        versions_second = applied_versions(db)
        assert versions_first == versions_second
        assert versions_first  # not empty — at least one migration applied

        row = conn2.execute(
            "SELECT behavioral_hash FROM strategies"
        ).fetchone()
        assert row["behavioral_hash"] == "h1"
    finally:
        conn2.close()


def test_broken_migration_rolls_back_cleanly(tmp_path):
    db = tmp_path / "lb.db"

    # Hand-rolled migrations dir whose only file deliberately fails midway:
    # the second CREATE TABLE x raises sqlite3.OperationalError because x
    # already exists. Structurally identical to any real broken migration
    # where one statement succeeds and the next fails.
    bad_dir = tmp_path / "migrations"
    bad_dir.mkdir()
    (bad_dir / "001_broken.sql").write_text(
        "CREATE TABLE x (a INTEGER);\n"
        "CREATE TABLE x (b INTEGER);\n"
    )

    with pytest.raises(sqlite3.OperationalError):
        initialize_db(db, migrations_dir=bad_dir)

    # No version row was inserted: the wrapped BEGIN/COMMIT in the runner
    # rolled back the whole script, including the schema_version INSERT.
    assert applied_versions(db) == []

    # Table x was rolled back: it must not exist. schema_version was created
    # by the runner *before* the failed migration ran (in
    # _ensure_schema_version_table), so it should be present and empty.
    conn = connect(db)
    try:
        names = sorted(
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        )
        assert "x" not in names
        assert "schema_version" in names
    finally:
        conn.close()
