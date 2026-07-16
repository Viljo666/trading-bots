"""Generic symbol validator — sweep + walk-forward on any yfinance futures symbol.

Usage:
    python validate_symbol.py CL=F
    python validate_symbol.py GC=F
    python validate_symbol.py NQ=F

Reports:
  - Full-window best config
  - Top train-set configs and their out-of-sample test performance
  - Verdict on whether the strategy survives OOS

NOTE: the strategy's RTH filter is hard-coded to US equity hours (9:30-16:00 ET).
For non-equity futures (CL, GC) this won't match their natural liquid hours, so
the `rth_only=True` variants may look worse than they should. The `24h` variants
use all hours regardless.
"""
from __future__ import annotations

import sys
from itertools import product

import pandas as pd

from data import DOLLARS_PER_POINT, load_15m
from strategy import Strategy, StrategyConfig, Trade


def evaluate(trades: list[Trade]) -> dict:
    if not trades:
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "points": 0.0, "dollars": 0.0, "pf": 0.0}
    pts = [t.pnl_points for t in trades]
    wins = [p for p in pts if p > 0]
    losses = [p for p in pts if p <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trades": len(trades),
        "wins": len(wins),
        "win_rate": round(100 * len(wins) / len(trades), 1),
        "points": round(sum(pts), 2),
        "dollars": round(trades[0].dollars_per_point * sum(pts), 0),
        "pf": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
    }


def run(bars: pd.DataFrame, cfg: StrategyConfig) -> dict:
    return evaluate(Strategy(bars=bars, config=cfg).run())


def all_configs(dpp: float) -> list[StrategyConfig]:
    return [
        StrategyConfig(rth_only=r, ema_period=e, min_slope_abs=s, atr_stop_mult=a, dollars_per_point=dpp)
        for r, e, s, a in product(
            [False, True],
            [None, 50, 200],
            [0.0, 0.5, 1.0],
            [None, 1.0, 1.5, 2.0],
        )
    ]


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python validate_symbol.py <yf_symbol>")
        print("Examples: CL=F, GC=F, NQ=F, YM=F")
        sys.exit(1)

    symbol = sys.argv[1]
    dpp = DOLLARS_PER_POINT.get(symbol)
    if dpp is None:
        print(f"Unknown symbol {symbol}. Add it to DOLLARS_PER_POINT in data.py.")
        sys.exit(1)

    print(f"=== Validating {symbol}  (${dpp}/pt) ===\n")

    bars = load_15m(symbol)
    print(f"Bars: {len(bars)}  {bars.index[0]} -> {bars.index[-1]}")

    split = int(len(bars) * 0.70)
    train = bars.iloc[:split]
    test = bars.iloc[split:]
    print(f"Train: {len(train)} bars   Test: {len(test)} bars\n")

    configs = all_configs(dpp)

    # full-window sweep
    full_rows = []
    for cfg in configs:
        full_rows.append({"config": cfg.label(), **run(bars, cfg), "_cfg": cfg})
    full_df = pd.DataFrame(full_rows)

    print("=== Top 10 full-window by $ profit (min 10 trades) ===")
    worthy = full_df[full_df["trades"] >= 10]
    with pd.option_context("display.width", 200, "display.max_colwidth", 60):
        print(worthy.sort_values("dollars", ascending=False).head(10).drop(columns="_cfg").to_string(index=False))

    # train-set sweep
    train_rows = []
    for cfg in configs:
        train_rows.append({"config": cfg.label(), **run(train, cfg), "_cfg": cfg})
    train_df = pd.DataFrame(train_rows)

    candidates = train_df[(train_df["trades"] >= 10) & (train_df["pf"] >= 1.10)].copy()
    candidates = candidates.sort_values("pf", ascending=False).head(10)

    print("\n=== Train-winners (PF>=1.10, trades>=10) ===")
    if candidates.empty:
        print("(none — strategy doesn't clear the train bar on this symbol)")
        print("\n=== Verdict ===")
        print(f"{symbol}: strategy fails to find any profitable train config. DO NOT ADD to live bot.")
        return

    print(candidates.drop(columns="_cfg").to_string(index=False))

    print("\n=== Same configs on unseen test set ===")
    oos_rows = []
    for _, row in candidates.iterrows():
        cfg: StrategyConfig = row["_cfg"]
        t_stats = run(test, cfg)
        oos_rows.append({
            "config": cfg.label(),
            "train_pf": row["pf"],
            "train_$": row["dollars"],
            "train_trades": row["trades"],
            "test_pf": t_stats["pf"],
            "test_$": t_stats["dollars"],
            "test_trades": t_stats["trades"],
            "test_win_rate": t_stats["win_rate"],
        })
    oos = pd.DataFrame(oos_rows)
    with pd.option_context("display.width", 200, "display.max_colwidth", 60):
        print(oos.to_string(index=False))

    print("\n=== Verdict ===")
    survivors = oos[oos["test_$"] > 0]
    if survivors.empty:
        print(f"{symbol}: NONE of the train-winners stayed profitable out-of-sample.")
        print("Strategy is overfit on this symbol. DO NOT add to live bot.")
    else:
        best = survivors.sort_values("test_$", ascending=False).iloc[0]
        print(f"{symbol}: {len(survivors)}/{len(oos)} configs survived OOS.")
        print(f"Best OOS config: {best['config']}")
        print(f"  train: ${best['train_$']:.0f} (PF {best['train_pf']}, {best['train_trades']} trades)")
        print(f"  test:  ${best['test_$']:.0f} (PF {best['test_pf']}, {best['test_trades']} trades)")
        if best["test_trades"] < 5:
            print(f"  WARNING: only {best['test_trades']} test trades — sample too small to trust.")


if __name__ == "__main__":
    main()
