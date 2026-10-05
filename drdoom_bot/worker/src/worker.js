// DRDOOM community bot as a Cloudflare Worker — port of ../bot.py + ../chain.py.
//
// Differences from the Python version, forced by the platform:
//   - Telegram pushes updates to this Worker (webhook) instead of being long-polled.
//   - State lives in D1 (schema.sql), including self-deleting notices.
//   - Periodic work (gate timeouts, raid unlock, buy alerts) runs from a 1-minute cron,
//     so buy alerts arrive up to a minute late.
//   - The free plan allows 50 outbound requests per invocation; `budget` keeps the buy
//     poll inside that.
//
// It also does one thing the Python version doesn't: a meme queue. Admins send finished
// meme images to the bot in a private chat; it posts one at each of MEME_HOURS.
//
// Secrets: BOT_TOKEN, WEBHOOK_SECRET. Vars: see wrangler.toml.

const RANKS = [[0, "Bystander"], [10, "Henchman"], [50, "Minion"], [150, "Enforcer"],
               [400, "Lieutenant"], [1000, "Mastermind"]];
const POINT_COOLDOWN = 20;
const MAX_WARNS = 3;
const MUTE_HOURS = 24;
const NOTICE_TTL = 45;
const ADMIN_CACHE_TTL = 300;
const SIG_PAGE = 40;
const SUBREQUEST_BUDGET = 45;   // of the free plan's 50
const BUY_SEND_RESERVE = 12;    // requests kept back for posting alerts and housekeeping

const SOL_ADDRESS = /\b[1-9A-HJ-NP-Za-km-z]{32,44}\b/g;
const EVM_ADDRESS = /\b0x[a-fA-F0-9]{40}\b/g;
const BARE_URL = /(?:https?:\/\/|www\.|t\.me\/)\S+/gi;

export const MUTED = { can_send_messages: false };
export const OPEN = Object.fromEntries([
  "can_send_messages", "can_send_audios", "can_send_documents", "can_send_photos",
  "can_send_videos", "can_send_video_notes", "can_send_voice_notes", "can_send_polls",
  "can_send_other_messages", "can_add_web_page_previews", "can_invite_users"].map((k) => [k, true]));

const esc = (s) => String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;");
const mention = (u) => `<a href="tg://user?id=${u.id}">${esc(u.first_name || "villain")}</a>`;
const rankFor = (points) => RANKS.filter(([need]) => points >= need).pop()[1];
const isNumber = (s) => /^\d+(\.\d+)?$/.test(s);

function short(x, prefix = "") {
  x = Number(x || 0);
  for (const [div, suffix] of [[1e9, "B"], [1e6, "M"], [1e3, "K"]]) {
    if (Math.abs(x) >= div) return `${prefix}${(x / div).toFixed(2)}${suffix}`;
  }
  return `${prefix}${x.toFixed(prefix ? 2 : 0)}`;
}

function hostOf(url) {
  try {
    return new URL(url.includes("://") ? url : "https://" + url).hostname.toLowerCase().replace(/^www\./, "");
  } catch {
    return "";
  }
}

// A transaction is a buy when its fee payer ends up with more of the token and less SOL.
// True on the Pump.fun curve and on the AMM after graduation. SOL includes network fees.
export function parseBuy(tx, mint, signature = "") {
  const meta = tx?.meta;
  if (!meta || meta.err) return null;
  const first = tx.transaction.message.accountKeys[0];
  const payer = typeof first === "string" ? first : first.pubkey;
  const held = (balances) => (balances || [])
    .filter((b) => b.mint === mint && b.owner === payer)
    .reduce((sum, b) => sum + Number(b.uiTokenAmount.uiAmountString || 0), 0);
  const tokens = held(meta.postTokenBalances) - held(meta.preTokenBalances);
  const sol = (meta.preBalances[0] - meta.postBalances[0]) / 1e9;
  return tokens > 0 && sol > 0 ? { signature, buyer: payer, sol, tokens } : null;
}

