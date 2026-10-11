"""Quick sweep: timeframes, EMA pairs, stop multiples, ADX threshold."""
from __future__ import annotations

import argparse
import logging
import sys
import time

import pandas as pd

from backtest import fetch_ohlcv, report, simulate
from config import EXCHANGE, RISK, STRATEGY

log = logging.getLogger("trend_sweep")

CONFIGS = [
    # (label,    tf,     ema_f, ema_s, adx, stop_atr, target_R)
    ("baseline", "5m",   9,    21,   25.0, 1.5, 2.0),
    ("slow-5m",  "5m",  20,    50,   25.0, 2.0, 2.0),
    ("std-15m",  "15m",  9,    21,   25.0, 1.5, 2.0),
    ("slow-15m", "15m", 20,    50,   25.0, 2.0, 2.0),
    ("slow-1h",  "1h",  20,    50,   25.0, 2.0, 2.5),
    ("strict",   "15m", 20,    50,   30.0, 2.5, 2.5),
]

SYMBOLS = ["SOL/USDT:USDT", "BTC/USDT:USDT"]
WINDOWS = [(30, 0, "recent"), (60, 30, "mid"), (90, 60, "old")]

BARS_PER_DAY = {"1m": 1440, "3m": 480, "5m": 288, "15m": 96, "30m": 48, "1h": 24}


def apply(tf: str, ef: int, es: int, adx: float, stop_atr: float, target_r: float) -> None:
    EXCHANGE.timeframe = tf
    STRATEGY.ema_fast = ef
    STRATEGY.ema_slow = es
    STRATEGY.adx_min = adx
    STRATEGY.atr_stop_mult = stop_atr
    STRATEGY.target_r = target_r


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    p = argparse.ArgumentParser()
    p.add_argument("--total-days", type=int, default=95)
    args = p.parse_args()

    print(f"{'symbol':<18}{'config':<12}{'recent':>14}{'mid':>14}{'old':>14}{'NET 90d':>14}")
    print("-" * 86)

    for sym in SYMBOLS:
        # Fetch each tf once per symbol (cached across configs).
        tf_data: dict[str, pd.DataFrame] = {}
        for cfg in CONFIGS:
            tf = cfg[1]
            if tf in tf_data:
                continue
            bars = args.total_days * BARS_PER_DAY[tf]
            since_ms = int(time.time() * 1000) - args.total_days * 86_400_000
            log.info("fetch %s %s %d bars", sym, tf, bars)
            tf_data[tf] = fetch_ohlcv(sym, tf, since_ms, bars)

        for label, tf, ef, es, adx, sa, tr in CONFIGS:
            apply(tf, ef, es, adx, sa, tr)
            full = tf_data[tf]
            row_returns = []
            for dstart, dend, _ in WINDOWS:
                end_ts = full.index[-1] - pd.Timedelta(days=dend)
                start_ts = full.index[-1] - pd.Timedelta(days=dstart)
                sub = full.loc[(full.index >= start_ts) & (full.index < end_ts)].copy()
                if len(sub) < 100:
                    row_returns.append(0.0)
                    continue
                _, eq = simulate(sub, RISK.account_size_usdt)
                row_returns.append((eq / RISK.account_size_usdt - 1) * 100)
            net = sum(row_returns)
            print(f"{sym:<18}{label:<12}{row_returns[0]:>13.2f}%{row_returns[1]:>13.2f}%{row_returns[2]:>13.2f}%{net:>13.2f}%")
        print()


if __name__ == "__main__":
    main()
