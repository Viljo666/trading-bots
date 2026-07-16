"""Auto-deploy: pull the repo from GitHub and restart bots whose code changed.

Runs under launchd every 5 minutes. Flow:
  1. skip if no git repo / no origin remote / dirty working tree (never clobber
     local edits — sends a one-time Telegram warning instead)
  2. fetch origin main; if remote is ahead, ff-only pull
  3. restart (kickstart -k) every launchd bot whose directory had changes
  4. Telegram a deploy report (commit + restarted bots)

This is what makes "edit the bots with Claude from the phone" real: a cloud
Claude Code session pushes to main, and within 5 minutes the Mac is running it.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT / ".env")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
DIRTY_FLAG = Path(__file__).resolve().parent / ".dirty_warned"
UID = os.getuid()

# dir prefix -> launchd label
BOT_DIRS = {
    "sweep_futures/": "com.villerajala.sweepfutures",
    "poc_futures/": "com.villerajala.pocfutures",
    "tori_futures/": "com.villerajala.toribot",
    "crypto_scalper/": "com.villerajala.cryptoscalper",
    "tsmom/": "com.villerajala.tsmomdaily",
    "commander/": "com.villerajala.botcommander",
}


def tg(text: str) -> None:
    if not (TOKEN and CHAT_ID):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text[:4000],
                            "parse_mode": "Markdown"}, timeout=10)
    except Exception:
        pass


def git(*args: str) -> tuple[int, str]:
    p = subprocess.run(["git", "-C", str(PROJECT), *args],
                       capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def main() -> None:
    if not (PROJECT / ".git").exists():
        return
    rc, _ = git("remote", "get-url", "origin")
    if rc != 0:
        return

    rc, out = git("status", "--porcelain")
    if rc == 0 and out.strip():
        if not DIRTY_FLAG.exists():
            tg("⚠️ *deploy*: working tree has local edits — auto-pull paused "
               "until it's clean/committed.")
            DIRTY_FLAG.touch()
        return
    DIRTY_FLAG.unlink(missing_ok=True)

    rc, out = git("fetch", "origin", "main")
    if rc != 0:
        return
    _, local = git("rev-parse", "HEAD")
    _, remote = git("rev-parse", "origin/main")
    if local == remote:
        return

    _, changed = git("diff", "--name-only", "HEAD", "origin/main")
    rc, out = git("pull", "--ff-only", "origin", "main")
    if rc != 0:
        tg(f"⚠️ *deploy failed* (pull): {out[:200]}")
        return

    restarted = []
    for prefix, label in BOT_DIRS.items():
        if any(f.startswith(prefix) for f in changed.splitlines()):
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{UID}/{label}"],
                           capture_output=True)
            restarted.append(prefix.rstrip("/"))
    _, sha = git("rev-parse", "--short", "HEAD")
    _, msg = git("log", "-1", "--pretty=%s")
    tg(f"🚀 *deployed* `{sha}` — {msg[:100]}\n"
       f"restarted: {', '.join(restarted) if restarted else 'none (no bot dirs changed)'}")


if __name__ == "__main__":
    main()
