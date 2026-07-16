"""Out-of-sample validation.

Split ES 15m data chronologically into train (first 70%) and test (last 30%).
Sweep every filter combo on train-only, pick the top configs by profit factor,
then score those same configs on the untouched test set. A strategy that holds
up out-of-sample keeps making money on data it never saw; one that's overfit
collapses.
"""
from __future__ import annotations

from itertools import product

import pandas as pd

from data import load_es_15m
from strategy import ES_DOLLARS_PER_POINT, Strategy, StrategyConfig, Trade


def evaluate(trades: list[Trade]) -> dict:
    if not trades:
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "points": 0.0, "dollars": 0.0, "pf": 0.0}
    pts = [t.pnl_points for t in trades]
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
    }


def run_config(bars: pd.DataFrame, cfg: StrategyConfig) -> dict:
    strat = Strategy(bars=bars, config=cfg)
    return evaluate(strat.run())


def all_configs() -> list[StrategyConfig]:
    rth_opts = [False, True]
    ema_opts = [None, 50, 200]
    slope_opts = [0.0, 0.5, 1.0]
    atr_opts = [None, 1.0, 1.5, 2.0]
    return [
        StrategyConfig(rth_only=r, ema_period=e, min_slope_abs=s, atr_stop_mult=a)
        for r, e, s, a in product(rth_opts, ema_opts, slope_opts, atr_opts)
    ]


def main() -> None:
    print("Loading ES 15m bars...")
    bars = load_es_15m()
    print(f"  {len(bars)} bars, {bars.index[0]} -> {bars.index[-1]}")

    split = int(len(bars) * 0.70)
    train = bars.iloc[:split]
    test = bars.iloc[split:]
    print(f"  train: {len(train)} bars  {train.index[0]} -> {train.index[-1]}")
    print(f"  test:  {len(test)} bars  {test.index[0]} -> {test.index[-1]}\n")

    configs = all_configs()

    # score every config on train
    train_rows = []
    for cfg in configs:
        stats = run_config(train, cfg)
        train_rows.append({"config": cfg.label(), **stats, "_cfg": cfg})
    train_df = pd.DataFrame(train_rows)

    # candidates: profitable on train with a respectable sample
    candidates = train_df[(train_df["trades"] >= 10) & (train_df["pf"] >= 1.10)].copy()
    candidates = candidates.sort_values("pf", ascending=False).head(10)

    if candidates.empty:
        print("No configs cleared the train-set bar (PF>=1.10, trades>=10). Strategy fails out-of-sample by default.")
        return

    print("=== Top train configs (PF >= 1.10, trades >= 10) ===")
    print(candidates.drop(columns="_cfg").to_string(index=False))

    # evaluate each surviving config on the untouched test window
    print("\n=== Same configs on UNSEEN test set ===")
    rows = []
    for _, row in candidates.iterrows():
        cfg: StrategyConfig = row["_cfg"]
        test_stats = run_config(test, cfg)
        rows.append(
            {
                "config": cfg.label(),
                "train_pf": row["pf"],
                "train_$": row["dollars"],
                "train_trades": row["trades"],
                "test_pf": test_stats["pf"],
                "test_$": test_stats["dollars"],
                "test_trades": test_stats["trades"],
                "test_win_rate": test_stats["win_rate"],
            }
        )
    result = pd.DataFrame(rows)
    with pd.option_context("display.width", 200, "display.max_colwidth", 60):
        print(result.to_string(index=False))

    # summary verdict
    print("\n=== Verdict ===")
    still_profitable = result[result["test_$"] > 0]
    if still_profitable.empty:
        print("None of the train-winners made money on the test set. Strategy is likely overfit.")
    else:
        best = still_profitable.sort_values("test_$", ascending=False).iloc[0]
        print(f"{len(still_profitable)}/{len(result)} configs stayed profitable out-of-sample.")
        print(f"Best OOS: {best['config']}")
        print(f"  train: ${best['train_$']:.0f} (PF {best['train_pf']:.2f}, {best['train_trades']} trades)")
        print(f"  test:  ${best['test_$']:.0f} (PF {best['test_pf']:.2f}, {best['test_trades']} trades)")


if __name__ == "__main__":
    main()
