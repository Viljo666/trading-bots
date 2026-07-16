"""Compute prior-day and overnight levels from 15m bars (US/Eastern session).

PDH/PDL = prior US RTH session high/low (9:30-16:00 ET).
ONH/ONL = overnight high/low (prior 16:00 ET to today's 9:30 ET).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional

import pandas as pd

ET = "US/Eastern"
RTH_OPEN = time(9, 30)
RTH_CLOSE = time(16, 0)


@dataclass
class Levels:
    pdh: Optional[float]
    pdl: Optional[float]
    onh: Optional[float]
    onl: Optional[float]
    as_of: pd.Timestamp

    def to_dict(self) -> dict:
        return {
            "PDH": self.pdh,
            "PDL": self.pdl,
            "ONH": self.onh,
            "ONL": self.onl,
            "as_of": str(self.as_of),
        }


def compute_levels(bars: pd.DataFrame, now: Optional[pd.Timestamp] = None) -> Levels:
    if bars.empty:
        return Levels(None, None, None, None, pd.Timestamp.now(tz="UTC"))

    df = bars.copy()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(ET)

    if now is None:
        now = df.index[-1]
    elif now.tzinfo is None:
        now = pd.Timestamp(now, tz="UTC").tz_convert(ET)
    else:
        now = now.tz_convert(ET)

    in_rth_mask = (df.index.time >= RTH_OPEN) & (df.index.time < RTH_CLOSE)
    in_rth = df[in_rth_mask]
    if in_rth.empty:
        return Levels(None, None, None, None, now)

    sessions = sorted({ts.date() for ts in in_rth.index})
    today = now.date()
    if now.time() < RTH_CLOSE:
        prior_candidates = [s for s in sessions if s < today]
    else:
        prior_candidates = [s for s in sessions if s <= today]
    if not prior_candidates:
        return Levels(None, None, None, None, now)

    prior = prior_candidates[-1]
    prior_bars = in_rth[in_rth.index.date == prior]
    pdh = float(prior_bars["high"].max()) if not prior_bars.empty else None
    pdl = float(prior_bars["low"].min()) if not prior_bars.empty else None

    on_start = pd.Timestamp(datetime.combine(prior, RTH_CLOSE)).tz_localize(ET)
    today_open = pd.Timestamp(datetime.combine(today, RTH_OPEN)).tz_localize(ET)
    on_end = today_open if now >= today_open else now
    if on_end <= on_start:
        return Levels(pdh, pdl, None, None, now)

    overnight = df[(df.index >= on_start) & (df.index < on_end)]
    onh = float(overnight["high"].max()) if not overnight.empty else None
    onl = float(overnight["low"].min()) if not overnight.empty else None

    return Levels(pdh=pdh, pdl=pdl, onh=onh, onl=onl, as_of=now)
