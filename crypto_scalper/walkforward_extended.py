"""Extended walk-forward: 6 windows × 2 configs.

- CURRENT: whatever's in config.py now (drifted).
- ORIGINAL: the tight-entry config we originally validated (sigma=2.5, RSI 20/80, ADX<30, stop=0.5%, tstop=60).
"""
from __future__ import annotations

import logging
import sys
import time

import pandas as pd

from backtest import fetch_ohlcv, simulate
from config import EXCHANGE, RISK, STRATEGY

log = logging.getLogger("wf_ext")

SYMBOL = "SOL/USDT:USDT"
TIMEFRAME = "1m"
MAKER_FEE_BPS = 2.0
SPREAD_BPS = 1.0

WINDOWS = [
    ("w1 [0-30d recent]",    30,   0),
    ("w2 [30-60d]",          60,  30),
    ("w3 [60-90d]",          90,  60),
    ("w4 [90-120d]",        120,  90),
    ("w5 [120-150d]",       150, 120),
    ("w6 [150-180d oldest]", 180, 150),
]

# (label, band_sigma, rsi_long_max, rsi_short_min, adx_max, stop_pct, time_stop_bars)
CONFIGS = [
    ("CURRENT (drifted)",  STRATEGY.band_sigma, STRATEGY.rsi_long_max, STRATEGY.rsi_short_min,
                           STRATEGY.adx_max, STRATEGY.stop_pct, STRATEGY.time_stop_bars),
    ("ORIGINAL tight-entry", 2.5, 20.0, 80.0, 30.0, 0.005, 60),
]


def run_config(full: pd.DataFrame, label: str, sigma: float, rsi_l: float, rsi_s: float,
               adx: float, stop: float, tstop: int) -> None:
    STRATEGY.band_sigma = sigma
    STRATEGY.rsi_long_max = rsi_l
    STRATEGY.rsi_short_min = rsi_s
    STRATEGY.adx_max = adx
    STRATEGY.stop_pct = stop
    STRATEGY.time_stop_bars = tstop

    print(f"\n{'='*80}")
    print(f"{label}: sigma={sigma} rsi={rsi_l:.0f}/{rsi_s:.0f} adx<{adx:.0f} stop={stop*100:.2f}% tstop={tstop}b")
    print(f"{'='*80}")
    header = f"{'window':<24}{'trades':>8}{'win%':>8}{'exp':>10}{'pnl':>12}{'return%':>10}{'tgt':>6}"
    print(header)
    print("-" * len(header))
    net_pnl = 0.0
    positive = 0
    for wlabel, dstart, dend in WINDOWS:
        end_ts = full.index[-1] - pd.Timedelta(days=dend)
        start_ts = full.index[-1] - pd.Timedelta(days=dstart)
        sub = full.loc[(full.index >= start_ts) & (full.index < end_ts)].copy()
        if len(sub) < STRATEGY.vwap_window + 100:
            continue
        trades, equity_end = simulate(sub, MAKER_FEE_BPS, SPREAD_BPS, RISK.account_size_usdt)
        n = len(trades)
        wins = sum(1 for t in trades if t.pnl_usdt > 0)
        wr = wins / n if n else 0.0
        pnl = sum(t.pnl_usdt for t in trades)
        exp_ = pnl / n if n else 0.0
        ret_pct = (equity_end / RISK.account_size_usdt - 1) * 100
        tgt = sum(1 for t in trades if t.reason == "target-vwap")
        if pnl > 0:
            positive += 1
        net_pnl += pnl
        print(f"{wlabel:<24}{n:>8}{wr*100:>7.1f}%{exp_:>10.2f}{pnl:>12.2f}{ret_pct:>9.2f}%{tgt:>6}")
    print()
    print(f"NET 180d: {net_pnl:+.2f} USDT  ({net_pnl/RISK.account_size_usdt*100:+.2f}%)   Positive windows: {positive}/{len(WINDOWS)}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    total_days = 185
    bars = total_days * 1440
    since_ms = int(time.time() * 1000) - total_days * 86_400_000
    log.info("Fetching %d bars (%d days) of %s %s ...", bars, total_days, SYMBOL, TIMEFRAME)
    full = fetch_ohlcv(SYMBOL, TIMEFRAME, since_ms, bars)
    log.info("Got %d bars: %s → %s", len(full), full.index[0], full.index[-1])

    for cfg in CONFIGS:
        run_config(full, *cfg)


if __name__ == "__main__":
    main()
