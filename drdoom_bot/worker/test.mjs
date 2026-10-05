// Offline tests for the Worker: real SQLite behind a D1-shaped shim, fake Telegram/RPC.
// Run:  node --test test.mjs
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { beforeEach, test } from "node:test";

import worker, { Bot, MUTED, OPEN, parseBuy, voteMarkup } from "./src/worker.js";

const CHAT = -100123;
const ADMIN = { id: 1, first_name: "Boss" };
const ALICE = { id: 2, first_name: "Alice" };
const BOB = { id: 3, first_name: "Bob <b>" };
const CA = "HoTY1CsQCCUvhmjx3ikMVCMrYTsBfENQxzKmC7ebpump";
const SCHEMA = readFileSync(new URL("./schema.sql", import.meta.url), "utf8");

function d1() {
  const db = new DatabaseSync(":memory:");
  db.exec(SCHEMA);
  return { prepare: (sql) => ({ bind: (...args) => {
    const st = db.prepare(sql);
    return { first: async () => st.get(...args) ?? null, all: async () => ({ results: st.all(...args) }),
             run: async () => { st.run(...args); return {}; } };
  } }) };
}

let calls, clock, env, mid, nextId, rpc;

function fakeFetch(url, init) {
  const json = (body, status = 200) => new Response(JSON.stringify(body), { status });
  if (url.includes("api.telegram.org")) {
    const method = url.split("/").pop();
    const params = typeof init.body === "string" ? JSON.parse(init.body) : Object.fromEntries(init.body);
    calls.push([method, params]);
    if (method === "getChatAdministrators") return json({ ok: true, result: [{ user: ADMIN }] });
    if (method === "getChat") return json({ ok: true, result: { permissions: { can_send_messages: true, can_send_photos: true } } });
    if (method === "sendMessage" || method === "sendPhoto") return json({ ok: true, result: { message_id: ++nextId } });
    return json({ ok: true, result: true });
  }
  if (url.includes("dexscreener")) return json({ pairs: [{ marketCap: 4200 }] });
  const { method, params } = JSON.parse(init.body);
  return rpc(method, params, json);
}

const bot = () => new Bot(env, { fetch: async (u, i) => fakeFetch(u, i), now: () => clock });
const of = (method) => calls.filter(([m]) => m === method).map(([, p]) => p);
const texts = () => of("sendMessage").map((p) => p.text);
const deleted = () => of("deleteMessage").map((p) => p.message_id);
const answers = () => of("answerCallbackQuery").map((p) => p.text);

async function say(user, text = "", extra = {}) {
  const msg = { message_id: ++mid, chat: { id: CHAT, type: "supergroup" }, from: user, ...extra };
  if (text) msg[extra.photo ? "caption" : "text"] = text;
  await bot().handle({ message: msg });   // a fresh Bot per update, as in production
  return mid;
}

const press = (user, data) =>
  bot().handle({ callback_query: { id: "cq", from: user, data, message: { chat: { id: CHAT } } } });

beforeEach(() => {
  calls = []; clock = 1_000_000; mid = 0; nextId = 1000; rpc = null;
  env = { DB: d1(), BOT_TOKEN: "x", CHAT_ID: String(CHAT), BUY_ALERTS: "0", WEBHOOK_SECRET: "s3cret" };
});

test("gate mutes, then verifies only the joiner", async () => {
  await say(ADMIN, "", { new_chat_members: [ALICE] });
  assert.deepEqual(of("restrictChatMember")[0].permissions, MUTED);
  await press(BOB, `v:${ALICE.id}`);
  assert.equal(of("restrictChatMember").length, 1);
  await press(ALICE, `v:${ALICE.id}`);
  assert.deepEqual(of("restrictChatMember")[1].permissions, OPEN);
  assert.match(texts().at(-1), /Admins never DM first/);
});

