"""Morning digest — one Telegram summarizing the last 24h across the stable.

Sections: gateway health, per-bot status + notable events (signals armed,
brackets, no-trades, errors, restarts), crypto scalper trade count. Scheduled
by launchd (com.villerajala.botdigest) each morning; also runnable on demand
via the commander's `digest` command or `python digest.py --print` (no send).
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))
import macro_calendar
load_dotenv(PROJECT / ".env")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
IBC_LOG = Path.home() / "IBC" / "healthcheck.log"
UID = os.getuid()

WINDOW_H = 24
INTERESTING = {"signal", "armed", "bracket_placed", "no_trade", "cutoff_cancel",
               "flatten", "bracket_error", "poll_error", "connected"}


def tg(text: str) -> None:
    if not (TOKEN and CHAT_ID):
        return
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                  data={"chat_id": CHAT_ID, "text": text[:4000],
                        "parse_mode": "Markdown"}, timeout=10)


def port_up(port: int = 4002) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            return True
    except Exception:
        return False


def job_pid(label: str) -> str:
    p = subprocess.run(["launchctl", "print", f"gui/{UID}/{label}"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        return ""
    m = re.search(r"pid = (\d+)", p.stdout)
    return m.group(1) if m else ""


def recent_events(logfile: Path, since: datetime) -> list[dict]:
    out = []
    try:
        for line in logfile.read_text().splitlines()[-2000:]:
            try:
                e = json.loads(line)
            except Exception:
                continue
            ts = e.get("ts", "")
            try:
                t = datetime.fromisoformat(ts)
            except Exception:
                continue
            if t >= since and e.get("kind") in INTERESTING:
                out.append(e)
    except Exception:
        pass
    return out


def summarize_bot(name: str, label: str, logfile: Path, since: datetime) -> str:
    pid = job_pid(label)
    dot = "🟢" if pid else "🔴"
    ev = recent_events(logfile, since)
    counts: dict[str, int] = {}
    for e in ev:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    notable = []
    for e in ev:
        k = e["kind"]
        if k in ("signal", "armed"):
            notable.append(f"  · {k} {e.get('symbol','')} {e.get('side','')} "
                           f"@{e.get('poc') or e.get('entry') or ''}")
        elif k in ("bracket_placed", "flatten", "cutoff_cancel", "bracket_error"):
            notable.append(f"  · {k} {e.get('symbol','')}")
    restarts = max(0, counts.get("connected", 0) - 0)
    bits = []
    if counts.get("signal") or counts.get("armed"):
        bits.append(f"{counts.get('signal', 0) + counts.get('armed', 0)} setup(s)")
    if counts.get("no_trade"):
        bits.append(f"{counts['no_trade']} no-trade")
    if counts.get("poll_error"):
        bits.append(f"{counts['poll_error']} poll-err")
    if restarts > 1:
        bits.append(f"{restarts} restarts")
    line = f"{dot} *{name}*" + (f" — {', '.join(bits)}" if bits else " — quiet")
    return "\n".join([line] + notable[:6])


def crypto_summary(since: datetime) -> str:
    logf = PROJECT / "crypto_scalper" / "logs" / "live.log"
    pid = job_pid("com.villerajala.cryptoscalper")
    dot = "🟢" if pid else "🔴"
    opens = closes = 0
    try:
        cutoff = since.strftime("%Y-%m-%d %H:%M")
        for line in logf.read_text().splitlines()[-3000:]:
            if line[:16] >= cutoff[:16]:
                if "OPEN " in line:
                    opens += 1
                if "CLOSE " in line:
                    closes += 1
    except Exception:
        pass
    return f"{dot} *crypto* — {opens} open / {closes} close (24h)"


def gateway_summary(since: datetime) -> str:
    up = port_up()
    dot = "🟢" if up else "🔴"
    incidents = []
    try:
        for line in IBC_LOG.read_text().splitlines()[-200:]:
            m = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] (.+)", line)
            if not m:
                continue
            t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").astimezone()
            if t >= since.astimezone() and "OK" not in m.group(2):
                incidents.append(f"  · {m.group(1)[5:16]} {m.group(2)[:60]}")
    except Exception:
        pass
    line = f"{dot} *gateway* — port 4002 {'UP' if up else 'DOWN'}"
    if incidents:
        line += "\n" + "\n".join(incidents[-3:])
    return line


def build() -> str:
    since = datetime.now(timezone.utc) - timedelta(hours=WINDOW_H)
    parts = [f"☀️ *Morning digest* — last {WINDOW_H}h",
             gateway_summary(since),
             macro_calendar.briefing(),
             summarize_bot("poc (MYM/MES)", "com.villerajala.pocfutures",
                           PROJECT / "poc_futures" / "launchd.out.log", since),
             summarize_bot("sweep (MNQ/MCL)", "com.villerajala.sweepfutures",
                           PROJECT / "sweep_futures" / "launchd.out.log", since),
             summarize_bot("tsmom", "com.villerajala.tsmomdaily",
                           PROJECT / "tsmom" / "launchd.out.log", since
                           ).replace("🔴 *tsmom*", "🕐 *tsmom* (daily job)"),
             crypto_summary(since)]
    return "\n".join(parts)


if __name__ == "__main__":
    text = build()
    if "--print" in sys.argv:
        print(text)
    else:
        tg(text)
