"""Walk-forward backtest reusing scanner + regime + strategy unchanged.

Fetches 6 months of daily bars (scanner input) and 15-min bars (signals + regime)
for the full universe, then simulates the bot day-by-day:

    for each trading day D:
        rank candidates from daily bars strictly before D
        shortlist top N + ETFs
        iterate 15-min bars of day D:
            - detect regime from SPY bars so far today
            - check exits on open positions (bracket: stop or target touch)
            - evaluate strategy on each shortlist symbol; enter at NEXT bar open
        flatten anything still open at 15:55 ET

Outputs:
    journal/backtest_<start>_to_<end>.md    summary
    journal/backtest_trades.csv              full trade log
"""
from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import pandas as pd
import pytz
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

import config
import regime as regime_mod
import scanner
import strategy
from risk import size_trade

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("backtest")

ET = pytz.timezone("America/New_York")
RTH_OPEN = dtime(9, 30)
RTH_CLOSE = dtime(16, 0)
FLATTEN_T = dtime(15, 55)


def _rth_only(df: pd.DataFrame) -> pd.DataFrame:
    """Drop bars outside 09:30-16:00 ET. Assumes df.index is ET-tz."""
    t = df.index.time
    mask = (t >= RTH_OPEN) & (t < RTH_CLOSE)
    return df[mask]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class BTPosition:
    symbol: str
    side: str              # "long" or "short"
    qty: int
    entry: float
    stop: float            # current (live) stop — may be moved to BE
    target: float
    entry_ts: pd.Timestamp
    setup: str
    reason: str
    stop_orig: float = 0.0 # original stop, used to compute risk for BE trigger
    be_triggered: bool = False


@dataclass
class BTTrade:
    symbol: str
    side: str
    qty: int
    entry: float
    stop: float
    target: float
    exit: float
    entry_ts: pd.Timestamp
    exit_ts: pd.Timestamp
    exit_reason: str       # "target" | "stop" | "eod"
    setup: str
    reason: str
    pnl: float
    r_multiple: float


@dataclass
class BTState:
    equity: float
    positions: dict[str, BTPosition] = field(default_factory=dict)
    trades: list[BTTrade] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Data fetch
# ---------------------------------------------------------------------------

def _client() -> StockHistoricalDataClient:
    return StockHistoricalDataClient(config.ALPACA.api_key, config.ALPACA.secret_key)


def _fetch(
    client: StockHistoricalDataClient,
    symbols: list[str],
    tf: TimeFrame,
    start: datetime,
    end: datetime,
    chunk: int = 100,
) -> dict[str, pd.DataFrame]:
    """Fetch bars for many symbols in chunks, return per-symbol DataFrames."""
    out: dict[str, pd.DataFrame] = {}
    for i in range(0, len(symbols), chunk):
        batch = symbols[i : i + chunk]
        try:
            req = StockBarsRequest(symbol_or_symbols=batch, timeframe=tf, start=start, end=end)
            df = client.get_stock_bars(req).df
        except Exception as e:
            log.warning("fetch batch %d-%d failed: %s", i, i + len(batch), e)
            continue
        if df is None or df.empty:
            continue
        for sym in batch:
            if sym in df.index.get_level_values(0):
                sub = df.xs(sym, level=0).copy()
                sub.index = pd.to_datetime(sub.index)
                out[sym] = sub
        log.info("fetched %d/%d symbols so far", len(out), len(symbols))
    return out


# ---------------------------------------------------------------------------
# Simulation helpers
# ---------------------------------------------------------------------------

