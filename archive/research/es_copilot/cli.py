"""Text-mode REPL co-pilot.

Runs the IBKR poll loop on the asyncio main thread (required by ib_insync)
and a blocking REPL on a worker thread. They share a CopilotState; the only
cross-thread writes from the REPL are to `state.user_position`, which is a
single attribute assignment — safe enough for v1 without a lock.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading

from ib_insync import util

from .agent import Copilot
from .broker import connect, resolve_front_month
from .config import IBKR_PAPER_PORT, SYMBOL
from .events import StrategyEvent
from .orchestrator import Orchestrator
from .state import CopilotState, UserPosition

HELP = """
Commands:
  /snapshot               Show latest price, bar timestamp, strategy state
  /levels                 Show PDH / PDL / ONH / ONL
  /setups                 Show current monitor view (pending trendline, etc.)
  /events [N]             Show last N strategy events (default 10)
  /long  <entry> <stop> [target] [qty]    Log a long entry
  /short <entry> <stop> [target] [qty]    Log a short entry
  /flat                   Clear logged position
  /pos                    Show logged position
  /help                   This help
  /quit                   Exit

Anything not starting with / is sent to Claude.
""".strip()


def handle_command(line: str, state: CopilotState) -> None:
    parts = line.split()
    cmd = parts[0]
    if cmd == "/help":
        print(HELP)
    elif cmd == "/snapshot":
        print({
            "price": state.last_price,
            "bar_ts": state.last_bar_ts,
            "connected": state.connected,
            "state": state.monitor.state if state.monitor else "not_initialized",
        })
    elif cmd == "/levels":
        print(state.levels.to_dict() if state.levels else "no levels yet")
    elif cmd == "/setups":
        print(state.monitor.to_dict() if state.monitor else "no monitor yet")
    elif cmd == "/events":
        n = int(parts[1]) if len(parts) > 1 else 10
        if not state.recent_events:
            print("no events yet")
            return
        for ev in state.recent_events[-n:]:
            print(f"  {ev.fired_at.isoformat()}  {ev.headline()}")
    elif cmd in ("/long", "/short"):
        if len(parts) < 3:
            print("usage: /long <entry> <stop> [target] [qty]")
            return
        direction = "long" if cmd == "/long" else "short"
        try:
            entry = float(parts[1])
            stop = float(parts[2])
            target = float(parts[3]) if len(parts) > 3 else None
            qty = int(parts[4]) if len(parts) > 4 else 1
        except ValueError as e:
            print(f"bad number: {e}")
            return
        state.user_position = UserPosition(direction, qty, entry, stop, target)
        print(f"logged: {direction} {qty} @ {entry}, stop {stop}"
              + (f", target {target}" if target else ""))
    elif cmd == "/flat":
        state.user_position = None
        print("flat")
    elif cmd == "/pos":
        print(state.user_position.to_dict() if state.user_position else "no position")
    elif cmd in ("/quit", "/exit"):
        os._exit(0)
    else:
        print(f"unknown command: {cmd}  (try /help)")


def repl(state: CopilotState, copilot: Copilot, stop_event: threading.Event) -> None:
    print("\nES Co-pilot ready. Type a question, or /help for commands.\n")
    while not stop_event.is_set():
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            stop_event.set()
            os._exit(0)
        if not line:
            continue
        if line.startswith("/"):
            try:
                handle_command(line, state)
            except Exception as e:
                print(f"[error] {e}")
            continue
        try:
            reply = copilot.ask(line)
            print(f"\ncopilot> {reply}\n")
        except Exception as e:
            print(f"[claude error] {e}")


def make_event_printer():
    def on_event(event: StrategyEvent) -> None:
        print(f"\n[!] {event.headline()}\n", flush=True)
    return on_event


def main() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("error: set ANTHROPIC_API_KEY before launching the co-pilot.", file=sys.stderr)
        sys.exit(1)

    print(f"Connecting to IB Gateway on 127.0.0.1:{IBKR_PAPER_PORT} ...")
    ib = connect()
    print(f"Connected. Resolving {SYMBOL} front-month contract...")
    contract = resolve_front_month(ib)
    ib.qualifyContracts(contract)
    print(f"OK: {SYMBOL} expiry {contract.lastTradeDateOrContractMonth} conId={contract.conId}")

    state = CopilotState()
    orchestrator = Orchestrator(ib, contract, state)
    orchestrator.on_event(make_event_printer())
    copilot = Copilot(state)

    stop_event = threading.Event()
    repl_thread = threading.Thread(
        target=repl, args=(state, copilot, stop_event), daemon=True
    )
    repl_thread.start()

    asyncio.ensure_future(orchestrator.run())
    try:
        util.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
