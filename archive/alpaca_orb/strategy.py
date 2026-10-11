"""Entry/exit logic.

Two setups, dispatched by market regime:
- trend regime  -> ORB (Opening Range Breakout)
- range regime  -> volume-profile + S/R + trendline reversal
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import time as dtime

import pandas as pd

import indicators
from config import ORB, STRATEGY
from regime import Regime

log = logging.getLogger(__name__)


@dataclass
class Signal:
    symbol: str
    side: str             # "buy" or "sell"
    entry: float
    stop: float
    target: float
    reason: str
    setup: str            # "orb" | "reversal"
    atr: float = 0.0      # ATR at signal time — used for stale-signal check in broker


def _split_sessions(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split intraday bars into (prior_session, current_session) by calendar day."""
    df = df.copy()
    df.index = pd.to_datetime(df.index)
    df["_date"] = df.index.date
    dates = sorted(df["_date"].unique())
    if len(dates) < 2:
        return df.drop(columns="_date"), df.iloc[0:0].drop(columns="_date")
    prior = df[df["_date"] == dates[-2]].drop(columns="_date")
    current = df[df["_date"] == dates[-1]].drop(columns="_date")
    return prior, current


def evaluate(symbol: str, intraday: pd.DataFrame, regime: Regime) -> Signal | None:
    """Dispatch to the right setup based on regime + config toggles."""
    if regime.label in ("trend_up", "trend_down") and STRATEGY.enable_orb:
        return evaluate_orb(symbol, intraday, regime)
    if regime.label == "range" and STRATEGY.enable_reversal:
        return evaluate_reversal(symbol, intraday)
    return None  # neutral, or setup disabled -> no trade


# ---------------------------------------------------------------------------
# ORB (Opening Range Breakout)
# ---------------------------------------------------------------------------

def evaluate_orb(symbol: str, intraday: pd.DataFrame, regime: Regime) -> Signal | None:
    """Trade the break of the first N bars' high/low, aligned with SPY trend."""
    if len(intraday) < 20:
        return None

    _, current = _split_sessions(intraday)
    if len(current) < ORB.range_bars + 1:
        return None

    # Don't fire ORB late in the day
    last_idx = current.index[-1]
    if last_idx.time() >= _parse_time(ORB.latest_entry_time):
        return None

    or_bars = current.iloc[: ORB.range_bars]
    orh = float(or_bars["high"].max())
    orl = float(or_bars["low"].min())
    last = current.iloc[-1]
    price = float(last["close"])

    a = float(indicators.atr(intraday, STRATEGY.atr_period).iloc[-1])
    if a <= 0 or pd.isna(a):
        return None

    avg_vol = float(intraday["volume"].tail(20).mean())
    vol_ok = float(last["volume"]) >= avg_vol * ORB.volume_mult

    # Long breakout — only if SPY regime is trend_up
    if regime.allows_long() and price > orh and vol_ok and float(last["close"]) > float(last["open"]):
        stop = orl - a * ORB.stop_buffer_atr
        risk = price - stop
        if risk <= 0:
            return None
        target = price + risk * ORB.target_r_multiple
        return Signal(
            symbol=symbol, side="buy", entry=price, stop=stop, target=target,
            reason=f"ORB long break {orh:.2f} (range {orl:.2f}-{orh:.2f}), SPY+{regime.deviation_pct:.2f}%",
            setup="orb", atr=a,
        )

    # Short breakout — only if SPY regime is trend_down
    if regime.allows_short() and price < orl and vol_ok and float(last["close"]) < float(last["open"]):
        stop = orh + a * ORB.stop_buffer_atr
        risk = stop - price
        if risk <= 0:
            return None
        target = price - risk * ORB.target_r_multiple
        return Signal(
            symbol=symbol, side="sell", entry=price, stop=stop, target=target,
            reason=f"ORB short break {orl:.2f} (range {orl:.2f}-{orh:.2f}), SPY{regime.deviation_pct:.2f}%",
            setup="orb", atr=a,
        )

    return None


