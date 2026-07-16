"""Backtest the VWAP+RSI scalper on historical 1m OHLCV.

Pulls history via CCXT (no API key required for public OHLCV) and simulates fills
at next-bar open with a configurable fee + spread cost.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass

import ccxt
import pandas as pd

from config import EXCHANGE, RISK, STRATEGY
from indicators import add_indicators
from risk import size_trade
from strategy import should_exit

log = logging.getLogger("backtest")

# Bybit taker fee (linear perp) ~ 0.055%; spread ~ 1bp on BTC. Round-trip cost ≈ 0.12%.
DEFAULT_FEE_BPS = 5.5
DEFAULT_SPREAD_BPS = 1.0


@dataclass
class Trade:
    side: str
    entry_ts: pd.Timestamp
    entry: float
    exit_ts: pd.Timestamp
    exit: float
    qty: float
    pnl_usdt: float
    reason: str


def fetch_ohlcv(symbol: str, timeframe: str, since_ms: int, total_bars: int) -> pd.DataFrame:
    """Page through CCXT OHLCV endpoints until we have `total_bars`."""
    cls = getattr(ccxt, EXCHANGE.name)
    ex = cls({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    if EXCHANGE.testnet and hasattr(ex, "set_sandbox_mode"):
        ex.set_sandbox_mode(False)  # public history is on mainnet
    ex.load_markets()

    rows: list[list] = []
    cursor = since_ms
    page = 1000
    while len(rows) < total_bars:
        chunk = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=page)
        if not chunk:
            break
        rows.extend(chunk)
        cursor = chunk[-1][0] + 1
        if len(chunk) < page:
            break
        time.sleep(ex.rateLimit / 1000.0)

    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates(subset="ts").sort_values("ts")
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("ts")
    return df.iloc[:total_bars]


def simulate(df: pd.DataFrame, fee_bps: float, spread_bps: float, equity_start: float) -> tuple[list[Trade], float]:
    enriched = add_indicators(
        df, STRATEGY.vwap_window, STRATEGY.band_sigma, STRATEGY.rsi_period, STRATEGY.adx_period
    )
    enriched = enriched.dropna(subset=["vwap", "band_upper", "band_lower", "rsi", "adx"])

    trades: list[Trade] = []
    equity = equity_start
    in_pos = False
    side = "none"
    entry = 0.0
    entry_ts = None
    qty = 0.0
    bars_in_trade = 0

    cost_bps = fee_bps + spread_bps  # one-way cost in bps, applied at entry and exit

    for i in range(len(enriched) - 1):
        row = enriched.iloc[i]
        next_open = float(enriched.iloc[i + 1]["open"])

        if not in_pos:
            close = float(row["close"])
            rsi_val = float(row["rsi"])
            upper = float(row["band_upper"])
            lower = float(row["band_lower"])
            adx_val = float(row["adx"])
            sig_side = "none"
            stop = 0.0
            target = float(row["vwap"])
            if adx_val < STRATEGY.adx_max:
                if close <= lower and rsi_val <= STRATEGY.rsi_long_max:
                    sig_side = "long"
                    stop = close * (1 - STRATEGY.stop_pct)
                elif close >= upper and rsi_val >= STRATEGY.rsi_short_min:
                    sig_side = "short"
                    stop = close * (1 + STRATEGY.stop_pct)

            if sig_side == "none":
                continue

            sized = size_trade(sig_side, next_open, stop, target, equity)
            if sized is None:
                continue

            entry_cost = next_open * cost_bps / 10_000.0
            entry = next_open + entry_cost if sig_side == "long" else next_open - entry_cost
            qty = sized.qty
            side = sig_side
            entry_ts = enriched.index[i + 1]
            in_pos = True
            bars_in_trade = 0
            continue

        # In a position — check exit on this bar's close vs VWAP and entry levels.
        bars_in_trade += 1
        current = float(row["close"])
        vwap = float(row["vwap"])
        do_exit, reason = should_exit(side, entry, current, vwap, bars_in_trade)
        if do_exit:
            exit_cost = next_open * cost_bps / 10_000.0
            exit_px = next_open - exit_cost if side == "long" else next_open + exit_cost
            pnl = (exit_px - entry) * qty if side == "long" else (entry - exit_px) * qty
            trades.append(Trade(
                side=side,
                entry_ts=entry_ts,
                entry=entry,
                exit_ts=enriched.index[i + 1],
                exit=exit_px,
                qty=qty,
                pnl_usdt=pnl,
                reason=reason,
            ))
            equity += pnl
            in_pos = False
            side = "none"
            entry = 0.0
            qty = 0.0
            bars_in_trade = 0

    return trades, equity


def report(trades: list[Trade], equity_start: float, equity_end: float) -> None:
    n = len(trades)
    if n == 0:
        print("No trades.")
        return
    pnl = sum(t.pnl_usdt for t in trades)
    wins = [t for t in trades if t.pnl_usdt > 0]
    losses = [t for t in trades if t.pnl_usdt <= 0]
    win_rate = len(wins) / n
    avg_win = sum(t.pnl_usdt for t in wins) / len(wins) if wins else 0.0
    avg_loss = sum(t.pnl_usdt for t in losses) / len(losses) if losses else 0.0
    expectancy = pnl / n
    by_reason = {}
    for t in trades:
        by_reason[t.reason] = by_reason.get(t.reason, 0) + 1

    print(f"Trades:       {n}")
    print(f"Win rate:     {win_rate:.1%}")
    print(f"Avg win:      {avg_win:.2f} USDT")
    print(f"Avg loss:     {avg_loss:.2f} USDT")
    print(f"Expectancy:   {expectancy:.2f} USDT/trade")
    print(f"Net PnL:      {pnl:.2f} USDT")
    print(f"Equity:       {equity_start:.2f} -> {equity_end:.2f} ({(equity_end / equity_start - 1):+.2%})")
    print("Exit reasons: " + ", ".join(f"{k}={v}" for k, v in by_reason.items()))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=7, help="History to backtest, in days")
    p.add_argument("--symbol", default=EXCHANGE.symbol)
    p.add_argument("--fee-bps", type=float, default=DEFAULT_FEE_BPS)
    p.add_argument("--spread-bps", type=float, default=DEFAULT_SPREAD_BPS)
    args = p.parse_args()

    bars_per_day = 1440  # 1m
    total = args.days * bars_per_day
    since_ms = int(time.time() * 1000) - args.days * 86_400_000

    log.info("Fetching %d bars of %s %s ...", total, args.symbol, EXCHANGE.timeframe)
    df = fetch_ohlcv(args.symbol, EXCHANGE.timeframe, since_ms, total)
    log.info("Got %d bars from %s to %s", len(df), df.index[0], df.index[-1])

    trades, equity_end = simulate(df, args.fee_bps, args.spread_bps, RISK.account_size_usdt)
    report(trades, RISK.account_size_usdt, equity_end)


if __name__ == "__main__":
    main()
