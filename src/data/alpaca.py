"""Alpaca data provider — stub for Phase 1.

Retained as a fallback option per DESIGN.md §3 but not actively wired up.
The Phase 1 primary stocks provider is Polygon (src/data/polygon.py).
"""

from __future__ import annotations

from datetime import date

import pandas as pd

from .base import DataProvider


class AlpacaProvider(DataProvider):
    name = "alpaca"

    def fetch_bars(
        self, symbol: str, timeframe: str, start: date, end: date
    ) -> pd.DataFrame:
        raise NotImplementedError(
            "Alpaca provider is a Phase-1 stub. Use PolygonProvider for stocks. "
            "If you need Alpaca, port the auth + IEX-feed logic from the "
            "legacy Phase-1 backtester archive."
        )
