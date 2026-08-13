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
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

# reuse the canonical IBKR wrapper from the Tori bot. APPEND (not insert-0) so this
# folder's own `notify` module still wins over tori_futures/notify.py.
sys.path.append(str(Path(__file__).resolve().parent.parent / "tori_futures"))
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

TAU = 0.20
IDM = 1.5
FALLBACK_CAPITAL = 1_000_000.0   # used if NetLiquidation can't be read

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


def get_capital(ib) -> float:
    try:
        for v in ib.accountValues():
            if v.tag == "NetLiquidation" and v.currency in ("USD", "BASE"):
                return float(v.value)
    except Exception:
        pass
    return FALLBACK_CAPITAL


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
    return pd.Series(
        [float(b.close) for b in bars],
        index=pd.to_datetime([b.date for b in bars]),
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="compute targets, place no orders")
    args = ap.parse_args()

    log_event({"kind": "start", "mode": "dry_run" if args.dry_run else "live"})
    ib = connect_with_recovery()
    if ib is None:
        log_event({"kind": "error", "msg": "connect failed even after Gateway restart"})
        notify.error("connect failed even after Gateway restart — manual login needed")
        sys.exit(1)

    capital = get_capital(ib)
    weight = 1.0 / len(SYMBOLS)
    log_event({"kind": "connected", "accounts": ib.managedAccounts(), "capital": round(capital)})

    rebalances = []
    for sym, exch, mult in SYMBOLS:
        try:
            # active = front month, rolled forward if front is near expiry
            contract = resolve_active_contract(ib, sym, exch, roll_buffer_days=ROLL_BUFFER_DAYS)
            ib.qualifyContracts(contract)

            # ROLL-OUT: close any open position held in a NON-active (older) contract
            # for this symbol. Without this, an expiring contract's position is left
            # to settle/deliver instead of rolling to the new front month.
            for p in positions_for_symbol(ib, sym):
                if p.contract.conId != contract.conId:
                    side = "SELL" if p.position > 0 else "BUY"
                    # Contracts returned by ib.positions() have an EMPTY exchange field,
                    # and placing an order on one is rejected with
                    # "Error 321: Missing order exchange" (silently cancelled, so the
                    # old position never closed and exposure doubled). Re-qualify by
                    # conId with the exchange set before sending the closing order.
                    old_c = Future(conId=p.contract.conId, exchange=exch, currency="USD")
                    try:
                        ib.qualifyContracts(old_c)
                    except Exception:
                        old_c = p.contract
                        old_c.exchange = exch
                    if not args.dry_run:
                        place_market(ib, old_c, side, abs(int(p.position)), outside_rth=True)
                    log_event({
                        "kind": "roll_out", "symbol": sym,
                        "old_expiry": p.contract.lastTradeDateOrContractMonth,
                        "new_expiry": contract.lastTradeDateOrContractMonth,
                        "closed": int(p.position),
                    })

            closes = daily_close_series(ib, sym, exch)
            if len(closes) < 260:
                log_event({"kind": "skip", "symbol": sym, "reason": f"only {len(closes)} daily bars"})
                continue

            forecast = float(combined_forecast(closes).iloc[-1])
            target_frac = float(
                position_series(closes, mult, capital, tau=TAU, idm=IDM, weight=weight).iloc[-1]
            )
            target = int(round(target_frac))
            current = current_position_size(ib, contract)   # active contract only
            order = target - current

            rec = {
                "symbol": sym, "forecast": round(forecast, 1), "expiry": contract.lastTradeDateOrContractMonth,
                "target_frac": round(target_frac, 2), "target": target,
                "current": current, "order": order,
            }
            rebalances.append(rec)
            log_event({"kind": "plan", **rec})

            if order != 0 and not args.dry_run:
                side = "BUY" if order > 0 else "SELL"
                place_market(ib, contract, side, abs(order), outside_rth=True)
                log_event({"kind": "order", "symbol": sym, "side": side, "qty": abs(order)})
        except Exception as e:
            log_event({"kind": "error", "symbol": sym, "msg": str(e)})

    notify.daily_summary(rebalances, dry_run=args.dry_run)
    log_event({"kind": "done", "rebalances": len(rebalances),
               "orders": sum(1 for r in rebalances if r["order"] != 0)})
    ib.disconnect()


if __name__ == "__main__":
    main()
