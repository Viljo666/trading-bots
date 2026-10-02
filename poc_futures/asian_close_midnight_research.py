"""Causal NQ/MNQ research: enter after Asia, target the New York midnight open.

There is no exchange-defined "Asian close" for CME equity futures, so the
research compares several fixed New York times.  Each trade uses one MNQ,
one tick of adverse entry/stop/exit slippage, and a $4 round-turn cost.

The first 60% of sessions are development, the next 20% validation, and the
last 20% a final chronological holdout that is never used for ranking.
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
REPORT = Path(__file__).with_name("asian_close_midnight_results.json")
ET = "America/New_York"
TICK = 0.25
MNQ_DPP = 2.0
ROUND_TURN_COST = 4.0

ENTRY_TIMES = ["00:05", "01:00", "02:00", "03:00", "04:00", "05:00"]
FLATTEN_TIMES = ["09:25", "10:30", "15:55"]
RISK_CAPS = [50.0, 75.0, 100.0, 125.0, 150.0, 200.0]
MIN_TARGET_POINTS = [0.0, 5.0, 10.0, 20.0]
MAX_DISTANCE_ATR = [0.25, 0.50, 1.00, math.inf]


@dataclass(frozen=True)
class Config:
    entry_time: str
    flatten_time: str
    stop_mode: str
    risk_cap: float
    stop_atr: float = 0.0
    stop_buffer: float = 0.5
    min_target_points: float = 0.0
    max_distance_atr: float = math.inf


@dataclass
class Day:
    day: date
    midnight_open: float
    prior_rth_atr: float
    entry_prices: dict[str, float]


@dataclass
class Trade:
    day: date
    side: int
    entry: float
    stop: float
    target: float
    target_distance: float
    distance_atr: float
    planned_loss: float
    pnl: float
    outcome: str


def clock(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def timestamp(day: date, value: str) -> pd.Timestamp:
    return pd.Timestamp.combine(day, clock(value)).tz_localize(ET)


def load_bars() -> pd.DataFrame:
    raw = pd.read_csv(DATA)
    raw.index = pd.to_datetime(raw["timestamp"], utc=True).dt.tz_convert(ET)
    bars = raw[["open", "high", "low", "close", "volume"]].astype(float).dropna()
    return bars[~bars.index.duplicated(keep="last")].sort_index()


def prepare(bars: pd.DataFrame) -> tuple[list[Day], dict[date, pd.DataFrame]]:
    by_day = {day: frame for day, frame in bars.groupby(bars.index.date)}
    rth = bars.between_time("09:30", "15:55")
    daily = rth.groupby(rth.index.date).agg(
        high=("high", "max"), low=("low", "min"), close=("close", "last")
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
    prior_atr = true_range.rolling(14).mean().shift(1)

    days: list[Day] = []
    for day, frame in by_day.items():
        midnight = frame[(frame.index.hour == 0) & (frame.index.minute == 0)]
        atr = float(prior_atr.get(day, np.nan))
        if midnight.empty or not np.isfinite(atr) or atr <= 0:
            continue
        prices: dict[str, float] = {}
        for value in ENTRY_TIMES:
            row = frame[frame.index == timestamp(day, value)]
            if not row.empty:
                prices[value] = float(row.open.iloc[0])
        if prices:
            days.append(Day(day, float(midnight.open.iloc[0]), atr, prices))
    return days, by_day


def build_path_cache(
    bars: pd.DataFrame, by_day: dict[date, pd.DataFrame], days: list[Day]
) -> dict[tuple, dict]:
    """Build each reusable signal/path slice once for the full grid search."""
    cache: dict[tuple, dict] = {}
    for day in days:
        frame = by_day[day.day]
        for entry_time in day.entry_prices:
            start = timestamp(day.day, entry_time)
            previous = bars[bars.index < start].tail(1)
            asia_start = start.normalize() - pd.Timedelta(hours=4)
            formed = bars[(bars.index >= asia_start) & (bars.index < start)]
            cache[(day.day, entry_time)] = {
                "previous_low": float(previous.low.iloc[0]) if not previous.empty else np.nan,
                "previous_high": float(previous.high.iloc[0]) if not previous.empty else np.nan,
                "asia_low": float(formed.low.min()) if not formed.empty else np.nan,
                "asia_high": float(formed.high.max()) if not formed.empty else np.nan,
            }
            for flatten_time in FLATTEN_TIMES:
                flatten = timestamp(day.day, flatten_time)
                future = frame[(frame.index >= start) & (frame.index < flatten)]
                flatten_row = frame[frame.index == flatten]
                raw_exit = (
                    float(flatten_row.open.iloc[0])
                    if not flatten_row.empty
                    else float(future.close.iloc[-1]) if not future.empty else np.nan
                )
                cache[(day.day, entry_time, flatten_time)] = {
                    "high": future.high.to_numpy(dtype=float),
                    "low": future.low.to_numpy(dtype=float),
                    "times": future.index,
                    "raw_exit": raw_exit,
                }
    return cache


def stop_price(
    cache: dict[tuple, dict],
    day: Day,
    config: Config,
    entry: float,
    side: int,
) -> float | None:
    signal = cache[(day.day, config.entry_time)]
    if config.stop_mode == "fixed":
        risk_points = (config.risk_cap - ROUND_TURN_COST) / MNQ_DPP - TICK
        return entry - side * risk_points if risk_points > 0 else None
    if config.stop_mode == "signal_bar":
        if not np.isfinite(signal["previous_low"]):
            return None
        return (
            signal["previous_low"] - config.stop_buffer
            if side == 1
            else signal["previous_high"] + config.stop_buffer
        )
    if config.stop_mode == "asia_extreme":
        if not np.isfinite(signal["asia_low"]):
            return None
        return (
            signal["asia_low"] - config.stop_buffer
            if side == 1
            else signal["asia_high"] + config.stop_buffer
        )
    if config.stop_mode == "atr":
        return entry - side * config.stop_atr * day.prior_rth_atr
    raise ValueError(config.stop_mode)


def simulate(
    cache: dict[tuple, dict],
    day: Day,
    config: Config,
) -> Trade | None:
    raw_entry = day.entry_prices.get(config.entry_time)
    if raw_entry is None:
        return None
    target = day.midnight_open
    raw_side = 1 if target > raw_entry else -1 if target < raw_entry else 0
    if raw_side == 0:
        return None
    entry = raw_entry + raw_side * TICK
    target_distance = raw_side * (target - entry)
    distance_atr = target_distance / day.prior_rth_atr
    if target_distance <= 0 or target_distance < config.min_target_points:
        return None
    if distance_atr > config.max_distance_atr:
        return None

    stop = stop_price(cache, day, config, entry, raw_side)
    if stop is None:
        return None
    risk_points = raw_side * (entry - stop)
    if risk_points <= 0:
        return None
    planned_loss = (risk_points + TICK) * MNQ_DPP + ROUND_TURN_COST
    if planned_loss > config.risk_cap + 1e-9:
        return None

    path = cache[(day.day, config.entry_time, config.flatten_time)]
    if len(path["high"]) == 0:
        return None

    pnl: float | None = None
    outcome = "time"
    for high, low in zip(path["high"], path["low"]):
        stop_hit = low <= stop if raw_side == 1 else high >= stop
        target_hit = high >= target if raw_side == 1 else low <= target
        # Conservative when both levels occur inside one five-minute candle.
        if stop_hit:
            pnl = -planned_loss
            outcome = "stop"
            break
        if target_hit:
            pnl = target_distance * MNQ_DPP - ROUND_TURN_COST
            outcome = "target"
            break

    if pnl is None:
        raw_exit = path["raw_exit"]
        exit_price = raw_exit - raw_side * TICK
        pnl = raw_side * (exit_price - entry) * MNQ_DPP - ROUND_TURN_COST

    return Trade(
        day=day.day,
        side=raw_side,
        entry=entry,
        stop=stop,
        target=target,
        target_distance=target_distance,
        distance_atr=distance_atr,
        planned_loss=planned_loss,
        pnl=pnl,
        outcome=outcome,
    )


def metrics(trades: list[Trade]) -> dict[str, float | int]:
    if not trades:
        return {
            "n": 0,
            "win_rate": 0.0,
            "target_rate": 0.0,
            "net_dollars": 0.0,
            "avg_dollars": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "worst_trade": 0.0,
        }
    pnl = np.asarray([trade.pnl for trade in trades], dtype=float)
    gains = pnl[pnl > 0].sum()
    losses = -pnl[pnl <= 0].sum()
    curve = np.r_[0.0, pnl.cumsum()]
    drawdown = curve - np.maximum.accumulate(curve)
    return {
        "n": int(len(trades)),
        "win_rate": float(100.0 * np.mean(pnl > 0)),
        "target_rate": float(100.0 * np.mean([t.outcome == "target" for t in trades])),
        "net_dollars": float(pnl.sum()),
        "avg_dollars": float(pnl.mean()),
        "profit_factor": float(gains / losses) if losses > 0 else float("inf"),
        "max_drawdown": float(drawdown.min()),
        "worst_trade": float(pnl.min()),
    }


def unprotected_trade(
    cache: dict[tuple, dict], day: Day, entry_time: str, flatten_time: str
) -> dict | None:
    raw_entry = day.entry_prices.get(entry_time)
    if raw_entry is None or raw_entry == day.midnight_open:
        return None
    side = 1 if day.midnight_open > raw_entry else -1
    entry = raw_entry + side * TICK
    reward = side * (day.midnight_open - entry)
    if reward <= 0:
        return None
    path = cache[(day.day, entry_time, flatten_time)]
    if len(path["high"]) == 0:
        return None
    target_hits = path["high"] >= day.midnight_open if side == 1 else path["low"] <= day.midnight_open
    hit = bool(np.any(target_hits))
    first_hit = int(np.flatnonzero(target_hits)[0]) if hit else len(target_hits) - 1
    hit_time = path["times"][first_hit].isoformat() if hit else None
    # Once the target is touched, the trade is finished.  Measuring adverse
    # movement after that point would falsely exaggerate the required stop.
    observed_low = path["low"][: first_hit + 1]
    observed_high = path["high"][: first_hit + 1]
    adverse = entry - float(observed_low.min()) if side == 1 else float(observed_high.max()) - entry
    if hit:
        pnl = reward * MNQ_DPP - ROUND_TURN_COST
    else:
        raw_exit = path["raw_exit"]
        pnl = side * ((raw_exit - side * TICK) - entry) * MNQ_DPP - ROUND_TURN_COST
    return {
        "day": day.day.isoformat(),
        "side": "long" if side == 1 else "short",
        "entry": entry,
        "target": day.midnight_open,
        "distance_points": reward,
        "hit": hit,
        "hit_time": hit_time,
        "max_adverse_points": max(0.0, adverse),
        "pnl_dollars": pnl,
    }


def score(result: dict[str, float | int], minimum_trades: int) -> float:
    n = int(result["n"])
    avg = float(result["avg_dollars"])
    pf = min(float(result["profit_factor"]), 3.0)
    drawdown = max(100.0, abs(float(result["max_drawdown"])))
    if n < minimum_trades or avg <= 0 or pf <= 1.0:
        return -math.inf
    return avg * math.sqrt(n) * pf / drawdown


def run() -> dict:
    bars = load_bars()
    days, by_day = prepare(bars)
    cache = build_path_cache(bars, by_day, days)
    first_validation = days[int(len(days) * 0.60)].day
    first_holdout = days[int(len(days) * 0.80)].day

    raw: list[dict] = []
    raw_rows: dict[tuple[str, str], list[dict]] = {}
    for entry_time, flatten_time in itertools.product(ENTRY_TIMES, FLATTEN_TIMES):
        rows = [
            trade
            for day in days
            if (trade := unprotected_trade(cache, day, entry_time, flatten_time)) is not None
        ]
        raw_rows[(entry_time, flatten_time)] = rows
        pnl = np.asarray([row["pnl_dollars"] for row in rows], dtype=float)
        curve = np.r_[0.0, pnl.cumsum()]
        dd = curve - np.maximum.accumulate(curve)
        raw.append(
            {
                "entry_time": entry_time,
                "flatten_time": flatten_time,
                "n": len(rows),
                "touch_rate": 100.0 * np.mean([row["hit"] for row in rows]),
                "net_dollars": float(pnl.sum()),
                "avg_dollars": float(pnl.mean()),
                "max_drawdown": float(dd.min()),
                "worst_trade": float(pnl.min()),
                "median_target_points": float(np.median([row["distance_points"] for row in rows])),
                "median_max_adverse_points": float(np.median([row["max_adverse_points"] for row in rows])),
            }
        )

    stop_variants = (
        [("fixed", cap, 0.0) for cap in RISK_CAPS]
        + [("signal_bar", cap, 0.0) for cap in [100.0, 150.0, 200.0]]
        + [("asia_extreme", cap, 0.0) for cap in [100.0, 150.0, 200.0]]
        + [("atr", cap, atr) for cap in [100.0, 150.0, 200.0] for atr in [0.10, 0.15, 0.20]]
    )

    candidates: list[dict] = []
    for entry_time, flatten_time, stop_variant, min_points, max_atr in itertools.product(
        ENTRY_TIMES, FLATTEN_TIMES, stop_variants, MIN_TARGET_POINTS, MAX_DISTANCE_ATR
    ):
        stop_mode, risk_cap, stop_atr = stop_variant
        config = Config(
            entry_time=entry_time,
            flatten_time=flatten_time,
            stop_mode=stop_mode,
            risk_cap=risk_cap,
            stop_atr=stop_atr,
            min_target_points=min_points,
            max_distance_atr=max_atr,
        )
        trades = [
            trade for day in days if (trade := simulate(cache, day, config)) is not None
        ]
        development = [trade for trade in trades if trade.day < first_validation]
        validation = [trade for trade in trades if first_validation <= trade.day < first_holdout]
        holdout = [trade for trade in trades if trade.day >= first_holdout]
        dev_metrics = metrics(development)
        validation_metrics = metrics(validation)
        # Require a meaningful sample in both ranking periods.  The final 20%
        # remains untouched regardless of score.
        candidate_score = min(score(dev_metrics, 60), score(validation_metrics, 20))
        candidates.append(
            {
                "score": candidate_score,
                "config": asdict(config),
                "development": dev_metrics,
                "validation": validation_metrics,
                "holdout": metrics(holdout),
                "combined": metrics(trades),
            }
        )

    ranked = sorted(candidates, key=lambda row: row["score"], reverse=True)
    finalists = [row for row in ranked if math.isfinite(row["score"])][:100]

    # Primary simple versions: always trade each candidate time with a fixed,
    # Lucid-sized $100 stop and no fitted distance filter.
    primary: list[dict] = []
    for entry_time, flatten_time in itertools.product(ENTRY_TIMES, FLATTEN_TIMES):
        config = Config(entry_time, flatten_time, "fixed", 100.0)
        trades = [
            trade for day in days if (trade := simulate(cache, day, config)) is not None
        ]
        primary.append(
            {
                "config": asdict(config),
                "development": metrics([t for t in trades if t.day < first_validation]),
                "validation": metrics([t for t in trades if first_validation <= t.day < first_holdout]),
                "holdout": metrics([t for t in trades if t.day >= first_holdout]),
                "combined": metrics(trades),
            }
        )

    friday = "2026-09-11"
    friday_rows = [
        {"entry_time": entry, "flatten_time": flatten, **row}
        for (entry, flatten), rows in raw_rows.items()
        for row in rows
        if row["day"] == friday
    ]
    return {
        "data": {
            "bars": len(bars),
            "sessions": len(days),
            "first": days[0].day.isoformat(),
            "last": days[-1].day.isoformat(),
            "validation_from": first_validation.isoformat(),
            "holdout_from": first_holdout.isoformat(),
        },
        "assumptions": {
            "instrument": "1 MNQ",
            "entry": "market at selected ET bar open plus one adverse tick",
            "target": "same-calendar-day 00:00 New York open",
            "cost": ROUND_TURN_COST,
            "stop_first_on_ambiguous_bar": True,
        },
        "unprotected": raw,
        "primary_fixed_100": primary,
        "top_ranked": finalists,
        "last_friday": friday_rows,
    }


def main() -> None:
    report = run()
    REPORT.write_text(json.dumps(report, indent=2, allow_nan=True) + "\n")
    print(json.dumps(report["data"], indent=2))
    print("\nUnprotected touch rate and tail:")
    for row in report["unprotected"]:
        print(
            f"  {row['entry_time']} -> {row['flatten_time']}: n={row['n']} "
            f"touch={row['touch_rate']:.1f}% avg=${row['avg_dollars']:+.2f} "
            f"DD=${row['max_drawdown']:.0f} worst=${row['worst_trade']:.0f}"
        )
    print("\nSimple $100-stop versions (final holdout):")
    for row in report["primary_fixed_100"]:
        result = row["holdout"]
        print(
            f"  {row['config']['entry_time']} -> {row['config']['flatten_time']}: "
            f"n={result['n']} win={result['win_rate']:.1f}% "
            f"avg=${result['avg_dollars']:+.2f} PF={result['profit_factor']:.2f} "
            f"DD=${result['max_drawdown']:.0f}"
        )
    print("\nTop robust configurations (final holdout shown, not ranked):")
    for row in report["top_ranked"][:10]:
        config, result = row["config"], row["holdout"]
        print(
            f"  {config['entry_time']} {config['stop_mode']} cap${config['risk_cap']:.0f} "
            f"min{config['min_target_points']:.0f}pt maxATR{config['max_distance_atr']} "
            f"flat{config['flatten_time']} | n={result['n']} win={result['win_rate']:.1f}% "
            f"avg=${result['avg_dollars']:+.2f} PF={result['profit_factor']:.2f} "
            f"DD=${result['max_drawdown']:.0f}"
        )
    print("\nFriday 2026-09-11:")
    for row in report["last_friday"]:
        print(
            f"  {row['entry_time']} -> {row['flatten_time']} {row['side']} "
            f"entry={row['entry']:.2f} target={row['target']:.2f} "
            f"hit={row['hit']} MAE={row['max_adverse_points']:.2f}pt "
            f"pnl=${row['pnl_dollars']:+.2f}"
        )
    print(f"\nSaved {REPORT}")


if __name__ == "__main__":
    main()
