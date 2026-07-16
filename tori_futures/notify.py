"""Telegram notifications for the Tori futures bot.

Reuses TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the parent project's .env.
Silent no-op if creds are missing — never crashes the trading loop.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import requests
from dotenv import load_dotenv

# .env lives in parent dir alongside the Alpaca bot
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

log = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
API_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage" if TOKEN else ""


def enabled() -> bool:
    return bool(TOKEN and CHAT_ID)


def send(text: str) -> None:
    """Send a Markdown-formatted message. Failures are logged, never raised."""
    if not enabled():
        return
    try:
        resp = requests.post(
            API_URL,
            data={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "Markdown",
                "disable_web_page_preview": "true",
            },
            timeout=5,
        )
        if resp.status_code != 200:
            log.warning("Telegram error %d: %s", resp.status_code, resp.text[:200])
    except Exception as e:
        log.warning("Telegram send failed: %s", e)


# ---------- event-specific formatters ----------

def startup(symbols: list[str]) -> None:
    send(f"*Tori bot started*\nWatching: {', '.join(symbols)}")


def position_opened(symbol: str, direction: str, entry: float, stop: float, config: str) -> None:
    arrow = "📈 LONG" if direction == "long" else "📉 SHORT"
    risk_pts = abs(entry - stop)
    text = (
        f"*{arrow} {symbol}*\n"
        f"Entry: `{entry:.2f}`\n"
        f"Stop:  `{stop:.2f}`  ({risk_pts:.2f} pts risk)\n"
        f"_{config}_"
    )
    send(text)


def position_closed(symbol: str, direction: str, entry: float, exit_price: float,
                    pnl_points: float, pnl_dollars: float, reason: str) -> None:
    tag = "✅ WIN" if pnl_dollars > 0 else "❌ LOSS"
    text = (
        f"*{tag} {symbol}* ({direction})\n"
        f"Entry: `{entry:.2f}` → Exit: `{exit_price:.2f}`\n"
        f"P&L: *{pnl_points:+.2f} pts* (${pnl_dollars:+,.2f})\n"
        f"_{reason}_"
    )
    send(text)


def error(symbol: Optional[str], msg: str) -> None:
    prefix = f"*[{symbol}]* " if symbol else ""
    send(f"⚠️ {prefix}error: {msg[:300]}")


def shutdown() -> None:
    send("*Tori bot stopped*")


def disconnected(reason: str = "") -> None:
    suffix = f"\n_{reason}_" if reason else ""
    send(
        "🔌 *Lost connection to IB Gateway*\n"
        "Please log back into IB Gateway (paper mode). "
        "The bot will auto-reconnect once Gateway is up.{}".format(suffix)
    )


def stale_bars(symbol: str, minutes: int) -> None:
    send(
        f"⏱ *No bars for {symbol} in {minutes} min*\n"
        "Data feed appears stalled. Bot will restart to recover."
    )


def gateway_unreachable(attempts: int) -> None:
    send(
        f"🔌 *IB Gateway not reachable*\n"
        f"Bot has tried {attempts} times to connect. "
        f"Please log into IB Gateway (paper mode).\n"
        f"Bot keeps retrying every 30 sec."
    )
