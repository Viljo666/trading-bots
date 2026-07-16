"""Telegram command interface for the trading bots.

Long-polls the existing Telegram bot for messages FROM THE OWNER (TELEGRAM_CHAT_ID
only) and executes a whitelist of operational commands against the launchd-managed
bots. Runs under launchd itself (KeepAlive), so it's always reachable from the
phone.

Commands (text the bot):
  status                  overview of every bot: running? mode? last event
  logs <bot>              last 8 log lines
  pause <bot>             stop a bot (launchd bootout)
  resume <bot>            start it again
  restart <bot>           kickstart (restart process)
  dry <bot>               switch bot to --dry mode (alerts only) and restart
  golive <bot> now        switch bot to LIVE paper orders and restart ('now' required)
  positions               all open futures positions at IBKR
  flatten <bot> now       market-close that bot's symbols + cancel orders ('now' required)
  help                    this list

Security: every update is checked against TELEGRAM_CHAT_ID; anything else is
ignored (and logged). Destructive commands require the literal suffix 'now'.
"""
from __future__ import annotations

import json
import logging
import os
import plistlib
import random
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
PROJECT = ROOT.parent
load_dotenv(PROJECT / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("commander")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"
OFFSET_FILE = ROOT / ".offset"
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
UID = os.getuid()

IB_HOST, IB_PORT = "127.0.0.1", 4002


@dataclass(frozen=True)
class BotSpec:
    key: str
    label: str                 # launchd label
    logfile: Path              # main jsonl/out log to tail
    symbols: tuple = ()        # IBKR futures symbols it trades (for flatten)
    supports_dry: bool = False # has a --dry flag in its plist


BOTS: dict[str, BotSpec] = {
    "sweep": BotSpec("sweep", "com.villerajala.sweepfutures",
                     PROJECT / "sweep_futures" / "launchd.out.log", ("MNQ", "MCL"), True),
    "poc": BotSpec("poc", "com.villerajala.pocfutures",
                   PROJECT / "poc_futures" / "launchd.out.log", ("MYM", "MES"), True),
    # tori retired 2026-07-16 (strategy shelved since June; plist archived in tori_futures/)
    "crypto": BotSpec("crypto", "com.villerajala.cryptoscalper",
                      PROJECT / "crypto_scalper" / "logs" / "live.log"),
    "tsmom": BotSpec("tsmom", "com.villerajala.tsmomdaily",
                     PROJECT / "tsmom" / "launchd.out.log"),
}

HELP = (
    "*Bot commander*\n"
    "`status` — all bots + gateway\n"
    "`logs <bot>` — recent log lines\n"
    "`pause|resume|restart <bot>`\n"
    "`dry <bot>` — alerts-only mode\n"
    "`golive <bot> now` — LIVE paper orders\n"
    "`positions` — open IBKR positions\n"
    "`flatten <bot> now` — close its positions\n"
    "`gateway` — IB Gateway status\n"
    "`gateway restart now` — force Gateway relogin\n"
    "`digest` — 24h summary (auto 09:00 Mon-Sat)\n"
    "`report` — weekly setups vs backtest (auto Sun)\n"
    f"bots: {', '.join(BOTS)}"
)

GATEWAY_LABEL = "com.villerajala.ibcgateway"
GATEWAY_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{GATEWAY_LABEL}.plist"
IBC_DIR = Path.home() / "IBC"


# ---------- telegram ----------

def send(text: str) -> None:
    try:
        requests.post(f"{API}/sendMessage", data={
            "chat_id": CHAT_ID, "text": text[:4000], "parse_mode": "Markdown",
            "disable_web_page_preview": "true"}, timeout=10)
    except Exception as e:
        log.warning("send failed: %s", e)


def get_updates(offset: int) -> list[dict]:
    try:
        r = requests.get(f"{API}/getUpdates",
                         params={"timeout": 50, "offset": offset}, timeout=60)
        return r.json().get("result", []) if r.status_code == 200 else []
    except Exception:
        time.sleep(5)
        return []


# ---------- launchd helpers ----------

def _run(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def is_running(label: str) -> tuple[bool, str]:
    rc, out = _run(["launchctl", "print", f"gui/{UID}/{label}"])
    if rc != 0:
        return False, "not loaded"
    pid = ""
    for line in out.splitlines():
        if "pid =" in line:
            pid = line.split("=")[1].strip(); break
    return bool(pid), f"pid {pid}" if pid else "loaded, not running"


def plist_path(label: str) -> Path:
    return LAUNCH_AGENTS / f"{label}.plist"


def bot_mode(spec: BotSpec) -> str:
    if not spec.supports_dry:
        return "live"
    try:
        args = plistlib.loads(plist_path(spec.label).read_bytes())["ProgramArguments"]
        return "dry" if "--dry" in args else "LIVE-paper"
    except Exception:
        return "?"


def set_dry(spec: BotSpec, dry: bool) -> str:
    path = plist_path(spec.label)
    pl = plistlib.loads(path.read_bytes())
    args = pl["ProgramArguments"]
    if dry and "--dry" not in args:
        args.append("--dry")
    elif not dry and "--dry" in args:
        args.remove("--dry")
    path.write_bytes(plistlib.dumps(pl))
    _run(["launchctl", "bootout", f"gui/{UID}/{spec.label}"])
    time.sleep(2)
    rc, out = _run(["launchctl", "bootstrap", f"gui/{UID}", str(path)])
    return "ok" if rc == 0 else f"bootstrap failed: {out[:120]}"


def tail_log(path: Path, n: int = 8) -> str:
    try:
        lines = path.read_text().splitlines()[-n:]
        return "\n".join(l[:180] for l in lines) or "(empty)"
    except Exception as e:
        return f"(no log: {e})"


# ---------- IBKR helpers ----------

def ib_connect():
    from ib_insync import IB
    ib = IB()
    ib.connect(IB_HOST, IB_PORT, clientId=random.randint(10000, 65000),
               readonly=False, timeout=10)
    return ib


def positions_text() -> str:
    try:
        ib = ib_connect()
        rows = [p for p in ib.positions() if p.position != 0]
        ib.disconnect()
        if not rows:
            return "No open positions."
        return "\n".join(
            f"{p.contract.symbol} {p.contract.lastTradeDateOrContractMonth}: "
            f"{int(p.position):+d} @ {p.avgCost:.2f}" for p in rows)
    except Exception as e:
        return f"IB error: {e}"


def flatten_bot(spec: BotSpec) -> str:
    if not spec.symbols:
        return f"{spec.key} has no IBKR symbols mapped."
    try:
        from ib_insync import MarketOrder
        ib = ib_connect()
        # cancel that bot's working orders
        n_cancel = 0
        for t in ib.openTrades():
            if t.contract.symbol in spec.symbols:
                ib.cancelOrder(t.order); n_cancel += 1
        ib.sleep(1)
        closed = []
        for p in ib.positions():
            if p.contract.symbol in spec.symbols and p.position != 0:
                side = "SELL" if p.position > 0 else "BUY"
                c = p.contract
                c.exchange = c.exchange or "SMART"
                ib.placeOrder(c, MarketOrder(side, abs(int(p.position))))
                closed.append(f"{c.symbol} {int(p.position):+d}")
        ib.sleep(2)
        ib.disconnect()
        return (f"cancelled {n_cancel} orders; "
                + (f"closing: {', '.join(closed)}" if closed else "no open positions"))
    except Exception as e:
        return f"IB error: {e}"


# ---------- gateway ----------

def port_up(port: int = IB_PORT) -> bool:
    import socket
    try:
        with socket.create_connection((IB_HOST, port), timeout=3):
            return True
    except Exception:
        return False


def gateway_status() -> str:
    up = port_up()
    running, detail = is_running(GATEWAY_LABEL)
    dot = "🟢" if up else "🔴"
    last = ""
    try:
        lines = (IBC_DIR / "healthcheck.log").read_text().splitlines()
        if lines:
            last = f"\nlast healthcheck: {lines[-1][:120]}"
    except Exception:
        pass
    return (f"{dot} *gateway* — API port {IB_PORT} {'UP' if up else 'DOWN'}; "
            f"launchd {detail}{last}")


def gateway_restart() -> str:
    """Force a full Gateway recycle: unload agent, kill leftovers, reload, wait."""
    _run(["launchctl", "unload", str(GATEWAY_PLIST)])
    for pat in ("IbcGateway", "ibcstart", "start_gateway_paper"):
        subprocess.run(["pkill", "-f", pat], capture_output=True)
    time.sleep(8)
    rc, out = _run(["launchctl", "load", str(GATEWAY_PLIST)])
    if rc != 0:
        return f"⚠️ reload failed: {out[:150]}"
    send("♻️ Gateway recycling — waiting for login (up to 5 min)…")
    for i in range(30):
        time.sleep(10)
        if port_up():
            return f"🟢 Gateway recovered after ~{(i + 1) * 10}s. Bots will reconnect."
    return ("🔴 Gateway did not come back in 5 min — likely needs manual login "
            "(2FA?). Check the Mac when possible.")


# ---------- command dispatch ----------

SCHEDULED_BOTS = {"tsmom"}   # daily jobs: idle between runs is normal


def status_text() -> str:
    out = []
    for spec in BOTS.values():
        running, detail = is_running(spec.label)
        mode = bot_mode(spec)
        if running:
            dot = "🟢"
        elif spec.key in SCHEDULED_BOTS:
            dot, detail = "🕐", "idle until next scheduled run"
        else:
            dot = "🔴"
        out.append(f"{dot} *{spec.key}* ({mode}) — {detail}")
    return "\n".join(out)


def handle(text: str) -> str:
    parts = text.strip().lower().split()
    if not parts:
        return HELP
    cmd, args = parts[0], parts[1:]

    if cmd in ("help", "/help", "/start"):
        return HELP
    if cmd == "status":
        return status_text() + "\n" + gateway_status()
    if cmd == "positions":
        return positions_text()
    if cmd == "digest":
        import digest
        return digest.build()
    if cmd == "report":
        import weekly_report
        return weekly_report.build()
    if cmd == "gateway":
        if args and args[0] == "restart":
            if "now" not in args:
                return "⚠️ This drops all bot connections for ~1-3 min. Confirm: `gateway restart now`"
            return gateway_restart()
        return gateway_status()

    if cmd in ("logs", "pause", "resume", "restart", "dry", "golive", "flatten"):
        if not args or args[0] not in BOTS:
            return f"usage: `{cmd} <bot>` — bots: {', '.join(BOTS)}"
        spec = BOTS[args[0]]

        if cmd == "logs":
            return f"*{spec.key}*\n```\n{tail_log(spec.logfile)}\n```"
        if cmd == "pause":
            _run(["launchctl", "bootout", f"gui/{UID}/{spec.label}"])
            return f"{spec.key} paused (bootout)."
        if cmd == "resume":
            rc, out = _run(["launchctl", "bootstrap", f"gui/{UID}", str(plist_path(spec.label))])
            return f"{spec.key} resumed." if rc == 0 else f"resume failed: {out[:120]}"
        if cmd == "restart":
            rc, out = _run(["launchctl", "kickstart", "-k", f"gui/{UID}/{spec.label}"])
            return f"{spec.key} restarted." if rc == 0 else f"restart failed: {out[:120]}"
        if cmd == "dry":
            if not spec.supports_dry:
                return f"{spec.key} has no dry mode."
            return f"{spec.key} -> dry: {set_dry(spec, True)}"
        if cmd == "golive":
            if not spec.supports_dry:
                return f"{spec.key} has no dry/live toggle."
            if "now" not in args:
                return f"⚠️ This arms REAL paper orders. Confirm with: `golive {spec.key} now`"
            return f"{spec.key} -> LIVE-paper: {set_dry(spec, False)}"
        if cmd == "flatten":
            if "now" not in args:
                return f"⚠️ This market-closes {spec.key}'s positions. Confirm with: `flatten {spec.key} now`"
            return flatten_bot(spec)

    return f"Unknown command `{cmd}`.\n\n{HELP}"


def main() -> None:
    if not (TOKEN and CHAT_ID):
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env")
    try:
        offset = int(OFFSET_FILE.read_text())
    except Exception:
        offset = 0
    log.info("commander up (offset=%d)", offset)
    send("🎛 *Bot commander online* — text `help` for commands.")
    while True:
        for u in get_updates(offset):
            offset = u["update_id"] + 1
            OFFSET_FILE.write_text(str(offset))
            msg = u.get("message") or u.get("edited_message") or {}
            chat = str(msg.get("chat", {}).get("id", ""))
            text = msg.get("text", "")
            if chat != CHAT_ID:
                log.warning("ignored message from chat %s", chat)
                continue
            if not text:
                continue
            log.info("cmd: %s", text)
            try:
                send(handle(text))
            except Exception as e:
                send(f"⚠️ command failed: {e}")


if __name__ == "__main__":
    main()
