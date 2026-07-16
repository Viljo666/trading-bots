"""Diagnose live vs backtest divergence.

Re-runs the backtest on the exact time period the live paper bot was running,
then compares aggregate stats. If backtest also loses on this window → strategy
honest but in a bad regime. If backtest still makes money → simulator bug.

Also looks at trade-level overlap: did live take trades the backtest skipped?
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from backtest import DEFAULT_FEE_BPS, DEFAULT_SPREAD_BPS, fetch_ohlcv, simulate
from config import EXCHANGE, JOURNAL_DIR, RISK, STRATEGY


def load_live_trades() -> list[dict]:
    events: list[dict] = []
    for f in sorted(JOURNAL_DIR.glob("paper_*.jsonl")):
        for line in f.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    for e in events:
        e["ts"] = datetime.fromisoformat(e["ts"])
    events.sort(key=lambda e: e["ts"])

    trades: list[dict] = []
    active: dict | None = None
    for e in events:
        if e.get("event") == "open":
            active = e
        elif e.get("event") == "close" and active is not None:
            trades.append({
                "side": active["side"],
                "open_ts": active["ts"],
                "open_px": float(active["price"]),
                "close_ts": e["ts"],
                "close_px": float(e["price"]),
                "qty": float(active["qty"]),
                "pnl": float(e["pnl"]),
            })
            active = None
    return trades


def main() -> None:
    live_trades = load_live_trades()
    if not live_trades:
        print("No live trades to compare against.")
        return

    first_ts = live_trades[0]["open_ts"]
    last_ts = live_trades[-1]["close_ts"]
    print(f"Live window: {first_ts:%Y-%m-%d %H:%M} → {last_ts:%Y-%m-%d %H:%M} UTC")
    print(f"Live trades: {len(live_trades)}")

    # Fetch OHLCV covering the live window + warmup buffer.
    days_back = max(2, int((datetime.now(timezone.utc) - first_ts).total_seconds() / 86400) + 2)
    bars = days_back * 1440  # 1m bars
    since_ms = int(first_ts.timestamp() * 1000) - STRATEGY.vwap_window * 60 * 1000  # warmup
    print(f"\nFetching {bars} bars of {EXCHANGE.symbol} 1m (warmup-extended)...")
    df = fetch_ohlcv(EXCHANGE.symbol, "1m", since_ms, bars + STRATEGY.vwap_window + 50)
    print(f"Got {len(df)} bars: {df.index[0]} → {df.index[-1]}")

    # Slice to the live window for the backtest.
    bt_window = df.loc[df.index >= pd.Timestamp(first_ts) - pd.Timedelta(minutes=STRATEGY.vwap_window)]
    bt_window = bt_window.loc[bt_window.index <= pd.Timestamp(last_ts) + pd.Timedelta(hours=1)]

    print(f"\nRe-running backtest over the same period with maker fees ({2.0} bps)...")
    bt_trades, equity_end = simulate(bt_window, fee_bps=2.0, spread_bps=1.0,
                                     equity_start=RISK.account_size_usdt)

    # ---- Aggregate comparison ----
    def stats(trades, name):
        n = len(trades)
        if n == 0:
            print(f"  {name}: no trades")
            return
        if isinstance(trades[0], dict):
            pnls = [t["pnl"] for t in trades]
        else:
            pnls = [t.pnl_usdt for t in trades]
        wins = sum(1 for p in pnls if p > 0)
        pnl = sum(pnls)
        wr = wins / n
        print(f"  {name:<20} n={n:3d}  win%={wr*100:5.1f}  pnl={pnl:+7.2f}  avg={pnl/n:+.3f}")

    print(f"\n{'='*64}")
    print("AGGREGATE COMPARISON")
    print(f"{'='*64}")
    stats(live_trades, "LIVE (paper)")
    stats(bt_trades, "BACKTEST (same window)")

    # ---- Time-overlap analysis: did backtest take similar trades? ----
    print(f"\n{'='*64}")
    print("TRADE-LEVEL OVERLAP")
    print(f"{'='*64}")
    print("Backtest trades during live window:")
    print(f"  {'side':<6}{'open':<20}{'close':<20}{'pnl':>10}{'reason':>12}")
    for t in bt_trades:
        if t.entry_ts < pd.Timestamp(first_ts) or t.exit_ts > pd.Timestamp(last_ts):
            continue
        print(f"  {t.side:<6}{t.entry_ts.strftime('%Y-%m-%d %H:%M'):<20}"
              f"{t.exit_ts.strftime('%Y-%m-%d %H:%M'):<20}{t.pnl_usdt:>+10.2f}{t.reason:>12}")

    print(f"\nLive trades (for reference):")
    print(f"  {'side':<6}{'open':<20}{'close':<20}{'pnl':>10}")
    for t in live_trades:
        print(f"  {t['side']:<6}{t['open_ts'].strftime('%Y-%m-%d %H:%M'):<20}"
              f"{t['close_ts'].strftime('%Y-%m-%d %H:%M'):<20}{t['pnl']:>+10.2f}")


if __name__ == "__main__":
    main()
