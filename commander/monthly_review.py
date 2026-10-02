"""Send the once-monthly advisory stress test to Telegram."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

import stress_test


PROJECT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT / ".env")


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    text = stress_test.build()
    if "--print" in sys.argv:
        print(text)
        return
    if not (token and chat_id):
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing")
    requests.post(f"https://api.telegram.org/bot{token}/sendMessage", data={
        "chat_id": chat_id, "text": text[:4000], "parse_mode": "Markdown",
    }, timeout=10).raise_for_status()


if __name__ == "__main__":
    main()
