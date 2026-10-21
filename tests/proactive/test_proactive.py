"""Reaching out on its own (proactive.py; PROTOCOL.md, "Unprompted
messages"): off by default, the Friends preset turns it on, heartbeats
at jittered times within their frequency, and an unprompted message goes
only to one of its heartbeat slot's targets, with the slot's nonce,
while the slot is fresh and unspent, within the caps, outside quiet
hours, never to someone who did not DM the bot first, never into an
ownerless or paused place, never twice without an answer, and never to
someone who opted out (which only they can do). The owner-DM gate still
refuses everything else. One test runs the real wake hook.

Drives monitor.py's main-loop tick (PRO.tick) and process_outbox with
the stubbed API and a fake clock near real time.

Usage: [MUT_DIR=<tree>] python3 tests/proactive/test_proactive.py"""
import json
import os
import shutil
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
from common import HOOK_RUNTIME, MUT_DIR, scratch  # noqa: E402
from settings_harness import (BOT, CH, CH2, CH_G2, G1, OWNER, U1,  # noqa: E402
                              U2, MS, bump, dmsg, gmsg, load_monitor, quiet,
                              sf, state)

DM_U1 = "400000000000000079"
DM_U2 = "400000000000000080"
OWNER_DM = "4" + OWNER[1:]          # what the fake API opens for OWNER
HOOK = os.path.join(MUT_DIR, "hooks", "mention-watch.sh")
HOUR = 3600

BASE = f"""version: 1
owners: ["{OWNER}"]
token_from: env:MUSE_TEST_TOKEN
servers:
  "{G1}":
    name: Lounge
"""
ON = f"""version: 1
owners: ["{OWNER}"]
token_from: env:MUSE_TEST_TOKEN
everywhere:
  proactive:
    quiet_hours: "off"
    per_person_per_week: 2
dms:
  who:
    dms_from: anyone
  proactive:
    enabled: on
servers:
  "{G1}":
    name: Lounge
    proactive:
      enabled: on
"""


class Base(unittest.TestCase):
    CFG = ON

    def setUp(self):
        self.m = load_monitor(self.CFG)
        m = self.m
        self.now = time.time()
        m.PRO.clock = lambda: self.now
        m.DM_CHANNELS[DM_U1] = "u1"
        self.st = state(m)
        # Quiet channels: the last message an hour ago, from a person.
        for cid in (CH, CH2, CH_G2):
            self.talk(cid, U2, age=HOUR)
        self.out = ""

    # -- helpers -------------------------------------------------------------
    def talk(self, cid, uid, age=0.0, bot=False):
        mid = sf(age)
        self.st.setdefault("history", {}).setdefault(cid, []).append(
            {"id": mid, "author": "x", "author_id": uid, "bot": bot,
             "text": "hi", "ts": None})
        return mid

    def dmed(self, uid, cid, age=2 * HOUR):
        """uid DMed the bot (a queued DM, as enqueue_message records)."""
        mid = sf(age)
        self.m.note_item_meta(self.st, mid, author_id=uid, channel_id=cid,
                              bot=False, dm=True, kind="dm", owner_dm=False)
        return mid

    def heartbeat(self):
        """Make the heartbeat due and tick: the slot item, or None."""
        pro = self.m.PRO
        d = pro.data()
        d["freq"] = pro.every("proactive.frequency")
        d["next"] = self.now - 1
        pro._next_tick = 0
        with quiet() as buf:
            pro.tick(self.st, self.now)
        self.out += buf.getvalue()
        slots = [q for q in self.m.load_queue() if q.get("kind") ==
                 "proactive"]
        return slots[-1] if slots else None

    def targets(self, slot):
        return {t["channel_id"] for t in slot["proactive"]["targets"]}

    def nonce(self):
        return self.m.PRO.data()["slot"]["nonce"]

    def post(self, slot, cid, text="hey, how did the exam go?", **kw):
        it = {"item": slot["message_id"], "channel_id": cid, "text": text,
              "owner_dm_nonce": self.nonce()}
        it.update(kw)
        return it

    def outbox(self, *items):
        self.n_rejected = len(self.rejected())
        with open(self.m.OUTBOX_FILE, "w") as f:
            json.dump(list(items), f)
        with quiet() as buf:
            self.m.process_outbox(self.st)
        self.out += buf.getvalue()

    def rejected(self):
        try:
            with open(self.m.OUTBOX_REJECTED_FILE) as f:
                return json.load(f)
        except OSError:
            return []

    def sends(self, cid=None):
        return [d for p, d in self.m.api.posts if p.endswith("/messages")
                and (cid is None or p == f"/channels/{cid}/messages")]

    def apply(self, changes):
        MS.apply(self.m.cfg_path, changes, OWNER, role="owner")
        bump(os.path.join(self.m.tmp, "config.discord.yaml"))

    def assert_refused(self):
        self.assertEqual(self.sends(), [], self.out)
        self.assertGreater(len(self.rejected()), self.n_rejected, self.out)


