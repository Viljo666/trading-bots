"""Research a causal NQ opening-range retracement to the midnight open.

The script uses the cached five-minute NQ history and never connects to a
broker.  Signals are formed from completed bars only.  Results include one
MNQ contract, one tick of adverse entry/stop slippage, and a conservative
$4.00 round-turn cost.

The first 60% of sessions are development data.  The final 40% are a
chronological holdout and are not used to rank configurations.
"""
from __future__ import annotations

import itertools
import json
import math
from dataclasses import asdict, dataclass
from datetime import date, time
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "orb_futures" / "data" / "NQ_5m.csv"
REPORT = Path(__file__).with_name("midnight_open_results.json")
ET = "America/New_York"
TICK = 0.25
MNQ_DOLLARS_PER_POINT = 2.0
ROUND_TURN_COST = 4.0
DEVELOPMENT_FRACTION = 0.60


@dataclass(frozen=True)
class Config:
    opening_range_minutes: int = 5
    entry_mode: str = "away_rejection"
    entry_cutoff: str = "10:00"
    stop_mode: str = "signal_bar"
    stop_buffer_points: float = 0.5
    stop_atr: float = 0.10
    min_reward_risk: float = 1.0
    max_distance_atr: float = 0.30
    flatten: str = "11:00"
    max_risk_dollars: float = 200.0


@dataclass
class DayData:
    day: date
    midnight_open: float
    rth_open: float
    side: int
    prior_atr: float
    distance_atr: float
    frame: pd.DataFrame


@dataclass
class Trade:
    day: date
    side: int
    entry: float
    stop: float
    target: float
    risk_points: float
    planned_reward_risk: float
    distance_atr: float
    pnl_dollars: float
    outcome: str


