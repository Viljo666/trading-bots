"""EWMAC trend forecasts (Carver-style continuous forecasts).

forecast pipeline per instrument:
  raw          = EWMA(price, fast) - EWMA(price, slow)
  raw_forecast = raw / daily_price_vol          (vol in price points)
  scaled       = raw_forecast * forecast_scalar  (long-run |forecast| ~ 10)
  capped       = clip(scaled, -20, 20)
  combined     = mean(capped over speeds) * FDM, capped to ±20

The combined forecast is a continuous number centred on 0; +10 is an "average"
long conviction, -10 average short, ±20 the cap.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Carver published forecast scalars for EWMAC(fast, slow=4*fast)
EWMAC_SPEEDS = {
    (16, 64): 4.10,
    (32, 128): 2.79,
    (64, 256): 1.91,
}
FORECAST_CAP = 20.0
FDM = 1.1            # forecast diversification multiplier for ~3 correlated speeds
VOL_SPAN = 32        # EWM span for price volatility


def daily_price_vol(price: pd.Series, span: int = VOL_SPAN) -> pd.Series:
    """EWM std of daily price changes, in price points. Floored to avoid div-by-zero."""
    changes = price.diff()
    vol = changes.ewm(span=span, min_periods=span).std()
    return vol.replace(0.0, np.nan).ffill()


def ewmac_forecast(price: pd.Series, fast: int, slow: int, scalar: float) -> pd.Series:
    raw = price.ewm(span=fast, min_periods=fast).mean() - price.ewm(span=slow, min_periods=slow).mean()
    vol = daily_price_vol(price)
    raw_forecast = raw / vol
    scaled = raw_forecast * scalar
    return scaled.clip(-FORECAST_CAP, FORECAST_CAP)


def combined_forecast(price: pd.Series) -> pd.Series:
    """Average the EWMAC speeds, apply FDM, cap to ±20."""
    forecasts = [
        ewmac_forecast(price, fast, slow, scalar)
        for (fast, slow), scalar in EWMAC_SPEEDS.items()
    ]
    combo = pd.concat(forecasts, axis=1).mean(axis=1) * FDM
    return combo.clip(-FORECAST_CAP, FORECAST_CAP)


def annualized_return_vol(price: pd.Series, span: int = VOL_SPAN) -> pd.Series:
    """Annualized % stdev of returns (for position sizing). 16 ≈ sqrt(256)."""
    returns = price.pct_change()
    daily = returns.ewm(span=span, min_periods=span).std()
    return (daily * 16.0).replace(0.0, np.nan).ffill()


if __name__ == "__main__":
    from data_daily import load_daily

    px = load_daily("ES=F")["close"]
    f = combined_forecast(px)
    print("ES combined forecast — last 10 days:")
    print(f.tail(10).round(2))
    print(f"\nlong-run mean |forecast|: {f.abs().mean():.2f} (target ~10)")
    print(f"% time long: {(f > 0).mean()*100:.0f}%  short: {(f < 0).mean()*100:.0f}%")
