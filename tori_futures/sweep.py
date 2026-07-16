"""Grid-search filter combinations to find the best Tori-trendline config on ES 15m."""
from __future__ import annotations

from itertools import product

import pandas as pd

from data import load_es_15m
from strategy import ES_DOLLARS_PER_POINT, Strategy, StrategyConfig, Trade


def evaluate(trades: list[Trade]) -> dict:
    if not trades:
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "points": 0.0, "dollars": 0.0, "pf": 0.0, "max_dd_pts": 0.0}

    pts = [t.pnl_points for t in trades]
    cum = pd.Series(pts).cumsum()
    peak = cum.cummax()
    dd = (cum - peak).min()

    wins = [p for p in pts if p > 0]
    losses = [p for p in pts if p <= 0]
    total = sum(pts)
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    return {
        "trades": len(trades),
        "wins": len(wins),
        "win_rate": round(100 * len(wins) / len(trades), 1),
        "points": round(total, 1),
        "dollars": round(total * ES_DOLLARS_PER_POINT, 0),
        "pf": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "max_dd_pts": round(dd, 1),
    }


def main() -> None:
    print("Loading ES 15m bars...")
    bars = load_es_15m()
    print(f"  {len(bars)} bars, {bars.index[0]} -> {bars.index[-1]}\n")

    # filter grids
    rth_opts = [False, True]
    ema_opts = [None, 50, 200]
    slope_opts = [0.0, 0.5, 1.0]           # min |points/bar|
    atr_opts = [None, 1.0, 1.5, 2.0]       # ATR multipliers for initial stop

    rows = []
    for rth, ema, slope, atr in product(rth_opts, ema_opts, slope_opts, atr_opts):
        cfg = StrategyConfig(
            rth_only=rth,
            ema_period=ema,
            min_slope_abs=slope,
            atr_stop_mult=atr,
        )
        strat = Strategy(bars=bars, config=cfg)
        trades = strat.run()
        stats = evaluate(trades)
        rows.append({"config": cfg.label(), **stats})

    df = pd.DataFrame(rows)

    print("=== Full grid (sorted by $ profit) ===")
    with pd.option_context("display.max_rows", None, "display.width", 200, "display.max_colwidth", 60):
        print(df.sort_values("dollars", ascending=False).to_string(index=False))

    print("\n=== Top 10 by profit factor (min 10 trades) ===")
    worthy = df[df["trades"] >= 10].copy()
    with pd.option_context("display.width", 200, "display.max_colwidth", 60):
        print(worthy.sort_values("pf", ascending=False).head(10).to_string(index=False))

    print("\n=== Top 10 by $ profit (min 10 trades) ===")
    with pd.option_context("display.width", 200, "display.max_colwidth", 60):
        print(worthy.sort_values("dollars", ascending=False).head(10).to_string(index=False))


if __name__ == "__main__":
    main()