class OffByDefault(Base):
    CFG = BASE

    def test_no_heartbeat_wakes(self):
        self.dmed(U1, DM_U1)
        for _ in range(50):
            self.now += 6 * HOUR
            self.assertIsNone(self.heartbeat(), self.out)
        self.assertEqual(self.m.load_queue(), [])
        self.assertEqual(self.sends(), [])

    def test_friends_preset_turns_it_on(self):
        self.assertIsNone(self.heartbeat())
        st = MS.load(self.m.cfg_path)
        place = MS.Place.server(G1)
        self.apply(MS.preset_changes(st, "friends", place))
        self.now += HOUR
        slot = self.heartbeat()
        self.assertIsNotNone(slot, self.out)
        # That server's channels only: not the other server, not DMs.
        self.assertEqual(self.targets(slot), {CH, CH2})
        # Work turns it off again.
        self.apply(MS.preset_changes(MS.load(self.m.cfg_path), "work",
                                     place))
        self.assertFalse(self.m.settings().get(
            "proactive.enabled", self.m.scope_for(G1, CH)))


class Schedule(Base):
    def test_frequency_and_jitter_bounds(self):
        pro = self.m.PRO
        for freq, (lo, hi) in (("rarely", (12 * HOUR, 24 * HOUR)),
                               ("sometimes", (4 * HOUR, 6 * HOUR)),
                               ("often", (45 * 60, 75 * 60))):
            gaps = [pro.schedule(self.now, freq) - self.now
                    for _ in range(300)]
            self.assertTrue(all(lo <= g <= hi for g in gaps), freq)
            self.assertGreater(max(gaps) - min(gaps), (hi - lo) / 2, freq)

    def test_never_scheduled_in_quiet_hours(self):
        pro = self.m.PRO
        pro.every = lambda key, _e=pro.every: (
            "22:00-09:00" if key == "proactive.quiet_hours" else _e(key))
        for k in range(300):
            t = pro.schedule(self.now + k * 1234.5, "often")
            hour = time.gmtime(t).tm_hour
            self.assertFalse(hour >= 22 or hour < 9, time.ctime(t))

    def test_one_heartbeat_per_gap(self):
        """Ticking every minute for two days at "rarely" (12-24 h apart)
        wakes 2 to 4 times, never with two slots open."""
        self.m.PRO.data()["next"] = None
        wakes, t0 = 0, self.now
        while self.now < t0 + 48 * HOUR:
            self.m.PRO._next_tick = 0
            with quiet():
                self.m.PRO.tick(self.st, self.now)
            q = [x for x in self.m.load_queue() if x["kind"] == "proactive"]
            self.assertLessEqual(len(q), 1)
            if q:
                wakes += 1
                self.m.queue_edit(drop=lambda x: x["kind"] == "proactive")
                self.m.PRO.data()["slot"] = None
            self.now += 60
        self.assertTrue(2 <= wakes <= 4, wakes)


