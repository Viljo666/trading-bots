"""Broad, causal combination search for the NQ prior-POC setup.

Research only.  The search is deliberately hierarchical:

1. Entry and context combinations are screened on three chronological blocks.
2. Only stable entries reach the stop/target/breakeven/liquidity sweep.
3. Finalists are checked on a later cached holdout and, with ``--fresh``, the
   most recent IBKR data that was not used by the main search.

This avoids spending the statistical holdout while blindly optimizing every
exit around already-unstable entries.  Every feature is causal at decision
time; no future candle is used to form an entry or target.
"""
from __future__ import annotations

import argparse
import heapq
import itertools
import json
import math
import random
import sys
from dataclasses import asdict, dataclass, replace
from datetime import date, time
from pathlib import Path

import numpy as np
import pandas as pd

import nq_research as base


ROOT = Path(__file__).resolve().parent.parent
REPORT = Path(__file__).resolve().parent / "nq_combination_results.json"
TICK = 0.25
COMMISSION = 4.62
DOLLARS_PER_POINT = 20.0

P1_END = date(2025, 10, 1)
P2_END = date(2026, 1, 1)
P3_END = date(2026, 4, 1)
RECENT_START = date(2026, 6, 22)


@dataclass(frozen=True)
class EntrySpec:
    mode: str = "close_retest"       # close_retest | touch
    confirmation_minutes: int = 5    # 5 | 15
    margin_atr: float = 0.0
    volume_ratio: float = 0.0
    retest_timeout_min: int = 999
    cutoff: str = "15:00"


@dataclass(frozen=True)
class FilterSpec:
    min_distance_atr: float = 0.0
    max_distance_atr: float = math.inf
    opening: str = "any"             # any | aligned
    direction: str = "both"          # both | long | short
    previous_day: str = "any"        # any | aligned | opposite
    overnight: str = "any"           # any | aligned | opposite
    atr_regime: str = "any"          # any | high | low


@dataclass(frozen=True)
class ExitSpec:
    stop_atr: float = 0.25
    reward_risk: float = 3.0
    breakeven_r: float | None = None
    target_mode: str = "fixed"       # fixed | liquidity_1.0 | liquidity_1.5
    flatten: str = "15:55"
    same_bar_stop: bool = False
    slippage_ticks: float = 1.0
    commission: float = COMMISSION


@dataclass
class DayData:
    day: date
    side: int
    poc: float
    atr: float
    distance_atr: float
    opening_aligned: bool
    previous_aligned: bool
    overnight_aligned: bool
    atr_high: bool
    previous_high: float
    previous_low: float
    overnight_high: float
    overnight_low: float
    frame: pd.DataFrame


@dataclass(frozen=True)
class EntryEvent:
    day_data: DayData
    entry_i: int
    confirmation_i: int | None


@dataclass(frozen=True)
class Candidate:
    entry: EntrySpec
    filters: FilterSpec
    exit: ExitSpec
    score: float


def _clock(value: str) -> time:
    hour, minute = value.split(":")
    return time(int(hour), int(minute))


def _period(day: date) -> str:
    if day < P1_END:
        return "P1"
    if day < P2_END:
        return "P2"
    if day < P3_END:
        return "P3"
    return "P4"


def metrics(values: list[float] | np.ndarray) -> dict[str, float | int]:
    r = np.asarray(values, dtype=float)
    r = r[np.isfinite(r)]
    if len(r) == 0:
        return {"n": 0, "win": 0.0, "total_r": 0.0, "avg_r": 0.0, "pf": 0.0, "max_dd": 0.0}
    gains = r[r > 0].sum()
    losses = -r[r <= 0].sum()
    curve = np.r_[0.0, np.cumsum(r)]
    drawdown = curve - np.maximum.accumulate(curve)
    return {
        "n": int(len(r)),
        "win": float(100 * np.mean(r > 0)),
        "total_r": float(r.sum()),
        "avg_r": float(r.mean()),
        "pf": float(gains / losses) if losses > 0 else float("inf"),
        "max_dd": float(drawdown.min()),
    }


