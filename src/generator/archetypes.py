"""Archetype definitions matching DESIGN.md §4.

Each archetype carries:
  * thesis statement
  * academic / practitioner references
  * allowed asset classes and timeframes
  * 1-2 positive examples (real strategies that fit the archetype)
  * 2-3 counter-examples (strategies that LOOK adjacent but don't fit)

Counter-examples are critical for the prompt: they tell Claude what NOT to
generate when given this archetype.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ArchetypeDefinition:
    name: str
    thesis: str
    references: list[str]
    allowed_assets: list[str]
    allowed_timeframes: list[str]
    examples: list[str]
    counter_examples: list[str]
    notes: str = ""


ARCHETYPES: dict[str, ArchetypeDefinition] = {
    "mean_reversion": ArchetypeDefinition(
        name="mean_reversion",
        thesis=(
            "Asset prices that move strongly in one direction over a short window tend to "
            "partially revert. Buy weakness in established uptrends; sell strength in "
            "established downtrends."
        ),
        references=[
            "Larry Connors, 'Short Term Trading Strategies That Work' (2008)",
            "Jegadeesh (1990), 'Evidence of predictable behavior of security returns'",
        ],
        allowed_assets=["stocks", "crypto"],
        allowed_timeframes=["1h", "1d"],
        examples=[
            "RSI(2) < 5 AND close > SMA(200) → buy; exit when RSI(2) > 70 or N days later.",
            "Z-score of close vs 20-day SMA < -2.0 AND ROC(10) > 0 → buy.",
        ],
        counter_examples=[
            "Trend-following: 'buy when 50-day MA crosses above 200-day MA' is momentum, not mean reversion.",
            "Volatility breakout: 'buy when close exceeds 20-day high' is breakout, not reversion.",
            "Time-of-day rules with no reversion logic (that's seasonality or microstructure).",
        ],
        notes=(
            "Mean-reversion strategies live on lower-frequency bars (1h/1d) where the "
            "reversion has time to materialize. Avoid 5m unless reversion-window is in "
            "minutes (microstructure)."
        ),
    ),
    "momentum": ArchetypeDefinition(
        name="momentum",
        thesis=(
            "Assets that have outperformed continue to outperform over similar windows. "
            "Persistent trends in returns are exploitable on the time horizon of the "
            "trend itself."
        ),
        references=[
            "Jegadeesh & Titman (1993), 'Returns to Buying Winners and Selling Losers'",
            "Asness, Moskowitz, Pedersen (2013), 'Value and Momentum Everywhere'",
        ],
        allowed_assets=["stocks", "crypto"],
        allowed_timeframes=["1d"],
        examples=[
            "ROC(126) (6-month) > 10% AND ROC(21) > 0 → long; rebalance monthly.",
            "Close > SMA(50) AND SMA(50) > SMA(200) → long; exit on cross-down.",
        ],
        counter_examples=[
            "Mean reversion: 'buy when RSI < 30' is the OPPOSITE of momentum.",
            "Volatility breakout based on price levels, not return persistence.",
            "Intraday strategies (timeframes 5m/15m/1h) — momentum on those scales is microstructure.",
        ],
    ),
    "volatility_breakout": ArchetypeDefinition(
        name="volatility_breakout",
        thesis=(
            "Periods of compressed volatility precede directional breakouts. Enter on "
            "range expansion, scaled by ATR, exit on opposite signal or stop."
        ),
        references=[
            "Donchian channels (Richard Donchian, 1960s)",
            "Turtle Traders (Dennis & Eckhardt rules, 1983)",
            "John Bollinger, 'Bollinger on Bollinger Bands' (2001)",
        ],
        allowed_assets=["stocks", "crypto"],
        allowed_timeframes=["1h", "1d"],
        examples=[
            "Close > MAX(high, 20) AND ATR(14) > rolling-mean ATR(50) → long with ATR-based stop.",
            "Close > BB_upper(20, 2.0) AND ATR(14) is rising → long.",
        ],
        counter_examples=[
            "Mean reversion against the band (e.g. buy when close < BB_lower) is reversion, not breakout.",
            "Pure momentum without an ATR / volatility trigger.",
            "Microstructure scalps that use intraday session structure (separate archetype).",
        ],
    ),
    "seasonality": ArchetypeDefinition(
        name="seasonality",
        thesis=(
            "Specific calendar effects (day-of-week, month-end, holidays) create "
            "exploitable patterns due to structural flows: dividends, rebalancing, "
            "tax-loss harvesting, retirement contributions."
        ),
        references=[
            "'Sell in May and Go Away' — Bouman & Jacobsen (2002)",
            "Turn-of-month effect — Lakonishok & Smidt (1988)",
        ],
        allowed_assets=["stocks"],
        allowed_timeframes=["1d"],
        examples=[
            "Long SPY on the last 5 trading days of each month, exit on the 1st.",
            "Long QQQ on Mondays where Friday's close was up; exit Tuesday close.",
        ],
        counter_examples=[
            "Strategies that only use indicators and have no calendar trigger.",
            "Time-of-day intraday rules (those are microstructure).",
            "Random date conditions with no plausible flow-based explanation.",
        ],
        notes=(
            "DSL note: time_of_day operates on minutes since midnight ET. For day-of-week "
            "or day-of-month rules, those are not yet expressible in the DSL — Phase 3 "
            "leaves seasonality LIMITED to time-of-day-able patterns. Day-of-week comes "
            "in Phase 3.5."
        ),
    ),
    "pairs": ArchetypeDefinition(
        name="pairs",
        thesis=(
            "Cointegrated pairs revert to their mean spread. Long the underperformer / "
            "short the outperformer when the spread crosses N standard deviations."
        ),
        references=[
            "Gatev, Goetzmann, Rouwenhorst (2006), 'Pairs Trading'",
        ],
        allowed_assets=["stocks"],
        allowed_timeframes=["1d"],
        examples=[
            "Pair: KO/PEP. Z-score of (price_KO - hedge_ratio * price_PEP) > 2 → short pair.",
        ],
        counter_examples=[
            "Single-symbol strategies (those are mean reversion).",
            "Strategies that compare to an INDEX rather than a specific paired asset.",
        ],
        notes=(
            "DEFERRED to Phase 3.5: the engine is single-symbol; pair trading needs "
            "multi-symbol position management. The generator will reject pairs specs "
            "until that lands. Listed here so the framework is complete."
        ),
    ),
    "microstructure": ArchetypeDefinition(
        name="microstructure",
        thesis=(
            "Institutional order flow creates predictable intraday patterns (open auction "
            "imbalance, opening-range breakouts, closing imbalance, lunch-hour drift). "
            "Strategies fit a single trading day."
        ),
        references=[
            "O'Hara, 'Market Microstructure Theory' (1995)",
            "Casper-style opening-range scalping is in this family.",
        ],
        allowed_assets=["stocks"],
        allowed_timeframes=["5m", "15m"],
        examples=[
            "Opening range = first 30 min. Long on confirmed breakout above OR.high; "
            "stop at OR.low; exit at 15:50 ET. (This is Casper.)",
            "Long the gap-down: if open < prev close - 1*ATR, buy at 10:00, exit by 14:00.",
        ],
        counter_examples=[
            "Daily-bar momentum or reversion (those have their own archetypes).",
            "Strategies that hold overnight (microstructure typically exits before close).",
            "Strategies that don't use time-of-day at all.",
        ],
    ),
    "overnight_session": ArchetypeDefinition(
        name="overnight_session",
        thesis=(
            "Overnight returns differ systematically from intraday returns due to news, "
            "foreign markets, retail flow patterns. Buy near close / sell near open, or "
            "the reverse, based on which side carries the historical edge."
        ),
        references=[
            "Lou, Polk, Skouras (2019), 'A Tug of War: Overnight versus Intraday'",
        ],
        allowed_assets=["stocks"],
        allowed_timeframes=["1d"],
        examples=[
            "Long SPY at close, sell at next open. Net P&L = open - close (overnight gap).",
            "Conditional version: only enter overnight long if today's intraday return < 0.",
        ],
        counter_examples=[
            "Intraday-only strategies (those are microstructure).",
            "Multi-day holding strategies (those are momentum or seasonality).",
        ],
        notes=(
            "Engine note: overnight strategies need the engine to support 'enter at "
            "close, exit at next open'. For Phase 3, generators may produce overnight "
            "specs but the translator emits a placeholder that runs only on bars where "
            "is_session_end() is True, with EOD exit."
        ),
    ),
}


def get_archetype(name: str) -> ArchetypeDefinition:
    if name not in ARCHETYPES:
        raise ValueError(f"unknown archetype {name!r}; valid: {sorted(ARCHETYPES)}")
    return ARCHETYPES[name]


def all_archetype_names() -> list[str]:
    return sorted(ARCHETYPES.keys())
