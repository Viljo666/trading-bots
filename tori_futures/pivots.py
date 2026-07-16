"""Swing high / swing low pivot detection with 3-left, 3-right confirmation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pandas as pd

LEFT = 3
RIGHT = 3


@dataclass(frozen=True)
class Pivot:
    index: int          # integer position in the bar series
    timestamp: pd.Timestamp
    price: float
    kind: Literal["high", "low"]


def find_pivots(bars: pd.DataFrame, left: int = LEFT, right: int = RIGHT) -> list[Pivot]:
    """Return every confirmed pivot in the series.

    A bar at position i is a swing high if its high is strictly greater than the
    highs of `left` bars to the left AND `right` bars to the right. Swing low is
    the symmetric condition on lows.

    Only bars with enough lookahead (i + right < len) can ever be confirmed.
    """
    highs = bars["high"].to_numpy()
    lows = bars["low"].to_numpy()
    n = len(bars)
    pivots: list[Pivot] = []

    for i in range(left, n - right):
        window_high = highs[i - left : i + right + 1]
        if highs[i] == window_high.max() and (window_high == highs[i]).sum() == 1:
            pivots.append(Pivot(i, bars.index[i], float(highs[i]), "high"))
            continue

        window_low = lows[i - left : i + right + 1]
        if lows[i] == window_low.min() and (window_low == lows[i]).sum() == 1:
            pivots.append(Pivot(i, bars.index[i], float(lows[i]), "low"))

    return pivots


def pivots_confirmed_by(pivots: list[Pivot], bar_index: int, right: int = RIGHT) -> list[Pivot]:
    """Return pivots that are confirmed as of `bar_index` (i.e. the right-side lookahead has elapsed)."""
    return [p for p in pivots if p.index + right <= bar_index]


if __name__ == "__main__":
    from data import load_es_15m

    bars = load_es_15m()
    pivots = find_pivots(bars)
    print(f"{len(pivots)} pivots in {len(bars)} bars")
    for p in pivots[-10:]:
        print(p)
