"""Telegram notifications for the POC bot. Silent no-op without creds."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
log = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
API_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage" if TOKEN else ""


def send(text: str) -> None:
    if not (TOKEN and CHAT_ID):
        return
    try:
        r = requests.post(API_URL, data={
            "chat_id": CHAT_ID, "text": text,
            "parse_mode": "Markdown", "disable_web_page_preview": "true",
        }, timeout=5)
        if r.status_code != 200:
            log.warning("Telegram error %d: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("Telegram send failed: %s", e)


def startup(symbols: list[str]) -> None:
    send(f"*POC bot started* 🎯\nWatching: {', '.join(symbols)}\n"
         "_prior-day POC continuation, 0.25×ATR stop, 1:3_")


def armed(symbol: str, plan, dry: bool) -> None:
    arrow = "📈 LONG" if plan.side == "long" else "📉 SHORT"
    tag = " (DRY)" if dry else ""
    send(f"*{arrow} {symbol} armed{tag}* ×{plan.qty}\n"
         f"POC entry `{plan.poc:.2f}` (open {plan.day_open:.2f})\n"
         f"Stop `{plan.stop:.2f}` ({plan.risk_pts:.1f} pts)\n"
         f"Target `{plan.target:.2f}` (1:3)")


def no_trade(symbol: str, reason: str) -> None:
    send(f"*{symbol}*: no trade today — _{reason}_")


def cutoff_cancel(symbol: str) -> None:
    send(f"*{symbol}*: 15:00 cutoff — unfilled POC entry cancelled")


def flattened(symbol: str) -> None:
    send(f"*{symbol}*: 15:55 — flattened open position at close")


def error(symbol, msg: str) -> None:
    prefix = f"*[{symbol}]* " if symbol else ""
    send(f"⚠️ {prefix}{msg[:300]}")


def risk_blocked(symbol: str, reason: str) -> None:
    send(f"🛡️ *{symbol}* setup blocked by shared futures risk guard\n_{reason[:250]}_")


def shutdown() -> None:
    send("*POC bot stopped*")


def gateway_unreachable(attempts: int) -> None:
    send(f"🔌 *IB Gateway not reachable* (tried {attempts}×). POC bot keeps retrying.")


def stale_poll(minutes: int) -> None:
    send(f"⏱ *POC bot: no successful poll in {minutes} min* — restarting to recover.")
