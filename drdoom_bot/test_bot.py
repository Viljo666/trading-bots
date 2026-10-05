"""Offline tests for the community bot: a fake Telegram records every API call.

Run:  ../.venv/bin/python -m unittest test_bot -v
"""
import unittest

import bot as botmod
import chain

CHAT = -100123
ADMIN = {"id": 1, "first_name": "Boss"}
ALICE = {"id": 2, "first_name": "Alice"}
BOB = {"id": 3, "first_name": "Bob <b>"}
CA = botmod.DEFAULT_CA


class FakeTg:
    def __init__(self):
        self.calls = []
        self.next_id = 1000

    def call(self, method, **params):
        self.calls.append((method, params))
        if method == "getChatAdministrators":
            return [{"user": ADMIN}]
        if method == "getChat":
            return {"permissions": {"can_send_messages": True, "can_send_photos": True}}
        if method in ("sendMessage", "sendPhoto"):
            self.next_id += 1
            return {"message_id": self.next_id}
        return True

    def of(self, method):
        return [p for m, p in self.calls if m == method]

    def texts(self):
        return [p["text"] for p in self.of("sendMessage")]


class Clock:
    t = 1_000_000.0

    def __call__(self):
        return self.t


class BotTest(unittest.TestCase):
    def setUp(self):
        self.tg, self.clock = FakeTg(), Clock()
        cfg = botmod.Config({"DRDOOM_BOT_TOKEN": "x", "DRDOOM_CHAT_ID": str(CHAT), "BUY_ALERTS": "0"})
        self.bot = botmod.Bot(self.tg, cfg, now=self.clock)
        self.mid = 0

    def say(self, user, text="", **extra):
        self.mid += 1
        msg = {"message_id": self.mid, "chat": {"id": CHAT, "type": "supergroup"}, "from": user, **extra}
        if text:
            msg["caption" if "photo" in extra else "text"] = text
        self.bot.handle({"message": msg})
        return self.mid

    def press(self, user, data):
        self.bot.handle({"callback_query": {"id": "cq", "from": user, "data": data,
                                            "message": {"chat": {"id": CHAT}}}})

    def answers(self):
        return [p["text"] for p in self.tg.of("answerCallbackQuery")]

    # --- gate ---

    def test_gate_mutes_then_verifies_only_the_joiner(self):
        self.say(ADMIN, new_chat_members=[ALICE])
        self.assertEqual(self.tg.of("restrictChatMember")[0]["permissions"], botmod.MUTED)
        self.press(BOB, f"v:{ALICE['id']}")
        self.assertEqual(len(self.tg.of("restrictChatMember")), 1)
        self.press(ALICE, f"v:{ALICE['id']}")
        self.assertEqual(self.tg.of("restrictChatMember")[1]["permissions"], botmod.OPEN)
        self.assertIn("Admins never DM first", self.tg.texts()[-1])

    def test_unverified_member_is_kicked_after_timeout(self):
        self.say(ADMIN, new_chat_members=[ALICE])
        self.clock.t += 60
        self.bot.tick()
        self.assertEqual(self.tg.of("banChatMember"), [])
        self.clock.t += 5 * 60
        self.bot.tick()
        self.assertEqual(self.tg.of("banChatMember")[0]["user_id"], ALICE["id"])
        self.assertEqual(len(self.tg.of("unbanChatMember")), 1)

    # --- moderation ---

    def deleted(self):
        return [p["message_id"] for p in self.tg.of("deleteMessage")]

    def test_links_deleted_for_members_not_admins(self):
        bad = self.say(ALICE, "free airdrop at https://scam.example/claim")
        ok = self.say(ADMIN, "read https://scam.example/claim to see the scam")
        self.assertIn(bad, self.deleted())
        self.assertNotIn(ok, self.deleted())

    def test_allowed_and_token_links(self):
        for text in ("https://drdoom.pages.dev/", "look https://x.com/someone/status/1",
                     f"https://pump.fun/coin/{CA}", f"the ca is {CA}"):
            self.assertNotIn(self.say(ALICE, text), self.deleted(), text)
        for text in ("https://pump.fun/coin/9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin",
                     "buy 9xQeWvG816bUx9EPjHmaT23yvVM2ZWbrrpZb9PusVFin instead",
                     "0x52908400098527886E0F7030069857D2E4169EE7", "t.me/otherchat",
                     "https://x.com.evil.io/a"):
            self.assertIn(self.say(ALICE, text), self.deleted(), text)

    def test_hidden_link_and_forward(self):
        hidden = self.say(ALICE, "click here", entities=[{"type": "text_link", "offset": 0, "length": 5,
                                                          "url": "https://evil.example"}])
        emoji = self.say(ALICE, "🔥🔥 evil.example", entities=[{"type": "url", "offset": 5, "length": 12}])
        fwd = self.say(ALICE, "gm", forward_origin={"type": "channel"})
        self.assertEqual(self.deleted(), [hidden, emoji, fwd])

    def test_three_warnings_mute(self):
        for _ in range(3):
            self.say(ALICE, "https://scam.example")
        mutes = self.tg.of("restrictChatMember")
        self.assertEqual(len(mutes), 1)
        self.assertEqual(mutes[0]["until_date"], int(self.clock.t + 24 * 3600))
        self.assertIn("muted", self.tg.texts()[-1])

    def test_admin_commands_need_admin(self):
        target = {"message_id": 50, "from": BOB}
        self.say(ALICE, "/ban", reply_to_message=target)
        self.assertEqual(self.tg.of("banChatMember"), [])
        self.say(ADMIN, "/ban", reply_to_message=target)
        self.assertEqual(self.tg.of("banChatMember")[0]["user_id"], BOB["id"])
        self.assertIn("Bob &lt;b&gt;", self.tg.texts()[-1])

    def test_notices_self_delete(self):
        self.say(ALICE, "https://scam.example")
        notice = self.tg.next_id
        self.clock.t += botmod.NOTICE_TTL + 1
        self.bot.tick()
        self.assertIn(notice, self.deleted())

    def test_other_chats_ignored(self):
        self.bot.handle({"message": {"message_id": 1, "chat": {"id": -999, "type": "supergroup"},
                                     "from": ALICE, "text": "/ca"}})
        self.assertEqual(self.tg.calls, [])

    # --- info / ranks ---

    def test_ca_command(self):
        self.say(ALICE, "/ca@DrDoomBot")
        self.assertIn(CA, self.tg.texts()[-1])

    def test_points_cooldown_and_promotion(self):
        for _ in range(5):
            self.say(ALICE, "hello there")
        self.say(ALICE, "/rank")
        self.assertIn("1 points", self.tg.texts()[-1])
        for _ in range(9):
            self.clock.t += botmod.POINT_COOLDOWN + 1
            self.say(ALICE, "hello again")
        self.assertIn("Henchman", self.tg.texts()[-1])

    # --- contest ---

    def test_contest_flow(self):
        photo = [{"file_id": "small"}, {"file_id": "big"}]
        self.say(ALICE, "/contest start memes")
        self.assertEqual(self.bot.active_contest(), None)
        self.say(ADMIN, "/contest start Villain of the week")
        self.say(ALICE, "/submit", photo=photo)
        self.say(ALICE, "/submit", photo=photo)
        self.assertEqual(len(self.tg.of("sendPhoto")), 1)
        self.assertEqual(self.tg.of("sendPhoto")[0]["photo"], "big")
        self.press(ALICE, "vote:1")
        self.press(BOB, "vote:1")
        self.press(BOB, "vote:1")
        self.assertEqual(self.answers(), ["You can't vote for yourself.", "Vote counted.",
                                          "You already voted for this one."])
        self.assertEqual(self.tg.of("editMessageReplyMarkup")[-1]["reply_markup"],
                         botmod.Bot.vote_markup(1, 1))
        self.say(ADMIN, "/contest end")
        self.assertIn("1. Alice — 1 votes", self.tg.of("sendPhoto")[-1]["caption"])
        self.press(BOB, "vote:1")
        self.assertEqual(self.answers()[-1], "This contest is over.")

    # --- raid ---

    def test_raid_locks_then_restores_permissions(self):
        self.say(ADMIN, "/raid https://evil.example/x 10")
        self.assertEqual(self.tg.of("setChatPermissions"), [])
        self.say(ADMIN, "/raid https://x.com/drdoom/status/1 10")
        self.assertEqual(self.tg.of("setChatPermissions")[0]["permissions"], botmod.MUTED)
        self.assertEqual(len(self.tg.of("pinChatMessage")), 1)
        self.clock.t += 601
        self.bot.tick()
        restored = self.tg.of("setChatPermissions")[1]["permissions"]
        self.assertEqual(restored, {"can_send_messages": True, "can_send_photos": True})
        self.assertEqual(len(self.tg.of("unpinChatMessage")), 1)
        self.bot.tick()
        self.assertEqual(len(self.tg.of("setChatPermissions")), 2)


