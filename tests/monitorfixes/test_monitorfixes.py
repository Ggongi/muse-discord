"""monitor.py fixes (2026-10-19), offline:

1. A message refused by a daily cap (limits.per_day /
   per_person_per_day) is dropped at once: ledgered skipped ("daily cap
   reached"), its pending burst removed, never retried, never forced
   through by a restart's flush; the author's later messages are judged
   on their own and nobody else is affected.
2. A directly-addressed message the settings decline (who.replies off,
   who.allow / who.ignore, who.bots: ignore, DMs off for an owner) is
   ledgered skipped, so the sweep does not answer it after the setting is
   relaxed. Ordinary chatter is not ledgered.
3. A brand-new thread found by the thread refresh is seeded at its own
   id, so a mention posted in it before the gateway event was drained is
   still staged (by the drain or the poll), exactly once.
4. Discord system notices (a pin, "started a thread") are never a
   trigger or a follow-up, in servers or DMs.

Usage: [MUT_DIR=<tree>] python3 tests/monitorfixes/test_monitorfixes.py"""
import os
import queue
import re
import sys
import time
import unittest
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "_lib"))
from settings_harness import (BOT, CH, G1, OTHER_BOT,  # noqa: E402
                              OWNER, U1, U2, EPOCH, HTTPErr, bump, dmsg,
                              gmsg, load_monitor, quiet, sf, state, write)

BASE = f"""version: 1
owners: ["{OWNER}"]
token_from: env:MUSE_TEST_TOKEN
"""
DM_CH = "400000000000000077"


def sf_at(ts):
    """A snowflake for wall time ts."""
    return str((int(ts * 1000) - EPOCH) << 22)


class Base(unittest.TestCase):
    cfg = BASE

    def setUp(self):
        self.m = load_monitor(self.cfg)
        self.st = state(self.m)

    def rewrite(self, text):
        write(self.m.cfg_path, text)
        bump(self.m.cfg_path)

    def ledger(self, mid):
        return self.m._ledger_load()["entries"].get(mid)

    def stage(self, msg, guild=G1, cname="Lounge #general"):
        """Classify and stage msg the way the poll / gateway do."""
        with quiet() as buf:
            kind = self.m.classify_message(self.st, msg, cname, guild)
            if kind and kind != "join":
                self.m.stage_found(self.st, msg, cname, guild, kind)
        return kind, buf.getvalue()

    def flush(self, force=False):
        """flush_pending; unforced, every staged burst counts as matured
        (its coalesce window over), as in the main loop."""
        for entry in self.st.get("pending", {}).values():
            entry["first_seen"] = 0
        q = []
        with quiet() as buf:
            self.m.flush_pending(self.st, q, force=force)
        return q, buf.getvalue()

    def sweep(self, channels=None, dms=False):
        q = []
        with quiet():
            self.m.sweep_mentions(self.st, q,
                                  channels if channels is not None
                                  else {CH: self.m.CHANNELS[CH]}, dms=dms)
        return q


