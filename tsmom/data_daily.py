"""Daily multi-instrument futures data loader (yfinance).

Daily bars go back years (unlike 15m which yfinance caps at 60d), which is what
time-series momentum needs. Continuous front-month proxies via the =F symbols.
"""
from __future__ import annotations

import pandas as pd
import yfinance as yf

# symbol -> (contract point value / multiplier, asset class)
# Full validated set (walk-forward Sharpe 0.72). NOTE: ES/NQ/CL/GC overlap with the
# Tori bot on the shared IBKR paper account — running both requires account isolation
# (separate paper account) or pausing Tori. A disjoint set was tested and only scored
# Sharpe 0.29 (the edge lives in ES/NQ/GC/CL), so we keep the full set.
INSTRUMENTS = {
    # current live set
    "ES=F":  {"multiplier": 50.0,       "asset": "equity", "micro": ("MES", 5.0)},
    "NQ=F":  {"multiplier": 20.0,       "asset": "equity", "micro": ("MNQ", 2.0)},
    "RTY=F": {"multiplier": 50.0,       "asset": "equity", "micro": ("M2K", 5.0)},
    "ZN=F":  {"multiplier": 1000.0,     "asset": "bond",   "micro": None},
    "CL=F":  {"multiplier": 1000.0,     "asset": "energy", "micro": ("MCL", 100.0)},
    "GC=F":  {"multiplier": 100.0,      "asset": "metal",  "micro": ("MGC", 10.0)},
    "6E=F":  {"multiplier": 125000.0,   "asset": "fx",     "micro": ("M6E", 12500.0)},
    # expanded-universe candidates
    "YM=F":  {"multiplier": 5.0,        "asset": "equity", "micro": ("MYM", 0.5)},
    "ZB=F":  {"multiplier": 1000.0,     "asset": "bond",   "micro": None},
    "6J=F":  {"multiplier": 12500000.0, "asset": "fx",     "micro": None},
    "SI=F":  {"multiplier": 5000.0,     "asset": "metal",  "micro": ("SIL", 1000.0)},
    "HG=F":  {"multiplier": 25000.0,    "asset": "metal",  "micro": ("MHG", 2500.0)},
    "NG=F":  {"multiplier": 10000.0,    "asset": "energy", "micro": ("MNG", 1000.0)},
}


def load_daily(symbol: str, period: str = "10y") -> pd.DataFrame:
    """Daily OHLCV for one futures symbol. Index = tz-naive date."""
    df = yf.download(
        tickers=symbol,
        period=period,
        interval="1d",
        auto_adjust=False,
        progress=False,
        threads=False,
    )
    if df.empty:
        raise RuntimeError(f"yfinance returned no daily data for {symbol}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]].dropna()
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df.index.name = "date"
    return df


def load_all(period: str = "10y") -> dict[str, pd.DataFrame]:
    out = {}
    for sym in INSTRUMENTS:
        try:
            out[sym] = load_daily(sym, period)
        except Exception as e:
            print(f"  {sym}: FAILED ({e})")
    return out


if __name__ == "__main__":
    data = load_all()
    for sym, df in data.items():
        print(f"{sym:6s} {len(df):5d} bars  {df.index[0].date()} -> {df.index[-1].date()}  last close {df['close'].iloc[-1]:.2f}")
