"""Reminders and watches with replies bound to their channel and owner
commands bound to their run: a schedule action rides on a reply to a
queued message, in its channel, so one on a made-up item is refused; a
fire's answer goes out in the fire's channel; an owner's server message
that may set web watches holds both an owner_command_nonce and a
web_nonce, and the wake hook hands both to one wake that holds nothing
but the owner's items.

Usage: [MUT_DIR=<tree>] python3 tests/watches/test_bound.py"""
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_watches as tw  # noqa: E402
from common import HOOK_RUNTIME, scratch  # noqa: E402
from settings_harness import CH, OWNER, U1, sf  # noqa: E402

ADD = [{"op": "add", "kind": "remind", "after": 120}]


class Bound(tw.Base):
    def test_schedule_on_a_made_up_item_is_refused(self):
        self.outbox({"item": sf(), "channel_id": CH, "text": "Sure.",
                     "schedule": {"v": 1, "changes": ADD}})
        self.assertTrue(self.was_rejected())
        self.assertEqual(self.items(), {})

    def test_fire_answer_goes_out_in_its_channel(self):
        self.remind(after=60)
        self.tick(ahead=61)
        (f,) = self.fires()
        self.outbox({"item": f["message_id"], "channel_id": CH,
                     "text": "Reminder!"})
        self.assertEqual(len(self.new_sends()), 1, self.out)

    def test_owner_server_message_holds_both_nonces(self):
        own, _ = self.ask(OWNER, "<@bot> pin that, and watch the page")
        other, _ = self.ask(U1, "<@bot> hi")
        with open(self.mod.OWNER_ITEMS_FILE) as f:
            cmds = json.load(f)
        with open(self.mod.WEB_NONCES_FILE) as f:
            web = json.load(f)
        self.assertEqual(list(cmds), [own["id"]])
        self.assertIn(own["id"], web)
        self.assertNotEqual(cmds[own["id"]], web[own["id"]])
        if not shutil.which("jq"):
            return
        d = scratch("muse-watch-bound-")
        env = dict(os.environ, HATCH_HOOK_RUNTIME=HOOK_RUNTIME,
                   HOOK_STATE=os.path.join(d, "hookstate.json"),
                   MUSE_DISCORD_DIR=self.mod.tmp,
                   MUSE_DISCORD_CONFIG=self.mod.cfg_path)

        def wake():
            r = subprocess.run(["bash", tw.HOOK], env=env, text=True,
                               capture_output=True, timeout=30)
            lines = r.stdout.splitlines()
            return r.stdout, (json.loads(lines[1])["items"] if lines
                              and lines[0].startswith("WAKE:") else None)
        out, items = wake()
        self.assertEqual([(i["message_id"], i.get("owner_command_nonce"),
                           i.get("web_nonce")) for i in items],
                         [(own["id"], cmds[own["id"]], web[own["id"]])])
        out, items = wake()
        self.assertEqual([i["message_id"] for i in items], [other["id"]])
        for n in (cmds[own["id"]], web[own["id"]]):
            self.assertNotIn(n, out)


if __name__ == "__main__":
    unittest.main()
