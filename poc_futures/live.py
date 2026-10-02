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
import sys
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from ib_insync import IB, util

import notify
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    # Keep this bot's own broker.py ahead of the workspace's stock broker.py.
    sys.path.append(str(PROJECT_ROOT))
from futures_risk import check_new_futures_trade
from macro_calendar import context_at
from shadow_results import active_shadows, close_event, expired_event, observe_bar
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
        state = json.loads(STATE_FILE.read_text())
        return state if isinstance(state, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    # A restart must never leave a half-written state file and accidentally
    # re-arm the same session's order.
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, default=str))
    temporary.replace(STATE_FILE)


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

    def _shadow(self) -> dict | None:
        """The one dry-run outcome being followed for this symbol."""
        return active_shadows(self.state).get(self.spec.ibkr_symbol)

    def _start_shadow(self, plan, now: pd.Timestamp, day: str) -> None:
        """Start a price-only ledger after a dry setup is armed.

        A shadow starts from the next complete five-minute bar.  That avoids
        pretending that an order entered part-way through the bar in which the
        runner happened to wake up.
        """
        active_shadows(self.state)[self.spec.ibkr_symbol] = {
            "strategy": "poc",
            "symbol": self.spec.ibkr_symbol,
            "side": plan.side,
            "entry": float(plan.poc),
            "stop": float(plan.stop),
            "target": float(plan.target),
            "dollars_per_point": float(self.spec.dollars_per_point),
            "day": day,
            "armed_at": now.isoformat(),
            "macro_context": context_at(now.to_pydatetime()),
            "not_before": now.ceil("5min").isoformat(),
            "status": "pending",
        }
        save_state(self.state)
        log_event({"kind": "shadow_armed", "mode": "shadow", "strategy": "poc",
                   "symbol": self.spec.ibkr_symbol, "side": plan.side,
                   "entry": plan.poc, "stop": plan.stop, "target": plan.target,
                   "macro_context": active_shadows(self.state)[self.spec.ibkr_symbol]["macro_context"]})

    def _finish_shadow(self, record: dict, event: dict) -> None:
        active_shadows(self.state).pop(self.spec.ibkr_symbol, None)
        save_state(self.state)
        log_event(event)

    async def _advance_shadow(self, now: pd.Timestamp, day: str) -> None:
        """Resolve a dry POC outcome from completed, RTH five-minute bars only."""
        if not self.dry:
            return
        record = self._shadow()
        if record is None:
            return
        if record.get("day") != day:
            # Do not silently use a later session to resolve an interrupted day.
            self._finish_shadow(record, expired_event(record, "session_gap", now.isoformat()))
            return

        probe = now.floor("5min").isoformat()
        if record.get("last_probe") == probe:
            return
        record["last_probe"] = probe

        bars = await fetch_5m_bars(self.ib, self.contract)
        if bars.empty:
            save_state(self.state)
            return
        bars.index = bars.index.tz_convert(ET)
        completed = bars[(bars.index.date == now.date()) &
                         (bars.index + pd.Timedelta(minutes=5) <= now)]
        if completed.empty:
            save_state(self.state)
            return

        not_before = pd.Timestamp(record["not_before"])
        last_seen = pd.Timestamp(record["last_bar"]) if record.get("last_bar") else None
        for bar_ts, bar in completed.iterrows():
            if bar_ts < not_before or (last_seen is not None and bar_ts <= last_seen):
                continue
            record["last_bar"] = bar_ts.isoformat()
            if record["status"] == "pending":
                touched = (bar.high >= record["entry"] if record["side"] == "long"
                           else bar.low <= record["entry"])
                if touched:
                    record["status"] = "open"
                    record["opened_at"] = bar_ts.isoformat()
                    record["entry_bar"] = bar_ts.isoformat()
                    log_event({"kind": "shadow_filled", "mode": "shadow", "strategy": "poc",
                               "symbol": self.spec.ibkr_symbol, "side": record["side"],
                               "entry": record["entry"],
                               "entry_lag_bars": record.get("entry_lag_bars", 0)})
                else:
                    record["entry_lag_bars"] = int(record.get("entry_lag_bars", 0)) + 1
                continue  # never resolve an exit in the same ambiguous bar

            observe_bar(record, float(bar.high), float(bar.low))
            stop_hit = (bar.low <= record["stop"] if record["side"] == "long"
                        else bar.high >= record["stop"])
            target_hit = (bar.high >= record["target"] if record["side"] == "long"
                          else bar.low <= record["target"])
            if stop_hit:  # conservative when both happen inside one 5m bar
                self._finish_shadow(record, close_event(record, record["stop"], "stop", bar_ts.isoformat()))
                return
            if target_hit:
                self._finish_shadow(record, close_event(record, record["target"], "target", bar_ts.isoformat()))
                return

        # Process all completed price action before applying the strategy's
        # cutoff / flatten rules, so the final permitted bar still counts.
        if record["status"] == "pending" and now.time() >= _t(PARAMS.entry_cutoff_et):
            self._finish_shadow(record, expired_event(record, "entry_cutoff", now.isoformat()))
            return
        if record["status"] == "open" and now.time() >= _t(PARAMS.flatten_et):
            last_close = float(completed.iloc[-1]["close"])
            self._finish_shadow(record, close_event(record, last_close, "session_close", now.isoformat()))
            return
        save_state(self.state)

    async def poll(self) -> None:
        now = pd.Timestamp.now(tz=ET)
        if now.weekday() >= 5:
            return
        t = now.time()
        day = str(now.date())
        sym = self.spec.ibkr_symbol

        # Dry-only, price-bar shadow tracking. It is deliberately independent
        # of the real-paper bracket path below.
        await self._advance_shadow(now, day)

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

        risk_check = check_new_futures_trade(
            self.ib, self.contract, plan.side, plan.qty, plan.poc, plan.stop,
        )
        if not risk_check.allowed:
            log_event({"kind": "risk_blocked", "symbol": sym, "side": plan.side,
                       **risk_check.event_fields()})
            notify.risk_blocked(sym, risk_check.reason)
            self._mark(day, "armed")
            return

        log_event({"kind": "armed", "symbol": sym, "side": plan.side, "poc": plan.poc,
                   "stop": plan.stop, "target": plan.target, "qty": plan.qty,
                   "open": plan.day_open, "dry": self.dry,
                   "shared_stop_risk": round(risk_check.proposed_stop_risk, 2),
                   "shared_group": risk_check.group})
        if self.dry:
            self._start_shadow(plan, now, day)
        if not self.dry:
            try:
                trades = place_entry_bracket(
                    self.ib, self.contract, plan.side, plan.qty,
                    plan.poc, plan.stop, plan.target, self.spec.tick_size,
                )
                statuses = [str(t.orderStatus.status or "PendingSubmit") for t in trades]
                failed = {"Inactive", "Cancelled", "ApiCancelled"}
                if len(trades) != 3 or any(status in failed for status in statuses):
                    cancel_open_orders(self.ib, self.contract)
                    log_event({"kind": "bracket_rejected", "symbol": sym,
                               "order_ids": [t.order.orderId for t in trades], "statuses": statuses})
                    notify.error(sym, "bracket rejected; all related orders cancelled")
                    self._mark(day, "armed")
                    return
                log_event({"kind": "bracket_placed", "symbol": sym,
                           "order_ids": [t.order.orderId for t in trades], "statuses": statuses,
                           "entry": plan.poc, "stop": plan.stop, "target": plan.target})
            except Exception as e:
                notify.error(sym, f"bracket failed: {e}")
                log_event({"kind": "bracket_error", "symbol": sym, "err": str(e)})
                return
        notify.armed(sym, plan, self.dry)
        self._mark(day, "armed")


