"""Sweep timeframes (1m, 2m, 5m, 15m) on the winning 'tight-entry' params.

VWAP window scales to keep ~24h; time_stop scales to keep ~1h hold limit.
Fees: maker (2 bps) + spread (1 bps).
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from backtest import fetch_ohlcv, simulate
from config import EXCHANGE, RISK, STRATEGY

log = logging.getLogger("sweep_tf")

SYMBOLS = ["BTC/USDT:USDT", "SOL/USDT:USDT"]

# (timeframe, bars_per_day, label)
TIMEFRAMES = [
    ("1m",  1440, "1m"),
    ("3m",   480, "3m"),
    ("5m",   288, "5m"),
    ("15m",   96, "15m"),
]

MAKER_FEE_BPS = 2.0
SPREAD_BPS = 1.0


def apply_tight_entry(bars_per_day: int) -> None:
    """Tight-entry params with vwap window=24h and time stop=1h, scaled to TF."""
    STRATEGY.vwap_window = bars_per_day                    # 24h
    STRATEGY.band_sigma = 2.5
    STRATEGY.rsi_long_max = 20.0
    STRATEGY.rsi_short_min = 80.0
    STRATEGY.stop_pct = 0.005
    STRATEGY.time_stop_bars = max(2, bars_per_day // 24)   # ~1h


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30)
    args = p.parse_args()

    print(f"Sweep: {len(SYMBOLS)} symbols x {len(TIMEFRAMES)} timeframes, "
          f"{args.days}d, tight-entry params, maker={MAKER_FEE_BPS}bps + spread={SPREAD_BPS}bps\n")

    header = f"{'symbol':<18}{'tf':<6}{'trades':>8}{'win%':>8}{'exp':>10}{'pnl':>12}{'tgt':>6}{'time-stop':>11}"
    sep = "-" * len(header)
    print(header)
    print(sep)

    for sym in SYMBOLS:
        for tf, bpd, label in TIMEFRAMES:
            total = args.days * bpd
            since_ms = int(time.time() * 1000) - args.days * 86_400_000
            df = fetch_ohlcv(sym, tf, since_ms, total)
            apply_tight_entry(bpd)
            trades, equity_end = simulate(df, MAKER_FEE_BPS, SPREAD_BPS, RISK.account_size_usdt)
            n = len(trades)
            wins = [t for t in trades if t.pnl_usdt > 0]
            win_rate = len(wins) / n if n else 0.0
            pnl = sum(t.pnl_usdt for t in trades)
            exp_ = pnl / n if n else 0.0
            target_exits = sum(1 for t in trades if t.reason == "target-vwap")
            time_stops = sum(1 for t in trades if t.reason == "time-stop")
            print(f"{sym:<18}{label:<6}{n:>8}{win_rate*100:>7.1f}%{exp_:>10.2f}{pnl:>12.2f}{target_exits:>6}{time_stops:>11}")
        print()


if __name__ == "__main__":
    main()