# ---------------------------------------------------------------------------
# Volume-profile / S/R reversal
# ---------------------------------------------------------------------------

def evaluate_reversal(symbol: str, intraday: pd.DataFrame) -> Signal | None:
    """Look for a confluence reversal on the most recent 15-min bar.

    Long: price touches prior-day VAL/POC/support + bullish reversal bar + near VWAP.
    Short: price touches prior-day VAH/POC/resistance + bearish reversal bar + near VWAP.
    """
    if len(intraday) < 20:
        return None

    prior, current = _split_sessions(intraday)
    if prior.empty or current.empty:
        return None

    vp = indicators.volume_profile(prior, bins=STRATEGY.vp_bins, value_area_pct=STRATEGY.value_area_pct)
    levels = indicators.support_resistance(intraday, lookback=80)
    a = float(indicators.atr(intraday, STRATEGY.atr_period).iloc[-1])
    if a <= 0 or pd.isna(a):
        return None

    tol = a * STRATEGY.level_proximity_atr
    vw = indicators.vwap(current)
    last_vwap = float(vw.iloc[-1]) if not vw.empty and pd.notna(vw.iloc[-1]) else None
    last = current.iloc[-1]
    price = float(last["close"])

    support_trend = indicators.fit_trendline(intraday, lookback=30, kind="support")
    resistance_trend = indicators.fit_trendline(intraday, lookback=30, kind="resistance")

    # --- Long setup ---
    long_levels = [("VAL", vp.val), ("POC", vp.poc)] + [("S", s) for s in levels.supports]
    if support_trend is not None:
        tv = support_trend.value_at(len(intraday) - 1)
        long_levels.append(("TL-S", tv))

    for name, lv in long_levels:
        if not lv or lv <= 0:
            continue
        if indicators.is_near(price, lv, tol) and indicators.bullish_reversal_bar(intraday, STRATEGY.reversal_volume_mult):
            if last_vwap is not None and price < last_vwap * 0.995:
                continue
            stop = min(float(last["low"]), lv) - a * STRATEGY.atr_stop_mult
            risk = price - stop
            if risk <= 0:
                continue
            target = price + risk * STRATEGY.target_r_multiple
            return Signal(
                symbol=symbol, side="buy", entry=price, stop=stop, target=target,
                reason=f"reversal long @ {name}={lv:.2f} + bullish bar, ATR={a:.2f}",
                setup="reversal", atr=a,
            )

    # --- Short setup ---
    short_levels = [("VAH", vp.vah), ("POC", vp.poc)] + [("R", r) for r in levels.resistances]
    if resistance_trend is not None:
        tv = resistance_trend.value_at(len(intraday) - 1)
        short_levels.append(("TL-R", tv))

    for name, lv in short_levels:
        if not lv or lv <= 0:
            continue
        if indicators.is_near(price, lv, tol) and indicators.bearish_reversal_bar(intraday, STRATEGY.reversal_volume_mult):
            if last_vwap is not None and price > last_vwap * 1.005:
                continue
            stop = max(float(last["high"]), lv) + a * STRATEGY.atr_stop_mult
            risk = stop - price
            if risk <= 0:
                continue
            target = price - risk * STRATEGY.target_r_multiple
            return Signal(
                symbol=symbol, side="sell", entry=price, stop=stop, target=target,
                reason=f"reversal short @ {name}={lv:.2f} + bearish bar, ATR={a:.2f}",
                setup="reversal", atr=a,
            )

    return None


def should_convert_to_swing(position_plpc: float, price: float, vwap_now: float | None, side: str) -> bool:
    """Keep a winner overnight if it's >= min_r profit and on the right side of VWAP."""
    if position_plpc < STRATEGY.swing_min_r_multiple * 0.01:
        return False
    if vwap_now is None:
        return False
    if side == "long":
        return price > vwap_now
    return price < vwap_now


def _parse_time(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))
