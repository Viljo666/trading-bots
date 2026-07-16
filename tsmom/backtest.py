"""Portfolio backtest for the TSMOM (Carver) strategy.

Method (no lookahead):
  - position[t]  = target contracts computed from data up to close of day t
  - pnl on t+1   = position[t] * multiplier * (price[t+1] - price[t])
  - costs        = contracts_traded * cost_per_contract  (applied on turnover)
  - portfolio    = sum across instruments; returns = daily_pnl / capital

Reports Sharpe, CAGR, max drawdown, per-instrument contribution. Fractional
contracts kept (measures the strategy cleanly; live would round + buffer).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from data_daily import INSTRUMENTS, load_all
from sizing import position_series

CAPITAL = 100_000.0
TAU = 0.20
IDM = 1.5
COST_PER_CONTRACT = 3.0   # USD per contract traded (commission + modest slippage)
TRADING_DAYS = 256


def backtest(period: str = "10y", capital: float = CAPITAL, tau: float = TAU) -> dict:
    data = load_all(period)
    syms = list(data.keys())
    weight = 1.0 / len(syms)

    pnl_by_sym: dict[str, pd.Series] = {}
    cost_by_sym: dict[str, pd.Series] = {}

    for sym in syms:
        df = data[sym]
        price = df["close"]
        mult = INSTRUMENTS[sym]["multiplier"]

        pos = position_series(price, mult, capital, tau=tau, idm=IDM, weight=weight)
        held = pos.shift(1)                       # trade on next bar — no lookahead
        price_change = price.diff()
        pnl = held * mult * price_change

        traded = pos.diff().abs()                 # contracts traded each day
        cost = traded * COST_PER_CONTRACT

        pnl_by_sym[sym] = pnl.fillna(0.0)
        cost_by_sym[sym] = cost.fillna(0.0)

    gross = pd.DataFrame(pnl_by_sym).sum(axis=1)
    costs = pd.DataFrame(cost_by_sym).sum(axis=1)
    net = gross - costs

    # align to a clean date range where we actually have signals (drop warmup)
    net = net.loc[net.ne(0).cumsum() > 0]
    daily_ret = net / capital

    equity = capital + net.cumsum()
    peak = equity.cummax()
    dd = (equity - peak) / peak

    ann_ret = daily_ret.mean() * TRADING_DAYS
    ann_vol = daily_ret.std() * np.sqrt(TRADING_DAYS)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    total_net = net.sum()
    total_cost = costs.sum()
    n_years = len(net) / TRADING_DAYS
    cagr = (equity.iloc[-1] / capital) ** (1 / n_years) - 1 if n_years > 0 else 0.0

    per_sym = {sym: pnl_by_sym[sym].sum() - cost_by_sym[sym].sum() for sym in syms}

    return {
        "syms": syms,
        "start": net.index[0].date(),
        "end": net.index[-1].date(),
        "years": round(n_years, 1),
        "sharpe": round(sharpe, 2),
        "ann_vol_pct": round(ann_vol * 100, 1),
        "cagr_pct": round(cagr * 100, 1),
        "total_net": round(total_net, 0),
        "total_cost": round(total_cost, 0),
        "max_dd_pct": round(dd.min() * 100, 1),
        "per_sym": {s: round(v, 0) for s, v in per_sym.items()},
        "equity": equity,
    }


def main() -> None:
    print("Running TSMOM backtest (Carver EWMAC, vol-targeted)...\n")
    r = backtest()
    print(f"Instruments : {', '.join(r['syms'])}")
    print(f"Period      : {r['start']} -> {r['end']}  ({r['years']} yrs)")
    print(f"Capital     : ${CAPITAL:,.0f}   vol target tau={TAU}  IDM={IDM}")
    print(f"Cost/contract: ${COST_PER_CONTRACT}\n")
    print("=== Portfolio ===")
    print(f"  Sharpe          {r['sharpe']}")
    print(f"  Ann vol         {r['ann_vol_pct']}%   (target {int(TAU*100)}%)")
    print(f"  CAGR            {r['cagr_pct']}%")
    print(f"  Net P&L         ${r['total_net']:,.0f}")
    print(f"  Costs paid      ${r['total_cost']:,.0f}")
    print(f"  Max drawdown    {r['max_dd_pct']}%")
    print("\n=== Net P&L by instrument ===")
    for s, v in sorted(r["per_sym"].items(), key=lambda kv: kv[1], reverse=True):
        print(f"  {s:6s} ${v:>12,.0f}")


if __name__ == "__main__":
    main()
