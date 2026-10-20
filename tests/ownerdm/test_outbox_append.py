"""bin/outbox-append.py and owner_dm_nonce: passed through as is on any
item; a nonce that is not a copied payload value (wrong type or shape),
or one that disagrees with the settings action's, appends nothing and
says why. The monitor holds the real nonce; this only catches typos.

Usage: [MUT_DIR=<tree>] python3 tests/ownerdm/test_outbox_append.py"""
import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
from common import MUT_DIR, scratch  # noqa: E402

N = "ab" * 16
CHANGE = {"op": "set", "place": "defaults", "key": "who.replies",
          "value": "off"}


def pkg_copy():
    d = scratch("muse-odm-append-")
    os.makedirs(os.path.join(d, "bin"))
    shutil.copy(os.path.join(MUT_DIR, "bin", "outbox-append.py"),
                os.path.join(d, "bin"))
    for f in ("muse_settings.py", "settings_nl.py"):
        shutil.copy(os.path.join(MUT_DIR, f), d)
    shutil.copytree(os.path.join(MUT_DIR, "plugins"),
                    os.path.join(d, "plugins"))
    return d


class OwnerDmNonce(unittest.TestCase):
    def setUp(self):
        self.pkg = pkg_copy()

    def append(self, items):
        r = subprocess.run([sys.executable, os.path.join(
            self.pkg, "bin", "outbox-append.py")], input=json.dumps(items),
            capture_output=True, text=True, timeout=30)
        return r.returncode, r.stderr

    def outbox(self):
        try:
            with open(os.path.join(self.pkg, "reply_outbox.json")) as f:
                return json.load(f)
        except OSError:
            return []

    def test_passed_through(self):
        items = [{"item": "1", "channel_id": "2", "reply_to": "1",
                  "text": "hi", "owner_dm_nonce": N},
                 {"item": "3", "channel_id": "2", "reply_to": "3",
                  "silent": True, "owner_dm_nonce": N},
                 {"item": "4", "channel_id": "2", "reply_to": "4",
                  "text": "Sure.", "owner_dm_nonce": N,
                  "settings": {"v": 1, "nonce": N, "changes": [CHANGE]}}]
        rc, err = self.append(items)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.outbox(), items)

    def test_bad_nonces_append_nothing(self):
        for bad, why in ((12345, "copy the item's"),
                         ("not-a-nonce", "copy the item's"),
                         (N.upper(), "copy the item's"),
                         (N, "differ")):
            it = {"item": "1", "channel_id": "2", "reply_to": "1",
                  "text": "Sure.", "owner_dm_nonce": bad}
            if why == "differ":
                it["settings"] = {"v": 1, "nonce": "cd" * 16,
                                  "changes": [CHANGE]}
            rc, err = self.append([it])
            self.assertEqual(rc, 1, bad)
            self.assertIn(why, err)
            self.assertEqual(self.outbox(), [])


if __name__ == "__main__":
    unittest.main()