export class Bot {
  constructor(env, { fetch = globalThis.fetch.bind(globalThis), now = () => Date.now() / 1000 } = {}) {
    this.db = env.DB;
    this.fetch = fetch;
    this.now = now;
    this.requests = 0;
    const site = env.SITE || "https://drdoom.pages.dev/";
    this.cfg = {
      token: env.BOT_TOKEN,
      chatId: Number(env.CHAT_ID),
      ca: env.CA || "HoTY1CsQCCUvhmjx3ikMVCMrYTsBfENQxzKmC7ebpump",
      ticker: env.TICKER || "DRDOOM",
      site,
      rpcUrl: env.SOLANA_RPC_URL || "https://api.mainnet-beta.solana.com",
      buyAlerts: (env.BUY_ALERTS ?? "1") === "1",
      minBuySol: Number(env.MIN_BUY_SOL || 0.1),
      gateMinutes: Number(env.GATE_TIMEOUT_MIN || 5),
      memeHours: (env.MEME_HOURS ?? "").split(",").map((h) => h.trim()).filter(Boolean).map(Number),   // UTC
      memeChat: env.MEME_CHAT_ID ? Number(env.MEME_CHAT_ID) : Number(env.CHAT_ID),
      allowed: [hostOf(site), ...(env.ALLOWED_DOMAINS || "x.com,twitter.com").split(",")
        .map((d) => d.trim().toLowerCase()).filter(Boolean)],
      // Links to these are fine only when they point at OUR token, not someone else's.
      tokenDomains: ["pump.fun", "dexscreener.com", "solscan.io"],
    };
    this.cfg.buyUrl = `https://pump.fun/coin/${this.cfg.ca}`;
    this.cfg.chartUrl = `https://dexscreener.com/solana/${this.cfg.ca}`;
  }

  // ---------- storage / telegram helpers ----------

  one(sql, ...args) { return this.db.prepare(sql).bind(...args).first(); }
  async all(sql, ...args) { return (await this.db.prepare(sql).bind(...args).all()).results; }
  run(sql, ...args) { return this.db.prepare(sql).bind(...args).run(); }

  async kv(key) { return (await this.one("SELECT v FROM kv WHERE k=?", key))?.v ?? null; }
  kvSet(key, value) {
    return value == null ? this.run("DELETE FROM kv WHERE k=?", key)
      : this.run("INSERT OR REPLACE INTO kv VALUES (?, ?)", key, String(value));
  }

  async http(url, init) {
    this.requests++;
    return this.fetch(url, init);
  }

  // Call a Bot API method. Returns `result`, or null on any failure (logged).
  async tg(method, params = {}) {
    try {
      const r = await this.http(`https://api.telegram.org/bot${this.cfg.token}/${method}`, {
        method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(params) });
      const body = await r.json();
      if (body.ok) return body.result;
      console.warn(`${method} rejected: ${body.description}`);
    } catch (e) {
      console.warn(`${method} failed: ${e}`);
    }
    return null;
  }

  async send(text, { chatId, ttl, ...extra } = {}) {
    const msg = await this.tg("sendMessage", { chat_id: chatId ?? this.cfg.chatId, text, parse_mode: "HTML",
                                               disable_web_page_preview: true, ...extra });
    if (msg && ttl) await this.run("INSERT INTO ephemeral VALUES (?, ?)", msg.message_id, this.now() + ttl);
    return msg;
  }

  del(msgId) { return this.tg("deleteMessage", { chat_id: this.cfg.chatId, message_id: msgId }); }
  restrict(userId, permissions, extra = {}) {
    return this.tg("restrictChatMember", { chat_id: this.cfg.chatId, user_id: userId, permissions, ...extra });
  }

  async isAdmin(msg) {
    // Anonymous admins post "as the group"; their `from` is a placeholder account.
    if (msg.sender_chat?.id === this.cfg.chatId) return true;
    let cache = JSON.parse((await this.kv("admins")) || "null");
    if (!cache || this.now() - cache.ts > ADMIN_CACHE_TTL) {
      const got = await this.tg("getChatAdministrators", { chat_id: this.cfg.chatId });
      if (got) {
        cache = { ts: this.now(), ids: got.map((a) => a.user.id) };
        await this.kvSet("admins", JSON.stringify(cache));
      }
    }
    return !!cache && cache.ids.includes(msg.from?.id);
  }

  // ---------- update routing ----------

  async handle(update) {
    if (update.callback_query) return this.onCallback(update.callback_query);
    const msg = update.message;
    if (!msg) return;
    if (msg.chat.type === "private") return this.onPrivate(msg);
    if (msg.chat.id === this.cfg.chatId) return this.onGroup(msg);
  }

