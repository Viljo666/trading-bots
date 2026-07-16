"""Streaming-bar diagnostic.

Run this after IB Gateway is logged in to see which futures actually receive
live bar updates on your paper account. Subscribes to ES, MES, NQ, MNQ, CL, GC
in parallel for 3 minutes and counts update events per symbol.

If a symbol shows 0 updates, your paper account is missing the streaming data
permission for it — switch to the micro variant or use delayed data.

Usage:
  ../.venv/bin/python diagnose_streaming.py
"""
from __future__ import annotations

import time
from collections import defaultdict
from datetime import datetime, timezone

from ib_insync import Future, util

from broker import connect_paper, resolve_front_month

TEST_SYMBOLS = [
    ("ES", "CME"),
    ("MES", "CME"),
    ("NQ", "CME"),
    ("MNQ", "CME"),
    ("CL", "NYMEX"),
    ("MCL", "NYMEX"),
    ("GC", "COMEX"),
    ("MGC", "COMEX"),
]

OBSERVE_SECONDS = 180


def main() -> None:
    print("Connecting to IB Gateway...")
    ib = connect_paper()
    print(f"Connected. accounts={ib.managedAccounts()}\n")

    counts: dict[str, int] = defaultdict(int)
    last_seen: dict[str, datetime] = {}
    handles = []

    for symbol, exchange in TEST_SYMBOLS:
        try:
            contract = resolve_front_month(ib, symbol, exchange)
            ib.qualifyContracts(contract)
        except Exception as e:
            print(f"  {symbol:5s}  FAILED to resolve: {e}")
            continue

        bars = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr="1 D",
            barSizeSetting="15 mins",
            whatToShow="TRADES",
            useRTH=False,
            formatDate=2,
            keepUpToDate=True,
        )

        def make_handler(sym: str):
            def _h(_b, _has_new):
                counts[sym] += 1
                last_seen[sym] = datetime.now(timezone.utc)
            return _h

        bars.updateEvent += make_handler(symbol)
        handles.append((symbol, bars))
        print(f"  {symbol:5s}  subscribed (initial bars={len(bars)})")

    print(f"\nObserving for {OBSERVE_SECONDS}s — leave Gateway running...\n")
    deadline = time.time() + OBSERVE_SECONDS
    while time.time() < deadline:
        ib.sleep(5)
        line = "  ".join(f"{s}={counts[s]}" for s, _ in handles)
        print(f"  [{int(deadline - time.time()):>3}s left]  {line}")

    print("\n=== Final results (updates received) ===")
    print(f"{'symbol':<6} {'updates':<8} {'last seen':<28}")
    for symbol, _ in TEST_SYMBOLS:
        c = counts.get(symbol, 0)
        ls = last_seen.get(symbol)
        print(f"{symbol:<6} {c:<8} {str(ls) if ls else '(never)'}")

    print("\nInterpretation:")
    print("  >0 updates = streaming works, use this symbol")
    print("  0 updates  = paper account lacks live permission; try the micro variant or skip")

    ib.disconnect()


if __name__ == "__main__":
    main()
