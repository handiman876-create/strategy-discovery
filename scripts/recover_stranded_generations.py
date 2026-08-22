#!/usr/bin/env python3
"""Re-validate generations that were stranded by a spec-recovery gap, and
optionally fast-screen the ones that now parse — with no new API calls.

WHY THIS EXISTS
---------------
Every generation attempt, successful or not, is archived to
results/generations/*.json with the model's verbatim `raw_tool_input`. When a
stringify quirk outruns the safety net, those archives are not garbage: they
hold fully-formed strategy specs that were rejected on a serialization detail,
already paid for.

2026-08-19 → 08-21: Sonnet 4.6 began JSON-encoding `position_sizing`, which the
then-hand-written field list in spec_recovery did not cover. 99 generations
were rejected across three nights at a cost of ~$1.93. Once the helper derives
its field set from the model (see spec_recovery._structured_fields), those
specs validate — so they can be recovered and screened for free rather than
regenerated.

This is a general recovery tool, not a one-off backfill: any future stringify
gap strands generations the same way, and re-running this with a date window
recovers them. Deliberately NOT a one-shot script that hardcodes the
position_sizing case (see stored_values_age_relative_to_logic in the project
memory — recovery re-derives from source data, it does not patch a symptom).

USAGE
-----
  # Dry run: how many stranded generations validate under current logic?
  recover_stranded_generations.py --since 2026-08-19

  # Same, then translate + fast-screen the recovered specs (runs backtests).
  recover_stranded_generations.py --since 2026-08-19 --evaluate

Dry run is the default and touches nothing: no API calls, no backtests, no
leaderboard writes. --evaluate runs one fast evaluation per recovered spec and
records eval_type='fast' rows, exactly as the nightly loop would.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT))
# NOTE: do NOT put scripts/ on sys.path — scripts/leaderboard.py shadows the
# `leaderboard` package and breaks every import below it. autodiscover.py is
# loaded by explicit path instead (see below).

from dotenv import load_dotenv

from evaluation.baskets import FAST_BASKET, KNOWN_BASKETS, basket_identity
from evaluation.fast_pipeline import run_fast_evaluation
from generator.dedup import compute_strategy_hash
from generator.paths import generations_dir
from generator.spec import StrategySpec
from generator.translator import TranslationError, translate_to_file
from leaderboard.adapters import to_generation_metadata
from leaderboard.db import initialize_db
from leaderboard.record import record_generation

# Reuse the nightly loop's class loader and backtest config rather than
# restating them: a recovered spec must be screened under EXACTLY the same
# conditions as a freshly generated one, or its fast row is not comparable to
# the rows already in the leaderboard.
_ad_spec = importlib.util.spec_from_file_location(
    "autodiscover_script", _ROOT / "scripts" / "autodiscover.py"
)
_ad = importlib.util.module_from_spec(_ad_spec)
_ad_spec.loader.exec_module(_ad)
_load_class, _cfg = _ad._load_class, _ad._cfg

SPEND_LEDGER = _ROOT / "results" / "api_spend.json"


class _ArchivedLog:
    """Re-hydrates the fields to_generation_metadata reads from a GenerationLog.

    The recovered spec never went through generate_and_translate, so no
    strategies row exists for it — and record_evaluation has a foreign-key
    dependency on that row, which is why an un-recorded recovery logs
    "cannot record evaluation: strategy ... not found" and silently keeps the
    fast result out of the leaderboard.
    """

    def __init__(self, payload: dict, path: Path):
        self.timestamp = payload.get("timestamp")
        self.model = payload.get("model", "unknown")
        self.prompt_hash = payload.get("prompt_hash", "")
        self.actual_cost_usd = float(payload.get("actual_cost_usd") or 0.0)
        self.raw_response_path = str(path)


def _billed_call_ids() -> set[str]:
    """Every call_id the SpendTracker has recorded, i.e. every generation the
    API actually billed us for.

    This is the only reliable way to tell a production generation from a test
    fixture: pytest writes fixture records into results/generations/ with a
    plausible model, call_id, and actual_cost_usd, so nothing inside the record
    itself distinguishes them. The ledger is written only by real API calls.
    (The pollution is a separate repo issue — the tests should be writing to a
    tmp dir. Until they do, recovery has to filter.)
    """
    try:
        data = json.loads(SPEND_LEDGER.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"WARN spend ledger unreadable ({e}); cannot separate production "
              f"generations from test fixtures — treating all records as billed",
              flush=True)
        return set()
    ids: set[str] = set()
    for month in data.get("months", {}).values():
        for rows in month.values():
            if not isinstance(rows, list):
                continue
            for row in rows:
                if isinstance(row, dict) and row.get("call_id"):
                    ids.add(row["call_id"])
    return ids


def _iter_records(since: str | None, until: str | None):
    """Yield (path, payload) for archived generations in [since, until].

    Filenames are ISO timestamps (2026-08-21T07-08-50.396003+00-00_arch...),
    so a lexical compare on the leading 10 characters is a date filter.
    """
    for path in sorted(generations_dir().glob("*.json")):
        day = path.name[:10]
        if since and day < since:
            continue
        if until and day > until:
            continue
        try:
            yield path, json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(f"SKIP {path.name}: unreadable ({e})", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=None, metavar="YYYY-MM-DD",
                    help="Only consider generations archived on or after this date.")
    ap.add_argument("--until", default=None, metavar="YYYY-MM-DD",
                    help="Only consider generations archived on or before this date.")
    ap.add_argument("--evaluate", action="store_true",
                    help="Translate and fast-screen the recovered specs. Runs one "
                         "backtest sweep per spec and writes eval_type='fast' rows. "
                         "Without this flag the script only reports what WOULD be "
                         "recovered and exits.")
    ap.add_argument("--basket", default=None, choices=sorted(KNOWN_BASKETS),
                    help="Symbol basket for the fast screen. Must match the basket "
                         "the nightly loop uses or the ci_lower numbers are not "
                         "comparable. Defaults to FAST_BASKET.")
    ap.add_argument("--include-unbilled", action="store_true",
                    help="Also consider records whose call_id is absent from "
                         "results/api_spend.json. OFF by default: the test suite "
                         "writes fixture records into results/generations/ (26 "
                         "landed there during one pytest run on 2026-08-21) and "
                         "they carry a plausible-looking actual_cost_usd, so cost "
                         "cannot tell them apart from real generations. The spend "
                         "ledger can: it holds only call_ids the API actually "
                         "billed. That is also the right definition of a record "
                         "worth recovering — one we already paid for.")
    ap.add_argument("--timeframes", default=None, metavar="1d,1h",
                    help="Comma-separated timeframes to evaluate; others are counted "
                         "and skipped. Fast-eval cost is wildly non-uniform by bar "
                         "size (measured 2026-08-05: 1d ~8s, 1h ~8s, 15m ~162s, "
                         "5m ~684s, 1m ~2112s per spec), so a mixed batch is "
                         "dominated by its intraday tail — 26 5m specs are ~5 hours "
                         "while 63 daily/hourly specs are ~12 minutes. Split the "
                         "batch when wall-clock matters.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Stop after this many recovered specs (0 = no limit). "
                         "Applies to evaluation, not to the scan.")
    args = ap.parse_args()

    load_dotenv(_ROOT / ".env", override=True)

    scanned = 0
    recovered: list[tuple[Path, StrategySpec, dict]] = []
    still_failing: Counter[str] = Counter()
    was_already_valid = 0
    skipped_unbilled = 0
    recovered_cost = 0.0
    billed = set() if args.include_unbilled else _billed_call_ids()

    for path, payload in _iter_records(args.since, args.until):
        cost = float(payload.get("actual_cost_usd") or 0.0)
        if billed and payload.get("call_id") not in billed:
            skipped_unbilled += 1
            continue
        raw = payload.get("raw_tool_input")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = None
        if not isinstance(raw, dict):
            continue
        scanned += 1

        # A record that already produced a spec was never stranded; count it
        # separately so the recovery rate is not inflated by past successes.
        if payload.get("spec") is not None:
            was_already_valid += 1
            continue

        try:
            # model_validate runs the mode="before" validator, which is where
            # the generalized recovery lives — the same code path the live
            # generator uses. Passing a dict copy keeps the archive untouched.
            # record_quirks=False: replaying an archive is not a fresh
            # observation of the model, and counting it would inflate the very
            # series we use to judge whether the safety net still earns its keep.
            spec = StrategySpec.model_validate(
                dict(raw),
                context={"model": payload.get("model", "unknown"),
                         "record_quirks": False},
            )
        except Exception as e:
            still_failing[" ".join(str(e).split())[:120]] += 1
            continue
        recovered.append((path, spec, payload))
        recovered_cost += cost

    print(f"SCAN dir={generations_dir()} window={args.since or 'any'}..{args.until or 'any'}")
    print(f"SCAN records={scanned} already_valid={was_already_valid} "
          f"stranded={scanned - was_already_valid}"
          + (f" (skipped {skipped_unbilled} unbilled/test-fixture record(s))"
             if skipped_unbilled else ""))
    print(f"RECOVERED {len(recovered)} spec(s) now validate under current logic "
          f"— ${recovered_cost:.4f} of already-billed generation reclaimed")

    # Dedup by structural hash: the retry loop makes up to 3 attempts per
    # candidate and they are often near-identical, so the raw recovered count
    # overstates how many distinct strategies are actually on the table.
    by_hash: dict[str, tuple[Path, StrategySpec, dict]] = {}
    unhashable = 0
    for path, spec, payload in recovered:
        try:
            by_hash.setdefault(compute_strategy_hash(spec), (path, spec, payload))
        except Exception:
            unhashable += 1
    print(f"UNIQUE {len(by_hash)} distinct structural hash(es)"
          + (f" ({unhashable} unhashable)" if unhashable else ""))

    if still_failing:
        print(f"STILL-FAILING {sum(still_failing.values())} record(s), by reason:")
        for reason, n in still_failing.most_common():
            print(f"  {n:4d}  {reason}")

    if not args.evaluate:
        print("\nDRY RUN — no backtests, no leaderboard writes, no API calls.")
        print("Re-run with --evaluate to translate and fast-screen the "
              f"{len(by_hash)} unique recovered spec(s).")
        return 0

    fast_basket = KNOWN_BASKETS[args.basket] if args.basket else FAST_BASKET
    basket_label, basket_h = basket_identity(fast_basket)
    print(f"\nBASKET fast={basket_label} ({basket_h}) symbols={fast_basket}", flush=True)
    conn = initialize_db(str(_ROOT / "db" / "leaderboard.db"))

    want_tf = None
    if args.timeframes:
        want_tf = {t.strip() for t in args.timeframes.split(",") if t.strip()}
        deferred = [s for _, s, _p in by_hash.values() if not (set(s.timeframes) & want_tf)]
        if deferred:
            # No silent caps: say what was left out and why, or the run reads as
            # "screened everything" when it screened a subset.
            import collections
            by = collections.Counter(tuple(s.timeframes) for s in deferred)
            print(f"DEFERRED {len(deferred)} spec(s) outside --timeframes="
                  f"{sorted(want_tf)}: {dict(by)}", flush=True)

    screened = skipped_tf = 0
    for h, (path, spec, payload) in by_hash.items():
        if want_tf and not (set(spec.timeframes) & want_tf):
            skipped_tf += 1
            continue
        if args.limit and screened >= args.limit:
            print(f"LIMIT reached ({args.limit}); {len(by_hash) - screened} spec(s) "
                  f"left unscreened", flush=True)
            break
        try:
            code_path = translate_to_file(spec, overwrite=True)
        except TranslationError as e:
            print(f"XLATE-FAIL {spec.name} hash={h[:12]}: {str(e)[:120]}", flush=True)
            continue
        # Provenance: imported_from marks these as RECOVERED rather than
        # freshly generated, so the leaderboard does not later imply the
        # nightly loop produced them on 2026-08-21. Bound once and applied to
        # BOTH the generation row and the eval row below — tagging only the
        # generation row (the state before 2026-08-22) left evaluations.
        # imported_from NULL, indistinguishable from a nightly eval, so the
        # only discriminator was the evaluated_at window.
        provenance = f"recovered:{path.name}"
        try:
            record_generation(
                conn, spec, h,
                to_generation_metadata(
                    [_ArchivedLog(payload, path)],
                    archetype=spec.archetype,
                    spec_path=str(code_path),
                ),
                imported_from=provenance,
            )
        except Exception as e:
            print(f"LB-WARN {spec.name} hash={h[:12]}: generation row not written "
                  f"({str(e)[:100]}); fast row will not persist", flush=True)
        try:
            cls = _load_class(code_path, spec.name)
            fast = run_fast_evaluation(cls, backtest_config=_cfg(), conn=conn,
                                       strategy_hash=h, symbols=fast_basket,
                                       imported_from=provenance)
        except Exception as e:
            print(f"FAST-ERROR {spec.name} hash={h[:12]}: {str(e)[:120]}", flush=True)
            continue
        screened += 1
        print(f"RECOV {spec.name} hash={h[:12]} tf={list(spec.timeframes)} "
              f"ci_lower={fast.ci_lower:.3f} fast_pf={fast.median_pf:.2f} "
              f"fast_score={fast.breakdown.score:.2f} "
              f"trades={fast.n_oos_trades_total} src={path.name}", flush=True)

    conn.close()
    print(f"\nDONE screened={screened} of {len(by_hash)} unique recovered spec(s)"
          + (f", {skipped_tf} deferred by --timeframes" if skipped_tf else ""))
    # Promotion is NOT automatic. ci_lower > 1.0 on a fast screen is the
    # generator's promotion gate (see project_generator_screens_on_ci_lower),
    # and promoting to canonical is a spend decision that stays with a human.
    print("Promotion to canonical is a manual decision — nothing was promoted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
