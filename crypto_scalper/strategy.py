"""VWAP mean-reversion + RSI scalping strategy.

Long  when close < lower band AND RSI <= rsi_long_max.
Short when close > upper band AND RSI >= rsi_short_min.
Exit  when close crosses VWAP (target), stop at fixed pct, or time-stop.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from config import STRATEGY
from indicators import add_indicators


@dataclass
class Signal:
    side: str           # "long", "short", or "none"
    entry: float
    stop: float
    target: float
    reason: str


def latest_signal(df: pd.DataFrame) -> Signal:
    """Inspect the most recent closed bar and return an entry signal (or 'none')."""
    if len(df) < STRATEGY.vwap_window // 10:
        return Signal("none", 0.0, 0.0, 0.0, "warmup")

    enriched = add_indicators(
        df, STRATEGY.vwap_window, STRATEGY.band_sigma, STRATEGY.rsi_period,
        STRATEGY.adx_period, STRATEGY.trend_ema_period,
    )
    bar = enriched.iloc[-1]

    if pd.isna(bar.get("vwap")) or pd.isna(bar.get("rsi")) or pd.isna(bar.get("adx")):
        return Signal("none", 0.0, 0.0, 0.0, "indicators-nan")

    close = float(bar["close"])
    vwap = float(bar["vwap"])
    upper = float(bar["band_upper"])
    lower = float(bar["band_lower"])
    rsi_val = float(bar["rsi"])
    adx_val = float(bar["adx"])
    ema_trend = float(bar["ema_trend"]) if not pd.isna(bar.get("ema_trend")) else None

    if adx_val >= STRATEGY.adx_max:
        return Signal("none", 0.0, 0.0, 0.0, f"trending: adx {adx_val:.1f} >= {STRATEGY.adx_max:.1f}")

    if close <= lower and rsi_val <= STRATEGY.rsi_long_max:
        # Trend filter: only go long when price is above the trend EMA (uptrend).
        # Skips mean-reversion longs in downtrends, where "buy the dip" keeps losing.
        if ema_trend is not None and close < ema_trend:
            return Signal("none", 0.0, 0.0, 0.0,
                          f"long-filtered: close {close:.2f} < ema{STRATEGY.trend_ema_period} {ema_trend:.2f} (downtrend)")
        stop = close * (1.0 - STRATEGY.stop_pct)
        return Signal("long", close, stop, vwap, f"long: close {close:.2f} <= lower {lower:.2f}, rsi {rsi_val:.1f}")

    if close >= upper and rsi_val >= STRATEGY.rsi_short_min:
        stop = close * (1.0 + STRATEGY.stop_pct)
        return Signal("short", close, stop, vwap, f"short: close {close:.2f} >= upper {upper:.2f}, rsi {rsi_val:.1f}")

    return Signal("none", 0.0, 0.0, 0.0, "no-edge")


def should_exit(side: str, entry: float, current: float, vwap: float, bars_in_trade: int) -> tuple[bool, str]:
    """Exit rules: VWAP touch (target), fixed-pct stop, or time stop."""
    if side == "long":
        if current >= vwap:
            return True, "target-vwap"
        if current <= entry * (1.0 - STRATEGY.stop_pct):
            return True, "stop"
    elif side == "short":
        if current <= vwap:
            return True, "target-vwap"
        if current >= entry * (1.0 + STRATEGY.stop_pct):
            return True, "stop"

    if bars_in_trade >= STRATEGY.time_stop_bars:
        return True, "time-stop"

    return False, ""