class Posting(Base):
    def setUp(self):
        super().setUp()
        self.dmed(U1, DM_U1)
        self.slot = self.heartbeat()
        self.assertIsNotNone(self.slot, self.out)

    def test_targets(self):
        # Owners by DM, people who DMed first, quiet channels where it is
        # on (not G2's), never anyone else.
        self.assertEqual(self.targets(self.slot),
                         {OWNER_DM, DM_U1, CH, CH2})
        # No message text from anyone reaches the item.
        self.assertNotIn('"hi"', json.dumps(self.slot))

    def test_post_to_a_target(self):
        self.outbox(self.post(self.slot, DM_U1))
        sent = self.sends(DM_U1)
        self.assertEqual([s["content"] for s in sent],
                         ["hey, how did the exam go?"], self.out)
        self.assertEqual(sent[0]["allowed_mentions"],
                         {"parse": [], "users": [U1]})
        self.assertNotIn(self.slot["message_id"],
                         [q["message_id"] for q in self.m.load_queue()])

    def test_channel_post_pings_nobody(self):
        self.outbox(self.post(self.slot, CH, "<@222000000000000002> @everyone"
                                             " anyone up for a game?"))
        sent = self.sends(CH)
        self.assertEqual(len(sent), 1, self.out)
        self.assertEqual(sent[0]["allowed_mentions"], {"parse": []})
        self.assertNotIn("@everyone", sent[0]["content"])

    def test_outside_targets_refused(self):
        for cid in (DM_U2, CH_G2):
            self.outbox(self.post(self.slot, cid))
            self.assert_refused()
        # Even a place that would be eligible now, when the slot does not
        # list it.
        del self.m.PRO.data()["slot"]["targets"][CH2]
        self.m.PRO.data()["slot"]["spent"] = False
        self.outbox(self.post(self.slot, CH2))
        self.assert_refused()

    def test_bad_or_missing_nonce_refused_slot_untouched(self):
        for n in (None, "0" * 32):
            it = self.post(self.slot, DM_U1)
            if n is None:
                del it["owner_dm_nonce"]
            else:
                it["owner_dm_nonce"] = n
            self.outbox(it)
            self.assert_refused()
        # The real run still can.
        self.outbox(self.post(self.slot, DM_U1))
        self.assertEqual(len(self.sends(DM_U1)), 1, self.out)

    def test_stale_slot_refused(self):
        self.now += 16 * 60
        self.outbox(self.post(self.slot, DM_U1))
        self.assert_refused()

    def test_reused_slot_refused(self):
        self.outbox(self.post(self.slot, CH))
        self.assertEqual(len(self.sends(CH)), 1, self.out)
        self.outbox(self.post(self.slot, DM_U1))
        self.assertEqual(self.sends(DM_U1), [], self.out)
        self.assertGreater(len(self.rejected()), self.n_rejected)
        # Silence spends it too.
        self.now += HOUR
        slot = self.heartbeat()
        self.assertIn(DM_U1, self.targets(slot), self.out)
        n = self.nonce()
        self.outbox({"item": slot["message_id"], "channel_id": DM_U1,
                     "silent": True, "owner_dm_nonce": n})
        self.outbox(self.post(slot, DM_U1, owner_dm_nonce=n))
        self.assertEqual(self.sends(DM_U1), [], self.out)
        self.assertGreater(len(self.rejected()), self.n_rejected)

    def test_over_cap_refused(self):
        # The per person weekly cap (2 here) is reached after the slot
        # was made: the post is refused at send time.
        pro = self.m.PRO
        for k in range(2):
            pro.data()["sent"].append({"at": self.now - (k + 2) * 86400,
                                       "channel_id": DM_U1, "user_id": U1,
                                       "type": "dm"})
        self.outbox(self.post(self.slot, DM_U1))
        self.assert_refused()

    def test_total_cap(self):
        pro = self.m.PRO
        pro.data()["sent"] += [{"at": self.now - 60, "channel_id": "1",
                                "user_id": None, "type": "channel"}] * 2
        self.outbox(self.post(self.slot, CH))
        self.assert_refused()
        self.now += HOUR
        self.assertIsNone(self.heartbeat(), self.out)   # rarely: 2 a day

    def test_no_second_message_without_an_answer(self):
        self.outbox(self.post(self.slot, DM_U1))
        self.assertEqual(len(self.sends(DM_U1)), 1, self.out)
        self.now += 2 * 86400
        slot = self.heartbeat()
        self.assertNotIn(DM_U1, self.targets(slot))
        # They answer: eligible again (a day later, after the quiet gap).
        self.dmed(U1, DM_U1, age=-(self.now - time.time()) + 2 * HOUR)
        self.m.PRO.data()["slot"] = None
        self.m.queue_edit(drop=lambda q: q["kind"] == "proactive")
        slot = self.heartbeat()
        self.assertIn(DM_U1, self.targets(slot), self.out)

    def test_quiet_hours(self):
        h = time.gmtime(self.now).tm_hour
        hours = "%02d:00-%02d:00" % (h, (h + 2) % 24)
        self.m.PRO.every = lambda key, _e=self.m.PRO.every: (
            hours if key == "proactive.quiet_hours" else _e(key))
        self.outbox(self.post(self.slot, DM_U1))
        self.assert_refused()
        self.now += HOUR
        self.m.PRO.data()["slot"] = None
        self.assertIsNone(self.heartbeat())

    def test_slot_cannot_touch_anything_else(self):
        """The slot's item may not reply under, react to, or act on a
        message, nor carry a settings or opt-out action."""
        mid = sf()
        for extra in ({"reply_to": mid}, {"reactions": ["x"]},
                      {"settings": {"v": 1, "changes": []}},
                      {"proactive": "opt-out"}):
            self.outbox(self.post(self.slot, DM_U1, **extra))
            self.assert_refused()


