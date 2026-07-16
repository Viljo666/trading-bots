"""Live paper runner for the 4h sweep bot — polling architecture.

Every poll, for each symbol:
  1. async-fetch fresh 4h bars from IBKR
  2. if a NEW bar has closed since last poll, run the entry detector on it
  3. if it fires AND we have no open position/orders -> place a bracket (OCO)

Exits are managed at the exchange by the bracket, so the bot's only job is to
detect setups and arm brackets. Fills are reported via Telegram.

First poll PRIMES silently: historical signals in the 90d window are not traded;
we only act on bars that close after startup.

Prereqs: IB Gateway (paper, port 4002) running, logged in, Read-Only API OFF.
Run:  ../.venv/bin/python live.py        (live paper)
      ../.venv/bin/python live.py --dry  (no orders; log signals only)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
from ib_insync import IB, util

import notify
from broker import (
    connect_paper,
    fetch_4h_bars,
    has_open_position_or_orders,
    place_bracket,
    resolve_active_contract,
)
from config import LOG_FILE, PARAMS, SESSION, SYMBOLS, SymbolSpec
from strategy_live import detect_entry_on_last_bar


def log_event(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    line = json.dumps(event, default=str)
    print(line, flush=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


class SymbolBot:
    def __init__(self, ib: IB, spec: SymbolSpec, contract, dry: bool) -> None:
        self.ib = ib
        self.spec = spec
        self.contract = contract
        self.dry = dry
        self.last_bar_ts: Optional[pd.Timestamp] = None
        self.primed = False

    async def poll(self) -> None:
        df = await fetch_4h_bars(self.ib, self.contract)
        if len(df) < max(PARAMS.trend_ema, PARAMS.swing_lookback) + 2:
            return
        latest_ts = df.index[-1]

        # prime on first poll: adopt current bar as baseline, trade nothing historical
        if not self.primed:
            self.last_bar_ts = latest_ts
            self.primed = True
            log_event({"kind": "primed", "symbol": self.spec.ibkr_symbol,
                       "bars": len(df), "last_bar": latest_ts})
            return

        if self.last_bar_ts is not None and latest_ts <= self.last_bar_ts:
            return  # no new closed bar yet
        self.last_bar_ts = latest_ts

        sig = detect_entry_on_last_bar(df, self.spec.dollars_per_point, PARAMS)
        if sig is None:
            return

        if has_open_position_or_orders(self.ib, self.contract):
            log_event({"kind": "signal_skipped_open_position",
                       "symbol": self.spec.ibkr_symbol, "side": sig.side})
            return

        log_event({"kind": "signal", "symbol": self.spec.ibkr_symbol, "side": sig.side,
                   "entry": sig.entry, "stop": sig.stop, "target": sig.target,
                   "qty": sig.qty, "dry": self.dry})

        if self.dry:
            notify.bracket_placed(self.spec.ibkr_symbol + " (DRY)", sig)
            return
        try:
            place_bracket(self.ib, self.contract, sig.side, sig.qty,
                          sig.stop, sig.target, self.spec.tick_size)
            notify.bracket_placed(self.spec.ibkr_symbol, sig)
            log_event({"kind": "bracket_placed", "symbol": self.spec.ibkr_symbol})
        except Exception as e:
            notify.error(self.spec.ibkr_symbol, f"bracket failed: {e}")
            log_event({"kind": "bracket_error", "symbol": self.spec.ibkr_symbol, "err": str(e)})


async def run(dry: bool) -> None:
    ib = connect_paper()
    log_event({"kind": "connected", "port": SESSION.paper_port, "dry": dry})

    bots: list[SymbolBot] = []
    for spec in SYMBOLS:
        try:
            contract = resolve_active_contract(ib, spec.ibkr_symbol, spec.exchange)
            bots.append(SymbolBot(ib, spec, contract, dry))
            log_event({"kind": "contract", "symbol": spec.ibkr_symbol,
                       "expiry": contract.lastTradeDateOrContractMonth})
        except Exception as e:
            notify.error(spec.ibkr_symbol, f"contract resolve failed: {e}")

    if not bots:
        raise RuntimeError("No contracts resolved; aborting.")
    notify.startup([b.spec.ibkr_symbol for b in bots])

    stop = asyncio.Event()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(s, stop.set)
        except NotImplementedError:
            pass

    last_ok = datetime.now(timezone.utc)
    stalled = False
    while not stop.is_set():
        if not ib.isConnected():
            notify.error(None, "IB disconnected; reconnecting")
            try:
                ib = connect_paper()
                for b in bots:
                    b.ib = ib
            except Exception as e:
                log_event({"kind": "reconnect_failed", "err": str(e)})
        try:
            for b in bots:
                await b.poll()
            last_ok = datetime.now(timezone.utc)
        except Exception as e:
            log_event({"kind": "poll_error", "err": str(e)})
            mins = (datetime.now(timezone.utc) - last_ok).total_seconds() / 60
            if mins >= SESSION.watchdog_stall_min:
                notify.stale_poll(int(mins))
                stalled = True
                break
        try:
            await asyncio.wait_for(stop.wait(), timeout=SESSION.poll_interval_sec)
        except asyncio.TimeoutError:
            pass

    notify.shutdown()
    log_event({"kind": "shutdown", "stalled": stalled})
    try:
        ib.disconnect()
    except Exception:
        pass
    # Exit non-zero on a watchdog stall so launchd (KeepAlive/SuccessfulExit=false)
    # RESTARTS us — e.g. after a Gateway outage. A clean SIGINT/SIGTERM exits 0.
    if stalled:
        raise SystemExit(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="detect + alert, place no orders")
    args = ap.parse_args()
    util.patchAsyncio()
    asyncio.run(run(args.dry))


if __name__ == "__main__":
    main()
