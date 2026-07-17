"""Symbol baskets for evaluation, and their identity (label + hash).

WHY THIS MODULE EXISTS
----------------------
A ci_lower is only comparable to another ci_lower drawn from the SAME basket.
Until 2026-07-17 the fast screen ran one hardcoded basket
(`["AMD", "NFLX", "SPY", "QQQ", "NVDA"]`, 3/5 high-beta tech) and no eval row
recorded which basket produced it — so two rows from different baskets looked
identical and silently ranked against each other.

That basket over-promoted beta as signal. Measured on the two strategies that
reached canonical (2026-07-16):

                          d0cc300e5c07      fdc88ceb54fd
    tech5_v1 (fast)         ci 1.054  ->      ci 1.145      both promoted
    diverse8_v1 (fast)      ci 0.218  ->      ci 0.634      both screened out
    sp500_phase2_seed42     ci 0.288  ->      ci 0.963      both FAILED canonical

diverse8_v1 reproduces the canonical verdict at the fast tier; tech5_v1
contradicted it twice. diverse8_v1 is also a strict SUBSET of the canonical
roster, which is why it predicts rather than merely differs — a basket sharing
few names with canonical can diverge from it in either direction.

CENTRALIZATION
--------------
Basket identity is needed in at least three places: the fast pipeline (which
basket to run), the leaderboard adapter (which basket to stamp on a row), and
any query comparing rows across baskets. Deriving it independently in each
would let the label drift from the symbols it names. `basket_identity()` is
the single writer; callers pass symbols and get back the pair.
"""

from __future__ import annotations

import hashlib

# ── Baskets ─────────────────────────────────────────────────────────────────

# The fast screen's basket, as of 2026-07-17. Composition is deliberate: 2
# broad indices (SPY, QQQ), 2 high-beta (NVDA, AMD), 2 financials (BLK, MS),
# 1 low-vol staples (PG), 1 laggard semi (QCOM). PG and QCOM are the names
# that do the work — both failed canonical (PF 0.54 / 0.56) on a strategy the
# old basket rated ci_lower 1.145.
#
# All 8 are cached under data/polygon/ (2021-04-28 -> 2024-12-31, 926 days)
# and all 8 are members of sp500_phase2_seed42, so the fast tier stays a strict
# subset of canonical. Changing these symbols WITHOUT bumping the label below
# silently corrupts cross-run comparison — bump both together.
FAST_BASKET: list[str] = ["AMD", "BLK", "MS", "NVDA", "PG", "QCOM", "QQQ", "SPY"]
FAST_BASKET_LABEL = "diverse8_v1"

# Every basket that has ever produced an eval row. Reverse-looked-up by symbol
# set, so a basket is recognized by WHAT IT CONTAINS, not by what a caller
# claims it is.
#
# tech5_v1 is retired — retained because 221 historical fast rows carry it and
# those rows stay valid (they are honest measurements of that basket); they are
# merely incomparable to diverse8_v1 rows. Do not delete: a NULL basket_version
# would make them unreadable rather than merely stale.
KNOWN_BASKETS: dict[str, list[str]] = {
    "tech5_v1": ["AMD", "NFLX", "NVDA", "QQQ", "SPY"],
    "diverse8_v1": FAST_BASKET,
    "sp500_phase2_seed42": [
        "AMD", "BLK", "MS", "MSFT", "NFLX", "NVDA", "PG", "QCOM", "QQQ", "SPY",
    ],
}


# ── Identity ────────────────────────────────────────────────────────────────


def basket_hash(symbols: list[str]) -> str:
    """Stable 12-char identity of a symbol set, order-independent.

    Sorted before hashing so ["SPY", "QQQ"] and ["QQQ", "SPY"] are the same
    basket — callers pass symbols in whatever order they hold them, and the
    evaluation itself does not depend on order."""
    joined = ",".join(sorted(symbols))
    return hashlib.sha256(joined.encode()).hexdigest()[:12]


def basket_label(symbols: list[str]) -> str:
    """Human-readable name for a symbol set, or `unknown_<hash>` if unregistered.

    The label is what a human reads in a query; the hash is what proves the
    label still matches its symbols. An unregistered basket is not an error —
    an ad-hoc probe is legitimate — but it is marked so it can't be mistaken
    for a blessed roster."""
    target = frozenset(symbols)
    for label, members in KNOWN_BASKETS.items():
        if frozenset(members) == target:
            return label
    return f"unknown_{basket_hash(symbols)}"


def basket_identity(symbols: list[str]) -> tuple[str, str]:
    """(label, hash) for a symbol set — the pair every eval row records.

    Both are stored rather than either alone: the label alone can drift from
    its symbols if someone edits KNOWN_BASKETS in place, and the hash alone is
    unreadable. Stored together, a mismatch is detectable."""
    return basket_label(symbols), basket_hash(symbols)
