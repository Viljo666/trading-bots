"""Telegram notifications. Silent no-op if token/chat_id are unset."""
from __future__ import annotations

import logging
import os

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


def startup(mode: str, symbol: str, timeframe: str, equity: float, params: str) -> None:
    tag = "PAPER" if mode == "paper" else "LIVE"
    send(
        f"*Crypto scalper started* ({tag})\n"
        f"`{symbol}` {timeframe}  Equity: ${equity:,.2f}\n"
        f"_{params}_"
    )


def entry_filled(symbol: str, side: str, qty: float, price: float) -> None:
    arrow = "↑" if side == "long" else "↓"
    send(f"{arrow} *{side.upper()}* `{symbol}` {qty:g} @ ${price:,.4f}")


def position_closed(symbol: str, side: str, qty: float, entry: float, exit_price: float,
                    pnl: float, equity: float, reason: str) -> None:
    tag = "WIN" if pnl > 0 else "LOSS"
    pnl_pct = ((exit_price / entry) - 1) * 100 * (1 if side == "long" else -1)
    send(
        f"*{tag}* `{symbol}` {side} {qty:g}\n"
        f"Entry ${entry:,.4f} → Exit ${exit_price:,.4f}  ({pnl_pct:+.2f}%)\n"
        f"P&L ${pnl:+,.2f}  Equity ${equity:,.2f}\n"
        f"_{reason}_"
    )


def daily_loss_limit(equity: float) -> None:
    send(f"*HALT* Daily loss limit hit. Equity ${equity:,.2f}. No new entries today.")


def shutdown(equity: float) -> None:
    send(f"*Crypto scalper stopped*\nFinal equity: ${equity:,.2f}")


def error(msg: str) -> None:
    send(f"*ERROR* (crypto_scalper)\n`{msg[:300]}`")