def tx(payer_pre, payer_post, tok_pre, tok_post, err=None, mint=CA):
    def bal(amount, owner):
        return {"mint": mint, "owner": owner, "uiTokenAmount": {"uiAmountString": str(amount)}}
    return {"transaction": {"message": {"accountKeys": [{"pubkey": "PAYER"}, {"pubkey": "CURVE"}]}},
            "meta": {"err": err, "preBalances": [payer_pre, 0], "postBalances": [payer_post, 0],
                     "preTokenBalances": [bal(tok_pre, "PAYER"), bal(1e6, "CURVE")] if tok_pre else [bal(1e6, "CURVE")],
                     "postTokenBalances": [bal(tok_post, "PAYER"), bal(1e6 - tok_post, "CURVE")]}}


class ChainTest(unittest.TestCase):
    def test_buy_sell_and_failed(self):
        buy = chain.parse_buy(tx(2_000_000_000, 1_500_000_000, 0, 5000), CA, "sig")
        self.assertEqual((buy.buyer, buy.sol, buy.tokens), ("PAYER", 0.5, 5000))
        self.assertIsNone(chain.parse_buy(tx(1_000_000_000, 1_400_000_000, 5000, 0), CA))
        self.assertIsNone(chain.parse_buy(tx(2_000_000_000, 1_500_000_000, 0, 5000, err={"x": 1}), CA))
        self.assertIsNone(chain.parse_buy(tx(2_000_000_000, 1_500_000_000, 0, 5000, mint="OTHER"), CA))

    def test_watcher_first_run_is_silent_then_reports_new_buys(self):
        state = {}
        pages = [[{"signature": "s1"}], [{"signature": "s3"}, {"signature": "s2", "err": {"x": 1}}]]
        txs = {"s3": tx(2_000_000_000, 1_000_000_000, 0, 9000)}
        calls = []

        def fake_rpc(url, method, params):
            calls.append((method, params))
            return pages.pop(0) if method == "getSignaturesForAddress" else txs[params[0]]

        real, chain.rpc, chain.FETCH_PAUSE = chain.rpc, fake_rpc, 0
        try:
            w = chain.BuyWatcher("u", CA, lambda: state.get("s"), lambda s: state.update(s=s), 0.1)
            self.assertEqual(w.poll(), [])
            buys = w.poll()
        finally:
            chain.rpc = real
        self.assertEqual([(b.signature, b.sol) for b in buys], [("s3", 1.0)])
        self.assertEqual(calls[1][1][1]["until"], "s1")
        self.assertEqual(state["s"], "s3")


if __name__ == "__main__":
    unittest.main()