# ---------------------------------------------------------------------------
# 1. Daily caps drop the message at once.
# ---------------------------------------------------------------------------
class DailyCap(Base):
    cfg = BASE + f"""servers:
  "{G1}":
    limits:
      per_person_per_day: 1
"""

    def test_capped_burst_is_dropped_not_retried(self):
        m = self.m
        first = gmsg(U1, "@muse one", mention=True)
        self.stage(first)
        q, _ = self.flush()
        self.assertEqual([i["message_id"] for i in q], [first["id"]])

        second = gmsg(U1, "@muse two", mention=True, age=600)
        frag = gmsg(U1, "and a detail", age=599)
        self.stage(second)
        self.assertIsNone(self.stage(frag)[0])  # folded into the burst
        q, out = self.flush()
        self.assertEqual(q, [])
        self.assertIn(f"Rate limit: user {U1} hit per-user daily cap", out)
        self.assertEqual(self.st["pending"], {})  # dropped, not retrying
        self.assertNotIn("retry", out)
        for mid in (second["id"], frag["id"]):
            ent = self.ledger(mid)
            self.assertEqual(ent["status"], "skipped")
            self.assertEqual(ent["reason"], "daily cap reached")

        # A restart's forced flush has nothing left to push through, even
        # on a new day (counters reset).
        self.st["rate"]["day"] = "2000-01-01"
        q, _ = self.flush(force=True)
        self.assertEqual(q, [])
        # ... and the sweep leaves it alone (terminal in the ledger).
        m.api.history[CH] = [second, frag]
        self.assertEqual(self.sweep(), [])

    def test_later_messages_are_judged_on_their_own(self):
        self.stage(gmsg(U1, "@muse one", mention=True))
        self.flush()
        capped = gmsg(U1, "@muse two", mention=True)
        self.stage(capped)
        self.flush()
        # The burst used to stay pending (next_try in 5 min) and swallow
        # this as a fragment; now it opens a burst of its own.
        self.assertEqual(self.st["pending"], {})
        third = gmsg(U1, "@muse three", mention=True)
        self.stage(third)
        entry = self.st["pending"][f"{CH}:{U1}"]
        self.assertEqual(entry["first_id"], third["id"])
        self.assertEqual(entry["fragments"], [])
        q, _ = self.flush()
        self.assertEqual(q, [])  # still over the cap: skipped too
        self.assertEqual(self.ledger(third["id"])["reason"],
                         "daily cap reached")
        # Someone else is unaffected.
        other = gmsg(U2, "@muse hi", mention=True)
        self.stage(other)
        q, _ = self.flush()
        self.assertEqual([i["message_id"] for i in q], [other["id"]])
        # Under the cap again (a new day), the author is answered.
        self.st["rate"]["day"] = "2000-01-01"
        fourth = gmsg(U1, "@muse four", mention=True)
        self.stage(fourth)
        q, _ = self.flush()
        self.assertEqual([i["message_id"] for i in q], [fourth["id"]])

    def test_per_day_total_cap_too(self):
        self.rewrite(BASE + f'servers:\n  "{G1}":\n    limits:\n'
                     "      per_day: 1\n")
        self.stage(gmsg(U1, "@muse one", mention=True))
        self.flush()
        capped = gmsg(U2, "@muse two", mention=True)
        self.stage(capped)
        q, out = self.flush()
        self.assertEqual(q, [])
        self.assertIn("daily cap (1)", out)
        self.assertEqual(self.st["pending"], {})
        self.assertEqual(self.ledger(capped["id"])["status"], "skipped")

    def test_sweep_recovery_hitting_the_cap_is_terminal(self):
        m = self.m
        self.stage(gmsg(U1, "@muse one", mention=True))
        self.flush()
        lost = gmsg(U1, "@muse lost one", mention=True, age=600)
        m.api.history[CH] = [lost]
        self.assertEqual(self.sweep(), [])
        ent = self.ledger(lost["id"])
        self.assertEqual((ent["status"], ent["reason"]),
                         ("skipped", "daily cap reached"))
        self.st["rate"]["day"] = "2000-01-01"
        self.assertEqual(self.sweep(), [])  # not retried later


