"""Walk-forward test for the POC-continuation strategy.

Grid-optimize on the IN-SAMPLE first 60% of sessions, freeze the best config,
measure on the untouched final 40%. Also reports the OOS-optimal (hindsight)
config and grid-wide OOS robustness, mirroring sweep_futures/wf.py.

Direction (continuation vs fade) is part of the grid — the direction flip came
from looking at the data, so it must prove itself in-sample alone.
"""
from __future__ import annotations

import itertools

import pandas as pd

from backtest import Params, backtest_symbol, load

SYMS = ["YM", "ES"]          # NQ already shown negative; Dow was the user's target
GRID = {
    "reverse": [True, False],            # continuation vs fade
    "atr_mult": [0.25, 0.5, 0.75, 1.0],
    "rr": [1.0, 1.5, 2.0, 3.0],
    "use_breakeven": [True, False],
}
IS_FRAC = 0.60
MIN_TRADES = 20


def stats(df: pd.DataFrame) -> dict:
    if len(df) == 0:
        return {"n": 0, "win": 0.0, "avgR": 0.0, "totR": 0.0, "pf": 0.0}
    r = df.r_multiple
    gl = -r[r <= 0].sum()
    return {"n": len(r), "win": (r > 0).mean() * 100, "avgR": r.mean(),
            "totR": r.sum(), "pf": (r[r > 0].sum() / gl) if gl > 0 else float("inf")}


def main() -> None:
    bars = {s: load(s) for s in SYMS}
    results = []
    for rev, am, rr, be in itertools.product(GRID["reverse"], GRID["atr_mult"],
                                             GRID["rr"], GRID["use_breakeven"]):
        p = Params(rr=rr, atr_mult=am, use_breakeven=be, reverse=rev)
        rows = []
        for s in SYMS:
            rows += [t.__dict__ for t in backtest_symbol(s, bars[s], p)]
        if not rows:
            continue
        df = pd.DataFrame(rows).sort_values("date")
        split = df.iloc[int(len(df) * IS_FRAC)]["date"]
        is_df, oos_df = df[df.date < split], df[df.date >= split]
        if len(is_df) < MIN_TRADES:
            continue
        results.append({"reverse": rev, "atr": am, "rr": rr, "be": be,
                        "is": stats(is_df), "oos": stats(oos_df)})

    def show(r: dict) -> None:
        cfg = (f"{'CONTINUATION' if r['reverse'] else 'FADE'} atr={r['atr']} "
               f"rr=1:{r['rr']} breakeven={'on' if r['be'] else 'off'}")
        i, o = r["is"], r["oos"]
        print(f"  {cfg}")
        print(f"  IS : n={i['n']:3d} win={i['win']:.1f}% avgR={i['avgR']:+.3f} PF={i['pf']:.2f}")
        print(f"  OOS: n={o['n']:3d} win={o['win']:.1f}% avgR={o['avgR']:+.3f} PF={o['pf']:.2f}\n")

    is_best = max(results, key=lambda r: r["is"]["avgR"])
    valid = [r for r in results if r["oos"]["n"] >= MIN_TRADES]
    oos_best = max(valid, key=lambda r: r["oos"]["avgR"])

    print("=== IS-OPTIMAL config carried to OOS (the honest test) ===")
    show(is_best)
    print("=== OOS-OPTIMAL (hindsight ceiling) ===")
    show(oos_best)

    # user's-spec continuation variant: atr 0.5, rr 2, BE on
    chosen = next((r for r in results
                   if r["reverse"] and r["atr"] == 0.5 and r["rr"] == 2.0 and r["be"]), None)
    if chosen:
        print("=== Continuation @ user's spec (atr0.5 / 1:2 / BE-on) ===")
        show(chosen)

    cont = [r for r in valid if r["reverse"]]
    fade = [r for r in valid if not r["reverse"]]
    pos_c = sum(1 for r in cont if r["oos"]["avgR"] > 0)
    pos_f = sum(1 for r in fade if r["oos"]["avgR"] > 0)
    print(f"=== Grid robustness OOS: continuation {pos_c}/{len(cont)} positive | "
          f"fade {pos_f}/{len(fade)} positive ===")


if __name__ == "__main__":
    main()
