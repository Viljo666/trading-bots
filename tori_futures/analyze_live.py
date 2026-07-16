"""Analyze live_log.jsonl from the paper forward test.

Compares live performance against backtest expectations and reports:
  - trade count, win rate, total $, profit factor, max drawdown
  - per-trade table
  - cumulative PnL curve (text plot)
  - comparison to backtest reference numbers
  - bot health: uptime, errors, connection events

Run: ../.venv/bin/python analyze_live.py
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

LOG_FILE = Path(__file__).parent / "live_log.jsonl"

# Reference: from walk-forward validation on unseen 20-day test window
BACKTEST_REFERENCE = {
    "config": "RTH | noEMA | anySlope | pivotStop",
    "test_trades": 6,
    "test_win_rate": 50.0,
    "test_dollars": 1888.0,
    "test_profit_factor": 1.86,
    "test_window_days": 20,
}


def load_events() -> list[dict]:
    if not LOG_FILE.exists():
        return []
    events = []
    with LOG_FILE.open() as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    return events


def first_priming_per_symbol(events: list[dict]) -> dict[str, pd.Timestamp]:
    """For each symbol, the earliest priming timestamp (`last_bar_ts` of the first
    `primed` event we ever logged for that symbol). Trades with `entry_time` at
    or before this point are pre-bot-live signals from the strategy reproducing
    history — not real bot decisions — and must be filtered out of any P&L report.
    """
    out: dict[str, pd.Timestamp] = {}
    for e in events:
        if e.get("kind") != "primed":
            continue
        sym = e.get("symbol")
        ts = e.get("last_bar_ts")
        if not sym or not ts:
            continue
        ts = pd.Timestamp(ts)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        if sym not in out or ts < out[sym]:
            out[sym] = ts
    return out


def trades_df(events: list[dict]) -> pd.DataFrame:
    rows = [e for e in events if e.get("kind") == "signal_close"]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True)
    df["exit_time"] = pd.to_datetime(df["exit_time"], utc=True)
    df["duration_min"] = (df["exit_time"] - df["entry_time"]).dt.total_seconds() / 60
    if "symbol" not in df.columns:
        df["symbol"] = "ES"   # back-compat for logs from single-symbol era

    # filter pre-priming history reproductions and "end_of_data" still-open positions
    primings = first_priming_per_symbol(events)
    if primings:
        keep = df.apply(
            lambda r: (r["symbol"] in primings) and (r["entry_time"] > primings[r["symbol"]]),
            axis=1,
        )
        df = df[keep]
    if "exit_reason" in df.columns:
        df = df[df["exit_reason"] != "end_of_data"]

    return df.reset_index(drop=True)


def summary(df: pd.DataFrame) -> dict:
    if df.empty:
        return {}
    wins = df[df["pnl_points"] > 0]
    losses = df[df["pnl_points"] <= 0]
    gross_win = wins["pnl_points"].sum()
    gross_loss = abs(losses["pnl_points"].sum())
    cum = df["pnl_points"].cumsum()
    peak = cum.cummax()
    dd = (cum - peak).min()
    return {
        "trades": len(df),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(100 * len(wins) / len(df), 1),
        "total_points": round(df["pnl_points"].sum(), 2),
        "total_dollars": round(df["pnl_dollars"].sum(), 2),
        "avg_win_points": round(wins["pnl_points"].mean(), 2) if len(wins) else 0.0,
        "avg_loss_points": round(losses["pnl_points"].mean(), 2) if len(losses) else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else float("inf"),
        "max_drawdown_points": round(dd, 2),
        "avg_duration_min": round(df["duration_min"].mean(), 1),
    }


def text_equity_curve(df: pd.DataFrame, width: int = 60) -> str:
    if df.empty:
        return "(no trades yet)"
    cum = df["pnl_points"].cumsum().tolist()
    lo, hi = min(0, min(cum)), max(0, max(cum))
    if hi == lo:
        return "(flat — all zeros)"
    lines = []
    for i, v in enumerate(cum):
        pos = int((v - lo) / (hi - lo) * (width - 1))
        zero_pos = int((0 - lo) / (hi - lo) * (width - 1))
        row = [" "] * width
        row[zero_pos] = "|"
        row[pos] = "*" if v >= 0 else "x"
        lines.append(f"{i+1:3d}  {v:+7.2f}  {''.join(row)}")
    return "\n".join(lines)


def health(events: list[dict]) -> dict:
    if not events:
        return {"status": "no log file"}
    starts = [e for e in events if e.get("kind") == "startup"]
    errors = [e for e in events if e.get("kind") == "error"]
    ready = next((e for e in events if e.get("kind") == "ready"), None)
    first_ts = pd.to_datetime(events[0]["ts"])
    last_ts = pd.to_datetime(events[-1]["ts"])
    now = datetime.now(timezone.utc)
    last_age_min = (now - last_ts.to_pydatetime().replace(tzinfo=timezone.utc)).total_seconds() / 60
    return {
        "log_first_event": str(first_ts),
        "log_last_event": str(last_ts),
        "minutes_since_last_event": round(last_age_min, 1),
        "startups": len(starts),
        "errors": len(errors),
        "config": ready["config"] if ready else "n/a",
    }


def main() -> None:
    events = load_events()
    if not events:
        print(f"No log at {LOG_FILE}. Has live.py run yet?")
        return

    print("=" * 70)
    print("TORI FUTURES BOT — LIVE FORWARD TEST ANALYSIS")
    print("=" * 70)

    h = health(events)
    print("\n--- Bot health ---")
    for k, v in h.items():
        print(f"  {k:30s} {v}")
    if h.get("minutes_since_last_event", 0) > 120:
        print("  ⚠  No events in >2h — bot may be stopped or Gateway disconnected.")

    primings = first_priming_per_symbol(events)
    if primings:
        print("\n--- First priming per symbol (only trades after this count) ---")
        for sym in sorted(primings):
            print(f"  {sym:6s} {primings[sym]}")

    # raw vs filtered counts so the user sees what got dropped
    raw_signal_closes = sum(1 for e in events if e.get("kind") == "signal_close")
    raw_post_priming = 0
    raw_real_closed = 0
    for e in events:
        if e.get("kind") != "signal_close":
            continue
        sym = e.get("symbol", "ES")
        ts = pd.Timestamp(e.get("entry_time"))
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        primed_at = primings.get(sym)
        if primed_at is not None and ts > primed_at:
            raw_post_priming += 1
            if e.get("exit_reason") != "end_of_data":
                raw_real_closed += 1
    print(f"\n--- Trade log filtering ---")
    print(f"  total signal_close events       {raw_signal_closes}")
    print(f"  after first priming             {raw_post_priming}")
    print(f"  closed (excluding end_of_data)  {raw_real_closed}  <-- these are the real bot trades")

    errs = [e for e in events if e.get("kind") == "error"]
    if errs:
        print(f"\n--- Errors ({len(errs)}) ---")
        for e in errs[-5:]:
            print(f"  [{e['ts']}] {e.get('msg')}")

    df = trades_df(events)

    if df.empty:
        print("\n--- Live summary ---")
        print("  No completed trades yet.")
    else:
        # per-symbol breakdown
        print("\n--- Live summary per symbol ---")
        for sym, sub in df.groupby("symbol"):
            print(f"\n  [{sym}]")
            for k, v in summary(sub).items():
                print(f"    {k:28s} {v}")

        print("\n--- Live summary (combined, $ basis) ---")
        combined = summary(df)
        for k, v in combined.items():
            print(f"  {k:30s} {v}")

    print("\n--- Backtest reference (ES, OOS 20-day window) ---")
    for k, v in BACKTEST_REFERENCE.items():
        print(f"  {k:30s} {v}")

    if not df.empty:
        es_df = df[df["symbol"] == "ES"]
        if not es_df.empty:
            live = summary(es_df)
            print("\n--- Live ES vs backtest reference ---")
            pf_delta = live["profit_factor"] - BACKTEST_REFERENCE["test_profit_factor"]
            wr_delta = live["win_rate_pct"] - BACKTEST_REFERENCE["test_win_rate"]
            print(f"  profit_factor diff:  {pf_delta:+.2f}  (live {live['profit_factor']} vs ref {BACKTEST_REFERENCE['test_profit_factor']})")
            print(f"  win_rate diff:       {wr_delta:+.1f}pp (live {live['win_rate_pct']} vs ref {BACKTEST_REFERENCE['test_win_rate']})")

        print("\n--- Trades ---")
        cols = ["symbol", "direction", "entry_time", "entry_price", "exit_time", "exit_price",
                "pnl_points", "pnl_dollars", "duration_min", "exit_reason"]
        with pd.option_context("display.max_rows", None, "display.width", 200):
            print(df[cols].to_string(index=False))

        print("\n--- Equity curve (combined $) ---")
        # switch the equity plot to dollars so ES and NQ are comparable
        dollars_df = df.copy()
        dollars_df["pnl_points"] = dollars_df["pnl_dollars"]  # reuse the plot helper
        print(text_equity_curve(dollars_df))


if __name__ == "__main__":
    main()