# ---------------------------------------------------------------------------
# 2. Mentions the settings decline stay declined.
# ---------------------------------------------------------------------------
class RefusedStaysRefused(Base):
    def test_replies_off_then_on(self):
        m = self.m
        off = BASE + "all_servers:\n  who:\n    replies: off\n"
        self.rewrite(off)
        mention = gmsg(U1, "@muse anyone?", mention=True, age=600)
        reply = gmsg(U2, "hey", reply_to_author=BOT, age=599)
        chatter = gmsg(U1, "just chatting", age=598)
        for msg in (mention, reply, chatter):
            self.assertIsNone(self.stage(msg)[0])
        for msg in (mention, reply):
            ent = self.ledger(msg["id"])
            self.assertEqual(ent["status"], "skipped")
            self.assertIn("who.replies", ent["reason"])
        self.assertIsNone(self.ledger(chatter["id"]))  # chatter: no entry
        # The setting is relaxed: the sweep must not answer them now.
        self.rewrite(BASE)
        lost = gmsg(U2, "@muse never seen by poll or gateway",
                    mention=True, age=590)
        m.api.history[CH] = [mention, reply, chatter, lost]
        self.assertEqual([i["message_id"] for i in self.sweep()],
                         [lost["id"]])  # only the genuinely lost one

    def test_who_ignore_and_allow(self):
        m = self.m
        self.rewrite(BASE + f'everywhere:\n  who:\n    ignore: ["{U1}"]\n')
        ignored = gmsg(U1, "@muse hi", mention=True, age=600)
        self.assertIsNone(self.stage(ignored)[0])
        self.assertIn("who.ignore", self.ledger(ignored["id"])["reason"])
        self.rewrite(BASE + f'everywhere:\n  who:\n    allow: ["{U1}"]\n')
        not_allowed = gmsg(U2, "@muse hi", mention=True, age=600)
        self.assertIsNone(self.stage(not_allowed)[0])
        self.assertEqual(self.ledger(not_allowed["id"])["status"], "skipped")
        self.rewrite(BASE)
        m.api.history[CH] = [ignored, not_allowed]
        self.assertEqual(self.sweep(), [])

    def test_bots_ignore(self):
        m = self.m
        self.rewrite(BASE + "everywhere:\n  who:\n    bots: ignore\n")
        beep = gmsg(OTHER_BOT, "@muse beep", mention=True, bot=True, age=600)
        chatter = gmsg(OTHER_BOT, "beep boop", bot=True, age=599)
        self.assertIsNone(self.stage(beep)[0])
        self.assertIsNone(self.stage(chatter)[0])
        self.assertEqual(self.ledger(beep["id"])["status"], "skipped")
        self.assertIsNone(self.ledger(chatter["id"]))
        self.rewrite(BASE)
        m.api.history[CH] = [beep, chatter]
        self.assertEqual(self.sweep(), [])

    def test_allowed_mentions_are_not_ledgered_at_classify(self):
        mention = gmsg(U1, "@muse hi", mention=True)
        kind, _ = self.stage(mention)
        self.assertEqual(kind, "mention")
        # Staged, not skipped (the ledger says queued only once queued).
        self.assertIsNone(self.ledger(mention["id"]))

    def test_trigger_kind_has_no_side_effects(self):
        self.rewrite(BASE + "all_servers:\n  who:\n    replies: off\n")
        msg = gmsg(U1, "@muse hi", mention=True)
        with quiet():
            self.assertIsNone(self.m.trigger_kind(msg, G1))
        self.assertIsNone(self.ledger(msg["id"]))

    def test_owner_dm_with_dms_off(self):
        m = self.m
        self.rewrite(BASE + "dms:\n  who:\n    replies: off\n")
        m.DM_CHANNELS[DM_CH] = "owner"
        dm = dmsg(OWNER, "you there?")
        dm["id"] = sf(600)
        inbox = queue.Queue()
        inbox.put(dm)
        with quiet():
            self.assertEqual(m.drain_gateway(self.st, inbox,
                                             m.CHANNELS)[2], [])
        self.assertEqual(self.ledger(dm["id"])["status"], "skipped")
        # A stranger's declined DM stays unrecorded.
        stranger = dmsg(U1, "hello", channel="400000000000000078")
        inbox.put(stranger)
        with quiet():
            m.drain_gateway(self.st, inbox, m.CHANNELS)
        self.assertIsNone(self.ledger(stranger["id"]))
        # An owner DM lost while DMs are off is not answered by the sweep
        # either (it used to be: the DM sweep ignored who.replies).
        lost = dmsg(OWNER, "lost while off")
        lost["id"] = sf(590)
        m.api.history[DM_CH] = [dm, lost]
        self.assertEqual(self.sweep({}, dms=True), [])
        # DMs back on: the declined one stays declined.
        self.rewrite(BASE)
        self.assertEqual([i["message_id"] for i in self.sweep({}, dms=True)],
                         [lost["id"]])

    def test_ledger_stays_bounded(self):
        self.rewrite(BASE + "all_servers:\n  who:\n    replies: off\n")
        with quiet():
            for _ in range(1010):
                self.m.note_refused(gmsg(U1, "@muse", mention=True), "#c",
                                    "who.replies is off here")
        self.assertEqual(len(self.m._ledger_load()["entries"]), 1000)


