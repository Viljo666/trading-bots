"""Trend-follow strategy.

ENTRY (long):
  - ema_fast crosses above ema_slow on this bar
  - slow EMA slope (now vs N bars ago) is positive
  - ADX >= adx_min
ENTRY (short): mirror.

EXITS (whichever first):
  - Opposite EMA cross
  - ATR stop: entry ∓ atr_stop_mult * ATR
  - Trailing stop: once price has moved >= 1R favorably, trail at trail_atr_mult * ATR
  - Time stop: time_stop_bars after entry
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from config import STRATEGY


@dataclass
class Signal:
    side: str                 # "long" / "short" / "none"
    entry: float
    stop: float
    target: float             # 1R target — used for trailing-trigger
    reason: str


def _crossed_above(s1_now: float, s1_prev: float, s2_now: float, s2_prev: float) -> bool:
    return s1_prev <= s2_prev and s1_now > s2_now


def _crossed_below(s1_now: float, s1_prev: float, s2_now: float, s2_prev: float) -> bool:
    return s1_prev >= s2_prev and s1_now < s2_now


def latest_signal(enriched: pd.DataFrame) -> Signal:
    """Inspect the most recent CLOSED bar. Bar i is the trigger; we'd enter at bar i+1 open."""
    if len(enriched) < max(STRATEGY.ema_slow, STRATEGY.atr_period, STRATEGY.adx_period) + STRATEGY.slope_lookback + 2:
        return Signal("none", 0.0, 0.0, 0.0, "warmup")
    bar = enriched.iloc[-1]
    prev = enriched.iloc[-2]
    if pd.isna(bar.get("ema_fast")) or pd.isna(bar.get("ema_slow")) or pd.isna(bar.get("atr")) or pd.isna(bar.get("adx")):
        return Signal("none", 0.0, 0.0, 0.0, "indicators-nan")

    fast_now, fast_prev = float(bar["ema_fast"]), float(prev["ema_fast"])
    slow_now, slow_prev = float(bar["ema_slow"]), float(prev["ema_slow"])
    atr_v = float(bar["atr"])
    adx_v = float(bar["adx"])
    close = float(bar["close"])

    if adx_v < STRATEGY.adx_min:
        return Signal("none", 0.0, 0.0, 0.0, f"ranging adx {adx_v:.1f} < {STRATEGY.adx_min:.1f}")

    # Slow EMA slope over slope_lookback bars.
    look = STRATEGY.slope_lookback
    if len(enriched) < look + 2:
        return Signal("none", 0.0, 0.0, 0.0, "warmup-slope")
    slow_back = float(enriched["ema_slow"].iloc[-(look + 1)])
    slope = slow_now - slow_back

    if _crossed_above(fast_now, fast_prev, slow_now, slow_prev) and slope > 0:
        stop = close - STRATEGY.atr_stop_mult * atr_v
        target = close + STRATEGY.target_r * (close - stop)
        return Signal("long", close, stop, target,
                      f"long: fast x↑slow, adx {adx_v:.1f}, slope+{slope:.4f}")

    if _crossed_below(fast_now, fast_prev, slow_now, slow_prev) and slope < 0:
        stop = close + STRATEGY.atr_stop_mult * atr_v
        target = close - STRATEGY.target_r * (stop - close)
        return Signal("short", close, stop, target,
                      f"short: fast x↓slow, adx {adx_v:.1f}, slope{slope:.4f}")

    return Signal("none", 0.0, 0.0, 0.0, "no-cross")