  // Private chat: info commands for anyone, the meme queue for group admins.
  async onPrivate(msg) {
    const [cmd] = this.parseCommand(msg);
    const chatId = msg.chat.id;
    if ((msg.photo || cmd === "memes" || cmd === "meme") && (await this.isAdmin(msg))) {
      if (cmd === "memes") return this.cmdMemes(msg, "", chatId);
      if (cmd === "meme") return this.cmdMeme(msg, "", chatId);
      return this.addMeme(msg);
    }
    return (INFO[cmd] || INFO.help).call(this, chatId);
  }

  async onGroup(msg) {
    for (const user of msg.new_chat_members || []) if (!user.is_bot) await this.gate(user);
    if (msg.new_chat_members || msg.left_chat_member || msg.is_automatic_forward) return;
    const user = msg.from || {};
    const admin = await this.isAdmin(msg);
    if (!admin) {
      if (await this.one("SELECT 1 AS x FROM pending WHERE user_id=?", user.id ?? 0)) return this.del(msg.message_id);
      const reason = this.violation(msg);
      if (reason) {
        await this.del(msg.message_id);
        return this.warn(user, reason);
      }
    }
    const [cmd, arg] = this.parseCommand(msg);
    if (INFO[cmd]) return INFO[cmd].call(this, this.cfg.chatId);
    if (MEMBER[cmd]) return MEMBER[cmd].call(this, msg, arg);
    if (ADMIN[cmd]) return admin ? ADMIN[cmd].call(this, msg, arg) : undefined;
    if (!admin || !msg.sender_chat) return this.award(user, msg);
  }

  parseCommand(msg) {
    const text = (msg.text || msg.caption || "").trim();
    if (!text.startsWith("/")) return ["", ""];
    const space = text.indexOf(" ");
    const head = space < 0 ? text : text.slice(0, space);
    return [head.slice(1).split("@")[0].toLowerCase(), space < 0 ? "" : text.slice(space + 1).trim()];
  }

  // ---------- entry gate ----------

  async gate(user) {
    if (!(await this.restrict(user.id, MUTED))) return;  // not admin yet, or the user is an admin
    const minutes = this.cfg.gateMinutes;
    const msg = await this.send(
      `${mention(user)}, the lair has a door policy. Tap below within ${minutes} minutes to prove you're human.`,
      { reply_markup: { inline_keyboard: [[{ text: "I am not a bot", callback_data: `v:${user.id}` }]] } });
    await this.run("INSERT OR REPLACE INTO pending VALUES (?, ?, ?)", user.id, msg?.message_id ?? null,
                   this.now() + minutes * 60);
  }

  async verify(cq, userId) {
    if (cq.from.id !== userId) return this.answer(cq, "This button is for someone else.");
    const row = await this.one("SELECT msg_id FROM pending WHERE user_id=?", userId);
    if (!row) return this.answer(cq, "Already verified.");
    await this.restrict(userId, OPEN);
    await this.run("DELETE FROM pending WHERE user_id=?", userId);
    if (row.msg_id) await this.del(row.msg_id);
    await this.answer(cq, "Welcome to the lair.");
    await this.send(`${mention(cq.from)} has entered the chat.\n\n` +
                    "/ca for the token address · /rules for the house rules\n" +
                    "<b>Admins never DM first.</b> Anyone who does is a scammer.", { ttl: 300 });
  }

  async expirePending() {
    for (const row of await this.all("SELECT user_id, msg_id FROM pending WHERE deadline < ? LIMIT 5", this.now())) {
      // Ban then unban = kick: they can rejoin and try again.
      await this.tg("banChatMember", { chat_id: this.cfg.chatId, user_id: row.user_id });
      await this.tg("unbanChatMember", { chat_id: this.cfg.chatId, user_id: row.user_id, only_if_banned: true });
      if (row.msg_id) await this.del(row.msg_id);
      await this.run("DELETE FROM pending WHERE user_id=?", row.user_id);
    }
  }

  // ---------- moderation ----------

  violation(msg) {
    if (msg.forward_origin || msg.forward_from || msg.forward_from_chat) return "forwarded messages";
    const text = msg.text || msg.caption || "";
    const urls = text.match(BARE_URL) || [];
    for (const ent of [...(msg.entities || []), ...(msg.caption_entities || [])]) {
      if (ent.type === "text_link") urls.push(ent.url);
      // Telegram offsets are UTF-16 code units, which is how JS indexes strings.
      else if (ent.type === "url") urls.push(text.substr(ent.offset, ent.length));
    }
    if (urls.some((u) => !this.urlAllowed(u))) return "links";
    const addresses = [...(text.match(SOL_ADDRESS) || []), ...(text.match(EVM_ADDRESS) || [])];
    if (addresses.some((a) => a !== this.cfg.ca)) return "other contract addresses";
    return null;
  }

