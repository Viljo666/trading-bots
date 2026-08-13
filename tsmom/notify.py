"""Telegram notifications for the TSMOM daily bot. Reuses parent .env creds."""
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
        r = requests.post(
            API_URL,
            data={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": "true"},
            timeout=5,
        )
        if r.status_code != 200:
            log.warning("Telegram %d: %s", r.status_code, r.text[:200])
    except Exception as e:
        log.warning("Telegram send failed: %s", e)


def daily_summary(rebalances: list[dict], dry_run: bool) -> None:
    """rebalances: list of {symbol, forecast, target, current, order}"""
    tag = "🧪 TSMOM dry-run" if dry_run else "📊 TSMOM daily rebalance"
    lines = [f"*{tag}*"]
    traded = [r for r in rebalances if r["order"] != 0]
    if not traded:
        lines.append("_no position changes today_")
    for r in rebalances:
        arrow = ""
        if r["order"] > 0:
            arrow = f"  BUY {r['order']}"
        elif r["order"] < 0:
            arrow = f"  SELL {abs(r['order'])}"
        lines.append(
            f"`{r['symbol']:4s}` fc {r['forecast']:+.0f}  tgt {r['target']:+d} (have {r['current']:+d}){arrow}"
        )
    send("\n".join(lines))


def gateway_unreachable(attempts: int) -> None:
    send(f"🔌 *IB Gateway not reachable* (tried {attempts}×). TSMOM will retry after Gateway restart.")


def error(msg: str) -> None:
    send(f"⚠️ TSMOM error: {msg[:300]}")
