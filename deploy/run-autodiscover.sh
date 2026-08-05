#!/usr/bin/env bash
#
# Nightly strategy autodiscovery wrapper (invoked by strategy-discovery.service).
#
#   - Generation + fast eval ONLY (--fast-only); never runs the expensive
#     canonical stage — that stays a manual, reviewed step.
#   - flock so a nightly run can never overlap a manual run (shared DB safety).
#   - Bounded spend via --cost-ceiling.
#
set -uo pipefail

REPO="/root/strategy-discovery"
cd "$REPO"

LOCK="$REPO/autodiscover.lock"
LOG_SUMMARY="$REPO/logs/autodiscover_summary.json"
DATED_SUMMARY="$REPO/logs/autodiscover_summary_$(date -u +%Y%m%d).json"

# Marker whose mtime is the run's start. The dated copy below is made ONLY if
# the summary is newer than this — see the guard for why that matters.
START_MARKER="$(mktemp)"
trap 'rm -f "$START_MARKER"' EXIT

# Non-blocking lock: if a run (manual or a previous nightly) is still going,
# skip cleanly rather than piling on / corrupting leaderboard.db.
exec 9>"$LOCK" || { echo "$(date -Is) autodiscover: cannot open lock $LOCK"; exit 1; }
if ! flock -n 9; then
    echo "$(date -Is) autodiscover: another run holds the lock — skipping this cycle."
    exit 0
fi

echo "===== $(date -Is) autodiscover START (n=20, cost-ceiling=\$0.60, fast-only) ====="
venv/bin/python scripts/autodiscover.py \
    --n 20 \
    --cost-ceiling 0.60 \
    --fast-only \
    --summary "$LOG_SUMMARY"
rc=$?          # MUST stay adjacent to the python call — anything between here
               # and the assignment clobbers $? and the service reports success
               # on a failed run (the exact defect commit 531ed6c fixed).

# Dated snapshot, so each night is preserved instead of overwriting the last —
# this is what makes run-over-run ci_lower trend analysis possible at all.
#
# GUARDED on the summary being newer than the run's start. autodiscover writes
# the summary incrementally via flush(), so a run that dies before the first
# flush leaves the PREVIOUS run's file sitting there untouched. An unguarded cp
# would then stamp yesterday's results with today's date — silently fabricating
# a data point, which is worse for trend analysis than having no data point.
# Copied regardless of rc: a run that failed partway still produced real
# candidates worth keeping, and rc is already reported separately below.
if [ -f "$LOG_SUMMARY" ] && [ "$LOG_SUMMARY" -nt "$START_MARKER" ]; then
    cp -- "$LOG_SUMMARY" "$DATED_SUMMARY"
    echo "$(date -Is) autodiscover: dated summary saved -> $DATED_SUMMARY"
else
    echo "$(date -Is) autodiscover: NO dated summary — $LOG_SUMMARY was not" \
         "written by this run (missing, or stale from a previous run)."
fi

echo "===== $(date -Is) autodiscover END (exit=$rc) ====="
exit "$rc"
