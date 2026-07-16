"""4h futures bar loader.

yfinance only serves ~730 days of intraday history and not at a native 4h
interval, so we pull 1h bars and resample up to 4h on clean UTC buckets
(00/04/08/12/16/20 UTC). Good enough for a swing-style liquidity-sweep test.
"""
from __future__ import annotations

import pandas as pd
import yfinance as yf

# dollars per full 1-point move (one contract) — for $ P&L reporting
DOLLARS_PER_POINT = {
    "ES=F": 50.0,    # E-mini S&P 500
    "NQ=F": 20.0,    # E-mini Nasdaq-100
    "YM=F": 5.0,     # E-mini Dow
    "RTY=F": 50.0,   # E-mini Russell 2000
    "CL=F": 1000.0,  # WTI crude oil
    "GC=F": 100.0,   # Gold (100 oz)
}

# minimum price increment (one tick) per symbol — for slippage modelling
TICK_SIZE = {
    "ES=F": 0.25,
    "NQ=F": 0.25,
    "YM=F": 1.0,
    "RTY=F": 0.10,
    "CL=F": 0.01,
    "GC=F": 0.10,
}

# default universe = the liquid index + commodity futures
PAIRS = ["ES=F", "NQ=F", "YM=F", "RTY=F", "CL=F", "GC=F"]


def load_4h(yf_symbol: str, period: str = "730d") -> pd.DataFrame:
    """Download 1h bars and resample to clean 4h UTC candles."""
    df = yf.download(
        tickers=yf_symbol,
        period=period,
        interval="1h",
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    if df.empty:
        raise RuntimeError(f"yfinance returned no data for {yf_symbol}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    bars = df.resample("4h", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    ).dropna()
    bars.index.name = "timestamp"
    return bars


if __name__ == "__main__":
    for sym in PAIRS:
        try:
            b = load_4h(sym)
            print(f"{sym}: {len(b)} 4h bars, {b.index[0]} -> {b.index[-1]}")
        except Exception as e:
            print(f"{sym}: FAILED ({e})")
