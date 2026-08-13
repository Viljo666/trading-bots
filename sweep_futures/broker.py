"""IBKR paper wrapper for the sweep bot: 4h bars + native bracket (OCO) orders.

Bracket = market entry + resting take-profit LIMIT + stop-loss STOP in one OCA
group. The exits live at the exchange, so they execute even if this bot or the
Gateway goes offline — the right design for a swing strategy on 4h bars.

Connection/retry logic mirrors the Tori bot. Paper only (port 4002); never the
live port by default.
"""
from __future__ import annotations

import random
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
from ib_insync import IB, Future, LimitOrder, MarketOrder, StopOrder

from config import SESSION

ALERT_FLAG = Path(__file__).resolve().parent / ".gateway_alert_sent"


def _cid() -> int:
    return random.randint(100, 9999)


def connect_paper(max_attempts: int = 60, retry_delay_sec: float = 30.0) -> IB:
    try:
        import notify
    except Exception:
        notify = None  # type: ignore
    last_err: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        ib = IB()
        try:
            ib.connect(SESSION.paper_host, SESSION.paper_port, clientId=_cid(),
                       readonly=False, timeout=15)
            if ib.isConnected():
                ALERT_FLAG.unlink(missing_ok=True)
                try:
                    ib.reqMarketDataType(3)  # delayed ok for paper
                except Exception:
                    pass
                return ib
        except Exception as e:
            last_err = e
            try:
                ib.disconnect()
            except Exception:
                pass
        if notify and attempt == 3 and not ALERT_FLAG.exists():
            notify.gateway_unreachable(attempt)
            ALERT_FLAG.touch()
        time.sleep(retry_delay_sec)
    raise RuntimeError(f"Could not connect to IB paper on port {SESSION.paper_port}: {last_err}")


def resolve_active_contract(ib: IB, symbol: str, exchange: str,
                            roll_buffer_days: int = 8) -> Future:
    """Front-month, rolling out `roll_buffer_days` before last trade date."""
    generic = Future(symbol=symbol, exchange=exchange, currency="USD")
    details = ib.reqContractDetails(generic)
    if not details:
        raise RuntimeError(f"No contract details for {symbol}@{exchange}")

    def key(c) -> str:
        e = c.lastTradeDateOrContractMonth
        return e if len(e) == 8 else e + "31"

    cutoff = (datetime.utcnow() + timedelta(days=roll_buffer_days)).strftime("%Y%m%d")
    contracts = sorted((d.contract for d in details), key=key)
    for c in contracts:
        if key(c) > cutoff:
            return c
    return contracts[-1]


async def fetch_4h_bars(ib: IB, contract: Future) -> pd.DataFrame:
    """One-shot async fetch of 4h bars as a clean OHLCV frame (UTC index)."""
    raw = await ib.reqHistoricalDataAsync(
        contract, endDateTime="", durationStr=SESSION.history_duration,
        barSizeSetting=SESSION.bar_size, whatToShow="TRADES", useRTH=False,
        formatDate=2, keepUpToDate=False,
    )
    if not raw:
        # Must carry a tz-aware DatetimeIndex: callers do `df.index + pd.Timedelta(...)`,
        # and a default RangeIndex makes numpy raise "Concatenation operation is not
        # implemented for NumPy arrays" — which crashed every poll whenever the IBKR
        # data farm hiccuped and returned no bars.
        return pd.DataFrame(
            {c: [] for c in ("open", "high", "low", "close", "volume")},
            index=pd.DatetimeIndex([], tz="UTC", name="timestamp"),
        )
    df = pd.DataFrame([{
        "timestamp": pd.Timestamp(b.date, tz="UTC") if pd.Timestamp(b.date).tzinfo is None
        else pd.Timestamp(b.date),
        "open": float(b.open), "high": float(b.high), "low": float(b.low),
        "close": float(b.close), "volume": float(b.volume or 0),
    } for b in raw]).set_index("timestamp")
    return df


def place_bracket(ib: IB, contract: Future, side: str, qty: int,
                  stop_price: float, target_price: float, tick: float):
    """Market entry + OCO (take-profit limit + stop) bracket. Returns the trades."""
    action = "BUY" if side == "long" else "SELL"
    exit_action = "SELL" if side == "long" else "BUY"
    oca = f"sweep_{contract.symbol}_{int(time.time())}"

    def r(px: float) -> float:  # round to tick
        return round(round(px / tick) * tick, 8)

    parent = MarketOrder(action, qty)
    parent.orderId = ib.client.getReqId()
    parent.transmit = False
    parent.outsideRth = True

    tp = LimitOrder(exit_action, qty, r(target_price))
    tp.orderId = ib.client.getReqId()
    tp.parentId = parent.orderId
    tp.ocaGroup = oca
    tp.ocaType = 1
    tp.transmit = False
    tp.outsideRth = True

    sl = StopOrder(exit_action, qty, r(stop_price))
    sl.orderId = ib.client.getReqId()
    sl.parentId = parent.orderId
    sl.ocaGroup = oca
    sl.ocaType = 1
    sl.transmit = True   # last child transmits the whole bracket
    sl.outsideRth = True

    trades = [ib.placeOrder(contract, o) for o in (parent, tp, sl)]
    return trades


def has_open_position_or_orders(ib: IB, contract: Future) -> bool:
    """True if we already hold this contract or have working orders on it."""
    for p in ib.positions():
        if p.contract.conId == contract.conId and p.position != 0:
            return True
    for t in ib.openTrades():
        if t.contract.conId == contract.conId:
            return True
    return False
