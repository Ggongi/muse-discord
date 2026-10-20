"""The wake hook (hooks/mention-watch.sh) and owner-DM nonces: an owner
DM's owner_dm_nonce reaches only a wake that holds nothing but owner
DMs, and an outbox entry naming an owner DM counts as answering it only
when it carries that DM's nonce, so forged entries from another wake
(which the monitor refuses) never keep the DM from its own wake. Runs
the hook with bash and jq against the stubbed hatch hook runtime.

Usage: [MUT_DIR=<tree>] python3 tests/ownerdm/test_hook.py"""
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
from common import HOOK_RUNTIME, MUT_DIR, scratch  # noqa: E402

HOOK = os.path.join(MUT_DIR, "hooks", "mention-watch.sh")
N1, N2 = "a" * 32, "b" * 32


def item(mid, kind="dm", speaker="owner", frags=()):
    return {"message_id": mid, "channel_id": "9", "kind": kind,
            "content": "x",
            "burst_fragments": [{"message_id": f} for f in frags],
            "settings": {"where": "dm" if kind == "dm" else "server",
                         "speaker": speaker}}


@unittest.skipUnless(shutil.which("jq"), "jq not installed")
class ForgedAnswersDoNotHideOwnerDms(unittest.TestCase):
    def setUp(self):
        self.d = scratch("muse-hook-odm-")
        self.pkg = os.path.join(self.d, "pkg")
        os.makedirs(self.pkg)
        self.env = dict(os.environ, HATCH_HOOK_RUNTIME=HOOK_RUNTIME,
                        HOOK_STATE=os.path.join(self.d, "hookstate.json"),
                        MUSE_DISCORD_DIR=self.pkg,
                        MUSE_DISCORD_CONFIG=os.path.join(self.pkg,
                                                         "config.yaml"))
        self.write("owner_dm_nonces.json", {"1": N1, "2": N2})

    def write(self, name, data):
        with open(os.path.join(self.pkg, name), "w") as f:
            json.dump(data, f)

    def wake(self):
        os.path.exists(self.env["HOOK_STATE"]) \
            and os.remove(self.env["HOOK_STATE"])
        r = subprocess.run(["bash", HOOK], env=self.env, capture_output=True,
                           text=True, timeout=30)
        lines = r.stdout.splitlines()
        if not lines or not lines[0].startswith("WAKE:"):
            return None
        return json.loads(lines[1])

    def ids(self):
        p = self.wake()
        return None if p is None else [i["message_id"] for i in p["items"]]

    def test_forged_entries_do_not_count(self):
        self.write("reply_queue.json", [item("1", frags=["11"]), item("2")])
        for forged in ([{"item": "1", "text": "x"}],
                       [{"reply_to": "11", "text": "x"}],
                       [{"item": "1", "silent": True,
                         "owner_dm_nonce": N2}],      # another DM's
                       [{"item": "1", "settings": {"nonce": "0" * 32}}],
                       [{"item": "1", "owner_dm_nonce": 5}]):
            for name in ("reply_outbox.json",
                         "reply_outbox.processing.json"):
                self.write(name, forged)
                self.assertEqual(self.ids(), ["1", "2"], forged)
                os.remove(os.path.join(self.pkg, name))

    def test_real_answers_count(self):
        self.write("reply_queue.json", [item("1", frags=["11"]), item("2")])
        self.write("reply_outbox.json", [
            {"item": "1", "reply_to": "11", "owner_dm_nonce": N1},
            {"item": "2", "settings": {"nonce": N2}}])
        self.assertIsNone(self.ids())
        self.write("reply_outbox.json", [])
        self.write("reply_outbox.processing.json", [
            {"reply_to": "11", "owner_dm_nonce": N1}])
        self.assertEqual(self.ids(), ["2"])

    def test_other_items_unchanged(self):
        # Not owner DMs: any outbox entry naming them answers them, as
        # before; a nonce never reaches their wake.
        self.write("owner_dm_nonces.json", {"1": N1, "7": "c" * 32})
        self.write("reply_queue.json", [item("5", "mention", "member"),
                                        item("6", "dm", "member"),
                                        item("7", "mention", "owner")])
        self.write("reply_outbox.json", [{"item": "5", "text": "x"}])
        p = self.wake()
        self.assertEqual([i["message_id"] for i in p["items"]], ["6", "7"])
        self.assertNotIn("owner_dm_nonce", json.dumps(p))
        self.assertNotIn("c" * 32, json.dumps(p))

    def test_mixed_queue_nonce_only_in_owner_wake(self):
        self.write("reply_queue.json", [item("5", "mention", "member"),
                                        item("1")])
        os.path.exists(self.env["HOOK_STATE"]) \
            and os.remove(self.env["HOOK_STATE"])
        run = subprocess.run(["bash", HOOK], env=self.env,
                             capture_output=True, text=True, timeout=30)
        first = json.loads(run.stdout.splitlines()[1])
        self.assertEqual([(i["message_id"], i.get("owner_dm_nonce"))
                          for i in first["items"]], [("1", N1)])
        run = subprocess.run(["bash", HOOK], env=self.env,
                             capture_output=True, text=True, timeout=30)
        second = json.loads(run.stdout.splitlines()[1])
        self.assertEqual([i["message_id"] for i in second["items"]], ["5"])
        self.assertNotIn(N1, run.stdout)
        self.assertNotIn(N2, run.stdout)

    def test_unreadable_nonce_file(self):
        with open(os.path.join(self.pkg, "owner_dm_nonces.json"), "w") as f:
            f.write("{not json")
        self.write("reply_queue.json", [item("1")])
        self.write("reply_outbox.json", [{"item": "1", "text": "x"}])
        # No nonces known: the entry counts, as before (and the monitor
        # refuses it, then the DM is open again).
        self.assertIsNone(self.ids())
        os.remove(os.path.join(self.pkg, "reply_outbox.json"))
        p = self.wake()
        self.assertNotIn("owner_dm_nonce", p["items"][0])


if __name__ == "__main__":
    unittest.main()
