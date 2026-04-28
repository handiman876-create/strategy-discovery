"""Symbol rosters for evaluation.

KNOWN LIMITATION: The S&P 500 list below is current membership as of 2026-04,
not point-in-time historical membership. Backtests against this roster may
exhibit survivorship bias — companies that were once S&P 500 members but
have since been delisted or removed are not included. This is an explicit
Phase 2+ trade-off; point-in-time membership is a Phase 5+ enhancement.

For crypto, the top-15 list excludes stablecoins.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

# 50-name subset of the current S&P 500 spanning sectors. Includes the 5 names
# Phase 1 already cached so a seeded sample can land on a known mix.
SP500_SUBSET: list[str] = [
    # tech / semis (high-volatility — what Casper expects to trade well on)
    "AAPL", "MSFT", "AMZN", "GOOGL", "META",
    "NVDA", "AMD", "AVGO", "INTC", "QCOM",
    "ORCL", "CRM", "ADBE", "NFLX", "TSLA",
    # financials / industrials
    "JPM", "BAC", "GS", "MS", "C",
    "BLK", "AXP", "BRK.B", "V", "MA",
    "BA", "CAT", "HON", "GE", "RTX",
    # healthcare
    "JNJ", "PFE", "UNH", "MRK", "LLY",
    "ABBV", "CVS", "TMO", "DHR", "ABT",
    # consumer / energy / index ETFs
    "WMT", "COST", "HD", "PG", "KO",
    "XOM", "CVX", "DIS", "SPY", "QQQ",
]

CRYPTO_TOP_15: list[str] = [
    "XBTUSD",  # BTC
    "ETHUSD",
    "SOLUSD",
    "XRPUSD",
    "ADAUSD",
    "AVAXUSD",
    "LINKUSD",
    "DOTUSD",
    "MATICUSD",
    "ATOMUSD",
    "LTCUSD",
    "BCHUSD",
    "FILUSD",
    "ETCUSD",
    "AAVEUSD",
]


def random_subset(roster: list[str], n: int, seed: int) -> list[str]:
    """Reproducibly draw n distinct symbols from the roster."""
    if n > len(roster):
        raise ValueError(f"requested {n} symbols but roster has only {len(roster)}")
    rng = random.Random(seed)
    return sorted(rng.sample(roster, n))


def sp500_random_subset(n: int = 10, seed: int = 0) -> list[str]:
    return random_subset(SP500_SUBSET, n, seed)


def sp500_with_required(
    required: list[str], n: int = 10, seed: int = 0
) -> list[str]:
    """Return `required` plus (n - len(required)) seeded picks from SP500_SUBSET\\required."""
    if any(s not in SP500_SUBSET for s in required):
        missing = [s for s in required if s not in SP500_SUBSET]
        raise ValueError(f"required symbols not in roster: {missing}")
    pool = [s for s in SP500_SUBSET if s not in required]
    extras = random_subset(pool, n - len(required), seed)
    return sorted(required + extras)


def top_crypto(n: int = 10) -> list[str]:
    return CRYPTO_TOP_15[:n]


def save_symbol_list(symbols: list[str], path: Path, *, seed: int, source: str) -> None:
    """Persist a symbol pick for reproducibility (logs seed + roster source)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "symbols": symbols,
                "seed": seed,
                "source": source,
                "n": len(symbols),
            },
            indent=2,
        )
    )


def load_symbol_list(path: Path) -> list[str]:
    return json.loads(path.read_text())["symbols"]
