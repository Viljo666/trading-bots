"""Live level computation — faithful to backtest.py.

Given ~30 days of 5m bars, compute: prior completed RTH session's POC, the
14-day RTH daily ATR as of that prior session, and today's RTH open. Decide
the continuation side and the full order plan (entry stop @ POC, SL, TP, qty).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from config import ET, PARAMS, RISK, SymbolSpec


@dataclass
class Plan:
    side: str            # "long" | "short"
    poc: float           # entry stop level
    stop: float
    target: float
    risk_pts: float
    qty: int
    day_open: float
    prev_session: object  # date of the POC's session


def session_poc(sess: pd.DataFrame, bin_pts: float) -> float:
    lo = np.floor(sess["low"].min() / bin_pts) * bin_pts
    hi = np.ceil(sess["high"].max() / bin_pts) * bin_pts
    edges = np.arange(lo, hi + bin_pts, bin_pts)
    if len(edges) < 2:
        return float(sess["close"].iloc[-1])
    vol = np.zeros(len(edges) - 1)
    centers = edges[:-1] + bin_pts / 2
    for h, l, v in zip(sess["high"].to_numpy(), sess["low"].to_numpy(), sess["volume"].to_numpy()):
        if v <= 0 or h < l:
            continue
        i0 = max(0, int((l - lo) // bin_pts))
        i1 = min(len(vol) - 1, int((h - lo) // bin_pts))
        vol[i0:i1 + 1] += v / (i1 - i0 + 1)
    return float(centers[int(vol.argmax())])


def daily_atr(rth: pd.DataFrame, period: int) -> pd.Series:
    d = rth.resample("1D").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    tr = pd.concat([d.high - d.low, (d.high - d.close.shift()).abs(),
                    (d.low - d.close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    atr.index = atr.index.date
    return atr


def size_position(risk_pts: float, dollars_per_point: float) -> int:
    if risk_pts <= 0:
        return 0
    dollars = RISK.account_equity_usd * RISK.risk_per_trade_pct
    qty = int(math.floor(dollars / (risk_pts * dollars_per_point)))
    return max(RISK.min_contracts, min(RISK.max_contracts, qty))


def build_plan(bars: pd.DataFrame, spec: SymbolSpec, today) -> Plan | None:
    """bars: 5m OHLCV indexed in ET. today: the ET session date to trade."""
    rth = bars.between_time("09:30", "16:00")
    grouped = dict(rth.groupby(rth.index.date).__iter__())
    # PRIOR sessions need enough bars for a meaningful volume profile; TODAY
    # only needs its opening bar — we arm at 09:35, when just 1-2 bars exist.
    # (Applying the >=10 filter to today made the bot skip every session.)
    sessions = {d: s for d, s in grouped.items() if d >= today or len(s) >= 10}
    if today not in sessions or len(sessions[today]) == 0:
        return None                       # today's open bar not in yet
    prior_dates = sorted(d for d in sessions if d < today)
    if len(prior_dates) < PARAMS.atr_period + 1:
        return None
    prev_day = prior_dates[-1]

    atr = daily_atr(rth[rth.index.date < today], PARAMS.atr_period)
    a = atr.get(prev_day, np.nan)
    if np.isnan(a) or a <= 0:
        return None

    poc = session_poc(sessions[prev_day], spec.bin_pts)
    day_open = float(sessions[today]["open"].iloc[0])
    if poc == day_open:
        return None
    approach_up = poc > day_open
    side = "long" if approach_up else "short"     # continuation through the POC
    risk = PARAMS.atr_mult * float(a)
    stop = poc - risk if side == "long" else poc + risk
    target = poc + PARAMS.rr * risk if side == "long" else poc - PARAMS.rr * risk
    qty = size_position(risk, spec.dollars_per_point)
    if qty == 0:
        return None
    return Plan(side, poc, stop, target, risk, qty, day_open, prev_day)
