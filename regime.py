"""Market-regime detection using SPY vs its own VWAP.

Regime decides which strategy fires:
- "trend_up"   -> ORB longs only
- "trend_down" -> ORB shorts only
- "range"      -> VP/S/R reversal setups only
- "neutral"    -> no new entries (in-between state)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

import pandas as pd

import indicators
from config import REGIME

log = logging.getLogger(__name__)

RegimeLabel = Literal["trend_up", "trend_down", "range", "neutral"]


@dataclass
class Regime:
    label: RegimeLabel
    spy_price: float
    spy_vwap: float
    deviation_pct: float  # signed: (price - vwap)/price * 100

    def allows_long(self) -> bool:
        return self.label == "trend_up"

    def allows_short(self) -> bool:
        return self.label == "trend_down"

    def allows_reversal(self) -> bool:
        return self.label == "range"


def detect(spy_current_session: pd.DataFrame, prior_close: float | None = None) -> Regime:
    """Classify regime from SPY's current-session intraday bars.

    If `prior_close` is provided AND today's open gapped beyond the configured
    threshold AND we're still in the early-session window, override to trend.
    This prevents the bot from shorting into a strong gap-up morning (or
    longing into a gap-down) before VWAP has time to diverge from price.
    """
    if spy_current_session is None or len(spy_current_session) < REGIME.min_bars:
        return Regime(label="neutral", spy_price=0.0, spy_vwap=0.0, deviation_pct=0.0)

    vw = indicators.vwap(spy_current_session)
    price = float(spy_current_session["close"].iloc[-1])
    vwap_now = float(vw.iloc[-1]) if not vw.empty and pd.notna(vw.iloc[-1]) else price
    if vwap_now <= 0:
        return Regime(label="neutral", spy_price=price, spy_vwap=vwap_now, deviation_pct=0.0)

    dev_pct = (price - vwap_now) / price * 100.0

    # Early-session gap override: ignore VWAP deviation if the session opened
    # with a big gap and we're still in the early-bar window.
    if (
        prior_close is not None
        and prior_close > 0
        and len(spy_current_session) <= REGIME.gap_override_min_bars
    ):
        session_open = float(spy_current_session["open"].iloc[0])
        gap_pct = (session_open - prior_close) / prior_close * 100.0
        if abs(gap_pct) >= REGIME.gap_override_threshold_pct:
            label: RegimeLabel = "trend_up" if gap_pct > 0 else "trend_down"
            log.info("Regime gap override: gap %+.2f%% -> %s", gap_pct, label)
            return Regime(label=label, spy_price=price, spy_vwap=vwap_now, deviation_pct=dev_pct)

    abs_dev = abs(dev_pct)
    if abs_dev >= REGIME.trend_threshold_pct:
        label = "trend_up" if dev_pct > 0 else "trend_down"
    elif abs_dev <= REGIME.range_threshold_pct:
        label = "range"
    else:
        label = "neutral"

    return Regime(label=label, spy_price=price, spy_vwap=vwap_now, deviation_pct=dev_pct)
