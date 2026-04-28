"""Behavioral dedup: hash a strategy by the trade-list it produces on the
fixture. Two textually different specs that produce identical trades on the
fixture are duplicates.

Hash signature: SHA-256 over a sorted list of (entry_time_iso, side, exit_reason, round(pnl, 4))
tuples. Sorted entry_time gives stability against trade-iteration-order quirks.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Type

from engine.backtester import BacktestConfig, run_backtest
from engine.session import CryptoSession, RegularTradingHours
from strategy.base import Strategy

from .fixture import fixture_for_timeframe


def behavioral_hash(
    strategy_class: Type[Strategy],
    *,
    timeframe: str | None = None,
    starting_capital: float = 10_000.0,
    slippage: float = 0.01,
) -> str:
    """Run `strategy_class` on the fixture and return SHA-256 of its trade
    fingerprint. Returns the same hash for any spec that produces the same
    trades on the same fixture."""
    tf = timeframe or _infer_timeframe(strategy_class)
    bars = fixture_for_timeframe(tf)
    asset_class = "stocks" if "stocks" in getattr(strategy_class, "supported_assets", []) else "crypto"
    cfg = BacktestConfig(
        starting_capital=starting_capital,
        commission=0.0,
        slippage=slippage,
        realistic_fills=True,
        session=RegularTradingHours() if asset_class == "stocks" else CryptoSession(),
    )
    result = run_backtest(strategy_class.__name__, bars, strategy_class(), cfg)

    fingerprint: list[tuple[str, str, str, float]] = []
    for t in result.trades:
        fingerprint.append(
            (
                t.entry_time.isoformat(),
                t.side,
                t.exit_reason,
                round(t.pnl, 4),
            )
        )
    fingerprint.sort()
    payload = json.dumps(fingerprint, sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def _infer_timeframe(strategy_class: Type[Strategy]) -> str:
    tfs = getattr(strategy_class, "timeframes", None) or []
    if not tfs:
        raise ValueError(f"{strategy_class.__name__} has no timeframes declared")
    # Pick the lowest-frequency one for the fixture (we have 5m raw → resample up).
    priority = {"1d": 4, "1h": 3, "15m": 2, "5m": 1}
    return sorted(tfs, key=lambda t: priority.get(t, 0))[-1]
