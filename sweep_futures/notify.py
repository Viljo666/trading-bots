"""Telegram notifications for the sweep-futures bot. Silent no-op without creds."""
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


def enabled() -> bool:
    return bool(TOKEN and CHAT_ID)


def send(text: str) -> None:
    if not enabled():
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
    send(f"*Sweep bot started* 🌊\nWatching 4h: {', '.join(symbols)}\n"
         "_swing-20 sweep + EMA-200 + 1:3 RR_")


def bracket_placed(symbol: str, sig) -> None:
    arrow = "📈 LONG" if sig.side == "long" else "📉 SHORT"
    send(f"*{arrow} {symbol}* ×{sig.qty}\n"
         f"Entry ~`{sig.entry:.2f}`\n"
         f"Stop `{sig.stop:.2f}` ({sig.risk_pts:.2f} pts)\n"
         f"Target `{sig.target:.2f}` (1:3)\n"
         f"_swept {sig.swept_level:.2f}_")


def exit_filled(symbol: str, kind: str, price: float, pnl_usd: float) -> None:
    tag = "✅ TARGET" if pnl_usd > 0 else ("❌ STOP" if kind == "stop" else "↩️ EXIT")
    send(f"*{tag} {symbol}*\nFill `{price:.2f}`  P&L *${pnl_usd:+,.2f}*")


def error(symbol, msg: str) -> None:
    prefix = f"*[{symbol}]* " if symbol else ""
    send(f"⚠️ {prefix}{msg[:300]}")


def shutdown() -> None:
    send("*Sweep bot stopped*")


def gateway_unreachable(attempts: int) -> None:
    send(f"🔌 *IB Gateway not reachable* (tried {attempts}×). "
         "Log into Gateway (paper). Bot keeps retrying.")


def stale_poll(minutes: int) -> None:
    send(f"⏱ *Sweep bot: no successful poll in {minutes} min* — restarting to recover.")
