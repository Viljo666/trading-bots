"""Build continuous intraday futures history from IBKR by stitching dated contracts.

ContFuture can't page intraday history (error 10339 + times out past ~1 month), so
we fetch each quarterly contract (H/M/U/Z) during the window it was front month
[prev_expiry, expiry] and concatenate. Paging is done in 1-month chunks (the
largest 5-min request IBKR reliably returns). Back-adjustment is irrelevant for
ORB (every trade is intraday), and using each contract only inside its front-month
window keeps us on the liquid leg.

Run:  ../.venv/bin/python ibkr_data.py --interval 5m --quarters 5
"""
from __future__ import annotations

import argparse
import random
import time
from pathlib import Path

import pandas as pd
from ib_insync import IB, Future

DATA_DIR = Path(__file__).resolve().parent / "data"
DATA_DIR.mkdir(exist_ok=True)
ET = "America/New_York"

SPECS = {"ES": "CME", "NQ": "CME", "YM": "CBOT", "RTY": "CME"}
BAR = {"5m": "5 mins", "15m": "15 mins"}


def recent_quarters(n: int) -> list[str]:
    """Last n+1 quarterly contract months as 'YYYYMM', newest first."""
    now = pd.Timestamp.utcnow()
    # next quarterly expiry month >= current month
    y, m = now.year, now.month
    qm = ((m - 1) // 3 + 1) * 3  # 3,6,9,12 for the current quarter
    if now.month > qm or (now.month == qm and now.day > 18):
        qm += 3
    if qm > 12:
        qm -= 12; y += 1
    out = []
    for _ in range(n + 1):
        out.append(f"{y}{qm:02d}")
        qm -= 3
        if qm < 1:
            qm += 12; y -= 1
    return out


def _to_df(bars) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    return pd.DataFrame([{
        "timestamp": pd.Timestamp(b.date, tz="UTC") if pd.Timestamp(b.date).tzinfo is None
        else pd.Timestamp(b.date),
        "open": float(b.open), "high": float(b.high), "low": float(b.low),
        "close": float(b.close), "volume": float(b.volume or 0),
    } for b in bars]).set_index("timestamp")


def fetch_symbol(ib: IB, symbol: str, exchange: str, bar: str, quarters: int) -> pd.DataFrame:
    months = recent_quarters(quarters)              # e.g. [202609, 202606, ...]
    frames: list[pd.DataFrame] = []
    for i in range(quarters):                        # one window per active contract
        ym = months[i]
        prev_expiry_ym = months[i + 1]
        fut = Future(symbol, lastTradeDateOrContractMonth=ym, exchange=exchange,
                     currency="USD", includeExpired=True)
        try:
            ib.qualifyContracts(fut)
        except Exception as e:
            print(f"  {symbol} {ym}: qualify failed ({str(e)[:40]})"); continue
        if len(fut.lastTradeDateOrContractMonth) != 8:
            print(f"  {symbol} {ym}: not resolved (no security definition); skip"); continue
        expiry = pd.Timestamp(fut.lastTradeDateOrContractMonth, tz="UTC")
        win_start = pd.Timestamp(prev_expiry_ym + "15", tz="UTC")  # ~prior expiry
        # Expired contracts have no data "now" — page back from the expiry date.
        now = pd.Timestamp.utcnow()
        end = "" if expiry > now else fut.lastTradeDateOrContractMonth + " 17:00:00"
        got: list[pd.DataFrame] = []
        for _chunk in range(4):                      # up to ~4 months back per contract
            bars = ib.reqHistoricalData(
                fut, endDateTime=end, durationStr="1 M", barSizeSetting=BAR[bar],
                whatToShow="TRADES", useRTH=False, formatDate=2, timeout=120)
            df = _to_df(bars)
            if df.empty:
                break
            got.append(df)
            earliest = df.index[0]
            end = earliest.to_pydatetime()
            time.sleep(2)
            if earliest <= win_start:
                break
        if got:
            cdf = pd.concat(got)
            cdf = cdf[(cdf.index > win_start) & (cdf.index <= expiry + pd.Timedelta(days=1))]
            frames.append(cdf)
            print(f"  {symbol} {fut.localSymbol}: {len(cdf)} bars in window {win_start.date()}..{expiry.date()}")
    if not frames:
        return pd.DataFrame()
    full = pd.concat(frames)
    full = full[~full.index.duplicated()].sort_index()
    full.index = full.index.tz_convert(ET)
    full.index.name = "timestamp"
    return full


def load_cached(symbol: str, bar: str) -> pd.DataFrame:
    df = pd.read_csv(DATA_DIR / f"{symbol}_{bar}.csv", index_col="timestamp", parse_dates=["timestamp"])
    df.index = (df.index.tz_localize("UTC") if df.index.tz is None else df.index).tz_convert(ET)
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=["ES", "NQ", "YM"])
    ap.add_argument("--interval", default="5m", choices=["5m", "15m"])
    ap.add_argument("--quarters", type=int, default=5, help="number of front-month windows (~quarters)")
    args = ap.parse_args()

    ib = IB()
    ib.connect("127.0.0.1", 4002, clientId=random.randint(100, 9999), timeout=15)
    print("connected:", ib.isConnected())
    try:
        for sym in args.symbols:
            print(f"fetching {sym} {args.interval} ({args.quarters} quarters)...")
            df = fetch_symbol(ib, sym, SPECS[sym], args.interval, args.quarters)
            if df.empty:
                print(f"  {sym}: NO DATA"); continue
            path = DATA_DIR / f"{sym}_{args.interval}.csv"
            df.to_csv(path)
            print(f"  {sym}: {len(df)} bars {df.index[0].date()}->{df.index[-1].date()} -> {path.name}")
    finally:
        ib.disconnect()


if __name__ == "__main__":
    main()