def prepare_days(bars: pd.DataFrame) -> list[DayData]:
    profile = bars.between_time("09:30", "15:55")
    atr = base.daily_atr(profile)
    atr_median = atr.rolling(50, min_periods=20).median()
    volume_median = profile.volume.rolling(20, min_periods=10).median().shift(1)
    sessions = [(day, frame) for day, frame in profile.groupby(profile.index.date) if len(frame) >= 70]
    days: list[DayData] = []

    for i in range(1, len(sessions)):
        previous_day, previous = sessions[i - 1]
        day, session = sessions[i]
        day_atr = float(atr.get(previous_day, np.nan))
        if not np.isfinite(day_atr) or day_atr <= 0:
            continue
        poc = base.session_poc(previous)
        day_open = float(session.open.iloc[0])
        if poc == day_open:
            continue
        side = 1 if poc > day_open else -1
        opening = session.iloc[0]
        opening_aligned = bool((opening.close - opening.open) * side > 0)
        previous_aligned = bool((previous.close.iloc[-1] - previous.open.iloc[0]) * side > 0)
        previous_close = float(previous.close.iloc[-1])
        overnight_aligned = bool((day_open - previous_close) * side > 0)
        overnight = bars[(bars.index > previous.index[-1]) & (bars.index < session.index[0])]
        if overnight.empty:
            overnight_high = previous_close
            overnight_low = previous_close
        else:
            overnight_high = float(overnight.high.max())
            overnight_low = float(overnight.low.min())

        trade_frame = session.between_time("09:35", "15:50").copy()
        trade_frame["volume_ratio"] = trade_frame.volume / volume_median.reindex(trade_frame.index)
        median_atr = float(atr_median.get(previous_day, np.nan))
        days.append(
            DayData(
                day=day,
                side=side,
                poc=poc,
                atr=day_atr,
                distance_atr=abs(poc - day_open) / day_atr,
                opening_aligned=opening_aligned,
                previous_aligned=previous_aligned,
                overnight_aligned=overnight_aligned,
                atr_high=bool(np.isfinite(median_atr) and day_atr > median_atr),
                previous_high=float(previous.high.max()),
                previous_low=float(previous.low.min()),
                overnight_high=overnight_high,
                overnight_low=overnight_low,
                frame=trade_frame,
            )
        )
    return days


def entry_event(day: DayData, spec: EntrySpec) -> EntryEvent | None:
    rows = list(day.frame.itertuples())
    cutoff = _clock(spec.cutoff)
    if spec.mode == "touch":
        for i, bar in enumerate(rows):
            if bar.Index.time() >= cutoff:
                return None
            touched = bar.high >= day.poc if day.side == 1 else bar.low <= day.poc
            if touched:
                return EntryEvent(day, i, None)
        return None

    confirmation_i: int | None = None
    confirmation_time: pd.Timestamp | None = None
    for i, bar in enumerate(rows):
        if bar.Index.time() >= cutoff:
            return None
        if confirmation_i is None:
            bar_close = bar.Index + pd.Timedelta(minutes=5)
            complete = spec.confirmation_minutes == 5 or (
                spec.confirmation_minutes == 15
                and bar_close.minute % 15 == 0
                and bar_close.time() > time(9, 45)
            )
            if not complete:
                continue
            displacement = day.side * (float(bar.close) - day.poc)
            volume_ok = spec.volume_ratio <= 0 or (
                np.isfinite(bar.volume_ratio) and float(bar.volume_ratio) >= spec.volume_ratio
            )
            if displacement > spec.margin_atr * day.atr and volume_ok:
                confirmation_i = i
                confirmation_time = bar_close
            continue

        assert confirmation_time is not None
        if (bar.Index - confirmation_time).total_seconds() / 60 > spec.retest_timeout_min:
            return None
        retested = bar.low <= day.poc if day.side == 1 else bar.high >= day.poc
        if retested:
            return EntryEvent(day, i, confirmation_i)
    return None