async def _wait_for_stop(stop: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=SESSION.poll_interval_sec)
    except asyncio.TimeoutError:
        pass


async def _connect_until_available(stop: asyncio.Event) -> IB | None:
    """Stay alive through a Gateway outage without launchd restart loops."""
    attempts = 0
    alerted = False
    while not stop.is_set():
        try:
            ib = connect_paper()
            if attempts:
                log_event({"kind": "gateway_recovered", "attempts": attempts})
            return ib
        except Exception as e:
            attempts += 1
            if not alerted:
                notify.gateway_unreachable(attempts)
                alerted = True
            log_event({"kind": "connect_failed", "attempt": attempts, "err": str(e)})
            await _wait_for_stop(stop)
    return None


async def run(dry: bool) -> None:
    stop = asyncio.Event()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(s, stop.set)
        except NotImplementedError:
            pass

    ib = await _connect_until_available(stop)
    if ib is None:
        return
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

    last_ok = datetime.now(timezone.utc)
    stalled = False
    gateway_alerted = False
    reconnect_attempts = 0
    while not stop.is_set():
        if not ib.isConnected():
            reconnect_attempts += 1
            if not gateway_alerted:
                # The machine is often unattended, so only alert once per
                # outage and keep trying quietly afterward.
                notify.gateway_unreachable(reconnect_attempts)
                gateway_alerted = True
            log_event({"kind": "gateway_disconnected", "attempt": reconnect_attempts})
            try:
                ib = connect_paper()
                for b in bots:
                    b.ib = ib
                log_event({"kind": "gateway_recovered", "attempts": reconnect_attempts})
                last_ok = datetime.now(timezone.utc)
                reconnect_attempts = 0
                gateway_alerted = False
            except Exception as e:
                log_event({"kind": "reconnect_failed", "err": str(e)})
                # Avoid historical-data requests through a disconnected API.
                # The watchdog remains reserved for a connected-but-stuck bot.
                await _wait_for_stop(stop)
                continue
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
        await _wait_for_stop(stop)

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
