"""Opening-Range Breakout backtest on index/commodity futures (RTH session).

Definition (matches the user's idea: 30-min opening range, 5-min break):
  - Opening range = the first `or_minutes` of the 09:30 ET cash session.
  - After the OR window, the first `interval` bar to CLOSE beyond the OR high/low
    is the breakout -> enter that direction at the bar close.
  - Stop = opposite side of the OR (+/- buffer). Target = RR * risk.
  - One trade per session per symbol. No new entry after `cutoff` ET. If neither
    stop nor target is hit, exit at the RTH close (16:00 ET).

Data: yfinance intraday (~60 days only — this is a PRELIMINARY test). Fills at the
breakout bar close with `slippage_ticks` per side + commission. Conservative:
if a bar trades through both stop and target, the stop is assumed first.

Usage:
  python backtest.py                      # 30m OR, 5m break, RR2, all symbols
  python backtest.py --interval 15m       # 30m OR, 15m break (current stock-bot style)
  python backtest.py --rr 3 --direction with_trend
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import time

import pandas as pd
import yfinance as yf

DOLLARS_PER_POINT = {"ES=F": 50.0, "NQ=F": 20.0, "YM=F": 5.0, "RTY=F": 50.0, "CL=F": 1000.0, "GC=F": 100.0,
                     "ES": 50.0, "NQ": 20.0, "YM": 5.0, "RTY": 50.0}
TICK_SIZE = {"ES=F": 0.25, "NQ=F": 0.25, "YM=F": 1.0, "RTY=F": 0.10, "CL=F": 0.01, "GC=F": 0.10,
             "ES": 0.25, "NQ": 0.25, "YM": 1.0, "RTY": 0.10}
PAIRS = ["ES=F", "NQ=F", "YM=F", "RTY=F", "CL=F", "GC=F"]
ET = "America/New_York"
DATA_DIR = "data"


def load_intraday(symbol: str, interval: str, source: str = "yf") -> pd.DataFrame:
    if source == "ibkr":
        import os
        path = os.path.join(os.path.dirname(__file__), DATA_DIR, f"{symbol}_{interval}.csv")
        df = pd.read_csv(path)
        df.index = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(ET)
        return df[["open", "high", "low", "close", "volume"]].dropna()
    df = yf.download(symbol, period="60d", interval=interval, auto_adjust=False,
                     progress=False, threads=False)
    if df.empty:
        raise RuntimeError(f"no data for {symbol}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
    df.index = (df.index.tz_localize("UTC") if df.index.tz is None else df.index).tz_convert(ET)
    return df


@dataclass
class Params:
    or_minutes: int = 30
    cutoff: time = time(11, 0)     # no new breakout entries after this ET time
    rr: float = 2.0
    buffer_ticks: float = 1.0      # stop placed this many ticks beyond OR
    direction: str = "both"        # both | with_trend (long if open>prev RTH close)
    slippage_ticks: float = 1.0
    commission_per_contract: float = 4.0


@dataclass
class Trade:
    symbol: str; date: object; side: str; entry: float; exit: float
    risk_pts: float; pnl_pts: float; pnl_usd: float; reason: str; r_multiple: float


def backtest_symbol(symbol: str, bars: pd.DataFrame, p: Params) -> list[Trade]:
    tick = TICK_SIZE[symbol]; dpp = DOLLARS_PER_POINT[symbol]
    slip = p.slippage_ticks * tick; comm_pts = p.commission_per_contract / dpp
    rth = bars.between_time("09:30", "16:00")
    trades: list[Trade] = []
    prev_close = None

    for day, sess in rth.groupby(rth.index.date):
        if len(sess) < 8:
            prev_close = sess["close"].iloc[-1] if len(sess) else prev_close
            continue
        open_t = sess.index[0]
        or_end = open_t + pd.Timedelta(minutes=p.or_minutes)
        or_bars = sess[sess.index < or_end]
        post = sess[sess.index >= or_end]
        if len(or_bars) == 0 or len(post) == 0:
            prev_close = sess["close"].iloc[-1]; continue
        orh = float(or_bars["high"].max()); orl = float(or_bars["low"].min())
        sess_open = float(sess["open"].iloc[0])

        allow_long = allow_short = True
        if p.direction == "with_trend" and prev_close is not None:
            allow_long = sess_open >= prev_close
            allow_short = sess_open <= prev_close

        entered = None
        for ts, row in post.iterrows():
            if ts.time() >= p.cutoff:
                break
            c = float(row["close"])
            if allow_long and c > orh:
                stop = orl - p.buffer_ticks * tick
                entry = c + slip; risk = entry - stop
                if risk > 0:
                    entered = ("long", entry, stop, entry + p.rr * risk, ts)
                break
            if allow_short and c < orl:
                stop = orh + p.buffer_ticks * tick
                entry = c - slip; risk = stop - entry
                if risk > 0:
                    entered = ("short", entry, stop, entry - p.rr * risk, ts)
                break
        prev_close = float(sess["close"].iloc[-1])
        if entered is None:
            continue

        side, entry, stop, target, ts0 = entered
        rest = sess[sess.index > ts0]
        exit_price = reason = None
        for _, row in rest.iterrows():
            lo, hi = float(row["low"]), float(row["high"])
            hit_stop = lo <= stop if side == "long" else hi >= stop
            hit_tgt = hi >= target if side == "long" else lo <= target
            if hit_stop:
                exit_price, reason = stop, "stop"; break
            if hit_tgt:
                exit_price, reason = target, "target"; break
        if exit_price is None:
            exit_price, reason = float(sess["close"].iloc[-1]), "session_close"

        exit_fill = exit_price - (slip if side == "long" else -slip)
        gross = (exit_fill - entry) if side == "long" else (entry - exit_fill)
        pnl_pts = gross - comm_pts; risk = abs(entry - stop)
        trades.append(Trade(symbol, day, side, entry, exit_fill, risk,
                            pnl_pts, pnl_pts * dpp, reason, pnl_pts / risk))
    return trades


def summarize(name: str, trades: list[Trade]) -> dict:
    if not trades:
        return {"sym": name, "trades": 0}
    df = pd.DataFrame([t.__dict__ for t in trades])
    wins = df[df.pnl_pts > 0]
    gl = -df[df.pnl_pts <= 0].pnl_usd.sum()
    return {"sym": name, "trades": len(df), "win%": round(100*len(wins)/len(df), 1),
            "totR": round(df.r_multiple.sum(), 1), "avgR": round(df.r_multiple.mean(), 3),
            "pnl_$": round(df.pnl_usd.sum(), 0),
            "pf": round(wins.pnl_usd.sum()/gl, 2) if gl > 0 else float("inf")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=PAIRS)
    ap.add_argument("--interval", default="5m", help="break bar size: 5m or 15m")
    ap.add_argument("--or-minutes", type=int, default=30)
    ap.add_argument("--rr", type=float, default=2.0)
    ap.add_argument("--direction", default="both", choices=["both", "with_trend"])
    ap.add_argument("--source", default="yf", choices=["yf", "ibkr"],
                    help="yf=yfinance 60d; ibkr=cached stitched CSVs (~1y)")
    args = ap.parse_args()
    p = Params(or_minutes=args.or_minutes, rr=args.rr, direction=args.direction)

    allt: list[Trade] = []; rows = []
    for sym in args.symbols:
        try:
            bars = load_intraday(sym, args.interval, args.source)
        except Exception as e:
            print(f"{sym}: load failed ({e})"); continue
        tr = backtest_symbol(sym, bars, p); allt.extend(tr); rows.append(summarize(sym, tr))
        print(f"{sym}: {len(tr)} trades, {bars.index[0].date()}->{bars.index[-1].date()}")

    print(f"\n=== ORB {args.or_minutes}m OR / {args.interval} break / RR1:{args.rr} / {args.direction} ===")
    print(pd.DataFrame(rows).to_string(index=False))
    if allt:
        print("\n=== Portfolio ===")
        print(pd.DataFrame([summarize("ALL", allt)]).to_string(index=False))


if __name__ == "__main__":
    main()
