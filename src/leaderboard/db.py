"""SQLite connection + migration runner for the leaderboard."""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import Iterator

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "db" / "leaderboard.db"
_MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_FILENAME_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")

logger = logging.getLogger(__name__)


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with foreign keys enforced, WAL journaling,
    and a Row factory. Caller owns the connection and must close it.

    isolation_level is set to None (autocommit mode) so callers control
    transactions explicitly via BEGIN/COMMIT — needed because the migration
    runner relies on executescript() not auto-committing partway through a
    multi-statement migration."""
    path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def initialize_db(
    db_path: Path | str | None = None,
    migrations_dir: Path | str | None = None,
) -> sqlite3.Connection:
    """Open a connection (creating the file if needed) and apply pending
    migrations from `migrations_dir` in version order. When `migrations_dir`
    is None, falls back to the package's `migrations/` directory — this
    override exists so tests can point at hand-rolled migration sets without
    monkeypatching module internals. Idempotent: re-running with all
    migrations applied is a no-op. Returns the open connection."""
    conn = connect(db_path)
    _ensure_schema_version_table(conn)
    applied = _applied_versions(conn)
    mdir = Path(migrations_dir) if migrations_dir is not None else _MIGRATIONS_DIR
    for version, sql_path in _pending_migrations(applied, mdir):
        logger.info("applying migration %03d: %s", version, sql_path.name)
        sql = sql_path.read_text()
        # executescript() auto-commits any pending Python-side transaction at
        # start — so wrapping with conn.execute("BEGIN") then conn.executescript
        # then conn.execute("COMMIT") doesn't actually wrap. Instead we put
        # BEGIN and COMMIT inside the script itself; SQLite rolls back the
        # whole BEGIN'd transaction if any statement raises mid-script.
        # Version interpolation is safe because _pending_migrations already
        # constrained version to \d{3} via _MIGRATION_FILENAME_RE.
        wrapped = (
            "BEGIN;\n"
            + sql
            + f"\nINSERT INTO schema_version (version, applied_at) "
            + f"VALUES ({version}, datetime('now'));\n"
            + "COMMIT;\n"
        )
        try:
            conn.executescript(wrapped)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass  # transaction already rolled back by SQLite on error
            raise
    return conn


def applied_versions(db_path: Path | str | None = None) -> list[int]:
    """Return the sorted list of schema versions applied to the DB. Opens
    and closes its own connection for one-off introspection."""
    conn = connect(db_path)
    try:
        _ensure_schema_version_table(conn)
        return sorted(_applied_versions(conn))
    finally:
        conn.close()


# ── internals ────────────────────────────────────────────────────────────────


def _ensure_schema_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        "  version INTEGER PRIMARY KEY,"
        "  applied_at TIMESTAMP NOT NULL"
        ")"
    )


def _applied_versions(conn: sqlite3.Connection) -> set[int]:
    return {r[0] for r in conn.execute("SELECT version FROM schema_version")}


def _pending_migrations(
    applied: set[int], migrations_dir: Path
) -> Iterator[tuple[int, Path]]:
    for path in sorted(migrations_dir.glob("*.sql")):
        m = _MIGRATION_FILENAME_RE.match(path.name)
        if m is None:
            logger.warning("skipping malformed migration filename: %s", path.name)
            continue
        version = int(m.group(1))
        if version in applied:
            continue
        yield version, path
