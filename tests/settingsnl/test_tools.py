"""The tools around settings requests, offline: bin/outbox-append.py
refuses a malformed `settings` action with a message the worker can act
on (and appends nothing), and bin/settings.py lists places from
directory.json (`places`) and names channels in its sentences.

Usage: [MUT_DIR=<tree>] python3 tests/settingsnl/test_tools.py"""
import json
import os
import shutil
import subprocess
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
from common import MUT_DIR, scratch  # noqa: E402

G1, CH, CH2, CAT = ("500000000000000001", "701000000000000001",
                    "702000000000000001", "800000000000000001")
G2, CH_G2 = "600000000000000001", "704000000000000001"
DIRECTORY = {
    "updated": 0,
    "guilds": [{"id": G1, "name": "Work", "member_count": 120},
               {"id": G2, "name": "Friends", "member_count": None}],
    "channels": {
        G1: [{"id": CAT, "name": "Staff", "type": 4, "parent_id": None},
             {"id": CH, "name": "general", "type": 0, "parent_id": CAT},
             {"id": CH2, "name": "random", "type": 0, "parent_id": None},
             {"id": "705000000000000001", "name": "Lobby", "type": 2,
              "parent_id": None}],
        G2: [{"id": CH_G2, "name": "general", "type": 0,
              "parent_id": None}]},
}


def pkg_copy(with_nl=True):
    d = scratch("muse-nl-tool-")
    os.makedirs(os.path.join(d, "bin"))
    shutil.copy(os.path.join(MUT_DIR, "bin", "outbox-append.py"),
                os.path.join(d, "bin"))
    shutil.copy(os.path.join(MUT_DIR, "muse_settings.py"), d)
    if with_nl:
        shutil.copy(os.path.join(MUT_DIR, "settings_nl.py"), d)
    shutil.copytree(os.path.join(MUT_DIR, "plugins"),
                    os.path.join(d, "plugins"))
    return d


def append(pkg, items):
    r = subprocess.run([sys.executable,
                        os.path.join(pkg, "bin", "outbox-append.py")],
                       input=json.dumps(items), capture_output=True,
                       text=True, timeout=30)
    return r.returncode, r.stdout, r.stderr


def outbox(pkg):
    try:
        with open(os.path.join(pkg, "reply_outbox.json")) as f:
            return json.load(f)
    except OSError:
        return []


def settings_item(changes, **kw):
    it = {"item": "123", "channel_id": "456", "reply_to": "123",
          "text": "Sure.", "settings": {"v": 1, "changes": changes}}
    it.update(kw)
    return it


class OutboxAppend(unittest.TestCase):
    def setUp(self):
        self.pkg = pkg_copy()

    def test_good_item_is_appended(self):
        it = settings_item([{"op": "set", "place": f"channel:{G1}:{CH}",
                             "key": "who.replies",
                             "value": "when-addressed"}])
        rc, out, err = append(self.pkg, [it])
        self.assertEqual(rc, 0, err)
        self.assertEqual(outbox(self.pkg), [it])

    def test_shape_errors_append_nothing_and_say_why(self):
        cases = [
            ([{"op": "set", "place": "here", "key": "who.replies",
               "value": "off"}], {}, '"channel:<server id>:<channel id>"'),
            ([{"op": "set", "place": "dms", "key": "who.replies",
               "value": "quiet"}], {},
             "valid: off, when-addressed, conversational"),
            ([{"op": "set", "place": "dms", "key": "replies",
               "value": "off"}], {}, "is not a setting"),
            ([{"op": "add", "place": "dms", "key": "who.ignore",
               "value": ["@sam"]}], {}, '"role:<role id>"'),
            ([{"op": "undo"}, {"op": "pause", "place": "dms"}], {},
             "goes alone"),
            ([{"op": "set", "place": "dms", "key": "who.replies",
               "value": "off"}], {"reactions": ["👍"]},
             "carries no reactions"),
            ([{"op": "set", "place": "dms", "key": "who.replies",
               "value": "off"}], {"text": "x" * 400}, "300 characters"),
        ]
        for changes, extra, needle in cases:
            rc, out, err = append(self.pkg, [settings_item(changes,
                                                           **extra)])
            self.assertEqual(rc, 1, (changes, extra))
            self.assertIn("nothing appended", err)
            self.assertIn(needle, err, (changes, extra))
        self.assertEqual(outbox(self.pkg), [])

    def test_monitor_fields_refused_on_any_item(self):
        rc, _out, err = append(self.pkg, [{"item": "1", "channel_id": "2",
                                           "text": "hi", "_nl": {}}])
        self.assertEqual(rc, 1)
        self.assertIn("_nl", err)

    def test_one_bad_item_blocks_the_batch(self):
        good = {"item": "1", "channel_id": "2", "text": "hi"}
        bad = settings_item([{"op": "nope"}])
        rc, _out, err = append(self.pkg, [good, bad])
        self.assertEqual(rc, 1)
        self.assertIn("item 2:", err)
        self.assertEqual(outbox(self.pkg), [])

    def test_without_settings_nl_the_monitor_checks(self):
        pkg = pkg_copy(with_nl=False)
        rc, _out, err = append(pkg, [settings_item([{"op": "nope"}])])
        self.assertEqual(rc, 0)
        self.assertIn("the monitor will check it", err)


