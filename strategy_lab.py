"""Strategy lab: compare candidate strategies on the SAME 6-month window.

Backtest-only. Does NOT touch the live bot or its config. Three variants:

  1. momentum  — multi-day momentum swing on DAILY bars (holds overnight).
  2. core_sat  — 70% SPY buy-hold + 30% the ORB intraday bot (satellite alpha).
  3. orb_swing — handled by backtest.py --orb-swing (intraday engine), not here.

This script implements #1 and #2 (both daily-bar / analytical, so fast).
#3 is run separately because it needs the slow 15-min engine.

Usage:
    python strategy_lab.py --months 6 --orb-return 6.71
        --orb-return: the ORB-only bot's % return on the same window (from
                      backtest.py), used to compute the core+satellite blend.
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytz
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

import config
import indicators
import scanner
from risk import size_trade

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
log = logging.getLogger("lab")


# ---------------------------------------------------------------------------
# Momentum swing — config (lab-local, independent of live STRATEGY config)
# ---------------------------------------------------------------------------

TREND_SMA = 50          # price must be above this rising SMA
BREAKOUT_SMA = 20       # exit trigger: close below this SMA
NEAR_HIGH_LOOKBACK = 20 # must be within NEAR_HIGH_PCT of the N-day high
NEAR_HIGH_PCT = 5.0
RS_LOOKBACK = 60        # relative strength window vs SPY
ATR_PERIOD = 14
ATR_STOP_MULT = 2.5     # initial + trailing stop distance
MAX_POSITIONS = 5
RISK_PCT = 0.01


@dataclass
class SwingPos:
    symbol: str
    qty: int
    entry: float
    entry_date: pd.Timestamp
    stop: float
    highest_close: float


@dataclass
class LabResult:
    name: str
    start: str
    end: str
    ending_equity: float
    trades: int
    wins: int
    equity_curve: list[float] = field(default_factory=list)

    @property
    def ret_pct(self) -> float:
        return (self.ending_equity - 100_000.0) / 100_000.0 * 100.0

    @property
    def win_rate(self) -> float:
        return (self.wins / self.trades * 100.0) if self.trades else 0.0

    @property
    def max_drawdown_pct(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0]
        mdd = 0.0
        for v in self.equity_curve:
            peak = max(peak, v)
            mdd = min(mdd, (v - peak) / peak)
        return mdd * 100.0


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

def _client() -> StockHistoricalDataClient:
    return StockHistoricalDataClient(config.ALPACA.api_key, config.ALPACA.secret_key)


def _fetch_daily(client, symbols: list[str], start, end, chunk=100) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(symbols), chunk):
        batch = symbols[i:i + chunk]
        try:
            req = StockBarsRequest(symbol_or_symbols=batch,
                                   timeframe=TimeFrame(1, TimeFrameUnit.Day),
                                   start=start, end=end)
            df = client.get_stock_bars(req).df
        except Exception as e:
            log.warning("fetch %d failed: %s", i, e)
            continue
        if df is None or df.empty:
            continue
        for sym in batch:
            if sym in df.index.get_level_values(0):
                sub = df.xs(sym, level=0).copy()
                sub.index = pd.to_datetime(sub.index)
                out[sym] = sub
    return out


# ---------------------------------------------------------------------------
# Strategy 1: momentum swing (daily, holds overnight)
# ---------------------------------------------------------------------------

def run_momentum(daily: dict[str, pd.DataFrame], universe: list[str],
                 warmup: int) -> LabResult:
    spy = daily.get("SPY")
    if spy is None:
        raise RuntimeError("SPY daily bars missing")
    all_dates = sorted(spy.index)
    sim_dates = all_dates[warmup:]  # leave warmup for SMAs/RS

    # Precompute per-symbol indicator frames
    feats: dict[str, pd.DataFrame] = {}
    for sym, df in daily.items():
        if len(df) < warmup + 5:
            continue
        f = df.copy()
        f["sma_trend"] = f["close"].rolling(TREND_SMA).mean()
        f["sma_break"] = f["close"].rolling(BREAKOUT_SMA).mean()
        f["hi_n"] = f["high"].rolling(NEAR_HIGH_LOOKBACK).max()
        f["atr"] = indicators.atr(f, ATR_PERIOD)
        f["ret_rs"] = f["close"].pct_change(RS_LOOKBACK)
        feats[sym] = f

    spy_rs = spy["close"].pct_change(RS_LOOKBACK)

    equity = 100_000.0
    cash = equity
    positions: dict[str, SwingPos] = {}
    trades = wins = 0
    curve: list[float] = []

    for i, d in enumerate(sim_dates):
        # --- mark + manage exits using day d's bars ---
        for sym in list(positions.keys()):
            f = feats.get(sym)
            if f is None or d not in f.index:
                continue
            row = f.loc[d]
            pos = positions[sym]
            o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
            exit_px = None
            # gap-aware stop
            if o <= pos.stop:
                exit_px = o
            elif l <= pos.stop:
                exit_px = pos.stop
            elif not np.isnan(row["sma_break"]) and c < float(row["sma_break"]):
                exit_px = c  # trend break -> exit on close
            if exit_px is not None:
                cash += pos.qty * exit_px
                pnl = (exit_px - pos.entry) * pos.qty
                trades += 1
                wins += 1 if pnl > 0 else 0
                positions.pop(sym)
            else:
                # trail the stop up
                pos.highest_close = max(pos.highest_close, c)
                if not np.isnan(row["atr"]):
                    pos.stop = max(pos.stop, pos.highest_close - ATR_STOP_MULT * float(row["atr"]))

        # --- entries: rank eligible names, fill at NEXT day's open ---
        if len(positions) < MAX_POSITIONS and i + 1 < len(sim_dates):
            nxt = sim_dates[i + 1]
            spy_rs_d = float(spy_rs.loc[d]) if d in spy_rs.index and not np.isnan(spy_rs.loc[d]) else 0.0
            cands = []
            for sym in universe:
                if sym in positions or sym == "SPY":
                    continue
                f = feats.get(sym)
                if f is None or d not in f.index or nxt not in f.index:
                    continue
                row = f.loc[d]
                if np.isnan(row["sma_trend"]) or np.isnan(row["atr"]) or row["atr"] <= 0:
                    continue
                c = float(row["close"])
                # rising trend SMA
                idx = f.index.get_loc(d)
                if idx < 10:
                    continue
                sma_then = float(f["sma_trend"].iloc[idx - 10])
                if np.isnan(sma_then):
                    continue
                price_above = c > float(row["sma_trend"])
                rising = float(row["sma_trend"]) > sma_then
                near_high = c >= float(row["hi_n"]) * (1 - NEAR_HIGH_PCT / 100.0)
                rs = float(row["ret_rs"]) if not np.isnan(row["ret_rs"]) else -1
                stronger = rs > spy_rs_d
                if price_above and rising and near_high and stronger:
                    cands.append((rs, sym, c, float(row["atr"])))
            cands.sort(reverse=True)  # strongest RS first
            for rs, sym, c, a in cands:
                if len(positions) >= MAX_POSITIONS:
                    break
                entry = float(feats[sym].loc[nxt]["open"])
                stop = entry - ATR_STOP_MULT * a
                t = size_trade(sym, "buy", entry, stop, entry + a, equity=cash)
                if t is None or t.qty <= 0:
                    continue
                cost = t.qty * entry
                if cost > cash:
                    continue
                cash -= cost
                positions[sym] = SwingPos(sym, t.qty, entry, nxt, stop, entry)

        # --- mark-to-market equity at day d close ---
        mtm = cash
        for sym, pos in positions.items():
            f = feats.get(sym)
            if f is not None and d in f.index:
                mtm += pos.qty * float(f.loc[d]["close"])
            else:
                mtm += pos.qty * pos.entry
        equity = mtm
        curve.append(equity)

    # liquidate remainder at last close
    last = sim_dates[-1]
    for sym, pos in positions.items():
        f = feats.get(sym)
        px = float(f.loc[last]["close"]) if (f is not None and last in f.index) else pos.entry
        cash += pos.qty * px
        pnl = (px - pos.entry) * pos.qty
        trades += 1
        wins += 1 if pnl > 0 else 0
    equity = cash
    curve.append(equity)

    return LabResult("momentum_swing", str(sim_dates[0].date()), str(sim_dates[-1].date()),
                     equity, trades, wins, curve)


# ---------------------------------------------------------------------------
# Benchmark + core-satellite
# ---------------------------------------------------------------------------

def spy_buy_hold(daily, warmup) -> LabResult:
    spy = daily["SPY"]
    dates = sorted(spy.index)[warmup:]
    p0 = float(spy.loc[dates[0]]["open"])
    qty = 100_000.0 / p0
    curve = [qty * float(spy.loc[d]["close"]) for d in dates]
    end = qty * float(spy.loc[dates[-1]]["close"])
    return LabResult("spy_buy_hold", str(dates[0].date()), str(dates[-1].date()),
                     end, 1, 1, curve)


def core_satellite(daily, warmup, orb_return_pct, core_w=0.70) -> LabResult:
    """70% SPY buy-hold + 30% ORB bot (modeled as linear scaling of its % return)."""
    spy = daily["SPY"]
    dates = sorted(spy.index)[warmup:]
    p0 = float(spy.loc[dates[0]]["open"])
    spy_qty = (100_000.0 * core_w) / p0
    sat_start = 100_000.0 * (1 - core_w)
    # Satellite grows linearly to its final return across the window (approx).
    n = len(dates)
    curve = []
    for i, d in enumerate(dates):
        core_v = spy_qty * float(spy.loc[d]["close"])
        frac = (i + 1) / n
        sat_v = sat_start * (1 + (orb_return_pct / 100.0) * frac)
        curve.append(core_v + sat_v)
    end = curve[-1]
    return LabResult(f"core{int(core_w*100)}_sat", str(dates[0].date()), str(dates[-1].date()),
                     end, 0, 0, curve)


# ---------------------------------------------------------------------------
# Dual Momentum (Antonacci GEM) — monthly, asset-class ETFs, holds for weeks
# ---------------------------------------------------------------------------

DM_LOOKBACK_M = 12          # 12-month momentum
DM_US = "SPY"               # US equities
DM_INTL = "EFA"             # international equities (MSCI EAFE)
DM_BOND = "AGG"             # aggregate bonds (defensive)
DM_CASH = "BIL"             # 1-3mo T-bills (absolute-momentum benchmark)


def _month_end_closes(df: pd.DataFrame) -> pd.Series:
    """Resample daily closes to month-end last value."""
    s = df["close"].copy()
    s.index = pd.to_datetime(s.index)
    return s.resample("ME").last()


def run_dual_momentum(years: int = 3) -> None:
    """Antonacci Global Equities Momentum. Self-contained: fetches its own ETFs.

    Monthly rule:
      if SPY_12mo > BIL_12mo (positive absolute momentum):
          hold whichever of {SPY, EFA} has higher 12mo return
      else:
          hold AGG (bonds)
    Reports CAGR, max drawdown, Sharpe vs SPY buy-hold over the same months.

    NOTE: uses price-only closes (no dividends), so it slightly understates
    total return for all sleeves — but symmetrically, so the comparison holds.
    """
    end = datetime.now(pytz.UTC) - timedelta(days=1)
    start = end - timedelta(days=365 * years + 400)  # extra year for 12mo lookback
    syms = [DM_US, DM_INTL, DM_BOND, DM_CASH]
    log.info("Dual Momentum: fetching %s ...", syms)
    daily = _fetch_daily(_client(), syms, start, end)

    missing = [s for s in syms if s not in daily]
    if missing:
        log.warning("Dual Momentum: missing ETFs %s — cannot run.", missing)
        return

    me = {s: _month_end_closes(daily[s]) for s in syms}
    # Align all on the common month-end index
    idx = me[DM_US].index
    for s in syms:
        idx = idx.intersection(me[s].index)
    idx = idx.sort_values()
    if len(idx) < DM_LOOKBACK_M + 3:
        log.warning("Dual Momentum: only %d months of data — not enough.", len(idx))
        return

    def mom(sym, t_i):
        s = me[sym]
        cur = float(s.loc[idx[t_i]])
        prev = float(s.loc[idx[t_i - DM_LOOKBACK_M]])
        return cur / prev - 1.0

    equity = 100_000.0
    curve = [equity]
    holdings_log: dict[str, int] = {}
    monthly_rets: list[float] = []
    spy_curve = [100_000.0]
    spy_qty = 100_000.0 / float(me[DM_US].loc[idx[DM_LOOKBACK_M]])

    # Simulate from month LOOKBACK to second-to-last (need next month to realize return)
    for t_i in range(DM_LOOKBACK_M, len(idx) - 1):
        if mom(DM_US, t_i) > mom(DM_CASH, t_i):
            pick = DM_US if mom(DM_US, t_i) >= mom(DM_INTL, t_i) else DM_INTL
        else:
            pick = DM_BOND
        holdings_log[pick] = holdings_log.get(pick, 0) + 1
        # realize next month's return of the picked asset
        c_now = float(me[pick].loc[idx[t_i]])
        c_nxt = float(me[pick].loc[idx[t_i + 1]])
        r = c_nxt / c_now - 1.0
        monthly_rets.append(r)
        equity *= (1 + r)
        curve.append(equity)
        spy_curve.append(spy_qty * float(me[DM_US].loc[idx[t_i + 1]]))

    n_months = len(monthly_rets)
    years_actual = n_months / 12.0
    cagr = (equity / 100_000.0) ** (1 / years_actual) - 1 if years_actual > 0 else 0
    spy_end = spy_curve[-1]
    spy_cagr = (spy_end / 100_000.0) ** (1 / years_actual) - 1 if years_actual > 0 else 0

    def max_dd(c):
        peak, mdd = c[0], 0.0
        for v in c:
            peak = max(peak, v)
            mdd = min(mdd, (v - peak) / peak)
        return mdd * 100.0

    rets = np.array(monthly_rets)
    sharpe = (rets.mean() / rets.std() * np.sqrt(12)) if rets.std() > 0 else 0.0
    spy_rets = np.diff(spy_curve) / spy_curve[:-1]
    spy_sharpe = (spy_rets.mean() / spy_rets.std() * np.sqrt(12)) if spy_rets.std() > 0 else 0.0

    print("\n" + "=" * 70)
    print(f"DUAL MOMENTUM (GEM)  —  {idx[DM_LOOKBACK_M].date()} to {idx[-1].date()}  ({n_months} months)")
    print("=" * 70)
    print(f"{'Metric':<16}{'Dual Momentum':>18}{'SPY buy-hold':>18}")
    print("-" * 70)
    print(f"{'Total return':<16}{(equity/1000-100):>17.2f}%{(spy_end/1000-100):>17.2f}%")
    print(f"{'CAGR':<16}{cagr*100:>17.2f}%{spy_cagr*100:>17.2f}%")
    print(f"{'Max drawdown':<16}{max_dd(curve):>17.2f}%{max_dd(spy_curve):>17.2f}%")
    print(f"{'Sharpe (ann.)':<16}{sharpe:>18.2f}{spy_sharpe:>18.2f}")
    print("-" * 70)
    print(f"Ending equity: ${equity:,.0f}  vs SPY ${spy_end:,.0f}")
    print(f"Months held: {dict(sorted(holdings_log.items(), key=lambda x: -x[1]))}")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=6)
    ap.add_argument("--orb-return", type=float, default=6.71,
                    help="ORB-only bot %% return on same window (from backtest.py)")
    ap.add_argument("--dual-momentum", action="store_true",
                    help="Run the Dual Momentum (GEM) prototype instead of the intraday comparison.")
    ap.add_argument("--dm-years", type=int, default=3, help="Dual Momentum lookback span in years.")
    args = ap.parse_args()

    if args.dual_momentum:
        run_dual_momentum(years=args.dm_years)
        return 0

    end = datetime.now(pytz.UTC) - timedelta(days=1)
    start = end - timedelta(days=args.months * 31 + 90)  # extra 90d warmup for SMA50/RS60
    universe = scanner.build_universe()
    log.info("Fetching daily bars for %d symbols...", len(universe))
    daily = _fetch_daily(_client(), universe, start, end)
    log.info("Fetched %d symbols", len(daily))

    warmup = RS_LOOKBACK + 10  # ~70 trading days reserved

    results = [
        spy_buy_hold(daily, warmup),
        run_momentum(daily, universe, warmup),
        core_satellite(daily, warmup, args.orb_return),
    ]

    print("\n" + "=" * 78)
    print(f"{'Strategy':<20}{'Return':>10}{'Alpha':>10}{'MaxDD':>10}{'Trades':>9}{'Win%':>8}")
    print("-" * 78)
    bench = results[0].ret_pct
    for r in results:
        print(f"{r.name:<20}{r.ret_pct:>9.2f}%{r.ret_pct - bench:>9.2f}%"
              f"{r.max_drawdown_pct:>9.2f}%{r.trades:>9}{r.win_rate:>7.1f}%")
    print("=" * 78)
    print(f"Window: {results[0].start} -> {results[0].end}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
