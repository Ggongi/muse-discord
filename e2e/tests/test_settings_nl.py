"""Offline tests for the e2e pieces of settings requests: the fake
worker's [[settings:...]] directive, the overlay_lacks check (and its
scenario argument), sut_files.overlay, and the shipped scenarios.

Run from the repo root: python3 -m unittest discover -s e2e/tests -t .
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

E2E = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, E2E)

import checks  # noqa: E402
import fake_worker  # noqa: E402
import scenario  # noqa: E402
import sut  # noqa: E402

REPO = os.path.dirname(E2E)
GID, CID = "500000000000000001", "701000000000000001"


def item(content, mid="301", gid=GID, frags=()):
    return {"message_id": mid, "channel_id": CID, "guild_id": gid,
            "kind": "mention", "author": "a", "content": content,
            "burst_fragments": [{"message_id": i, "content": c}
                                for i, c in frags]}


class Directive(unittest.TestCase):
    def test_parse(self):
        cases = [
            ("set who.replies=off @here",
             {"op": "set", "place": f"channel:{GID}:{CID}",
              "key": "who.replies", "value": "off"}),
            ("reset style.length @server",
             {"op": "reset", "place": f"server:{GID}",
              "key": "style.length"}),
            ("add who.ignore=1,role:2 @server",
             {"op": "add", "place": f"server:{GID}", "key": "who.ignore",
              "value": ["1", "role:2"]}),
            ("add style.notes=be-nice @here",
             {"op": "add", "place": f"channel:{GID}:{CID}",
              "key": "style.notes", "value": "be-nice"}),
            ("undo", {"op": "undo"}),
            ("undo @dms", {"op": "undo", "place": "dms"}),
            ("preset work @server",
             {"op": "preset", "place": f"server:{GID}", "name": "work"}),
            ("pause @defaults", {"op": "pause", "place": "defaults"}),
            (f"set who.replies=off @channel:{GID}:9",
             {"op": "set", "place": f"channel:{GID}:9",
              "key": "who.replies", "value": "off"}),
        ]
        for text, want in cases:
            self.assertEqual(fake_worker.settings_change(text, item("")),
                             want, text)
        # In a DM, "here" and "server" mean the DMs.
        self.assertEqual(fake_worker.settings_change(
            "pause @here", item("", gid=None))["place"], "dms")
        self.assertIsNone(fake_worker.settings_change("  ", item("")))

    def test_reply_carries_the_action(self):
        it = item("<@1> [[settings:set who.replies=off @here]] hush",
                  frags=[("302", "[[settings:reset style.length @server]]"),
                         ("303", "[[settings:set who.replies=off @here]]")])
        self.assertIn("settings", fake_worker.directives(it))
        out, _ = fake_worker.build_reply(it)
        self.assertEqual(out["text"], "ack 3")
        self.assertEqual(out["settings"], {"v": 1,
                                           "nonce": fake_worker.NO_NONCE,
                                           "changes": [
            {"op": "set", "place": f"channel:{GID}:{CID}",
             "key": "who.replies", "value": "off"},
            {"op": "reset", "place": f"server:{GID}",
             "key": "style.length"}]})
        out, _ = fake_worker.build_reply(
            item("[[settings:undo]] [[silent]]"))
        self.assertTrue(out["silent"])
        self.assertNotIn("settings", out)
        out, _ = fake_worker.build_reply(item("plain"))
        self.assertNotIn("settings", out)
        # A payload nonce (owner-DM-only wakes) is echoed, on the item
        # and in the settings action.
        it = item("[[settings:undo]]")
        it["owner_dm_nonce"] = "ab" * 16
        out, _ = fake_worker.build_reply(it)
        self.assertEqual(out["settings"]["nonce"], "ab" * 16)
        self.assertEqual(out["owner_dm_nonce"], "ab" * 16)
        it = item("plain")
        it["owner_dm_nonce"] = "cd" * 16
        out, _ = fake_worker.build_reply(it)
        self.assertEqual(out["owner_dm_nonce"], "cd" * 16)
        self.assertNotIn("owner_dm_nonce", fake_worker.build_reply(
            item("plain"))[0])

    def test_outbox_append_accepts_it(self):
        pkg = tempfile.mkdtemp(prefix="e2e-nl-")
        self.addCleanup(shutil.rmtree, pkg, True)
        os.makedirs(os.path.join(pkg, "bin"))
        for f in ("muse_settings.py", "settings_nl.py"):
            shutil.copy(os.path.join(REPO, f), pkg)
        shutil.copy(os.path.join(REPO, "bin", "outbox-append.py"),
                    os.path.join(pkg, "bin"))
        shutil.copytree(os.path.join(REPO, "plugins"),
                        os.path.join(pkg, "plugins"))
        out, _ = fake_worker.build_reply(
            item("[[settings:set who.replies=off @here]]"))
        self.assertTrue(fake_worker.append_outbox(pkg, [out]))
        with open(os.path.join(pkg, "reply_outbox.json")) as f:
            self.assertEqual(json.load(f), [out])
        bad, _ = fake_worker.build_reply(
            item("[[settings:set who.replies=loud @here]]", mid="9"))
        r = subprocess.run([sys.executable,
                            os.path.join(pkg, "bin", "outbox-append.py")],
                           input=json.dumps([bad]), capture_output=True,
                           text=True, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("valid: off, when-addressed, conversational",
                      r.stderr)


def tr(overlay):
    return {"sut_files": {"overlay": overlay}}


class OverlayLacks(unittest.TestCase):
    def run_check(self, arg, transcript):
        return checks.evaluate({"expect": {"checks": [
            {"overlay_lacks": arg}]}}, transcript)[0]

    def test_missing_or_empty_passes(self):
        self.assertTrue(self.run_check("who.replies", tr(None))["ok"])
        self.assertTrue(self.run_check("who.replies", {})["ok"])
        r = self.run_check("who.replies", tr({"values": {}, "history": []}))
        self.assertTrue(r["ok"])
        self.assertIn("0 entries", r["detail"])

    def test_finds_the_key_at_any_place(self):
        for key in (f'servers."{GID}".who.replies', "dms.who.replies",
                    f'servers."{GID}".channels."{CID}".replies'):
            r = self.run_check("who.replies", tr(
                {"values": {key: {"value": "off", "by": "1"}}}))
            self.assertFalse(r["ok"], key)
            self.assertIn("'off'", r["detail"])
        r = self.run_check("style.notes", tr({"values": {
            f'servers."{GID}".channels."{CID}".notes': {"value": "x"}}}))
        self.assertFalse(r["ok"])

    def test_other_keys_pass(self):
        r = self.run_check("who.replies", tr({"values": {
            "dms.who.dms_from": {"value": "anyone"},
            "everywhere.style.length": {"value": "brief"}}}))
        self.assertTrue(r["ok"], r)
        self.assertIn("2 entries", r["detail"])
        # Suffixes match whole key parts only.
        r = self.run_check("replies", tr({"values": {
            "everywhere.who.no_replies": {"value": 1}}}))
        self.assertTrue(r["ok"])

    def test_bad_overlay(self):
        self.assertFalse(self.run_check("who.replies", tr(["x"]))["ok"])
        self.assertFalse(self.run_check("who.replies",
                                        tr({"values": ["x"]}))["ok"])

    def test_scenario_argument(self):
        base = {"id": "s", "tier": 1, "title": "t",
                "steps": [{"send": {"as": "a", "text": "<@SUT> hi",
                                    "label": "q1"}}],
                "expect": {"checks": [{"overlay_lacks": "who.replies"}]}}
        scenario.validate(base, "s.yaml")
        for bad in ("", "Who Replies", 3, None, ["who.replies"]):
            sc = json.loads(json.dumps(base))
            sc["expect"]["checks"] = [{"overlay_lacks": bad}]
            with self.assertRaises(scenario.ScenarioError, msg=bad):
                scenario.validate(sc, "s.yaml")


class SutOverlay(unittest.TestCase):
    def test_read_yaml(self):
        d = tempfile.mkdtemp(prefix="e2e-nl-sut-")
        self.addCleanup(shutil.rmtree, d, True)
        s = sut.SUT.__new__(sut.SUT)
        s.pkg_dir = d
        self.assertIsNone(s.read_yaml(sut.OVERLAY_NAME))
        with open(os.path.join(d, sut.OVERLAY_NAME), "w") as f:
            f.write("version: 1\nvalues:\n  dms.who.replies:\n"
                    "    value: 'off'\n")
        self.assertEqual(s.read_yaml(sut.OVERLAY_NAME)["values"],
                         {"dms.who.replies": {"value": "off"}})
        with open(os.path.join(d, sut.OVERLAY_NAME), "w") as f:
            f.write("values: [unclosed\n")
        self.assertIsNone(s.read_yaml(sut.OVERLAY_NAME))


class Scenarios(unittest.TestCase):
    def test_shipped(self):
        t1 = {s["id"] for s in scenario.load_all(scenario.SCENARIOS_DIR, 1)}
        t2 = {s["id"] for s in scenario.load_all(scenario.SCENARIOS_DIR, 2)}
        self.assertLessEqual({"settings_action_refused_in_server",
                              "settings_action_refused_non_owner"}, t1)
        self.assertLessEqual({"settings_owner_asks_in_server",
                              "settings_injection_in_server"}, t2)

    def test_directives_in_the_tier1_scenarios_pass_the_shape_check(self):
        sys.path.insert(0, REPO)
        import settings_nl
        for sc in scenario.load_all(scenario.SCENARIOS_DIR, 1):
            for step in sc["steps"]:
                text = (step.get("send") or {}).get("text") or ""
                if "[[settings:" not in text:
                    continue
                out, _ = fake_worker.build_reply(item(text))
                self.assertIn("settings", out, sc["id"])
                self.assertEqual(settings_nl.shape_problems(
                    out, os.path.join(REPO, "plugins")), [], sc["id"])


if __name__ == "__main__":
    unittest.main()
