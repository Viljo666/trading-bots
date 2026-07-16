"""Out-of-sample / robustness validation for TSMOM.

TSMOM uses Carver's PUBLISHED constants (EWMAC speeds, forecast scalars, vol
target, IDM) — nothing is fit to our data — so classic parameter overfitting
is a minor risk. The real question is REGIME-DEPENDENCE: does the edge hold
across different years/eras, or did one lucky period carry everything?

This script checks:
  1. Year-by-year Sharpe / return / maxDD
  2. First-half vs second-half (true out-of-sample halves)
  3. % of years profitable
  4. Robustness: drop each instrument, see how much Sharpe moves
     (if removing one instrument kills it, the result was a fluke)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from data_daily import INSTRUMENTS, load_all
from sizing import position_series

CAPITAL = 100_000.0
TAU = 0.20
IDM = 1.5
COST_PER_CONTRACT = 3.0
TRADING_DAYS = 256


def net_daily_pnl(data: dict, syms: list[str], capital: float = CAPITAL) -> pd.Series:
    """Net daily portfolio P&L (currency) summed across the given symbols."""
    weight = 1.0 / len(syms)
    frames = []
    for sym in syms:
        price = data[sym]["close"]
        mult = INSTRUMENTS[sym]["multiplier"]
        pos = position_series(price, mult, capital, tau=TAU, idm=IDM, weight=weight)
        held = pos.shift(1)
        pnl = held * mult * price.diff()
        cost = pos.diff().abs() * COST_PER_CONTRACT
        frames.append((pnl - cost).fillna(0.0))
    net = pd.concat(frames, axis=1).sum(axis=1)
    return net.loc[net.ne(0).cumsum() > 0]


def stats(net: pd.Series, capital: float = CAPITAL) -> dict:
    if len(net) < 20:
        return {"days": len(net), "sharpe": float("nan"), "ret_pct": float("nan"), "maxdd_pct": float("nan")}
    ret = net / capital
    ann_ret = ret.mean() * TRADING_DAYS
    ann_vol = ret.std() * np.sqrt(TRADING_DAYS)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    equity = capital + net.cumsum()
    dd = ((equity - equity.cummax()) / equity.cummax()).min()
    return {
        "days": len(net),
        "sharpe": round(sharpe, 2),
        "ret_pct": round(net.sum() / capital * 100, 1),
        "maxdd_pct": round(dd * 100, 1),
    }


def main() -> None:
    print("Loading 10y daily data...")
    data = load_all("10y")
    syms = list(data.keys())
    net = net_daily_pnl(data, syms)
    full = stats(net)
    print(f"\n=== FULL PERIOD ({net.index[0].date()} -> {net.index[-1].date()}) ===")
    print(f"  Sharpe {full['sharpe']}   total return {full['ret_pct']}%   maxDD {full['maxdd_pct']}%")

    # 1. year by year
    print("\n=== YEAR BY YEAR ===")
    print(f"  {'year':6s} {'sharpe':>7s} {'return%':>8s} {'maxDD%':>7s}")
    pos_years = 0
    total_years = 0
    for year, grp in net.groupby(net.index.year):
        s = stats(grp)
        if np.isnan(s["sharpe"]):
            continue
        total_years += 1
        if s["ret_pct"] > 0:
            pos_years += 1
        print(f"  {year:6d} {s['sharpe']:>7.2f} {s['ret_pct']:>8.1f} {s['maxdd_pct']:>7.1f}")
    print(f"\n  Profitable years: {pos_years}/{total_years} ({100*pos_years/total_years:.0f}%)")

    # 2. first half vs second half (true OOS split)
    mid = net.index[len(net) // 2]
    h1, h2 = net.loc[:mid], net.loc[mid:]
    s1, s2 = stats(h1), stats(h2)
    print("\n=== FIRST HALF vs SECOND HALF (out-of-sample halves) ===")
    print(f"  H1 {h1.index[0].date()}->{h1.index[-1].date()}: Sharpe {s1['sharpe']}  ret {s1['ret_pct']}%  maxDD {s1['maxdd_pct']}%")
    print(f"  H2 {h2.index[0].date()}->{h2.index[-1].date()}: Sharpe {s2['sharpe']}  ret {s2['ret_pct']}%  maxDD {s2['maxdd_pct']}%")

    # 3. robustness: drop each instrument
    print("\n=== ROBUSTNESS: drop-one-instrument (full-set Sharpe = %.2f) ===" % full["sharpe"])
    print(f"  {'dropped':8s} {'sharpe':>7s}  {'vs full':>8s}")
    for drop in syms:
        subset = [s for s in syms if s != drop]
        s = stats(net_daily_pnl(data, subset))
        delta = s["sharpe"] - full["sharpe"]
        print(f"  {drop:8s} {s['sharpe']:>7.2f}  {delta:>+8.2f}")

    print("\n=== READ ===")
    print("  Healthy if: most years positive, BOTH halves have Sharpe > ~0.4,")
    print("  and dropping any single instrument doesn't collapse Sharpe.")


if __name__ == "__main__":
    main()