class Eligibility(Base):
    def test_never_dm_someone_who_did_not_dm_first(self):
        # U2 talked to the bot in a server only; U1 DMed it.
        mid = sf(2 * HOUR)
        self.m.note_item_meta(self.st, mid, author_id=U2, channel_id=CH,
                              bot=False, dm=False, kind="mention",
                              owner_dm=False)
        self.dmed(U1, DM_U1)
        # A bot that DMed counts for nothing either.
        self.m.note_item_meta(self.st, sf(2 * HOUR), author_id="333",
                              channel_id=DM_U2, bot=True, dm=True,
                              kind="dm", owner_dm=False)
        slot = self.heartbeat()
        users = {t.get("user_id") for t in slot["proactive"]["targets"]}
        self.assertIn(U1, users)
        self.assertNotIn(U2, users)
        self.assertNotIn("333", users)
        self.assertNotIn(DM_U2, self.targets(slot))

    def test_dm_settings_still_apply(self):
        self.dmed(U1, DM_U1)
        self.apply([MS.Change(MS.Place.dms(), "who.dms_from", "owners")])
        slot = self.heartbeat()
        self.assertNotIn(DM_U1, self.targets(slot))

    def test_never_in_ownerless_or_paused_places(self):
        self.m.OWNER_IN_GUILD[G1] = False      # no owner in G1
        slot = self.heartbeat()
        self.assertFalse({CH, CH2} & self.targets(slot), self.out)
        self.m.OWNER_IN_GUILD.pop(G1)
        self.m.PRO.data()["slot"] = None
        self.m.queue_edit(drop=lambda q: True)
        self.apply(MS.pause_changes(MS.Place.server(G1))
                   + MS.pause_changes(MS.Place.dms()))
        self.assertIsNone(self.heartbeat(), self.out)

    def test_never_into_an_active_conversation(self):
        self.talk(CH, U2, age=60)          # people are talking in CH
        self.talk(CH2, BOT, age=45 * 60)   # the bot spoke last in CH2
        self.dmed(U1, DM_U1, age=5 * 60)   # U1 is talking to the bot
        slot = self.heartbeat()
        self.assertEqual(self.targets(slot), {OWNER_DM}, self.out)


class OptOut(Base):
    def queue_msg(self, msg, dm, guild=None):
        q = []
        with quiet():
            self.assertTrue(self.m.enqueue_message(
                msg, "x", self.st, q, dm=dm, guild_id=guild))
        self.m.queue_edit(add=q)
        return msg

    def reply(self, msg, **kw):
        it = {"item": msg["id"], "channel_id": msg["channel_id"],
              "reply_to": msg["id"], "text": "Got it."}
        it.update(kw)
        return it

    def test_opt_out_honoured_and_only_by_the_person(self):
        self.dmed(U1, DM_U1)
        # U2 asks in a server to stop: only U2 is opted out, whatever
        # the worker writes.
        m2 = self.queue_msg(gmsg(U2, "<@bot> stop messaging me",
                                 mention=True), dm=False, guild=G1)
        self.outbox(self.reply(m2, proactive="opt-out", user_id=U1,
                               author_id=U1))
        self.assertEqual(set(self.m.PRO.data()["opted_out"]), {U2},
                         self.out)
        self.assertEqual(len(self.sends(CH)), 1)   # the reply went out
        slot = self.heartbeat()
        self.assertIn(DM_U1, self.targets(slot))
        # U1 opts out in their DM: never a target again...
        m1 = dmsg(U1, "please stop messaging me", channel=DM_U1)
        m1["id"] = sf(2 * HOUR)    # not a conversation going on
        self.queue_msg(m1, dm=True)
        self.outbox(self.reply(m1, proactive="opt-out"))
        self.assertIn(U1, self.m.PRO.data()["opted_out"])
        self.outbox(self.post(slot, DM_U1))
        self.assertEqual(self.sends(DM_U1)[1:], [], self.out)
        self.now += 3 * 86400
        self.assertNotIn(DM_U1, self.targets(self.heartbeat() or {
            "proactive": {"targets": []}}))
        # ...until they opt back in the same way.
        m1 = self.queue_msg(dmsg(U1, "ok you can message me again",
                                 channel=DM_U1), dm=True)
        self.outbox(self.reply(m1, proactive="opt-in"))
        self.assertNotIn(U1, self.m.PRO.data()["opted_out"])

    def test_opt_out_needs_a_persons_own_message(self):
        # Not on a message the monitor never queued, not on another
        # channel, not on a bot's message.
        for it in ({"item": sf(), "channel_id": CH, "text": "ok",
                    "proactive": "opt-out"},):
            self.outbox(it)
            self.assert_refused()
        m2 = self.queue_msg(gmsg(U2, "<@bot> stop", mention=True), dm=False,
                            guild=G1)
        self.outbox(self.reply(m2, channel_id=CH2, proactive="opt-out"))
        self.assert_refused()
        self.assertEqual(self.m.PRO.data()["opted_out"], {})


