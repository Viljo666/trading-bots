"""Run the Tori trendline strategy over historical ES 15m bars and report stats."""
from __future__ import annotations

import pandas as pd

from data import load_es_15m
from strategy import ES_DOLLARS_PER_POINT, Strategy, Trade


def summarize(trades: list[Trade]) -> dict:
    if not trades:
        return {"trades": 0}

    wins = [t for t in trades if t.pnl_points > 0]
    losses = [t for t in trades if t.pnl_points <= 0]
    total_points = sum(t.pnl_points for t in trades)
    gross_win = sum(t.pnl_points for t in wins)
    gross_loss = abs(sum(t.pnl_points for t in losses))

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1),
        "total_points": round(total_points, 2),
        "total_dollars": round(total_points * ES_DOLLARS_PER_POINT, 2),
        "avg_win_points": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss_points": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
    }


def trades_table(trades: list[Trade]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "direction": t.direction,
                "entry_time": t.entry_time,
                "entry": round(t.entry_price, 2),
                "exit_time": t.exit_time,
                "exit": round(t.exit_price, 2) if t.exit_price is not None else None,
                "points": round(t.pnl_points, 2),
                "dollars": round(t.pnl_dollars, 2),
                "reason": t.exit_reason,
            }
            for t in trades
        ]
    )


def main() -> None:
    print("Loading ES 15m bars from yfinance...")
    bars = load_es_15m()
    print(f"  {len(bars)} bars, {bars.index[0]} -> {bars.index[-1]}")

    print("Running strategy...")
    strat = Strategy(bars=bars)
    trades = strat.run()

    stats = summarize(trades)
    print("\n=== Results ===")
    for k, v in stats.items():
        print(f"  {k:16s} {v}")

    if trades:
        print("\n=== Trades ===")
        with pd.option_context("display.max_rows", None, "display.width", 200):
            print(trades_table(trades).to_string(index=False))


if __name__ == "__main__":
    main()
