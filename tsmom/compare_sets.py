"""Head-to-head comparison of TSMOM configurations.

Three PRINCIPLED variants (structural, from Carver's framework — not data-mined):
  A. current7   — the live set, 1/N weights, IDM 1.5 (baseline)
  B. expanded13 — 13 instruments, 1/N weights, IDM 2.0
  C. classbal13 — 13 instruments, equal risk per ASSET CLASS (5 classes @ 20%,
                  split within class), IDM 2.0

Judgement criteria (in order):
  1. H2 Sharpe (recent era 2021+ — where the current config is weakest, 0.36)
  2. Full-period Sharpe and max drawdown
  3. No single instrument carrying the result

More instruments and class-balanced risk are the two canonical TSMOM improvements
(diversification), so this comparison carries low overfitting risk: we are not
tuning signal parameters at all.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from data_daily import INSTRUMENTS, load_all
from sizing import position_series

CAPITAL = 100_000.0
TAU = 0.20
COST_PER_CONTRACT = 3.0
TRADING_DAYS = 256

CURRENT7 = ["ES=F", "NQ=F", "RTY=F", "ZN=F", "CL=F", "GC=F", "6E=F"]
EXPANDED13 = CURRENT7 + ["YM=F", "ZB=F", "6J=F", "SI=F", "HG=F", "NG=F"]


def class_weights(syms: list[str]) -> dict[str, float]:
    """Equal risk per asset class, split equally within class."""
    classes: dict[str, list[str]] = {}
    for s in syms:
        classes.setdefault(INSTRUMENTS[s]["asset"], []).append(s)
    n_classes = len(classes)
    w = {}
    for cls, members in classes.items():
        for s in members:
            w[s] = (1.0 / n_classes) / len(members)
    return w


def net_pnl(data: dict, syms: list[str], weights: dict[str, float], idm: float) -> pd.Series:
    frames = []
    for sym in syms:
        price = data[sym]["close"]
        mult = INSTRUMENTS[sym]["multiplier"]
        pos = position_series(price, mult, CAPITAL, tau=TAU, idm=idm, weight=weights[sym])
        pnl = pos.shift(1) * mult * price.diff()
        cost = pos.diff().abs() * COST_PER_CONTRACT
        frames.append((pnl - cost).fillna(0.0))
    net = pd.concat(frames, axis=1).sum(axis=1)
    return net.loc[net.ne(0).cumsum() > 0]


def stats(net: pd.Series) -> dict:
    ret = net / CAPITAL
    ann_ret = ret.mean() * TRADING_DAYS
    ann_vol = ret.std() * np.sqrt(TRADING_DAYS)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else 0.0
    equity = CAPITAL + net.cumsum()
    dd = ((equity - equity.cummax()) / equity.cummax()).min()
    return {
        "sharpe": round(sharpe, 2),
        "vol%": round(ann_vol * 100, 1),
        "ret%": round(net.sum() / CAPITAL * 100, 1),
        "maxDD%": round(dd * 100, 1),
    }


def halves(net: pd.Series) -> tuple[dict, dict]:
    mid = net.index[len(net) // 2]
    return stats(net.loc[:mid]), stats(net.loc[mid:])


def main() -> None:
    print("Loading 10y daily data for 13 instruments...")
    data = load_all("10y")
    missing = [s for s in EXPANDED13 if s not in data]
    if missing:
        print(f"  WARNING missing: {missing}")
    syms13 = [s for s in EXPANDED13 if s in data]
    syms7 = [s for s in CURRENT7 if s in data]

    configs = {
        "A current7 (1/N, IDM1.5)": (syms7, {s: 1 / len(syms7) for s in syms7}, 1.5),
        "B expanded13 (1/N, IDM2.0)": (syms13, {s: 1 / len(syms13) for s in syms13}, 2.0),
        "C classbal13 (class wts, IDM2.0)": (syms13, class_weights(syms13), 2.0),
    }

    rows = []
    pnl_store = {}
    for name, (syms, w, idm) in configs.items():
        net = net_pnl(data, syms, w, idm)
        full = stats(net)
        h1, h2 = halves(net)
        pnl_store[name] = net
        rows.append({
            "config": name,
            "full_sharpe": full["sharpe"], "full_ret%": full["ret%"], "maxDD%": full["maxDD%"],
            "H1_sharpe": h1["sharpe"], "H2_sharpe": h2["sharpe"], "H2_ret%": h2["ret%"],
        })

    df = pd.DataFrame(rows)
    with pd.option_context("display.width", 200):
        print("\n=== CONFIG COMPARISON ===")
        print(df.to_string(index=False))

    # per-instrument contribution for the expanded set (config C)
    print("\n=== Per-instrument net P&L (config C) ===")
    syms, w, idm = configs["C classbal13 (class wts, IDM2.0)"]
    for sym in syms:
        price = data[sym]["close"]
        mult = INSTRUMENTS[sym]["multiplier"]
        pos = position_series(price, mult, CAPITAL, tau=TAU, idm=idm, weight=w[sym])
        pnl = (pos.shift(1) * mult * price.diff()).fillna(0.0)
        cost = (pos.diff().abs() * COST_PER_CONTRACT).fillna(0.0)
        print(f"  {sym:6s} ({INSTRUMENTS[sym]['asset']:6s}, w={w[sym]:.3f})  ${(pnl-cost).sum():>10,.0f}")

    print("\n=== Class weights used (config C) ===")
    for s, v in class_weights(syms13).items():
        print(f"  {s:6s} {v:.3f}")


if __name__ == "__main__":
    main()
