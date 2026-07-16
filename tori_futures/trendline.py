"""2-touch trendline construction and break detection."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pivots import Pivot


@dataclass(frozen=True)
class Trendline:
    p1: Pivot   # earlier anchor
    p2: Pivot   # later anchor
    kind: Literal["down", "up"]   # down = resistance above price, up = support below price

    @property
    def slope(self) -> float:
        return (self.p2.price - self.p1.price) / (self.p2.index - self.p1.index)

    def value_at(self, bar_index: int) -> float:
        """Project the line's y-value at a given bar index (extrapolation allowed)."""
        return self.p1.price + self.slope * (bar_index - self.p1.index)


def _last_n_of_kind(pivots: list[Pivot], kind: Literal["high", "low"], n: int) -> list[Pivot]:
    filtered = [p for p in pivots if p.kind == kind]
    return filtered[-n:] if len(filtered) >= n else []


def current_down_trendline(pivots: list[Pivot]) -> Trendline | None:
    """Descending line through the 2 most recent swing highs (H2 < H1)."""
    highs = _last_n_of_kind(pivots, "high", 2)
    if not highs:
        return None
    p1, p2 = highs
    if p2.price >= p1.price:
        return None   # not descending — no valid down-trendline
    return Trendline(p1=p1, p2=p2, kind="down")


def current_up_trendline(pivots: list[Pivot]) -> Trendline | None:
    """Ascending line through the 2 most recent swing lows (L2 > L1)."""
    lows = _last_n_of_kind(pivots, "low", 2)
    if not lows:
        return None
    p1, p2 = lows
    if p2.price <= p1.price:
        return None
    return Trendline(p1=p1, p2=p2, kind="up")


def closed_through(line: Trendline, bar_index: int, close_price: float) -> bool:
    """Did this bar's close pierce the trendline in the break direction?

    - Down-trendline (resistance above): bullish break when close is above the line.
    - Up-trendline (support below): bearish break when close is below the line.
    """
    level = line.value_at(bar_index)
    if line.kind == "down":
        return close_price > level
    return close_price < level


if __name__ == "__main__":
    from data import load_es_15m
    from pivots import find_pivots

    bars = load_es_15m()
    pivots = find_pivots(bars)
    down = current_down_trendline(pivots)
    up = current_up_trendline(pivots)
    print(f"Down trendline: {down}")
    print(f"Up trendline:   {up}")
