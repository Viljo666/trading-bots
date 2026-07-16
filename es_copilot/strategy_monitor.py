"""Wraps Tori's Strategy as a sensor — runs it on each poll, diffs state vs
the previous poll, and emits BREAK_CONFIRMED / ENTRY_SIGNAL / INVALIDATED /
SAFETY_LINE_HIT events. Never places orders.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import pandas as pd

_TORI = Path(__file__).resolve().parent.parent / "tori_futures"
if str(_TORI) not in sys.path:
    sys.path.insert(0, str(_TORI))

from strategy import State, Strategy, StrategyConfig   # noqa: E402

from .config import (
    DOLLARS_PER_POINT,
    STRATEGY_ATR_STOP_MULT,
    STRATEGY_EMA_PERIOD,
    STRATEGY_MIN_SLOPE_ABS,
    STRATEGY_RTH_ONLY,
    SYMBOL,
)
from .events import EventKind, StrategyEvent


def make_config() -> StrategyConfig:
    return StrategyConfig(
        rth_only=STRATEGY_RTH_ONLY,
        ema_period=STRATEGY_EMA_PERIOD,
        min_slope_abs=STRATEGY_MIN_SLOPE_ABS,
        atr_stop_mult=STRATEGY_ATR_STOP_MULT,
        dollars_per_point=DOLLARS_PER_POINT,
    )


@dataclass
class MonitorSnapshot:
    state: str
    pending_direction: Optional[str] = None
    pending_line_value_now: Optional[float] = None
    pending_break_bar_ts: Optional[str] = None
    position_direction: Optional[str] = None
    position_entry: Optional[float] = None
    position_stop: Optional[float] = None
    position_safety_line_now: Optional[float] = None
    last_bar_ts: Optional[str] = None
    last_close: Optional[float] = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class StrategyMonitor:
    config: StrategyConfig = field(default_factory=make_config)
    last_state: State = State.IDLE
    last_trade_count: int = 0
    last_pending_direction: Optional[str] = None
    primed: bool = False
    primed_at_ts: Optional[pd.Timestamp] = None
    last_snapshot: Optional[MonitorSnapshot] = None

    def process(self, bars: pd.DataFrame) -> tuple[list[StrategyEvent], MonitorSnapshot]:
        events: list[StrategyEvent] = []
        if len(bars) < 50:
            snap = self.last_snapshot or MonitorSnapshot(state="warming_up")
            return events, snap

        strat = Strategy(bars=bars, config=self.config)
        strat.run()
        snapshot = self._snapshot(strat, bars)
        self.last_snapshot = snapshot

        if not self.primed:
            # First poll replays full history silently — we only act on signals
            # firing on bars newer than primed_at_ts.
            self.last_trade_count = len(strat.trades)
            self.primed_at_ts = pd.Timestamp(bars.index[-1])
            self.last_state = State.IDLE
            self.last_pending_direction = strat.pending.direction if strat.pending else None
            self.primed = True
            return events, snapshot

        if len(strat.trades) > self.last_trade_count:
            for t in strat.trades[self.last_trade_count:]:
                if not self._post_priming(t.entry_time):
                    continue
                events.append(StrategyEvent(
                    kind=EventKind.SAFETY_LINE_HIT,
                    direction=t.direction,
                    symbol=SYMBOL,
                    bar_ts=t.exit_time,
                    price=float(t.exit_price) if t.exit_price else 0.0,
                    note=t.exit_reason,
                ))
            self.last_trade_count = len(strat.trades)

        cur_state = strat.state
        if cur_state is not self.last_state:
            if cur_state is State.WAITING_PULLBACK and strat.pending is not None:
                p = strat.pending
                if self._post_priming(bars.index[p.break_bar_index]):
                    events.append(StrategyEvent(
                        kind=EventKind.BREAK_CONFIRMED,
                        direction=p.direction,
                        symbol=SYMBOL,
                        bar_ts=bars.index[p.break_bar_index],
                        price=float(bars["close"].iloc[p.break_bar_index]),
                        trendline_value=p.line.value_at(p.break_bar_index),
                        note="trendline broken on bar close",
                    ))
            elif cur_state is State.IN_POSITION and strat.position is not None:
                pos = strat.position
                if self._post_priming(pos.entry_time):
                    events.append(StrategyEvent(
                        kind=EventKind.ENTRY_SIGNAL,
                        direction=pos.direction,
                        symbol=SYMBOL,
                        bar_ts=pos.entry_time,
                        price=pos.entry_price,
                        stop=pos.initial_stop,
                        note="pullback to broken line",
                    ))
            elif cur_state is State.IDLE and self.last_state is State.WAITING_PULLBACK:
                events.append(StrategyEvent(
                    kind=EventKind.INVALIDATED,
                    direction=self.last_pending_direction or "n/a",
                    symbol=SYMBOL,
                    bar_ts=bars.index[-1],
                    price=float(bars["close"].iloc[-1]),
                    note="pending pullback invalidated",
                ))
            self.last_state = cur_state

        self.last_pending_direction = strat.pending.direction if strat.pending else None
        return events, snapshot

    def _post_priming(self, ts) -> bool:
        if self.primed_at_ts is None:
            return False
        return pd.Timestamp(ts) > self.primed_at_ts

    def _snapshot(self, strat: Strategy, bars: pd.DataFrame) -> MonitorSnapshot:
        snap = MonitorSnapshot(
            state=strat.state.value,
            last_bar_ts=str(bars.index[-1]),
            last_close=float(bars["close"].iloc[-1]),
        )
        if strat.pending is not None:
            snap.pending_direction = strat.pending.direction
            snap.pending_line_value_now = strat.pending.line.value_at(len(bars) - 1)
            snap.pending_break_bar_ts = str(bars.index[strat.pending.break_bar_index])
        if strat.position is not None:
            snap.position_direction = strat.position.direction
            snap.position_entry = strat.position.entry_price
            snap.position_stop = strat.position.initial_stop
            if strat.position.safety_line is not None:
                snap.position_safety_line_now = strat.position.safety_line.value_at(len(bars) - 1)
        return snap
