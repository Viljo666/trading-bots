"""Backtest two MNQ from 02:00 ET toward a capped midnight-open target.

Rule under test:
* Enter two MNQ at the 02:00 New York bar open, toward that day's 00:00 open.
* If the midnight-open reward is greater than $200 gross, target $200 gross.
* Otherwise target the midnight open.
* If neither target nor stop is reached, flatten at the selected cutoff.

Fills use one adverse tick at market entries, stops, and time exits.  Target
orders fill at their limit price.  Round-turn friction is $4 per MNQ.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

import numpy as np

from asian_close_midnight_research import (
    ET,
    TICK,
    build_path_cache,
    load_bars,
    metrics as base_metrics,
    prepare,
    timestamp,
)


REPORT = Path(__file__).with_name("mnq2_midnight_capped_results.json")
ENTRY_TIME = "02:00"
CONTRACTS = 2
DOLLARS_PER_POINT = 2.0 * CONTRACTS
ROUND_TURN_COST = 4.0 * CONTRACTS
GROSS_PROFIT_CAP = 200.0
CAP_POINTS = GROSS_PROFIT_CAP / DOLLARS_PER_POINT
CUTOFFS = ["09:25", "10:30", "15:55"]
STOP_CAPS = [100.0, 150.0, 200.0, 250.0, 300.0, math.inf]


@dataclass(frozen=True)
class Config:
    cutoff: str
    stop_cap: float = math.inf


@dataclass
class Trade:
    day: date
    side: int
    entry: float
    midnight_open: float
    target: float
    target_kind: str
    available_gross_profit: float
    planned_loss: float
    pnl: float
    outcome: str


def simulate(day, cache: dict[tuple, dict], config: Config) -> Trade | None:
    raw_entry = day.entry_prices.get(ENTRY_TIME)
    if raw_entry is None or raw_entry == day.midnight_open:
        return None
    side = 1 if day.midnight_open > raw_entry else -1
    entry = raw_entry + side * TICK
    midnight_points = side * (day.midnight_open - entry)
    if midnight_points <= 0:
        return None

    target_points = min(midnight_points, CAP_POINTS)
    target = entry + side * target_points
    target_kind = "200_cap" if midnight_points > CAP_POINTS else "midnight"
    available_gross = midnight_points * DOLLARS_PER_POINT

    if math.isfinite(config.stop_cap):
        risk_points = (config.stop_cap - ROUND_TURN_COST) / DOLLARS_PER_POINT - TICK
        if risk_points <= 0:
            return None
        stop = entry - side * risk_points
        planned_loss = (risk_points + TICK) * DOLLARS_PER_POINT + ROUND_TURN_COST
    else:
        stop = -math.inf if side == 1 else math.inf
        planned_loss = math.inf

    path = cache[(day.day, ENTRY_TIME, config.cutoff)]
    if len(path["high"]) == 0:
        return None

    pnl = None
    outcome = "cutoff"
    for high, low in zip(path["high"], path["low"]):
        stop_hit = low <= stop if side == 1 else high >= stop
        target_hit = high >= target if side == 1 else low <= target
        # Conservative when target and stop are both inside one 5m candle.
        if stop_hit:
            pnl = -planned_loss
            outcome = "stop"
            break
        if target_hit:
            pnl = target_points * DOLLARS_PER_POINT - ROUND_TURN_COST
            outcome = target_kind
            break

    if pnl is None:
        raw_exit = float(path["raw_exit"])
        exit_price = raw_exit - side * TICK
        pnl = side * (exit_price - entry) * DOLLARS_PER_POINT - ROUND_TURN_COST

    return Trade(
        day.day,
        side,
        entry,
        day.midnight_open,
        target,
        target_kind,
        available_gross,
        planned_loss,
        pnl,
        outcome,
    )


def metrics(trades: list[Trade]) -> dict:
    base = base_metrics(
        [
            type("BaseTrade", (), {"pnl": trade.pnl, "outcome": trade.outcome})()
            for trade in trades
        ]
    )
    if not trades:
        return {
            **base,
            "target_rate": 0.0,
            "gross_200_rate": 0.0,
            "gross_200_available_rate": 0.0,
            "average_winner": 0.0,
            "average_loser": 0.0,
            "longest_losing_streak": 0,
        }
    pnl = np.asarray([trade.pnl for trade in trades], dtype=float)
    streak = longest = 0
    for value in pnl:
        streak = streak + 1 if value <= 0 else 0
        longest = max(longest, streak)
    target_outcomes = {"200_cap", "midnight"}
    return {
        **base,
        "target_rate": float(100 * np.mean([trade.outcome in target_outcomes for trade in trades])),
        "gross_200_rate": float(100 * np.mean([trade.outcome == "200_cap" for trade in trades])),
        "gross_200_available_rate": float(
            100 * np.mean([trade.available_gross_profit > GROSS_PROFIT_CAP for trade in trades])
        ),
        "average_winner": float(pnl[pnl > 0].mean()) if np.any(pnl > 0) else 0.0,
        "average_loser": float(pnl[pnl <= 0].mean()) if np.any(pnl <= 0) else 0.0,
        "longest_losing_streak": longest,
    }


def split_metrics(trades: list[Trade], validation: date, holdout: date) -> dict:
    return {
        "development": metrics([trade for trade in trades if trade.day < validation]),
        "validation": metrics(
            [trade for trade in trades if validation <= trade.day < holdout]
        ),
        "holdout": metrics([trade for trade in trades if trade.day >= holdout]),
        "recent_15": metrics(trades[-15:]),
        "combined": metrics(trades),
    }


def run() -> dict:
    bars = load_bars()
    days, by_day = prepare(bars)
    cache = build_path_cache(bars, by_day, days)
    validation = days[int(len(days) * 0.60)].day
    holdout = days[int(len(days) * 0.80)].day

    results: list[dict] = []
    trade_sets: dict[tuple[str, float], list[Trade]] = {}
    for cutoff in CUTOFFS:
        for stop_cap in STOP_CAPS:
            config = Config(cutoff, stop_cap)
            trades = [
                trade
                for day in days
                if (trade := simulate(day, cache, config)) is not None
            ]
            trade_sets[(cutoff, stop_cap)] = trades
            results.append({"config": asdict(config), **split_metrics(trades, validation, holdout)})

    exact = trade_sets[("10:30", math.inf)]
    friday_trade = next((trade for trade in exact if str(trade.day) == "2026-09-11"), None)
    friday = (
        {**asdict(friday_trade), "day": friday_trade.day.isoformat()}
        if friday_trade is not None
        else None
    )
    return {
        "data": {
            "bars": len(bars),
            "sessions": len(days),
            "first": days[0].day.isoformat(),
            "last": days[-1].day.isoformat(),
            "validation_from": validation.isoformat(),
            "holdout_from": holdout.isoformat(),
        },
        "assumptions": {
            "position": "2 MNQ",
            "entry": "02:00 New York open plus one adverse tick",
            "profit_rule": "nearer of same-day midnight open or $200 gross profit",
            "maximum_target_net_after_assumed_costs": GROSS_PROFIT_CAP - ROUND_TURN_COST,
            "round_turn_cost": ROUND_TURN_COST,
            "stop_first_on_ambiguous_bar": True,
        },
        "results": results,
        "last_friday": friday,
        "exact_1030_no_stop_trades": [
            {**asdict(trade), "day": trade.day.isoformat()} for trade in exact
        ],
    }


def main() -> None:
    report = run()
    REPORT.write_text(json.dumps(report, indent=2, allow_nan=True) + "\n")
    print(json.dumps(report["data"], indent=2))
    print("\nTwo MNQ, target min(midnight, $200 gross):")
    for row in report["results"]:
        config = row["config"]
        if config["cutoff"] != "10:30":
            continue
        stop = "none" if math.isinf(config["stop_cap"]) else f"${config['stop_cap']:.0f}"
        print(f"\n  cutoff 10:30 stop {stop}")
        for period in ["development", "validation", "holdout", "recent_15", "combined"]:
            result = row[period]
            print(
                f"    {period:11} n={result['n']:3} win={result['win_rate']:5.1f}% "
                f"target={result['target_rate']:5.1f}% $200={result['gross_200_rate']:5.1f}% "
                f"net=${result['net_dollars']:+8.0f} PF={result['profit_factor']:.2f} "
                f"DD=${result['max_drawdown']:7.0f} worst=${result['worst_trade']:7.0f}"
            )
    print("\nFriday 2026-09-11:", report["last_friday"])
    print("Saved", REPORT)


if __name__ == "__main__":
    main()