def _to_et(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    return idx.tz_convert(ET)


def _slice_until(df: pd.DataFrame, ts: pd.Timestamp) -> pd.DataFrame:
    """Return rows with index <= ts (i.e. information available at ts)."""
    return df.loc[:ts]


def _check_exit(pos: BTPosition, bar: pd.Series) -> tuple[float, str] | None:
    """Bracket exit on a single bar. Conservative: if both stop and target touched, stop wins."""
    hi = float(bar["high"])
    lo = float(bar["low"])
    if pos.side == "long":
        if lo <= pos.stop:
            return pos.stop, "stop"
        if hi >= pos.target:
            return pos.target, "target"
    else:
        if hi >= pos.stop:
            return pos.stop, "stop"
        if lo <= pos.target:
            return pos.target, "target"
    return None


def _maybe_arm_be(pos: BTPosition, bar: pd.Series, be_trigger_r: float | None) -> None:
    """If this bar's MFE reached the BE trigger, move stop to entry for the NEXT bar.

    Evaluated AFTER the bar's exit check, so it never causes a same-bar scratch
    (which would require lookahead inside a bar).
    """
    if be_trigger_r is None or pos.be_triggered:
        return
    hi, lo = float(bar["high"]), float(bar["low"])
    if pos.side == "long":
        risk = pos.entry - pos.stop_orig
        trigger_price = pos.entry + risk * be_trigger_r
        if hi >= trigger_price:
            pos.stop = pos.entry
            pos.be_triggered = True
    else:
        risk = pos.stop_orig - pos.entry
        trigger_price = pos.entry - risk * be_trigger_r
        if lo <= trigger_price:
            pos.stop = pos.entry
            pos.be_triggered = True


def _close_position(
    state: BTState, sym: str, exit_price: float, exit_ts: pd.Timestamp, reason: str
) -> None:
    pos = state.positions.pop(sym)
    if pos.side == "long":
        pnl = (exit_price - pos.entry) * pos.qty
        risk_per_share = pos.entry - pos.stop
    else:
        pnl = (pos.entry - exit_price) * pos.qty
        risk_per_share = pos.stop - pos.entry
    risk_dollars = risk_per_share * pos.qty if risk_per_share > 0 else 0.0
    r = pnl / risk_dollars if risk_dollars > 0 else 0.0
    state.equity += pnl
    state.trades.append(
        BTTrade(
            symbol=pos.symbol, side=pos.side, qty=pos.qty, entry=pos.entry,
            stop=pos.stop, target=pos.target, exit=exit_price,
            entry_ts=pos.entry_ts, exit_ts=exit_ts, exit_reason=reason,
            setup=pos.setup, reason=pos.reason, pnl=pnl, r_multiple=r,
        )
    )


# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------

def _intraday_refresh_bt(
    premarket: list[scanner.Candidate],
    intraday_all: dict[str, pd.DataFrame],
    ts: pd.Timestamp,
) -> list[str]:
    """Backtest-local mirror of `scanner.intraday_refresh`.

    Re-ranks pre-market candidates using same-day 15-min bars up to (and including)
    `ts`, then returns the top-N symbols. This matches what the live bot does each
    scan cycle, so backtest signals come from the same narrowed universe as live.
    """
    today = ts.date()
    refreshed: list[scanner.Candidate] = []
    for c in premarket:
        df_sym = intraday_all.get(c.symbol)
        if df_sym is None:
            continue
        window = df_sym.loc[:ts]
        today_df = window[window.index.date == today]
        if today_df.empty:
            continue
        today_vol = float(today_df["volume"].sum())
        rng = (float(today_df["high"].max()) - float(today_df["low"].min()))
        today_range_pct = rng / c.price * 100.0 if c.price else 0.0
        rel_vol = today_vol / c.avg_volume if c.avg_volume else 0.0
        refreshed.append(scanner.Candidate(
            symbol=c.symbol, price=float(today_df["close"].iloc[-1]),
            gap_pct=c.gap_pct, rel_volume=rel_vol, prior_close=c.prior_close,
            avg_volume=c.avg_volume, range_pct=today_range_pct,
        ))
    refreshed.sort(key=lambda x: x.score(), reverse=True)
    return [r.symbol for r in refreshed[: config.SCANNER.top_n_intraday]]


def _spy_n_day_return(spy_daily: pd.DataFrame, d: pd.Timestamp.date, n: int) -> float | None:
    """SPY's N-day % return as of the close BEFORE day `d`. None if not enough history."""
    if spy_daily is None or spy_daily.empty:
        return None
    idx = spy_daily.index
    mask = (idx.tz_convert("UTC").date if idx.tz is not None else idx.date) < d
    prior = spy_daily[mask]
    if len(prior) < n + 1:
        return None
    p_now = float(prior["close"].iloc[-1])
    p_then = float(prior["close"].iloc[-1 - n])
    return (p_now - p_then) / p_then * 100.0


def run_backtest(months: int, etfs_only: bool, be_trigger_r: float | None = None,
                 trend_filter: bool = False, orb_swing: bool = False) -> tuple[list[BTTrade], dict]:
    end = datetime.now(pytz.UTC) - timedelta(days=1)
    start = end - timedelta(days=months * 31)

    if etfs_only:
        universe = sorted(set(config.SCANNER.etf_universe))
    else:
        universe = scanner.build_universe()
    log.info("Universe=%d, window=%s -> %s", len(universe), start.date(), end.date())

    client = _client()

    log.info("Fetching daily bars...")
    daily_all = _fetch(client, universe, TimeFrame(1, TimeFrameUnit.Day), start, end)
    log.info("Fetching 15-min bars...")
    intraday_all = _fetch(
        client, universe, TimeFrame(15, TimeFrameUnit.Minute),
        start - timedelta(days=5), end,
    )

    # Normalize all intraday DataFrames to ET-indexed, RTH only
    for sym, df in list(intraday_all.items()):
        df = df.copy()
        df.index = _to_et(pd.DatetimeIndex(df.index))
        intraday_all[sym] = _rth_only(df)

    spy_intra = intraday_all.get(config.REGIME.spy_symbol)
    if spy_intra is None or spy_intra.empty:
        raise RuntimeError("SPY 15-min bars missing — cannot run regime detection.")

    trading_days = sorted({ts.date() for ts in spy_intra.index})
    log.info("Simulating %d trading days", len(trading_days))

    state = BTState(equity=config.RISK.account_size)
    starting_equity = state.equity

    daily_counts = {"has_data": 0, "hist_ok": 0, "price_ok": 0, "vol_ok": 0}
    signals_seen = 0
    signals_taken = 0

    spy_daily = daily_all.get(config.REGIME.spy_symbol)

    for d in trading_days:
        # SPY prior close for gap-override regime check
        spy_prior_close = None
        if spy_daily is not None:
            idx = spy_daily.index
            mask = (idx.tz_convert("UTC").date if idx.tz is not None else idx.date) < d
            prior = spy_daily[mask]
            if not prior.empty:
                spy_prior_close = float(prior["close"].iloc[-1])

        # Scanner: daily bars strictly before d
        daily_slice: dict[str, pd.DataFrame] = {}
        for sym, df in daily_all.items():
            idx = df.index
            if idx.tz is not None:
                mask = idx.tz_convert("UTC").date < d
            else:
                mask = idx.date < d
            sub = df[mask]
            if not sub.empty:
                daily_slice[sym] = sub

        candidates, counts = scanner.rank_candidates(daily_slice)
        shortlist = scanner.shortlist_with_etfs(candidates)
        for k in daily_counts:
            daily_counts[k] += counts.get(k, 0)

        # Trend filter: SPY's N-day % return ending the day before `d`.
        spy_n_day = None
        if trend_filter:
            spy_n_day = _spy_n_day_return(
                daily_all.get(config.REGIME.spy_symbol), d,
                config.STRATEGY.trend_filter_lookback_days,
            )

        # Today's bars (per symbol)
        spy_today = spy_intra[spy_intra.index.date == d]
        if spy_today.empty:
            continue

        # Daily starting equity for risk gate
        day_start_equity = state.equity

        for ts in spy_today.index:
            # 1) Check exits on open positions using the bar with this ts
            for sym in list(state.positions.keys()):
                df_sym = intraday_all.get(sym)
                if df_sym is None:
                    continue
                if ts not in df_sym.index:
                    continue
                bar = df_sym.loc[ts]
                pos = state.positions[sym]
                # Don't exit on the entry bar itself
                if ts <= pos.entry_ts:
                    continue
                result = _check_exit(pos, bar)
                if result:
                    _close_position(state, sym, result[0], ts, result[1])
                else:
                    _maybe_arm_be(pos, bar, be_trigger_r)

            # 2) EOD flatten at 15:55
            if ts.time() >= FLATTEN_T:
                # Regime at the close, for swing-carry decision.
                rg_close = regime_mod.detect(spy_today.loc[:ts], prior_close=spy_prior_close)
                for sym in list(state.positions.keys()):
                    df_sym = intraday_all.get(sym)
                    if df_sym is None or ts not in df_sym.index:
                        continue
                    close_px = float(df_sym.loc[ts]["close"])
                    pos = state.positions[sym]
                    # ORB+swing: let strong, trend-aligned winners ride overnight.
                    if orb_swing:
                        risk = abs(pos.entry - pos.stop_orig)
                        if risk > 0:
                            r_now = ((close_px - pos.entry) if pos.side == "long"
                                     else (pos.entry - close_px)) / risk
                            aligned = ((pos.side == "long" and rg_close.label == "trend_up") or
                                       (pos.side == "short" and rg_close.label == "trend_down"))
                            if r_now >= 1.5 and aligned:
                                continue  # hold overnight; bracket stop/target still active
                    _close_position(state, sym, close_px, ts, "eod")
                continue  # no new entries after flatten

            # 3) Regime (with gap-override using prior SPY close)
            spy_so_far = spy_today.loc[:ts]
            rg = regime_mod.detect(spy_so_far, prior_close=spy_prior_close)
            if rg.label == "neutral":
                continue

            # 4) Risk gate
            if len(state.positions) >= config.RISK.max_concurrent_positions:
                continue
            day_pnl_pct = (state.equity - day_start_equity) / day_start_equity if day_start_equity else 0
            if day_pnl_pct <= -config.RISK.max_daily_loss_pct:
                continue

            # 5) Pick the right universe for the regime, then evaluate signals.
            #    trend -> ORB on intraday-refreshed top-N. range -> reversal on full shortlist.
            if rg.label == "range":
                shortlist_syms = [c.symbol for c in shortlist]
            else:
                shortlist_syms = _intraday_refresh_bt(shortlist, intraday_all, ts)
            for sym in shortlist_syms:
                if sym in state.positions:
                    continue
                df_sym = intraday_all.get(sym)
                if df_sym is None:
                    continue
                window = _slice_until(df_sym, ts)
                if len(window) < 20:
                    continue
                sig = strategy.evaluate(sym, window, rg)
                if sig is None:
                    continue

                # Trend filter: block reversal trades that fade a strong macro move.
                if trend_filter and sig.setup == "reversal" and spy_n_day is not None:
                    thr = config.STRATEGY.trend_filter_threshold_pct
                    if sig.side == "sell" and spy_n_day >= thr:
                        continue  # don't short into an N-day uptrend
                    if sig.side == "buy" and spy_n_day <= -thr:
                        continue  # don't long into an N-day downtrend

                signals_seen += 1

                # Find NEXT bar for entry fill
                future = df_sym.loc[ts:]
                if len(future) < 2:
                    continue
                next_bar = future.iloc[1]
                next_ts = future.index[1]
                # Must still be same trading day
                if next_ts.date() != d or next_ts.time() >= FLATTEN_T:
                    continue
                entry_px = float(next_bar["open"])

                # Re-size based on actual entry (stop/target stay where strategy placed them)
                trade = size_trade(
                    symbol=sig.symbol, side=sig.side, entry=entry_px,
                    stop=sig.stop, target=sig.target, equity=state.equity,
                )
                if trade is None:
                    continue

                state.positions[sym] = BTPosition(
                    symbol=sym,
                    side="long" if sig.side == "buy" else "short",
                    qty=trade.qty, entry=entry_px,
                    stop=sig.stop, target=sig.target,
                    entry_ts=next_ts, setup=sig.setup, reason=sig.reason,
                    stop_orig=sig.stop,
                )
                signals_taken += 1

                if len(state.positions) >= config.RISK.max_concurrent_positions:
                    break

    # Any stragglers (shouldn't be any, but safety)
    if state.positions:
        last_ts = spy_intra.index[-1]
        for sym in list(state.positions.keys()):
            df_sym = intraday_all.get(sym)
            if df_sym is None or df_sym.empty:
                continue
            _close_position(state, sym, float(df_sym.iloc[-1]["close"]), df_sym.index[-1], "eod")

    # Benchmark: SPY buy & hold over window
    spy_daily = daily_all.get(config.REGIME.spy_symbol)
    bench_pct = None
    if spy_daily is not None and len(spy_daily) >= 2:
        bench_pct = (float(spy_daily["close"].iloc[-1]) - float(spy_daily["close"].iloc[0])) \
            / float(spy_daily["close"].iloc[0]) * 100.0

    meta = {
        "start": start.date().isoformat(),
        "end": end.date().isoformat(),
        "trading_days": len(trading_days),
        "starting_equity": starting_equity,
        "ending_equity": state.equity,
        "universe_size": len(universe),
        "daily_funnel": daily_counts,
        "signals_seen": signals_seen,
        "signals_taken": signals_taken,
        "bench_pct": bench_pct,
    }
    return state.trades, meta


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _write_csv(trades: list[BTTrade], path: Path) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "entry_ts", "exit_ts", "symbol", "side", "setup", "qty",
            "entry", "stop", "target", "exit", "exit_reason",
            "pnl", "r_multiple", "reason",
        ])
        for t in trades:
            w.writerow([
                t.entry_ts.isoformat(), t.exit_ts.isoformat(), t.symbol, t.side, t.setup,
                t.qty, f"{t.entry:.2f}", f"{t.stop:.2f}", f"{t.target:.2f}",
                f"{t.exit:.2f}", t.exit_reason,
                f"{t.pnl:.2f}", f"{t.r_multiple:.2f}", t.reason,
            ])