test("unverified member is kicked after the timeout", async () => {
  await say(ADMIN, "", { new_chat_members: [ALICE] });
  clock += 60; await bot().tick();
  assert.equal(of("banChatMember").length, 0);
  clock += 300; await bot().tick();
  assert.equal(of("banChatMember")[0].user_id, ALICE.id);
  assert.equal(of("unbanChatMember").length, 1);
});

test("links are deleted for members, not admins", async () => {
  const bad = await say(ALICE, "free airdrop at https://scam.example/claim");
  const ok = await say(ADMIN, "read https://scam.example/claim to see the scam");
  assert.ok(deleted().includes(bad));
  assert.ok(!deleted().includes(ok));
});

test("hidden links and forwards", async () => {
  const hidden = await say(ALICE, "click here", { entities: [{ type: "text_link", offset: 0, length: 5, url: "https://evil.example" }] });
  const emoji = await say(ALICE, "🔥🔥 evil.example", { entities: [{ type: "url", offset: 5, length: 12 }] });
  const fwd = await say(ALICE, "gm", { forward_origin: { type: "channel" } });
  assert.deepEqual(deleted(), [hidden, emoji, fwd]);
});

test("three warnings mute for 24h", async () => {
  for (let i = 0; i < 3; i++) await say(ALICE, "https://scam.example");
  const mutes = of("restrictChatMember");
  assert.equal(mutes.length, 1);
  assert.equal(mutes[0].until_date, clock + 24 * 3600);
  assert.match(texts().at(-1), /muted/);
});

test("admin commands need admin", async () => {
  const target = { message_id: 50, from: BOB };
  await say(ALICE, "/ban", { reply_to_message: target });
  assert.equal(of("banChatMember").length, 0);
  await say(ADMIN, "/ban", { reply_to_message: target });
  assert.equal(of("banChatMember")[0].user_id, BOB.id);
  assert.match(texts().at(-1), /Bob &lt;b&gt;/);
});

test("notices delete themselves on a later tick", async () => {
  await say(ALICE, "https://scam.example");
  const notice = nextId;
  clock += 46; await bot().tick();
  assert.ok(deleted().includes(notice));
  calls = []; await bot().tick();
  assert.equal(deleted().length, 0);
});

test("other chats are ignored; /ca answers in the group and in private", async () => {
  await bot().handle({ message: { message_id: 1, chat: { id: -999, type: "supergroup" }, from: ALICE, text: "/ca" } });
  assert.equal(calls.length, 0);
  await say(ALICE, "/ca@drdoom_lair_bot");
  assert.ok(texts().at(-1).includes(CA));
  await bot().handle({ message: { message_id: 2, chat: { id: 77, type: "private" }, from: ALICE, text: "/ca" } });
  assert.equal(of("sendMessage").at(-1).chat_id, 77);
});

test("admin list is cached across updates", async () => {
  await say(ALICE, "hello there");
  await say(ALICE, "hello again");
  assert.equal(of("getChatAdministrators").length, 1);
});

test("points respect the cooldown and promote at 10", async () => {
  for (let i = 0; i < 5; i++) await say(ALICE, "hello there");
  await say(ALICE, "/rank");
  assert.match(texts().at(-1), /1 points/);
  for (let i = 0; i < 9; i++) { clock += 21; await say(ALICE, "hello again"); }
  assert.match(texts().at(-1), /Henchman/);
});

test("contest: submit once, vote once, no self-votes, closes", async () => {
  const photo = [{ file_id: "small" }, { file_id: "big" }];
  await say(ALICE, "/contest start memes");
  assert.equal(await bot().activeContest(), null);
  await say(ADMIN, "/contest start Villain of the week");
  await say(ALICE, "/submit", { photo });
  await say(ALICE, "/submit", { photo });
  assert.equal(of("sendPhoto").length, 1);
  assert.equal(of("sendPhoto")[0].photo, "big");
  await press(ALICE, "vote:1"); await press(BOB, "vote:1"); await press(BOB, "vote:1");
  assert.deepEqual(answers(), ["You can't vote for yourself.", "Vote counted.", "You already voted for this one."]);
  assert.deepEqual(of("editMessageReplyMarkup").at(-1).reply_markup, voteMarkup(1, 1));
  await say(ADMIN, "/contest end");
  assert.match(of("sendPhoto").at(-1).caption, /1\. Alice — 1 votes/);
  await press(BOB, "vote:1");
  assert.equal(answers().at(-1), "This contest is over.");
});