def build_events(days: list[DayData], spec: EntrySpec) -> list[EntryEvent]:
    return [event for day in days if (event := entry_event(day, spec)) is not None]


def filter_event(event: EntryEvent, spec: FilterSpec) -> bool:
    day = event.day_data
    if not spec.min_distance_atr <= day.distance_atr <= spec.max_distance_atr:
        return False
    if spec.opening == "aligned" and not day.opening_aligned:
        return False
    if spec.direction == "long" and day.side != 1:
        return False
    if spec.direction == "short" and day.side != -1:
        return False
    if spec.previous_day == "aligned" and not day.previous_aligned:
        return False
    if spec.previous_day == "opposite" and day.previous_aligned:
        return False
    if spec.overnight == "aligned" and not day.overnight_aligned:
        return False
    if spec.overnight == "opposite" and day.overnight_aligned:
        return False
    if spec.atr_regime == "high" and not day.atr_high:
        return False
    if spec.atr_regime == "low" and day.atr_high:
        return False
    return True


def _target(event: EntryEvent, spec: ExitSpec, risk: float) -> float:
    day = event.day_data
    fixed = day.poc + day.side * spec.reward_risk * risk
    if spec.target_mode == "fixed":
        return fixed
    minimum_r = 1.0 if spec.target_mode == "liquidity_1.0" else 1.5
    candidates = [day.previous_high, day.overnight_high] if day.side == 1 else [day.previous_low, day.overnight_low]
    pre_entry = day.frame.iloc[: event.entry_i + 1]
    remaining: list[float] = []
    for level in candidates:
        distance_r = day.side * (level - day.poc) / risk
        not_swept = float(pre_entry.high.max()) < level if day.side == 1 else float(pre_entry.low.min()) > level
        if distance_r >= minimum_r and not_swept:
            remaining.append(level)
    if not remaining:
        return fixed
    return min(remaining) if day.side == 1 else max(remaining)


def exit_r(event: EntryEvent, spec: ExitSpec) -> float | None:
    day = event.day_data
    rows = list(day.frame.itertuples())
    flatten = _clock(spec.flatten)
    if rows[event.entry_i].Index.time() >= flatten:
        return None
    risk = spec.stop_atr * day.atr
    stop = day.poc - day.side * risk
    target = _target(event, spec, risk)
    target_r = day.side * (target - day.poc) / risk
    slip = spec.slippage_ticks * TICK
    cost_points = spec.commission / DOLLARS_PER_POINT
    be_armed = False

    def net_r(exit_price: float, adverse_slip: bool) -> float:
        fill = exit_price - day.side * slip if adverse_slip else exit_price
        points = day.side * (fill - day.poc) - cost_points
        return points / risk

    entry_bar = rows[event.entry_i]
    entry_stop = entry_bar.low <= stop if day.side == 1 else entry_bar.high >= stop
    if spec.same_bar_stop and entry_stop:
        return net_r(stop, True)

    final_close: float | None = None
    for bar in rows[event.entry_i + 1 :]:
        if bar.Index.time() >= flatten:
            break
        final_close = float(bar.close)
        active_stop = day.poc if be_armed else stop
        stop_hit = bar.low <= active_stop if day.side == 1 else bar.high >= active_stop
        target_hit = bar.high >= target if day.side == 1 else bar.low <= target
        if stop_hit:
            return net_r(active_stop, True)
        if target_hit:
            return net_r(target, False)
        if spec.breakeven_r is not None and not be_armed:
            trigger = day.poc + day.side * spec.breakeven_r * risk
            reached = bar.high >= trigger if day.side == 1 else bar.low <= trigger
            if reached:
                be_armed = True

    eligible = day.frame[day.frame.index.time < flatten]
    if final_close is None:
        if eligible.empty:
            return None
        final_close = float(eligible.close.iloc[-1])
    return net_r(final_close, True)


