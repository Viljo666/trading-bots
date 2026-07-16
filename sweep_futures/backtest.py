"""Liquidity-sweep reversal backtest on 4h futures candles.

Strategy (per the spec):
  1. A 4h candle SWEEPS the prior candle's extreme:
       - bull sweep:  low[s]  < low[s-1]   (took out sell-side liquidity)
       - bear sweep:  high[s] > high[s-1]  (took out buy-side liquidity)
  2. Within `confirm_window` candles (the sweep candle itself counts), a candle
     CLOSES back through the pre-sweep close (reclaim / confirmation):
       - bull: close[c] > close[s-1]   -> go LONG
       - bear: close[c] < close[s-1]   -> go SHORT
  3. Enter at the NEXT bar's open. Stop = the swept extreme (worst low/high over
     the s..c range). Target = entry +/- RR * risk.  Default RR = 2 (1:2).

Fills are modelled at next-bar open with `slippage_ticks` of adverse slippage on
both entry and exit, plus `commission_per_contract` round-turn. One position per
symbol at a time. If a single bar trades through both stop and target, the stop
is assumed hit first (conservative).

Usage:
    python backtest.py                 # all default pairs
    python backtest.py --symbols ES=F NQ=F
    python backtest.py --rr 3 --confirm-window 2 --no-shorts
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field

import pandas as pd

from data import DOLLARS_PER_POINT, PAIRS, TICK_SIZE, load_4h


@dataclass
class Params:
    rr: float = 2.0
    confirm_window: int = 3      # candles after a sweep in which a reclaim is valid (incl. sweep candle)
    max_hold_bars: int = 60      # force-exit if neither stop nor target hit (~10 days on 4h)
    slippage_ticks: float = 1.0  # adverse ticks per side
    commission_per_contract: float = 4.0  # round-turn $ per contract
    allow_longs: bool = True
    allow_shorts: bool = True
    # --- selectivity filters ---
    swing_lookback: int = 0      # 0 = sweep prior candle; >0 = sweep an N-bar swing extreme
    trend_ema: int = 0           # 0 = no trend filter; else only longs above EMA, shorts below


@dataclass
class Trade:
    symbol: str
    side: str
    entry_ts: pd.Timestamp
    entry: float
    exit_ts: pd.Timestamp
    exit: float
    risk_pts: float
    pnl_pts: float
    pnl_usd: float
    reason: str
    r_multiple: float


def backtest_symbol(symbol: str, bars: pd.DataFrame, p: Params) -> list[Trade]:
    tick = TICK_SIZE.get(symbol, 0.25)
    dpp = DOLLARS_PER_POINT.get(symbol, 1.0)
    slip = p.slippage_ticks * tick
    comm_pts = p.commission_per_contract / dpp  # commission expressed in points

    o = bars["open"].to_numpy()
    h = bars["high"].to_numpy()
    l = bars["low"].to_numpy()
    c = bars["close"].to_numpy()
    ts = bars.index
    n = len(bars)

    # trend filter (EMA of close); only take longs above it / shorts below it
    ema = bars["close"].ewm(span=p.trend_ema, adjust=False).mean().to_numpy() if p.trend_ema else None

    trades: list[Trade] = []
    lb = p.swing_lookback
    i = max(1, lb)
    while i < n - 1:
        # --- detect a sweep on candle i ---
        if lb > 0:
            # swept level = the swing low/high over the lb candles BEFORE i
            prior_low = l[i - lb:i].min()
            prior_high = h[i - lb:i].max()
            bull_sweep = l[i] < prior_low
            bear_sweep = h[i] > prior_high
        else:
            bull_sweep = l[i] < l[i - 1]
            bear_sweep = h[i] > h[i - 1]
        ref_close = c[i - 1]  # the pre-sweep close we must reclaim
        # apply trend filter to the *direction* of the trade
        if ema is not None:
            if bull_sweep and not (c[i] > ema[i] or c[i - 1] > ema[i - 1]):
                bull_sweep = False
            if bear_sweep and not (c[i] < ema[i] or c[i - 1] < ema[i - 1]):
                bear_sweep = False

        signal = None  # ("long"/"short", entry_idx, stop_price)
        if bull_sweep and p.allow_longs:
            stop = l[i]
            for cdl in range(i, min(i + p.confirm_window, n - 1) + 1):
                stop = min(stop, l[cdl])
                if c[cdl] > ref_close:                 # reclaim above pre-sweep close
                    if cdl + 1 < n:
                        signal = ("long", cdl + 1, stop)
                    break
        elif bear_sweep and p.allow_shorts:
            stop = h[i]
            for cdl in range(i, min(i + p.confirm_window, n - 1) + 1):
                stop = max(stop, h[cdl])
                if c[cdl] < ref_close:                 # reclaim below pre-sweep close
                    if cdl + 1 < n:
                        signal = ("short", cdl + 1, stop)
                    break

        if signal is None:
            i += 1
            continue

        side, e_idx, stop = signal
        entry = o[e_idx] + (slip if side == "long" else -slip)
        if side == "long":
            risk = entry - stop
        else:
            risk = stop - entry
        if risk <= 0:
            i += 1
            continue
        target = entry + p.rr * risk if side == "long" else entry - p.rr * risk

        # --- walk forward bar-by-bar to resolve the trade ---
        exit_price = exit_ts = reason = None
        for j in range(e_idx, min(e_idx + p.max_hold_bars, n)):
            hit_stop = l[j] <= stop if side == "long" else h[j] >= stop
            hit_tgt = h[j] >= target if side == "long" else l[j] <= target
            if hit_stop:  # conservative: stop checked first
                exit_price, exit_ts, reason = stop, ts[j], "stop"
                break
            if hit_tgt:
                exit_price, exit_ts, reason = target, ts[j], "target"
                break
        if exit_price is None:  # ran out of bars / hit max hold
            j = min(e_idx + p.max_hold_bars, n) - 1
            exit_price, exit_ts, reason = c[j], ts[j], "time"

        exit_fill = exit_price - (slip if side == "long" else -slip)
        gross = (exit_fill - entry) if side == "long" else (entry - exit_fill)
        pnl_pts = gross - comm_pts
        trades.append(Trade(
            symbol=symbol, side=side, entry_ts=ts[e_idx], entry=entry,
            exit_ts=exit_ts, exit=exit_fill, risk_pts=risk,
            pnl_pts=pnl_pts, pnl_usd=pnl_pts * dpp, reason=reason,
            r_multiple=pnl_pts / risk,
        ))
        # resume scanning after this trade closes (no overlap)
        i = max(i + 1, j + 1)

    return trades


def summarize(symbol: str, trades: list[Trade]) -> dict:
    if not trades:
        return {"symbol": symbol, "trades": 0}
    df = pd.DataFrame([t.__dict__ for t in trades])
    wins = df[df.pnl_pts > 0]
    gross_win = wins.pnl_usd.sum()
    gross_loss = -df[df.pnl_pts <= 0].pnl_usd.sum()
    return {
        "symbol": symbol,
        "trades": len(df),
        "win%": round(100 * len(wins) / len(df), 1),
        "tot_R": round(df.r_multiple.sum(), 1),
        "avg_R": round(df.r_multiple.mean(), 3),
        "expectancy_$": round(df.pnl_usd.mean(), 1),
        "pnl_$": round(df.pnl_usd.sum(), 0),
        "pf": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "longs": int((df.side == "long").sum()),
        "shorts": int((df.side == "short").sum()),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=PAIRS)
    ap.add_argument("--period", default="730d")
    ap.add_argument("--rr", type=float, default=2.0)
    ap.add_argument("--confirm-window", type=int, default=3)
    ap.add_argument("--max-hold-bars", type=int, default=60)
    ap.add_argument("--slippage-ticks", type=float, default=1.0)
    ap.add_argument("--no-longs", action="store_true")
    ap.add_argument("--no-shorts", action="store_true")
    ap.add_argument("--swing-lookback", type=int, default=0,
                    help="0=sweep prior candle; N=sweep an N-bar swing extreme")
    ap.add_argument("--trend-ema", type=int, default=0,
                    help="0=off; else longs only above EMA(span), shorts only below")
    ap.add_argument("--csv", help="optional path to dump all trades")
    args = ap.parse_args()

    p = Params(
        rr=args.rr, confirm_window=args.confirm_window, max_hold_bars=args.max_hold_bars,
        slippage_ticks=args.slippage_ticks,
        allow_longs=not args.no_longs, allow_shorts=not args.no_shorts,
        swing_lookback=args.swing_lookback, trend_ema=args.trend_ema,
    )

    all_trades: list[Trade] = []
    rows = []
    for sym in args.symbols:
        try:
            bars = load_4h(sym, args.period)
        except Exception as e:
            print(f"{sym}: data load failed ({e})")
            continue
        tr = backtest_symbol(sym, bars, p)
        all_trades.extend(tr)
        rows.append(summarize(sym, tr))
        span = f"{bars.index[0].date()}->{bars.index[-1].date()}"
        print(f"{sym}: {len(tr)} trades over {len(bars)} bars ({span})")

    print("\n=== Per-symbol summary ===")
    sumdf = pd.DataFrame(rows)
    print(sumdf.to_string(index=False))

    if all_trades:
        agg = summarize("ALL", all_trades)
        print("\n=== Portfolio (1 contract each, equal weight) ===")
        print(pd.DataFrame([agg]).to_string(index=False))

    if args.csv and all_trades:
        pd.DataFrame([t.__dict__ for t in all_trades]).to_csv(args.csv, index=False)
        print(f"\nWrote {len(all_trades)} trades -> {args.csv}")


if __name__ == "__main__":
    main()