  urlAllowed(url) {
    const host = hostOf(url);
    const under = (domains) => domains.some((d) => host === d || host.endsWith("." + d));
    return under(this.cfg.allowed) || (under(this.cfg.tokenDomains) && url.includes(this.cfg.ca));
  }

  async warn(user, reason) {
    const { n } = await this.one("INSERT INTO warns VALUES (?, 1) ON CONFLICT(user_id) DO UPDATE SET n = n + 1 " +
                                 "RETURNING n", user.id);
    if (n >= MAX_WARNS) {
      await this.mute(user.id, MUTE_HOURS);
      await this.run("DELETE FROM warns WHERE user_id=?", user.id);
      return this.send(`${mention(user)} is muted for ${MUTE_HOURS}h (${esc(reason)}).`, { ttl: NOTICE_TTL });
    }
    return this.send(`${mention(user)}: no ${esc(reason)} here. Warning ${n}/${MAX_WARNS}.`, { ttl: NOTICE_TTL });
  }

  mute(userId, hours) {
    return this.restrict(userId, MUTED, { until_date: Math.floor(this.now() + hours * 3600) });
  }

  // The user an admin command is aimed at: the author of the replied-to message.
  async target(msg) {
    const user = msg.reply_to_message?.from;
    if (!user) await this.send("Reply to the user's message with that command.", { ttl: NOTICE_TTL });
    return user;
  }

  async cmdWarn(msg, arg) {
    const user = await this.target(msg);
    if (user) await this.warn(user, arg || "breaking the rules");
  }

  async cmdUnwarn(msg) {
    const user = await this.target(msg);
    if (!user) return;
    await this.run("DELETE FROM warns WHERE user_id=?", user.id);
    await this.send(`Warnings cleared for ${mention(user)}.`, { ttl: NOTICE_TTL });
  }

  async cmdMute(msg, arg) {
    const user = await this.target(msg);
    if (!user) return;
    const hours = isNumber(arg) ? Number(arg) : MUTE_HOURS;
    await this.mute(user.id, hours);
    await this.send(`${mention(user)} is muted for ${hours}h.`, { ttl: NOTICE_TTL });
  }

  async cmdUnmute(msg) {
    const user = await this.target(msg);
    if (!user) return;
    await this.restrict(user.id, OPEN);
    await this.send(`${mention(user)} can speak again.`, { ttl: NOTICE_TTL });
  }

  async cmdBan(msg) {
    const user = await this.target(msg);
    if (!user) return;
    await this.tg("banChatMember", { chat_id: this.cfg.chatId, user_id: user.id });
    await this.del(msg.reply_to_message.message_id);
    await this.send(`${mention(user)} has been banished.`, { ttl: NOTICE_TTL });
  }

  // ---------- info commands ----------

  cmdCa(chatId) {
    return this.send(`<b>$${esc(this.cfg.ticker)} token address</b>\n<code>${this.cfg.ca}</code>\n\n` +
                     "Tap to copy. Any other address posted here is fake.", { chatId });
  }

  cmdSite(chatId) { return this.send(esc(this.cfg.site), { chatId }); }

  cmdBuy(chatId) {
    return this.send(`<a href="${this.cfg.buyUrl}">Buy $${esc(this.cfg.ticker)} on Pump.fun</a>\n` +
                     `<code>${this.cfg.ca}</code>\n\nMeme coin. No promises, no financial advice.`, { chatId });
  }

  cmdChart(chatId) {
    return this.send(`<a href="${this.cfg.chartUrl}">DexScreener</a> · <a href="${this.cfg.buyUrl}">Pump.fun</a>`,
                     { chatId });
  }

  async cmdPrice(chatId) {
    const p = await this.tokenInfo();
    if (!p) return this.send("No market data yet. Try /chart.", { chatId });
    const change = p.priceChange?.h24;
    const tx = p.txns?.h24 || {};
    return this.send([
      `<b>$${esc(this.cfg.ticker)}</b>  $${esc(p.priceUsd ?? "?")}` +
        (change != null ? `  (${change >= 0 ? "+" : ""}${Number(change).toFixed(1)}% 24h)` : ""),
      `Market cap: ${short(p.marketCap || p.fdv, "$")}`,
      `24h volume: ${short(p.volume?.h24, "$")}`,
      `24h trades: ${tx.buys || 0} buys / ${tx.sells || 0} sells`,
      `<a href="${this.cfg.chartUrl}">Chart</a>`].join("\n"), { chatId });
  }

