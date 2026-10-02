"""IBKR paper-trading wrapper for the Tori ES futures bot.

Responsibilities:
  - connect to a running IB Gateway / TWS instance (paper port)
  - resolve the front-month ES contract
  - fetch a rolling 60-day window of 15m bars with live updates
  - place and close 1-contract market orders on signal

Assumes IB Gateway paper mode on port 4002 (or TWS paper on 7497).
Never connects to live port (7496 / 4001) by default.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
from ib_insync import IB, Future, MarketOrder, BarDataList


PAPER_HOST = "127.0.0.1"
PAPER_PORT_GATEWAY = 4002
PAPER_PORT_TWS = 7497

# Persists across process restarts so we only Telegram-alert ONCE per outage,
# not every 30-min retry cycle. Created when an alert fires; deleted on successful
# connect so the next outage triggers a fresh alert.
GATEWAY_ALERT_FLAG = Path(__file__).resolve().parent / ".gateway_alert_sent"


def _random_client_id() -> int:
    # Random each run avoids "half-open" session collisions in Gateway.
    return random.randint(100, 9999)


def _alert_flag_set() -> bool:
    return GATEWAY_ALERT_FLAG.exists()


def _set_alert_flag() -> None:
    try:
        GATEWAY_ALERT_FLAG.touch()
    except Exception:
        pass


def _clear_alert_flag() -> None:
    try:
        GATEWAY_ALERT_FLAG.unlink(missing_ok=True)
    except Exception:
        pass


@dataclass
class LiveBarsHandle:
    ib: IB
    bars: BarDataList

    def to_dataframe(self) -> pd.DataFrame:
        # When IBKR briefly returns an empty bar list (transient farm hiccup), the
        # pd.DataFrame([]).set_index("timestamp") path crashes with
        # "None of ['timestamp'] are in the columns". Return an empty-but-shaped
        # frame instead so the caller can len()==0 it and skip the poll.
        if not self.bars:
            idx = pd.DatetimeIndex([], name="timestamp", tz="UTC")
            return pd.DataFrame(
                {"open": [], "high": [], "low": [], "close": [], "volume": []},
                index=idx,
            )
        df = pd.DataFrame(
            [
                {
                    "timestamp": pd.Timestamp(b.date).tz_localize("UTC")
                    if pd.Timestamp(b.date).tzinfo is None
                    else pd.Timestamp(b.date),
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": float(b.volume or 0),
                }
                for b in self.bars
            ]
        ).set_index("timestamp")
        return df


def connect_paper(
    port: int = PAPER_PORT_GATEWAY,
    client_id: Optional[int] = None,
    max_attempts: int = 60,
    retry_delay_sec: float = 30.0,
) -> IB:
    """Connect to a running IB Gateway / TWS paper session, retrying until it comes up.

    With max_attempts=60 and retry_delay=30s, will keep trying for up to 30 minutes.
    Sends a Telegram alert after a few failed attempts so the user knows Gateway is down,
    and re-alerts every 20 attempts (~10 min) so it doesn't go silent if user is away.
    """
    import time
    try:
        import notify   # local import: same package, but optional for tests
    except Exception:
        notify = None   # type: ignore

    last_err: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        cid = client_id if client_id is not None else _random_client_id()
        ib = IB()
        try:
            ib.connect(PAPER_HOST, port, clientId=cid, readonly=False, timeout=15)
            if ib.isConnected():
                # cleared alert flag means next outage will trigger a fresh alert
                _clear_alert_flag()
                # paper accounts often lack a live data subscription for index futures;
                # fall back to delayed data so bar updates still flow (15-20 min lag).
                try:
                    ib.reqMarketDataType(3)  # 3 = delayed
                except Exception:
                    pass
                return ib
        except Exception as e:
            last_err = e
            try:
                ib.disconnect()
            except Exception:
                pass

        # Send ONE Telegram alert per outage. The flag is persisted on disk so it
        # survives launchd restarting this process every 30 min — without that, the
        # user gets spammed for hours when Gateway is down and they're away.
        if notify and attempt == 3 and not _alert_flag_set():
            notify.gateway_unreachable(attempt)
            _set_alert_flag()

        time.sleep(retry_delay_sec)

    raise RuntimeError(
        f"Could not connect to IB on port {port} after {max_attempts} attempts. "
        f"Last error: {last_err}. Is Gateway running in paper mode with API enabled?"
    )


def resolve_front_month(ib: IB, symbol: str, exchange: str = "CME") -> Future:
    """Pick the front-month futures contract for a given symbol/exchange."""
    generic = Future(symbol=symbol, exchange=exchange, currency="USD")
    details = ib.reqContractDetails(generic)
    if not details:
        raise RuntimeError(f"No contract details returned for {symbol}@{exchange}")
    today = datetime.utcnow().strftime("%Y%m%d")
    upcoming = sorted(
        [d.contract for d in details if d.contract.lastTradeDateOrContractMonth >= today],
        key=lambda c: c.lastTradeDateOrContractMonth,
    )
    if not upcoming:
        raise RuntimeError(f"No upcoming {symbol} expiries found")
    return upcoming[0]


def resolve_es_contract(ib: IB) -> Future:
    """Back-compat alias."""
    return resolve_front_month(ib, "ES", "CME")


def list_contracts(ib: IB, symbol: str, exchange: str = "CME") -> list[Future]:
    """All non-expired contracts for a symbol, sorted by expiry ascending."""
    generic = Future(symbol=symbol, exchange=exchange, currency="USD")
    details = ib.reqContractDetails(generic)
    if not details:
        raise RuntimeError(f"No contract details returned for {symbol}@{exchange}")
    today = datetime.utcnow().strftime("%Y%m%d")

    def expiry_key(c: Future) -> str:
        e = c.lastTradeDateOrContractMonth
        return e if len(e) == 8 else e + "31"  # normalize YYYYMM -> month end

    return sorted(
        [d.contract for d in details if expiry_key(d.contract) >= today],
        key=expiry_key,
    )


def resolve_active_contract(ib: IB, symbol: str, exchange: str = "CME",
                            roll_buffer_days: int = 8) -> Future:
    """Front-month contract, but ROLL to the next one when the front is within
    `roll_buffer_days` of its last trade date.

    This keeps us out of the expiry/delivery window: cash-settled contracts (ES/NQ/
    RTY) roll cleanly; physically-deliverable ones (CL/ZN/GC) get out before notice.
    NOTE: this is a calendar-day buffer, not a true first-notice-day check — adequate
    for paper, but for real money a volume/open-interest or notice-day roll is safer.
    """
    from datetime import timedelta

    contracts = list_contracts(ib, symbol, exchange)
    if not contracts:
        raise RuntimeError(f"No upcoming {symbol} contracts")
    cutoff = (datetime.utcnow() + timedelta(days=roll_buffer_days)).strftime("%Y%m%d")
    for c in contracts:
        exp = c.lastTradeDateOrContractMonth
        exp = exp if len(exp) == 8 else exp + "31"
        if exp > cutoff:
            return c
    return contracts[-1]


def positions_for_symbol(ib: IB, symbol: str) -> list:
    """All open futures positions for a symbol (possibly across multiple expiries)."""
    return [
        p for p in ib.positions()
        if p.contract.symbol == symbol and p.contract.secType == "FUT" and p.position != 0
    ]


def subscribe_15m_bars(
    ib: IB,
    contract: Future,
    on_update: Callable[[LiveBarsHandle, bool], None],
    duration: str = "60 D",
) -> LiveBarsHandle:
    """Seed with 60 days of 15m history and keep it updated live.

    `on_update(handle, has_new_bar)` fires every time IBKR pushes a bar update.
    `has_new_bar` is True only when a previously-forming bar has just closed.
    """
    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=duration,
        barSizeSetting="15 mins",
        whatToShow="TRADES",
        useRTH=False,
        formatDate=2,   # UTC seconds
        keepUpToDate=True,
    )
    handle = LiveBarsHandle(ib=ib, bars=bars)

    def _handler(_bars: BarDataList, has_new_bar: bool) -> None:
        on_update(handle, has_new_bar)

    bars.updateEvent += _handler
    return handle


def fetch_15m_bars(ib: IB, contract: Future, duration: str = "60 D") -> LiveBarsHandle:
    """One-shot SYNC fetch of 15m bars — no live update subscription.

    Use only outside of an asyncio event loop (e.g. during startup). For polling
    from inside a running loop, use `fetch_15m_bars_async`.
    """
    bars = ib.reqHistoricalData(
        contract,
        endDateTime="",
        durationStr=duration,
        barSizeSetting="15 mins",
        whatToShow="TRADES",
        useRTH=False,
        formatDate=2,
        keepUpToDate=False,
    )
    return LiveBarsHandle(ib=ib, bars=bars)


async def fetch_15m_bars_async(ib: IB, contract: Future, duration: str = "60 D") -> LiveBarsHandle:
    """Async variant of fetch_15m_bars — call from inside the asyncio loop."""
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
    return LiveBarsHandle(ib=ib, bars=bars)


def place_market(ib: IB, contract: Future, side: str, quantity: int = 1,
                 outside_rth: bool = False, order_ref: str | None = None):
    """Place a market order. `side` must be 'BUY' or 'SELL'.

    `outside_rth=True` lets the order fill outside regular trading hours and uses
    GTC so it isn't auto-cancelled by the DAY-TIF order preset (Error 10349) when
    placed pre/post-RTH. Tori leaves this False (it only trades during RTH); the
    TSMOM daily rebalancer sets it True (runs pre-open across bonds/FX/metals).
    """
    if side not in ("BUY", "SELL"):
        raise ValueError(f"invalid side {side}")
    order = MarketOrder(side, quantity)
    if order_ref:
        order.orderRef = order_ref
    if outside_rth:
        order.outsideRth = True
        order.tif = "GTC"
    trade = ib.placeOrder(contract, order)
    return trade


def flatten_position(ib: IB, contract: Future):
    """Close any open position on `contract` at market. Idempotent if flat."""
    positions = [p for p in ib.positions() if p.contract.conId == contract.conId]
    if not positions:
        return None
    pos = positions[0]
    if pos.position == 0:
        return None
    side = "SELL" if pos.position > 0 else "BUY"
    return place_market(ib, contract, side, abs(int(pos.position)))


def current_position_size(ib: IB, contract: Future) -> int:
    for p in ib.positions():
        if p.contract.conId == contract.conId:
            return int(p.position)
    return 0
