"""Sweep across (symbol, variant) with maker fees.

Tests the same parameter variants as sweep.py but on BTC/ETH/SOL perps,
and assumes maker fees (~2 bps) instead of taker. Caveat: the backtest fills
at next-bar open, which is a taker-style assumption. Lower fees here are an
optimistic proxy for "if the bot used post-only limit orders that filled".
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from backtest import fetch_ohlcv, simulate
from config import EXCHANGE, RISK, STRATEGY
from sweep import VARIANTS, apply

log = logging.getLogger("sweep_pairs")

SYMBOLS = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
MAKER_FEE_BPS = 2.0
SPREAD_BPS = 1.0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30)
    args = p.parse_args()

    bars_per_day = 1440
    total = args.days * bars_per_day
    since_ms = int(time.time() * 1000) - args.days * 86_400_000

    print(f"Sweep: {len(SYMBOLS)} symbols x {len(VARIANTS)} variants, "
          f"{args.days}d, fee={MAKER_FEE_BPS}bps + spread={SPREAD_BPS}bps\n")

    header = f"{'symbol':<18}{'variant':<20}{'trades':>8}{'win%':>8}{'exp':>10}{'pnl':>12}{'tgt':>6}"
    sep = "-" * len(header)

    for sym in SYMBOLS:
        log.info("Fetching %d bars (%d days) of %s ...", total, args.days, sym)
        df = fetch_ohlcv(sym, EXCHANGE.timeframe, since_ms, total)
        log.info("Got %d bars from %s to %s\n", len(df), df.index[0], df.index[-1])

        print(header)
        print(sep)
        for v in VARIANTS:
            apply(v)
            trades, equity_end = simulate(df, MAKER_FEE_BPS, SPREAD_BPS, RISK.account_size_usdt)
            n = len(trades)
            wins = [t for t in trades if t.pnl_usdt > 0]
            win_rate = len(wins) / n if n else 0.0
            pnl = sum(t.pnl_usdt for t in trades)
            exp_ = pnl / n if n else 0.0
            target_exits = sum(1 for t in trades if t.reason == "target-vwap")
            print(f"{sym:<18}{v.name:<20}{n:>8}{win_rate*100:>7.1f}%{exp_:>10.2f}{pnl:>12.2f}{target_exits:>6}")
        print()


if __name__ == "__main__":
    main()