  cmdRules(chatId) {
    return this.send("<b>House rules</b>\n" +
                     "1. No links, forwards or other contract addresses.\n" +
                     "2. No spam, no begging, no impersonating admins.\n" +
                     "3. Admins never DM first. Report anyone who does.\n" +
                     "4. Nobody here can promise you a price. Don't spend what you can't lose.\n" +
                     `${MAX_WARNS} warnings = ${MUTE_HOURS}h mute.`, { chatId });
  }

  cmdHelp(chatId) {
    return this.send("<b>DRDOOM bot</b>\n" +
                     "/ca · /buy · /price · /chart · /site · /rules\n" +
                     "/rank · /top — villain ranks from chat activity\n" +
                     "/submit — enter the meme contest (send a photo with /submit as the caption)\n\n" +
                     "<i>Admins:</i> /contest start &lt;topic&gt; · /contest end · /raid &lt;x link&gt; [lock min] · /meme · /memes · " +
                     "/warn /unwarn /mute [h] /unmute /ban (as a reply)\n" +
                     "Send me photos in a private chat to add them to the meme queue.", { chatId });
  }

  // Most liquid DexScreener pair for the token, or null if it isn't indexed yet.
  async tokenInfo() {
    try {
      const r = await this.http(`https://api.dexscreener.com/latest/dex/tokens/${this.cfg.ca}`);
      const pairs = (await r.json()).pairs || [];
      return pairs.sort((a, b) => (b.liquidity?.usd || 0) - (a.liquidity?.usd || 0))[0] || null;
    } catch (e) {
      console.warn(`dexscreener failed: ${e}`);
      return null;
    }
  }

  // ---------- ranks ----------

  async award(user, msg) {
    if (!user.id || user.is_bot) return;
    if ((msg.text || "").length < 3 && !msg.photo) return;
    const row = await this.one("SELECT points, last_ts FROM activity WHERE user_id=?", user.id);
    const points = row?.points ?? 0;
    if (this.now() - (row?.last_ts ?? 0) < POINT_COOLDOWN) return;
    await this.run("INSERT OR REPLACE INTO activity VALUES (?, ?, ?, ?)", user.id, user.first_name || "villain",
                   points + 1, this.now());
    if (rankFor(points + 1) !== rankFor(points)) {
      await this.send(`${mention(user)} has been promoted to <b>${rankFor(points + 1)}</b>.`);
    }
  }

  async cmdRank(msg) {
    const user = msg.from || {};
    const points = (await this.one("SELECT points FROM activity WHERE user_id=?", user.id ?? 0))?.points ?? 0;
    const next = RANKS.map(([need]) => need).find((need) => need > points);
    await this.send(`${mention(user)}: <b>${rankFor(points)}</b>, ${points} points.` +
                    (next ? ` ${next - points} more to the next rank.` : ""));
  }

  async cmdTop() {
    const rows = await this.all("SELECT name, points FROM activity ORDER BY points DESC LIMIT 10");
    if (!rows.length) return this.send("No villains ranked yet.");
    return this.send("<b>Most wanted</b>\n" + rows.map(
      (r, i) => `${i + 1}. ${esc(r.name)} — ${rankFor(r.points)} (${r.points})`).join("\n"));
  }

  // ---------- meme contest ----------

  activeContest() { return this.one("SELECT id, topic FROM contest WHERE active=1 ORDER BY id DESC"); }

