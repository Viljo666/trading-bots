#!/usr/bin/env python3
"""
Trading Bot Monitor
Runs every minute via launchd and writes status.json to the Trading bot folder.
Claude reads this file to report bot status without needing screen access.
"""

import json
import subprocess
import os
import re
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).parent

BOTS = {
    "main_alpaca": {
        "label": "Main Bot (Alpaca)",
        "match": ["main.py"],
        "log": BASE / "logs" / f"{datetime.now().strftime('%Y-%m-%d')}.log",
    },
    "tsmom": {
        "label": "TSMOM (daily IBKR paper)",
        "match": ["tsmom/live_daily.py", "tsmom\\live_daily.py"],
        "log": BASE / "tsmom" / "daily_log.jsonl",
    },
    "poc_futures": {
        "label": "POC Futures (dry)",
        "match": ["poc_futures/live.py", "poc_futures\\live.py"],
        "log": BASE / "poc_futures" / "live_log.jsonl",
    },
    "sweep_futures": {
        "label": "Sweep Futures (dry)",
        "match": ["sweep_futures/live.py", "sweep_futures\\live.py"],
        "log": BASE / "sweep_futures" / "live_log.jsonl",
    },
    "crypto_scalper": {
        "label": "Crypto Scalper",
        "match": ["crypto_scalper/live.py", "crypto_scalper\\live.py"],
        "log": BASE / "crypto_scalper" / "logs" / "launchd.out.log",
    },
}


def get_processes():
    """Return list of running Python processes with their command lines."""
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,%cpu=,%mem=,etime=,command="],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout
    except Exception:
        return ""


def check_bot(bot_key, bot_cfg, ps_output):
    """Check if a bot is running and get its recent log tail."""
    status = {
        "label": bot_cfg["label"],
        "running": False,
        "pid": None,
        "cpu": None,
        "mem": None,
        "uptime": None,
        "last_log_lines": [],
        "last_log_time": None,
    }

    # Search ps output for this bot
    for match in bot_cfg["match"]:
        for line in ps_output.splitlines():
            if match in line and "bot_monitor" not in line:
                parts = line.strip().split(maxsplit=4)
                if len(parts) == 5:
                    status["running"] = True
                    status["pid"] = parts[0]
                    status["cpu"] = parts[1] + "%"
                    status["mem"] = parts[2] + "%"
                    status["uptime"] = parts[3]
                break
        if status["running"]:
            break

    # Get last few log lines
    log_path = bot_cfg.get("log")
    if log_path and Path(log_path).exists():
        try:
            with open(log_path, "r") as f:
                lines = f.readlines()
            recent = [l.rstrip() for l in lines[-5:] if l.strip()]
            status["last_log_lines"] = recent

            # Try to extract timestamp from last line
            if recent:
                last = recent[-1]
                # Match ISO timestamps or log-style timestamps
                ts_match = re.search(
                    r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})', last
                )
                if ts_match:
                    status["last_log_time"] = ts_match.group(1)
        except Exception as e:
            status["last_log_lines"] = [f"(error reading log: {e})"]

    return status


def main():
    ps_output = get_processes()
    now = datetime.now(timezone.utc).isoformat()

    report = {
        "checked_at": now,
        "bots": {}
    }

    for key, cfg in BOTS.items():
        report["bots"][key] = check_bot(key, cfg, ps_output)

    out_path = BASE / "status.json"
    tmp_path = out_path.with_suffix(".tmp")
    with open(tmp_path, "w") as f:
        json.dump(report, f, indent=2)
    tmp_path.replace(out_path)

    # Also print summary to stdout (captured by launchd)
    running = [v["label"] for v in report["bots"].values() if v["running"]]
    stopped = [v["label"] for v in report["bots"].values() if not v["running"]]
    print(f"[{now}] Running: {running} | Stopped: {stopped}")


if __name__ == "__main__":
    main()
