"""Replies stay where they belong (monitor.py's own_channel_problem):
after the owner-DM gate, every outbox item must answer a message the
monitor queued (item, else reply_to, through burst fragments), in that
message's own channel. A run woken by anyone (and maybe prompt-injected)
can no longer post under a made-up item, or under a real item into some
other channel: an ownerless or paused server, a place where replies are
off, or a DM. Normal and multi-part replies, threads, bursts and
released never_share holds work as before.

Usage: [MUT_DIR=<tree>] python3 tests/ownerdm/test_replies_bound.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_binding as tb  # noqa: E402
from settings_harness import (CH, CH2, CH_G2, G1, MS, OWNER,  # noqa: E402
                              THREAD, U1, U2, bump, dmsg, gmsg, quiet, sf,
                              write)

DM_U1, DM_U2 = tb.DM_U1, "400000000000000080"


class _B(tb.Base):
    def setUp(self):
        super().setUp()
        self.mod.DM_CHANNEL_USER[DM_U2] = U2

    def forged(self, cid, **kw):
        return dict({"item": sf(), "channel_id": cid, "text": "hello there"},
                    **kw)

    def config(self, extra):
        write(self.mod.cfg_path, tb.CFG + extra)
        bump(self.mod.cfg_path)

    def apply(self, changes):
        MS.apply(self.mod.cfg_path, changes, OWNER, role="owner")
        bump(os.path.join(self.mod.tmp, "config.discord.yaml"))

    def assert_refused_here(self, cid):
        self.assertEqual(self.sends(cid), [], self.out)
        self.assert_refused()


class GeneralPath(_B):
    """Review H's repros on main: a made-up item id, any channel_id."""

    def test_ownerless_server_channel(self):
        self.mod.OWNER_IN_GUILD[G1] = False
        self.outbox(self.forged(CH))
        self.assert_refused_here(CH)

    def test_paused_server_channel(self):
        self.apply(MS.pause_changes(MS.Place.server(G1)))
        self.outbox(self.forged(CH2))
        self.assert_refused_here(CH2)

    def test_server_where_replies_are_off(self):
        self.config("all_servers:\n  who:\n    replies: off\n")
        self.outbox(self.forged(CH_G2))
        self.assert_refused_here(CH_G2)

    def test_dm_of_someone_the_dm_settings_refuse(self):
        self.config("dms:\n  who:\n    dms_from: owners\n")
        self.outbox(self.forged(DM_U2))
        self.assert_refused_here(DM_U2)

    def test_real_item_into_another_channel(self):
        """A stranger's real mention, answered into someone's DM or
        another server's channel."""
        s = self.stranger_mention()
        for cid in (DM_U1, CH_G2):
            self.outbox(self.reply(s, "psst", channel_id=cid))
            self.assert_refused_here(cid)
        self.assertIn(s["id"], self.queued())    # stays open
        self.outbox(self.reply(s, "hi"))
        self.assertEqual(len(self.sends(CH)), 1, self.out)

    def test_nothing_named_or_entirely_unknown(self):
        for item in ({"channel_id": CH, "text": "loose"},
                     {"channel_id": CH, "reply_to": sf(), "text": "late"},
                     {"channel_id": CH, "reply_to": sf(), "silent": True},
                     self.forged(CH, reply_to=sf(), reactions=["✅"])):
            self.outbox(item)
            self.assert_refused_here(CH)
        self.assertEqual(self.reactions, [])


class StillWorks(_B):
    def test_reply_and_multi_part(self):
        s = self.stranger_mention()
        u = self.queue(dmsg(U1, "hello", channel=DM_U1))
        self.outbox(self.reply(s, "hi"),
                    dict(self.reply(u), text=None, parts=["one", "two"]))
        self.assertEqual([x["content"] for x in self.sends(CH)], ["hi"])
        self.assertEqual([x["content"] for x in self.sends(DM_U1)],
                         ["one", "two"])
        self.assertEqual(self.queued(), [])

    def test_thread_reply_goes_to_the_thread(self):
        t = self.queue(gmsg(U1, "<@bot> hi", mention=True, channel=THREAD),
                       dm=False, guild=G1)
        self.outbox(self.reply(t, "parent?", channel_id=CH))
        self.assert_refused_here(CH)
        self.outbox(self.reply(t, "here"))
        self.assertEqual([x["content"] for x in self.sends(THREAD)],
                         ["here"])

    def test_burst_under_its_last_fragment(self):
        f1 = gmsg(U1, "one", mention=True)
        f2 = gmsg(U1, "two")
        trig = gmsg(U1, "three")
        self.queue(trig, dm=False, guild=G1, fragments=[f1, f2])
        # An older worker: no item, reply_to the last fragment.
        self.outbox({"channel_id": CH, "reply_to": f2["id"], "text": "ok"})
        self.assertEqual([x["content"] for x in self.sends(CH)], ["ok"],
                         self.out)
        self.assertEqual(self.queued(), [])

    def test_released_hold_is_sent(self):
        self.config('everywhere:\n  privacy:\n    never_share: ["falcon"]\n')
        s = self.stranger_mention()
        self.outbox(self.reply(s, "project falcon is"))
        self.assertEqual(self.sends(CH), [])
        hid = next(iter(self.st["holds"]))
        # A forged hold id lets nothing past.
        self.outbox(self.forged(CH, _hold="0" * 12))
        self.assert_refused_here(CH)
        with quiet():
            self.mod.apply_record(self.st, {"kind": "hold_choice",
                                            "hold": hid, "action": "send",
                                            "by": OWNER})
        self.process()
        self.assertEqual([x["content"] for x in self.sends(CH)],
                         ["project falcon is"], self.out)


if __name__ == "__main__":
    unittest.main()
