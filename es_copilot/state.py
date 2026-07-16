"""Shared in-memory state — orchestrator writes, agent tools read."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .events import StrategyEvent
from .levels import Levels
from .strategy_monitor import MonitorSnapshot


@dataclass
class UserPosition:
    direction: str
    quantity: int
    entry_price: float
    stop: float
    target: Optional[float] = None
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "direction": self.direction,
            "quantity": self.quantity,
            "entry_price": self.entry_price,
            "stop": self.stop,
            "target": self.target,
            "opened_at": self.opened_at.isoformat(),
        }


@dataclass
class CopilotState:
    last_price: Optional[float] = None
    last_bar_ts: Optional[str] = None
    levels: Optional[Levels] = None
    monitor: Optional[MonitorSnapshot] = None
    user_position: Optional[UserPosition] = None
    recent_events: list[StrategyEvent] = field(default_factory=list)
    connected: bool = False
    last_poll_at: Optional[datetime] = None

    def add_event(self, event: StrategyEvent, keep_last: int = 50) -> None:
        self.recent_events.append(event)
        if len(self.recent_events) > keep_last:
            self.recent_events = self.recent_events[-keep_last:]
