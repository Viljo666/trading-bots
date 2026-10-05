"""DRDOOM community bot — one Telegram bot that runs the group.

Runs under launchd (KeepAlive) on its OWN bot token (DRDOOM_BOT_TOKEN). It must never
share a token with commander/commander.py: two getUpdates consumers on one token
steal each other's updates.

What it does in the configured group (DRDOOM_CHAT_ID):
  entry gate   new members are muted until they tap a button; kicked if they don't
  moderation   deletes links, forwards and foreign contract addresses from non-admins;
               3 warnings = 24h mute
  info         /ca /site /buy /price /chart /rules /help
  buy alerts   posts on-chain buys above MIN_BUY_SOL
  ranks        villain titles from chat activity: /rank /top
  contests     /contest start|end, /submit a photo, vote with the button
  raids        /raid <x.com link> [lock minutes]

Admin-only (reply to a message): /warn /unwarn /mute [hours] /unmute /ban
The bot has to be a group admin with: delete messages, ban users, pin messages.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

import chain

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("drdoom")

DEFAULT_CA = "HoTY1CsQCCUvhmjx3ikMVCMrYTsBfENQxzKmC7ebpump"

RANKS = [(0, "Bystander"), (10, "Henchman"), (50, "Minion"), (150, "Enforcer"),
         (400, "Lieutenant"), (1000, "Mastermind")]
POINT_COOLDOWN = 20      # seconds between messages that earn a point
MAX_WARNS = 3
MUTE_HOURS = 24
NOTICE_TTL = 45          # moderation notices delete themselves after this many seconds
ADMIN_CACHE_TTL = 300
BUY_POLL_SECONDS = 20

SOL_ADDRESS = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
EVM_ADDRESS = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
BARE_URL = re.compile(r"(?:https?://|www\.|t\.me/)\S+", re.I)

MUTED = {"can_send_messages": False}
OPEN = {k: True for k in (
    "can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos",
    "can_send_videos", "can_send_video_notes", "can_send_voice_notes", "can_send_polls",
    "can_send_other_messages", "can_add_web_page_previews", "can_invite_users")}

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS pending (user_id INTEGER PRIMARY KEY, msg_id INTEGER, deadline REAL);
CREATE TABLE IF NOT EXISTS warns (user_id INTEGER PRIMARY KEY, n INTEGER);
CREATE TABLE IF NOT EXISTS activity (user_id INTEGER PRIMARY KEY, name TEXT, points INTEGER, last_ts REAL);
CREATE TABLE IF NOT EXISTS contest (id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT, active INTEGER, started REAL);
CREATE TABLE IF NOT EXISTS entry (id INTEGER PRIMARY KEY AUTOINCREMENT, contest_id INTEGER, user_id INTEGER,
                                  name TEXT, file_id TEXT, msg_id INTEGER, votes INTEGER DEFAULT 0,
                                  UNIQUE (contest_id, user_id));
CREATE TABLE IF NOT EXISTS vote (entry_id INTEGER, user_id INTEGER, PRIMARY KEY (entry_id, user_id));
"""


class Config:
    def __init__(self, env=os.environ):
        self.token = env.get("DRDOOM_BOT_TOKEN", "").strip()
        # Numeric id, or the @username of a public group (resolved to an id at startup).
        self.chat_ref = env.get("DRDOOM_CHAT_ID", "").strip()
        self.chat_id = int(self.chat_ref) if self.chat_ref.lstrip("-").isdigit() else None
        self.ca = env.get("DRDOOM_CA", DEFAULT_CA).strip()
        self.ticker = env.get("DRDOOM_TICKER", "DRDOOM").strip()
        self.site = env.get("DRDOOM_SITE", "https://drdoom.pages.dev/").strip()
        self.rpc_url = env.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com").strip()
        self.buy_alerts = env.get("BUY_ALERTS", "1").strip() == "1"
        self.min_buy_sol = float(env.get("MIN_BUY_SOL", "0.1"))
        self.gate_minutes = float(env.get("GATE_TIMEOUT_MIN", "5"))
        extra = [d.strip().lower() for d in env.get("ALLOWED_DOMAINS", "x.com,twitter.com").split(",")]
        self.allowed_domains = {urlparse(self.site).hostname or "", *filter(None, extra)}
        # Links to these are fine only when they point at OUR token, not someone else's.
        self.token_domains = {"pump.fun", "dexscreener.com", "solscan.io"}

    @property
    def buy_url(self):
        return f"https://pump.fun/coin/{self.ca}"

    @property
    def chart_url(self):
        return f"https://dexscreener.com/solana/{self.ca}"


