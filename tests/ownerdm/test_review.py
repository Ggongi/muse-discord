"""Regressions from the adversarial review of the owner-DM binding
(PR #34, review D): burst fragments forgotten by the bounded
reply_aliases, non-canonical ids, a wake run straddling the monitor's
nonce and queue writes, owner DM channels the monitor's records forgot,
nonces in the rejected copy of an unparseable outbox, and discord-api.py
on a DM listed without recipients.

Usage: [MUT_DIR=<tree>] python3 tests/ownerdm/test_review.py"""
import json
import os
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "_lib"))
from common import MUT_DIR  # noqa: E402
from settings_harness import (CH, G1, OWNER, U1, dmsg, gmsg,  # noqa: E402
                              quiet, state)
import test_binding as TB  # noqa: E402
from test_binding import DM, DM_U1, Base  # noqa: E402


def churn_aliases(mod, st, n=501):
    """Other people's bursts: alias_fragments keeps only the newest 500
    fragment -> trigger aliases (state["reply_aliases"])."""
    for i in range(n):
        mod.alias_fragments(st, "7%017d" % i, [{"id": "8%017d" % i}])


class BurstFragments(Base):
    """A fragment of a queued owner DM is that DM, whatever reply_aliases
    (bounded, fed by everyone's bursts) still remembers."""

    def burst(self):
        trig = dmsg(OWNER, "so", channel=DM)
        frag = dmsg(OWNER, "what do you think?", channel=DM)
        self.queue(trig, fragments=[frag])
        return trig, frag

    def real(self, trig, frag):
        self.outbox({"item": trig["id"], "channel_id": DM,
                     "reply_to": frag["id"], "text": "real",
                     "owner_dm_nonce": self.nonce(trig["id"])})
        self.assertEqual([s["content"] for s in self.sends(DM)], ["real"],
                         self.out)
        self.assertEqual(self.queued(), [])
        self.assertNotIn(trig["id"], self.st["nl_nonces"])

    def test_real_reply_under_the_fragment_after_eviction(self):
        trig, frag = self.burst()
        self.assertEqual(self.st["nl_nonces"][trig["id"]]["fragments"],
                         [frag["id"]])
        churn_aliases(self.mod, self.st)
        self.assertNotIn(frag["id"], self.st["reply_aliases"])
        self.real(trig, frag)

    def test_stranger_silent_under_the_fragment_after_eviction(self):
        trig, frag = self.burst()
        churn_aliases(self.mod, self.st)
        for cid in (CH, DM):
            self.outbox({"channel_id": cid, "reply_to": frag["id"],
                         "silent": True})
            self.assert_refused("owner DM")
        self.assert_open(trig)
        self.assertNotIn(frag["id"], self.st["replied_to"])
        self.real(trig, frag)

    def test_state_lost_real_reply_under_the_fragment(self):
        trig, frag = self.burst()
        self.st = state(self.mod)
        with quiet():
            self.mod.owner_dm_upkeep(self.st, force=True)
        self.assertEqual(self.st["nl_nonces"][trig["id"]]["fragments"],
                         [frag["id"]])
        self.real(trig, frag)

    def test_state_lost_stranger_silent_under_the_fragment(self):
        trig, frag = self.burst()
        self.st = state(self.mod)
        # Before the upkeep re-mints too: the queue's burst is the record.
        self.outbox({"channel_id": CH, "reply_to": frag["id"],
                     "silent": True})
        self.assertIn(trig["id"], self.queued(), self.out)
        with quiet():
            self.mod.owner_dm_upkeep(self.st, force=True)
        self.outbox({"channel_id": CH, "reply_to": frag["id"],
                     "silent": True})
        self.assertIn(trig["id"], self.queued(), self.out)
        self.assertNotIn(frag["id"], self.st["replied_to"])