test("raid locks the chat, then restores the original permissions once", async () => {
  await say(ADMIN, "/raid https://evil.example/x 10");
  assert.equal(of("setChatPermissions").length, 0);
  await say(ADMIN, "/raid https://x.com/drdoom/status/1 10");
  assert.deepEqual(of("setChatPermissions")[0].permissions, MUTED);
  assert.equal(of("pinChatMessage").length, 1);
  clock += 601; await bot().tick();
  assert.deepEqual(of("setChatPermissions")[1].permissions, { can_send_messages: true, can_send_photos: true });
  assert.equal(of("unpinChatMessage").length, 1);
  await bot().tick();
  assert.equal(of("setChatPermissions").length, 2);
});

const bal = (amount, owner) => ({ mint: CA, owner, uiTokenAmount: { uiAmountString: String(amount) } });
const buyTx = (lamports, tokens) => ({
  transaction: { message: { accountKeys: [{ pubkey: "PAYERxxxxWALLET" }] } },
  meta: { err: null, preBalances: [5e9], postBalances: [5e9 - lamports],
          preTokenBalances: [bal(1e6, "CURVE")], postTokenBalances: [bal(tokens, "PAYERxxxxWALLET"), bal(1e6 - tokens, "CURVE")] } });

test("parseBuy: buys only", () => {
  assert.deepEqual(parseBuy(buyTx(5e8, 5000), CA, "s"), { signature: "s", buyer: "PAYERxxxxWALLET", sol: 0.5, tokens: 5000 });
  assert.equal(parseBuy(buyTx(-5e8, 5000), CA), null);
  assert.equal(parseBuy({ ...buyTx(5e8, 5000), meta: { err: {} } }, CA), null);
});

test("buy alerts: silent first run, then posts new buys above the minimum", async () => {
  env.BUY_ALERTS = "1";
  const pages = [[{ signature: "s1" }],
                 [{ signature: "s4" }, { signature: "s3" }, { signature: "s2", err: {} }]];
  const txs = { s3: buyTx(1e9, 9000), s4: buyTx(1e7, 90) };
  const seen = [];
  rpc = (method, params, json) => {
    seen.push([method, params]);
    return json({ result: method === "getSignaturesForAddress" ? pages.shift() : txs[params[0]] });
  };
  await bot().tick();
  assert.equal(of("sendMessage").length, 0);
  await bot().tick();
  assert.equal(seen[1][1][1].until, "s1");
  assert.equal(of("sendMessage").length, 1);
  assert.match(texts()[0], /1\.00 SOL for 9\.00K DRDOOM/);
  assert.match(texts()[0], /Market cap: \$4\.20K/);
});

test("buy poll stays inside the subrequest budget", async () => {
  env.BUY_ALERTS = "1";
  const many = Array.from({ length: 40 }, (_, i) => ({ signature: `s${i}` }));
  const pages = [[{ signature: "old" }], many];
  let total = 0;
  rpc = (method, params, json) => {
    total++;
    return json({ result: method === "getSignaturesForAddress" ? pages.shift() : buyTx(1e9, 100) });
  };
  await bot().tick();
  total = 0; calls = [];
  await bot().tick();
  assert.ok(total + calls.length + 1 <= 50, `used ${total + calls.length + 1} requests`);
  assert.ok(of("sendMessage").length >= 10);
});

test("a failing RPC does not break the tick", async () => {
  env.BUY_ALERTS = "1";
  rpc = (method, params, json) => json({}, 429);
  await bot().tick();
});