class OwnerDmGate(Base):
    def test_slot_nonce_opens_nothing_else(self):
        self.dmed(U1, DM_U1)
        slot = self.heartbeat()
        n = self.nonce()
        # The owner's own DM, queued with its own nonce.
        q = []
        dm = dmsg(OWNER, "hey", channel=OWNER_DM)
        dm["id"] = sf(2 * HOUR)          # not a conversation going on
        with quiet():
            self.m.enqueue_message(dm, "DM", self.st, q, dm=True)
        self.m.queue_edit(add=q)
        for it in (
                # An ordinary item posting into the owner's DM, with the
                # slot's nonce, naming no slot, or naming the owner's DM.
                {"item": sf(), "channel_id": OWNER_DM, "text": "x",
                 "owner_dm_nonce": n},
                {"item": dm["id"], "channel_id": OWNER_DM,
                 "reply_to": dm["id"], "text": "x", "owner_dm_nonce": n},
                {"item": dm["id"], "channel_id": OWNER_DM, "silent": True,
                 "owner_dm_nonce": n},
                # The slot's item replying under the owner's DM.
                self.post(slot, OWNER_DM, reply_to=dm["id"])):
            self.outbox(it)
            self.assert_refused()
        self.assertIn(dm["id"], [x["message_id"]
                                 for x in self.m.load_queue()])
        # A slot post to an owner who is a target is fine.
        self.outbox(self.post(slot, OWNER_DM))
        self.assertEqual(len(self.sends(OWNER_DM)), 1, self.out)


@unittest.skipUnless(shutil.which("jq"), "jq not installed")
class Hook(unittest.TestCase):
    def test_slot_wakes_alone_with_its_nonce(self):
        d = scratch("muse-hook-pro-")
        pkg = os.path.join(d, "pkg")
        os.makedirs(pkg)
        env = dict(os.environ, HATCH_HOOK_RUNTIME=HOOK_RUNTIME,
                   HOOK_STATE=os.path.join(d, "hookstate.json"),
                   MUSE_DISCORD_DIR=pkg,
                   MUSE_DISCORD_CONFIG=os.path.join(pkg, "config.yaml"))
        nonce, odm = "a" * 32, "b" * 32

        def write(name, data):
            with open(os.path.join(pkg, name), "w") as f:
                json.dump(data, f)

        def wake():
            r = subprocess.run(["bash", HOOK], env=env, capture_output=True,
                               text=True, timeout=30)
            lines = r.stdout.splitlines()
            if not lines or not lines[0].startswith("WAKE:"):
                return None
            return json.loads(lines[1])

        slot = {"message_id": "7", "kind": "proactive", "channel_id": None,
                "proactive": {"targets": []}}
        mention = {"message_id": "5", "kind": "mention", "channel_id": "9",
                   "settings": {"where": "server", "speaker": "member"}}
        owner = {"message_id": "1", "kind": "dm", "channel_id": "8",
                 "settings": {"where": "dm", "speaker": "owner"}}
        write("owner_dm_nonces.json", {"7": nonce, "1": odm})
        write("reply_queue.json", [mention, slot, owner])
        got = [(i["message_id"], i.get("owner_dm_nonce"))
               for i in wake()["items"]]
        self.assertEqual(got, [("1", odm)])          # the owner's DM first
        got = [(i["message_id"], i.get("owner_dm_nonce"), i.get("proactive"))
               for i in wake()["items"]]
        self.assertEqual(got, [("7", nonce, {"targets": []})])   # then alone
        p = wake()
        self.assertEqual([i["message_id"] for i in p["items"]], ["5"])
        self.assertNotIn(nonce, json.dumps(p))
        # A forged answer (no nonce) does not hide the slot from its wake.
        os.remove(env["HOOK_STATE"])
        write("reply_queue.json", [slot])
        write("reply_outbox.json", [{"item": "7", "silent": True}])
        self.assertEqual([i["message_id"] for i in wake()["items"]], ["7"])


if __name__ == "__main__":
    unittest.main()
