"""TSMOM once-daily live paper rebalancer (IBKR).

Runs ONCE per invocation (not a daemon):
  1. Connect to IBKR paper Gateway (reuses tori_futures/broker.py)
  2. For each instrument: fetch ~2y daily bars, compute combined EWMAC forecast
     and vol-targeted position, round to whole contracts
  3. Compare to current IBKR position, place market orders to rebalance
  4. Log to daily_log.jsonl + Telegram summary, then disconnect and exit

Scheduled once daily by launchd (com.villerajala.tsmomdaily). Low-maintenance by
design: a missed day barely matters for a daily-bar strategy.

Capital for sizing = account NetLiquidation (paper ~$1M) so positions round to
meaningful integers. Sharpe is capital-independent so this stays comparable to
the backtest.

Usage:
  python live_daily.py --dry-run    # compute + log targets, place NO orders
  python live_daily.py              # live: place rebalance orders
"""
from __future__ import annotations

import argparse
import fcntl
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

# Keep imports deterministic when this file is invoked from launchd, a shell, or a
# test runner.  The workspace root also has a different broker.py, so the Tori
# futures wrapper must be ahead of it while this folder's notify.py stays first.
THIS_DIR = Path(__file__).resolve().parent
TORI_DIR = THIS_DIR.parent / "tori_futures"
for _path in (str(TORI_DIR), str(THIS_DIR)):
    try:
        sys.path.remove(_path)
    except ValueError:
        pass
sys.path.insert(0, str(THIS_DIR))
sys.path.insert(1, str(TORI_DIR))
from broker import (  # noqa: E402
    connect_paper, resolve_active_contract, current_position_size,
    place_market, positions_for_symbol,
)
from ib_insync import ContFuture, Future  # noqa: E402

ROLL_BUFFER_DAYS = 8  # roll to next contract when front is within this many days of expiry

from sizing import position_series  # noqa: E402
from signals import combined_forecast  # noqa: E402
import notify  # noqa: E402

LOG_FILE = Path(__file__).resolve().parent / "daily_log.jsonl"
STATE_FILE = Path(__file__).resolve().parent / ".rebalancer_state.json"
LOCK_FILE = Path(__file__).resolve().parent / ".rebalancer.lock"

TAU = 0.20
IDM = 1.5

# Do not invent a capital number if IBKR is unavailable. A guessed account size
# can turn a temporary broker problem into a badly sized order.
MIN_DAILY_BARS = 260
MAX_STALE_DAYS = 5
ORDER_FILL_TIMEOUT_SECONDS = 30
POSITION_SYNC_SECONDS = 3

# These are deliberately above normal model targets, but stop a corrupted price,
# multiplier, or account value from producing an outsized paper order.  They are
# safety rails, not trading signals.
MAX_ABS_CONTRACTS = {
    "ES": 4, "NQ": 2, "RTY": 6, "ZN": 12,
    "CL": 3, "GC": 3, "EUR": 8,
}
MAX_DAILY_TURNOVER = 25

# (ibkr_symbol, exchange, multiplier)
SYMBOLS = [
    ("ES", "CME",   50.0),
    ("NQ", "CME",   20.0),
    ("RTY", "CME",  50.0),
    ("ZN", "CBOT",  1000.0),
    ("CL", "NYMEX", 1000.0),
    ("GC", "COMEX", 100.0),
    ("EUR", "CME",  125000.0),   # Euro FX future (IBKR symbol is EUR, not 6E)
]


