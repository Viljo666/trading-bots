"""Live paper runner for the POC-continuation bot.

Daily cycle per symbol (all times ET):
  09:35+  compute prior-day POC, ATR, today's open -> arm a resting stop-entry
          bracket at the POC (or Telegram-only in --dry). Once per session.
  15:00   cancel the entry if it never filled (backtest's entry cutoff).
  15:55   force-flatten any open position + cancel children (flat by close).

The bracket rests at the exchange, so exits fire even if the bot dies. State
(armed/cutoff/flattened per date) is persisted to state.json so a restart
mid-session doesn't double-arm. Watchdog exits non-zero so launchd restarts us.

Run:  ../.venv/bin/python live.py --dry    (signals only, no orders)
      ../.venv/bin/python live.py          (real paper brackets)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
from datetime import datetime, time as dtime, timezone
from typing import Optional

import pandas as pd
from ib_insync import IB, util

import notify
from broker import (cancel_open_orders, connect_paper, fetch_5m_bars, flatten,
                    place_entry_bracket, position_size, resolve_active_contract)
from config import ET, LOG_FILE, PARAMS, SESSION, STATE_FILE, SYMBOLS, SymbolSpec
from strategy_live import build_plan


def log_event(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    line = json.dumps(event, default=str)
    print(line, flush=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, default=str))


def _t(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


class SymbolBot:
    def __init__(self, ib: IB, spec: SymbolSpec, contract, dry: bool, state: dict) -> None:
        self.ib = ib
        self.spec = spec
        self.contract = contract
        self.dry = dry
        self.state = state          # shared dict, persisted

    def _key(self, day, phase: str) -> str:
        return f"{self.spec.ibkr_symbol}:{day}:{phase}"

    def _done(self, day, phase: str) -> bool:
        return self.state.get(self._key(day, phase), False)

    def _mark(self, day, phase: str) -> None:
        self.state[self._key(day, phase)] = True
        save_state(self.state)

    async def poll(self) -> None:
        now = pd.Timestamp.now(tz=ET)
        if now.weekday() >= 5:
            return
        t = now.time()
        day = str(now.date())
        sym = self.spec.ibkr_symbol

        # --- 15:55 force-flatten ---
        if t >= _t(PARAMS.flatten_et) and not self._done(day, "flat"):
            if not self.dry and (position_size(self.ib, self.contract) != 0
                                 or any(tr.contract.conId == self.contract.conId
                                        for tr in self.ib.openTrades())):
                flatten(self.ib, self.contract)
                notify.flattened(sym)
                log_event({"kind": "flatten", "symbol": sym})
            self._mark(day, "flat")
            return

        # --- 15:00 cutoff: cancel unfilled entry ---
        if t >= _t(PARAMS.entry_cutoff_et) and self._done(day, "armed") \
                and not self._done(day, "cutoff"):
            if not self.dry and position_size(self.ib, self.contract) == 0:
                n = cancel_open_orders(self.ib, self.contract)
                if n:
                    notify.cutoff_cancel(sym)
                    log_event({"kind": "cutoff_cancel", "symbol": sym, "orders": n})
            self._mark(day, "cutoff")
            return

        # --- arm once per session ---
        if t < _t(PARAMS.arm_after_et) or t >= _t(PARAMS.entry_cutoff_et) \
                or self._done(day, "armed"):
            return

        bars = await fetch_5m_bars(self.ib, self.contract)
        if bars.empty:
            return
        bars.index = bars.index.tz_convert(ET)
        today = now.date()
        plan = build_plan(bars, self.spec, today)
        if plan is None:
            has_today = (bars.between_time("09:30", "16:00").index.date == today).any()
            if has_today:   # permanent skip (POC==open or thin history)
                notify.no_trade(sym, "no valid setup (POC==open or insufficient history)")
                log_event({"kind": "no_trade", "symbol": sym})
                self._mark(day, "armed")
            return          # transient: today's open bar not in yet — retry

    # safety: never double-arm if a restart happens mid-session
        if not self.dry and (position_size(self.ib, self.contract) != 0
                             or any(tr.contract.conId == self.contract.conId
                                    for tr in self.ib.openTrades())):
            log_event({"kind": "skip_arm_existing_orders", "symbol": sym})
            self._mark(day, "armed")
            return

        log_event({"kind": "armed", "symbol": sym, "side": plan.side, "poc": plan.poc,
                   "stop": plan.stop, "target": plan.target, "qty": plan.qty,
                   "open": plan.day_open, "dry": self.dry})
        if not self.dry:
            try:
                place_entry_bracket(self.ib, self.contract, plan.side, plan.qty,
                                    plan.poc, plan.stop, plan.target, self.spec.tick_size)
            except Exception as e:
                notify.error(sym, f"bracket failed: {e}")
                log_event({"kind": "bracket_error", "symbol": sym, "err": str(e)})
                return
        notify.armed(sym, plan, self.dry)
        self._mark(day, "armed")


async def run(dry: bool) -> None:
    ib = connect_paper()
    log_event({"kind": "connected", "port": SESSION.paper_port, "dry": dry})
    state = load_state()

    bots: list[SymbolBot] = []
    for spec in SYMBOLS:
        try:
            contract = resolve_active_contract(ib, spec.ibkr_symbol, spec.exchange)
            bots.append(SymbolBot(ib, spec, contract, dry, state))
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
    if stalled:
        raise SystemExit(1)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="alerts only, no orders")
    args = ap.parse_args()
    util.patchAsyncio()
    asyncio.run(run(args.dry))


if __name__ == "__main__":
    main()