  async cmdContest(msg, arg) {
    const [action, ...rest] = arg.split(" ");
    const current = await this.activeContest();
    if (action === "start") {
      if (current) return this.send("A contest is already running. /contest end first.", { ttl: NOTICE_TTL });
      const topic = rest.join(" ").trim() || "Best DRDOOM meme";
      await this.run("INSERT INTO contest (topic, active, started) VALUES (?, 1, ?)", topic, this.now());
      return this.send(`<b>Meme contest: ${esc(topic)}</b>\n\nSend a photo with <code>/submit</code> as the ` +
                       "caption. One entry each. Vote with the button under each entry.");
    }
    if (action === "end") {
      if (!current) return this.send("No contest is running.", { ttl: NOTICE_TTL });
      await this.run("UPDATE contest SET active=0 WHERE id=?", current.id);
      const rows = await this.all("SELECT name, votes, file_id FROM entry WHERE contest_id=? " +
                                  "ORDER BY votes DESC, id LIMIT 3", current.id);
      if (!rows.length) return this.send(`Contest over: ${esc(current.topic)}. No entries.`);
      const board = rows.map((r, i) => `${i + 1}. ${esc(r.name)} — ${r.votes} votes`).join("\n");
      return this.tg("sendPhoto", { chat_id: this.cfg.chatId, photo: rows[0].file_id, parse_mode: "HTML",
                                    caption: `<b>Contest over: ${esc(current.topic)}</b>\n\n${board}` });
    }
    return this.send("Use /contest start &lt;topic&gt; or /contest end.", { ttl: NOTICE_TTL });
  }

  async cmdSubmit(msg) {
    const current = await this.activeContest();
    const user = msg.from || {};
    if (!current) return this.send("No contest is running right now.", { ttl: NOTICE_TTL });
    const source = msg.photo ? msg : msg.reply_to_message || {};
    if (!source.photo || source.from?.id !== user.id) {
      return this.send("Send your own photo with /submit as the caption.", { ttl: NOTICE_TTL });
    }
    const name = user.first_name || "villain";
    const fileId = source.photo[source.photo.length - 1].file_id;
    const entry = await this.one("INSERT OR IGNORE INTO entry (contest_id, user_id, name, file_id) " +
                                 "VALUES (?, ?, ?, ?) RETURNING id", current.id, user.id, name, fileId);
    if (!entry) return this.send(`${mention(user)}, one entry per contest.`, { ttl: NOTICE_TTL });
    const posted = await this.tg("sendPhoto", {
      chat_id: this.cfg.chatId, photo: fileId, parse_mode: "HTML",
      caption: `Entry by ${esc(name)} — ${esc(current.topic)}`, reply_markup: voteMarkup(entry.id, 0) });
    if (posted) await this.run("UPDATE entry SET msg_id=? WHERE id=?", posted.message_id, entry.id);
  }

  async vote(cq, entryId) {
    const row = await this.one("SELECT e.user_id, e.msg_id, c.active FROM entry e JOIN contest c " +
                               "ON c.id = e.contest_id WHERE e.id=?", entryId);
    if (!row || !row.active) return this.answer(cq, "This contest is over.");
    if (row.user_id === cq.from.id) return this.answer(cq, "You can't vote for yourself.");
    const added = await this.one("INSERT OR IGNORE INTO vote VALUES (?, ?) RETURNING entry_id", entryId, cq.from.id);
    if (!added) return this.answer(cq, "You already voted for this one.");
    const { votes } = await this.one("UPDATE entry SET votes = votes + 1 WHERE id=? RETURNING votes", entryId);
    await this.tg("editMessageReplyMarkup", { chat_id: this.cfg.chatId, message_id: row.msg_id,
                                              reply_markup: voteMarkup(entryId, votes) });
    return this.answer(cq, "Vote counted.");
  }

  // ---------- raids ----------

  async cmdRaid(msg, arg) {
    const [url, rest = ""] = arg.split(" ");
    if (!["x.com", "twitter.com"].includes(hostOf(url || "-"))) {
      return this.send("Use /raid &lt;x.com link&gt; [lock minutes].", { ttl: NOTICE_TTL });
    }
    const minutes = isNumber(rest) ? Math.min(Number(rest), 60) : 0;
    let text = "<b>RAID</b>\n\nLike, reply and repost. Bring the villain energy.";
    if (minutes) {
      const chat = (await this.tg("getChat", { chat_id: this.cfg.chatId })) || {};
      if (await this.tg("setChatPermissions", { chat_id: this.cfg.chatId, permissions: MUTED })) {
        await this.kvSet("raid_perms", JSON.stringify(chat.permissions || OPEN));
        await this.kvSet("raid_until", this.now() + minutes * 60);
        text += `\nChat is locked for ${minutes} minutes.`;
      }
    }
    const posted = await this.send(text, { reply_markup: { inline_keyboard: [[{ text: "Open the post", url }]] } });
    if (posted) {
      await this.tg("pinChatMessage", { chat_id: this.cfg.chatId, message_id: posted.message_id });
      await this.kvSet("raid_msg", posted.message_id);
    }
  }

