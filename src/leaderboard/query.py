"""Read functions for the leaderboard. Every public function returns
dataclasses (Strategy / Generation / Evaluation / ArchetypeSummary), never
raw sqlite3.Row tuples — keeping the type discipline at the DB boundary
means callers (CLI, integration hooks, tests) can rely on attribute access
and IDE-visible field names.

failed_gates is stored as JSON; common SQLite JSON1 patterns:
    SELECT json_extract(failed_gates, '$[*].name') FROM evaluations ...
    SELECT * FROM evaluations, json_each(failed_gates) WHERE json_extract(value, '$.name') = 'score'
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

from .models import (
    ArchetypeSummary,
    Evaluation,
    Generation,
    Status,
    Strategy,
)


# ── Allowlists for parameters that can't be SQL-bound ────────────────────────

_LIST_STRATEGIES_ORDER_COLUMNS: frozenset[str] = frozenset(
    {"last_seen_at", "first_generated_at", "generation_count"}
)

_QUIRK_NAME_TO_COLUMN: dict[str, str] = {
    "stringification": "stringification_firings",
    "kwarg_validator": "kwarg_validator_firings",
    "unreachable_default": "unreachable_default_firings",
}

_VALID_EVAL_TYPES: frozenset[str] = frozenset({"fast", "canonical", "holdout"})


# ── Public API ───────────────────────────────────────────────────────────────


def get_strategy(
    conn: sqlite3.Connection, behavioral_hash: str
) -> Optional[Strategy]:
    """Return the Strategy with this behavioral_hash, or None if not found."""
    row = conn.execute(
        "SELECT * FROM strategies WHERE behavioral_hash = ?",
        (behavioral_hash,),
    ).fetchone()
    return _strategy_from_row(row) if row is not None else None


def list_strategies(
    conn: sqlite3.Connection,
    *,
    archetype: Optional[str] = None,
    status: Optional[Status] = None,
    timeframe: Optional[str] = None,
    limit: int = 50,
    order_by: str = "last_seen_at",
) -> list[Strategy]:
    """List strategies with optional filters.

    Default order is `last_seen_at DESC` so a freshly-loaded `leaderboard
    list` shows the strategies generated most recently. Allowed `order_by`
    values: 'last_seen_at', 'first_generated_at', 'generation_count'. Order
    is always DESC. order_by is allowlisted (not SQL-bound) to prevent
    injection — passing an unknown column raises ValueError."""
    if order_by not in _LIST_STRATEGIES_ORDER_COLUMNS:
        raise ValueError(
            f"unknown order_by {order_by!r}; allowed: "
            f"{sorted(_LIST_STRATEGIES_ORDER_COLUMNS)}"
        )

    where: list[str] = []
    params: list = []
    if archetype is not None:
        where.append("archetype = ?")
        params.append(archetype)
    if status is not None:
        where.append("status = ?")
        params.append(status.value if isinstance(status, Status) else status)
    if timeframe is not None:
        where.append("timeframe = ?")
        params.append(timeframe)

    where_clause = f"WHERE {' AND '.join(where)}" if where else ""
    sql = (
        f"SELECT * FROM strategies {where_clause} "
        f"ORDER BY {order_by} DESC LIMIT ?"
    )
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_strategy_from_row(r) for r in rows]


def get_authoritative_result(
    conn: sqlite3.Connection, strategy_hash: str
) -> Optional[Evaluation]:
    """Returns the highest-priority evaluation: holdout > canonical > fast.
    Within the same type, returns the most recent. Returns None if no
    evaluations exist for this strategy."""
    sql = """
        SELECT *,
               CASE eval_type
                   WHEN 'holdout' THEN 3
                   WHEN 'canonical' THEN 2
                   WHEN 'fast' THEN 1
                   ELSE 0
               END AS _priority
        FROM evaluations
        WHERE strategy_hash = ?
        ORDER BY _priority DESC, evaluated_at DESC
        LIMIT 1
    """
    row = conn.execute(sql, (strategy_hash,)).fetchone()
    return _evaluation_from_row(row) if row is not None else None


def get_generation_history(
    conn: sqlite3.Connection, strategy_hash: str
) -> list[Generation]:
    """Return all generation events for a strategy, oldest first."""
    rows = conn.execute(
        "SELECT * FROM generations WHERE strategy_hash = ? "
        "ORDER BY generated_at ASC, id ASC",
        (strategy_hash,),
    ).fetchall()
    return [_generation_from_row(r) for r in rows]


def get_archetype_summary(
    conn: sqlite3.Connection,
    archetype: str,
    *,
    timeframe: Optional[str] = None,
    since: Optional[str] = None,
) -> ArchetypeSummary:
    """Aggregate metrics for one archetype.

    `since` is an ISO timestamp filter applied to the *appropriate* timestamp
    in each table: strategies.first_generated_at, generations.generated_at,
    evaluations.evaluated_at. A strategy whose first_generated_at predates
    `since` is excluded from n_strategies + by_status, even if it has
    generations or evaluations after `since` — the archetype-summary scope
    is "strategies first generated in this window."
    """
    # ── strategies side ──
    s_where = ["archetype = ?"]
    s_params: list = [archetype]
    if timeframe is not None:
        s_where.append("timeframe = ?")
        s_params.append(timeframe)
    if since is not None:
        s_where.append("first_generated_at >= ?")
        s_params.append(since)
    s_where_sql = " AND ".join(s_where)

    status_rows = conn.execute(
        f"SELECT status, COUNT(*) AS c FROM strategies "
        f"WHERE {s_where_sql} GROUP BY status",
        s_params,
    ).fetchall()
    by_status: dict[str, int] = {r["status"]: r["c"] for r in status_rows}
    n_strategies = sum(by_status.values())

    # ── generations side ──
    # generations.archetype is denormalized so we can filter without joining
    # to strategies in the common case. We do join when a timeframe filter
    # is set, since timeframe lives only on strategies.
    g_where = ["generations.archetype = ?"]
    g_params: list = [archetype]
    g_join = ""
    if timeframe is not None:
        g_join = "JOIN strategies s ON s.behavioral_hash = generations.strategy_hash"
        g_where.append("s.timeframe = ?")
        g_params.append(timeframe)
    if since is not None:
        g_where.append("generations.generated_at >= ?")
        g_params.append(since)
    g_where_sql = " AND ".join(g_where)

    gen_row = conn.execute(
        f"""
        SELECT
            COUNT(*)                                 AS n,
            SUM(generations.cost_usd)                AS total_cost,
            SUM(generations.stringification_firings) AS sfir,
            SUM(generations.kwarg_validator_firings) AS kfir,
            SUM(generations.unreachable_default_firings) AS ufir
        FROM generations {g_join} WHERE {g_where_sql}
        """,
        g_params,
    ).fetchone()
    n_generations = int(gen_row["n"] or 0) if gen_row else 0
    total_cost_usd = (
        float(gen_row["total_cost"])
        if gen_row and gen_row["total_cost"] is not None
        else None
    )
    quirk_counts = {
        "stringification": int(gen_row["sfir"] or 0) if gen_row else 0,
        "kwarg_validator": int(gen_row["kfir"] or 0) if gen_row else 0,
        "unreachable_default": int(gen_row["ufir"] or 0) if gen_row else 0,
    }

    # ── evaluations side ──
    e_where = ["s.archetype = ?"]
    e_params: list = [archetype]
    e_join = "JOIN strategies s ON s.behavioral_hash = evaluations.strategy_hash"
    if timeframe is not None:
        e_where.append("s.timeframe = ?")
        e_params.append(timeframe)
    if since is not None:
        e_where.append("evaluations.evaluated_at >= ?")
        e_params.append(since)
    e_where_sql = " AND ".join(e_where)

    n_evaluations_by_type = {"fast": 0, "canonical": 0, "holdout": 0}
    n_promising_by_type = {"fast": 0, "canonical": 0, "holdout": 0}
    eval_rows = conn.execute(
        f"""
        SELECT eval_type,
               COUNT(*) AS n,
               SUM(promising) AS np
        FROM evaluations {e_join} WHERE {e_where_sql}
        GROUP BY eval_type
        """,
        e_params,
    ).fetchall()
    for r in eval_rows:
        et = r["eval_type"]
        if et in n_evaluations_by_type:
            n_evaluations_by_type[et] = int(r["n"])
            n_promising_by_type[et] = int(r["np"] or 0)

    # Median score across all in-scope evaluations. SQLite has no MEDIAN
    # function; pulling scores into Python keeps the implementation simple
    # and the row count is bounded by archetype × window.
    score_rows = conn.execute(
        f"SELECT score FROM evaluations {e_join} "
        f"WHERE {e_where_sql} AND score IS NOT NULL",
        e_params,
    ).fetchall()
    scores = [float(r["score"]) for r in score_rows]
    median_score = statistics.median(scores) if scores else None

    return ArchetypeSummary(
        archetype=archetype,
        timeframe=timeframe,
        since=since,
        n_strategies=n_strategies,
        n_generations=n_generations,
        n_evaluations_by_type=n_evaluations_by_type,
        n_promising_by_type=n_promising_by_type,
        by_status=by_status,
        median_score=median_score,
        total_cost_usd=total_cost_usd,
        quirk_counts=quirk_counts,
    )


def get_quirk_trend(
    conn: sqlite3.Connection,
    quirk_name: str,
    window_days: int = 7,
) -> list[tuple[str, int]]:
    """Return a per-day count of quirk firings over the last `window_days`,
    oldest day first. Days with no firings appear with count 0; the result
    always has exactly `window_days` entries.

    Aggregates from the per-generation counter columns
    (stringification_firings, kwarg_validator_firings,
    unreachable_default_firings) by date(generated_at). Per-firing detail
    (which clause / which model output) is not persisted — if a future
    phase needs that, add a quirk_events table via a new migration.

    Raises ValueError on unknown `quirk_name`. The mapping is closed (three
    values: 'stringification', 'kwarg_validator', 'unreachable_default')
    rather than silently extensible — adding a new counter requires both
    a schema migration and an entry in _QUIRK_NAME_TO_COLUMN."""
    if quirk_name not in _QUIRK_NAME_TO_COLUMN:
        raise ValueError(
            f"unknown quirk_name {quirk_name!r}; allowed: "
            f"{sorted(_QUIRK_NAME_TO_COLUMN)}"
        )
    if window_days < 1:
        raise ValueError(f"window_days must be >= 1; got {window_days}")
    col = _QUIRK_NAME_TO_COLUMN[quirk_name]

    today = datetime.now(timezone.utc).date()
    days: list[str] = [
        (today - timedelta(days=i)).isoformat()
        for i in range(window_days - 1, -1, -1)
    ]

    # SUM nonzero firings per day for the windowed range. We zero-fill in
    # Python for days that don't appear in the result set.
    sql = (
        f"SELECT date(generated_at) AS d, SUM({col}) AS c "
        f"FROM generations "
        f"WHERE {col} > 0 AND date(generated_at) >= ? "
        f"GROUP BY date(generated_at)"
    )
    counts = {
        r["d"]: int(r["c"] or 0)
        for r in conn.execute(sql, (days[0],)).fetchall()
    }
    return [(d, counts.get(d, 0)) for d in days]


def get_promising_candidates(
    conn: sqlite3.Connection,
    eval_type: str = "canonical",
) -> list[Strategy]:
    """Return strategies that have at least one promising evaluation of the
    given eval_type, ordered by their *most recent* promising eval's score
    DESC. NULL scores naturally sort last in DESC order under SQLite."""
    if eval_type not in _VALID_EVAL_TYPES:
        raise ValueError(
            f"unknown eval_type {eval_type!r}; allowed: "
            f"{sorted(_VALID_EVAL_TYPES)}"
        )
    sql = """
        WITH ranked AS (
            SELECT strategy_hash, score, evaluated_at,
                   ROW_NUMBER() OVER (
                       PARTITION BY strategy_hash
                       ORDER BY evaluated_at DESC
                   ) AS rn
            FROM evaluations
            WHERE eval_type = ? AND promising = 1
        )
        SELECT s.*, ranked.score AS _score, ranked.evaluated_at AS _at
        FROM strategies s
        JOIN ranked ON s.behavioral_hash = ranked.strategy_hash AND ranked.rn = 1
        ORDER BY _score DESC, _at DESC
    """
    rows = conn.execute(sql, (eval_type,)).fetchall()
    return [_strategy_from_row(r) for r in rows]


# ── Row → dataclass converters ───────────────────────────────────────────────


def _strategy_from_row(row: sqlite3.Row) -> Strategy:
    return Strategy(
        behavioral_hash=row["behavioral_hash"],
        name=row["name"],
        archetype=row["archetype"],
        timeframe=row["timeframe"],
        spec_json=row["spec_json"],
        first_generated_at=row["first_generated_at"],
        last_seen_at=row["last_seen_at"],
        generation_count=row["generation_count"],
        status=Status(row["status"]),
        fast_evaluated_at=row["fast_evaluated_at"],
        canonical_evaluated_at=row["canonical_evaluated_at"],
        holdout_evaluated_at=row["holdout_evaluated_at"],
        paper_candidate_at=row["paper_candidate_at"],
        paper_started_at=row["paper_started_at"],
        paper_ended_at=row["paper_ended_at"],
        archived_at=row["archived_at"],
        paper_outcome=row["paper_outcome"],
        paper_notes=row["paper_notes"],
        archive_reason=row["archive_reason"],
        imported_from=row["imported_from"],
    )


def _generation_from_row(row: sqlite3.Row) -> Generation:
    return Generation(
        id=row["id"],
        strategy_hash=row["strategy_hash"],
        generated_at=row["generated_at"],
        archetype=row["archetype"],
        model_version=row["model_version"],
        prompt_hash=row["prompt_hash"],
        requested_timeframe=row["requested_timeframe"],
        cost_usd=row["cost_usd"],
        retry_count=row["retry_count"],
        duration_seconds=row["duration_seconds"],
        stringification_firings=row["stringification_firings"],
        kwarg_validator_firings=row["kwarg_validator_firings"],
        unreachable_default_firings=row["unreachable_default_firings"],
        raw_response_path=row["raw_response_path"],
        spec_path=row["spec_path"],
        imported_from=row["imported_from"],
    )


def _evaluation_from_row(row: sqlite3.Row) -> Evaluation:
    return Evaluation(
        id=row["id"],
        strategy_hash=row["strategy_hash"],
        eval_type=row["eval_type"],
        evaluated_at=row["evaluated_at"],
        n_oos_trades=row["n_oos_trades"],
        promising=bool(row["promising"]),
        results_dir=row["results_dir"],
        config_json=row["config_json"],
        duration_seconds=row["duration_seconds"],
        median_pf=row["median_pf"],
        score=row["score"],
        failed_gates=row["failed_gates"],
        imported_from=row["imported_from"],
    )
