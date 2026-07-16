"""Prior-day POC fade backtest (Dow / index futures).

Idea (user's): each session tends to sweep the previous day's volume-profile POC.
  - Compute prior RTH day's POC (price level with the most traded volume).
  - At today's 09:30 open: POC ABOVE open -> SHORT limit at POC; POC BELOW -> BUY limit.
  - Enter when price trades to the POC (limit fill). One trade/session.
  - Stop = atr_mult * dailyATR beyond the POC. Target = 2R (1:2).
  - Move stop to BREAKEVEN once price travels be_trigger_frac (33%) toward target.
  - Exit at RTH close if neither stop nor target hit.

Volume profile: each 5m bar's volume is spread evenly across the price bins its
high-low spans (bin = `bin_pts`). POC = highest-volume bin's mid price.

Fills: limit entry & target fill at their price (no adverse slippage); stop and
time exits take `slippage_ticks` adverse + commission. Conservative: a bar that
trades through both stop and target is counted as the stop.

Data: cached IBKR 5m CSVs from ../orb_futures/data (ES/NQ/YM, ~1yr).

Usage:
  python backtest.py                         # YM, atr_mult 0.5
  python backtest.py --symbols YM ES NQ
  python backtest.py --atr-mult 0.75 --no-breakeven
"""
from __future__ import annotations

import argparse
import os
from dataclasses import dataclass

import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "orb_futures", "data")
ET = "America/New_York"
DPP = {"ES": 50.0, "NQ": 20.0, "YM": 5.0}
TICK = {"ES": 0.25, "NQ": 0.25, "YM": 1.0}
BIN_PTS = {"ES": 1.0, "NQ": 4.0, "YM": 10.0}   # volume-profile price bin size


def load(symbol: str) -> pd.DataFrame:
    df = pd.read_csv(os.path.join(DATA_DIR, f"{symbol}_5m.csv"))
    df.index = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(ET)
    return df[["open", "high", "low", "close", "volume"]].dropna()


@dataclass
class Params:
    rr: float = 2.0
    atr_mult: float = 0.5
    be_trigger_frac: float = 0.33
    use_breakeven: bool = True
    reverse: bool = False            # True = trade WITH the sweep (continuation), not fade
    entry_cutoff: str = "15:00"      # no new POC-limit fills after this ET
    slippage_ticks: float = 1.0
    commission_per_contract: float = 4.0


@dataclass
class Trade:
    symbol: str; date: object; side: str; poc: float; day_open: float
    entry: float; stop: float; exit: float; risk_pts: float
    pnl_pts: float; pnl_usd: float; reason: str; r_multiple: float


