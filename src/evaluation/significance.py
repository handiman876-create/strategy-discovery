"""Statistical significance: bootstrap CI on profit factor + random-entry baseline.

bootstrap_profit_factor
-----------------------
Resample trades with replacement; compute PF for each resample; return point
estimate + 5%/95% percentile CI. PF caps at PF_CAP when a resample contains
no losing trades; we report the fraction capped so callers can sanity-check.

random_baseline (for Casper-style intraday strategies)
------------------------------------------------------
Construction (per Q3 spec):
  * For each session, pick N random 5-min RTH bars uniformly (N = number of
    actual strategy entries in that session). If the strategy made no entries
    that session, the baseline makes none either.
  * Each pick gets a 50/50 random direction (long or short).
  * Exit logic identical to the strategy (Casper): opposite-bracket stop on
    that session's OR (first 5-min bar's high/low), target at risk × rr_ratio,
    EOD forced-exit at 15:50 ET.
  * Run M trials with different RNG seeds → distribution of PFs.
  * p-value = fraction of baseline PFs ≥ strategy PF.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from typing import Sequence

import numpy as np
import pandas as pd

from engine.portfolio import Trade

PF_CAP = 100.0


# ── Bootstrap PF ─────────────────────────────────────────────────────────────


@dataclass
class BootstrapResult:
    point_estimate: float
    ci_lower: float
    ci_upper: float
    n_resamples: int
    fraction_capped: float


def profit_factor(pnls: Sequence[float], cap: float = PF_CAP) -> float:
    wins = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    if losses <= 0:
        return cap if wins > 0 else 0.0
    return wins / losses


def bootstrap_profit_factor(
    trades: Sequence[Trade],
    n_resamples: int = 5000,
    seed: int = 0,
    ci_low: float = 0.05,
    ci_high: float = 0.95,
) -> BootstrapResult:
    if not trades:
        return BootstrapResult(0.0, 0.0, 0.0, 0, 0.0)

    pnls = np.array([t.pnl for t in trades], dtype=float)
    n = len(pnls)
    rng = np.random.default_rng(seed)
    indices = rng.integers(0, n, size=(n_resamples, n))

    pfs = np.empty(n_resamples)
    capped = 0
    for i in range(n_resamples):
        sample = pnls[indices[i]]
        wins = sample[sample > 0].sum()
        losses = -sample[sample < 0].sum()
        if losses <= 0:
            pfs[i] = PF_CAP if wins > 0 else 0.0
            capped += 1
        else:
            pfs[i] = wins / losses

    return BootstrapResult(
        point_estimate=float(profit_factor(pnls.tolist())),
        ci_lower=float(np.quantile(pfs, ci_low)),
        ci_upper=float(np.quantile(pfs, ci_high)),
        n_resamples=n_resamples,
        fraction_capped=capped / n_resamples,
    )


# ── Random baseline ──────────────────────────────────────────────────────────


@dataclass
class BaselineResult:
    strategy_pf: float
    baseline_pfs: list[float]
    p_value: float       # P(baseline_pf >= strategy_pf)
    median_baseline_pf: float
    n_trials: int


def trade_count_warning(n_trades: int, threshold: int = 100) -> str | None:
    if n_trades < threshold:
        return (
            f"Under-sampled: {n_trades} trades < threshold {threshold}; "
            f"results are less statistically reliable."
        )
    return None


def random_baseline(
    bars: pd.DataFrame,
    strategy_trades: list[Trade],
    *,
    rr_ratio: float = 2.0,
    eod_exit: time = time(15, 50),
    m_trials: int = 200,
    seed: int = 0,
    slippage: float = 0.01,
    realistic_fills: bool = True,
) -> BaselineResult:
    """Random baseline that holds exit logic constant; only entry timing
    and direction are randomized."""
    strat_pf = profit_factor([t.pnl for t in strategy_trades])

    # entries-per-session derived from the actual strategy run
    if strategy_trades:
        per_session = (
            pd.Series([t.entry_time.date() for t in strategy_trades])
            .value_counts()
            .to_dict()
        )
    else:
        per_session = {}

    # Pre-group bars by session for fast lookup.
    bars = bars.copy()
    bars["session"] = bars["timestamp"].dt.date
    by_session = {sess: g.reset_index(drop=True) for sess, g in bars.groupby("session", sort=False)}

    baseline_pfs: list[float] = []
    rng = np.random.default_rng(seed)
    for trial in range(m_trials):
        pnls: list[float] = []
        for session_date, n_entries in per_session.items():
            session_bars = by_session.get(session_date)
            if session_bars is None or len(session_bars) < 2:
                continue
            or_bar = session_bars.iloc[0]
            or_high, or_low = float(or_bar["high"]), float(or_bar["low"])
            # Eligible entry bars: all RTH bars after the OR bar, before EOD.
            eligible = session_bars.iloc[1:].copy()
            eligible = eligible[eligible["timestamp"].dt.time < eod_exit]
            if len(eligible) == 0:
                continue
            n_pick = min(n_entries, len(eligible))
            picks = rng.choice(len(eligible), size=n_pick, replace=False)
            for idx in picks:
                entry_bar = eligible.iloc[idx]
                direction = "long" if rng.random() < 0.5 else "short"
                pnl = _simulate_baseline_trade(
                    direction=direction,
                    entry_bar=entry_bar,
                    or_high=or_high,
                    or_low=or_low,
                    rr_ratio=rr_ratio,
                    eod_exit=eod_exit,
                    later_bars=eligible[
                        eligible["timestamp"] > entry_bar["timestamp"]
                    ],
                    slippage=slippage,
                    realistic_fills=realistic_fills,
                )
                if pnl is not None:
                    pnls.append(pnl)
        baseline_pfs.append(profit_factor(pnls))

    if not baseline_pfs:
        p_value = 1.0
        median = 0.0
    else:
        ge = sum(1 for pf in baseline_pfs if pf >= strat_pf)
        p_value = ge / len(baseline_pfs)
        median = float(np.median(baseline_pfs))

    return BaselineResult(
        strategy_pf=strat_pf,
        baseline_pfs=baseline_pfs,
        p_value=p_value,
        median_baseline_pf=median,
        n_trials=m_trials,
    )


def _simulate_baseline_trade(
    *,
    direction: str,
    entry_bar: pd.Series,
    or_high: float,
    or_low: float,
    rr_ratio: float,
    eod_exit: time,
    later_bars: pd.DataFrame,
    slippage: float,
    realistic_fills: bool,
) -> float | None:
    """Mirrors the engine's market-entry + opposite-bracket-stop + RR-target +
    EOD logic for a single random entry."""
    # Entry fills at the close of `entry_bar` (random baseline fills "in-bar"
    # at the chosen bar's close, since there's no "next bar" lookup machinery
    # and we're modelling a counterfactual entry, not a live signal).
    entry_close = float(entry_bar["close"])
    entry_price = (
        entry_close + slippage if direction == "long" else entry_close - slippage
    )

    if direction == "long":
        stop_price = or_low
        risk = entry_price - stop_price
        if risk <= 0:
            return None
        target_price = entry_price + risk * rr_ratio
    else:
        stop_price = or_high
        risk = stop_price - entry_price
        if risk <= 0:
            return None
        target_price = entry_price - risk * rr_ratio

    if later_bars.empty:
        # EOD on entry bar
        exit_price = entry_close
        return _pnl(direction, entry_price, exit_price, slippage, "eod", realistic_fills)

    for _, b in later_bars.iterrows():
        # Stop / target — applied intra-bar exactly as engine does.
        if direction == "long":
            if float(b["low"]) <= stop_price:
                return _pnl(direction, entry_price, stop_price, slippage, "stop", realistic_fills)
            if float(b["high"]) >= target_price:
                return _pnl(direction, entry_price, target_price, slippage, "target", realistic_fills)
        else:
            if float(b["high"]) >= stop_price:
                return _pnl(direction, entry_price, stop_price, slippage, "stop", realistic_fills)
            if float(b["low"]) <= target_price:
                return _pnl(direction, entry_price, target_price, slippage, "target", realistic_fills)
        if b["timestamp"].time() >= eod_exit:
            return _pnl(direction, entry_price, float(b["close"]), slippage, "eod", realistic_fills)

    # No exit triggered → close at last available bar's close (EOD-ish)
    last_close = float(later_bars.iloc[-1]["close"])
    return _pnl(direction, entry_price, last_close, slippage, "eod", realistic_fills)


def _pnl(
    direction: str,
    entry_price: float,
    raw_exit_price: float,
    slippage: float,
    reason: str,
    realistic_fills: bool,
) -> float:
    # Slippage policy mirrors engine.execution.apply_exit_slippage:
    #   - In realistic mode, slippage is applied to all exits.
    #   - In regression mode, stop/target fills get no slippage.
    if not realistic_fills and reason in ("stop", "target"):
        exit_price = raw_exit_price
    elif direction == "long":
        exit_price = raw_exit_price - slippage
    else:
        exit_price = raw_exit_price + slippage
    sign = 1.0 if direction == "long" else -1.0
    return (exit_price - entry_price) * sign