def config_complexity(entry: EntrySpec, filters: FilterSpec, exit_spec: ExitSpec) -> int:
    count = 0
    count += entry.confirmation_minutes != 5
    count += entry.margin_atr > 0
    count += entry.volume_ratio > 0
    count += entry.retest_timeout_min < 999
    count += entry.cutoff != "15:00"
    count += filters.min_distance_atr > 0
    count += math.isfinite(filters.max_distance_atr)
    count += filters.opening != "any"
    count += filters.direction != "both"
    count += filters.previous_day != "any"
    count += filters.overnight != "any"
    count += filters.atr_regime != "any"
    count += exit_spec.stop_atr != 0.25
    count += exit_spec.reward_risk != 3.0
    count += exit_spec.breakeven_r is not None
    count += exit_spec.target_mode != "fixed"
    count += exit_spec.flatten != "15:55"
    return int(count)


def development_score(records: list[tuple[date, float]], complexity: int) -> tuple[float, dict] | None:
    periods = {name: metrics([r for day, r in records if _period(day) == name]) for name in ("P1", "P2", "P3")}
    combined = metrics([r for day, r in records if _period(day) in {"P1", "P2", "P3"}])
    if combined["n"] < 35 or any(periods[name]["n"] < 8 for name in periods):
        return None
    averages = np.array([periods[name]["avg_r"] for name in periods])
    if combined["avg_r"] <= 0 or combined["pf"] < 1.05 or int((averages > 0).sum()) < 2:
        return None
    score = float(np.min(averages) + 0.35 * np.median(averages) + 0.25 * combined["avg_r"] - 0.0125 * complexity)
    return score, {"combined": combined, **periods}


def push_top(heap: list, size: int, score: float, serial: int, payload) -> None:
    item = (score, serial, payload)
    if len(heap) < size:
        heapq.heappush(heap, item)
    elif score > heap[0][0]:
        heapq.heapreplace(heap, item)


def entry_specs() -> list[EntrySpec]:
    specs = [EntrySpec(mode="touch", cutoff=cutoff) for cutoff in ("12:30", "14:00", "15:00")]
    specs.extend(
        EntrySpec("close_retest", minutes, margin, volume, timeout, cutoff)
        for minutes, margin, volume, timeout, cutoff in itertools.product(
            (5, 15), (0.0, 0.025, 0.05), (0.0, 1.0, 1.25), (30, 60, 120, 999), ("12:30", "14:00", "15:00")
        )
        if minutes == 5 or volume == 0.0
    )
    return specs


def filter_specs() -> list[FilterSpec]:
    return [
        FilterSpec(*values)
        for values in itertools.product(
            (0.0, 0.03, 0.05, 0.075, 0.10),
            (math.inf, 0.50, 0.75, 1.0),
            ("any", "aligned"),
            ("both", "long", "short"),
            ("any", "aligned", "opposite"),
            ("any", "aligned", "opposite"),
            ("any", "high", "low"),
        )
    ]


def exit_specs() -> list[ExitSpec]:
    return [
        ExitSpec(*values)
        for values in itertools.product(
            (0.20, 0.25, 0.30, 0.40, 0.50),
            (2.0, 2.5, 3.0, 3.5, 4.0),
            (None, 1.0, 1.5, 2.0),
            ("fixed", "liquidity_1.0", "liquidity_1.5"),
            ("14:00", "15:00", "15:55"),
        )
    ]


def candidate_records(events: list[EntryEvent], filters: FilterSpec, exit_spec: ExitSpec) -> list[tuple[date, float]]:
    records = []
    for event in events:
        if not filter_event(event, filters):
            continue
        result = exit_r(event, exit_spec)
        if result is not None:
            records.append((event.day_data.day, result))
    return records


