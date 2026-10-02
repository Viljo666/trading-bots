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


def daily_summary(
    rebalances: list[dict],
    dry_run: bool,
    execution: list[dict] | None = None,
    integrity: str | None = None,
    safety_notes: list[str] | None = None,
) -> None:
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
    if execution:
        lines.append("")
        for result in execution:
            mark = "✅" if result.get("ok") else "⚠️"
            fill = f"{result.get('filled', 0):g}/{result.get('expected', 0):g}"
            lines.append(
                f"{mark} `{result['symbol']}` {result['side']} {result['qty']} — "
                f"{result.get('status', 'unknown')} ({fill} filled)"
            )
    if safety_notes:
        lines.append("")
        lines.extend(f"⚠️ {note}" for note in safety_notes[:5])
    if integrity:
        lines.append("")
        lines.append(f"*{integrity}*")
    send("\n".join(lines))


def gateway_unreachable(attempts: int) -> None:
    send(f"🔌 *IB Gateway not reachable* (tried {attempts}×). TSMOM will retry after Gateway restart.")


def error(msg: str) -> None:
    send(f"⚠️ TSMOM error: {msg[:300]}")
