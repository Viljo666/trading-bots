"""Live paper runner for the 4h sweep bot — polling architecture.

Every poll, for each symbol:
  1. async-fetch fresh 4h bars from IBKR
  2. if a NEW bar has closed since last poll, run the entry detector on it
  3. if it fires AND we have no open position/orders -> place a bracket (OCO)

Exits are managed at the exchange by the bracket, so the bot's only job is to
detect setups and arm brackets. Fills are reported via Telegram.

On its first-ever poll, the bot primes silently so historical signals are never
traded. Thereafter it remembers the last closed candle across restarts and may
process exactly one candle missed during a brief outage.

Prereqs: IB Gateway (paper, port 4002) running, logged in, Read-Only API OFF.
Run:  ../.venv/bin/python live.py        (live paper)
      ../.venv/bin/python live.py --dry  (no orders; log signals only)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from datetime import datetime, timezone
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
from shadow_results import active_shadows, close_event, observe_bar
from broker import (
    connect_paper,
    fetch_4h_bars,
    has_open_position_or_orders,
    place_bracket,
    resolve_active_contract,
)
from config import LOG_FILE, PARAMS, SESSION, STATE_FILE, SYMBOLS, SymbolSpec
from strategy_live import detect_entry_on_last_bar


BAR_INTERVAL = pd.Timedelta(hours=4)


def _utc_timestamp(value) -> Optional[pd.Timestamp]:
    if not value:
        return None
    try:
        ts = pd.Timestamp(value)
        if pd.isna(ts):
            return None
        return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    except (TypeError, ValueError):
        return None


def load_state() -> dict:
    try:
        with STATE_FILE.open() as f:
            state = json.load(f)
        if not isinstance(state, dict):
            raise ValueError("state must be an object")
        state.setdefault("version", 1)
        state.setdefault("last_closed_bars", {})
        if not isinstance(state["last_closed_bars"], dict):
            state["last_closed_bars"] = {}
        return state
    except FileNotFoundError:
        return {"version": 1, "last_closed_bars": {}}
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
        log_event({"kind": "state_load_error", "err": str(e)})
        return {"version": 1, "last_closed_bars": {}}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    try:
        with tmp.open("w") as f:
            json.dump(state, f, sort_keys=True)
        tmp.replace(STATE_FILE)
    except OSError as e:
        log_event({"kind": "state_save_error", "err": str(e)})


def startup_bar_action(last_bar: Optional[pd.Timestamp], latest_bar: pd.Timestamp) -> str:
    """Decide whether exactly one missed 4h close can safely be processed."""
    if last_bar is None:
        return "prime"
    if latest_bar <= last_bar:
        return "up_to_date"
    # Never act on several stale candles after a long outage; one intervening
    # close is safe and prevents a restart from discarding a fresh setup.
    return "process" if latest_bar - last_bar <= BAR_INTERVAL else "reprime"


def log_event(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    line = json.dumps(event, default=str)
    print(line, flush=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


class SymbolBot:
    def __init__(self, ib: IB, spec: SymbolSpec, contract, dry: bool, state: dict) -> None:
        self.ib = ib
        self.spec = spec
        self.contract = contract
        self.dry = dry
        self._state = state
        saved = state["last_closed_bars"].get(spec.ibkr_symbol)
        self.last_bar_ts = _utc_timestamp(saved)
        self.primed = self.last_bar_ts is not None

    def _record_last_bar(self, bar_ts: pd.Timestamp) -> None:
        self.last_bar_ts = bar_ts
        self.primed = True
        self._state["last_closed_bars"][self.spec.ibkr_symbol] = bar_ts.isoformat()
        save_state(self._state)

    def _shadow(self) -> dict | None:
        return active_shadows(self._state).get(self.spec.ibkr_symbol)

    def _start_shadow(self, sig) -> None:
        active_shadows(self._state)[self.spec.ibkr_symbol] = {
            "strategy": "sweep",
            "symbol": self.spec.ibkr_symbol,
            "side": sig.side,
            "entry": float(sig.entry),
            "stop": float(sig.stop),
            "target": float(sig.target),
            "dollars_per_point": float(self.spec.dollars_per_point),
            "opened_at": sig.confirm_ts.isoformat(),
            "macro_context": context_at(sig.confirm_ts.to_pydatetime()),
            "entry_bar": sig.confirm_ts.isoformat(),
            "status": "open",
            "bars_held": 0,
        }
        save_state(self._state)
        log_event({"kind": "shadow_armed", "mode": "shadow", "strategy": "sweep",
                   "symbol": self.spec.ibkr_symbol, "side": sig.side,
                   "entry": sig.entry, "stop": sig.stop, "target": sig.target,
                   "macro_context": active_shadows(self._state)[self.spec.ibkr_symbol]["macro_context"]})
        log_event({"kind": "shadow_filled", "mode": "shadow", "strategy": "sweep",
                   "symbol": self.spec.ibkr_symbol, "side": sig.side,
                   "entry": sig.entry, "stop": sig.stop, "target": sig.target})

    def _advance_shadow(self, df: pd.DataFrame) -> None:
        """Resolve the current dry sweep from later completed 4h candles."""
        if not self.dry:
            return
        record = self._shadow()
        if record is None:
            return
        entry_bar = _utc_timestamp(record.get("entry_bar"))
        last_seen = _utc_timestamp(record.get("last_bar"))
        if entry_bar is None:
            return
        for bar_ts, bar in df.iterrows():
            if bar_ts <= entry_bar or (last_seen is not None and bar_ts <= last_seen):
                continue
            record["last_bar"] = bar_ts.isoformat()
            observe_bar(record, float(bar.high), float(bar.low))
            stop_hit = (bar.low <= record["stop"] if record["side"] == "long"
                        else bar.high >= record["stop"])
            target_hit = (bar.high >= record["target"] if record["side"] == "long"
                          else bar.low <= record["target"])
            if stop_hit:  # conservative if a single candle reaches both levels
                active_shadows(self._state).pop(self.spec.ibkr_symbol, None)
                save_state(self._state)
                log_event(close_event(record, record["stop"], "stop", bar_ts.isoformat()))
                return
            if target_hit:
                active_shadows(self._state).pop(self.spec.ibkr_symbol, None)
                save_state(self._state)
                log_event(close_event(record, record["target"], "target", bar_ts.isoformat()))
                return
            if record["bars_held"] >= 60:  # same max hold as sweep backtest
                active_shadows(self._state).pop(self.spec.ibkr_symbol, None)
                save_state(self._state)
                log_event(close_event(record, float(bar.close), "time", bar_ts.isoformat()))
                return
        save_state(self._state)

    async def poll(self) -> None:
        df = await fetch_4h_bars(self.ib, self.contract)
        # IBKR includes the currently-FORMING bar as the last row. The strategy
        # confirms on candle close, so evaluating that half-empty bar misses
        # every signal (this cost us all 4 signals of the first dry month).
        # Keep only candles whose 4h window has fully elapsed.
        now = pd.Timestamp.now(tz="UTC")
        df = df[df.index + pd.Timedelta(hours=4) <= now]
        if len(df) < max(PARAMS.trend_ema, PARAMS.swing_lookback) + 2:
            return
        latest_ts = df.index[-1]

        # This only records a dry-run price outcome; it cannot affect orders,
        # risk checks, or the real-paper path.
        self._advance_shadow(df)

        action = startup_bar_action(self.last_bar_ts, latest_ts)
        if action == "prime":
            self._record_last_bar(latest_ts)
            log_event({"kind": "primed", "symbol": self.spec.ibkr_symbol,
                       "bars": len(df), "last_bar": latest_ts})
            return
        if action == "up_to_date":
            return  # no new closed bar yet
        if action == "reprime":
            previous = self.last_bar_ts
            self._record_last_bar(latest_ts)
            log_event({"kind": "reprimed_gap", "symbol": self.spec.ibkr_symbol,
                       "previous_bar": previous, "last_bar": latest_ts})
            return

        # Persist before placing a bracket. If the process dies during an IBKR
        # request, it may miss this signal but it cannot submit a duplicate order.
        self._record_last_bar(latest_ts)

        sig = detect_entry_on_last_bar(df, self.spec.dollars_per_point, PARAMS)
        if sig is None:
            return

        risk_check = check_new_futures_trade(
            self.ib, self.contract, sig.side, sig.qty, sig.entry, sig.stop,
        )
        if not risk_check.allowed:
            log_event({"kind": "risk_blocked", "symbol": self.spec.ibkr_symbol,
                       "side": sig.side, **risk_check.event_fields()})
            notify.risk_blocked(self.spec.ibkr_symbol, risk_check.reason)
            return

        if has_open_position_or_orders(self.ib, self.contract):
            log_event({"kind": "signal_skipped_open_position",
                       "symbol": self.spec.ibkr_symbol, "side": sig.side})
            return

        log_event({"kind": "signal", "symbol": self.spec.ibkr_symbol, "side": sig.side,
                   "entry": sig.entry, "stop": sig.stop, "target": sig.target,
                   "qty": sig.qty, "dry": self.dry,
                   "shared_stop_risk": round(risk_check.proposed_stop_risk, 2),
                   "shared_group": risk_check.group})

        if self.dry:
            if self._shadow() is not None:
                log_event({"kind": "shadow_skipped_active", "mode": "shadow",
                           "strategy": "sweep", "symbol": self.spec.ibkr_symbol,
                           "side": sig.side})
                return
            self._start_shadow(sig)
            notify.bracket_placed(self.spec.ibkr_symbol + " (DRY)", sig)
            return
        try:
            trades = place_bracket(self.ib, self.contract, sig.side, sig.qty,
                                   sig.stop, sig.target, self.spec.tick_size)
            statuses = [str(t.orderStatus.status or "PendingSubmit") for t in trades]
            failed = {"Inactive", "Cancelled", "ApiCancelled"}
            if len(trades) != 3 or any(status in failed for status in statuses):
                for trade in trades:
                    self.ib.cancelOrder(trade.order)
                log_event({"kind": "bracket_rejected", "symbol": self.spec.ibkr_symbol,
                           "order_ids": [t.order.orderId for t in trades], "statuses": statuses})
                notify.error(self.spec.ibkr_symbol, "bracket rejected; all related orders cancelled")
                return
            notify.bracket_placed(self.spec.ibkr_symbol, sig)
            log_event({"kind": "bracket_placed", "symbol": self.spec.ibkr_symbol,
                       "order_ids": [t.order.orderId for t in trades], "statuses": statuses,
                       "entry": sig.entry, "stop": sig.stop, "target": sig.target})
        except Exception as e:
            notify.error(self.spec.ibkr_symbol, f"bracket failed: {e}")
            log_event({"kind": "bracket_error", "symbol": self.spec.ibkr_symbol, "err": str(e)})


async def _wait_for_stop(stop: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=SESSION.poll_interval_sec)
    except asyncio.TimeoutError:
        pass


async def _connect_until_available(stop: asyncio.Event) -> IB | None:
    """Hold one process during a Gateway outage instead of launchd restart loops."""
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
                # One message per outage. This machine may be unattended for
                # days, so a five-minute reconnect loop must not flood Telegram.
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
                # Do not request historical data through a dead connection.
                # The normal watchdog is for a connected-but-stuck service;
                # Gateway outages should simply wait and retry indefinitely.
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
