"""Walk-forward validation: same params, three non-overlapping 30-day windows.

Tests SOL/USDT perp at 1m, 3m, 5m across days [0-30], [30-60], [60-90] back from now.
If edge holds in all three windows, the strategy generalizes. If only the most
recent window is positive, it was curve-fit / lucky.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import timedelta

import pandas as pd

from backtest import fetch_ohlcv, simulate
from config import EXCHANGE, RISK, STRATEGY

log = logging.getLogger("walkforward")

SYMBOL = "SOL/USDT:USDT"

TIMEFRAMES = [
    ("1m",  1440),
    ("3m",   480),
]

# ADX threshold sweep. 100 effectively disables the filter (always trade).
ADX_THRESHOLDS = [100.0, 30.0, 25.0, 20.0]

# (label, days_back_start, days_back_end). days_back_start is the older edge.
WINDOWS = [
    ("recent  [0-30d]",  30,  0),
    ("mid    [30-60d]",  60, 30),
    ("oldest [60-90d]",  90, 60),
]

MAKER_FEE_BPS = 2.0
SPREAD_BPS = 1.0


def apply_tight_entry(bars_per_day: int, adx_max: float) -> None:
    STRATEGY.vwap_window = bars_per_day                    # 24h
    STRATEGY.band_sigma = 2.5
    STRATEGY.rsi_long_max = 20.0
    STRATEGY.rsi_short_min = 80.0
    STRATEGY.stop_pct = 0.005
    STRATEGY.time_stop_bars = max(2, bars_per_day // 24)   # ~1h
    STRATEGY.adx_max = adx_max


def slice_window(df: pd.DataFrame, days_back_start: int, days_back_end: int) -> pd.DataFrame:
    end = df.index[-1] - timedelta(days=days_back_end)
    start = df.index[-1] - timedelta(days=days_back_start)
    return df.loc[(df.index >= start) & (df.index < end)].copy()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    p = argparse.ArgumentParser()
    p.add_argument("--total-days", type=int, default=95, help="History fetched (covers all 3 windows + buffer)")
    args = p.parse_args()

    print(f"Walk-forward: {SYMBOL}, tight-entry params, "
          f"maker={MAKER_FEE_BPS}bps + spread={SPREAD_BPS}bps\n")

    header = f"{'tf':<4}{'adx<':<6}{'window':<22}{'trades':>8}{'win%':>8}{'exp':>10}{'pnl':>12}{'return%':>10}"
    sep = "-" * len(header)

    for tf, bpd in TIMEFRAMES:
        total = args.total_days * bpd
        since_ms = int(time.time() * 1000) - args.total_days * 86_400_000
        log.info("Fetching %d bars of %s %s ...", total, SYMBOL, tf)
        full = fetch_ohlcv(SYMBOL, tf, since_ms, total)
        log.info("Got %d bars from %s to %s\n", len(full), full.index[0], full.index[-1])

        print(header)
        print(sep)
        for adx_max in ADX_THRESHOLDS:
            net_pnl = 0.0
            for label, dstart, dend in WINDOWS:
                sub = slice_window(full, dstart, dend)
                if len(sub) < bpd * 5:
                    print(f"{tf:<4}{adx_max:<6.0f}{label:<22}{'(insufficient bars)':>50}")
                    continue
                apply_tight_entry(bpd, adx_max)
                trades, equity_end = simulate(sub, MAKER_FEE_BPS, SPREAD_BPS, RISK.account_size_usdt)
                n = len(trades)
                wins = [t for t in trades if t.pnl_usdt > 0]
                win_rate = len(wins) / n if n else 0.0
                pnl = sum(t.pnl_usdt for t in trades)
                exp_ = pnl / n if n else 0.0
                ret_pct = (equity_end / RISK.account_size_usdt - 1) * 100
                net_pnl += pnl
                tag = f"<{adx_max:.0f}" if adx_max < 100 else "off"
                print(f"{tf:<4}{tag:<6}{label:<22}{n:>8}{win_rate*100:>7.1f}%{exp_:>10.2f}{pnl:>12.2f}{ret_pct:>9.2f}%")
            tag = f"<{adx_max:.0f}" if adx_max < 100 else "off"
            print(f"{tf:<4}{tag:<6}{'NET 90d':<22}{'':>8}{'':>8}{'':>10}{net_pnl:>12.2f}{(net_pnl/RISK.account_size_usdt)*100:>9.2f}%")
            print()
        print()


if __name__ == "__main__":
    main()