def fetch_recent(cached: pd.DataFrame) -> pd.DataFrame:
    sys.path.insert(0, str(ROOT / "orb_futures"))
    from ib_insync import IB
    import ibkr_data

    ib = IB()
    ib.connect("127.0.0.1", 4002, clientId=random.randint(50001, 60000), timeout=15)
    try:
        fresh = ibkr_data.fetch_symbol(ib, "NQ", "CME", "5m", 1)
    finally:
        ib.disconnect()
    fresh = fresh[["open", "high", "low", "close", "volume"]].astype(float).dropna()
    tail = fresh[fresh.index > cached.index.max()]
    return pd.concat([cached, tail]).sort_index().loc[lambda frame: ~frame.index.duplicated(keep="last")]


def serialize_candidate(candidate: Candidate, period_stats: dict, stress: dict) -> dict:
    return {
        "entry": asdict(candidate.entry),
        "filters": asdict(candidate.filters),
        "exit": asdict(candidate.exit),
        "development_score": candidate.score,
        "periods": period_stats,
        "stress": stress,
        "complexity": config_complexity(candidate.entry, candidate.filters, candidate.exit),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fresh", action="store_true", help="append current IBKR NQ history for final forward check")
    args = parser.parse_args()

    cached = base.load()
    days = prepare_days(cached)
    entries = entry_specs()
    filters = filter_specs()
    baseline_exit = ExitSpec()
    print(f"Prepared {len(days)} causal sessions; {len(entries)} entry specs × {len(filters)} context filters")

    event_cache: dict[EntrySpec, list[EntryEvent]] = {}
    context_heap: list = []
    serial = 0
    evaluated = passed = 0
    for entry_i, entry in enumerate(entries, 1):
        events = build_events(days, entry)
        event_cache[entry] = events
        baseline_results = {id(event): exit_r(event, baseline_exit) for event in events}
        for filter_spec in filters:
            evaluated += 1
            records = [
                (event.day_data.day, result)
                for event in events
                if filter_event(event, filter_spec)
                and (result := baseline_results[id(event)]) is not None
            ]
            scored = development_score(records, config_complexity(entry, filter_spec, baseline_exit))
            if scored is None:
                continue
            passed += 1
            score, stats = scored
            serial += 1
            push_top(context_heap, 40, score, serial, (entry, filter_spec, stats))
        if entry_i % 25 == 0 or entry_i == len(entries):
            print(f"  entry/context progress {entry_i}/{len(entries)}; evaluated {evaluated:,}; stable {passed:,}", flush=True)

    context_finalists = [item[2] for item in sorted(context_heap, reverse=True)]
    exits = exit_specs()
    print(f"Exit sweep: {len(context_finalists)} stable contexts × {len(exits)} exit combinations")
    exit_heap: list = []
    serial = 0
    exit_evaluated = exit_passed = 0
    for candidate_i, (entry, filter_spec, _) in enumerate(context_finalists, 1):
        events = event_cache[entry]
        selected = [event for event in events if filter_event(event, filter_spec)]
        for exit_spec in exits:
            exit_evaluated += 1
            records = []
            for event in selected:
                result = exit_r(event, exit_spec)
                if result is not None:
                    records.append((event.day_data.day, result))
            scored = development_score(records, config_complexity(entry, filter_spec, exit_spec))
            if scored is None:
                continue
            exit_passed += 1
            score, stats = scored
            serial += 1
            push_top(exit_heap, 100, score, serial, (Candidate(entry, filter_spec, exit_spec, score), stats))
        print(f"  exit progress {candidate_i}/{len(context_finalists)}", flush=True)

    optimized = [item[2] for item in sorted(exit_heap, reverse=True)]
    print(f"Evaluated {exit_evaluated:,} exit combinations; {exit_passed:,} passed development gates")

    # First untouched cached check: April through the end of the cached file.
    holdout_passers: list[tuple[Candidate, dict, dict]] = []
    for candidate, dev_stats in optimized:
        records = candidate_records(event_cache[candidate.entry], candidate.filters, candidate.exit)
        holdout = metrics([r for day, r in records if _period(day) == "P4"])
        if holdout["n"] >= 8 and holdout["avg_r"] > 0 and holdout["pf"] >= 1.05:
            holdout_passers.append((candidate, dev_stats, holdout))
    print(f"Cached holdout passers: {len(holdout_passers)}/{len(optimized)}")

    combined = fetch_recent(cached) if args.fresh else cached
    combined_days = prepare_days(combined)
    combined_cache: dict[EntrySpec, list[EntryEvent]] = {}
    reports = []
    for candidate, dev_stats, holdout in holdout_passers[:40]:
        events = combined_cache.setdefault(candidate.entry, build_events(combined_days, candidate.entry))
        records = candidate_records(events, candidate.filters, candidate.exit)
        recent = metrics([r for day, r in records if day >= RECENT_START]) if args.fresh else metrics([])
        stress_exit = replace(candidate.exit, same_bar_stop=True, slippage_ticks=2.0, commission=2 * COMMISSION)
        stress_records = candidate_records(events, candidate.filters, stress_exit)
        stress = metrics([r for day, r in stress_records if day < RECENT_START])
        period_stats = {**dev_stats, "P4": holdout, "recent": recent}
        reports.append(serialize_candidate(candidate, period_stats, stress))

    # A recent pass is intentionally modest because only ~2 months are available.
    reports.sort(
        key=lambda report: (
            report["periods"]["recent"]["n"] >= 5
            and report["periods"]["recent"]["avg_r"] > 0
            and report["periods"]["recent"]["pf"] >= 1.05,
            min(report["periods"]["P4"]["avg_r"], report["periods"]["recent"]["avg_r"]),
            report["development_score"],
            -report["complexity"],
        ),
        reverse=True,
    )

    baseline_entry = EntrySpec()
    baseline_filters = FilterSpec()
    baseline_events = build_events(combined_days, baseline_entry)
    baseline_records = candidate_records(baseline_events, baseline_filters, baseline_exit)
    baseline_periods = {
        name: metrics([r for day, r in baseline_records if _period(day) == name])
        for name in ("P1", "P2", "P3")
    }
    baseline_periods["P4"] = metrics(
        [r for day, r in baseline_records if P3_END <= day < RECENT_START]
    )
    baseline_periods["recent"] = metrics([r for day, r in baseline_records if day >= RECENT_START])

    output = {
        "data": {
            "cached_start": str(cached.index.min()),
            "cached_end": str(cached.index.max()),
            "combined_end": str(combined.index.max()),
            "sessions": len(combined_days),
        },
        "search": {
            "entry_specs": len(entries),
            "filter_specs": len(filters),
            "entry_filter_combinations": evaluated,
            "stable_entry_filter_combinations": passed,
            "exit_combinations": exit_evaluated,
            "stable_exit_combinations": exit_passed,
            "cached_holdout_passers": len(holdout_passers),
        },
        "baseline": baseline_periods,
        "finalists": reports[:20],
    }
    REPORT.write_text(json.dumps(output, indent=2, allow_nan=True))

    print("\nBASELINE CONFIRMED-RETEST")
    for name, stat in baseline_periods.items():
        print(f"  {name}: n={stat['n']:3d} avg={stat['avg_r']:+.3f}R PF={stat['pf']:.2f} total={stat['total_r']:+.2f}R")
    print("\nTOP FINALISTS")
    for rank, report in enumerate(reports[:10], 1):
        p = report["periods"]
        print(
            f"#{rank} score={report['development_score']:+.3f} complexity={report['complexity']} | "
            f"P4 n={p['P4']['n']} avg={p['P4']['avg_r']:+.3f} PF={p['P4']['pf']:.2f} | "
            f"recent n={p['recent']['n']} avg={p['recent']['avg_r']:+.3f} PF={p['recent']['pf']:.2f}"
        )
        print("   entry ", report["entry"])
        print("   filter", report["filters"])
        print("   exit  ", report["exit"])
    print(f"\nDetailed report: {REPORT}")


if __name__ == "__main__":
    main()
