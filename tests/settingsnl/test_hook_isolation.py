"""The wake hook (hooks/mention-watch.sh) and settings requests: an
owner's DM is handed out alone, never in the same wake as a server
message or anyone else's DM, so text someone else wrote never shares a
worker's context with an owner's settings request. The other items go
out on the next run. Runs the hook with bash and jq against the stubbed
hatch hook runtime (_lib/hook_runtime.sh).

Usage: [MUT_DIR=<tree>] python3 tests/settingsnl/test_hook_isolation.py"""
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


def item(mid, kind, speaker):
    return {"message_id": mid, "channel_id": "9", "kind": kind,
            "content": "x", "settings": {"where": "dm" if kind == "dm"
                                         else "server",
                                         "speaker": speaker}}


@unittest.skipUnless(shutil.which("jq"), "jq not installed")
class OwnerDmWakesAlone(unittest.TestCase):
    def setUp(self):
        self.d = scratch("muse-hook-nl-")
        self.pkg = os.path.join(self.d, "pkg")
        os.makedirs(self.pkg)
        self.env = dict(os.environ, HATCH_HOOK_RUNTIME=HOOK_RUNTIME,
                        HOOK_STATE=os.path.join(self.d, "hookstate.json"),
                        MUSE_DISCORD_DIR=self.pkg,
                        MUSE_DISCORD_CONFIG=os.path.join(self.pkg,
                                                         "config.yaml"))

    def queue(self, items, nonces=True):
        """Queue items; the monitor mints each owner DM's nonce first
        (an owner DM without one is not handed out yet)."""
        if nonces:
            with open(os.path.join(self.pkg, "owner_dm_nonces.json"),
                      "w") as f:
                json.dump({i["message_id"]: "%032x" % int(i["message_id"])
                           for i in items if i.get("kind") == "dm"
                           and (i.get("settings") or {}).get("speaker")
                           == "owner"}, f)
        with open(os.path.join(self.pkg, "reply_queue.json"), "w") as f:
            json.dump(items, f)

    def wake(self):
        r = subprocess.run(["bash", HOOK], env=self.env, capture_output=True,
                           text=True, timeout=30)
        lines = r.stdout.splitlines()
        if not lines or not lines[0].startswith("WAKE:"):
            self.payload = None
            return None, r.stderr
        self.payload = json.loads(lines[1])
        return [i["message_id"] for i in self.payload["items"]], r.stderr

    def test_nonce_only_in_an_owner_dm_only_wake(self):
        with open(os.path.join(self.pkg, "owner_dm_nonces.json"), "w") as f:
            json.dump({"2": "a" * 32, "5": "b" * 32}, f)
        self.queue([item("1", "mention", "member"),
                    item("2", "dm", "owner"),
                    item("5", "dm", "owner")], nonces=False)
        ids, _ = self.wake()
        self.assertEqual(ids, ["2", "5"])
        self.assertEqual([i.get("owner_dm_nonce")
                          for i in self.payload["items"]],
                         ["a" * 32, "b" * 32])
        ids, _ = self.wake()
        self.assertEqual(ids, ["1"])
        self.assertNotIn("owner_dm_nonce", self.payload["items"][0])
        self.assertNotIn("a" * 32, json.dumps(self.payload))
        # A nonce file naming a server item never reaches its wake.
        with open(os.path.join(self.pkg, "owner_dm_nonces.json"), "w") as f:
            json.dump({"7": "c" * 32}, f)
        self.queue([item("7", "mention", "owner")], nonces=False)
        ids, _ = self.wake()
        self.assertEqual(ids, ["7"])
        self.assertNotIn("owner_dm_nonce", self.payload["items"][0])

    def test_owner_dm_first_and_alone(self):
        self.queue([item("1", "mention", "member"),
                    item("2", "dm", "owner"),
                    item("3", "dm", "member"),
                    item("4", "mention", "owner"),
                    item("5", "dm", "owner")])
        ids, err = self.wake()
        self.assertEqual(ids, ["2", "5"])
        self.assertIn("owner DMs wake alone", err)
        ids, _ = self.wake()
        self.assertEqual(ids, ["1", "3", "4"])
        ids, _ = self.wake()
        self.assertIsNone(ids)   # every item is leased now

    def test_no_owner_dm_or_only_owner_dms(self):
        self.queue([item("1", "mention", "member"),
                    item("2", "mention", "owner")])
        self.assertEqual(self.wake()[0], ["1", "2"])
        self.queue([item("1", "mention", "member"),
                    item("2", "mention", "owner"),
                    item("7", "dm", "owner"), item("8", "dm", "owner")])
        ids, err = self.wake()
        self.assertEqual(ids, ["7", "8"])
        # A new server item while the owner DMs are leased: it wakes.
        self.queue([item("7", "dm", "owner"), item("9", "mention",
                                                   "member")])
        self.assertEqual(self.wake()[0], ["9"])

    def test_items_without_a_card(self):
        # An older monitor's items (no settings card) are never treated
        # as owner DMs.
        self.queue([{"message_id": "1", "channel_id": "9", "kind": "dm"},
                    item("2", "mention", "member")])
        self.assertEqual(self.wake()[0], ["1", "2"])


if __name__ == "__main__":
    unittest.main()
