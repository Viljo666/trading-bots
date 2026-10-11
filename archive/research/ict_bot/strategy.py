"""ICT mechanical setup:

LONG  (HTF bias = bullish):
  1. A recent swing low got swept (price pierced below it on an LTF bar — the
     "stop hunt" / liquidity grab).
  2. The sweep wick is "real" (>= sweep_min_wick_atr * ATR), filtering noise.
  3. After the sweep, a bullish FVG forms within the next few bars.
  4. Entry trigger: price retraces back into the FVG (touches its top edge).
  5. Stop: just below the sweep low (extreme of the sweeping bar) - buffer*ATR.
  6. Target: nearest unswept swing HIGH (opposing liquidity); require ≥ target_r_min R.

SHORT is the mirror.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from config import STRATEGY
from indicators import FVG, Swing, atr, find_fvgs, find_swings, mark_sweeps_inplace


@dataclass
class Trade:
    side: str
    entry_idx: int
    entry: float
    stop: float
    target: float
    exit_idx: int | None = None
    exit: float | None = None
    pnl_r: float | None = None
    reason: str = ""


def _opposing_liquidity(swings: list[Swing], side: str, after_idx: int) -> float | None:
    """Find the nearest unswept opposing swing AFTER the entry bar.
    For LONG: nearest unswept swing HIGH ahead in time (we use the price level).
    For SHORT: nearest unswept swing LOW.

    To avoid look-ahead, we use swings that were *already formed* before `after_idx`
    and remained unswept at that point. The target itself is just the price level —
    we don't peek at when it gets hit.
    """
    target_side = "high" if side == "long" else "low"
    candidates: list[float] = []
    for s in swings:
        if s.side != target_side:
            continue
        if s.idx >= after_idx:
            continue
        # Skip if it was already swept BEFORE the entry.
        if s.swept and s.swept_idx is not None and s.swept_idx <= after_idx:
            continue
        candidates.append(s.price)
    if not candidates:
        return None
    return max(candidates) if side == "long" else min(candidates)


def find_setups(ltf_df: pd.DataFrame, htf_bias_aligned: pd.Series) -> list[Trade]:
    """Scan LTF bars for ICT setups; return potential trades with entry/stop/target."""
    df = ltf_df
    n = len(df)
    if n < max(STRATEGY.swing_left + STRATEGY.swing_right + 5, STRATEGY.atr_period + 5):
        return []

    atr_series = atr(df, STRATEGY.atr_period)
    swings = find_swings(df, STRATEGY.swing_left, STRATEGY.swing_right)
    mark_sweeps_inplace(swings, df)
    fvgs = find_fvgs(df, STRATEGY.min_fvg_pct)

    # Index FVGs by formed_idx for quick lookup of "FVGs that just formed".
    fvgs_by_form: dict[int, list[FVG]] = {}
    for f in fvgs:
        fvgs_by_form.setdefault(f.formed_idx, []).append(f)

    trades: list[Trade] = []

    high = df["high"].values
    low = df["low"].values
    close = df["close"].values
    bias = htf_bias_aligned.values

    for s in swings:
        if not s.swept or s.swept_idx is None:
            continue
        sweep_idx = s.swept_idx
        if sweep_idx >= n - 1:
            continue

        # Validate "real" sweep: wick beyond the swing >= sweep_min_wick_atr * ATR.
        atr_val = float(atr_series.iloc[sweep_idx]) if not pd.isna(atr_series.iloc[sweep_idx]) else 0.0
        if atr_val <= 0:
            continue

        if s.side == "low":
            # Looking for LONG (HTF bullish + sweep below low).
            if bias[sweep_idx] != 1:
                continue
            wick = s.price - low[sweep_idx]
            if wick < STRATEGY.sweep_min_wick_atr * atr_val:
                continue
            # Find a bullish FVG that forms within the next few bars.
            window_end = min(n, sweep_idx + 8)
            entry = None
            entry_idx = None
            chosen_fvg: FVG | None = None
            for j in range(sweep_idx, window_end):
                for f in fvgs_by_form.get(j, []):
                    if f.side != "bullish":
                        continue
                    # Watch for retrace into FVG (low of a later bar touches f.top).
                    for k in range(j + 1, min(n, j + STRATEGY.fvg_max_age_bars)):
                        if low[k] <= f.top and high[k] >= f.bottom:
                            entry = f.top
                            entry_idx = k
                            chosen_fvg = f
                            break
                    if entry is not None:
                        break
                if entry is not None:
                    break
            if entry is None or entry_idx is None or chosen_fvg is None:
                continue
            stop = low[sweep_idx] - STRATEGY.stop_buffer_atr * atr_val
            target = _opposing_liquidity(swings, "long", entry_idx)
            if target is None:
                continue
            risk = entry - stop
            reward = target - entry
            if risk <= 0 or reward / risk < STRATEGY.target_r_min:
                continue
            trades.append(Trade("long", entry_idx, float(entry), float(stop), float(target),
                                reason=f"sweep@{sweep_idx} fvg@{chosen_fvg.formed_idx}"))

        elif s.side == "high":
            if bias[sweep_idx] != -1:
                continue
            wick = high[sweep_idx] - s.price
            if wick < STRATEGY.sweep_min_wick_atr * atr_val:
                continue
            window_end = min(n, sweep_idx + 8)
            entry = None
            entry_idx = None
            chosen_fvg = None
            for j in range(sweep_idx, window_end):
                for f in fvgs_by_form.get(j, []):
                    if f.side != "bearish":
                        continue
                    for k in range(j + 1, min(n, j + STRATEGY.fvg_max_age_bars)):
                        if high[k] >= f.bottom and low[k] <= f.top:
                            entry = f.bottom
                            entry_idx = k
                            chosen_fvg = f
                            break
                    if entry is not None:
                        break
                if entry is not None:
                    break
            if entry is None or entry_idx is None or chosen_fvg is None:
                continue
            stop = high[sweep_idx] + STRATEGY.stop_buffer_atr * atr_val
            target = _opposing_liquidity(swings, "short", entry_idx)
            if target is None:
                continue
            risk = stop - entry
            reward = entry - target
            if risk <= 0 or reward / risk < STRATEGY.target_r_min:
                continue
            trades.append(Trade("short", entry_idx, float(entry), float(stop), float(target),
                                reason=f"sweep@{sweep_idx} fvg@{chosen_fvg.formed_idx}"))

    # De-dup overlapping setups (same direction, entries within time_stop_bars of each other).
    deduped: list[Trade] = []
    for t in sorted(trades, key=lambda x: x.entry_idx):
        if deduped and t.side == deduped[-1].side and t.entry_idx - deduped[-1].entry_idx < STRATEGY.time_stop_bars:
            continue
        deduped.append(t)
    return deduped
