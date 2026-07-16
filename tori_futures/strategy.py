"""Tori-style trendline-break strategy with optional filters.

Base flow (long; short symmetric):
  IDLE
    scan last-2 swing highs for a descending trendline
    if current bar CLOSE > trendline -> break confirmed
  WAITING_FOR_PULLBACK
    when a later bar's low touches the broken line, enter long at that bar's close
    abandon if `pullback_timeout` bars pass without a touch, or close back below line
  IN_POSITION (long)
    initial stop: last swing-low pivot (or ATR-based, if configured) — HARD stop fill
    once 2 newer swing lows form an ascending safety line, switch stop to that line (trails)
    safety-line exit is on bar CLOSE through the line (wicks allowed)

Configurable filters (StrategyConfig):
  - rth_only:        only enter during US cash session (9:30-16:00 ET)
  - ema_period:      require close to be on correct side of EMA(period) for direction
  - min_slope_abs:   require |slope| of the trendline (points/bar) to be >= threshold
  - atr_stop_mult:   if set, initial stop = entry -/+ atr_mult * ATR(14), else swing pivot
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal, Optional

import numpy as np
import pandas as pd

from pivots import Pivot, find_pivots, pivots_confirmed_by, LEFT, RIGHT
from trendline import (
    Trendline,
    closed_through,
    current_down_trendline,
    current_up_trendline,
)


PULLBACK_TIMEOUT_BARS = 20
ES_DOLLARS_PER_POINT = 50.0   # kept for backtest/sweep scripts that hardcode ES
NQ_DOLLARS_PER_POINT = 20.0
ATR_PERIOD = 14

Direction = Literal["long", "short"]


class State(Enum):
    IDLE = "idle"
    WAITING_PULLBACK = "waiting_pullback"
    IN_POSITION = "in_position"


@dataclass(frozen=True)
class StrategyConfig:
    rth_only: bool = False
    ema_period: Optional[int] = None       # e.g. 50 for trend filter
    min_slope_abs: float = 0.0             # in points per bar
    atr_stop_mult: Optional[float] = None  # if None, use swing-pivot stop
    dollars_per_point: float = ES_DOLLARS_PER_POINT   # ES=50, NQ=20, YM=5

    def label(self) -> str:
        parts = []
        parts.append("RTH" if self.rth_only else "24h")
        parts.append(f"EMA{self.ema_period}" if self.ema_period else "noEMA")
        parts.append(f"slope>={self.min_slope_abs}" if self.min_slope_abs > 0 else "anySlope")
        parts.append(f"ATR{self.atr_stop_mult}" if self.atr_stop_mult else "pivotStop")
        return " | ".join(parts)


@dataclass
class Trade:
    direction: Direction
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    dollars_per_point: float = ES_DOLLARS_PER_POINT

    @property
    def pnl_points(self) -> float:
        if self.exit_price is None:
            return 0.0
        sign = 1.0 if self.direction == "long" else -1.0
        return sign * (self.exit_price - self.entry_price)

    @property
    def pnl_dollars(self) -> float:
        return self.pnl_points * self.dollars_per_point


@dataclass
class _Pending:
    line: Trendline
    direction: Direction
    break_bar_index: int


@dataclass
class _Position:
    direction: Direction
    entry_time: pd.Timestamp
    entry_price: float
    entry_bar_index: int
    initial_stop: float
    safety_line: Optional[Trendline] = None


def _compute_atr(bars: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    high = bars["high"]
    low = bars["low"]
    prev_close = bars["close"].shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def _compute_ema(bars: pd.DataFrame, period: int) -> pd.Series:
    return bars["close"].ewm(span=period, adjust=False).mean()


def _in_rth(ts: pd.Timestamp) -> bool:
    """9:30-16:00 US/Eastern. yfinance returns tz-aware UTC timestamps."""
    et = ts.tz_convert("US/Eastern") if ts.tzinfo is not None else ts.tz_localize("UTC").tz_convert("US/Eastern")
    if et.weekday() >= 5:
        return False
    minutes = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


@dataclass
class Strategy:
    bars: pd.DataFrame
    config: StrategyConfig = field(default_factory=StrategyConfig)
    state: State = State.IDLE
    pending: Optional[_Pending] = None
    position: Optional[_Position] = None
    trades: list[Trade] = field(default_factory=list)
    _pivots_cache: list[Pivot] = field(default_factory=list)
    _atr: Optional[pd.Series] = None
    _ema: Optional[pd.Series] = None

    def __post_init__(self) -> None:
        self._pivots_cache = find_pivots(self.bars)
        if self.config.atr_stop_mult is not None:
            self._atr = _compute_atr(self.bars)
        if self.config.ema_period is not None:
            self._ema = _compute_ema(self.bars, self.config.ema_period)

    # ------------------------------------------------------------------ helpers

    def _pivots_up_to(self, bar_index: int) -> list[Pivot]:
        return pivots_confirmed_by(self._pivots_cache, bar_index, RIGHT)

    def _last_pivot_of(self, kind: Literal["high", "low"], bar_index: int) -> Optional[Pivot]:
        for p in reversed(self._pivots_up_to(bar_index)):
            if p.kind == kind:
                return p
        return None

    def _passes_ema_filter(self, i: int, direction: Direction) -> bool:
        if self._ema is None:
            return True
        ema_val = self._ema.iloc[i]
        if np.isnan(ema_val):
            return False
        close = self.bars["close"].iloc[i]
        return (direction == "long" and close > ema_val) or (direction == "short" and close < ema_val)

    def _passes_slope_filter(self, line: Trendline) -> bool:
        return abs(line.slope) >= self.config.min_slope_abs

    def _passes_session_filter(self, ts: pd.Timestamp) -> bool:
        return _in_rth(ts) if self.config.rth_only else True

    # ------------------------------------------------------------------ main loop

    def process_bar(self, i: int) -> None:
        if i < LEFT + RIGHT:
            return

        bar = self.bars.iloc[i]
        close = float(bar["close"])
        high = float(bar["high"])
        low = float(bar["low"])
        ts = self.bars.index[i]

        # position management must run regardless of session filter
        # (we won't open new trades outside RTH but we must manage open ones)
        if self.state is State.IDLE:
            if self._passes_session_filter(ts):
                self._check_for_break(i, close)
        elif self.state is State.WAITING_PULLBACK:
            self._manage_pullback(i, ts, high, low, close)
        elif self.state is State.IN_POSITION:
            self._manage_position(i, ts, high, low, close)

    # ------------------------------------------------------------------ idle -> pending

    def _check_for_break(self, i: int, close: float) -> None:
        pivots = self._pivots_up_to(i)

        down = current_down_trendline(pivots)
        if (
            down
            and self._passes_slope_filter(down)
            and closed_through(down, i, close)
            and self._passes_ema_filter(i, "long")
        ):
            self.pending = _Pending(line=down, direction="long", break_bar_index=i)
            self.state = State.WAITING_PULLBACK
            return

        up = current_up_trendline(pivots)
        if (
            up
            and self._passes_slope_filter(up)
            and closed_through(up, i, close)
            and self._passes_ema_filter(i, "short")
        ):
            self.pending = _Pending(line=up, direction="short", break_bar_index=i)
            self.state = State.WAITING_PULLBACK

    # ---------------------------------------------------- pending -> position or idle

    def _manage_pullback(self, i: int, ts: pd.Timestamp, high: float, low: float, close: float) -> None:
        assert self.pending is not None
        line = self.pending.line
        direction = self.pending.direction
        level = line.value_at(i)

        if i - self.pending.break_bar_index > PULLBACK_TIMEOUT_BARS:
            self._reset_to_idle()
            return

        # invalidation: close back through the line in the wrong direction
        if direction == "long" and close < level:
            self._reset_to_idle()
            return
        if direction == "short" and close > level:
            self._reset_to_idle()
            return

        if direction == "long" and low <= level:
            self._open_position("long", i, ts, close)
        elif direction == "short" and high >= level:
            self._open_position("short", i, ts, close)

    def _open_position(self, direction: Direction, i: int, ts: pd.Timestamp, price: float) -> None:
        if self.config.atr_stop_mult is not None and self._atr is not None:
            atr = float(self._atr.iloc[i])
            if np.isnan(atr):
                self._reset_to_idle()
                return
            offset = self.config.atr_stop_mult * atr
            initial_stop = price - offset if direction == "long" else price + offset
        else:
            stop_pivot = self._last_pivot_of("low" if direction == "long" else "high", i)
            initial_stop = stop_pivot.price if stop_pivot else (price - 10.0 if direction == "long" else price + 10.0)

        self.position = _Position(
            direction=direction,
            entry_time=ts,
            entry_price=price,
            entry_bar_index=i,
            initial_stop=initial_stop,
        )
        self.pending = None
        self.state = State.IN_POSITION

    def _reset_to_idle(self) -> None:
        self.pending = None
        self.state = State.IDLE

    # ------------------------------------------------------------------ in-position

    def _manage_position(self, i: int, ts: pd.Timestamp, high: float, low: float, close: float) -> None:
        assert self.position is not None
        pos = self.position

        # (re)build safety line from opposite pivots that occurred after entry
        pivots = self._pivots_up_to(i)
        pivots_after_entry = [p for p in pivots if p.index >= pos.entry_bar_index]
        if pos.direction == "long":
            new_line = current_up_trendline(pivots_after_entry)
        else:
            new_line = current_down_trendline(pivots_after_entry)
        if new_line is not None:
            pos.safety_line = new_line

        if pos.safety_line is not None:
            # safety line: close-through semantic (wicks allowed, matches user spec)
            line_val = pos.safety_line.value_at(i)
            if pos.direction == "long" and close < line_val:
                self._close_position(ts, close, "close_below_safety_line")
            elif pos.direction == "short" and close > line_val:
                self._close_position(ts, close, "close_above_safety_line")
        else:
            # initial stop: HARD stop — fills at stop level the moment price touches it
            stop = pos.initial_stop
            if pos.direction == "long" and low <= stop:
                self._close_position(ts, stop, "initial_swing_stop")
            elif pos.direction == "short" and high >= stop:
                self._close_position(ts, stop, "initial_swing_stop")

    def _close_position(self, ts: pd.Timestamp, price: float, reason: str) -> None:
        assert self.position is not None
        pos = self.position
        self.trades.append(
            Trade(
                direction=pos.direction,
                entry_time=pos.entry_time,
                entry_price=pos.entry_price,
                exit_time=ts,
                exit_price=price,
                exit_reason=reason,
                dollars_per_point=self.config.dollars_per_point,
            )
        )
        self.position = None
        self.state = State.IDLE

    # ------------------------------------------------------------------ driver

    def run(self, close_open_at_end: bool = True) -> list[Trade]:
        """Process all bars. If `close_open_at_end` is True (backtest default), any
        still-open position is force-closed at the final bar with reason "end_of_data".

        In LIVE polling mode pass `close_open_at_end=False` so `self.state` and
        `self.position` reflect the real current open position — otherwise the live
        runner can never see IN_POSITION and never places an order.
        """
        for i in range(len(self.bars)):
            self.process_bar(i)
        if close_open_at_end and self.position is not None:
            last_ts = self.bars.index[-1]
            last_close = float(self.bars.iloc[-1]["close"])
            self._close_position(last_ts, last_close, "end_of_data")
        return self.trades