class Tg:
    def __init__(self, token: str):
        self.api = f"https://api.telegram.org/bot{token}"

    def call(self, method: str, **params):
        """Call a Bot API method. Returns `result`, or None on any failure (logged)."""
        try:
            r = requests.post(f"{self.api}/{method}", json=params, timeout=30)
            body = r.json()
        except Exception as e:
            log.warning("%s failed: %s", method, e)
            return None
        if not body.get("ok"):
            log.warning("%s rejected: %s", method, body.get("description"))
            return None
        return body.get("result")


def esc(s) -> str:
    return html.escape(str(s or ""))


def mention(user: dict) -> str:
    return f'<a href="tg://user?id={user["id"]}">{esc(user.get("first_name") or "villain")}</a>'


def money(x) -> str:
    x = float(x or 0)
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(x) >= div:
            return f"${x / div:.2f}{suffix}"
    return f"${x:.2f}"


def amount(x: float) -> str:
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if x >= div:
            return f"{x / div:.2f}{suffix}"
    return f"{x:.0f}"


def rank_for(points: int) -> str:
    return [title for need, title in RANKS if points >= need][-1]


def entity_text(text: str, ent: dict) -> str:
    """Slice an entity out of `text`. Telegram offsets are UTF-16 code units."""
    raw = text.encode("utf-16-le")
    return raw[ent["offset"] * 2:(ent["offset"] + ent["length"]) * 2].decode("utf-16-le", "ignore")