  async endRaidLock() {
    const until = await this.kv("raid_until");
    if (!until || this.now() < Number(until)) return;
    await this.tg("setChatPermissions", { chat_id: this.cfg.chatId, use_independent_chat_permissions: true,
                                          permissions: JSON.parse((await this.kv("raid_perms")) || JSON.stringify(OPEN)) });
    const pinned = await this.kv("raid_msg");
    if (pinned) await this.tg("unpinChatMessage", { chat_id: this.cfg.chatId, message_id: Number(pinned) });
    await this.run("DELETE FROM kv WHERE k IN ('raid_until', 'raid_perms', 'raid_msg')");
    await this.send("Raid over. Chat is open again.");
  }

  // ---------- buy alerts ----------

  async rpc(method, params) {
    const r = await this.http(this.cfg.rpcUrl, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }) });
    if (!r.ok) throw new Error(`${method}: HTTP ${r.status}`);
    const body = await r.json();
    if (body.error) throw new Error(`${method}: ${JSON.stringify(body.error)}`);
    return body.result;
  }

  // Buys newer than the last signature seen. The first run only records a starting point.
  async pollBuys() {
    const last = await this.kv("last_sig");
    const opts = { limit: SIG_PAGE, commitment: "confirmed", ...(last ? { until: last } : {}) };
    const sigs = (await this.rpc("getSignaturesForAddress", [this.cfg.ca, opts])) || [];
    if (!sigs.length) return [];
    await this.kvSet("last_sig", sigs[0].signature);
    if (!last) return [];
    const buys = [];
    for (const s of sigs.filter((x) => !x.err).reverse()) {
      if (this.requests >= SUBREQUEST_BUDGET - BUY_SEND_RESERVE) break;  // older ones first; the rest are skipped
      try {
        const tx = await this.rpc("getTransaction", [s.signature, {
          encoding: "jsonParsed", commitment: "confirmed", maxSupportedTransactionVersion: 1 }]);
        const buy = parseBuy(tx, this.cfg.ca, s.signature);
        if (buy && buy.sol >= this.cfg.minBuySol) buys.push(buy);
      } catch (e) {  // one bad fetch must not drop the rest of the batch
        console.warn(`getTransaction ${s.signature.slice(0, 12)} failed: ${e.message}`);
      }
    }
    return buys;
  }

  async postBuys() {
    if (!this.cfg.buyAlerts) return;
    let buys;
    try {
      buys = await this.pollBuys();
    } catch (e) {
      console.warn(`buy poll failed: ${e.message}`);
      return;
    }
    if (!buys.length) return;
    const info = await this.tokenInfo();
    const cap = info ? short(info.marketCap || info.fdv, "$") : null;
    const t = esc(this.cfg.ticker);
    for (const b of buys) {
      if (this.requests >= SUBREQUEST_BUDGET) break;
      const dots = "🟢".repeat(Math.max(1, Math.min(Math.floor(b.sol / this.cfg.minBuySol), 15)));
      await this.send(`<b>$${t} buy</b>\n${dots}\n\n${b.sol.toFixed(2)} SOL for ${short(b.tokens)} ${t}\n` +
                      `Buyer: <a href="https://solscan.io/account/${b.buyer}">${b.buyer.slice(0, 4)}…${b.buyer.slice(-4)}</a>` +
                      (cap ? `\nMarket cap: ${cap}` : "") + "\n" +
                      `<a href="https://solscan.io/tx/${b.signature}">Tx</a> · ` +
                      `<a href="${this.cfg.chartUrl}">Chart</a> · <a href="${this.cfg.buyUrl}">Buy</a>`);
    }
  }

  // ---------- meme queue ----------

  memesWaiting() { return this.one("SELECT COUNT(*) AS n FROM meme WHERE posted_at IS NULL").then((r) => r.n); }

  // An admin sent a photo in private chat. Telegram keeps the file; we only store its id.
  async addMeme(msg) {
    const photo = msg.photo[msg.photo.length - 1];
    const added = await this.one("INSERT OR IGNORE INTO meme (file_id, unique_id, caption, added_by) VALUES (?, ?, ?, ?) " +
                                 "RETURNING id", photo.file_id, photo.file_unique_id, msg.caption ?? null, msg.from.id);
    const n = await this.memesWaiting();
    return this.send(added ? `Added. ${n} in the queue.` : `Already have that one. ${n} in the queue.`,
                     { chatId: msg.chat.id });
  }

  // Post the oldest unposted meme. Returns false when the queue is empty.
  async postMeme() {
    const meme = await this.one("SELECT id, file_id, caption, added_by FROM meme WHERE posted_at IS NULL ORDER BY id LIMIT 1");
    if (!meme) return false;
    const sent = await this.tg("sendPhoto", { chat_id: this.cfg.memeChat, photo: meme.file_id,
                                              ...(meme.caption ? { caption: meme.caption } : {}) });
    if (!sent) return false;
    await this.run("UPDATE meme SET posted_at=? WHERE id=?", this.now(), meme.id);
    if ((await this.memesWaiting()) === 0) {
      await this.send("That was the last meme in the queue. Send me more photos to refill it.", { chatId: meme.added_by });
    }
    return true;
  }

  async cmdMeme(msg, arg, chatId = this.cfg.chatId) {
    if (!(await this.postMeme())) {
      await this.send("The meme queue is empty. Send photos to me in a private chat to fill it.", { chatId, ttl: chatId === this.cfg.chatId ? NOTICE_TTL : undefined });
    }
  }

  async cmdMemes(msg, arg, chatId = this.cfg.chatId) {
    const hours = this.cfg.memeHours.map((h) => `${String(h).padStart(2, "0")}:00`).join(" and ") || "never";
    await this.send(`${await this.memesWaiting()} memes in the queue. Posting daily at ${hours} UTC.`,
                    { chatId, ttl: chatId === this.cfg.chatId ? NOTICE_TTL : undefined });
  }

  async memeTick() {
    const d = new Date(this.now() * 1000);
    if (!this.cfg.memeHours.includes(d.getUTCHours())) return;
    const slot = d.toISOString().slice(0, 13);   // one scheduled meme per UTC hour
    if ((await this.kv("meme_slot")) === slot) return;
    await this.kvSet("meme_slot", slot);
    await this.postMeme();
  }

  // ---------- callbacks / periodic work ----------

  answer(cq, text) { return this.tg("answerCallbackQuery", { callback_query_id: cq.id, text }); }

  async onCallback(cq) {
    if (cq.message?.chat?.id !== this.cfg.chatId) return;
    const [kind, value] = (cq.data || "").split(":");
    if (!/^\d+$/.test(value || "")) return;
    if (kind === "v") return this.verify(cq, Number(value));
    if (kind === "vote") return this.vote(cq, Number(value));
  }

  async tick() {
    await this.expirePending();
    await this.endRaidLock();
    const due = await this.all("SELECT msg_id FROM ephemeral WHERE delete_at <= ? LIMIT 10", this.now());
    for (const { msg_id } of due) {
      await this.del(msg_id);
      await this.run("DELETE FROM ephemeral WHERE msg_id=?", msg_id);
    }
    await this.postBuys();
    await this.memeTick();
  }
}

