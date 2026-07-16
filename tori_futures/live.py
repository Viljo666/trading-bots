"""Live paper-trading runner — polling architecture.

Every POLL_INTERVAL_SEC seconds, for each symbol we:
  1. Re-fetch the latest 60 days of 15m bars from IBKR (one-shot, no streaming)
  2. Run Strategy on the fresh DataFrame
  3. Diff state vs the prior poll -> place/close orders on IBKR paper

This avoids the `keepUpToDate=True` streaming dependency, which doesn't reliably
fire on paper accounts without live market data subscriptions. Polling works
everywhere with a single requirement: IB Gateway is up.

Telegram pings on: startup, position open/close, IB disconnect, gateway down,
and "poll loop stalled" if no successful poll in WATCHDOG_STALL_MIN minutes.

Prereqs:
  * IB Gateway (paper, port 4002) running and logged in
  * Read-Only API UNCHECKED
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd
from ib_insync import IB, util

from broker import (
    GATEWAY_ALERT_FLAG,
    LiveBarsHandle,
    connect_paper,
    current_position_size,
    fetch_15m_bars_async,
    flatten_position,
    place_market,
    resolve_front_month,
)
from strategy import State, Strategy, StrategyConfig
import notify

LOG_FILE = Path(__file__).parent / "live_log.jsonl"

POLL_INTERVAL_SEC = 90        # how often to refresh bars + run strategy
WATCHDOG_STALL_MIN = 10       # if no successful poll in this many minutes, exit


@dataclass(frozen=True)
class SymbolSpec:
    ibkr_symbol: str
    exchange: str
    dollars_per_point: float
    config: StrategyConfig


SYMBOLS: list[SymbolSpec] = [
    SymbolSpec(
        ibkr_symbol="ES", exchange="CME", dollars_per_point=50.0,
        config=StrategyConfig(
            rth_only=True, ema_period=None, min_slope_abs=0.0, atr_stop_mult=None,
            dollars_per_point=50.0,
        ),
    ),
    SymbolSpec(
        ibkr_symbol="NQ", exchange="CME", dollars_per_point=20.0,
        config=StrategyConfig(
            rth_only=True, ema_period=None, min_slope_abs=0.0, atr_stop_mult=None,
            dollars_per_point=20.0,
        ),
    ),
    SymbolSpec(
        ibkr_symbol="CL", exchange="NYMEX", dollars_per_point=1000.0,
        config=StrategyConfig(
            rth_only=True, ema_period=None, min_slope_abs=0.0, atr_stop_mult=1.0,
            dollars_per_point=1000.0,
        ),
    ),
    SymbolSpec(
        ibkr_symbol="GC", exchange="COMEX", dollars_per_point=100.0,
        config=StrategyConfig(
            rth_only=False, ema_period=200, min_slope_abs=1.0, atr_stop_mult=None,
            dollars_per_point=100.0,
        ),
    ),
]


def log_event(event: dict) -> None:
    event["ts"] = datetime.now(timezone.utc).isoformat()
    line = json.dumps(event, default=str)
    print(line, flush=True)
    with LOG_FILE.open("a") as f:
        f.write(line + "\n")


class LiveBot:
    def __init__(self, ib: IB, contract, symbol: str, config: StrategyConfig) -> None:
        self.ib = ib
        self.contract = contract
        self.symbol = symbol
        self.config = config
        self.last_trade_count: int = 0
        self.last_state: State = State.IDLE
        self.last_successful_poll: datetime = datetime.now(timezone.utc)
        # First-poll priming: the strategy will reproduce all historical signals from
        # the 60d window. Capture them silently so we only act on signals that fire
        # AFTER the bot is live (entry_time > primed_at_ts).
        self.primed: bool = False
        self.primed_at_ts: Optional[pd.Timestamp] = None

    async def poll_once(self) -> None:
        """Fetch fresh bars (async) and run one strategy step."""
        handle = await fetch_15m_bars_async(self.ib, self.contract, duration="60 D")
        df = handle.to_dataframe()
        if len(df) < 50:
            return
        self.last_successful_poll = datetime.now(timezone.utc)
        self._step_with_df(df)

    def _is_post_priming(self, ts) -> bool:
        if self.primed_at_ts is None:
            return False
        return pd.Timestamp(ts) > self.primed_at_ts

    def _step_with_df(self, df: pd.DataFrame) -> None:
        strat = Strategy(bars=df, config=self.config)
        # close_open_at_end=False so an open position keeps strat.state = IN_POSITION
        # — otherwise the run() force-closes at end_of_data and the order_open
        # transition check below never sees IN_POSITION.
        strat.run(close_open_at_end=False)

        if not self.primed:
            log_event({
                "kind": "primed",
                "symbol": self.symbol,
                "historical_trades": len(strat.trades),
                "ending_state": strat.state.value,
                "last_bar_ts": str(df.index[-1]),
            })
            self.last_trade_count = len(strat.trades)
            self.primed_at_ts = pd.Timestamp(df.index[-1])
            self.last_state = State.IDLE   # force IDLE; freshness check gates real entries
            self.primed = True
            return

        # newly-closed trades — only those that ENTERED after we went live
        if len(strat.trades) > self.last_trade_count:
            for t in strat.trades[self.last_trade_count:]:
                if not self._is_post_priming(t.entry_time):
                    continue   # historical trade we never took — skip notify
                log_event({
                    "kind": "signal_close",
                    "symbol": self.symbol,
                    "direction": t.direction,
                    "entry_time": t.entry_time,
                    "entry_price": t.entry_price,
                    "exit_time": t.exit_time,
                    "exit_price": t.exit_price,
                    "exit_reason": t.exit_reason,
                    "pnl_points": round(t.pnl_points, 2),
                    "pnl_dollars": round(t.pnl_dollars, 2),
                })
                notify.position_closed(
                    symbol=self.symbol,
                    direction=t.direction,
                    entry=t.entry_price,
                    exit_price=t.exit_price or 0.0,
                    pnl_points=t.pnl_points,
                    pnl_dollars=t.pnl_dollars,
                    reason=t.exit_reason or "",
                )
                if current_position_size(self.ib, self.contract) != 0:
                    flatten_position(self.ib, self.contract)
                    log_event({"kind": "order_flatten", "symbol": self.symbol})
            self.last_trade_count = len(strat.trades)

        # newly-opened position — only if entry happened after priming
        if (strat.state is State.IN_POSITION
                and self.last_state is not State.IN_POSITION
                and strat.position is not None
                and self._is_post_priming(strat.position.entry_time)):
            direction = strat.position.direction
            side = "BUY" if direction == "long" else "SELL"
            place_market(self.ib, self.contract, side, quantity=1)
            log_event({
                "kind": "order_open",
                "symbol": self.symbol,
                "direction": direction,
                "entry_price_expected": strat.position.entry_price,
                "initial_stop": strat.position.initial_stop,
                "entry_time": str(strat.position.entry_time),
            })
            notify.position_opened(
                symbol=self.symbol,
                direction=direction,
                entry=strat.position.entry_price,
                stop=strat.position.initial_stop,
                config=self.config.label(),
            )
            self.last_state = State.IN_POSITION
        elif strat.state is not State.IN_POSITION and self.last_state is State.IN_POSITION:
            # we were in a real (post-priming) position and now we're out
            log_event({
                "kind": "state_change",
                "symbol": self.symbol,
                "from": self.last_state.value,
                "to": strat.state.value,
                "last_bar_ts": str(df.index[-1]),
                "last_close": float(df["close"].iloc[-1]),
            })
            self.last_state = strat.state


def main() -> None:
    log_event({"kind": "startup", "symbols": [s.ibkr_symbol for s in SYMBOLS]})
    ib = connect_paper()
    log_event({"kind": "connected", "accounts": ib.managedAccounts()})

    bots: list[LiveBot] = []
    for spec in SYMBOLS:
        try:
            contract = resolve_front_month(ib, spec.ibkr_symbol, spec.exchange)
            ib.qualifyContracts(contract)
        except Exception as e:
            log_event({"kind": "contract_error", "symbol": spec.ibkr_symbol, "msg": str(e)})
            continue

        log_event({
            "kind": "contract",
            "symbol": spec.ibkr_symbol,
            "exchange": spec.exchange,
            "expiry": contract.lastTradeDateOrContractMonth,
            "conId": contract.conId,
        })

        if current_position_size(ib, contract) != 0:
            flatten_position(ib, contract)
            log_event({"kind": "startup_flatten", "symbol": spec.ibkr_symbol})

        bot = LiveBot(ib=ib, contract=contract, symbol=spec.ibkr_symbol, config=spec.config)
        bots.append(bot)
        log_event({
            "kind": "ready",
            "symbol": spec.ibkr_symbol,
            "config": bot.config.label(),
        })

    notify.startup([b.symbol for b in bots])

    shutting_down = {"flag": False}

    def _shutdown(signum=None, frame=None):
        shutting_down["flag"] = True
        log_event({"kind": "shutdown", "signal": signum})
        for b in bots:
            try:
                if current_position_size(ib, b.contract) != 0:
                    flatten_position(ib, b.contract)
                    log_event({"kind": "shutdown_flatten", "symbol": b.symbol})
            except Exception:
                pass
        try:
            ib.disconnect()
        except Exception:
            pass
        notify.shutdown()
        raise SystemExit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    def _on_disconnect():
        if shutting_down["flag"]:
            return
        log_event({"kind": "ib_disconnected"})
        notify.disconnected()
        # set the alert flag so the launchd-restarted process doesn't immediately
        # send another "gateway unreachable" message for the same outage
        try:
            GATEWAY_ALERT_FLAG.touch()
        except Exception:
            pass
        os._exit(2)

    ib.disconnectedEvent += _on_disconnect

    # main poll loop
    async def _poll_loop():
        while not shutting_down["flag"]:
            for b in bots:
                try:
                    await b.poll_once()
                except Exception as e:
                    log_event({"kind": "error", "symbol": b.symbol, "msg": str(e)})
                    notify.error(b.symbol, str(e))
            await asyncio.sleep(POLL_INTERVAL_SEC)

    # watchdog: if any bot stops polling successfully for too long, bail
    async def _watchdog_loop():
        while not shutting_down["flag"]:
            await asyncio.sleep(60)
            now = datetime.now(timezone.utc)
            for b in bots:
                age_min = (now - b.last_successful_poll).total_seconds() / 60
                if age_min >= WATCHDOG_STALL_MIN:
                    log_event({"kind": "poll_stalled", "symbol": b.symbol, "age_min": round(age_min, 1)})
                    notify.stale_bars(b.symbol, int(age_min))
                    os._exit(3)

    asyncio.ensure_future(_poll_loop())
    asyncio.ensure_future(_watchdog_loop())

    util.run()


if __name__ == "__main__":
    main()
