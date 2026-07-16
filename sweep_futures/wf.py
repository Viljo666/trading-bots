"""Walk-forward / out-of-sample test — the only thing that detects overfitting.

Optimize the param grid on the IN-SAMPLE window (first `is_frac` of the data),
freeze the best config, then measure it on the OUT-OF-SAMPLE window it never saw.
We also report the OOS-optimal config so the IS->OOS degradation (the overfit
gap) is visible.

Trades are bucketed by entry date, so EMA/swing context is intact across the
split (no warmup gap).
"""
from __future__ import annotations

import itertools

import numpy as np
import pandas as pd

from backtest import Params, backtest_symbol
from data import load_4h

SYMS = ["ES=F", "NQ=F", "YM=F", "CL=F"]
GRID = {
    "swing_lookback": [10, 15, 20, 25, 30],
    "rr": [2.0, 2.5, 3.0, 3.5, 4.0],
    "trend_ema": [100, 200],
}
IS_FRAC = 0.60
MIN_TRADES = 25


def all_trades(bars_by_sym, p):
    rows = []
    for sym, bars in bars_by_sym.items():
        for t in backtest_symbol(sym, bars, p):
            rows.append((t.entry_ts, t.r_multiple))
    if not rows:
        return pd.DataFrame(columns=["entry_ts", "r"])
    return pd.DataFrame(rows, columns=["entry_ts", "r"]).sort_values("entry_ts")


def stats(df):
    if len(df) == 0:
        return {"n": 0, "win": 0, "avgR": 0, "totR": 0, "pf": 0}
    r = df.r.to_numpy()
    pf = r[r > 0].sum() / -r[r < 0].sum() if (r < 0).any() else float("inf")
    return {"n": len(r), "win": (r > 0).mean() * 100, "avgR": r.mean(),
            "totR": r.sum(), "pf": pf}


def main():
    bars_by_sym = {s: load_4h(s) for s in SYMS}
    # split date = IS_FRAC through the global time span
    tmin = min(b.index.min() for b in bars_by_sym.values())
    tmax = max(b.index.max() for b in bars_by_sym.values())
    split = tmin + (tmax - tmin) * IS_FRAC
    print(f"IS:  {tmin.date()} -> {split.date()}")
    print(f"OOS: {split.date()} -> {tmax.date()}\n")

    results = []
    for sl, rr, te in itertools.product(GRID["swing_lookback"], GRID["rr"], GRID["trend_ema"]):
        p = Params(swing_lookback=sl, trend_ema=te, rr=rr)
        df = all_trades(bars_by_sym, p)
        is_df = df[df.entry_ts < split]
        oos_df = df[df.entry_ts >= split]
        if len(is_df) < MIN_TRADES:
            continue
        results.append({
            "swing": sl, "rr": rr, "ema": te,
            "is": stats(is_df), "oos": stats(oos_df),
        })

    # best IS config by avg_R
    is_best = max(results, key=lambda r: r["is"]["avgR"])
    # best OOS config (oracle / hindsight) by avg_R
    oos_best = max((r for r in results if r["oos"]["n"] >= MIN_TRADES),
                   key=lambda r: r["oos"]["avgR"])

    def show(tag, r):
        cfg = f"swing={r['swing']} rr=1:{r['rr']} ema={r['ema']}"
        i, o = r["is"], r["oos"]
        print(f"{tag}\n  {cfg}")
        print(f"  IS : n={i['n']:3d} win={i['win']:.1f}% avgR={i['avgR']:+.3f} PF={i['pf']:.2f}")
        print(f"  OOS: n={o['n']:3d} win={o['win']:.1f}% avgR={o['avgR']:+.3f} PF={o['pf']:.2f}\n")

    print("=== IS-OPTIMAL config, carried to OOS (the honest test) ===")
    show("", is_best)
    print("=== OOS-OPTIMAL config (hindsight — shows the overfit ceiling) ===")
    show("", oos_best)

    # how does our chosen production config (swing20/ema200/rr3) do OOS?
    chosen = next(r for r in results if r["swing"] == 20 and r["ema"] == 200 and r["rr"] == 3.0)
    print("=== Our chosen production config (swing20/ema200/1:3) ===")
    show("", chosen)

    # robustness: how many of all grid configs stay positive OOS?
    valid = [r for r in results if r["oos"]["n"] >= MIN_TRADES]
    pos = sum(1 for r in valid if r["oos"]["avgR"] > 0)
    print(f"=== Grid robustness: {pos}/{len(valid)} configs positive avgR out-of-sample "
          f"({100*pos/len(valid):.0f}%) ===")


if __name__ == "__main__":
    main()
