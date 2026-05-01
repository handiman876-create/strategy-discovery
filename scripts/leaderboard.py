#!/usr/bin/env python3
"""Phase-4 leaderboard CLI.

Subcommands: list, show, promising, archetype, quirks, promote, archive.
Global flags: --db PATH (default db/leaderboard.db), --json (machine-readable).

Hash prefix matching: strategy_hash is 64 chars; users provide a prefix of
at least 6. Ambiguous prefixes list the matches and exit 1.

User-facing errors raise CliError → printed to stderr + exit 1. Unknown
errors propagate so traceback is preserved for debugging.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))

from leaderboard.db import DEFAULT_DB_PATH, initialize_db
from leaderboard.models import Status
from leaderboard.query import (
    get_archetype_summary,
    get_authoritative_result,
    get_generation_history,
    get_promising_candidates,
    get_quirk_trend,
    get_strategy,
    list_strategies,
)
from leaderboard.record import transition_status


# ── Errors ───────────────────────────────────────────────────────────────────


class CliError(Exception):
    """User-visible error; main() prints to stderr and exits 1."""


# ── Output formatting ────────────────────────────────────────────────────────


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    """Right-pad columns to header width OR widest cell, whichever is larger.
    Returns '(no rows)' when rows is empty so the caller doesn't need to
    branch on emptiness."""
    if not rows:
        return "(no rows)"
    n = len(headers)
    widths = [
        max(len(headers[i]), max(len(str(r[i])) for r in rows))
        for i in range(n)
    ]
    sep = "  "
    out = [sep.join(headers[i].ljust(widths[i]) for i in range(n))]
    out.append(sep.join("-" * widths[i] for i in range(n)))
    for r in rows:
        out.append(sep.join(str(r[i]).ljust(widths[i]) for i in range(n)))
    return "\n".join(out)


def _to_jsonable(obj):
    """asdict-then-walk for dataclasses; Status (str, Enum) auto-serializes
    as its underlying str via json.dumps. None / primitives pass through."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, list):
        return [_to_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if hasattr(obj, "__dataclass_fields__"):
        return _to_jsonable(asdict(obj))
    return obj


def _print_json(payload) -> None:
    print(json.dumps(_to_jsonable(payload), indent=2, default=str))


# ── Helpers ──────────────────────────────────────────────────────────────────


_MIN_PREFIX_LEN = 6
_RELATIVE_DATE_RE = re.compile(r"^(\d+)d$")


def _resolve_hash_prefix(conn, prefix: str) -> str:
    """Resolve a partial strategy_hash to a full one. Raises CliError if
    no match, ambiguous match, or prefix is shorter than the minimum."""
    if len(prefix) < _MIN_PREFIX_LEN:
        raise CliError(
            f"hash prefix must be at least {_MIN_PREFIX_LEN} characters; "
            f"got {len(prefix)}"
        )
    rows = conn.execute(
        "SELECT strategy_hash, name FROM strategies "
        "WHERE strategy_hash LIKE ? || '%' "
        "ORDER BY strategy_hash "
        "LIMIT 11",
        (prefix,),
    ).fetchall()
    if not rows:
        raise CliError(f"no strategy matches prefix {prefix!r}")
    if len(rows) > 1:
        listing = "\n".join(
            f"  {r['strategy_hash'][:16]}  {r['name']}" for r in rows[:10]
        )
        more = f"\n  ... and more" if len(rows) > 10 else ""
        raise CliError(
            f"prefix {prefix!r} matches {len(rows)} strategies; be more specific:"
            f"\n{listing}{more}"
        )
    return rows[0]["strategy_hash"]


def _parse_since(value: str) -> str:
    """Convert --since input into an ISO-8601 timestamp string for SQL
    binding. Accepts 'YYYY-MM-DD', a full ISO timestamp, or 'Nd' relative
    (e.g. '7d' → 7 days ago at the current UTC time)."""
    m = _RELATIVE_DATE_RE.match(value)
    if m:
        days = int(m.group(1))
        return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        d = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return d.isoformat()
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(value).isoformat()
    except ValueError as e:
        raise CliError(
            f"invalid --since {value!r}; expected YYYY-MM-DD or Nd "
            "(e.g. '7d')"
        ) from e


def _days_window_from_since(since_iso: Optional[str], default_days: int) -> int:
    """Convert a --since ISO string to a window_days int suitable for
    get_quirk_trend. Returns at least 1."""
    if since_iso is None:
        return default_days
    since_dt = datetime.fromisoformat(since_iso)
    if since_dt.tzinfo is None:
        since_dt = since_dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - since_dt
    return max(1, delta.days + 1)


# ── Subcommand handlers ──────────────────────────────────────────────────────


def cmd_list(args) -> int:
    conn = initialize_db(args.db)
    try:
        status = Status(args.status) if args.status else None
        rows = list_strategies(
            conn,
            archetype=args.archetype,
            status=status,
            timeframe=args.timeframe,
            limit=args.limit,
        )
    finally:
        conn.close()

    if args.json:
        _print_json(rows)
        return 0

    if not rows:
        print("(no strategies)")
        return 0
    headers = ["hash", "name", "archetype", "tf", "status", "gens", "last_seen"]
    table = [
        [
            s.strategy_hash[:8],
            s.name,
            s.archetype,
            s.timeframe,
            s.status.value if isinstance(s.status, Status) else s.status,
            str(s.generation_count),
            s.last_seen_at[:10],
        ]
        for s in rows
    ]
    print(format_table(headers, table))
    return 0


def cmd_show(args) -> int:
    conn = initialize_db(args.db)
    try:
        hash_ = _resolve_hash_prefix(conn, args.hash_prefix)
        strategy = get_strategy(conn, hash_)
        result = get_authoritative_result(conn, hash_)
        gens = get_generation_history(conn, hash_)
    finally:
        conn.close()

    if args.json:
        _print_json(
            {
                "strategy": strategy,
                "authoritative_result": result,
                "generation_history": gens,
            }
        )
        return 0

    print(f"strategy_hash:  {strategy.strategy_hash}")
    print(f"name:             {strategy.name}")
    print(f"archetype:        {strategy.archetype}")
    print(f"timeframe:        {strategy.timeframe}")
    print(f"status:           {strategy.status.value}")
    print(f"generation_count: {strategy.generation_count}")
    print(f"first_generated:  {strategy.first_generated_at}")
    print(f"last_seen:        {strategy.last_seen_at}")
    if strategy.archive_reason:
        print(f"archive_reason:   {strategy.archive_reason}")
    print()
    print("Authoritative result:")
    if result is None:
        print("  (no evaluations)")
    else:
        print(f"  type:      {result.eval_type}")
        print(f"  promising: {result.promising}")
        print(f"  trades:    {result.n_oos_trades}")
        print(f"  median_pf: {result.median_pf}")
        print(f"  score:     {result.score}")
        if result.failed_gates:
            print(f"  failed:    {result.failed_gates}")
    print()
    print(f"Generation history ({len(gens)} events):")
    for g in gens:
        print(
            f"  {g.generated_at}  model={g.model_version}  "
            f"prompt={g.prompt_hash[:8]}  retries={g.retry_count}"
        )
    return 0


def cmd_promising(args) -> int:
    conn = initialize_db(args.db)
    try:
        rows = get_promising_candidates(conn, eval_type=args.eval_type)
    finally:
        conn.close()
    if args.json:
        _print_json(rows)
        return 0
    if not rows:
        print(f"(no promising candidates at eval_type={args.eval_type})")
        return 0
    headers = ["hash", "name", "archetype", "tf", "status"]
    table = [
        [s.strategy_hash[:8], s.name, s.archetype, s.timeframe,
         s.status.value if isinstance(s.status, Status) else s.status]
        for s in rows
    ]
    print(format_table(headers, table))
    return 0


def cmd_archetype(args) -> int:
    since = _parse_since(args.since) if args.since else None
    conn = initialize_db(args.db)
    try:
        summary = get_archetype_summary(
            conn, args.archetype_name, timeframe=args.timeframe, since=since
        )
    finally:
        conn.close()
    if args.json:
        _print_json(summary)
        return 0
    print(f"archetype:    {summary.archetype}")
    if summary.timeframe:
        print(f"timeframe:    {summary.timeframe}")
    if summary.since:
        print(f"since:        {summary.since}")
    print(f"strategies:   {summary.n_strategies}")
    print(f"generations:  {summary.n_generations}")
    if summary.total_cost_usd is not None:
        print(f"total_cost:   ${summary.total_cost_usd:.4f}")
    print(f"median_score: {summary.median_score}")
    print(f"by_status:    {summary.by_status}")
    print(f"evaluations:  {summary.n_evaluations_by_type}")
    print(f"promising:    {summary.n_promising_by_type}")
    print(f"quirks:       {summary.quirk_counts}")
    return 0


def cmd_quirks(args) -> int:
    conn = initialize_db(args.db)
    try:
        since_iso = _parse_since(args.since) if args.since else None
        if args.trend:
            window = _days_window_from_since(since_iso, default_days=7)
            trend = get_quirk_trend(conn, args.trend, window_days=window)
            if args.json:
                _print_json([{"date": d, "count": c} for d, c in trend])
                return 0
            print(f"trend for {args.trend} over {window} days:")
            for d, c in trend:
                bar = "#" * c if c <= 40 else "#" * 40 + f" ({c})"
                print(f"  {d}  {c:>4}  {bar}")
            return 0

        # Summary across all 3 quirks
        window = _days_window_from_since(since_iso, default_days=30)
        totals = {}
        for name in ("stringification", "kwarg_validator", "unreachable_default"):
            trend = get_quirk_trend(conn, name, window_days=window)
            totals[name] = sum(c for _, c in trend)
        if args.json:
            _print_json({"window_days": window, "totals": totals})
            return 0
        print(f"quirk summary over {window} days:")
        for name, total in totals.items():
            print(f"  {name:25} {total}")
        return 0
    finally:
        conn.close()


def cmd_promote(args) -> int:
    target = Status(args.to)
    conn = initialize_db(args.db)
    try:
        hash_ = _resolve_hash_prefix(conn, args.hash_prefix)
        try:
            transition_status(
                conn,
                hash_,
                target,
                paper_outcome=args.paper_outcome,
                # --reason is repurposed as paper_notes when promoting INTO
                # paper_complete (the only state with a notes column).
                # Otherwise it's accepted but ignored — see help text.
                paper_notes=args.reason if target is Status.PAPER_COMPLETE else None,
            )
        except ValueError as e:
            raise CliError(str(e))
    finally:
        conn.close()
    print(f"promoted {hash_[:12]} to {target.value}")
    if args.reason and target is not Status.PAPER_COMPLETE:
        print(
            f"note: --reason recorded only for archive (target=archived) and "
            f"paper_complete; ignored for {target.value}",
            file=sys.stderr,
        )
    return 0


def cmd_archive(args) -> int:
    conn = initialize_db(args.db)
    try:
        hash_ = _resolve_hash_prefix(conn, args.hash_prefix)
        try:
            transition_status(
                conn, hash_, Status.ARCHIVED, archive_reason=args.reason
            )
        except ValueError as e:
            raise CliError(str(e))
    finally:
        conn.close()
    print(f"archived {hash_[:12]}: {args.reason}")
    return 0


def cmd_reconcile(args) -> int:
    """Re-derive promising + failed_gates for stored fast evaluations
    against current scoring logic. canonical/holdout reconciliation is a
    future enhancement (no rows of those types in the DB today)."""
    from leaderboard.reconcile import reconcile_evaluations

    conn = initialize_db(args.db)
    try:
        summary = reconcile_evaluations(conn, project_root=_ROOT)
    finally:
        conn.close()

    if getattr(args, "json", False):
        _print_json(
            {
                "n_reconciled": summary.n_reconciled,
                "n_unchanged": summary.n_unchanged,
                "n_skipped": summary.n_skipped,
                "changes": [
                    {
                        "eval_id": ch.eval_id,
                        "strategy_hash": ch.strategy_hash,
                        "old_promising": ch.old_promising,
                        "new_promising": ch.new_promising,
                        "old_failed_gates": ch.old_failed_gates,
                        "new_failed_gates": ch.new_failed_gates,
                    }
                    for ch in summary.changes
                ],
                "skipped": [
                    {"eval_id": eid, "reason": reason}
                    for eid, reason in summary.skipped
                ],
                "log_path": str(summary.log_path) if summary.log_path else None,
            }
        )
    else:
        print(summary.render())
    return 0


def cmd_backfill(args) -> int:
    """Walk results/ and import historical generation/eval artifacts as
    leaderboard rows marked imported_from='backfill'. Idempotent on the
    natural keys; logs all skips to results/backfill_<ts>.log."""
    from leaderboard.backfill import backfill_all

    results_dir = Path(args.results_dir)
    if not results_dir.exists():
        raise CliError(f"results dir does not exist: {results_dir}")

    conn = initialize_db(args.db)
    try:
        summary = backfill_all(conn, results_dir)
    finally:
        conn.close()

    if getattr(args, "json", False):
        _print_json({
            "imported_strategies": summary.imported_strategies,
            "imported_generations": summary.imported_generations,
            "imported_evaluations": summary.imported_evaluations,
            "skipped_generations": len(summary.skipped_generations),
            "skipped_evaluations": len(summary.skipped_evaluations),
            "log_path": str(summary.log_path) if summary.log_path else None,
        })
    else:
        print(summary.render())
    return 0


# ── Argparse wiring ──────────────────────────────────────────────────────────


def _build_subparser_globals() -> argparse.ArgumentParser:
    """Subparser-side common parser. Defaults are argparse.SUPPRESS so that
    when --db/--json appear *before* the subcommand (parsed by the top-level
    parser), the subparser doesn't overwrite the top-level value with its
    own default. With SUPPRESS, the attribute is added to the namespace only
    when the flag is explicitly given on the subparser side. Net effect: the
    user can write the global flags before OR after the subcommand
    interchangeably (e.g. `lb --json list` and `lb list --json` both work)."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db",
        type=Path,
        default=argparse.SUPPRESS,
        help=f"Path to leaderboard.db (default: {DEFAULT_DB_PATH})",
    )
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Emit JSON instead of tabular output",
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _build_subparser_globals()
    parser = argparse.ArgumentParser(
        prog="leaderboard.py",
        description="Phase 4 leaderboard CLI (read-only by default; "
        "promote/archive write).",
    )
    # Top-level versions carry the real defaults; the subparser versions
    # (via parents=[common]) use SUPPRESS so they don't overwrite. See
    # _build_subparser_globals for the rationale.
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to leaderboard.db (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of tabular output",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", parents=[common], help="List strategies")
    p_list.add_argument("--archetype")
    p_list.add_argument("--status", choices=[s.value for s in Status])
    p_list.add_argument("--timeframe")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", parents=[common], help="Show details for one strategy")
    p_show.add_argument("hash_prefix", help="strategy_hash prefix (>=6 chars)")
    p_show.set_defaults(func=cmd_show)

    p_prom = sub.add_parser("promising", parents=[common], help="List promising candidates")
    p_prom.add_argument(
        "--eval-type",
        dest="eval_type",
        choices=["fast", "canonical", "holdout"],
        default="canonical",
    )
    p_prom.set_defaults(func=cmd_promising)

    p_arch = sub.add_parser("archetype", parents=[common], help="Archetype rollup metrics")
    p_arch.add_argument("archetype_name")
    p_arch.add_argument("--timeframe")
    p_arch.add_argument("--since", help="YYYY-MM-DD or Nd (e.g. '7d')")
    p_arch.set_defaults(func=cmd_archetype)

    p_q = sub.add_parser("quirks", parents=[common], help="Quirk counters")
    p_q.add_argument(
        "--trend",
        choices=["stringification", "kwarg_validator", "unreachable_default"],
        help="Show day-by-day trend for one quirk instead of the summary",
    )
    p_q.add_argument("--since", help="YYYY-MM-DD or Nd (e.g. '7d')")
    p_q.set_defaults(func=cmd_quirks)

    # promote choices: every Status except 'generated' (initial) and 'archived'
    # (archive subcommand owns that).
    promote_targets = [
        s.value for s in Status if s not in (Status.GENERATED, Status.ARCHIVED)
    ]
    p_promote = sub.add_parser("promote", parents=[common], help="Advance a strategy's status")
    p_promote.add_argument("hash_prefix")
    p_promote.add_argument("--to", required=True, choices=promote_targets)
    p_promote.add_argument(
        "--paper-outcome",
        dest="paper_outcome",
        choices=["pass", "fail", "inconclusive"],
        help="Required when --to=real_money_candidate (must be 'pass' on the row)",
    )
    p_promote.add_argument(
        "--reason",
        help="Stored in paper_notes when --to=paper_complete; ignored otherwise",
    )
    p_promote.set_defaults(func=cmd_promote)

    p_archive = sub.add_parser("archive", parents=[common], help="Archive a strategy (terminal)")
    p_archive.add_argument("hash_prefix")
    p_archive.add_argument("--reason", required=True)
    p_archive.set_defaults(func=cmd_archive)

    p_recon = sub.add_parser(
        "reconcile",
        parents=[common],
        help="Re-classify stored fast evaluations against current scoring logic",
    )
    p_recon.set_defaults(func=cmd_reconcile)

    p_back = sub.add_parser(
        "backfill",
        parents=[common],
        help="Import historical results/ artifacts into the leaderboard",
    )
    p_back.add_argument(
        "--results-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "results",
        help="Path to the results/ directory to walk (default: repo results/)",
    )
    p_back.set_defaults(func=cmd_backfill)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