def session_poc(sess: pd.DataFrame, bin_pts: float) -> float:
    """Volume profile POC: spread each bar's volume across its high-low bins."""
    lo = np.floor(sess["low"].min() / bin_pts) * bin_pts
    hi = np.ceil(sess["high"].max() / bin_pts) * bin_pts
    edges = np.arange(lo, hi + bin_pts, bin_pts)
    if len(edges) < 2:
        return float(sess["close"].iloc[-1])
    vol = np.zeros(len(edges) - 1)
    centers = edges[:-1] + bin_pts / 2
    for h, l, v in zip(sess["high"].to_numpy(), sess["low"].to_numpy(), sess["volume"].to_numpy()):
        if v <= 0 or h < l:
            continue
        i0 = max(0, int((l - lo) // bin_pts))
        i1 = min(len(vol) - 1, int((h - lo) // bin_pts))
        n = i1 - i0 + 1
        vol[i0:i1 + 1] += v / n
    return float(centers[int(vol.argmax())])


def daily_atr(rth: pd.DataFrame, period: int = 14) -> pd.Series:
    """ATR from RTH daily bars, indexed by session date."""
    d = rth.resample("1D").agg({"high": "max", "low": "min", "close": "last"}).dropna()
    tr = pd.concat([d.high - d.low, (d.high - d.close.shift()).abs(),
                    (d.low - d.close.shift()).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()
    atr.index = atr.index.date
    return atr


def backtest_symbol(symbol: str, bars: pd.DataFrame, p: Params) -> list[Trade]:
    tick = TICK[symbol]; dpp = DPP[symbol]; bin_pts = BIN_PTS[symbol]
    slip = p.slippage_ticks * tick; comm_pts = p.commission_per_contract / dpp
    rth = bars.between_time("09:30", "16:00")
    atr = daily_atr(rth)
    cutoff = pd.Timestamp(p.entry_cutoff).time()

    sessions = [(d, s) for d, s in rth.groupby(rth.index.date) if len(s) >= 10]
    trades: list[Trade] = []
    for k in range(1, len(sessions)):
        prev_day, prev = sessions[k - 1]
        day, sess = sessions[k]
        a = atr.get(prev_day, np.nan)
        if np.isnan(a) or a <= 0:
            continue
        poc = session_poc(prev, bin_pts)
        day_open = float(sess["open"].iloc[0])
        if poc == day_open:
            continue
        approach_up = poc > day_open            # price must rally up to reach POC
        side = "short" if approach_up else "long"   # fade the touch
        if p.reverse:
            side = "long" if approach_up else "short"   # trade the continuation
        risk = p.atr_mult * a
        if side == "short":
            stop = poc + risk; target = poc - p.rr * risk
        else:
            stop = poc - risk; target = poc + p.rr * risk

        # find entry: price trades to the POC during RTH (before cutoff)
        entered = False
        rows = list(sess.itertuples())
        ei = None
        for idx in range(len(rows)):
            r = rows[idx]
            if r.Index.time() >= cutoff:
                break
            if (approach_up and r.high >= poc) or (not approach_up and r.low <= poc):
                entered = True; ei = idx; break
        if not entered:
            continue
        entry = poc  # limit fill at POC

        be_armed = False
        be_trigger = entry + (target - entry) * p.be_trigger_frac  # works for both sides
        exit_price = reason = None
        for idx in range(ei + 1, len(rows)):
            r = rows[idx]
            cur_stop = entry if be_armed else stop
            adverse = r.low if side == "long" else r.high
            favorable = r.high if side == "long" else r.low
            hit_stop = adverse <= cur_stop if side == "long" else adverse >= cur_stop
            hit_tgt = favorable >= target if side == "long" else favorable <= target
            if hit_stop:
                exit_price = cur_stop; reason = "be" if be_armed else "stop"; break
            if hit_tgt:
                exit_price = target; reason = "target"; break
            if p.use_breakeven and not be_armed:
                reached = favorable >= be_trigger if side == "long" else favorable <= be_trigger
                if reached:
                    be_armed = True
        if exit_price is None:
            exit_price = float(sess["close"].iloc[-1]); reason = "session_close"

        # slippage on stop/time exits only (limit target/entry fill clean)
        adj = 0.0 if reason == "target" else slip
        exit_fill = exit_price - adj if side == "long" else exit_price + adj
        gross = (exit_fill - entry) if side == "long" else (entry - exit_fill)
        pnl_pts = gross - comm_pts
        trades.append(Trade(symbol, day, side, poc, day_open, entry, stop, exit_fill,
                            risk, pnl_pts, pnl_pts * dpp, reason, pnl_pts / risk))
    return trades


def summarize(name: str, trades: list[Trade]) -> dict:
    if not trades:
        return {"sym": name, "trades": 0}
    df = pd.DataFrame([t.__dict__ for t in trades])
    wins = df[df.pnl_pts > 0]; gl = -df[df.pnl_pts <= 0].pnl_usd.sum()
    return {"sym": name, "trades": len(df), "win%": round(100 * len(wins) / len(df), 1),
            "totR": round(df.r_multiple.sum(), 1), "avgR": round(df.r_multiple.mean(), 3),
            "pnl_$": round(df.pnl_usd.sum(), 0),
            "pf": round(wins.pnl_usd.sum() / gl, 2) if gl > 0 else float("inf"),
            "be%": round(100 * (df.reason == "be").mean(), 0),
            "tgt%": round(100 * (df.reason == "target").mean(), 0)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["YM"])
    ap.add_argument("--rr", type=float, default=2.0)
    ap.add_argument("--atr-mult", type=float, default=0.5)
    ap.add_argument("--be-frac", type=float, default=0.33)
    ap.add_argument("--no-breakeven", action="store_true")
    ap.add_argument("--reverse", action="store_true", help="trade continuation, not fade")
    args = ap.parse_args()
    p = Params(rr=args.rr, atr_mult=args.atr_mult, be_trigger_frac=args.be_frac,
               use_breakeven=not args.no_breakeven, reverse=args.reverse)

    allt: list[Trade] = []; rows = []
    for sym in args.symbols:
        bars = load(sym)
        tr = backtest_symbol(sym, bars, p); allt.extend(tr); rows.append(summarize(sym, tr))
        print(f"{sym}: {len(tr)} trades, {bars.index[0].date()}->{bars.index[-1].date()}")
    print(f"\n=== POC fade / RR1:{args.rr} / ATR×{args.atr_mult} / BE@{int(args.be_frac*100)}%={'on' if not args.no_breakeven else 'OFF'} ===")
    print(pd.DataFrame(rows).to_string(index=False))
    if allt:
        print("\n=== Portfolio ===")
        print(pd.DataFrame([summarize("ALL", allt)]).to_string(index=False))


if __name__ == "__main__":
    main()