class CanonicalIds(Base):
    """Ids are compared as strings: only the canonical form (ASCII
    digits, no leading zero) is taken."""

    def dm_traffic(self):
        return [p for p, _ in self.mod.api.posts if DM in p]

    def test_leading_zero_channel(self):
        self.owner_dm()
        self.outbox({"channel_id": "0" + DM, "standalone": True,
                     "text": "Hi boss, please approve the new settings"})
        self.assertEqual(self.dm_traffic(), [], self.out)
        self.assertIn("not a Discord id", self.rejected()[-1]["reason"])

    def test_leading_zero_reaction(self):
        m = self.owner_dm()
        self.outbox({"channel_id": "0" + DM, "reply_to": "0" + m["id"],
                     "reactions": ["✅"]})
        self.assertEqual(self.reactions, [], self.out)
        self.assert_open(m)

    def test_other_non_canonical_forms(self):
        m = self.owner_dm()
        arabic = DM.translate(str.maketrans("0123456789",
                                            "٠١٢٣"
                                            "٤٥٦٧"
                                            "٨٩"))
        for cid in (arabic, " " + DM, DM + "\n", "+" + DM, "00" + DM):
            self.outbox({"channel_id": cid, "standalone": True,
                         "text": "x"})
            self.assertIn("not a Discord id",
                          self.rejected()[-1]["reason"], repr(cid))
        self.outbox({"item": "0" + m["id"], "channel_id": DM,
                     "reply_to": m["id"], "silent": True})
        self.assertIn("not a Discord id", self.rejected()[-1]["reason"])
        self.assertEqual(self.dm_traffic(), [])
        self.assert_open(m)
        # Numbers are canonical once written as strings.
        self.outbox({"item": int(m["id"]), "channel_id": int(DM),
                     "reply_to": int(m["id"]), "text": "real",
                     "owner_dm_nonce": self.nonce(m["id"])})
        self.assertEqual([s["content"] for s in self.sends(DM)], ["real"])

    def test_outbox_append_refuses_them_too(self):
        r = subprocess.run(
            [sys.executable, os.path.join(MUT_DIR, "bin",
                                          "outbox-append.py"), "/dev/null"],
            capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)   # (not a JSON list)
        sys.path.insert(0, os.path.join(MUT_DIR, "bin"))
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "outbox_append_review", os.path.join(MUT_DIR, "bin",
                                                 "outbox-append.py"))
        oa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(oa)
        for bad in ("0" + DM, DM.replace("4", "\u0664"), "0", True, -1):
            self.assertTrue(oa.problems({"channel_id": bad, "text": "x"}),
                            repr(bad))
        self.assertEqual(oa.problems({"channel_id": int(DM), "text": "x"}),
                         [])


class ForgottenChannels(Base):
    """The monitor's records of the owners' DM channels are best effort;
    a channel it cannot place is looked up, failing closed."""

    def forget(self):
        self.mod.DM_CHANNEL_USER.clear()
        for i in range(301):
            self.mod.note_item_meta(self.st, "6%017d" % i, channel_id=CH,
                                    dm=False, owner_dm=False)

    def test_owner_dm_channel_forgotten(self):
        m = self.owner_dm()
        self.assert_real_reply_goes_out(m)
        self.forget()
        # The stub API cannot look the channel up: refused.
        self.outbox({"channel_id": DM, "standalone": True,
                     "text": "unsolicited"})
        self.assertEqual(len(self.sends(DM)), 1, self.out)
        self.assertIn("could not be looked up",
                      self.rejected()[-1]["reason"])
        # The API says it is a DM with an owner (or with nobody listed).
        for recips in ([{"id": OWNER}], []):
            self.mod._CHANNEL_LOOKUP.clear()
            self.mod.api_get = lambda path, r=recips: {
                "id": DM, "type": 1, "recipients": r}
            self.outbox({"channel_id": DM, "standalone": True,
                         "text": "unsolicited"})
            self.assertEqual(len(self.sends(DM)), 1, self.out)
            self.assertIn("names no owner DM", self.rejected()[-1]["reason"])

    def test_known_and_looked_up_channels_still_work(self):
        s = self.stranger_mention()
        u = self.queue(dmsg(U1, "hello", channel=DM_U1))
        self.forget()
        self.mod.DM_CHANNEL_USER.clear()
        self.st["item_meta"].pop(u["id"], None)
        lookups = []

        def get(path):
            lookups.append(path)
            return {"id": path.rsplit("/", 1)[1], "type": 1,
                    "recipients": [{"id": U1}]}
        self.mod.api_get = get
        # A guild channel the bot reads, and a DM whose item's record is
        # gone but which is still queued there: no lookup.
        self.outbox(self.reply(s, "hi"), self.reply(u, "hi u1"))
        self.assertEqual([x["content"] for x in self.sends(CH)], ["hi"])
        self.assertEqual([x["content"] for x in self.sends(DM_U1)],
                         ["hi u1"])
        self.assertEqual(lookups, [])
        # Nothing on record at all (an older worker's late item): looked
        # up once, a non-owner's DM: sent.
        self.outbox({"channel_id": DM_U1, "text": "late", "standalone":
                     True}, {"channel_id": DM_U1, "text": "later",
                             "standalone": True})
        self.assertEqual([x["content"] for x in self.sends(DM_U1)],
                         ["hi u1", "late", "later"])
        self.assertEqual(lookups, [f"/channels/{DM_U1}"])


