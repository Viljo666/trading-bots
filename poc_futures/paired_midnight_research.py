"""Causal MNQ + MES 02:00 ET to midnight-open paired-exit research.

Enter one MNQ and one MES at the 02:00 New York bar open.  Each leg points
toward its own same-calendar-day midnight open.  In the linked version both
legs are closed on the first five-minute bar where either target is touched.

Because only five-minute OHLC bars are available, the non-target leg exits at
that bar's close plus one adverse tick.  This avoids assigning an unknowable
intrabar price.  Entries and time/basket exits also receive one adverse tick;
round-turn friction is $4 per micro contract.
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
DATA_DIR = ROOT / "orb_futures" / "data"
REPORT = Path(__file__).with_name("paired_midnight_results.json")
ET = "America/New_York"
TICK = 0.25
ENTRY_TIME = "02:00"
ROUND_TURN_COST = 4.0
CONTRACTS = {
    "NQ": {"micro": "MNQ", "dollars_per_point": 2.0},
    "ES": {"micro": "MES", "dollars_per_point": 5.0},
}


@dataclass(frozen=True)
class Config:
    cutoff: str = "10:30"
    same_direction_only: bool = False
    max_distance_atr: float = math.inf
    basket_stop: float = math.inf


@dataclass
class Session:
    day: date
    targets: dict[str, float]
    raw_entries: dict[str, float]
    prior_atr: dict[str, float]


@dataclass
class Trade:
    day: date
    pnl: float
    nq_pnl: float
    es_pnl: float
    exit_reason: str
    exit_time: str
    nq_side: int
    es_side: int
    nq_distance_atr: float
    es_distance_atr: float


def clock(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def timestamp(day: date, value: str) -> pd.Timestamp:
    # CME is closed during the Sunday DST transition; NaT makes those
    # nonexistent/duplicated clock readings fail the normal data checks.
    return pd.Timestamp.combine(day, clock(value)).tz_localize(
        ET, nonexistent="NaT", ambiguous="NaT"
    )


def load(symbol: str) -> pd.DataFrame:
    raw = pd.read_csv(DATA_DIR / f"{symbol}_5m.csv")
    raw.index = pd.to_datetime(raw["timestamp"], utc=True).dt.tz_convert(ET)
    bars = raw[["open", "high", "low", "close", "volume"]].astype(float).dropna()
    return bars[~bars.index.duplicated(keep="last")].sort_index()


def prior_rth_atr(bars: pd.DataFrame) -> pd.Series:
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
    return true_range.rolling(14).mean().shift(1)


def prepare(
    bars: dict[str, pd.DataFrame], cutoffs: list[str]
) -> tuple[list[Session], dict[tuple[date, str, str], pd.DataFrame]]:
    by_day = {
        symbol: {day: frame for day, frame in data.groupby(data.index.date)}
        for symbol, data in bars.items()
    }
    atrs = {symbol: prior_rth_atr(data) for symbol, data in bars.items()}
    common_days = sorted(set(by_day["NQ"]) & set(by_day["ES"]))
    sessions: list[Session] = []
    paths: dict[tuple[date, str, str], pd.DataFrame] = {}

    for day in common_days:
        targets: dict[str, float] = {}
        entries: dict[str, float] = {}
        session_atr: dict[str, float] = {}
        valid = True
        for symbol in CONTRACTS:
            frame = by_day[symbol][day]
            midnight = frame[(frame.index.hour == 0) & (frame.index.minute == 0)]
            entry = frame[frame.index == timestamp(day, ENTRY_TIME)]
            atr = float(atrs[symbol].get(day, np.nan))
            if midnight.empty or entry.empty or not np.isfinite(atr) or atr <= 0:
                valid = False
                break
            targets[symbol] = float(midnight.open.iloc[0])
            entries[symbol] = float(entry.open.iloc[0])
            session_atr[symbol] = atr
        if not valid:
            continue

        for symbol in CONTRACTS:
            frame = by_day[symbol][day]
            for cutoff in cutoffs:
                path = frame[
                    (frame.index >= timestamp(day, ENTRY_TIME))
                    & (frame.index < timestamp(day, cutoff))
                ]
                cutoff_row = frame[frame.index == timestamp(day, cutoff)]
                if path.empty:
                    valid = False
                    break
                path = path.copy()
                path.attrs["cutoff_open"] = (
                    float(cutoff_row.open.iloc[0])
                    if not cutoff_row.empty
                    else float(path.close.iloc[-1])
                )
                paths[(day, symbol, cutoff)] = path
            if not valid:
                break
        if valid:
            sessions.append(Session(day, targets, entries, session_atr))
    return sessions, paths


def leg_setup(session: Session, symbol: str) -> dict | None:
    target = session.targets[symbol]
    raw_entry = session.raw_entries[symbol]
    side = 1 if target > raw_entry else -1 if target < raw_entry else 0
    if side == 0:
        return None
    entry = raw_entry + side * TICK
    distance = side * (target - entry)
    if distance <= 0:
        return None
    return {
        "side": side,
        "entry": entry,
        "target": target,
        "distance": distance,
        "distance_atr": distance / session.prior_atr[symbol],
    }


def exit_pnl(symbol: str, setup: dict, raw_exit: float, at_target: bool = False) -> float:
    dpp = CONTRACTS[symbol]["dollars_per_point"]
    exit_price = setup["target"] if at_target else raw_exit - setup["side"] * TICK
    return setup["side"] * (exit_price - setup["entry"]) * dpp - ROUND_TURN_COST


def target_hit(symbol: str, setup: dict, row: pd.Series) -> bool:
    return bool(
        row.high >= setup["target"]
        if setup["side"] == 1
        else row.low <= setup["target"]
    )


def simulate_linked(
    session: Session,
    paths: dict[tuple[date, str, str], pd.DataFrame],
    config: Config,
) -> Trade | None:
    setup = {symbol: leg_setup(session, symbol) for symbol in CONTRACTS}
    if any(value is None for value in setup.values()):
        return None
    nq, es = setup["NQ"], setup["ES"]
    assert nq is not None and es is not None
    if config.same_direction_only and nq["side"] != es["side"]:
        return None
    if max(nq["distance_atr"], es["distance_atr"]) > config.max_distance_atr:
        return None

    nq_path = paths[(session.day, "NQ", config.cutoff)]
    es_path = paths[(session.day, "ES", config.cutoff)]
    common = nq_path.index.intersection(es_path.index)
    if common.empty:
        return None

    pnl = None
    nq_pnl = es_pnl = 0.0
    exit_reason = "cutoff"
    exit_time = timestamp(session.day, config.cutoff).isoformat()
    for stamp in common:
        nq_row, es_row = nq_path.loc[stamp], es_path.loc[stamp]
        nq_hit = target_hit("NQ", nq, nq_row)
        es_hit = target_hit("ES", es, es_row)
        if nq_hit or es_hit:
            nq_pnl = exit_pnl("NQ", nq, float(nq_row.close), nq_hit)
            es_pnl = exit_pnl("ES", es, float(es_row.close), es_hit)
            pnl = nq_pnl + es_pnl
            exit_reason = "both_targets" if nq_hit and es_hit else "nq_target" if nq_hit else "es_target"
            exit_time = stamp.isoformat()
            break

        if math.isfinite(config.basket_stop):
            marked_nq = exit_pnl("NQ", nq, float(nq_row.close))
            marked_es = exit_pnl("ES", es, float(es_row.close))
            if marked_nq + marked_es <= -config.basket_stop:
                nq_pnl, es_pnl = marked_nq, marked_es
                pnl = nq_pnl + es_pnl
                exit_reason = "basket_stop"
                exit_time = stamp.isoformat()
                break

    if pnl is None:
        nq_pnl = exit_pnl("NQ", nq, float(nq_path.attrs["cutoff_open"]))
        es_pnl = exit_pnl("ES", es, float(es_path.attrs["cutoff_open"]))
        pnl = nq_pnl + es_pnl

    return Trade(
        session.day,
        pnl,
        nq_pnl,
        es_pnl,
        exit_reason,
        exit_time,
        nq["side"],
        es["side"],
        nq["distance_atr"],
        es["distance_atr"],
    )


def simulate_single(
    session: Session,
    paths: dict[tuple[date, str, str], pd.DataFrame],
    symbol: str,
    cutoff: str,
) -> float | None:
    setup = leg_setup(session, symbol)
    if setup is None:
        return None
    path = paths[(session.day, symbol, cutoff)]
    for _, row in path.iterrows():
        if target_hit(symbol, setup, row):
            return exit_pnl(symbol, setup, float(row.close), True)
    return exit_pnl(symbol, setup, float(path.attrs["cutoff_open"]))


def simulate_independent_pair(
    session: Session,
    paths: dict[tuple[date, str, str], pd.DataFrame],
    cutoff: str,
) -> float | None:
    legs = [simulate_single(session, paths, symbol, cutoff) for symbol in CONTRACTS]
    return None if any(value is None for value in legs) else float(sum(legs))


def metrics(values: list[float]) -> dict[str, float | int]:
    if not values:
        return {
            "n": 0,
            "win_rate": 0.0,
            "net_dollars": 0.0,
            "avg_dollars": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "worst_trade": 0.0,
        }
    pnl = np.asarray(values, dtype=float)
    gains = pnl[pnl > 0].sum()
    losses = -pnl[pnl <= 0].sum()
    curve = np.r_[0.0, pnl.cumsum()]
    drawdown = curve - np.maximum.accumulate(curve)
    return {
        "n": int(len(pnl)),
        "win_rate": float(100 * np.mean(pnl > 0)),
        "net_dollars": float(pnl.sum()),
        "avg_dollars": float(pnl.mean()),
        "profit_factor": float(gains / losses) if losses else float("inf"),
        "max_drawdown": float(drawdown.min()),
        "worst_trade": float(pnl.min()),
    }


def split_metrics(trades: list[Trade], validation: date, holdout: date) -> dict:
    return {
        "development": metrics([trade.pnl for trade in trades if trade.day < validation]),
        "validation": metrics([trade.pnl for trade in trades if validation <= trade.day < holdout]),
        "holdout": metrics([trade.pnl for trade in trades if trade.day >= holdout]),
        "recent_15": metrics([trade.pnl for trade in trades[-15:]]),
        "combined": metrics([trade.pnl for trade in trades]),
    }


def score(result: dict) -> float:
    dev, val = result["development"], result["validation"]
    if dev["n"] < 100 or val["n"] < 35:
        return -math.inf
    if min(dev["profit_factor"], val["profit_factor"]) <= 1:
        return -math.inf
    return min(dev["avg_dollars"], val["avg_dollars"])


def run() -> dict:
    cutoffs = ["09:25", "10:30", "15:55"]
    bars = {symbol: load(symbol) for symbol in CONTRACTS}
    sessions, paths = prepare(bars, cutoffs)
    validation = sessions[int(len(sessions) * 0.60)].day
    holdout = sessions[int(len(sessions) * 0.80)].day

    exact_config = Config(cutoff="10:30")
    exact_trades = [
        trade
        for session in sessions
        if (trade := simulate_linked(session, paths, exact_config)) is not None
    ]

    comparisons: dict[str, dict] = {}
    for cutoff in cutoffs:
        linked = [
            trade
            for session in sessions
            if (trade := simulate_linked(session, paths, Config(cutoff=cutoff))) is not None
        ]
        comparisons[f"linked_{cutoff}"] = split_metrics(linked, validation, holdout)
        for symbol in CONTRACTS:
            rows = [
                (session.day, simulate_single(session, paths, symbol, cutoff))
                for session in sessions
            ]
            comparisons[f"{CONTRACTS[symbol]['micro']}_alone_{cutoff}"] = {
                "development": metrics([p for d, p in rows if p is not None and d < validation]),
                "validation": metrics([p for d, p in rows if p is not None and validation <= d < holdout]),
                "holdout": metrics([p for d, p in rows if p is not None and d >= holdout]),
                "recent_15": metrics([p for _, p in rows if p is not None][-15:]),
                "combined": metrics([p for _, p in rows if p is not None]),
            }
        independent = [
            (session.day, simulate_independent_pair(session, paths, cutoff))
            for session in sessions
        ]
        comparisons[f"independent_pair_{cutoff}"] = {
            "development": metrics([p for d, p in independent if p is not None and d < validation]),
            "validation": metrics([p for d, p in independent if p is not None and validation <= d < holdout]),
            "holdout": metrics([p for d, p in independent if p is not None and d >= holdout]),
            "recent_15": metrics([p for _, p in independent if p is not None][-15:]),
            "combined": metrics([p for _, p in independent if p is not None]),
        }

    candidates: list[dict] = []
    for cutoff, same_direction, max_atr, basket_stop in itertools.product(
        cutoffs, [False, True], [0.25, 0.50, 0.75, math.inf], [75.0, 100.0, 150.0, 200.0, math.inf]
    ):
        config = Config(cutoff, same_direction, max_atr, basket_stop)
        trades = [
            trade
            for session in sessions
            if (trade := simulate_linked(session, paths, config)) is not None
        ]
        result = split_metrics(trades, validation, holdout)
        candidates.append({"score": score(result), "config": asdict(config), **result})
    ranked = sorted(candidates, key=lambda row: row["score"], reverse=True)

    friday_trade = next((trade for trade in exact_trades if str(trade.day) == "2026-09-11"), None)
    friday = (
        {**asdict(friday_trade), "day": friday_trade.day.isoformat()}
        if friday_trade is not None
        else None
    )
    return {
        "data": {
            "bars_each": {symbol: len(data) for symbol, data in bars.items()},
            "sessions": len(sessions),
            "first": sessions[0].day.isoformat(),
            "last": sessions[-1].day.isoformat(),
            "validation_from": validation.isoformat(),
            "holdout_from": holdout.isoformat(),
        },
        "assumptions": {
            "positions": "1 MNQ + 1 MES",
            "entry": "02:00 New York bar open plus one adverse tick",
            "targets": "each market's same-calendar-day 00:00 New York open",
            "linked_exit": "close both on first 5m bar where either target is touched",
            "other_leg_fill": "target-touch bar close plus one adverse tick",
            "round_turn_cost_per_leg": ROUND_TURN_COST,
        },
        "exact_linked_1030": split_metrics(exact_trades, validation, holdout),
        "comparisons": comparisons,
        "robust_candidates": [row for row in ranked if math.isfinite(row["score"])][:20],
        "last_friday": friday,
        "exact_trades": [
            {**asdict(trade), "day": trade.day.isoformat()} for trade in exact_trades
        ],
    }


def show(label: str, result: dict) -> None:
    print(label)
    for period in ["development", "validation", "holdout", "recent_15", "combined"]:
        row = result[period]
        print(
            f"  {period:11} n={row['n']:3} win={row['win_rate']:5.1f}% "
            f"net=${row['net_dollars']:+8.0f} avg=${row['avg_dollars']:+6.1f} "
            f"PF={row['profit_factor']:.2f} DD=${row['max_drawdown']:7.0f} "
            f"worst=${row['worst_trade']:7.0f}"
        )


def main() -> None:
    report = run()
    REPORT.write_text(json.dumps(report, indent=2, allow_nan=True) + "\n")
    print(json.dumps(report["data"], indent=2))
    show("\nExact linked strategy, exit by 10:30:", report["exact_linked_1030"])
    print("\nComparisons, combined / recent 15:")
    for label, result in report["comparisons"].items():
        if label.endswith("10:30"):
            all_row, recent = result["combined"], result["recent_15"]
            print(
                f"  {label:28} all n={all_row['n']} net=${all_row['net_dollars']:+.0f} "
                f"PF={all_row['profit_factor']:.2f} DD=${all_row['max_drawdown']:.0f} | "
                f"recent net=${recent['net_dollars']:+.0f} PF={recent['profit_factor']:.2f}"
            )
    print("\nRobust candidates selected without holdout:", len(report["robust_candidates"]))
    for row in report["robust_candidates"][:10]:
        print(row["config"], "holdout", row["holdout"])
    print("\nFriday 2026-09-11:", report["last_friday"])
    print("Saved", REPORT)


if __name__ == "__main__":
    main()