test("webhook rejects requests without the secret", async () => {
  const waits = [];
  const ctx = { waitUntil: (p) => waits.push(p) };
  const req = (headers) => new Request("https://w.example/", { method: "POST", headers, body: "{}" });
  assert.equal((await worker.fetch(req({}), env, ctx)).status, 404);
  assert.equal((await worker.fetch(req({ "X-Telegram-Bot-Api-Secret-Token": "wrong" }), env, ctx)).status, 404);
  assert.equal((await worker.fetch(new Request("https://w.example/"), env, ctx)).status, 404);
  assert.equal(waits.length, 0);
  assert.equal((await worker.fetch(req({ "X-Telegram-Bot-Api-Secret-Token": "s3cret" }), env, ctx)).status, 200);
  assert.equal(waits.length, 1);
});

// --- AI memes ---

const IDEA = { top: "I control everything.", bottom: "Except the wifi.", scene_top: "The character points at a map.",
               scene_bottom: "The character shakes a router." };

function fakeAi({ ideas = [IDEA], flagFirst = false } = {}) {
  const log = [];
  let images = 0;
  return { log, async run(model, input) {
    log.push([model, input]);
    if (input.messages) return { choices: [{ message: { content: JSON.stringify(ideas[Math.min(log.filter(([, i]) => i.messages).length - 1, ideas.length - 1)]) } }] };
    if (flagFirst && images++ === 0) throw new Error("AiError: 3030: Your output has been flagged.");
    return { image: Buffer.from("PNGDATA").toString("base64") };
  } };
}

test("/meme is admin-only, queues the job, and the next tick posts one photo", async () => {
  env.AI = fakeAi(); env.REF_IMAGE = new Uint8Array([1, 2, 3]);
  await say(ALICE, "/meme");
  await bot().tick();
  assert.equal(of("sendPhoto").length, 0);
  await say(ADMIN, "/meme the wifi");
  await bot().tick();
  const [photo] = of("sendPhoto");
  assert.equal(photo.chat_id, String(CHAT));
  assert.equal(await photo.photo.text(), "PNGDATA");
  assert.match(env.AI.log[0][1].messages[1].content, /the wifi/);
  const form = await new Response(env.AI.log[1][1].multipart.body, { headers: { "content-type": env.AI.log[1][1].multipart.contentType } }).formData();
  assert.match(form.get("prompt"), /reads "I CONTROL EVERYTHING\." .* reads "EXCEPT THE WIFI\."/);
  assert.equal(form.get("input_image_0").size, 3);
  await bot().tick();
  assert.equal(of("sendPhoto").length, 1);
});

test("scheduled meme posts once per configured UTC hour", async () => {
  env.AI = fakeAi(); env.MEME_HOURS = "10,18";
  clock = Date.UTC(2026, 9, 6, 9, 59) / 1000;
  await bot().tick();
  assert.equal(of("sendPhoto").length, 0);
  clock += 60; await bot().tick(); clock += 60; await bot().tick();
  assert.equal(of("sendPhoto").length, 1);
  clock += 8 * 3600; await bot().tick();
  assert.equal(of("sendPhoto").length, 2);
  const recent = JSON.parse(await bot().kv("meme_recent"));
  assert.equal(recent.length, 2);
  assert.notEqual(recent[0].topic, recent[1].topic);
});

test("a flagged image is retried; unusable or off-limits captions are never posted", async () => {
  env.AI = fakeAi({ flagFirst: true });
  assert.equal(await bot().postMeme(null), true);
  assert.equal(of("sendPhoto").length, 1);
  calls = [];
  env.AI = fakeAi({ ideas: [{ ...IDEA, bottom: "EXCEPT THE COIN PRICE." }, { top: "x" }] });
  await say(ADMIN, "/meme");
  await bot().tick();
  assert.equal(of("sendPhoto").length, 0);
  assert.equal(env.AI.log.filter(([, i]) => i.multipart).length, 0);
  assert.match(texts().at(-1), /jammed/);
});

test("no AI binding: memes are skipped without breaking the tick", async () => {
  env.MEME_HOURS = "10"; clock = Date.UTC(2026, 9, 6, 10, 0) / 1000;
  await bot().tick();
  assert.equal(of("sendPhoto").length, 0);
});
