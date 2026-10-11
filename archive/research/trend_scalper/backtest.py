"""Trend-follow backtest with walk-forward.

Simulates: market entry at next-bar open after signal, ATR stop + 1R target →
trailing stop after 1R. Fees: taker on both sides (trend trades hit market).
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

log = logging.getLogger("trend_backtest")

TAKER_FEE_BPS = 5.0
SPREAD_BPS = 1.0


@dataclass
class FilledTrade:
    side: str
    entry_ts: pd.Timestamp
    entry: float
    exit_ts: pd.Timestamp
    exit: float
    qty: float
    pnl_usdt: float
    r_realized: float
    reason: str


def fetch_ohlcv(symbol: str, timeframe: str, since_ms: int, total_bars: int) -> pd.DataFrame:
    cls = getattr(ccxt, EXCHANGE.name)
    ex = cls({"enableRateLimit": True})
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


def simulate(df: pd.DataFrame, equity_start: float) -> tuple[list[FilledTrade], float]:
    """Bar-by-bar simulator. Walks the dataframe, opens/manages a single position at a time."""
    enriched = add_indicators(df, STRATEGY.ema_fast, STRATEGY.ema_slow, STRATEGY.atr_period, STRATEGY.adx_period)
    enriched = enriched.dropna(subset=["ema_fast", "ema_slow", "atr", "adx"])
    if len(enriched) < 50:
        return [], equity_start

    high = enriched["high"].values
    low = enriched["low"].values
    open_ = enriched["open"].values
    close = enriched["close"].values
    fast = enriched["ema_fast"].values
    slow = enriched["ema_slow"].values
    atr_v = enriched["atr"].values
    adx_v = enriched["adx"].values

    cost_bps_in = TAKER_FEE_BPS + SPREAD_BPS
    cost_bps_out = TAKER_FEE_BPS + SPREAD_BPS

    filled: list[FilledTrade] = []
    equity = equity_start

    in_pos = False
    side = "none"
    entry_idx = 0
    entry_px = 0.0
    stop = 0.0
    target = 0.0
    initial_risk = 0.0
    trailing_active = False
    qty = 0.0
    cooldown_until = -1
    last_exit_side: str | None = None

    for i in range(STRATEGY.slope_lookback + 2, len(enriched) - 1):
        if not in_pos:
            if i <= cooldown_until:
                continue
            # Slope of slow EMA
            slow_back = slow[i - STRATEGY.slope_lookback]
            slope = slow[i] - slow_back

            crossed_up = (fast[i - 1] <= slow[i - 1]) and (fast[i] > slow[i])
            crossed_dn = (fast[i - 1] >= slow[i - 1]) and (fast[i] < slow[i])
            adx_ok = adx_v[i] >= STRATEGY.adx_min

            sig_side = "none"
            if adx_ok:
                if crossed_up and slope > 0 and last_exit_side != "long":
                    sig_side = "long"
                elif crossed_dn and slope < 0 and last_exit_side != "short":
                    sig_side = "short"

            if sig_side == "none":
                continue
            # Cooldown re-entry on the same side: skip if we just exited that side
            if last_exit_side == sig_side and i <= cooldown_until:
                continue

            # Enter at next bar's open (taker)
            raw_entry = float(open_[i + 1])
            entry_px = raw_entry * (1 + cost_bps_in / 10_000.0) if sig_side == "long" else raw_entry * (1 - cost_bps_in / 10_000.0)
            atr_at_entry = float(atr_v[i])
            stop = entry_px - STRATEGY.atr_stop_mult * atr_at_entry if sig_side == "long" else entry_px + STRATEGY.atr_stop_mult * atr_at_entry
            initial_risk = abs(entry_px - stop)
            target = entry_px + STRATEGY.target_r * initial_risk if sig_side == "long" else entry_px - STRATEGY.target_r * initial_risk
            dollar_risk = equity * RISK.risk_per_trade_pct
            qty = round(dollar_risk / initial_risk, RISK.qty_decimals) if initial_risk > 0 else 0.0
            if qty <= 0:
                continue
            in_pos = True
            side = sig_side
            entry_idx = i + 1
            trailing_active = False
            continue

        # Manage open position from bar i+1 onward; here `i` is the just-closed bar.
        bar_idx = i  # use this bar's high/low to detect intra-bar hits
        if bar_idx <= entry_idx:
            continue

        h = high[bar_idx]
        l = low[bar_idx]
        atr_now = float(atr_v[bar_idx])
        exit_px = None
        reason = ""

        # 1) Hard stop
        if side == "long" and l <= stop:
            exit_px = stop
            reason = "stop"
        elif side == "short" and h >= stop:
            exit_px = stop
            reason = "stop"

        # 2) Target / trail trigger
        if exit_px is None:
            if side == "long":
                if not trailing_active and h >= target:
                    trailing_active = True
                    # Move stop to entry (breakeven) and trail thereafter
                    stop = max(stop, entry_px)
                if trailing_active:
                    new_stop = float(close[bar_idx]) - STRATEGY.trail_atr_mult * atr_now
                    if new_stop > stop:
                        stop = new_stop
            else:
                if not trailing_active and l <= target:
                    trailing_active = True
                    stop = min(stop, entry_px)
                if trailing_active:
                    new_stop = float(close[bar_idx]) + STRATEGY.trail_atr_mult * atr_now
                    if new_stop < stop:
                        stop = new_stop

        # 3) Opposite cross exit
        if exit_px is None:
            if side == "long" and fast[bar_idx - 1] >= slow[bar_idx - 1] and fast[bar_idx] < slow[bar_idx]:
                exit_px = float(open_[bar_idx + 1]) if bar_idx + 1 < len(enriched) else float(close[bar_idx])
                reason = "cross"
            elif side == "short" and fast[bar_idx - 1] <= slow[bar_idx - 1] and fast[bar_idx] > slow[bar_idx]:
                exit_px = float(open_[bar_idx + 1]) if bar_idx + 1 < len(enriched) else float(close[bar_idx])
                reason = "cross"

        # 4) Time stop
        if exit_px is None and bar_idx - entry_idx >= STRATEGY.time_stop_bars:
            exit_px = float(open_[bar_idx + 1]) if bar_idx + 1 < len(enriched) else float(close[bar_idx])
            reason = "time-stop"

        if exit_px is None:
            continue

        adj_exit = exit_px * (1 - cost_bps_out / 10_000.0) if side == "long" else exit_px * (1 + cost_bps_out / 10_000.0)
        pnl = (adj_exit - entry_px) * qty if side == "long" else (entry_px - adj_exit) * qty
        r_realized = pnl / (equity_start * RISK.risk_per_trade_pct) if equity_start > 0 else 0.0
        equity += pnl
        filled.append(FilledTrade(
            side=side, entry_ts=enriched.index[entry_idx], entry=entry_px,
            exit_ts=enriched.index[bar_idx], exit=adj_exit, qty=qty,
            pnl_usdt=pnl, r_realized=r_realized, reason=reason,
        ))
        last_exit_side = side
        cooldown_until = bar_idx + STRATEGY.cooldown_bars
        in_pos = False

    return filled, equity


def report(filled: list[FilledTrade], equity_start: float, equity_end: float, label: str = "") -> None:
    n = len(filled)
    if n == 0:
        print(f"{label}: no trades")
        return
    wins = [t for t in filled if t.pnl_usdt > 0]
    pnl = sum(t.pnl_usdt for t in filled)
    win_rate = len(wins) / n
    avg_r = sum(t.r_realized for t in filled) / n
    by_reason: dict[str, int] = {}
    for t in filled:
        by_reason[t.reason] = by_reason.get(t.reason, 0) + 1
    ret = (equity_end / equity_start - 1) * 100 if equity_start > 0 else 0.0
    print(f"{label}: trades={n:3d}  win%={win_rate*100:5.1f}  avgR={avg_r:+.2f}  pnl={pnl:+8.2f}  return={ret:+6.2f}%  exits={by_reason}")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--symbol", default=EXCHANGE.symbol)
    p.add_argument("--walkforward", action="store_true")
    args = p.parse_args()

    bars_per_day = {"1m": 1440, "3m": 480, "5m": 288, "15m": 96, "30m": 48, "1h": 24}[EXCHANGE.timeframe]
    if args.walkforward:
        windows = [(30, 0, "recent  [0-30d]"), (60, 30, "mid    [30-60d]"), (90, 60, "oldest [60-90d]")]
        total_days = 95
    else:
        windows = [(args.days, 0, f"last [{args.days}d]")]
        total_days = args.days

    total_bars = total_days * bars_per_day
    since_ms = int(time.time() * 1000) - total_days * 86_400_000
    log.info("Fetching %d bars of %s %s ...", total_bars, args.symbol, EXCHANGE.timeframe)
    df = fetch_ohlcv(args.symbol, EXCHANGE.timeframe, since_ms, total_bars)
    log.info("Got %d bars %s → %s\n", len(df), df.index[0], df.index[-1])

    print(f"Symbol={args.symbol} TF={EXCHANGE.timeframe} "
          f"params: ema={STRATEGY.ema_fast}/{STRATEGY.ema_slow} adx≥{STRATEGY.adx_min:.0f} "
          f"stop={STRATEGY.atr_stop_mult}*ATR target={STRATEGY.target_r}R trail={STRATEGY.trail_atr_mult}*ATR\n")

    net = 0.0
    for dstart, dend, label in windows:
        end_ts = df.index[-1] - pd.Timedelta(days=dend)
        start_ts = df.index[-1] - pd.Timedelta(days=dstart)
        sub = df.loc[(df.index >= start_ts) & (df.index < end_ts)].copy()
        if len(sub) < 100:
            print(f"{label}: insufficient bars")
            continue
        filled, equity_end = simulate(sub, RISK.account_size_usdt)
        report(filled, RISK.account_size_usdt, equity_end, label=label)
        net += equity_end - RISK.account_size_usdt
    if args.walkforward:
        print(f"\nNET 90d: {net:+.2f} USDT  ({net/RISK.account_size_usdt*100:+.2f}%)")


if __name__ == "__main__":
    main()