# ---------------------------------------------------------------------------
# 3. Brand-new threads are read from their start.
# ---------------------------------------------------------------------------
class ThreadAPI:
    """Guild/thread listing plus channel history honoring ?after=."""

    def __init__(self, threads):
        self.threads = threads   # [{"id", "parent_id", "name"}]
        self.history = {}

    def get(self, path):
        if path == "/users/@me/guilds":
            return [{"id": G1, "name": "Lounge"}]
        if path == f"/guilds/{G1}/channels":
            return [{"id": CH, "name": "general", "type": 0}]
        if path == f"/guilds/{G1}/threads/active":
            return {"threads": list(self.threads)}
        mt = re.match(r"/channels/(\d+)/messages(?:\?(.*))?$", path)
        if mt:
            qs = urllib.parse.parse_qs(mt[2] or "")
            msgs = sorted(self.history.get(mt[1], []),
                          key=lambda x: int(x["id"]))
            if "after" in qs:
                msgs = [x for x in msgs if int(x["id"]) > int(qs["after"][0])]
                return msgs[:int(qs.get("limit", ["50"])[0])]
            return list(reversed(msgs))[:int(qs.get("limit", ["50"])[0])]
        raise HTTPErr(404)


class NewThreads(Base):
    def setUp(self):
        super().setUp()
        self.m.CHANNELS.pop("703000000000000001", None)
        self.m.CHANNEL_CATEGORY.clear()

    def setup_thread(self, created, starter_mentions=False):
        tid = sf_at(created)
        api = ThreadAPI([{"id": tid, "parent_id": CH, "name": "plans"}])
        starter = gmsg(U2, "new post", channel=tid, mid=tid,
                       mention=starter_mentions)
        mention = gmsg(U1, "@muse look here", mention=True, channel=tid)
        api.history[tid] = [starter, mention]
        self.m.api_get = api.get
        return tid, starter, mention

    def refresh(self):
        with quiet() as buf:
            for _ in self.m.thread_refresh_iter(self.st):
                pass
        return buf.getvalue()

    def drain(self, *msgs):
        inbox = queue.Queue()
        for msg in msgs:
            inbox.put(msg)
        with quiet():
            return self.m.drain_gateway(self.st, inbox, self.m.CHANNELS)[0]

    def poll(self, tid):
        with quiet():
            return self.m.check_channel(
                tid, "Lounge #general > plans", self.st, G1)[0]

    def test_refresh_before_drain_still_stages_the_mention(self):
        tid, starter, mention = self.setup_thread(time.time() - 1)
        out = self.refresh()
        self.assertIn(tid, self.m.CHANNELS)
        self.assertEqual(self.st["last_seen"][tid], str(int(tid) - 1))
        self.assertIn("from its start", out)
        found = self.drain(starter, mention)
        self.assertEqual([x[0]["id"] for x in found], [mention["id"]])
        # The poll re-reads both: nothing staged twice.
        self.assertEqual(self.poll(tid), [])
        q, _ = self.flush()
        self.assertEqual([i["message_id"] for i in q], [mention["id"]])

    def test_poll_before_drain_stages_once(self):
        tid, starter, mention = self.setup_thread(time.time() - 1)
        self.refresh()
        self.assertEqual([x[0]["id"] for x in self.poll(tid)],
                         [mention["id"]])
        self.assertEqual(self.drain(starter, mention), [])
        q, _ = self.flush()
        self.assertEqual(len(q), 1)

    def test_forum_post_starter_mention(self):
        tid, starter, mention = self.setup_thread(time.time() - 1,
                                                  starter_mentions=True)
        self.refresh()
        # The post's first message has the thread's own id.
        found = self.poll(tid)
        self.assertEqual([x[0]["id"] for x in found],
                         [starter["id"], mention["id"]])
        self.assertEqual(self.drain(starter, mention), [])
        q, _ = self.flush()
        self.assertEqual(sorted(i["message_id"] for i in q),
                         sorted([starter["id"], mention["id"]]))

    def test_old_thread_keeps_history_as_history(self):
        # Created long before the monitor started: seeded at its newest.
        tid, starter, mention = self.setup_thread(time.time() - 3600)
        self.m.MONITOR_STARTED = time.time() - 60
        self.refresh()
        self.assertEqual(self.st["last_seen"][tid], mention["id"])
        self.assertEqual(self.poll(tid), [])

    def test_thread_older_than_previous_listing(self):
        # Long-running monitor; the guild's threads were last listed 5
        # min ago, this thread is 20 min old (e.g. access granted since):
        # history is history. One created since that listing is new.
        m = self.m
        m.MONITOR_STARTED = time.time() - 7200
        m._THREADS_LISTED[G1] = time.time() - 300
        tid, _, mention = self.setup_thread(time.time() - 1200)
        tid2 = sf_at(time.time() - 200)
        api = ThreadAPI([{"id": tid, "parent_id": CH, "name": "plans"},
                         {"id": tid2, "parent_id": CH, "name": "new"}])
        api.history = m.api_get.__self__.history
        api.history[tid2] = [gmsg(U1, "@muse", mention=True, channel=tid2)]
        m.api_get = api.get
        self.refresh()
        self.assertEqual(self.st["last_seen"][tid], mention["id"])
        self.assertEqual(self.st["last_seen"][tid2], str(int(tid2) - 1))
        # The next listing measures from this one.
        self.assertGreater(m._THREADS_LISTED[G1], time.time() - 5)

    def test_known_watermark_is_kept(self):
        tid, _, _ = self.setup_thread(time.time() - 1)
        self.st["last_seen"][tid] = "123"
        self.refresh()
        self.assertEqual(self.st["last_seen"][tid], "123")
        self.assertNotIn(tid, self.m._NEW_THREADS)



