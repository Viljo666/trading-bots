"""Two-MNQ liquidity-sweep reversal toward the New York midnight open.

The 02:00 New York open establishes direction toward the same day's 00:00
open.  The strategy then waits for price to sweep liquidity away from that
target, reclaim the swept level on a five-minute reversal candle, and enters
at the next five-minute open.  Profit is capped at $200 gross; otherwise the
target is the midnight open.

All decisions use completed bars only.  The chronological final 20% is not
used to choose configurations.
"""
from __future__ import annotations

import itertools
import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from asian_close_midnight_research import (
    TICK,
    load_bars,
    prepare,
    timestamp,
)


REPORT = Path(__file__).with_name("mnq2_sweep_midnight_results.json")
ENTRY_START = "02:00"
CONTRACTS = 2
DOLLARS_PER_POINT = 2.0 * CONTRACTS
ROUND_TURN_COST = 4.0 * CONTRACTS
GROSS_PROFIT_CAP = 200.0
CAP_POINTS = GROSS_PROFIT_CAP / DOLLARS_PER_POINT

LIQUIDITY_SOURCES = ["asia", "local_3", "local_6", "local_12"]
ENTRY_ENDS = ["03:00", "05:00", "07:00", "09:25"]
CUTOFFS = ["09:25", "10:30", "15:55"]
STOP_MODES = ["fixed", "signal_capped"]
STOP_CAPS = [100.0, 150.0, 200.0]
SWEEP_TICKS = [0, 1, 2]
SIGNAL_BUFFER_TICKS = 2


@dataclass(frozen=True)
class Config:
    liquidity_source: str
    entry_end: str
    cutoff: str
    stop_mode: str
    stop_cap: float
    sweep_ticks: int


@dataclass
class Setup:
    day: date
    side: int
    signal_time: pd.Timestamp
    entry_time: pd.Timestamp
    raw_entry: float
    midnight_open: float
    signal_high: float
    signal_low: float
    liquidity_level: float


@dataclass
class Trade:
    day: date
    side: int
    signal_time: str
    entry_time: str
    entry: float
    stop: float
    target: float
    target_kind: str
    planned_loss: float
    pnl: float
    outcome: str


def find_setup(
    bars: pd.DataFrame,
    by_day: dict[date, pd.DataFrame],
    day,
    config: Config,
) -> Setup | None:
    raw_two_open = day.entry_prices.get(ENTRY_START)
    if raw_two_open is None or raw_two_open == day.midnight_open:
        return None
    side = 1 if day.midnight_open > raw_two_open else -1
    start = timestamp(day.day, ENTRY_START)
    end = timestamp(day.day, config.entry_end)
    frame = by_day[day.day]
    scan = frame[(frame.index >= start) & (frame.index < end)]
    if len(scan) < 2:
        return None

    asia_start = start.normalize() - pd.Timedelta(hours=4)  # prior day 20:00 ET
    asia = bars[(bars.index >= asia_start) & (bars.index < start)]
    if asia.empty:
        return None
    asia_level = float(asia.low.min()) if side == 1 else float(asia.high.max())
    local_window = (
        int(config.liquidity_source.split("_")[1])
        if config.liquidity_source.startswith("local_")
        else 0
    )

    threshold = config.sweep_ticks * TICK
    for stamp, row in scan.iterrows():
        # If the destination traded before confirmation, this opportunity is
        # complete and cannot be entered retrospectively.
        target_already_hit = (
            float(row.high) >= day.midnight_open
            if side == 1
            else float(row.low) <= day.midnight_open
        )
        if target_already_hit:
            return None

        if local_window:
            previous = frame[frame.index < stamp].tail(local_window)
            if len(previous) < local_window:
                continue
            level = float(previous.low.min()) if side == 1 else float(previous.high.max())
        else:
            level = asia_level

        if side == 1:
            swept = float(row.low) <= level - threshold
            reclaimed = float(row.close) > level
            reversal_body = float(row.close) > float(row.open)
        else:
            swept = float(row.high) >= level + threshold
            reclaimed = float(row.close) < level
            reversal_body = float(row.close) < float(row.open)
        if not (swept and reclaimed and reversal_body):
            continue

        location = frame.index.get_loc(stamp)
        if not isinstance(location, (int, np.integer)) or location + 1 >= len(frame):
            return None
        next_row = frame.iloc[int(location) + 1]
        next_stamp = frame.index[int(location) + 1]
        if next_stamp >= end or next_stamp >= timestamp(day.day, config.cutoff):
            return None
        raw_entry = float(next_row.open)
        # The target must still be ahead in the original 02:00 direction.
        if side * (day.midnight_open - (raw_entry + side * TICK)) <= 0:
            return None
        return Setup(
            day.day,
            side,
            stamp,
            next_stamp,
            raw_entry,
            day.midnight_open,
            float(row.high),
            float(row.low),
            level,
        )
    return None


