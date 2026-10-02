"""Research-only ES test of the top-down POC/liquidity framework.

This is a separate experiment.  It does not import the archived Tori runner,
does not connect to IBKR, and cannot place orders.

Fixed v1 rules (written before looking at the results)
-------------------------------------------------------
* Bias: the latest *completed* monthly, weekly, and daily candles must all
  agree (close above open for long, below open for short).
* Trend change: a completed 4-hour candle closes through the latest confirmed
  4-hour swing high (long) or swing low (short).  A swing is only known after
  two later 4-hour candles, so the test has no look-ahead.
* Entry: the first bullish/bearish completed 5-minute or 15-minute candle,
  within 12 hours of that 4-hour break, must close on the correct side of the
  latest available prior-RTH POC.  One trade per 4-hour break.
* Stop: one tick beyond the last confirmed swing on the entry timeframe.
* Target: nearest qualifying upside/downside liquidity level: previous RTH
  high/low, overnight high/low, or recently confirmed equal high/low.  If no
  such level exists, use 2R.  A known level closer than 1.5R rejects the trade.
* Exit: stop, target, or the 15:55 ET close of that futures trading day.

The test applies one adverse tick at entry and exit plus $4.62 round-turn
commission.  When one 5-minute bar reaches both stop and target, it records
the stop first.  Results are split chronologically: first 60% development,
last 40% untouched holdout.

Usage:
    ../.venv/bin/python topdown_poc_liquidity_research.py

The input is the existing cached five-minute ES file from ``orb_futures``.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
MARKETS = {
    "ES": {
        "path": ROOT / "orb_futures" / "data" / "ES_5m.csv",
        "dollars_per_point": 50.0,
        "poc_bin_size": 1.0,
    },
    "NQ": {
        "path": ROOT / "orb_futures" / "data" / "NQ_5m.csv",
        "dollars_per_point": 20.0,
        "poc_bin_size": 4.0,
    },
}
ET = "America/New_York"
TICK = 0.25
ROUND_TURN_COMMISSION = 4.62
ENTRY_TIMEFRAMES = ("5min", "15min")
PIVOT_LEFT = 2
PIVOT_RIGHT = 2
EVENT_MAX_AGE = pd.Timedelta(hours=12)
MIN_TARGET_R = 1.5
FALLBACK_TARGET_R = 2.0
EQUAL_LEVEL_TOLERANCE = 2 * TICK
DEVELOPMENT_FRACTION = 0.60

Side = Literal["long", "short"]


@dataclass(frozen=True)
class CompletedDirection:
    available_at: pd.Timestamp
    direction: Side


@dataclass(frozen=True)
class Pivot:
    price: float
    kind: Literal["high", "low"]
    confirmed_at: pd.Timestamp


@dataclass(frozen=True)
class StructureEvent:
    available_at: pd.Timestamp
    direction: Side
    broken_level: float


@dataclass(frozen=True)
class RthLevels:
    available_at: pd.Timestamp
    poc: float
    high: float
    low: float


@dataclass
class Trade:
    timeframe: str
    event_time: pd.Timestamp
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    side: Side
    poc: float
    entry: float
    stop: float
    target: float
    target_source: str
    exit: float
    reason: str
    risk_points: float
    pnl_points: float
    pnl_usd: float
    r_multiple: float


def load_bars(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"Missing ES history: {path}")
    df = pd.read_csv(path)
    index = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(ET)
    df = df.set_index(index)[["open", "high", "low", "close", "volume"]]
    df = df.astype(float).dropna().sort_index()
    return df[~df.index.duplicated(keep="last")]


def _resample_ohlcv(bars: pd.DataFrame, rule: str) -> pd.DataFrame:
    return (
        bars.resample(rule, label="left", closed="left", origin="start_day")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna()
    )


def completed_directions(bars: pd.DataFrame, frequency: str) -> list[CompletedDirection]:
    """Return directions only when a calendar period has actually completed."""
    local_naive = bars.index.tz_localize(None)
    if frequency == "month":
        keys = local_naive.to_period("M")
    elif frequency == "week":
        keys = local_naive.to_period("W-FRI")
    elif frequency == "day":
        keys = local_naive.to_period("D")
    else:
        raise ValueError(f"Unsupported bias frequency: {frequency}")

    out: list[CompletedDirection] = []
    for _, period in bars.groupby(keys):
        open_ = float(period["open"].iloc[0])
        close = float(period["close"].iloc[-1])
        if close == open_:
            continue
        # Timestamps mark the start of a five-minute bar; the candle becomes
        # usable only after its final bar has closed.
        out.append(
            CompletedDirection(
                available_at=period.index[-1] + pd.Timedelta(minutes=5),
                direction="long" if close > open_ else "short",
            )
        )
    return out


def direction_at(records: list[CompletedDirection], when: pd.Timestamp) -> Side | None:
    times = [r.available_at for r in records]
    i = bisect_right(times, when) - 1
    return records[i].direction if i >= 0 else None


def confirmed_pivots(frame: pd.DataFrame, bar_minutes: int) -> list[Pivot]:
    """Pivots are emitted only at the time the right-side confirmation exists."""
    highs = frame["high"].to_numpy()
    lows = frame["low"].to_numpy()
    out: list[Pivot] = []
    for i in range(PIVOT_LEFT, len(frame) - PIVOT_RIGHT):
        hi_window = highs[i - PIVOT_LEFT : i + PIVOT_RIGHT + 1]
        lo_window = lows[i - PIVOT_LEFT : i + PIVOT_RIGHT + 1]
        available_at = frame.index[i + PIVOT_RIGHT] + pd.Timedelta(minutes=bar_minutes)
        if highs[i] == hi_window.max() and int((hi_window == highs[i]).sum()) == 1:
            out.append(Pivot(float(highs[i]), "high", available_at))
        elif lows[i] == lo_window.min() and int((lo_window == lows[i]).sum()) == 1:
            out.append(Pivot(float(lows[i]), "low", available_at))
    return out


def four_hour_structure_events(bars: pd.DataFrame) -> list[StructureEvent]:
    """Create causal 4-hour market-structure-break events."""
    four_hour = _resample_ohlcv(bars, "4h")
    highs = four_hour["high"].to_numpy()
    lows = four_hour["low"].to_numpy()
    closes = four_hour["close"].to_numpy()
    last_high: float | None = None
    last_low: float | None = None
    events: list[StructureEvent] = []

    for i in range(len(four_hour)):
        # At bar i, only the potential pivot i-right has enough later completed
        # bars to be known.  This deliberately delays every pivot confirmation.
        pivot_i = i - PIVOT_RIGHT
        if pivot_i >= PIVOT_LEFT:
            hi_window = highs[pivot_i - PIVOT_LEFT : pivot_i + PIVOT_RIGHT + 1]
            lo_window = lows[pivot_i - PIVOT_LEFT : pivot_i + PIVOT_RIGHT + 1]
            if highs[pivot_i] == hi_window.max() and int((hi_window == highs[pivot_i]).sum()) == 1:
                last_high = float(highs[pivot_i])
            elif lows[pivot_i] == lo_window.min() and int((lo_window == lows[pivot_i]).sum()) == 1:
                last_low = float(lows[pivot_i])

        if i == 0:
            continue
        close = float(closes[i])
        previous_close = float(closes[i - 1])
        available_at = four_hour.index[i] + pd.Timedelta(hours=4)
        if last_high is not None and close > last_high and previous_close <= last_high:
            events.append(StructureEvent(available_at, "long", last_high))
        elif last_low is not None and close < last_low and previous_close >= last_low:
            events.append(StructureEvent(available_at, "short", last_low))
    return events


def session_poc(rth: pd.DataFrame, bin_size: float = 1.0) -> float:
    """Simple 1-point volume profile used only after that RTH session completes."""
    lower = np.floor(rth["low"].min() / bin_size) * bin_size
    upper = np.ceil(rth["high"].max() / bin_size) * bin_size
    edges = np.arange(lower, upper + bin_size, bin_size)
    if len(edges) < 2:
        return float(rth["close"].iloc[-1])
    volume = np.zeros(len(edges) - 1)
    for high, low, bar_volume in zip(rth.high, rth.low, rth.volume):
        first = max(0, int((low - lower) // bin_size))
        last = min(len(volume) - 1, int((high - lower) // bin_size))
        volume[first : last + 1] += bar_volume / (last - first + 1)
    return float(edges[:-1][int(volume.argmax())] + bin_size / 2)


def rth_levels(bars: pd.DataFrame, bin_size: float) -> list[RthLevels]:
    rth = bars[(bars.index.weekday < 5)].between_time("09:30", "15:55")
    out: list[RthLevels] = []
    for _, session in rth.groupby(rth.index.date):
        if len(session) < 70:  # skip shortened / incomplete sessions
            continue
        out.append(
            RthLevels(
                available_at=session.index[-1] + pd.Timedelta(minutes=5),
                poc=session_poc(session, bin_size),
                high=float(session.high.max()),
                low=float(session.low.min()),
            )
        )
    return out


def latest_rth_level(levels: list[RthLevels], when: pd.Timestamp) -> RthLevels | None:
    times = [x.available_at for x in levels]
    i = bisect_right(times, when) - 1
    return levels[i] if i >= 0 else None


def _overnight_start(when: pd.Timestamp) -> pd.Timestamp:
    day = when.normalize()
    if when.time() >= pd.Timestamp("18:00").time():
        return day + pd.Timedelta(hours=18)
    return day - pd.Timedelta(days=1) + pd.Timedelta(hours=18)


def overnight_levels_before(bars: pd.DataFrame, known_before: pd.Timestamp) -> tuple[float, float] | None:
    """The overnight high/low known before the signal candle started."""
    start = _overnight_start(known_before)
    end = min(known_before.normalize() + pd.Timedelta(hours=9, minutes=30), known_before)
    overnight = bars[(bars.index >= start) & (bars.index < end)]
    if overnight.empty:
        return None
    return float(overnight.high.max()), float(overnight.low.min())


def equal_levels_before(pivots: list[Pivot], known_before: pd.Timestamp) -> tuple[float | None, float | None]:
    """Use the newest confirmed pair of near-equal highs and lows, if present."""
    known = [p for p in pivots if p.confirmed_at <= known_before]
    values: dict[str, float | None] = {"high": None, "low": None}
    for kind in ("high", "low"):
        same_kind = [p for p in known if p.kind == kind][-8:]
        for newer_i in range(len(same_kind) - 1, 0, -1):
            newer = same_kind[newer_i]
            for older in reversed(same_kind[:newer_i]):
                if abs(newer.price - older.price) <= EQUAL_LEVEL_TOLERANCE:
                    values[kind] = (newer.price + older.price) / 2
                    break
            if values[kind] is not None:
                break
    return values["high"], values["low"]


def futures_trading_date(when: pd.Timestamp) -> pd.Timestamp:
    """18:00 ET begins the next futures trading day."""
    day = when.normalize()
    if when.time() >= pd.Timestamp("18:00").time():
        day += pd.Timedelta(days=1)
    while day.weekday() >= 5:
        day += pd.Timedelta(days=1)
    return day


def session_deadline(when: pd.Timestamp) -> pd.Timestamp:
    return futures_trading_date(when) + pd.Timedelta(hours=15, minutes=55)


def target_for(
    side: Side,
    entry: float,
    risk: float,
    rth: RthLevels,
    overnight: tuple[float, float] | None,
    equal_high: float | None,
    equal_low: float | None,
) -> tuple[float, str] | None:
    candidates: list[tuple[float, str]] = []
    if side == "long":
        candidates.append((rth.high, "prior_rth_high"))
        if overnight is not None:
            candidates.append((overnight[0], "overnight_high"))
        if equal_high is not None:
            candidates.append((equal_high, "equal_high"))
        candidates = [(price, name) for price, name in candidates if price > entry]
        candidates.sort(key=lambda x: x[0])
        if not candidates:
            return entry + FALLBACK_TARGET_R * risk, "2R_fallback"
        target, source = candidates[0]
        return (target, source) if (target - entry) / risk >= MIN_TARGET_R else None

    candidates.append((rth.low, "prior_rth_low"))
    if overnight is not None:
        candidates.append((overnight[1], "overnight_low"))
    if equal_low is not None:
        candidates.append((equal_low, "equal_low"))
    candidates = [(price, name) for price, name in candidates if price < entry]
    candidates.sort(key=lambda x: x[0], reverse=True)
    if not candidates:
        return entry - FALLBACK_TARGET_R * risk, "2R_fallback"
    target, source = candidates[0]
    return (target, source) if (entry - target) / risk >= MIN_TARGET_R else None


def exit_trade(
    raw: pd.DataFrame,
    entry_time: pd.Timestamp,
    side: Side,
    entry: float,
    stop_trigger: float,
    target_trigger: float,
) -> tuple[pd.Timestamp, float, str]:
    """Simulate from the next completed 5-minute bar, pessimistic on collisions."""
    deadline = session_deadline(entry_time)
    future = raw[(raw.index >= entry_time) & (raw.index <= deadline)]
    for timestamp, bar in future.iterrows():
        high, low = float(bar.high), float(bar.low)
        stop_hit = low <= stop_trigger if side == "long" else high >= stop_trigger
        target_hit = high >= target_trigger if side == "long" else low <= target_trigger
        if stop_hit:  # deliberately pessimistic if both boundaries occur in one bar
            fill = stop_trigger - TICK if side == "long" else stop_trigger + TICK
            return timestamp + pd.Timedelta(minutes=5), fill, "stop"
        if target_hit:
            fill = target_trigger - TICK if side == "long" else target_trigger + TICK
            return timestamp + pd.Timedelta(minutes=5), fill, "target"
        if timestamp >= deadline:
            fill = float(bar.close) - TICK if side == "long" else float(bar.close) + TICK
            return timestamp + pd.Timedelta(minutes=5), fill, "session_close"
    # A partial final day cannot provide a proper forced exit; do not fabricate one.
    raise RuntimeError("No session-close bar available for an otherwise valid trade")


def run_timeframe(
    raw: pd.DataFrame,
    timeframe: str,
    biases: dict[str, list[CompletedDirection]],
    events: list[StructureEvent],
    rth: list[RthLevels],
    dollars_per_point: float,
) -> list[Trade]:
    minutes = 5 if timeframe == "5min" else 15
    frame = raw if timeframe == "5min" else _resample_ohlcv(raw, timeframe)
    pivots = confirmed_pivots(frame, minutes)
    event_times = [event.available_at for event in events]
    used_events: set[pd.Timestamp] = set()
    trades: list[Trade] = []

    for timestamp, bar in frame.iterrows():
        decision_time = timestamp + pd.Timedelta(minutes=minutes)
        event_i = bisect_right(event_times, timestamp) - 1
        if event_i < 0:
            continue
        event = events[event_i]
        if event.available_at in used_events or timestamp - event.available_at > EVENT_MAX_AGE:
            continue

        side = event.direction
        if not all(direction_at(biases[name], decision_time) == side for name in ("month", "week", "day")):
            continue
        level = latest_rth_level(rth, decision_time)
        if level is None:
            continue
        close, open_ = float(bar.close), float(bar.open)
        if side == "long" and not (close > open_ and close > level.poc):
            continue
        if side == "short" and not (close < open_ and close < level.poc):
            continue

        # The swing is confirmed before this signal candle starts.  It is the
        # operational, automatable meaning of the "last safe/protected level".
        candidates = [p for p in pivots if p.confirmed_at <= timestamp]
        stop_kind = "low" if side == "long" else "high"
        safe = next((p for p in reversed(candidates) if p.kind == stop_kind), None)
        if safe is None:
            continue
        entry = close + TICK if side == "long" else close - TICK
        stop_trigger = safe.price - TICK if side == "long" else safe.price + TICK
        stop_fill = stop_trigger - TICK if side == "long" else stop_trigger + TICK
        risk = (entry - stop_fill) if side == "long" else (stop_fill - entry)
        if risk <= 0:
            continue

        overnight = overnight_levels_before(raw, timestamp)
        equal_high, equal_low = equal_levels_before(pivots, timestamp)
        target_selection = target_for(side, entry, risk, level, overnight, equal_high, equal_low)
        if target_selection is None:
            # A nearer visible liquidity level leaves less than 1.5R.  It is a
            # skipped setup, not an excuse to pretend a farther 2R level was first.
            used_events.add(event.available_at)
            continue
        target_trigger, target_source = target_selection
        try:
            exit_time, exit_price, reason = exit_trade(
                raw, decision_time, side, entry, stop_trigger, target_trigger
            )
        except RuntimeError:
            continue

        gross = exit_price - entry if side == "long" else entry - exit_price
        pnl = gross - ROUND_TURN_COMMISSION / dollars_per_point
        trades.append(
            Trade(
                timeframe=timeframe,
                event_time=event.available_at,
                entry_time=decision_time,
                exit_time=exit_time,
                side=side,
                poc=level.poc,
                entry=entry,
                stop=stop_fill,
                target=target_trigger,
                target_source=target_source,
                exit=exit_price,
                reason=reason,
                risk_points=risk,
                pnl_points=pnl,
                pnl_usd=pnl * dollars_per_point,
                r_multiple=pnl / risk,
            )
        )
        used_events.add(event.available_at)
    return trades


def summarize(label: str, trades: list[Trade], dollars_per_point: float) -> dict[str, object]:
    if not trades:
        return {"period": label, "trades": 0}
    data = pd.DataFrame(asdict(t) for t in trades)
    pnl = data.pnl_points
    wins = pnl[pnl > 0]
    losses = pnl[pnl <= 0]
    equity = pnl.cumsum()
    drawdown = equity - equity.cummax()
    gross_loss = abs(float(losses.sum()))
    return {
        "period": label,
        "trades": len(data),
        "win%": round(float((pnl > 0).mean() * 100), 1),
        "net_pts": round(float(pnl.sum()), 2),
        "net_$": round(float(data.pnl_usd.sum()), 0),
        "avg_R": round(float(data.r_multiple.mean()), 3),
        "PF": round(float(wins.sum()) / gross_loss, 2) if gross_loss else float("inf"),
        "max_DD_$": round(float(drawdown.min() * dollars_per_point), 0),
        "target%": round(float((data.reason == "target").mean() * 100), 1),
        "stop%": round(float((data.reason == "stop").mean() * 100), 1),
    }


def development_holdout(trades: list[Trade]) -> tuple[list[Trade], list[Trade]]:
    dates = sorted({t.entry_time.date() for t in trades})
    if len(dates) < 2:
        return trades, []
    cut = dates[max(1, int(len(dates) * DEVELOPMENT_FRACTION)) - 1]
    return (
        [t for t in trades if t.entry_time.date() <= cut],
        [t for t in trades if t.entry_time.date() > cut],
    )


def in_new_york(trade: Trade) -> bool:
    return pd.Timestamp("09:30").time() <= trade.entry_time.time() <= pd.Timestamp("15:00").time()


def verdict(holdout: dict[str, object]) -> str:
    if (
        int(holdout.get("trades", 0)) >= 30
        and float(holdout.get("PF", 0)) >= 1.15
        and float(holdout.get("avg_R", 0)) > 0
    ):
        return "PROMISING FOR A LIMITED DRY-RUN ONLY — it still needs forward testing."
    return "DO NOT PROMOTE — this fixed version did not clear the research gate."


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", choices=sorted(MARKETS), default="ES")
    args = parser.parse_args()
    market = MARKETS[args.symbol]
    raw = load_bars(market["path"])
    biases = {name: completed_directions(raw, name) for name in ("month", "week", "day")}
    events = four_hour_structure_events(raw)
    rth = rth_levels(raw, float(market["poc_bin_size"]))

    print(f"=== Top-down 4H structure + prior POC + liquidity research ({args.symbol}) ===")
    print(f"Data: {raw.index[0]} to {raw.index[-1]} ({len(raw):,} five-minute bars)")
    print("Fixed v1: completed M/W/D candle bias; 4H swing-break; POC-side candle close;")
    print("safe-swing stop; nearest liquidity target / 2R fallback; $4.62 RT + adverse ticks.")
    print("No live bot, broker, or active configuration is used or changed.\n")

    for timeframe in ENTRY_TIMEFRAMES:
        trades = run_timeframe(raw, timeframe, biases, events, rth, float(market["dollars_per_point"]))
        development, holdout = development_holdout(trades)
        ny_trades = [t for t in trades if in_new_york(t)]
        report = pd.DataFrame(
            [
                summarize("all", trades, float(market["dollars_per_point"])),
                summarize("development (first 60%)", development, float(market["dollars_per_point"])),
                summarize("holdout (last 40%)", holdout, float(market["dollars_per_point"])),
                summarize("NY entries only", ny_trades, float(market["dollars_per_point"])),
            ]
        )
        print(f"--- {timeframe} entry-close version ---")
        print(report.to_string(index=False))
        print(f"Verdict: {verdict(summarize('holdout', holdout, float(market['dollars_per_point'])))}\n")


if __name__ == "__main__":
    main()
