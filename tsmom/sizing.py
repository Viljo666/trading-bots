"""Volatility-targeted position sizing (Carver).

N_contracts = (forecast / 10) * (capital * tau * IDM * weight)
              / (multiplier * price * sigma_pct)

  forecast    : combined EWMAC forecast (continuous, ±20)
  tau         : annual portfolio vol target (e.g. 0.20)
  IDM         : instrument diversification multiplier (>1)
  weight      : instrument weight (1/N to start)
  multiplier  : contract point value
  price       : instrument price
  sigma_pct   : annualized % stdev of returns

Returns FRACTIONAL contracts (backtest keeps them fractional to measure the
strategy cleanly; live rounds + buffers).
"""
from __future__ import annotations

import pandas as pd

from signals import annualized_return_vol, combined_forecast


def position_series(
    price: pd.Series,
    multiplier: float,
    capital: float,
    tau: float = 0.20,
    idm: float = 1.5,
    weight: float = 1.0,
) -> pd.Series:
    """Daily target position (fractional contracts) for one instrument."""
    forecast = combined_forecast(price)
    sigma_pct = annualized_return_vol(price)
    cash_per_contract_vol = multiplier * price * sigma_pct  # annual currency vol of 1 contract
    n = (forecast / 10.0) * (capital * tau * idm * weight) / cash_per_contract_vol
    return n.replace([float("inf"), float("-inf")], 0.0).fillna(0.0)


if __name__ == "__main__":
    from data_daily import INSTRUMENTS, load_daily

    capital = 100_000.0
    n_inst = len(INSTRUMENTS)
    print(f"Capital ${capital:,.0f}, {n_inst} instruments, tau=0.20\n")
    print(f"{'sym':6s} {'price':>10s} {'forecast':>9s} {'target_N':>9s}")
    for sym, meta in INSTRUMENTS.items():
        px = load_daily(sym)["close"]
        pos = position_series(px, meta["multiplier"], capital, weight=1.0 / n_inst)
        f = combined_forecast(px)
        print(f"{sym:6s} {px.iloc[-1]:>10.2f} {f.iloc[-1]:>9.2f} {pos.iloc[-1]:>9.2f}")