def simulate(
    by_day: dict[date, pd.DataFrame], setup: Setup, config: Config
) -> Trade | None:
    side = setup.side
    entry = setup.raw_entry + side * TICK
    midnight_points = side * (setup.midnight_open - entry)
    if midnight_points <= 0:
        return None
    target_points = min(midnight_points, CAP_POINTS)
    target = entry + side * target_points
    target_kind = "200_cap" if midnight_points > CAP_POINTS else "midnight"

    fixed_risk_points = (config.stop_cap - ROUND_TURN_COST) / DOLLARS_PER_POINT - TICK
    if fixed_risk_points <= 0:
        return None
    fixed_stop = entry - side * fixed_risk_points
    if config.stop_mode == "fixed":
        stop = fixed_stop
    elif config.stop_mode == "signal_capped":
        logical_stop = (
            setup.signal_low - SIGNAL_BUFFER_TICKS * TICK
            if side == 1
            else setup.signal_high + SIGNAL_BUFFER_TICKS * TICK
        )
        # Use the logical sweep-candle stop unless it exceeds the dollar cap.
        stop = max(logical_stop, fixed_stop) if side == 1 else min(logical_stop, fixed_stop)
    else:
        raise ValueError(config.stop_mode)

    risk_points = side * (entry - stop)
    if risk_points <= 0:
        return None
    planned_loss = (risk_points + TICK) * DOLLARS_PER_POINT + ROUND_TURN_COST
    if planned_loss > config.stop_cap + 1e-9:
        return None

    frame = by_day[setup.day]
    cutoff = timestamp(setup.day, config.cutoff)
    path = frame[(frame.index >= setup.entry_time) & (frame.index < cutoff)]
    if path.empty:
        return None

    pnl = None
    outcome = "cutoff"
    for row in path.itertuples():
        stop_hit = float(row.low) <= stop if side == 1 else float(row.high) >= stop
        target_hit = float(row.high) >= target if side == 1 else float(row.low) <= target
        if stop_hit:
            pnl = -planned_loss
            outcome = "stop"
            break
        if target_hit:
            pnl = target_points * DOLLARS_PER_POINT - ROUND_TURN_COST
            outcome = target_kind
            break

    if pnl is None:
        cutoff_row = frame[frame.index == cutoff]
        raw_exit = (
            float(cutoff_row.open.iloc[0])
            if not cutoff_row.empty
            else float(path.close.iloc[-1])
        )
        exit_price = raw_exit - side * TICK
        pnl = side * (exit_price - entry) * DOLLARS_PER_POINT - ROUND_TURN_COST

    return Trade(
        setup.day,
        side,
        setup.signal_time.isoformat(),
        setup.entry_time.isoformat(),
        entry,
        stop,
        target,
        target_kind,
        planned_loss,
        pnl,
        outcome,
    )


def metrics(trades: list[Trade]) -> dict[str, float | int | str | None]:
    if not trades:
        return {
            "n": 0,
            "win_rate": 0.0,
            "target_rate": 0.0,
            "gross_200_rate": 0.0,
            "net_dollars": 0.0,
            "avg_dollars": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "worst_trade": 0.0,
            "average_winner": 0.0,
            "average_loser": 0.0,
            "longest_losing_streak": 0,
            "first_trade": None,
            "last_trade": None,
        }
    pnl = np.asarray([trade.pnl for trade in trades], dtype=float)
    gains = pnl[pnl > 0].sum()
    losses = -pnl[pnl <= 0].sum()
    curve = np.r_[0.0, pnl.cumsum()]
    drawdown = curve - np.maximum.accumulate(curve)
    streak = longest = 0
    for value in pnl:
        streak = streak + 1 if value <= 0 else 0
        longest = max(longest, streak)
    targets = {"midnight", "200_cap"}
    return {
        "n": len(trades),
        "win_rate": float(100 * np.mean(pnl > 0)),
        "target_rate": float(100 * np.mean([t.outcome in targets for t in trades])),
        "gross_200_rate": float(100 * np.mean([t.outcome == "200_cap" for t in trades])),
        "net_dollars": float(pnl.sum()),
        "avg_dollars": float(pnl.mean()),
        "profit_factor": float(gains / losses) if losses else float("inf"),
        "max_drawdown": float(drawdown.min()),
        "worst_trade": float(pnl.min()),
        "average_winner": float(pnl[pnl > 0].mean()) if np.any(pnl > 0) else 0.0,
        "average_loser": float(pnl[pnl <= 0].mean()) if np.any(pnl <= 0) else 0.0,
        "longest_losing_streak": longest,
        "first_trade": trades[0].day.isoformat(),
        "last_trade": trades[-1].day.isoformat(),
    }


