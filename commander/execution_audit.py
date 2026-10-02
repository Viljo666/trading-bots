"""Read-only IBKR execution collector and report.

The collector never places, changes, or cancels an order.  It only asks the
paper Gateway for executions, stores those belonging to our tagged strategies,
and makes execution quality visible in the weekly review.  It is useful only
after a human chooses a live-paper trial; dry strategies simply produce no
records.
"""
from __future__ import annotations

import argparse
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ib_insync import IB


PROJECT = Path(__file__).resolve().parent.parent
LOG_FILE = Path(__file__).resolve().parent / "execution_audit.jsonl"
STATE_FILE = Path(__file__).resolve().parent / "execution_audit_state.json"
KNOWN_REFS = {"poc_futures", "sweep_futures", "tsmom"}


def _iso(value: Any) -> str:
    if isinstance(value, datetime):
        value = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
        return value.isoformat()
    return str(value)


def _commission(fill) -> float | None:
    value = getattr(getattr(fill, "commissionReport", None), "commission", None)
    try:
        value = float(value)
        return value if math.isfinite(value) and abs(value) < 1_000_000 else None
    except (TypeError, ValueError):
        return None


def normalize_fill(fill) -> dict[str, Any] | None:
    execution = fill.execution
    ref = str(getattr(execution, "orderRef", "") or "")
    if ref not in KNOWN_REFS:
        return None
    exec_id = str(getattr(execution, "execId", "") or "")
    if not exec_id:
        return None
    contract = fill.contract
    return {
        "kind": "execution",
        "strategy": ref,
        "exec_id": exec_id,
        "order_id": int(getattr(execution, "orderId", 0) or 0),
        "symbol": str(getattr(contract, "symbol", "")),
        "expiry": str(getattr(contract, "lastTradeDateOrContractMonth", "")),
        "side": str(getattr(execution, "side", "")),
        "shares": float(getattr(execution, "shares", 0.0) or 0.0),
        "price": float(getattr(execution, "price", 0.0) or 0.0),
        "commission": _commission(fill),
        "execution_time": _iso(getattr(fill, "time", "")),
    }


def _load_seen() -> set[str]:
    try:
        payload = json.loads(STATE_FILE.read_text())
        return {str(value) for value in payload.get("seen_exec_ids", [])}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return set()


def _save_seen(seen: set[str]) -> None:
    # IB execution IDs are durable; retain a bounded history for restarts.
    ids = sorted(seen)[-5_000:]
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"seen_exec_ids": ids}, indent=2) + "\n")
    tmp.replace(STATE_FILE)


def _append(event: dict[str, Any]) -> None:
    event["recorded_at"] = datetime.now(timezone.utc).isoformat()
    with LOG_FILE.open("a") as handle:
        handle.write(json.dumps(event, default=str) + "\n")


def collect_from_fills(fills: list[Any]) -> list[dict[str, Any]]:
    """Persist new known-strategy fills from an IBKR fill response."""
    seen = _load_seen()
    added: list[dict[str, Any]] = []
    for fill in fills:
        event = normalize_fill(fill)
        if event is None or event["exec_id"] in seen:
            continue
        _append(event)
        seen.add(event["exec_id"])
        added.append(event)
    _save_seen(seen)
    return added


def collect() -> list[dict[str, Any]]:
    """Connect read-only to the paper Gateway and collect executions."""
    ib = IB()
    try:
        ib.connect("127.0.0.1", 4002, clientId=random.randint(40000, 60000),
                   readonly=True, timeout=15)
        return collect_from_fills(ib.reqExecutions())
    finally:
        if ib.isConnected():
            ib.disconnect()


def _events_since(days: int = 30) -> list[dict[str, Any]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows: list[dict[str, Any]] = []
    try:
        for line in LOG_FILE.read_text().splitlines():
            row = json.loads(line)
            stamp = datetime.fromisoformat(str(row.get("execution_time", "")))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            if stamp >= cutoff:
                rows.append(row)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    return rows


def build(days: int = 30) -> str:
    rows = _events_since(days)
    if not rows:
        return (f"🧾 *Execution audit* — no tagged live-paper fills in the last {days}d\n"
                "_POC/Sweep are dry; collection is ready for any human-approved paper trial._")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["strategy"]), []).append(row)
    lines = [f"🧾 *Execution audit* — last {days}d"]
    for strategy, fills in sorted(grouped.items()):
        fees = [float(row["commission"]) for row in fills if row.get("commission") is not None]
        symbols = ", ".join(sorted({str(row["symbol"]) for row in fills}))
        fee_text = f" · commissions ${sum(fees):.2f}" if fees else " · commission pending"
        lines.append(f"*{strategy}*: {len(fills)} fills ({symbols}){fee_text}")
    lines.append("_Read-only reconciliation; it cannot submit or modify orders._")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collect", action="store_true", help="read executions from IBKR paper")
    parser.add_argument("--print", action="store_true", help="print the current audit summary")
    args = parser.parse_args()
    if args.collect:
        added = collect()
        print(f"Collected {len(added)} new tagged paper fills.")
    if args.print or not args.collect:
        print(build())


if __name__ == "__main__":
    main()
