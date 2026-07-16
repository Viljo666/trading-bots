"""Parameter sweep: fetch OHLCV once, run simulate() across variants, print comparison."""
from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass

from backtest import DEFAULT_FEE_BPS, DEFAULT_SPREAD_BPS, fetch_ohlcv, simulate
from config import EXCHANGE, RISK, STRATEGY

log = logging.getLogger("sweep")


@dataclass
class Variant:
    name: str
    vwap_window: int
    band_sigma: float
    rsi_long_max: float
    rsi_short_min: float
    stop_pct: float
    time_stop_bars: int = 30


VARIANTS = [
    Variant("baseline",       1440, 2.0, 25.0, 75.0, 0.003),
    Variant("tight-entry",    1440, 2.5, 20.0, 80.0, 0.005),
    Variant("short-vwap-4h",   240, 2.0, 25.0, 75.0, 0.003),
    Variant("short-vwap+wide", 240, 2.5, 20.0, 80.0, 0.005),
    Variant("ultrashort-1h",    60, 2.0, 25.0, 75.0, 0.003),
    Variant("longer-tstop",   1440, 2.0, 25.0, 75.0, 0.003, time_stop_bars=120),
]


def apply(v: Variant) -> None:
    STRATEGY.vwap_window = v.vwap_window
    STRATEGY.band_sigma = v.band_sigma
    STRATEGY.rsi_long_max = v.rsi_long_max
    STRATEGY.rsi_short_min = v.rsi_short_min
    STRATEGY.stop_pct = v.stop_pct
    STRATEGY.time_stop_bars = v.time_stop_bars


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30)
    args = p.parse_args()

    bars_per_day = 1440
    total = args.days * bars_per_day
    since_ms = int(time.time() * 1000) - args.days * 86_400_000

    log.info("Fetching %d bars (%d days) of %s ...", total, args.days, EXCHANGE.symbol)
    df = fetch_ohlcv(EXCHANGE.symbol, EXCHANGE.timeframe, since_ms, total)
    log.info("Got %d bars from %s to %s\n", len(df), df.index[0], df.index[-1])

    rows: list[tuple] = []
    for v in VARIANTS:
        apply(v)
        trades, equity_end = simulate(df, DEFAULT_FEE_BPS, DEFAULT_SPREAD_BPS, RISK.account_size_usdt)
        n = len(trades)
        wins = [t for t in trades if t.pnl_usdt > 0]
        win_rate = len(wins) / n if n else 0.0
        pnl = sum(t.pnl_usdt for t in trades)
        exp_ = pnl / n if n else 0.0
        target_exits = sum(1 for t in trades if t.reason == "target-vwap")
        rows.append((v.name, n, win_rate, exp_, pnl, target_exits))

    print(f"{'variant':<20}{'trades':>8}{'win%':>8}{'exp':>10}{'pnl':>12}{'tgt-vwap':>10}")
    print("-" * 68)
    for name, n, wr, exp_, pnl, tgt in rows:
        print(f"{name:<20}{n:>8}{wr*100:>7.1f}%{exp_:>10.2f}{pnl:>12.2f}{tgt:>10}")


if __name__ == "__main__":
    main()