def _write_report(trades: list[BTTrade], meta: dict, path: Path) -> None:
    n = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    total_pnl = sum(t.pnl for t in trades)
    win_rate = (len(wins) / n * 100.0) if n else 0.0
    avg_r = (sum(t.r_multiple for t in trades) / n) if n else 0.0
    avg_win = (sum(t.pnl for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t.pnl for t in losses) / len(losses)) if losses else 0.0
    expectancy = (avg_r) if n else 0.0

    ret_pct = (meta["ending_equity"] - meta["starting_equity"]) / meta["starting_equity"] * 100.0
    bench = meta.get("bench_pct")

    # By-setup breakdown
    by_setup: dict[str, list[BTTrade]] = {}
    for t in trades:
        by_setup.setdefault(t.setup, []).append(t)

    by_reason: dict[str, int] = {}
    for t in trades:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1

    lines = [
        f"# Backtest {meta['start']} → {meta['end']}",
        "",
        f"Universe: {meta['universe_size']} symbols · Trading days: {meta['trading_days']}",
        "",
        "## Performance",
        f"- Starting equity: **${meta['starting_equity']:,.2f}**",
        f"- Ending equity:   **${meta['ending_equity']:,.2f}**",
        f"- Net P&L:         **${total_pnl:+,.2f}** ({ret_pct:+.2f}%)",
    ]
    if bench is not None:
        lines.append(f"- SPY buy-hold:    **{bench:+.2f}%**  (alpha {ret_pct - bench:+.2f}%)")
    lines += [
        "",
        "## Trade stats",
        f"- Total trades: **{n}**",
        f"- Win rate: **{win_rate:.1f}%**  ({len(wins)}W / {len(losses)}L)",
        f"- Avg R-multiple: **{avg_r:+.2f}R**",
        f"- Avg win: ${avg_win:+,.2f} · Avg loss: ${avg_loss:+,.2f}",
        f"- Exit reasons: {by_reason}",
        "",
        "## By setup",
    ]
    for setup, ts in sorted(by_setup.items()):
        w = sum(1 for t in ts if t.pnl > 0)
        p = sum(t.pnl for t in ts)
        r = sum(t.r_multiple for t in ts) / len(ts)
        lines.append(
            f"- **{setup}**: {len(ts)} trades · {w/len(ts)*100:.1f}% win · ${p:+,.2f} · {r:+.2f}R avg"
        )

    lines += [
        "",
        "## Funnel (scanner)",
        f"- Daily summed: {meta['daily_funnel']}",
        f"- Signals generated: {meta['signals_seen']} · Signals taken: {meta['signals_taken']}",
        "",
        "## First 20 trades",
        "",
        "| When | Symbol | Setup | Side | Qty | Entry | Exit | Reason | P&L | R |",
        "|------|--------|-------|------|-----|-------|------|--------|-----|---|",
    ]
    for t in trades[:20]:
        lines.append(
            f"| {t.entry_ts:%Y-%m-%d %H:%M} | {t.symbol} | {t.setup} | {t.side} | {t.qty} | "
            f"{t.entry:.2f} | {t.exit:.2f} | {t.exit_reason} | ${t.pnl:+,.2f} | {t.r_multiple:+.2f} |"
        )

    path.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Walk-forward backtest")
    parser.add_argument("--months", type=int, default=6, help="Lookback in months (default 6)")
    parser.add_argument("--etfs-only", action="store_true", help="Restrict universe to ETFs")
    parser.add_argument("--be-trigger", type=float, default=None,
                        help="Move stop to break-even after price reaches +N×R (e.g. 0.5 or 1.0). Default: off.")
    parser.add_argument("--trend-filter", action="store_true",
                        help="Block reversal shorts/longs that fade a strong N-day SPY trend.")
    parser.add_argument("--orb-swing", action="store_true",
                        help="Let strong (>=1.5R), trend-aligned winners ride overnight.")
    args = parser.parse_args()

    trades, meta = run_backtest(
        months=args.months, etfs_only=args.etfs_only,
        be_trigger_r=args.be_trigger, trend_filter=args.trend_filter,
        orb_swing=args.orb_swing,
    )
    meta["be_trigger_r"] = args.be_trigger
    meta["trend_filter"] = args.trend_filter
    meta["orb_swing"] = args.orb_swing

    suffix = ""
    if args.be_trigger is not None:
        suffix += f"_be{args.be_trigger:g}R"
    if args.trend_filter:
        suffix += "_tf"
    if args.orb_swing:
        suffix += "_swing"
    md_path = config.JOURNAL_DIR / f"backtest_{meta['start']}_to_{meta['end']}{suffix}.md"
    csv_path = config.JOURNAL_DIR / f"backtest_trades{suffix}.csv"
    _write_report(trades, meta, md_path)
    _write_csv(trades, csv_path)
    log.info("Wrote %s and %s", md_path, csv_path)
    log.info(
        "Summary: %d trades, $%+.2f P&L (%+.2f%%)",
        len(trades),
        meta["ending_equity"] - meta["starting_equity"],
        (meta["ending_equity"] - meta["starting_equity"]) / meta["starting_equity"] * 100.0,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
