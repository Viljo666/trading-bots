"""Causal NQ research for the prior-RTH POC continuation setup.

This script is research-only.  It reads the cached NQ five-minute history and
never connects to a broker.  The first 60% of sessions are development data;
the final 40% are a chronological holdout.

The simulator matches the live/TradingView timing:

* prior RTH profile uses bars starting 09:30 through 15:55 ET;
* the first eligible entry bar starts at 09:35 ET;
* no entry bar may start at or after the configured cutoff;
* an unresolved trade is flattened at 15:55 ET using the 15:50 bar close;
* exits cannot occur in the same five-minute bar as the entry;
* stop wins ties when a later bar reaches stop and target.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "orb_futures" / "data" / "NQ_5m.csv"
ET = "America/New_York"
TICK = 0.25
DOLLARS_PER_POINT = 20.0
COMMISSION = 4.62
POC_BIN = 4.0
DEVELOPMENT_FRACTION = 0.60


@dataclass(frozen=True)
class Config:
    atr_mult: float = 0.25
    reward_risk: float = 3.0
    cutoff: time = time(15, 0)
    entry_mode: str = "touch"  # touch | close_retest
    min_distance_atr: float = 0.0
    max_distance_atr: float = float("inf")
    opening_bar_alignment: bool = False
    direction: str = "both"  # both | long | short
    confirmation_minutes: int = 5
    same_bar_stop: bool = False
    slippage_ticks: float = 1.0
    commission: float = COMMISSION


def load() -> pd.DataFrame:
    frame = pd.read_csv(DATA)
    frame.index = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert(ET)
    return frame[["open", "high", "low", "close", "volume"]].astype(float).dropna()


def session_poc(session: pd.DataFrame) -> float:
    lower = np.floor(session.low.min() / POC_BIN) * POC_BIN
    upper = np.ceil(session.high.max() / POC_BIN) * POC_BIN
    edges = np.arange(lower, upper + POC_BIN, POC_BIN)
    if len(edges) < 2:
        return float(session.close.iloc[-1])
    volume = np.zeros(len(edges) - 1)
    for high, low, bar_volume in zip(session.high, session.low, session.volume):
        first = max(0, int((low - lower) // POC_BIN))
        last = min(len(volume) - 1, int((high - lower) // POC_BIN))
        volume[first : last + 1] += bar_volume / (last - first + 1)
    return float(edges[:-1][int(volume.argmax())] + POC_BIN / 2)


def daily_atr(profile_bars: pd.DataFrame, period: int = 14) -> pd.Series:
    daily = profile_bars.resample("1D").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna()
    true_range = pd.concat(
        [
            daily.high - daily.low,
            (daily.high - daily.close.shift()).abs(),
            (daily.low - daily.close.shift()).abs(),
        ],
        axis=1,
    ).max(axis=1)
    result = true_range.rolling(period).mean()
    result.index = result.index.date
    return result


def _eligible_entry_index(rows: list, side: str, poc: float, config: Config) -> int | None:
    confirmed = config.entry_mode == "touch"
    for i, bar in enumerate(rows):
        if bar.Index.time() >= config.cutoff:
            break
        if config.entry_mode == "close_retest" and not confirmed:
            bar_close = bar.Index + pd.Timedelta(minutes=5)
            confirmation_complete = config.confirmation_minutes == 5 or (
                config.confirmation_minutes == 15
                and bar_close.minute % 15 == 0
                and bar_close.time() > time(9, 45)
            )
            if confirmation_complete:
                confirmed = bar.close > poc if side == "long" else bar.close < poc
            continue
        if config.entry_mode == "close_retest":
            # After a close through the level, a real pullback must trade back
            # down to POC for a long or back up to POC for a short.
            touched = bar.low <= poc if side == "long" else bar.high >= poc
        else:
            touched = bar.high >= poc if side == "long" else bar.low <= poc
        if touched:
            return i
    return None


def simulate(bars: pd.DataFrame, config: Config) -> pd.DataFrame:
    profile_bars = bars.between_time("09:30", "15:55")
    atr = daily_atr(profile_bars)
    sessions = [(day, session) for day, session in profile_bars.groupby(profile_bars.index.date)
                if len(session) >= 70]
    records: list[dict] = []

    for session_i in range(1, len(sessions)):
        previous_day, previous = sessions[session_i - 1]
        day, session = sessions[session_i]
        previous_atr = float(atr.get(previous_day, np.nan))
        if not np.isfinite(previous_atr) or previous_atr <= 0:
            continue

        poc = session_poc(previous)
        day_open = float(session.open.iloc[0])
        if poc == day_open:
            continue
        side = "long" if poc > day_open else "short"
        if config.direction != "both" and side != config.direction:
            continue

        distance_atr = abs(poc - day_open) / previous_atr
        if not config.min_distance_atr <= distance_atr <= config.max_distance_atr:
            continue

        opening_bar = session.iloc[0]
        opening_aligned = opening_bar.close > opening_bar.open if side == "long" else opening_bar.close < opening_bar.open
        if config.opening_bar_alignment and not opening_aligned:
            continue

        # Orders are armed after the opening bar closes.  The bar beginning at
        # 09:35 is the first one in which a fill can be observed.  Flattening
        # at 15:55 means the 15:50 bar is the final bar available to the trade.
        trade_bars = session.between_time("09:35", "15:50")
        rows = list(trade_bars.itertuples())
        entry_i = _eligible_entry_index(rows, side, poc, config)
        if entry_i is None:
            continue

        risk = config.atr_mult * previous_atr
        stop = poc - risk if side == "long" else poc + risk
        target = poc + config.reward_risk * risk if side == "long" else poc - config.reward_risk * risk
        exit_price: float | None = None
        reason = "session_close"
        entry_bar = rows[entry_i]
        entry_bar_stop = entry_bar.low <= stop if side == "long" else entry_bar.high >= stop
        if config.same_bar_stop and entry_bar_stop:
            slip = config.slippage_ticks * TICK
            exit_price = stop - slip if side == "long" else stop + slip
            reason = "same_bar_stop"

        for bar in rows[entry_i + 1 :] if exit_price is None else []:
            stop_hit = bar.low <= stop if side == "long" else bar.high >= stop
            target_hit = bar.high >= target if side == "long" else bar.low <= target
            if stop_hit:
                slip = config.slippage_ticks * TICK
                exit_price = stop - slip if side == "long" else stop + slip
                reason = "stop"
                break
            if target_hit:
                exit_price = target
                reason = "target"
                break
        if exit_price is None:
            final_close = float(trade_bars.close.iloc[-1])
            slip = config.slippage_ticks * TICK
            exit_price = final_close - slip if side == "long" else final_close + slip

        gross_points = exit_price - poc if side == "long" else poc - exit_price
        net_points = gross_points - config.commission / DOLLARS_PER_POINT
        records.append(
            {
                "date": day,
                "side": side,
                "entry_time": rows[entry_i].Index,
                "distance_atr": distance_atr,
                "opening_aligned": opening_aligned,
                "reason": reason,
                "r": net_points / risk,
                "pnl_usd": net_points * DOLLARS_PER_POINT,
            }
        )
    return pd.DataFrame(records)


def metrics(frame: pd.DataFrame) -> dict[str, float | int]:
    if frame.empty:
        return {"n": 0, "win": 0.0, "total_r": 0.0, "avg_r": 0.0, "pf_r": 0.0}
    wins = frame.r[frame.r > 0]
    losses = -frame.r[frame.r <= 0]
    return {
        "n": len(frame),
        "win": 100 * (frame.r > 0).mean(),
        "total_r": frame.r.sum(),
        "avg_r": frame.r.mean(),
        "pf_r": wins.sum() / losses.sum() if losses.sum() else float("inf"),
    }


def split(frame: pd.DataFrame, split_day) -> tuple[pd.DataFrame, pd.DataFrame]:
    return frame[frame.date < split_day], frame[frame.date >= split_day]


def describe(label: str, frame: pd.DataFrame, split_day) -> dict:
    development, holdout = split(frame, split_day)
    return {"label": label, "all": metrics(frame), "development": metrics(development), "holdout": metrics(holdout)}


def print_result(result: dict) -> None:
    fields = [result[part] for part in ("all", "development", "holdout")]
    formatted = []
    for value in fields:
        formatted.append(
            f"n={value['n']:3d} avg={value['avg_r']:+.3f}R PF={value['pf_r']:.2f}"
        )
    print(f"{result['label']:<31} | " + " | ".join(formatted))


def main() -> None:
    bars = load()
    profile = bars.between_time("09:30", "15:55")
    session_days = sorted(profile.index.date)
    split_day = session_days[int(len(session_days) * DEVELOPMENT_FRACTION)]

    candidates: list[tuple[str, Config]] = [("baseline aligned", Config())]
    for cutoff in (time(10, 30), time(11, 30), time(12, 30), time(14, 0)):
        candidates.append((f"cutoff {cutoff.strftime('%H:%M')}", Config(cutoff=cutoff)))
    candidates.extend(
        [
            ("confirmation close + retest", Config(entry_mode="close_retest")),
            ("opening bar aligned", Config(opening_bar_alignment=True)),
            ("POC distance >= 0.10 ATR", Config(min_distance_atr=0.10)),
            ("POC distance >= 0.20 ATR", Config(min_distance_atr=0.20)),
            ("POC distance <= 0.50 ATR", Config(max_distance_atr=0.50)),
            ("long only", Config(direction="long")),
            ("short only", Config(direction="short")),
        ]
    )
    for atr_mult in (0.15, 0.20, 0.30, 0.40, 0.50):
        candidates.append((f"stop {atr_mult:.2f} ATR", Config(atr_mult=atr_mult)))
    for reward_risk in (1.5, 2.0, 2.5, 3.5, 4.0):
        candidates.append((f"target {reward_risk:.1f}R", Config(reward_risk=reward_risk)))

    print(f"NQ data: {bars.index.min().date()} to {bars.index.max().date()} | holdout begins {split_day}")
    print("result                          | all                         | development                 | holdout")
    results = []
    for label, config in candidates:
        result = describe(label, simulate(bars, config), split_day)
        results.append(result)
        print_result(result)

    stable = [
        result for result in results
        if result["development"]["n"] >= 25
        and result["holdout"]["n"] >= 15
        and result["development"]["avg_r"] > 0
        and result["holdout"]["avg_r"] > 0
        and result["development"]["pf_r"] >= 1.10
        and result["holdout"]["pf_r"] >= 1.10
    ]
    print("\nStable candidates (positive with PF >= 1.10 in both periods):")
    if not stable:
        print("  none")
    else:
        for result in sorted(stable, key=lambda item: item["holdout"]["avg_r"], reverse=True):
            print_result(result)


if __name__ == "__main__":
    main()
