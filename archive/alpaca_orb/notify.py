"""Telegram notifications. Silent no-op if token/chat_id are not set."""
from __future__ import annotations

import logging
import os
from typing import Optional

import requests
from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
API_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage" if TOKEN else ""


def enabled() -> bool:
    return bool(TOKEN and CHAT_ID)


def send(text: str) -> None:
    if not enabled():
        return
    try:
        resp = requests.post(
            API_URL,
            data={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": "true"},
            timeout=5,
        )
        if resp.status_code != 200:
            log.warning("Telegram error %d: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        log.warning("Telegram send failed: %s", e)


def startup(equity: float, paper: bool) -> None:
    mode = "PAPER" if paper else "LIVE"
    send(f"*Bot started* ({mode})\nEquity: ${equity:,.2f}")


def order_submitted(
    setup: str, side: str, symbol: str, qty: int,
    entry: float, stop: float, target: float, r_mult: float, reason: str,
) -> None:
    tag = f"[{setup.upper()} {side.upper()}]"
    text = (
        f"{tag} `{symbol}` x{qty}\n"
        f"Entry ~${entry:.2f}  Stop ${stop:.2f}  Target ${target:.2f}\n"
        f"R:R {r_mult:.2f}\n"
        f"_{reason}_"
    )
    send(text)


def order_filled(symbol: str, side: str, qty: float, price: float, order_type: str) -> None:
    text = f"[FILL] {side.upper()} `{symbol}` x{qty:g} @ ${price:.2f} ({order_type})"
    send(text)


def position_closed(symbol: str, pnl: float, pnl_pct: float) -> None:
    tag = "[WIN]" if pnl > 0 else "[LOSS]"
    text = f"{tag} `{symbol}` realized ${pnl:+,.2f} ({pnl_pct:+.2f}%)"
    send(text)


def regime_change(label: str, dev_pct: float) -> None:
    send(f"[REGIME] {label}  SPY dev {dev_pct:+.2f}%")


def daily_loss_limit() -> None:
    send("[HALT] Daily loss limit hit — no new entries today.")


def session_summary(
    start_equity: float, end_equity: float, n_trades: int, bench_pct: Optional[float],
) -> None:
    pnl = end_equity - start_equity
    pct = (pnl / start_equity * 100.0) if start_equity else 0.0
    lines = [
        "*Session end*",
        f"P&L: ${pnl:+,.2f} ({pct:+.2f}%)",
        f"Trades: {n_trades}",
    ]
    if bench_pct is not None:
        lines.append(f"SPY: {bench_pct:+.2f}%  Alpha: {pct - bench_pct:+.2f}%")
    send("\n".join(lines))


def weekly_summary(week_pnl: float, days_traded: int, total_fills: int, end_equity: float) -> None:
    sign = "WEEK +" if week_pnl >= 0 else "WEEK "
    lines = [
        f"*{sign}${week_pnl:,.2f}*",
        f"Equity: ${end_equity:,.2f}",
        f"Active days: {days_traded}/5 · Fills: {total_fills}",
    ]
    if total_fills == 0:
        lines.append("_Bot was idle all week (no setups triggered)._")
    send("\n".join(lines))


def error(msg: str) -> None:
    send(f"[ERROR] {msg}")
