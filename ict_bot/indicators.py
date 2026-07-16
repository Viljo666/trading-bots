"""Mechanical detectors: FVGs, swing pivots, HTF bias, ATR."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass
class FVG:
    side: str          # "bullish" or "bearish"
    formed_idx: int    # bar index where the FVG completed
    top: float         # upper edge of the gap
    bottom: float      # lower edge
    filled_idx: int | None = None


@dataclass
class Swing:
    side: str          # "high" or "low"
    idx: int
    price: float
    swept: bool = False
    swept_idx: int | None = None


# ---- ATR ----
def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


# ---- HTF bias ----
def htf_bias_series(htf_df: pd.DataFrame, ema_period: int) -> pd.Series:
    """Returns +1 (bullish), -1 (bearish), 0 (neutral) per HTF bar."""
    ema = htf_df["close"].ewm(span=ema_period, adjust=False, min_periods=ema_period).mean()
    bias = pd.Series(0, index=htf_df.index, dtype=int)
    bias[htf_df["close"] > ema] = 1
    bias[htf_df["close"] < ema] = -1
    return bias


def align_htf_to_ltf(htf_bias: pd.Series, ltf_index: pd.DatetimeIndex) -> pd.Series:
    """For each LTF timestamp, return the bias of the most recent CLOSED HTF bar."""
    return htf_bias.reindex(ltf_index, method="ffill").fillna(0).astype(int)


# ---- Fair Value Gaps ----
def find_fvgs(df: pd.DataFrame, min_pct: float) -> list[FVG]:
    """Detect 3-candle FVGs.

    Bullish: low[i] > high[i-2]  → gap = (high[i-2], low[i])
    Bearish: high[i] < low[i-2]  → gap = (high[i], low[i-2])

    `i` is the third candle (the one that confirms the imbalance).
    Returns FVGs in detection order.
    """
    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    fvgs: list[FVG] = []
    for i in range(2, len(df)):
        # Bullish
        if low[i] > high[i - 2]:
            gap_pct = (low[i] - high[i - 2]) / close[i]
            if gap_pct >= min_pct:
                fvgs.append(FVG("bullish", i, top=float(low[i]), bottom=float(high[i - 2])))
        # Bearish
        elif high[i] < low[i - 2]:
            gap_pct = (low[i - 2] - high[i]) / close[i]
            if gap_pct >= min_pct:
                fvgs.append(FVG("bearish", i, top=float(low[i - 2]), bottom=float(high[i])))
    return fvgs


def fill_fvgs_inplace(fvgs: list[FVG], df: pd.DataFrame) -> None:
    """Mark FVGs as filled when price re-enters the gap zone after formation."""
    high = df["high"].values
    low = df["low"].values
    for fvg in fvgs:
        if fvg.filled_idx is not None:
            continue
        for j in range(fvg.formed_idx + 1, len(df)):
            if fvg.side == "bullish" and low[j] <= fvg.top:
                fvg.filled_idx = j
                break
            if fvg.side == "bearish" and high[j] >= fvg.bottom:
                fvg.filled_idx = j
                break


# ---- Swings (pivots) ----
def find_swings(df: pd.DataFrame, left: int, right: int) -> list[Swing]:
    """Pivot high = bar whose high is the highest of [i-left .. i+right] (inclusive of i).

    Pivot is confirmed only after `right` bars have closed past it.
    """
    high = df["high"].values
    low = df["low"].values
    swings: list[Swing] = []
    n = len(df)
    for i in range(left, n - right):
        window_h = high[i - left : i + right + 1]
        window_l = low[i - left : i + right + 1]
        if high[i] == window_h.max() and (window_h.max() > window_h[: left]).any():
            swings.append(Swing("high", i, float(high[i])))
        elif low[i] == window_l.min() and (window_l.min() < window_l[: left]).any():
            swings.append(Swing("low", i, float(low[i])))
    return swings


def mark_sweeps_inplace(swings: list[Swing], df: pd.DataFrame) -> None:
    """A swing high is 'swept' when a later bar's high pierces it.
    Same for swing low (a later bar's low pierces it). First piercing bar is recorded.
    """
    high = df["high"].values
    low = df["low"].values
    for s in swings:
        if s.swept:
            continue
        for j in range(s.idx + 1, len(df)):
            if s.side == "high" and high[j] > s.price:
                s.swept = True
                s.swept_idx = j
                break
            if s.side == "low" and low[j] < s.price:
                s.swept = True
                s.swept_idx = j
                break