def log_event(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    line = json.dumps(event, default=str)
    print(line, flush=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


HEALTHCHECK = str(Path.home() / "IBC" / "healthcheck.sh")


def connect_with_recovery():
    """Connect to Gateway; if it's down at run time, restart it and retry.

    This is the core reliability fix: the scheduled run no longer just dies when
    Gateway happens to be down (daily re-auth window, transient rejection, a dropped
    session). It self-heals by invoking the same restart the health check uses.
    Returns a connected IB, or None if recovery ultimately failed.
    """
    import subprocess

    # quick first attempt (~1 min)
    try:
        return connect_paper(max_attempts=6, retry_delay_sec=10)
    except Exception as e:
        log_event({"kind": "connect_retry", "msg": f"initial connect failed ({e}); restarting Gateway"})

    # restart Gateway (healthcheck clears stale procs, reloads agent, waits for port)
    try:
        subprocess.run([HEALTHCHECK], timeout=400)
    except Exception as e:
        log_event({"kind": "connect_retry", "msg": f"healthcheck invocation failed: {e}"})

    # second attempt, patient (up to ~10 min for a fresh login/cooldown)
    try:
        return connect_paper(max_attempts=20, retry_delay_sec=30)
    except Exception as e:
        log_event({"kind": "error", "msg": f"connect failed after restart: {e}"})
        return None


def acquire_run_lock():
    """Return an exclusive process lock, or None when a run is already active."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    handle = LOCK_FILE.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.write(str(datetime.now(timezone.utc).isoformat()))
    handle.flush()
    return handle


def load_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    temporary.replace(STATE_FILE)


def get_capital(ib) -> tuple[float, str]:
    """Return NetLiquidation expressed in USD, the strategy's sizing currency.

    This paper account is EUR-denominated.  IBKR reports ExchangeRate[USD] as
    EUR per USD, so its EUR NetLiquidation must be divided by that rate.  USD
    accounts return their NetLiquidation directly.  If neither is available the
    caller must stop: falling back to a hard-coded $1m can mis-size an order.
    """
    values = list(ib.accountValues())
    net_liq = next((v for v in values if v.tag == "NetLiquidation" and v.currency == "USD"), None)
    if net_liq is not None:
        return float(net_liq.value), "USD"

    net_liq = next((v for v in values if v.tag == "NetLiquidation" and v.currency not in ("BASE", "USD")), None)
    usd_rate = next((v for v in values if v.tag == "ExchangeRate" and v.currency == "USD"), None)
    if net_liq is not None and usd_rate is not None:
        rate = float(usd_rate.value)
        if rate > 0:
            return float(net_liq.value) / rate, f"{net_liq.currency}->USD @ {rate:.6f}"

    raise RuntimeError("IBKR did not provide a usable NetLiquidation and USD exchange rate")


def daily_close_series(ib, sym: str, exch: str) -> pd.Series:
    """~3y of CONTINUOUS daily closes (a single front-month contract only has a few
    months of history; EWMAC(64,256) needs 256+ bars, so we use the continuous future)."""
    cont = ContFuture(symbol=sym, exchange=exch, currency="USD")
    try:
        ib.qualifyContracts(cont)
    except Exception:
        pass
    bars = ib.reqHistoricalData(
        cont, endDateTime="", durationStr="3 Y",
        barSizeSetting="1 day", whatToShow="TRADES", useRTH=False,
        formatDate=2, keepUpToDate=False,
    )
    if not bars:
        return pd.Series(dtype=float)
    closes = pd.Series(
        [float(b.close) for b in bars],
        index=pd.to_datetime([b.date for b in bars]),
    ).replace([float("inf"), float("-inf")], pd.NA).dropna()
    closes = closes[~closes.index.duplicated(keep="last")].sort_index()

    # At the scheduled 09:00 New York run IBKR includes the currently-forming
    # daily candle.  The backtest is based on completed closes, so always discard
    # that trailing bar rather than letting an intraday move change a daily signal.
    return closes.iloc[:-1].copy() if len(closes) > 1 else pd.Series(dtype=float)


def signal_session(closes: pd.Series) -> str:
    return pd.Timestamp(closes.index[-1]).date().isoformat()


def is_stale(closes: pd.Series) -> bool:
    last_date = pd.Timestamp(closes.index[-1]).date()
    age = (datetime.now(timezone.utc).date() - last_date).days
    return age < 0 or age > MAX_STALE_DAYS


def limit_target(sym: str, current: int, requested: int) -> tuple[int, list[str]]:
    """Clamp an extreme target and limit one run's change without blocking a stop."""
    notes: list[str] = []
    cap = MAX_ABS_CONTRACTS[sym]
    target = max(-cap, min(cap, requested))
    if target != requested:
        notes.append(f"target clamped from {requested:+d} to {target:+d} (cap {cap})")

    # The global turnover guard below handles portfolio-wide errors.  This local
    # cap prevents a reversal in one symbol from becoming a single large order.
    max_change = cap
    change = target - current
    if abs(change) > max_change:
        target = current + (max_change if change > 0 else -max_change)
        notes.append(f"one-run change limited to {max_change} contracts")
    return target, notes


def wait_for_fill(ib, trade, quantity: int) -> dict[str, Any]:
    """Wait for a terminal IBKR order status and report the actual result."""
    deadline = time.monotonic() + ORDER_FILL_TIMEOUT_SECONDS
    terminal = {"Filled", "Cancelled", "ApiCancelled", "Inactive"}
    while time.monotonic() < deadline:
        status = str(trade.orderStatus.status or "")
        if status in terminal:
            break
        ib.sleep(0.25)

    status = str(trade.orderStatus.status or "")
    filled = float(trade.orderStatus.filled or 0)
    remaining = float(trade.orderStatus.remaining or 0)
    if status != "Filled":
        try:
            ib.cancelOrder(trade.order)
            ib.sleep(1)
            status = str(trade.orderStatus.status or status)
            filled = float(trade.orderStatus.filled or filled)
            remaining = float(trade.orderStatus.remaining or remaining)
        except Exception:
            pass
    return {
        "status": status or "TimedOut",
        "filled": filled,
        "remaining": remaining,
        "avg_fill_price": float(trade.orderStatus.avgFillPrice or 0),
        "expected": quantity,
        "ok": status == "Filled" and filled == quantity,
    }


def submit_and_confirm(ib, contract, sym: str, side: str, quantity: int, kind: str) -> dict[str, Any]:
    trade = place_market(ib, contract, side, quantity, outside_rth=True, order_ref="tsmom")
    result = wait_for_fill(ib, trade, quantity)
    result.update({"symbol": sym, "side": side, "qty": quantity, "order_kind": kind})
    log_event({"kind": "order_result", **result})
    return result


def verify_positions(ib, plans: list[dict[str, Any]]) -> list[str]:
    """Confirm both active targets and that old-expiry positions are gone."""
    ib.sleep(POSITION_SYNC_SECONDS)
    mismatches: list[str] = []
    for plan in plans:
        actual = current_position_size(ib, plan["contract"])
        if actual != plan["target"]:
            mismatches.append(f"{plan['symbol']} target {plan['target']:+d}, actual {actual:+d}")
        for old in positions_for_symbol(ib, plan["symbol"]):
            if old.contract.conId != plan["contract"].conId:
                mismatches.append(f"{plan['symbol']} old expiry {old.contract.lastTradeDateOrContractMonth} still {int(old.position):+d}")
    return mismatches


def contract_for_old_position(ib, position, exchange: str) -> Future:
    """Rebuild an old future with an exchange before attempting a roll close."""
    contract = Future(conId=position.contract.conId, exchange=exchange, currency="USD")
    try:
        ib.qualifyContracts(contract)
    except Exception:
        contract = position.contract
        contract.exchange = exchange
    return contract


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="compute targets, place no orders")
    args = ap.parse_args()

    lock_handle = acquire_run_lock()
    if lock_handle is None:
        log_event({"kind": "skip", "reason": "another TSMOM run is already active"})
        return

    ib = None
    try:
        log_event({"kind": "start", "mode": "dry_run" if args.dry_run else "live"})
        ib = connect_with_recovery()
        if ib is None:
            raise RuntimeError("connect failed even after Gateway restart — manual login needed")

        capital, capital_source = get_capital(ib)
        weight = 1.0 / len(SYMBOLS)
        log_event({
            "kind": "connected", "accounts": ib.managedAccounts(),
            "capital_usd": round(capital), "capital_source": capital_source,
        })

        # Build the complete portfolio plan before placing any order.  A missing or
        # stale price in one market now stops the whole daily rebalance rather than
        # leaving a partially updated portfolio.
        plans: list[dict[str, Any]] = []
        rebalances: list[dict[str, Any]] = []
        for sym, exch, mult in SYMBOLS:
            contract = resolve_active_contract(ib, sym, exch, roll_buffer_days=ROLL_BUFFER_DAYS)
            ib.qualifyContracts(contract)
            closes = daily_close_series(ib, sym, exch)
            if len(closes) < MIN_DAILY_BARS:
                raise RuntimeError(f"{sym}: only {len(closes)} completed daily bars")
            if is_stale(closes):
                raise RuntimeError(f"{sym}: last completed daily close is stale ({signal_session(closes)})")

            forecast = float(combined_forecast(closes).iloc[-1])
            target_frac = float(
                position_series(closes, mult, capital, tau=TAU, idm=IDM, weight=weight).iloc[-1]
            )
            requested_target = int(round(target_frac))
            current = current_position_size(ib, contract)
            target, safety_notes = limit_target(sym, current, requested_target)
            stale_positions = [p for p in positions_for_symbol(ib, sym) if p.contract.conId != contract.conId]
            rec = {
                "symbol": sym, "forecast": round(forecast, 1), "expiry": contract.lastTradeDateOrContractMonth,
                "target_frac": round(target_frac, 2), "requested_target": requested_target,
                "target": target, "current": current, "order": target - current,
                "signal_session": signal_session(closes), "safety_notes": safety_notes,
            }
            plans.append({"symbol": sym, "exchange": exch, "contract": contract,
                          "stale_positions": stale_positions, **rec})
            rebalances.append(rec)
            log_event({"kind": "plan", **rec})

        sessions = {p["signal_session"] for p in plans}
        if len(sessions) != 1:
            raise RuntimeError(f"markets returned mixed signal dates: {sorted(sessions)}")
        session = sessions.pop()
        prior_state = load_state()
        if not args.dry_run and prior_state.get("successful_signal_session") == session:
            log_event({"kind": "skip", "reason": "session already completed", "signal_session": session})
            return

        turnover = sum(abs(p["order"]) for p in plans)
        turnover += sum(abs(int(old.position)) for p in plans for old in p["stale_positions"])
        if turnover > MAX_DAILY_TURNOVER:
            raise RuntimeError(
                f"safety stop: planned turnover {turnover} exceeds {MAX_DAILY_TURNOVER} contracts"
            )

        execution: list[dict[str, Any]] = []
        failed_symbols: set[str] = set()
        if not args.dry_run:
            # Exit old contracts before entering their replacement.  If the exit is
            # not confirmed, do not open a new contract and accidentally double risk.
            for plan in plans:
                for old in plan["stale_positions"]:
                    side = "SELL" if old.position > 0 else "BUY"
                    old_contract = contract_for_old_position(ib, old, plan["exchange"])
                    result = submit_and_confirm(
                        ib, old_contract, plan["symbol"], side, abs(int(old.position)), "roll_out"
                    )
                    execution.append(result)
                    if not result["ok"]:
                        failed_symbols.add(plan["symbol"])

            for plan in plans:
                order = plan["order"]
                if order == 0 or plan["symbol"] in failed_symbols:
                    continue
                side = "BUY" if order > 0 else "SELL"
                result = submit_and_confirm(ib, plan["contract"], plan["symbol"], side, abs(order), "rebalance")
                execution.append(result)
                if not result["ok"]:
                    failed_symbols.add(plan["symbol"])

            mismatches = verify_positions(ib, plans)
        else:
            mismatches = []

        safety_notes = [f"{p['symbol']}: {note}" for p in plans for note in p["safety_notes"]]
        execution_failures = [r for r in execution if not r["ok"]]
        verified = not args.dry_run and not execution_failures and not mismatches
        if args.dry_run:
            integrity = "dry-run: no orders sent"
        elif verified:
            integrity = "all fills and positions verified"
            save_state({
                "successful_signal_session": session,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "capital_usd": round(capital, 2),
                "targets": {p["symbol"]: p["target"] for p in plans},
            })
        else:
            integrity = "ACTION NEEDED: order or position mismatch"
            notify.error("TSMOM order/position mismatch — check the daily rebalance summary")

        notify.daily_summary(
            rebalances, dry_run=args.dry_run, execution=execution,
            integrity=integrity, safety_notes=safety_notes + mismatches,
        )
        log_event({
            "kind": "done", "signal_session": session, "rebalances": len(rebalances),
            "orders": len(execution), "verified": verified, "mismatches": mismatches,
        })
    except Exception as e:
        log_event({"kind": "error", "msg": str(e)})
        notify.error(str(e))
        raise
    finally:
        if ib is not None:
            ib.disconnect()
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
            lock_handle.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