# ---------------------------------------------------------------------------
# 4. Discord system notices are never conversation.
# ---------------------------------------------------------------------------
class SystemNotices(Base):
    def test_pin_in_open_follow_up_window_is_ignored(self):
        m = self.m
        m.open_follow_up(self.st, CH, U1)
        pin = gmsg(U1, "", age=0)
        pin["type"] = 6  # CHANNEL_PINNED_MESSAGE
        started = gmsg(U1, "side chat", age=0)
        started["type"] = 18  # THREAD_CREATED
        self.assertIsNone(self.stage(pin)[0])
        self.assertIsNone(self.stage(started)[0])
        plain = gmsg(U1, "and another thing", age=0)
        self.assertEqual(self.stage(plain)[0], "follow_up")
        reply = gmsg(U2, "@muse yes", mention=True, reply_to_author=BOT)
        reply["type"] = 19
        self.assertEqual(self.stage(reply)[0], "mention")

    def test_owner_dm_pin_is_not_a_trigger(self):
        m = self.m
        m.DM_CHANNELS[DM_CH] = "owner"
        pin = dmsg(OWNER, "")
        pin["type"] = 6
        pin["id"] = sf(600)
        inbox = queue.Queue()
        inbox.put(pin)
        with quiet():
            self.assertEqual(m.drain_gateway(self.st, inbox,
                                             m.CHANNELS)[2], [])
        m.api.history[DM_CH] = [pin]
        self.assertEqual(self.sweep({}, dms=True), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