class Places(unittest.TestCase):
    def setUp(self):
        self.d = scratch("muse-nl-places-")
        self.cfg = os.path.join(self.d, "config.yaml")
        with open(self.cfg, "w") as f:
            f.write("version: 1\nowners: [\"111000000000000001\"]\n"
                    "token_from: env:MUSE_TEST_TOKEN\n")

    def directory(self, age=0):
        with open(os.path.join(self.d, "directory.json"), "w") as f:
            json.dump(dict(DIRECTORY, updated=int(time.time() - age)), f)

    def cli(self, *args):
        r = subprocess.run([sys.executable,
                            os.path.join(MUT_DIR, "bin", "settings.py"),
                            "--config", self.cfg] + list(args),
                           capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout, r.stderr

    def test_all(self):
        self.directory()
        rc, out, err = self.cli("places")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.splitlines(), [
            "defaults", "dms",
            f"server:{G1}  Work (120 members)",
            f"  channel:{G1}:{CH}  #general",
            f"  channel:{G1}:{CH2}  #random",
            f"server:{G2}  Friends (size unknown)",
            f"  channel:{G2}:{CH_G2}  #general"])

    def test_find_and_server(self):
        self.directory()
        rc, out, _ = self.cli("places", "--find", "GENERAL")
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines(), [
            f"server:{G1}  Work (120 members)",
            f"  channel:{G1}:{CH}  #general",
            f"server:{G2}  Friends (size unknown)",
            f"  channel:{G2}:{CH_G2}  #general"])
        rc, out, _ = self.cli("places", "--find", "work")
        self.assertIn(f"  channel:{G1}:{CH2}  #random", out)
        rc, out, _ = self.cli("places", "--server", G2)
        self.assertEqual(out.splitlines()[0], f"server:{G2}  Friends "
                                              f"(size unknown)")
        rc, out, _ = self.cli("places", "--find", "nowhere")
        self.assertEqual(rc, 1)
        self.assertIn("Nothing matches", out)

    def test_stale_and_missing(self):
        rc, _out, err = self.cli("places")
        self.assertEqual(rc, 1)
        self.assertIn("Start the monitor", err)
        self.directory(age=3 * 3600)
        rc, out, _ = self.cli("places")
        self.assertEqual(rc, 0)
        self.assertIn("3 hour(s) old; start the monitor", out)

    def test_sentences_name_channels(self):
        self.directory()
        rc, out, err = self.cli("set", "who.replies", "off", "--place",
                                f"channel:{G1}:{CH}")
        self.assertEqual(rc, 0, err)
        self.assertIn("In #general, When it answers: Chatty → Off", out)


if __name__ == "__main__":
    unittest.main()
