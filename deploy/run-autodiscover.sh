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

# Non-blocking lock: if a run (manual or a previous nightly) is still going,
# skip cleanly rather than piling on / corrupting leaderboard.db.
exec 9>"$LOCK" || { echo "$(date -Is) autodiscover: cannot open lock $LOCK"; exit 1; }
if ! flock -n 9; then
    echo "$(date -Is) autodiscover: another run holds the lock — skipping this cycle."
    exit 0
fi

echo "===== $(date -Is) autodiscover START (n=18, cost-ceiling=\$0.50, fast-only) ====="
venv/bin/python scripts/autodiscover.py \
    --n 18 \
    --cost-ceiling 0.50 \
    --fast-only \
    --summary "$LOG_SUMMARY"
rc=$?
echo "===== $(date -Is) autodiscover END (exit=$rc) ====="
exit "$rc"
