"""Futures 15m bar loader via yfinance (free, last ~60 days)."""
from __future__ import annotations

import pandas as pd
import yfinance as yf

# dollars per full-point move for reporting P&L
DOLLARS_PER_POINT = {
    "ES=F": 50.0,    # E-mini S&P 500
    "NQ=F": 20.0,    # E-mini Nasdaq-100
    "YM=F": 5.0,     # E-mini Dow
    "RTY=F": 50.0,   # E-mini Russell 2000
    "CL=F": 1000.0,  # WTI crude oil (1,000 barrels)
    "GC=F": 100.0,   # Gold (100 oz)
    "ZN=F": 1000.0,  # 10Y Treasury Note
    "6E=F": 125000.0,  # Euro FX
}


def load_15m(yf_symbol: str = "ES=F", period: str = "60d") -> pd.DataFrame:
    """Download a yfinance futures symbol at 15m resolution."""
    df = yf.download(
        tickers=yf_symbol,
        period=period,
        interval="15m",
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    if df.empty:
        raise RuntimeError(f"yfinance returned no data for {yf_symbol}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
    df = df.dropna()
    df.index.name = "timestamp"
    return df


# back-compat alias
def load_es_15m(period: str = "60d") -> pd.DataFrame:
    return load_15m("ES=F", period)


if __name__ == "__main__":
    for sym in ["ES=F", "CL=F", "GC=F"]:
        try:
            bars = load_15m(sym)
            print(f"{sym}: {len(bars)} bars, {bars.index[0]} -> {bars.index[-1]}")
        except Exception as e:
            print(f"{sym}: FAILED ({e})")
