"""Events the strategy monitor emits to the orchestrator + chat agent."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import pandas as pd


class EventKind(Enum):
    BREAK_CONFIRMED = "break_confirmed"
    ENTRY_SIGNAL = "entry_signal"
    INVALIDATED = "invalidated"
    SAFETY_LINE_HIT = "safety_line_hit"


@dataclass
class StrategyEvent:
    kind: EventKind
    direction: str
    symbol: str
    bar_ts: pd.Timestamp
    price: float
    trendline_value: Optional[float] = None
    stop: Optional[float] = None
    note: Optional[str] = None
    fired_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "direction": self.direction,
            "symbol": self.symbol,
            "bar_ts": str(self.bar_ts),
            "price": self.price,
            "trendline_value": self.trendline_value,
            "stop": self.stop,
            "note": self.note,
            "fired_at": self.fired_at.isoformat(),
        }

    def headline(self) -> str:
        d = self.direction.upper() if self.direction else ""
        if self.kind is EventKind.BREAK_CONFIRMED:
            return f"{self.symbol} {d} setup: trendline broke at {self.price:.2f}, watching for pullback"
        if self.kind is EventKind.ENTRY_SIGNAL:
            stop = f", stop {self.stop:.2f}" if self.stop else ""
            return f"{self.symbol} {d} ENTRY: pullback to {self.price:.2f}{stop}"
        if self.kind is EventKind.INVALIDATED:
            return f"{self.symbol} pending {d} invalidated at {self.price:.2f}"
        if self.kind is EventKind.SAFETY_LINE_HIT:
            return f"{self.symbol} {d} EXIT at {self.price:.2f} ({self.note or 'safety line'})"
        return f"{self.symbol} {self.kind.value}"