class Bot:
    def __init__(self, tg, cfg: Config, db_path=":memory:", now=time.time):
        self.tg, self.cfg, self.now = tg, cfg, now
        self.db = sqlite3.connect(db_path, isolation_level=None)
        self.db.executescript(SCHEMA)
        self.admins: set[int] = set()
        self.admins_ts = 0.0
        self.ephemeral: list[tuple[float, int]] = []   # (delete_at, message_id)
        self.next_buy_poll = 0.0
        self.watcher = chain.BuyWatcher(cfg.rpc_url, cfg.ca, lambda: self.kv("last_sig"),
                                        lambda s: self.kv_set("last_sig", s), cfg.min_buy_sol)

    # ---------- storage / telegram helpers ----------

    def kv(self, key, default=None):
        row = self.db.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
        return row[0] if row else default

    def kv_set(self, key, value):
        if value is None:
            self.db.execute("DELETE FROM kv WHERE k=?", (key,))
        else:
            self.db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, str(value)))

    def send(self, text, chat_id=None, ttl=None, **kw):
        msg = self.tg.call("sendMessage", chat_id=chat_id or self.cfg.chat_id, text=text,
                           parse_mode="HTML", disable_web_page_preview=True, **kw)
        if msg and ttl:
            self.ephemeral.append((self.now() + ttl, msg["message_id"]))
        return msg

    def delete(self, msg_id):
        self.tg.call("deleteMessage", chat_id=self.cfg.chat_id, message_id=msg_id)

    def is_admin(self, msg_or_cq: dict) -> bool:
        # Anonymous admins post "as the group"; their `from` is a placeholder account.
        if (msg_or_cq.get("sender_chat") or {}).get("id") == self.cfg.chat_id:
            return True
        if self.now() - self.admins_ts > ADMIN_CACHE_TTL:
            got = self.tg.call("getChatAdministrators", chat_id=self.cfg.chat_id)
            if got is not None:
                self.admins = {a["user"]["id"] for a in got}
                self.admins_ts = self.now()
        return (msg_or_cq.get("from") or {}).get("id") in self.admins

    # ---------- update routing ----------

    def handle(self, update: dict):
        if "callback_query" in update:
            return self.on_callback(update["callback_query"])
        msg = update.get("message")
        if not msg:
            return
        chat = msg["chat"]
        if chat["type"] == "private":
            return self.on_private(msg)
        if self.cfg.chat_id is None:
            # Setup helper: tells the owner which id to put in .env. Disabled once set.
            if (msg.get("text") or "").startswith("/id"):
                self.send(f"Chat id: <code>{chat['id']}</code>", chat_id=chat["id"])
            return
        if chat["id"] == self.cfg.chat_id:
            self.on_group(msg)

    def on_private(self, msg):
        cmd, _ = self.parse_command(msg)
        if cmd in self.INFO:
            self.INFO[cmd](self, msg["chat"]["id"])
        else:
            self.cmd_help(msg["chat"]["id"])

    def on_group(self, msg):
        for user in msg.get("new_chat_members", []):
            if not user.get("is_bot"):
                self.gate(user)
        if "new_chat_members" in msg or "left_chat_member" in msg or msg.get("is_automatic_forward"):
            return
        user = msg.get("from") or {}
        admin = self.is_admin(msg)
        if not admin:
            if self.db.execute("SELECT 1 FROM pending WHERE user_id=?", (user.get("id"),)).fetchone():
                return self.delete(msg["message_id"])
            reason = self.violation(msg)
            if reason:
                self.delete(msg["message_id"])
                return self.warn(user, reason)
        cmd, arg = self.parse_command(msg)
        if cmd in self.INFO:
            return self.INFO[cmd](self, self.cfg.chat_id)
        if cmd in self.MEMBER:
            return self.MEMBER[cmd](self, msg, arg)
        if cmd in self.ADMIN:
            if admin:
                self.ADMIN[cmd](self, msg, arg)
            return
        if not admin or "sender_chat" not in msg:
            self.award(user, msg)

    @staticmethod
    def parse_command(msg) -> tuple[str, str]:
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if not text.startswith("/"):
            return "", ""
        head, _, arg = text.partition(" ")
        return head[1:].split("@")[0].lower(), arg.strip()

    # ---------- entry gate ----------

    def gate(self, user):
        if not self.tg.call("restrictChatMember", chat_id=self.cfg.chat_id, user_id=user["id"],
                            permissions=MUTED):
            return  # not admin yet, or the user is an admin: nothing to gate
        minutes = self.cfg.gate_minutes
        msg = self.send(
            f"{mention(user)}, the lair has a door policy. Tap below within {minutes:g} minutes "
            "to prove you're human.",
            reply_markup={"inline_keyboard": [[{"text": "I am not a bot", "callback_data": f"v:{user['id']}"}]]})
        self.db.execute("INSERT OR REPLACE INTO pending VALUES (?, ?, ?)",
                        (user["id"], msg["message_id"] if msg else None, self.now() + minutes * 60))

    def verify(self, cq, user_id):
        user = cq["from"]
        if user["id"] != user_id:
            return self.answer(cq, "This button is for someone else.")
        row = self.db.execute("SELECT msg_id FROM pending WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            return self.answer(cq, "Already verified.")
        self.tg.call("restrictChatMember", chat_id=self.cfg.chat_id, user_id=user_id, permissions=OPEN)
        self.db.execute("DELETE FROM pending WHERE user_id=?", (user_id,))
        if row[0]:
            self.delete(row[0])
        self.answer(cq, "Welcome to the lair.")
        self.send(f"{mention(user)} has entered the chat.\n\n"
                  "/ca for the token address · /rules for the house rules\n"
                  "<b>Admins never DM first.</b> Anyone who does is a scammer.", ttl=300)

    def expire_pending(self):
        for user_id, msg_id in self.db.execute(
                "SELECT user_id, msg_id FROM pending WHERE deadline < ?", (self.now(),)).fetchall():
            # Ban then unban = kick: they can rejoin and try again.
            self.tg.call("banChatMember", chat_id=self.cfg.chat_id, user_id=user_id)
            self.tg.call("unbanChatMember", chat_id=self.cfg.chat_id, user_id=user_id, only_if_banned=True)
            if msg_id:
                self.delete(msg_id)
            self.db.execute("DELETE FROM pending WHERE user_id=?", (user_id,))

    # ---------- moderation ----------

    def violation(self, msg) -> str | None:
        if any(k in msg for k in ("forward_origin", "forward_from", "forward_from_chat")):
            return "forwarded messages"
        text = msg.get("text") or msg.get("caption") or ""
        urls = [m.group(0) for m in BARE_URL.finditer(text)]
        for ent in (msg.get("entities") or []) + (msg.get("caption_entities") or []):
            if ent["type"] == "text_link":
                urls.append(ent["url"])
            elif ent["type"] == "url":
                urls.append(entity_text(text, ent))
        if any(not self.url_allowed(u) for u in urls):
            return "links"
        addresses = SOL_ADDRESS.findall(text) + EVM_ADDRESS.findall(text)
        if any(a != self.cfg.ca for a in addresses):
            return "other contract addresses"
        return None

    def url_allowed(self, url: str) -> bool:
        host = (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
        host = host.removeprefix("www.")

        def under(domains):
            return any(host == d or host.endswith("." + d) for d in domains)

        if under(self.cfg.allowed_domains):
            return True
        return under(self.cfg.token_domains) and self.cfg.ca in url

    def warn(self, user, reason):
        self.db.execute("INSERT INTO warns VALUES (?, 1) ON CONFLICT(user_id) DO UPDATE SET n = n + 1",
                        (user["id"],))
        n = self.db.execute("SELECT n FROM warns WHERE user_id=?", (user["id"],)).fetchone()[0]
        if n >= MAX_WARNS:
            self.mute(user["id"], MUTE_HOURS)
            self.db.execute("DELETE FROM warns WHERE user_id=?", (user["id"],))
            self.send(f"{mention(user)} is muted for {MUTE_HOURS}h ({reason}).", ttl=NOTICE_TTL)
        else:
            self.send(f"{mention(user)}: no {reason} here. Warning {n}/{MAX_WARNS}.", ttl=NOTICE_TTL)

    def mute(self, user_id, hours):
        self.tg.call("restrictChatMember", chat_id=self.cfg.chat_id, user_id=user_id,
                     permissions=MUTED, until_date=int(self.now() + hours * 3600))

    def target(self, msg) -> dict | None:
        """The user an admin command is aimed at: the author of the replied-to message."""
        user = (msg.get("reply_to_message") or {}).get("from")
        if not user:
            self.send("Reply to the user's message with that command.", ttl=NOTICE_TTL)
        return user

    def cmd_warn(self, msg, arg):
        if user := self.target(msg):
            self.warn(user, arg or "breaking the rules")

    def cmd_unwarn(self, msg, arg):
        if user := self.target(msg):
            self.db.execute("DELETE FROM warns WHERE user_id=?", (user["id"],))
            self.send(f"Warnings cleared for {mention(user)}.", ttl=NOTICE_TTL)

    def cmd_mute(self, msg, arg):
        if user := self.target(msg):
            hours = float(arg) if arg.replace(".", "", 1).isdigit() else MUTE_HOURS
            self.mute(user["id"], hours)
            self.send(f"{mention(user)} is muted for {hours:g}h.", ttl=NOTICE_TTL)

    def cmd_unmute(self, msg, arg):
        if user := self.target(msg):
            self.tg.call("restrictChatMember", chat_id=self.cfg.chat_id, user_id=user["id"], permissions=OPEN)
            self.send(f"{mention(user)} can speak again.", ttl=NOTICE_TTL)

    def cmd_ban(self, msg, arg):
        if user := self.target(msg):
            self.tg.call("banChatMember", chat_id=self.cfg.chat_id, user_id=user["id"])
            self.delete(msg["reply_to_message"]["message_id"])
            self.send(f"{mention(user)} has been banished.", ttl=NOTICE_TTL)

    # ---------- info commands ----------

    def cmd_ca(self, chat_id):
        self.send(f"<b>${esc(self.cfg.ticker)} token address</b>\n<code>{self.cfg.ca}</code>\n\n"
                  "Tap to copy. Any other address posted here is fake.", chat_id=chat_id)

    def cmd_site(self, chat_id):
        self.send(esc(self.cfg.site), chat_id=chat_id)

    def cmd_buy(self, chat_id):
        self.send(f'<a href="{self.cfg.buy_url}">Buy ${esc(self.cfg.ticker)} on Pump.fun</a>\n'
                  f"<code>{self.cfg.ca}</code>\n\nMeme coin. No promises, no financial advice.",
                  chat_id=chat_id)

    def cmd_chart(self, chat_id):
        self.send(f'<a href="{self.cfg.chart_url}">DexScreener</a> · '
                  f'<a href="{self.cfg.buy_url}">Pump.fun</a>', chat_id=chat_id)

    def cmd_price(self, chat_id):
        p = chain.token_info(self.cfg.ca)
        if not p:
            return self.send("No market data yet. Try /chart.", chat_id=chat_id)
        change = (p.get("priceChange") or {}).get("h24")
        tx = (p.get("txns") or {}).get("h24") or {}
        lines = [f"<b>${esc(self.cfg.ticker)}</b>  ${esc(p.get('priceUsd', '?'))}"
                 + (f"  ({change:+.1f}% 24h)" if change is not None else ""),
                 f"Market cap: {money(p.get('marketCap') or p.get('fdv'))}",
                 f"24h volume: {money((p.get('volume') or {}).get('h24'))}",
                 f"24h trades: {tx.get('buys', 0)} buys / {tx.get('sells', 0)} sells",
                 f'<a href="{self.cfg.chart_url}">Chart</a>']
        self.send("\n".join(lines), chat_id=chat_id)

    def cmd_rules(self, chat_id):
        self.send("<b>House rules</b>\n"
                  "1. No links, forwards or other contract addresses.\n"
                  "2. No spam, no begging, no impersonating admins.\n"
                  "3. Admins never DM first. Report anyone who does.\n"
                  "4. Nobody here can promise you a price. Don't spend what you can't lose.\n"
                  f"{MAX_WARNS} warnings = {MUTE_HOURS}h mute.", chat_id=chat_id)

    def cmd_help(self, chat_id):
        self.send("<b>DRDOOM bot</b>\n"
                  "/ca · /buy · /price · /chart · /site · /rules\n"
                  "/rank · /top — villain ranks from chat activity\n"
                  "/submit — enter the meme contest (send a photo with /submit as the caption)\n\n"
                  "<i>Admins:</i> /contest start &lt;topic&gt; · /contest end · /raid &lt;x link&gt; [lock min] · "
                  "/warn /unwarn /mute [h] /unmute /ban (as a reply)", chat_id=chat_id)

    # ---------- ranks ----------

    def award(self, user, msg):
        if not user.get("id") or user.get("is_bot"):
            return
        if len(msg.get("text") or "") < 3 and "photo" not in msg:
            return
        row = self.db.execute("SELECT points, last_ts FROM activity WHERE user_id=?", (user["id"],)).fetchone()
        points, last = row or (0, 0)
        if self.now() - last < POINT_COOLDOWN:
            return
        self.db.execute("INSERT OR REPLACE INTO activity VALUES (?, ?, ?, ?)",
                        (user["id"], user.get("first_name") or "villain", points + 1, self.now()))
        if rank_for(points + 1) != rank_for(points):
            self.send(f"{mention(user)} has been promoted to <b>{rank_for(points + 1)}</b>.")

    def cmd_rank(self, msg, arg):
        user = msg.get("from") or {}
        row = self.db.execute("SELECT points FROM activity WHERE user_id=?", (user.get("id"),)).fetchone()
        points = row[0] if row else 0
        ahead = [need for need, _ in RANKS if need > points]
        nxt = f" {ahead[0] - points} more to the next rank." if ahead else ""
        self.send(f"{mention(user)}: <b>{rank_for(points)}</b>, {points} points.{nxt}")

    def cmd_top(self, msg, arg):
        rows = self.db.execute("SELECT name, points FROM activity ORDER BY points DESC LIMIT 10").fetchall()
        if not rows:
            return self.send("No villains ranked yet.")
        self.send("<b>Most wanted</b>\n" + "\n".join(
            f"{i}. {esc(name)} — {rank_for(p)} ({p})" for i, (name, p) in enumerate(rows, 1)))

    # ---------- meme contest ----------

    def active_contest(self):
        return self.db.execute("SELECT id, topic FROM contest WHERE active=1 ORDER BY id DESC").fetchone()

    def cmd_contest(self, msg, arg):
        action, _, topic = arg.partition(" ")
        current = self.active_contest()
        if action == "start":
            if current:
                return self.send("A contest is already running. /contest end first.", ttl=NOTICE_TTL)
            topic = topic.strip() or "Best DRDOOM meme"
            self.db.execute("INSERT INTO contest (topic, active, started) VALUES (?, 1, ?)", (topic, self.now()))
            self.send(f"<b>Meme contest: {esc(topic)}</b>\n\nSend a photo with <code>/submit</code> as the "
                      "caption. One entry each. Vote with the button under each entry.")
        elif action == "end":
            if not current:
                return self.send("No contest is running.", ttl=NOTICE_TTL)
            self.db.execute("UPDATE contest SET active=0 WHERE id=?", (current[0],))
            rows = self.db.execute("SELECT user_id, name, votes, file_id FROM entry WHERE contest_id=? "
                                   "ORDER BY votes DESC, id LIMIT 3", (current[0],)).fetchall()
            if not rows:
                return self.send(f"Contest over: {esc(current[1])}. No entries.")
            board = "\n".join(f"{i}. {esc(name)} — {votes} votes" for i, (_, name, votes, _) in enumerate(rows, 1))
            self.tg.call("sendPhoto", chat_id=self.cfg.chat_id, photo=rows[0][3], parse_mode="HTML",
                         caption=f"<b>Contest over: {esc(current[1])}</b>\n\n{board}")
        else:
            self.send("Use /contest start &lt;topic&gt; or /contest end.", ttl=NOTICE_TTL)

    def cmd_submit(self, msg, arg):
        current = self.active_contest()
        user = msg.get("from") or {}
        if not current:
            return self.send("No contest is running right now.", ttl=NOTICE_TTL)
        source = msg if "photo" in msg else (msg.get("reply_to_message") or {})
        if "photo" not in source or (source.get("from") or {}).get("id") != user.get("id"):
            return self.send("Send your own photo with /submit as the caption.", ttl=NOTICE_TTL)
        if self.db.execute("SELECT 1 FROM entry WHERE contest_id=? AND user_id=?",
                           (current[0], user["id"])).fetchone():
            return self.send(f"{mention(user)}, one entry per contest.", ttl=NOTICE_TTL)
        name = user.get("first_name") or "villain"
        cur = self.db.execute("INSERT INTO entry (contest_id, user_id, name, file_id) VALUES (?, ?, ?, ?)",
                              (current[0], user["id"], name, source["photo"][-1]["file_id"]))
        posted = self.tg.call("sendPhoto", chat_id=self.cfg.chat_id, photo=source["photo"][-1]["file_id"],
                              caption=f"Entry by {esc(name)} — {esc(current[1])}", parse_mode="HTML",
                              reply_markup=self.vote_markup(cur.lastrowid, 0))
        if posted:
            self.db.execute("UPDATE entry SET msg_id=? WHERE id=?", (posted["message_id"], cur.lastrowid))

    @staticmethod
    def vote_markup(entry_id, votes):
        return {"inline_keyboard": [[{"text": f"🔥 {votes}", "callback_data": f"vote:{entry_id}"}]]}

    def vote(self, cq, entry_id):
        row = self.db.execute("SELECT e.user_id, e.msg_id, c.active FROM entry e JOIN contest c "
                              "ON c.id = e.contest_id WHERE e.id=?", (entry_id,)).fetchone()
        if not row or not row[2]:
            return self.answer(cq, "This contest is over.")
        if row[0] == cq["from"]["id"]:
            return self.answer(cq, "You can't vote for yourself.")
        try:
            self.db.execute("INSERT INTO vote VALUES (?, ?)", (entry_id, cq["from"]["id"]))
        except sqlite3.IntegrityError:
            return self.answer(cq, "You already voted for this one.")
        self.db.execute("UPDATE entry SET votes = votes + 1 WHERE id=?", (entry_id,))
        votes = self.db.execute("SELECT votes FROM entry WHERE id=?", (entry_id,)).fetchone()[0]
        self.tg.call("editMessageReplyMarkup", chat_id=self.cfg.chat_id, message_id=row[1],
                     reply_markup=self.vote_markup(entry_id, votes))
        self.answer(cq, "Vote counted.")

    # ---------- raids ----------

    def cmd_raid(self, msg, arg):
        url, _, rest = arg.partition(" ")
        host = (urlparse(url).hostname or "").removeprefix("www.")
        if host not in ("x.com", "twitter.com"):
            return self.send("Use /raid &lt;x.com link&gt; [lock minutes].", ttl=NOTICE_TTL)
        minutes = min(float(rest), 60) if rest.replace(".", "", 1).isdigit() else 0
        text = "<b>RAID</b>\n\nLike, reply and repost. Bring the villain energy."
        if minutes:
            chat = self.tg.call("getChat", chat_id=self.cfg.chat_id) or {}
            if self.tg.call("setChatPermissions", chat_id=self.cfg.chat_id, permissions=MUTED):
                self.kv_set("raid_perms", json.dumps(chat.get("permissions") or OPEN))
                self.kv_set("raid_until", self.now() + minutes * 60)
                text += f"\nChat is locked for {minutes:g} minutes."
        posted = self.send(text, reply_markup={"inline_keyboard": [[{"text": "Open the post", "url": url}]]})
        if posted:
            self.tg.call("pinChatMessage", chat_id=self.cfg.chat_id, message_id=posted["message_id"])
            self.kv_set("raid_msg", posted["message_id"])

    def end_raid_lock(self):
        until = self.kv("raid_until")
        if not until or self.now() < float(until):
            return
        self.tg.call("setChatPermissions", chat_id=self.cfg.chat_id, use_independent_chat_permissions=True,
                     permissions=json.loads(self.kv("raid_perms") or json.dumps(OPEN)))
        if self.kv("raid_msg"):
            self.tg.call("unpinChatMessage", chat_id=self.cfg.chat_id, message_id=int(self.kv("raid_msg")))
        for key in ("raid_until", "raid_perms", "raid_msg"):
            self.kv_set(key, None)
        self.send("Raid over. Chat is open again.")

    # ---------- buy alerts ----------

    def post_buys(self):
        if not self.cfg.buy_alerts or self.now() < self.next_buy_poll:
            return
        self.next_buy_poll = self.now() + BUY_POLL_SECONDS
        try:
            buys = self.watcher.poll()
        except Exception as e:
            log.warning("buy poll failed: %s", e)
            return
        info = chain.token_info(self.cfg.ca) if buys else None
        cap = money(info.get("marketCap") or info.get("fdv")) if info else None
        for b in buys:
            dots = "🟢" * max(1, min(int(b.sol / self.cfg.min_buy_sol), 15))
            self.send(f"<b>${esc(self.cfg.ticker)} buy</b>\n{dots}\n\n"
                      f"{b.sol:.2f} SOL for {amount(b.tokens)} {esc(self.cfg.ticker)}\n"
                      f'Buyer: <a href="https://solscan.io/account/{b.buyer}">{b.buyer[:4]}…{b.buyer[-4:]}</a>'
                      + (f"\nMarket cap: {cap}" if cap else "") + "\n"
                      f'<a href="https://solscan.io/tx/{b.signature}">Tx</a> · '
                      f'<a href="{self.cfg.chart_url}">Chart</a> · <a href="{self.cfg.buy_url}">Buy</a>')

    # ---------- callbacks / periodic work ----------

    def answer(self, cq, text):
        self.tg.call("answerCallbackQuery", callback_query_id=cq["id"], text=text)

    def on_callback(self, cq):
        if ((cq.get("message") or {}).get("chat") or {}).get("id") != self.cfg.chat_id:
            return
        kind, _, value = (cq.get("data") or "").partition(":")
        if not value.isdigit():
            return
        if kind == "v":
            self.verify(cq, int(value))
        elif kind == "vote":
            self.vote(cq, int(value))

    def tick(self):
        if self.cfg.chat_id is None:
            return
        self.expire_pending()
        self.end_raid_lock()
        due = [m for t, m in self.ephemeral if t <= self.now()]
        self.ephemeral = [(t, m) for t, m in self.ephemeral if t > self.now()]
        for msg_id in due:
            self.delete(msg_id)
        self.post_buys()

    INFO = {"ca": cmd_ca, "contract": cmd_ca, "site": cmd_site, "website": cmd_site, "buy": cmd_buy,
            "chart": cmd_chart, "price": cmd_price, "rules": cmd_rules, "help": cmd_help, "start": cmd_help}
    MEMBER = {"rank": cmd_rank, "top": cmd_top, "submit": cmd_submit}
    ADMIN = {"warn": cmd_warn, "unwarn": cmd_unwarn, "mute": cmd_mute, "unmute": cmd_unmute,
             "ban": cmd_ban, "contest": cmd_contest, "raid": cmd_raid}


def main():
    cfg = Config()
    if not cfg.token:
        raise SystemExit("DRDOOM_BOT_TOKEN is not set (see drdoom_bot/.env.example)")
    tg = Tg(cfg.token)
    me = tg.call("getMe")
    if not me:
        raise SystemExit("Telegram rejected the token")
    if cfg.chat_ref.startswith("@"):
        chat = tg.call("getChat", chat_id=cfg.chat_ref)
        if not chat:
            raise SystemExit(f"Cannot find {cfg.chat_ref}: is it public, and is the bot a member?")
        if chat["type"] == "channel":
            raise SystemExit(f"{cfg.chat_ref} is a broadcast channel. The bot moderates a GROUP: "
                             "use the channel's linked discussion group instead.")
        cfg.chat_id = chat["id"]
    bot = Bot(tg, cfg, ROOT / "drdoom.db")
    log.info("running as @%s, chat_id=%s, buy alerts %s", me["username"], cfg.chat_id,
             "on" if cfg.buy_alerts else "off")
    if cfg.chat_id is None:
        log.info("DRDOOM_CHAT_ID not set: add the bot to the group and send /id there")
    while True:
        updates = tg.call("getUpdates", offset=int(bot.kv("offset", 0)), timeout=10,
                          allowed_updates=["message", "callback_query"])
        if updates is None:
            time.sleep(5)
        for update in updates or []:
            bot.kv_set("offset", update["update_id"] + 1)
            try:
                bot.handle(update)
            except Exception:
                log.exception("update %s failed", update.get("update_id"))
        try:
            bot.tick()
        except Exception:
            log.exception("tick failed")


if __name__ == "__main__":
    main()