def clock(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def load_bars() -> pd.DataFrame:
    bars = pd.read_csv(DATA)
    bars.index = pd.to_datetime(bars["timestamp"], utc=True).dt.tz_convert(ET)
    return bars[["open", "high", "low", "close", "volume"]].astype(float).dropna()


def prepare_days(bars: pd.DataFrame) -> list[DayData]:

    rth = bars.between_time("09:30", "15:55")
    daily = rth.groupby(rth.index.date).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
    )
    previous_close = daily.close.shift(1)
    true_range = pd.concat(
        [
            daily.high - daily.low,
            (daily.high - previous_close).abs(),
            (daily.low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    # Only information through the previous completed RTH session is used.
    prior_atr = true_range.rolling(14).mean().shift(1)

    days: list[DayData] = []
    for day, full_day in bars.groupby(bars.index.date):
        midnight = full_day[(full_day.index.hour == 0) & (full_day.index.minute == 0)]
        session = full_day.between_time("09:30", "15:55")
        atr = float(prior_atr.get(day, np.nan))
        if midnight.empty or len(session) < 70 or not np.isfinite(atr) or atr <= 0:
            continue
        midnight_open = float(midnight.open.iloc[0])
        rth_open = float(session.open.iloc[0])
        if midnight_open == rth_open:
            continue
        side = 1 if midnight_open > rth_open else -1
        days.append(
            DayData(
                day=day,
                midnight_open=midnight_open,
                rth_open=rth_open,
                side=side,
                prior_atr=atr,
                distance_atr=abs(midnight_open - rth_open) / atr,
                frame=session,
            )
        )
    return days


def load_days() -> list[DayData]:
    return prepare_days(load_bars())


def _target_touched(frame: pd.DataFrame, side: int, target: float) -> bool:
    if frame.empty:
        return False
    return bool(frame.high.max() >= target) if side == 1 else bool(frame.low.min() <= target)


def _entry_index(
    day: DayData,
    opening_range_minutes: int,
    entry_mode: str,
    entry_cutoff: str,
) -> tuple[int, float, float] | None:
    frame = day.frame
    opening_bars = opening_range_minutes // 5
    opening_range = frame.iloc[:opening_bars]
    if len(opening_range) != opening_bars:
        return None
    range_high = float(opening_range.high.max())
    range_low = float(opening_range.low.min())
    rows = list(frame.itertuples())
    cutoff = clock(entry_cutoff)

    if entry_mode == "opening_close":
        candidates = range(opening_bars - 1, opening_bars)
    else:
        candidates = range(opening_bars, len(rows))

    for index in candidates:
        bar = rows[index]
        if bar.Index.time() >= cutoff:
            break
        # A target touched at any time before the completed signal-bar close is
        # no longer available.  This is conservative when intrabar order is
        # unknown.
        if _target_touched(frame.iloc[: index + 1], day.side, day.midnight_open):
            return None

        if entry_mode == "opening_close":
            valid = day.side * (float(bar.close) - float(bar.open)) > 0
        elif entry_mode == "range_break":
            valid = float(bar.close) > range_high if day.side == 1 else float(bar.close) < range_low
        elif entry_mode == "away_rejection":
            valid = (
                float(bar.low) < range_low and float(bar.close) > range_low
                if day.side == 1
                else float(bar.high) > range_high and float(bar.close) < range_high
            )
        elif entry_mode == "open_reclaim":
            valid = float(bar.close) > day.rth_open if day.side == 1 else float(bar.close) < day.rth_open
        else:
            raise ValueError(f"Unknown entry mode: {entry_mode}")
        if valid:
            return index, range_high, range_low
    return None


def simulate(day: DayData, event: tuple[int, float, float], config: Config) -> Trade | None:
    entry_index, range_high, range_low = event
    rows = list(day.frame.itertuples())
    signal_bar = rows[entry_index]
    entry = float(signal_bar.close) + day.side * TICK

    if day.side * (day.midnight_open - entry) <= 0:
        return None
    if config.stop_mode == "signal_bar":
        stop = (
            float(signal_bar.low) - config.stop_buffer_points
            if day.side == 1
            else float(signal_bar.high) + config.stop_buffer_points
        )
    elif config.stop_mode == "opening_range":
        stop = (
            range_low - config.stop_buffer_points
            if day.side == 1
            else range_high + config.stop_buffer_points
        )
    elif config.stop_mode == "session_extreme":
        formed = day.frame.iloc[: entry_index + 1]
        stop = (
            float(formed.low.min()) - config.stop_buffer_points
            if day.side == 1
            else float(formed.high.max()) + config.stop_buffer_points
        )
    elif config.stop_mode == "atr":
        stop = entry - day.side * config.stop_atr * day.prior_atr
    else:
        raise ValueError(f"Unknown stop mode: {config.stop_mode}")

    risk_points = day.side * (entry - stop)
    if risk_points <= 0:
        return None
    planned_loss = (risk_points + TICK) * MNQ_DOLLARS_PER_POINT + ROUND_TURN_COST
    if planned_loss > config.max_risk_dollars:
        return None
    reward_points = day.side * (day.midnight_open - entry)
    reward_risk = reward_points / risk_points
    flatten = clock(config.flatten)
    pnl: float | None = None
    outcome = "session"
    for bar in rows[entry_index + 1 :]:
        if bar.Index.time() >= flatten:
            break
        stop_hit = float(bar.low) <= stop if day.side == 1 else float(bar.high) >= stop
        target_hit = (
            float(bar.high) >= day.midnight_open
            if day.side == 1
            else float(bar.low) <= day.midnight_open
        )
        # Stop-first is conservative if one later five-minute bar hits both.
        if stop_hit:
            pnl = -(risk_points + TICK) * MNQ_DOLLARS_PER_POINT - ROUND_TURN_COST
            outcome = "stop"
            break
        if target_hit:
            pnl = reward_points * MNQ_DOLLARS_PER_POINT - ROUND_TURN_COST
            outcome = "target"
            break

    if pnl is None:
        eligible = day.frame[day.frame.index.time < flatten]
        if eligible.empty:
            return None
        exit_price = float(eligible.close.iloc[-1]) - day.side * TICK
        pnl = day.side * (exit_price - entry) * MNQ_DOLLARS_PER_POINT - ROUND_TURN_COST

    return Trade(
        day=day.day,
        side=day.side,
        entry=entry,
        stop=stop,
        target=day.midnight_open,
        risk_points=risk_points,
        planned_reward_risk=reward_risk,
        distance_atr=day.distance_atr,
        pnl_dollars=pnl,
        outcome=outcome,
    )


def metrics(trades: list[Trade]) -> dict[str, float | int]:
    if not trades:
        return {
            "n": 0,
            "win": 0.0,
            "total_dollars": 0.0,
            "avg_dollars": 0.0,
            "pf": 0.0,
            "max_dd_dollars": 0.0,
        }
    values = np.asarray([trade.pnl_dollars for trade in trades], dtype=float)
    gains = values[values > 0].sum()
    losses = -values[values <= 0].sum()
    curve = np.r_[0.0, values.cumsum()]
    drawdown = curve - np.maximum.accumulate(curve)
    return {
        "n": int(len(values)),
        "win": float(100 * np.mean(values > 0)),
        "total_dollars": float(values.sum()),
        "avg_dollars": float(values.mean()),
        "pf": float(gains / losses) if losses > 0 else float("inf"),
        "max_dd_dollars": float(drawdown.min()),
    }


def rank_score(result: dict[str, float | int]) -> float:
    n = int(result["n"])
    if n < 15:
        return -math.inf
    avg = float(result["avg_dollars"])
    pf = min(float(result["pf"]), 4.0)
    drawdown = max(250.0, abs(float(result["max_dd_dollars"])))
    return avg * math.sqrt(n) * pf * 500.0 / drawdown


def search(days: list[DayData]) -> list[dict]:
    split_index = int(len(days) * DEVELOPMENT_FRACTION)
    split_day = days[split_index].day
    results: list[dict] = []

    stop_variants = [
        ("signal_bar", 0.5, 0.10),
        ("signal_bar", 1.0, 0.10),
        ("opening_range", 0.5, 0.10),
        ("session_extreme", 0.5, 0.10),
        ("atr", 0.5, 0.10),
        ("atr", 0.5, 0.15),
        ("atr", 0.5, 0.20),
    ]
    for opening_range, entry_mode, cutoff in itertools.product(
        [5, 15, 30],
        ["opening_close", "range_break", "away_rejection", "open_reclaim"],
        ["10:00", "10:30", "11:00"],
    ):
        events = [
            (day, event)
            for day in days
            if (
                event := _entry_index(
                    day,
                    opening_range_minutes=opening_range,
                    entry_mode=entry_mode,
                    entry_cutoff=cutoff,
                )
            )
            is not None
        ]
        for stop_variant, flatten in itertools.product(stop_variants, ["11:00", "12:00", "15:55"]):
            if clock(flatten) <= clock(cutoff):
                continue
            stop_mode, stop_buffer, stop_atr = stop_variant
            base_config = Config(
                opening_range_minutes=opening_range,
                entry_mode=entry_mode,
                entry_cutoff=cutoff,
                stop_mode=stop_mode,
                stop_buffer_points=stop_buffer,
                stop_atr=stop_atr,
                min_reward_risk=0.0,
                max_distance_atr=math.inf,
                flatten=flatten,
            )
            raw_trades = [
                trade
                for day, event in events
                if (trade := simulate(day, event, base_config)) is not None
            ]
            for min_rr, max_distance in itertools.product(
                [0.25, 0.50, 0.75, 1.0, 1.5],
                [0.10, 0.20, 0.30, 0.50, 1.00],
            ):
                config = Config(
                    **{
                        **asdict(base_config),
                        "min_reward_risk": min_rr,
                        "max_distance_atr": max_distance,
                    }
                )
                trades = [
                    trade
                    for trade in raw_trades
                    if trade.planned_reward_risk >= min_rr and trade.distance_atr <= max_distance
                ]
                development = [trade for trade in trades if trade.day < split_day]
                holdout = [trade for trade in trades if trade.day >= split_day]
                development_metrics = metrics(development)
                score = rank_score(development_metrics)
                if np.isfinite(score):
                    results.append(
                        {
                            "score": score,
                            "config": asdict(config),
                            "development": development_metrics,
                            "holdout": metrics(holdout),
                            "combined": metrics(trades),
                        }
                    )
    return sorted(results, key=lambda item: item["score"], reverse=True)


def main() -> None:
    days = load_days()
    results = search(days)
    payload = {
        "data": {
            "sessions": len(days),
            "first": str(days[0].day),
            "last": str(days[-1].day),
            "development_fraction": DEVELOPMENT_FRACTION,
        },
        "top": results[:100],
    }
    REPORT.write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload["data"], indent=2))
    for result in results[:20]:
        print(json.dumps(result, default=str))
    print(f"Saved {REPORT}")


if __name__ == "__main__":
    main()
