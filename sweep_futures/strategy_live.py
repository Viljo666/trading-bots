"""Live signal detection — faithful to backtest.py's logic.

`detect_entry_on_last_bar` fires ONLY when the just-closed 4h bar completes a
sweep→reclaim setup (i.e. the reclaim happens on the latest closed bar). This
mirrors the backtest exactly so live behaviour matches what we validated.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from config import PARAMS, RISK, StrategyParams


@dataclass
class EntrySignal:
    side: str            # "long" | "short"
    entry: float         # reference price (last close); live we fill at market
    stop: float
    target: float
    risk_pts: float
    qty: int
    swept_level: float
    confirm_ts: pd.Timestamp


def size_position(risk_pts: float, dollars_per_point: float) -> int:
    """Contracts so that hitting the stop ≈ risk_per_trade_pct of equity."""
    if risk_pts <= 0:
        return 0
    dollars_risk = RISK.account_equity_usd * RISK.risk_per_trade_pct
    raw = dollars_risk / (risk_pts * dollars_per_point)
    qty = int(math.floor(raw))
    qty = max(RISK.min_contracts, min(RISK.max_contracts, qty))
    return qty


def detect_entry_on_last_bar(
    bars: pd.DataFrame, dollars_per_point: float, p: StrategyParams = PARAMS
) -> EntrySignal | None:
    """Return an EntrySignal iff the latest CLOSED bar triggers a fresh entry."""
    lb = p.swing_lookback
    n = len(bars)
    if n < max(p.trend_ema, lb) + 2:
        return None

    h = bars["high"].to_numpy()
    l = bars["low"].to_numpy()
    c = bars["close"].to_numpy()
    ts = bars.index
    ema = bars["close"].ewm(span=p.trend_ema, adjust=False).mean().to_numpy()

    ci = n - 1  # latest closed bar = the confirmation candle
    earliest = max(lb, ci - p.confirm_window)
    for s in range(ci, earliest - 1, -1):
        prior_low = l[s - lb:s].min()
        prior_high = h[s - lb:s].max()
        ref_close = c[s - 1]

        bull = l[s] < prior_low and (c[s] > ema[s] or c[s - 1] > ema[s - 1])
        bear = h[s] > prior_high and (c[s] < ema[s] or c[s - 1] < ema[s - 1])

        if bull and c[ci] > ref_close:
            # reclaim must FIRST complete on the latest bar (none earlier in s..ci-1)
            if all(c[k] <= ref_close for k in range(s, ci)):
                stop = float(l[s:ci + 1].min())
                entry = float(c[ci])
                risk = entry - stop
                if risk <= 0:
                    return None
                qty = size_position(risk, dollars_per_point)
                return EntrySignal("long", entry, stop, entry + p.rr * risk,
                                   risk, qty, float(prior_low), ts[ci]) if qty else None
        if bear and c[ci] < ref_close:
            if all(c[k] >= ref_close for k in range(s, ci)):
                stop = float(h[s:ci + 1].max())
                entry = float(c[ci])
                risk = stop - entry
                if risk <= 0:
                    return None
                qty = size_position(risk, dollars_per_point)
                return EntrySignal("short", entry, stop, entry - p.rr * risk,
                                   risk, qty, float(prior_high), ts[ci]) if qty else None
    return None
