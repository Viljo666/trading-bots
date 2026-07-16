"""Minimal IBKR client for the co-pilot — connect, resolve MES, fetch 15m bars.

Read-only: the co-pilot never places orders. User executes in NinjaTrader.
"""
from __future__ import annotations

import random
from datetime import datetime

import pandas as pd
from ib_insync import IB, BarDataList, Future

from .config import EXCHANGE, IBKR_HOST, IBKR_PAPER_PORT, SYMBOL


def connect() -> IB:
    ib = IB()
    ib.connect(
        IBKR_HOST,
        IBKR_PAPER_PORT,
        clientId=random.randint(100, 9999),
        readonly=True,
        timeout=15,
    )
    if not ib.isConnected():
        raise RuntimeError("connect returned but isConnected() is False")
    try:
        ib.reqMarketDataType(3)   # delayed; paper account has no live CME sub
    except Exception:
        pass
    return ib


def resolve_front_month(ib: IB, symbol: str = SYMBOL, exchange: str = EXCHANGE) -> Future:
    generic = Future(symbol=symbol, exchange=exchange, currency="USD")
    details = ib.reqContractDetails(generic)
    if not details:
        raise RuntimeError(f"no contract details for {symbol}@{exchange}")
    today = datetime.utcnow().strftime("%Y%m%d")
    upcoming = sorted(
        [d.contract for d in details if d.contract.lastTradeDateOrContractMonth >= today],
        key=lambda c: c.lastTradeDateOrContractMonth,
    )
    if not upcoming:
        raise RuntimeError(f"no upcoming {symbol} expiries")
    return upcoming[0]


async def fetch_15m_bars_async(
    ib: IB, contract: Future, duration: str = "5 D"
) -> pd.DataFrame:
    bars = await ib.reqHistoricalDataAsync(
        contract,
        endDateTime="",
        durationStr=duration,
        barSizeSetting="15 mins",
        whatToShow="TRADES",
        useRTH=False,
        formatDate=2,
        keepUpToDate=False,
    )
    return _bars_to_df(bars)


def _bars_to_df(bars: BarDataList) -> pd.DataFrame:
    if not bars:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    rows = []
    for b in bars:
        ts = pd.Timestamp(b.date)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        rows.append({
            "timestamp": ts,
            "open": float(b.open),
            "high": float(b.high),
            "low": float(b.low),
            "close": float(b.close),
            "volume": float(b.volume or 0),
        })
    return pd.DataFrame(rows).set_index("timestamp")
