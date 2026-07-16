"""Weekly performance report — actual bot activity vs backtest expectations.

For each strategy bot: setups seen in the last 7 days vs the rate its backtest
predicts, plus restart/error health. Once the bots run live-paper (fills), this
is where realized R-multiples get compared against backtest expectancy — the
early-warning system for edge decay.

Scheduled Sundays by com.villerajala.botweekly; on demand via commander
`report` or `python weekly_report.py --print`.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT / ".env")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Backtest-derived expectations (see project memories):
#  - POC continuation YM+ES: ~129 trades/yr/symbol over ~250 sessions
#    -> ~52% of sessions produce an ENTRY; bot ARMS nearly every session.
#    Expected arms/week ~= 2 symbols x 5 sessions = 10.
#  - Sweep MNQ+MCL: ~135 trades / 2.4yr / basket -> ~1.1 signals/week total.
EXPECT = {
    "poc": {"events": ("armed",), "per_week": 10.0, "expectancy_R": +0.25,
            "note": "arms ~every session; ~52% of arms should fill (touch POC)"},
    "sweep": {"events": ("signal",), "per_week": 1.1, "expectancy_R": +0.28,
              "note": "selective — 0-3 signals/week is normal"},
}

LOGS = {
    "poc": PROJECT / "poc_futures" / "launchd.out.log",
    "sweep": PROJECT / "sweep_futures" / "launchd.out.log",
}


def tg(text: str) -> None:
    if not (TOKEN and CHAT_ID):
        return
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                  data={"chat_id": CHAT_ID, "text": text[:4000],
                        "parse_mode": "Markdown"}, timeout=10)


def events_since(logfile: Path, since: datetime) -> list[dict]:
    out = []
    try:
        for line in logfile.read_text().splitlines()[-5000:]:
            try:
                e = json.loads(line)
                if datetime.fromisoformat(e.get("ts", "")) >= since:
                    out.append(e)
            except Exception:
                continue
    except Exception:
        pass
    return out


def build() -> str:
    since = datetime.now(timezone.utc) - timedelta(days=7)
    parts = ["📊 *Weekly report* — last 7 days"]
    for key, exp in EXPECT.items():
        ev = events_since(LOGS[key], since)
        setups = [e for e in ev if e.get("kind") in exp["events"]]
        errors = sum(1 for e in ev if "error" in e.get("kind", ""))
        restarts = sum(1 for e in ev if e.get("kind") == "connected")
        sides = {}
        for s in setups:
            k = f"{s.get('symbol','?')} {s.get('side','?')}"
            sides[k] = sides.get(k, 0) + 1
        detail = ", ".join(f"{k}×{v}" for k, v in sides.items()) or "none"
        flag = ""
        if len(setups) > exp["per_week"] * 2.5:
            flag = " ⚠️ well above expected — check for a bug or regime shift"
        elif exp["per_week"] >= 5 and len(setups) < exp["per_week"] * 0.3:
            flag = " ⚠️ well below expected — check the bot is detecting"
        parts.append(
            f"*{key}*: {len(setups)} setups (expect ~{exp['per_week']:.0f}/wk){flag}\n"
            f"  {detail}\n"
            f"  restarts {restarts} · errors {errors} · _{exp['note']}_")
    parts.append("_Dry mode: no fills to score yet. After golive, this report "
                 "adds realized R vs expectancy (poc +0.25R, sweep +0.28R)._")
    return "\n".join(parts)


if __name__ == "__main__":
    text = build()
    if "--print" in sys.argv:
        print(text)
    else:
        tg(text)
