"""Once-daily long-only stock momentum shadow portfolio.

This runner places NO broker orders. It uses completed Alpaca daily bars, keeps
an independent virtual $100k ledger, and models fills at the next session open
with 5 bps slippage each way. It can safely run beside every live/paper bot.
"""
from __future__ import annotations

import csv
import fcntl
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pytz

import config
import indicators
import notify
import scanner
from strategy_lab import (
    ATR_PERIOD,
    ATR_STOP_MULT,
    BREAKOUT_SMA,
    MAX_POSITIONS,
    NEAR_HIGH_LOOKBACK,
    NEAR_HIGH_PCT,
    RISK_PCT,
    RS_LOOKBACK,
    SLIPPAGE_BPS,
    TREND_SMA,
    _client,
    _fetch_daily,
)

NAME = "momentum-shadow"
INITIAL_EQUITY = 100_000.0
MAX_POSITION_PCT = 0.25
LOOKBACK_CALENDAR_DAYS = 450

STATE_FILE = config.DATA_DIR / "momentum_shadow_state.json"
LOCK_FILE = config.DATA_DIR / "momentum_shadow.lock"
TRADES_FILE = config.JOURNAL_DIR / "momentum_shadow_trades.csv"
LOG_FILE = config.LOG_DIR / "momentum-shadow.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(NAME)


def _acquire_lock():
    handle = LOCK_FILE.open("w")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    handle.write(str(datetime.now(pytz.UTC).isoformat()))
    handle.flush()
    return handle


def _load_state() -> dict[str, Any] | None:
    try:
        state = json.loads(STATE_FILE.read_text())
        return state if isinstance(state, dict) and state.get("version") == 1 else None
    except (OSError, json.JSONDecodeError):
        return None


def _save_state(state: dict[str, Any]) -> None:
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    temporary.replace(STATE_FILE)


