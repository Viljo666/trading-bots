"""IBKR paper wrapper for the POC bot: 5m bars + stop-entry bracket + flatten.

Entry is a STOP order at the POC (price must trade *to* the level from the open
side — a limit there would fill immediately), with OCA stop-loss + take-profit
children. The whole bracket rests at the exchange; the bot just supervises
(cutoff-cancel at 15:00 ET, force-flatten at 15:55 ET).
"""
from __future__ import annotations

import random
import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
from ib_insync import IB, Future, LimitOrder, MarketOrder, StopOrder

from config import SESSION


def connect_paper(max_attempts: int = 1, retry_delay_sec: float = 0.0) -> IB:
    """Make a bounded connection attempt; live.py owns retrying and alerts."""
    last_err: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        ib = IB()
        try:
            ib.connect(SESSION.paper_host, SESSION.paper_port,
                       clientId=random.randint(100, 9999), readonly=False, timeout=15)
            if ib.isConnected():
                try:
                    ib.reqMarketDataType(3)
                except Exception:
                    pass
                return ib
        except Exception as e:
            last_err = e
            try:
                ib.disconnect()
            except Exception:
                pass
        if attempt < max_attempts and retry_delay_sec > 0:
            time.sleep(retry_delay_sec)
    raise RuntimeError(f"Could not connect to IB paper on port {SESSION.paper_port}: {last_err}")


def resolve_active_contract(ib: IB, symbol: str, exchange: str,
                            roll_buffer_days: int = 8) -> Future:
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


async def fetch_5m_bars(ib: IB, contract: Future) -> pd.DataFrame:
    raw = await ib.reqHistoricalDataAsync(
        contract, endDateTime="", durationStr=SESSION.history_duration,
        barSizeSetting="5 mins", whatToShow="TRADES", useRTH=False,
        formatDate=2, keepUpToDate=False)
    if not raw:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame([{
        "timestamp": pd.Timestamp(b.date, tz="UTC") if pd.Timestamp(b.date).tzinfo is None
        else pd.Timestamp(b.date),
        "open": float(b.open), "high": float(b.high), "low": float(b.low),
        "close": float(b.close), "volume": float(b.volume or 0),
    } for b in raw]).set_index("timestamp")
    return df


def place_entry_bracket(ib: IB, contract: Future, side: str, qty: int,
                        entry_stop: float, stop_loss: float, target: float, tick: float):
    """Stop-entry at the POC + OCA (TP limit / SL stop) children. DAY tif."""
    action = "BUY" if side == "long" else "SELL"
    exit_action = "SELL" if side == "long" else "BUY"
    oca = f"poc_{contract.symbol}_{int(time.time())}"

    def r(px: float) -> float:
        return round(round(px / tick) * tick, 8)

    parent = StopOrder(action, qty, r(entry_stop))
    parent.orderId = ib.client.getReqId()
    parent.orderRef = "poc_futures"
    parent.transmit = False

    tp = LimitOrder(exit_action, qty, r(target))
    tp.orderId = ib.client.getReqId()
    tp.orderRef = "poc_futures"
    tp.parentId = parent.orderId
    tp.ocaGroup = oca; tp.ocaType = 1; tp.transmit = False

    sl = StopOrder(exit_action, qty, r(stop_loss))
    sl.orderId = ib.client.getReqId()
    sl.orderRef = "poc_futures"
    sl.parentId = parent.orderId
    sl.ocaGroup = oca; sl.ocaType = 1; sl.transmit = True

    return [ib.placeOrder(contract, o) for o in (parent, tp, sl)]


def cancel_open_orders(ib: IB, contract: Future) -> int:
    n = 0
    for t in ib.openTrades():
        if t.contract.conId == contract.conId:
            ib.cancelOrder(t.order); n += 1
    return n


def position_size(ib: IB, contract: Future) -> int:
    for p in ib.positions():
        if p.contract.conId == contract.conId:
            return int(p.position)
    return 0


def flatten(ib: IB, contract: Future):
    """Cancel working orders and close any open position at market."""
    cancel_open_orders(ib, contract)
    pos = position_size(ib, contract)
    if pos == 0:
        return None
    side = "SELL" if pos > 0 else "BUY"
    return ib.placeOrder(contract, MarketOrder(side, abs(pos)))