def split_metrics(
    trades: list[Trade], validation: date, holdout: date, recent_from: date
) -> dict:
    return {
        "development": metrics([t for t in trades if t.day < validation]),
        "validation": metrics([t for t in trades if validation <= t.day < holdout]),
        "holdout": metrics([t for t in trades if t.day >= holdout]),
        "recent_15_sessions": metrics([t for t in trades if t.day >= recent_from]),
        "combined": metrics(trades),
    }


def score(result: dict) -> float:
    dev, val = result["development"], result["validation"]
    if dev["n"] < 25 or val["n"] < 8:
        return -math.inf
    if dev["profit_factor"] <= 1 or val["profit_factor"] <= 1:
        return -math.inf
    if dev["avg_dollars"] <= 0 or val["avg_dollars"] <= 0:
        return -math.inf
    return min(dev["avg_dollars"], val["avg_dollars"]) * math.sqrt(
        min(dev["n"], 3 * val["n"])
    )


def run() -> dict:
    bars = load_bars()
    days, by_day = prepare(bars)
    validation = days[int(len(days) * 0.60)].day
    holdout = days[int(len(days) * 0.80)].day
    recent_from = days[-15].day

    # Signal discovery is independent of stop mode/cap/cutoff, so cache it by
    # the parameters that actually affect confirmation.
    setup_cache: dict[tuple, Setup | None] = {}
    results: list[dict] = []
    for source, entry_end, cutoff, stop_mode, stop_cap, sweep_ticks in itertools.product(
        LIQUIDITY_SOURCES,
        ENTRY_ENDS,
        CUTOFFS,
        STOP_MODES,
        STOP_CAPS,
        SWEEP_TICKS,
    ):
        config = Config(source, entry_end, cutoff, stop_mode, stop_cap, sweep_ticks)
        trades: list[Trade] = []
        for day in days:
            key = (day.day, source, entry_end, sweep_ticks)
            if key not in setup_cache:
                setup_cache[key] = find_setup(bars, by_day, day, config)
            setup = setup_cache[key]
            if setup is not None and (trade := simulate(by_day, setup, config)) is not None:
                trades.append(trade)
        split = split_metrics(trades, validation, holdout, recent_from)
        results.append({"score": score(split), "config": asdict(config), **split})

    ranked = sorted(results, key=lambda row: row["score"], reverse=True)
    robust = [row for row in ranked if math.isfinite(row["score"])]
    return {
        "data": {
            "bars": len(bars),
            "sessions": len(days),
            "first": days[0].day.isoformat(),
            "last": days[-1].day.isoformat(),
            "validation_from": validation.isoformat(),
            "holdout_from": holdout.isoformat(),
            "recent_15_sessions_from": recent_from.isoformat(),
            "configurations": len(results),
        },
        "assumptions": {
            "position": "2 MNQ",
            "bias": "02:00 New York open toward same-day midnight open",
            "confirmation": "sweep, reclaim, reversal-body close; enter next 5m open",
            "profit_rule": "nearer of midnight open or $200 gross",
            "maximum_target_net_after_cost": GROSS_PROFIT_CAP - ROUND_TURN_COST,
            "cost": ROUND_TURN_COST,
            "slippage": "one adverse tick on market entry/stop/time exit",
        },
        "robust_candidates": robust[:100],
        "all_results": results,
    }


def main() -> None:
    report = run()
    REPORT.write_text(json.dumps(report, indent=2, allow_nan=True) + "\n")
    print(json.dumps(report["data"], indent=2))
    print("\nCandidates positive in both development and validation:", len(report["robust_candidates"]))
    for row in report["robust_candidates"][:15]:
        c, h, r, a = row["config"], row["holdout"], row["recent_15_sessions"], row["combined"]
        print(
            f"  {c['liquidity_source']:8} to {c['entry_end']} flat {c['cutoff']} "
            f"{c['stop_mode']} ${c['stop_cap']:.0f} sweep {c['sweep_ticks']}t | "
            f"hold n={h['n']} net=${h['net_dollars']:+.0f} PF={h['profit_factor']:.2f} "
            f"DD=${h['max_drawdown']:.0f}; recent n={r['n']} net=${r['net_dollars']:+.0f}; "
            f"all n={a['n']} PF={a['profit_factor']:.2f}"
        )
    print("Saved", REPORT)


if __name__ == "__main__":
    main()