def _features(daily: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    for sym, raw in daily.items():
        if len(raw) < RS_LOOKBACK + 12:
            continue
        frame = raw.copy().sort_index()
        frame["sma_trend"] = frame["close"].rolling(TREND_SMA).mean()
        frame["sma_break"] = frame["close"].rolling(BREAKOUT_SMA).mean()
        frame["hi_n"] = frame["high"].rolling(NEAR_HIGH_LOOKBACK).max()
        frame["atr"] = indicators.atr(frame, ATR_PERIOD)
        frame["ret_rs"] = frame["close"].pct_change(RS_LOOKBACK)
        out[sym] = frame
    return out


def _rank_candidates(
    feats: dict[str, pd.DataFrame], spy: pd.DataFrame, session: pd.Timestamp,
    excluded: set[str], universe: list[str],
) -> list[dict[str, Any]]:
    spy_rs = spy["close"].pct_change(RS_LOOKBACK)
    spy_value = float(spy_rs.loc[session]) if session in spy_rs.index and pd.notna(spy_rs.loc[session]) else 0.0
    ranked: list[dict[str, Any]] = []
    for sym in universe:
        if sym == "SPY" or sym in excluded:
            continue
        frame = feats.get(sym)
        if frame is None or session not in frame.index:
            continue
        row = frame.loc[session]
        if pd.isna(row["sma_trend"]) or pd.isna(row["atr"]) or float(row["atr"]) <= 0:
            continue
        idx = frame.index.get_loc(session)
        if not isinstance(idx, int) or idx < 10:
            continue
        sma_then = float(frame["sma_trend"].iloc[idx - 10])
        if np.isnan(sma_then):
            continue
        close = float(row["close"])
        rs = float(row["ret_rs"]) if pd.notna(row["ret_rs"]) else -1.0
        if (
            close > float(row["sma_trend"])
            and float(row["sma_trend"]) > sma_then
            and close >= float(row["hi_n"]) * (1.0 - NEAR_HIGH_PCT / 100.0)
            and rs > spy_value
        ):
            ranked.append({"symbol": sym, "rs": rs, "signal_atr": float(row["atr"]),
                           "signal_date": session.date().isoformat()})
    return sorted(ranked, key=lambda x: (x["rs"], x["symbol"]), reverse=True)


def _append_trade(trade: dict[str, Any]) -> None:
    fields = ["symbol", "entry_date", "exit_date", "qty", "entry", "exit", "pnl", "pnl_pct", "reason"]
    new_file = not TRADES_FILE.exists()
    with TRADES_FILE.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        if new_file:
            writer.writeheader()
        writer.writerow({key: trade[key] for key in fields})


def _close_position(
    state: dict[str, Any], symbol: str, raw_price: float, session: pd.Timestamp,
    reason: str, completed: list[dict[str, Any]],
) -> None:
    pos = state["positions"].pop(symbol)
    exit_price = raw_price * (1.0 - SLIPPAGE_BPS / 10_000.0)
    proceeds = int(pos["qty"]) * exit_price
    pnl = (exit_price - float(pos["entry"])) * int(pos["qty"])
    state["cash"] += proceeds
    trade = {
        "symbol": symbol,
        "entry_date": pos["entry_date"],
        "exit_date": session.date().isoformat(),
        "qty": int(pos["qty"]),
        "entry": round(float(pos["entry"]), 4),
        "exit": round(exit_price, 4),
        "pnl": round(pnl, 2),
        "pnl_pct": round((exit_price / float(pos["entry"]) - 1.0) * 100.0, 3),
        "reason": reason,
    }
    completed.append(trade)
    _append_trade(trade)


def _equity(state: dict[str, Any], feats: dict[str, pd.DataFrame], session: pd.Timestamp) -> float:
    value = float(state["cash"])
    for sym, pos in state["positions"].items():
        frame = feats.get(sym)
        price = float(frame.loc[session]["close"]) if frame is not None and session in frame.index else float(pos["entry"])
        value += int(pos["qty"]) * price
    return value


def _process_session(
    state: dict[str, Any], session: pd.Timestamp, feats: dict[str, pd.DataFrame],
    spy: pd.DataFrame, universe: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    completed: list[dict[str, Any]] = []
    opened: list[str] = []

    # Positions already held can gap below their protective shadow stop.
    for sym in list(state["positions"]):
        frame = feats.get(sym)
        if frame is not None and session in frame.index:
            opening = float(frame.loc[session]["open"])
            if opening <= float(state["positions"][sym]["stop"]):
                _close_position(state, sym, opening, session, "gap_stop", completed)

    sizing_equity = float(state.get("equity", INITIAL_EQUITY))
    pending = list(state.get("pending", []))
    state["pending"] = []
    for signal in pending:
        sym = signal["symbol"]
        if len(state["positions"]) >= MAX_POSITIONS or sym in state["positions"]:
            continue
        frame = feats.get(sym)
        if frame is None or session not in frame.index:
            continue
        entry = float(frame.loc[session]["open"]) * (1.0 + SLIPPAGE_BPS / 10_000.0)
        stop = entry - ATR_STOP_MULT * float(signal["signal_atr"])
        risk_per_share = entry - stop
        qty = min(
            int((sizing_equity * RISK_PCT) // risk_per_share),
            int((sizing_equity * MAX_POSITION_PCT) // entry),
            int(float(state["cash"]) // entry),
        ) if entry > 0 and risk_per_share > 0 else 0
        if qty <= 0:
            continue
        state["cash"] -= qty * entry
        state["positions"][sym] = {
            "qty": qty, "entry": entry, "entry_date": session.date().isoformat(),
            "stop": stop, "highest_close": entry,
        }
        opened.append(sym)

    # Conservative daily-bar sequencing: stop wins before a close-based exit.
    for sym in list(state["positions"]):
        frame = feats.get(sym)
        if frame is None or session not in frame.index:
            continue
        row = frame.loc[session]
        pos = state["positions"][sym]
        if float(row["low"]) <= float(pos["stop"]):
            _close_position(state, sym, float(pos["stop"]), session, "stop", completed)
            continue
        close = float(row["close"])
        if pd.notna(row["sma_break"]) and close < float(row["sma_break"]):
            _close_position(state, sym, close, session, "trend_break", completed)
            continue
        pos["highest_close"] = max(float(pos["highest_close"]), close)
        if pd.notna(row["atr"]):
            pos["stop"] = max(float(pos["stop"]), close - ATR_STOP_MULT * float(row["atr"]))

    state["equity"] = _equity(state, feats, session)
    slots = MAX_POSITIONS - len(state["positions"])
    excluded = set(state["positions"])
    candidates = _rank_candidates(feats, spy, session, excluded, universe)
    state["pending"] = candidates[:max(0, slots)]
    state["last_processed_date"] = session.date().isoformat()
    state.setdefault("equity_history", []).append({
        "date": session.date().isoformat(), "equity": round(float(state["equity"]), 2)
    })
    state["equity_history"] = state["equity_history"][-800:]
    return completed, opened


def _write_journal(
    state: dict[str, Any], session: pd.Timestamp, completed: list[dict[str, Any]], opened: list[str],
    spy_close: float,
) -> Path:
    start_spy = float(state["benchmark_start_price"])
    spy_return = (spy_close / start_spy - 1.0) * 100.0
    strategy_return = (float(state["equity"]) / INITIAL_EQUITY - 1.0) * 100.0
    path = config.JOURNAL_DIR / f"momentum_shadow_{session.date().isoformat()}.md"
    lines = [
        f"# Momentum Shadow — {session.date().isoformat()}", "",
        f"- Equity: **${float(state['equity']):,.2f}** ({strategy_return:+.2f}%)",
        f"- SPY since start: {spy_return:+.2f}%",
        f"- Alpha: {strategy_return - spy_return:+.2f}%",
        f"- Cash: ${float(state['cash']):,.2f}",
        f"- Positions: {len(state['positions'])} · Pending entries: {len(state.get('pending', []))}",
        f"- Mode: shadow only; 5 bps slippage each way", "",
        "## Activity", "",
    ]
    lines.append(f"Opened: {', '.join(opened)}" if opened else "Opened: none")
    if completed:
        for trade in completed:
            lines.append(f"- Closed {trade['symbol']}: ${trade['pnl']:+,.2f} ({trade['reason']})")
    else:
        lines.append("Closed: none")
    lines.extend(["", "## Positions", ""])
    if state["positions"]:
        for sym, pos in sorted(state["positions"].items()):
            lines.append(f"- {sym}: {pos['qty']} shares @ ${pos['entry']:.2f}; stop ${pos['stop']:.2f}")
    else:
        lines.append("_Flat._")
    lines.extend(["", "## Next-open candidates", ""])
    lines.append(", ".join(item["symbol"] for item in state.get("pending", [])) or "_None._")
    path.write_text("\n".join(lines) + "\n")
    return path


def main() -> int:
    lock = _acquire_lock()
    if lock is None:
        log.info("Another momentum-shadow run is active; exiting.")
        return 0
    config.validate()
    end = datetime.now(pytz.UTC) - timedelta(minutes=20)
    start = end - timedelta(days=LOOKBACK_CALENDAR_DAYS)
    universe = scanner.build_universe()
    log.info("Fetching adjusted daily bars for %d symbols", len(universe))
    daily = _fetch_daily(_client(), universe, start, end)
    spy = daily.get("SPY")
    if spy is None or len(spy) < RS_LOOKBACK + 12:
        raise RuntimeError("SPY daily history is unavailable or too short")
    feats = _features(daily)
    sessions = sorted(spy.index)
    latest = sessions[-1]

    state = _load_state()
    if state is None:
        state = {
            "version": 1,
            "created_at": datetime.now(pytz.UTC).isoformat(),
            "cash": INITIAL_EQUITY,
            "equity": INITIAL_EQUITY,
            "positions": {},
            "pending": [],
            "last_processed_date": latest.date().isoformat(),
            "benchmark_start_date": latest.date().isoformat(),
            "benchmark_start_price": float(spy.loc[latest]["close"]),
            "equity_history": [{"date": latest.date().isoformat(), "equity": INITIAL_EQUITY}],
        }
        state["pending"] = _rank_candidates(feats, spy, latest, set(), universe)[:MAX_POSITIONS]
        _save_state(state)
        journal_path = _write_journal(state, latest, [], [], float(spy.loc[latest]["close"]))
        names = ", ".join(item["symbol"] for item in state["pending"]) or "none"
        notify.send(f"*Momentum shadow initialized*\nEquity: $100,000\nNext-open candidates: {names}")
        log.info("Initialized at %s; journal=%s", latest.date(), journal_path)
        return 0

    last_date = pd.Timestamp(state["last_processed_date"]).date()
    new_sessions = [session for session in sessions if session.date() > last_date]
    if not new_sessions:
        log.info("No new completed daily bar after %s", last_date)
        return 0

    all_completed: list[dict[str, Any]] = []
    all_opened: list[str] = []
    journal_path = None
    for session in new_sessions:
        completed, opened = _process_session(state, session, feats, spy, universe)
        all_completed.extend(completed)
        all_opened.extend(opened)
        journal_path = _write_journal(state, session, completed, opened, float(spy.loc[session]["close"]))
        _save_state(state)

    spy_return = (float(spy.loc[new_sessions[-1]]["close"]) / float(state["benchmark_start_price"]) - 1.0) * 100.0
    strategy_return = (float(state["equity"]) / INITIAL_EQUITY - 1.0) * 100.0
    notify.send(
        f"*Momentum shadow* `{new_sessions[-1].date()}`\n"
        f"Equity: ${float(state['equity']):,.2f} ({strategy_return:+.2f}%)\n"
        f"SPY: {spy_return:+.2f}% · Alpha: {strategy_return - spy_return:+.2f}%\n"
        f"Opened: {', '.join(all_opened) or 'none'} · Closed: {len(all_completed)}\n"
        f"Positions: {len(state['positions'])} · Next: {len(state.get('pending', []))}"
    )
    log.info("Processed %d session(s); equity=$%.2f; journal=%s",
             len(new_sessions), float(state["equity"]), journal_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