export const voteMarkup = (entryId, votes) =>
  ({ inline_keyboard: [[{ text: `🔥 ${votes}`, callback_data: `vote:${entryId}` }]] });

const P = Bot.prototype;
const INFO = { ca: P.cmdCa, contract: P.cmdCa, site: P.cmdSite, website: P.cmdSite, buy: P.cmdBuy,
               chart: P.cmdChart, price: P.cmdPrice, rules: P.cmdRules, help: P.cmdHelp, start: P.cmdHelp };
const MEMBER = { rank: P.cmdRank, top: P.cmdTop, submit: P.cmdSubmit };
const ADMIN = { warn: P.cmdWarn, unwarn: P.cmdUnwarn, mute: P.cmdMute, unmute: P.cmdUnmute,
                ban: P.cmdBan, contest: P.cmdContest, raid: P.cmdRaid, meme: P.cmdMeme, memes: P.cmdMemes };

export default {
  async fetch(request, env, ctx) {
    // Only Telegram knows the secret it was given in setWebhook; everyone else gets nothing.
    if (request.method !== "POST" || !env.WEBHOOK_SECRET ||
        request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("not found", { status: 404 });
    }
    const update = await request.json();
    // Answer Telegram right away so a slow handler never triggers a redelivery.
    ctx.waitUntil(new Bot(env).handle(update).catch((e) => console.error("update failed", e)));
    return new Response("ok");
  },

  async scheduled(event, env, ctx) {
    ctx.waitUntil(new Bot(env).tick().catch((e) => console.error("tick failed", e)));
  },
};