class RejectedCopies(Base):
    def test_unparseable_or_non_list_outbox_is_scrubbed(self):
        m = self.owner_dm()
        real = self.nonce(m["id"])
        with open(self.mod.OUTBOX_FILE, "w") as f:
            json.dump({"items": [self.reply(m, owner_dm_nonce=real)]}, f)
        self.process()
        with open(self.mod.OUTBOX_FILE, "w") as f:
            f.write('[{"owner_dm_nonce": "%s", "text": ' % real)
        os.utime(self.mod.OUTBOX_FILE, (1, 1))   # not mid-write
        self.process()
        with open(self.mod.OUTBOX_REJECTED_FILE) as f:
            raw = f.read()
        self.assertEqual(len(json.loads(raw)), 2)
        self.assertNotIn(real, raw)
        self.assertIn("(redacted)", raw)
        self.assert_open(m)


class UpkeepFirst(Base):
    def test_upkeep_runs_even_when_the_rest_fails(self):
        m = self.owner_dm()
        self.st["nl_nonces"].clear()
        self.mod._OWNER_DM_UPKEEP["next"] = 0

        def boom(st):
            raise RuntimeError("boom")
        self.mod.nl_note_changes = boom
        with quiet():
            with self.assertRaises(RuntimeError):
                self.mod.nl_upkeep(self.st)
        self.assertIn(m["id"], self.st["nl_nonces"])


class StrangerUnaffected(Base):
    def test_stranger_mention_reply_still_works(self):
        s = self.queue(gmsg(U1, "<@bot> hi", mention=True), dm=False,
                       guild=G1)
        self.outbox(self.reply(s, "hi"))
        self.assertEqual([x["content"] for x in self.sends(CH)], ["hi"])


@unittest.skipUnless(shutil.which("jq"), "jq not installed")
class HookWithoutNonce(TB.WithTheWakeHook):
    """An owner DM in the queue before its nonce is in the file (a run
    straddling the monitor's writes, or a failed nonce write) is never
    handed out or leased without it, and nothing in the outbox counts as
    answering it meanwhile."""

    def test_owner_dm_waits_for_its_nonce(self):
        m = self.owner_dm()
        n = self.nonce(m["id"])
        with open(self.mod.OWNER_DM_NONCES_FILE, "w") as f:
            json.dump({}, f)
        self.assertIsNone(self.wake())
        if os.path.exists(self.env["HOOK_STATE"]):
            with open(self.env["HOOK_STATE"]) as f:
                self.assertNotIn(m["id"], json.load(f).get("woken") or {})
        with open(self.mod.OWNER_DM_NONCES_FILE, "w") as f:
            json.dump({m["id"]: n}, f)
        again = self.wake()
        self.assertEqual([(i["message_id"], i.get("owner_dm_nonce"))
                          for i in again["items"]], [(m["id"], n)])

    def test_others_go_out_while_it_waits(self):
        m = self.owner_dm()
        s = self.stranger_mention()
        with open(self.mod.OWNER_DM_NONCES_FILE, "w") as f:
            json.dump({}, f)
        p = self.wake()
        self.assertEqual([i["message_id"] for i in p["items"]], [s["id"]])
        self.assertNotIn("owner_dm_nonce", json.dumps(p))
        self.assertIn(m["id"], self.queued())

    def test_forged_entry_never_counts_while_it_waits(self):
        m = self.owner_dm()
        n = self.nonce(m["id"])
        with open(self.mod.OWNER_DM_NONCES_FILE, "w") as f:
            json.dump({}, f)
        with open(self.mod.OUTBOX_FILE, "w") as f:
            json.dump([{"item": m["id"], "channel_id": CH,
                        "reply_to": m["id"], "silent": True}], f)
        self.assertIsNone(self.wake())
        with open(self.mod.OWNER_DM_NONCES_FILE, "w") as f:
            json.dump({m["id"]: n}, f)
        p = self.wake()
        self.assertEqual([i["message_id"] for i in p["items"]], [m["id"]])


if __name__ == "__main__":
    unittest.main()
