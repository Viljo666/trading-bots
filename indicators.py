"""Technical indicators: volume profile, S/R, trendlines, VWAP, ATR."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.signal import find_peaks


@dataclass
class VolumeProfile:
    poc: float            # Point of Control (highest-volume price)
    vah: float            # Value Area High (70% upper bound)
    val: float            # Value Area Low
    hvns: list[float]     # High Volume Nodes
    lvns: list[float]     # Low Volume Nodes


@dataclass
class Levels:
    supports: list[float]
    resistances: list[float]


@dataclass
class Trendline:
    slope: float
    intercept: float  # value of the line at the first bar
    kind: str         # "support" or "resistance"

    def value_at(self, x: int) -> float:
        return self.slope * x + self.intercept


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ATR on high/low/close."""
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP. Assumes df is a single session of intraday bars."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    cum_vp = (tp * df["volume"]).cumsum()
    cum_v = df["volume"].cumsum().replace(0, np.nan)
    return cum_vp / cum_v


def volume_profile(df: pd.DataFrame, bins: int = 40, value_area_pct: float = 0.70) -> VolumeProfile:
    """Compute POC, VAH, VAL, HVN/LVN for a session of bars."""
    if df.empty:
        return VolumeProfile(poc=0.0, vah=0.0, val=0.0, hvns=[], lvns=[])

    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].values
    lo, hi = float(tp.min()), float(tp.max())
    if hi <= lo:
        return VolumeProfile(poc=lo, vah=lo, val=lo, hvns=[lo], lvns=[])

    edges = np.linspace(lo, hi, bins + 1)
    counts, _ = np.histogram(tp, bins=edges, weights=vol)
    centers = (edges[:-1] + edges[1:]) / 2.0

    poc_idx = int(np.argmax(counts))
    poc = float(centers[poc_idx])

    # Value area: expand around POC until >= value_area_pct of total volume
    total = counts.sum()
    target = total * value_area_pct
    acc = counts[poc_idx]
    lo_i = hi_i = poc_idx
    while acc < target and (lo_i > 0 or hi_i < len(counts) - 1):
        left = counts[lo_i - 1] if lo_i > 0 else -1
        right = counts[hi_i + 1] if hi_i < len(counts) - 1 else -1
        if right >= left:
            hi_i += 1
            acc += counts[hi_i]
        else:
            lo_i -= 1
            acc += counts[lo_i]

    val = float(centers[lo_i])
    vah = float(centers[hi_i])

    # HVN/LVN: peaks / valleys in the profile
    peaks, _ = find_peaks(counts, distance=3)
    valleys, _ = find_peaks(-counts, distance=3)
    hvns = [float(centers[i]) for i in peaks]
    lvns = [float(centers[i]) for i in valleys]

    return VolumeProfile(poc=poc, vah=vah, val=val, hvns=hvns, lvns=lvns)


def support_resistance(df: pd.DataFrame, lookback: int = 60, prominence_atr_mult: float = 0.5) -> Levels:
    """Detect horizontal S/R from swing highs/lows on recent bars."""
    if len(df) < 5:
        return Levels([], [])

    window = df.tail(lookback)
    a = atr(window).iloc[-1]
    prominence = max(float(a) * prominence_atr_mult, 1e-6)

    highs = window["high"].values
    lows = window["low"].values

    hi_peaks, _ = find_peaks(highs, prominence=prominence)
    lo_peaks, _ = find_peaks(-lows, prominence=prominence)

    resistances = sorted({round(float(highs[i]), 2) for i in hi_peaks})
    supports = sorted({round(float(lows[i]), 2) for i in lo_peaks})

    # Cluster nearby levels
    resistances = _cluster(resistances, tol=float(a) * 0.25)
    supports = _cluster(supports, tol=float(a) * 0.25)

    return Levels(supports=supports, resistances=resistances)


def _cluster(levels: list[float], tol: float) -> list[float]:
    if not levels:
        return []
    levels = sorted(levels)
    out: list[list[float]] = [[levels[0]]]
    for lv in levels[1:]:
        if abs(lv - out[-1][-1]) <= tol:
            out[-1].append(lv)
        else:
            out.append([lv])
    return [round(sum(g) / len(g), 2) for g in out]


def fit_trendline(df: pd.DataFrame, lookback: int = 30, kind: str = "support") -> Trendline | None:
    """Fit a trendline through recent swing lows (support) or swing highs (resistance)."""
    if len(df) < lookback:
        return None
    window = df.tail(lookback).reset_index(drop=True)

    if kind == "support":
        series = window["low"].values
        peaks, _ = find_peaks(-series, distance=2)
    else:
        series = window["high"].values
        peaks, _ = find_peaks(series, distance=2)

    if len(peaks) < 2:
        return None

    x = peaks.astype(float)
    y = series[peaks]
    # Least-squares line through the swing points
    slope, intercept = np.polyfit(x, y, 1)
    return Trendline(slope=float(slope), intercept=float(intercept), kind=kind)


def is_near(price: float, level: float, tolerance: float) -> bool:
    return abs(price - level) <= tolerance


def bullish_reversal_bar(df: pd.DataFrame, vol_mult: float = 1.5) -> bool:
    """Last bar: bullish engulfing or hammer with above-average volume."""
    if len(df) < 20:
        return False
    last = df.iloc[-1]
    prev = df.iloc[-2]
    avg_vol = df["volume"].tail(20).mean()

    body = last["close"] - last["open"]
    prev_body = prev["close"] - prev["open"]
    range_ = last["high"] - last["low"]

    engulfing = body > 0 and prev_body < 0 and last["close"] > prev["open"] and last["open"] < prev["close"]
    hammer = range_ > 0 and body > 0 and (last["open"] - last["low"]) / range_ > 0.5
    high_vol = last["volume"] > avg_vol * vol_mult
    return (engulfing or hammer) and high_vol


def bearish_reversal_bar(df: pd.DataFrame, vol_mult: float = 1.5) -> bool:
    """Last bar: bearish engulfing or shooting star with above-average volume."""
    if len(df) < 20:
        return False
    last = df.iloc[-1]
    prev = df.iloc[-2]
    avg_vol = df["volume"].tail(20).mean()

    body = last["close"] - last["open"]
    prev_body = prev["close"] - prev["open"]
    range_ = last["high"] - last["low"]

    engulfing = body < 0 and prev_body > 0 and last["close"] < prev["open"] and last["open"] > prev["close"]
    star = range_ > 0 and body < 0 and (last["high"] - last["open"]) / range_ > 0.5
    high_vol = last["volume"] > avg_vol * vol_mult
    return (engulfing or star) and high_vol
