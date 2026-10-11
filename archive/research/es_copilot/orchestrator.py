"""Async poll loop: pull bars, recompute levels, run strategy monitor, emit events."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Callable

from ib_insync import IB, Future

from .broker import fetch_15m_bars_async
from .config import BAR_DURATION, EVENT_LOG, LOG_DIR, POLL_INTERVAL_SEC
from .events import StrategyEvent
from .levels import compute_levels
from .state import CopilotState
from .strategy_monitor import StrategyMonitor

EventListener = Callable[[StrategyEvent], None]


class Orchestrator:
    def __init__(self, ib: IB, contract: Future, state: CopilotState):
        self.ib = ib
        self.contract = contract
        self.state = state
        self.monitor = StrategyMonitor()
        self.listeners: list[EventListener] = []
        self._running = False
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    def on_event(self, listener: EventListener) -> None:
        self.listeners.append(listener)

    async def poll_once(self) -> None:
        bars = await fetch_15m_bars_async(self.ib, self.contract, duration=BAR_DURATION)
        if bars.empty:
            return

        self.state.last_poll_at = datetime.now(timezone.utc)
        self.state.last_price = float(bars["close"].iloc[-1])
        self.state.last_bar_ts = str(bars.index[-1])
        self.state.levels = compute_levels(bars)
        self.state.connected = True

        events, snapshot = self.monitor.process(bars)
        self.state.monitor = snapshot
        for ev in events:
            self.state.add_event(ev)
            self._log_event(ev)
            for cb in self.listeners:
                try:
                    cb(ev)
                except Exception:
                    pass

    async def run(self) -> None:
        self._running = True
        while self._running:
            try:
                await self.poll_once()
            except Exception as e:
                self._log_raw({
                    "kind": "poll_error",
                    "msg": str(e),
                    "ts": datetime.now(timezone.utc).isoformat(),
                })
            await asyncio.sleep(POLL_INTERVAL_SEC)

    def stop(self) -> None:
        self._running = False

    def _log_event(self, event: StrategyEvent) -> None:
        self._log_raw(event.to_dict())

    def _log_raw(self, entry: dict) -> None:
        with EVENT_LOG.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
