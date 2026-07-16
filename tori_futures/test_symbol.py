"""Sweep + walk-forward for an arbitrary futures symbol.

Usage:
  ../.venv/bin/python test_symbol.py CL=F
  ../.venv/bin/python test_symbol.py GC=F

Does the full pipeline we did for ES:
  1. load 60d of 15m bars via yfinance
  2. grid search every filter combo
  3. train/test split the surviving configs
  4. report whether the best config holds up out-of-sample

Answers: does the Tori trendline strategy work on this symbol?
"""
from __future__ import annotations

import sys
from dataclasses import replace
from itertools import product

import pandas as pd

from data import DOLLARS_PER_POINT, load_15m
from strategy import Strategy, StrategyConfig, Trade


def evaluate(trades: list[Trade]) -> dict:
    if not trades:
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "points": 0.0, "dollars": 0.0, "pf": 0.0, "max_dd_pts": 0.0}
    pts = [t.pnl_points for t in trades]
    cum = pd.Series(pts).cumsum()
    dd = (cum - cum.cummax()).min()
    wins = [p for p in pts if p > 0]
    losses = [p for p in pts if p <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    dpp = trades[0].dollars_per_point
    return {
        "trades": len(trades),
        "wins": len(wins),
        "win_rate": round(100 * len(wins) / len(trades), 1),
        "points": round(sum(pts), 1),
        "dollars": round(sum(pts) * dpp, 0),
        "pf": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "max_dd_pts": round(dd, 1),
    }


def all_configs(dpp: float) -> list[StrategyConfig]:
    rth_opts = [False, True]
    ema_opts = [None, 50, 200]
    slope_opts = [0.0, 0.5, 1.0]
    atr_opts = [None, 1.0, 1.5, 2.0]
    return [
        StrategyConfig(
            rth_only=r, ema_period=e, min_slope_abs=s, atr_stop_mult=a, dollars_per_point=dpp
        )
        for r, e, s, a in product(rth_opts, ema_opts, slope_opts, atr_opts)
    ]


def run(bars: pd.DataFrame, cfg: StrategyConfig) -> list[Trade]:
    return Strategy(bars=bars, config=cfg).run()


def main(symbol: str = "ES=F") -> None:
    if symbol not in DOLLARS_PER_POINT:
        print(f"Unknown symbol {symbol}. Known: {list(DOLLARS_PER_POINT)}")
        sys.exit(1)
    dpp = DOLLARS_PER_POINT[symbol]

    print(f"\n===== {symbol}  (${dpp}/pt) =====")
    print("Loading bars...")
    bars = load_15m(symbol)
    print(f"  {len(bars)} bars, {bars.index[0]} -> {bars.index[-1]}")

    # full window sweep
    configs = all_configs(dpp)
    rows = []
    for cfg in configs:
        stats = evaluate(run(bars, cfg))
        rows.append({"config": cfg.label(), **stats, "_cfg": cfg})
    full = pd.DataFrame(rows)

    print("\n--- Top 5 full-window by $ profit (>=10 trades) ---")
    worthy = full[full["trades"] >= 10].copy().sort_values("dollars", ascending=False)
    with pd.option_context("display.width", 200, "display.max_colwidth", 60):
        print(worthy.drop(columns="_cfg").head(5).to_string(index=False))

    # train/test split
    split = int(len(bars) * 0.70)
    train, test = bars.iloc[:split], bars.iloc[split:]
    print(f"\n--- Walk-forward: train {len(train)} bars, test {len(test)} bars ---")

    train_rows = []
    for cfg in configs:
        train_rows.append({"config": cfg.label(), **evaluate(run(train, cfg)), "_cfg": cfg})
    train_df = pd.DataFrame(train_rows)
    candidates = train_df[(train_df["trades"] >= 10) & (train_df["pf"] >= 1.10)].sort_values("pf", ascending=False).head(7)

    if candidates.empty:
        print(f"\nVERDICT for {symbol}: no config cleared train bar (PF>=1.10, n>=10). Strategy does NOT work here.")
        return

    oos_rows = []
    for _, row in candidates.iterrows():
        cfg = row["_cfg"]
        test_stats = evaluate(run(test, cfg))
        oos_rows.append(
            {
                "config": cfg.label(),
                "train_pf": row["pf"],
                "train_$": row["dollars"],
                "train_n": row["trades"],
                "test_pf": test_stats["pf"],
                "test_$": test_stats["dollars"],
                "test_n": test_stats["trades"],
            }
        )
    result = pd.DataFrame(oos_rows)
    with pd.option_context("display.width", 200, "display.max_colwidth", 60):
        print(result.to_string(index=False))

    profitable_oos = result[result["test_$"] > 0]
    print(f"\nVERDICT for {symbol}:")
    if profitable_oos.empty:
        print("  NO config stayed profitable out-of-sample. Likely overfit — do NOT add to live.")
    else:
        best = profitable_oos.sort_values("test_$", ascending=False).iloc[0]
        print(f"  {len(profitable_oos)}/{len(result)} configs survived OOS.")
        print(f"  Best: {best['config']}")
        print(f"    train: ${best['train_$']:.0f} PF {best['train_pf']:.2f}  n={best['train_n']}")
        print(f"    test:  ${best['test_$']:.0f} PF {best['test_pf']:.2f}  n={best['test_n']}")
        if best["test_n"] < 5:
            print("  ⚠ Very small OOS sample — treat as exploratory only.")


if __name__ == "__main__":
    symbol = sys.argv[1] if len(sys.argv) > 1 else "ES=F"
    main(symbol)
