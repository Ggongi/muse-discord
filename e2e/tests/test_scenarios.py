"""Offline tests for e2e/scenario.py and the shipped scenario files.

Run from the repo root: python3 -m unittest discover -s e2e/tests -t .
"""
import ast
import copy
import os
import re
import struct
import sys
import tempfile
import unittest

E2E = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, E2E)

import checks  # noqa: E402
import scenario  # noqa: E402

REPO = os.path.dirname(E2E)
IDS = {"SUT": "111", "A": "222", "B": "333", "SUT_NAME": "muse-test"}


def base():
    """A minimal valid tier-1 scenario."""
    return {
        "id": "sample",
        "tier": 1,
        "title": "Sample",
        "steps": [
            {"send": {"as": "a", "text": "<@SUT> hi", "label": "q1"}},
            {"wait_for": {"sut_messages": 1, "timeout": 30}},
        ],
        "expect": {"settle": 5, "checks": [{"sut_reply_count": {"eq": 1}},
                                           {"replies_to": "q1"}]},
    }


def injection_patterns():
    """monitor.py's INJECTION_PATTERNS, read without importing monitor
    (its import reads config and the token)."""
    with open(os.path.join(REPO, "monitor.py")) as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", None) == "INJECTION_PATTERNS"
                for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("INJECTION_PATTERNS not found in monitor.py")


def texts(sc):
    """Every text a scenario sends, rendered, per step."""
    out = []
    for step in sc["steps"]:
        if "send" in step and step["send"].get("text"):
            out.append(scenario.render(step["send"]["text"], IDS))
        if "converse" in step:
            out += [scenario.render(x, IDS) for x in step["converse"]["lines"]]
    return out


class RealScenarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.all = scenario.load_all(scenario.SCENARIOS_DIR)

    def test_all_validate_and_ids_match_files(self):
        files = sorted(f[:-5] for f in os.listdir(scenario.SCENARIOS_DIR)
                       if f.endswith(".yaml"))
        self.assertEqual(sorted(s["id"] for s in self.all), files)

    def test_expected_scenarios_present(self):
        t1 = {s["id"] for s in scenario.load_all(scenario.SCENARIOS_DIR, 1)}
        t2 = {s["id"] for s in scenario.load_all(scenario.SCENARIOS_DIR, 2)}
        self.assertLessEqual({
            "mention_basic", "reply_to_sut", "bare_name_ignored",
            "unaddressed_chatter", "burst_coalesce", "injection_quarantined",
            "bot_direct", "silent_item", "long_reply_capped", "no_mass_ping",
            "restart_mid_send", "gateway_down_poll",
            "two_mentions_two_replies"}, t1)
        self.assertLessEqual({
            "persona_voice", "bot_conversation_ends", "burst_coherent",
            "stranger_owner_command", "owner_request", "image_seen",
            "not_addressed_stays_quiet"}, t2)
        self.assertFalse(t1 & t2)

    def test_tier_filter_accepts_string(self):
        self.assertEqual(
            [s["id"] for s in scenario.load_all(scenario.SCENARIOS_DIR, "2")],
            [s["id"] for s in self.all if s["tier"] == 2])

    def test_check_names_agree_with_checks_module(self):
        self.assertEqual(set(scenario.CHECK_ARGS), set(checks.CHECKS))

    def test_rendering_leaves_no_placeholders(self):
        for sc in self.all:
            for t in texts(sc):
                self.assertNotRegex(t, r"<@!?[A-Za-z_]+>", sc["id"])
                self.assertNotIn("{SUT_NAME}", t, sc["id"])

    def test_injection_text_only_where_quarantine_is_expected(self):
        """A send text that trips the monitor's injection filter would be
        quarantined silently, turning a scenario about something else
        into a false failure; and a quarantine scenario whose text no
        longer matches tests nothing."""
        pats = injection_patterns()
        for sc in self.all:
            hits = [(t, p) for t in texts(sc) for p in pats
                    if re.search(p, t.lower())]
            expects_q = any("quarantined" in c for c in sc["expect"]["checks"])
            if expects_q:
                self.assertTrue(hits, f"{sc['id']}: no text matches any "
                                      f"INJECTION_PATTERNS entry")
            else:
                self.assertFalse(hits, f"{sc['id']}: would be quarantined: "
                                       f"{hits}")

    def test_quarantine_label_text_matches(self):
        pats = injection_patterns()
        for sc in self.all:
            for c in sc["expect"]["checks"]:
                if "quarantined" not in c:
                    continue
                send = next(s["send"] for s in sc["steps"]
                            if s.get("send", {}).get("label")
                            == c["quarantined"])
                t = scenario.render(send["text"], IDS).lower()
                self.assertTrue(any(re.search(p, t) for p in pats), sc["id"])

    def test_bare_name_scenario_has_no_mention_in_q1(self):
        sc = next(s for s in self.all if s["id"] == "bare_name_ignored")
        q1 = sc["steps"][0]["send"]["text"]
        self.assertIn("{SUT_NAME}", q1)
        self.assertNotIn("<@", q1)

    def test_fixture_png(self):
        with open(scenario.fixture_path("test.png"), "rb") as f:
            data = f.read()
        self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
        w, h = struct.unpack(">II", data[16:24])
        self.assertEqual((w, h), (64, 64))
        self.assertLess(len(data), 4096)


class Render(unittest.TestCase):
    def test_render(self):
        self.assertEqual(
            scenario.render("<@SUT> ask <@A> and <@B>, {SUT_NAME}!", IDS),
            "<@111> ask <@222> and <@333>, muse-test!")

    def test_render_leaves_other_text(self):
        self.assertEqual(scenario.render("{x} <@999>", IDS), "{x} <@999>")


class Negative(unittest.TestCase):
    def bad(self, sc, *fragments, source=None):
        with self.assertRaises(scenario.ScenarioError) as cm:
            scenario.validate(sc, source=source)
        msg = str(cm.exception)
        for frag in fragments:
            self.assertIn(frag, msg)
        return msg

    def test_base_is_valid(self):
        scenario.validate(base())

    def test_not_a_mapping(self):
        self.bad(["x"], "mapping")

    def test_unknown_top_key(self):
        sc = base()
        sc["chekcs"] = []
        self.bad(sc, "sample", "unknown field(s) chekcs")

    def test_missing_required(self):
        sc = base()
        del sc["title"]
        self.bad(sc, "missing required field(s) title")

    def test_bad_id(self):
        sc = base()
        sc["id"] = "Bad-Id"
        self.bad(sc, "id", "[a-z0-9_]+")

    def test_id_must_equal_file_name(self):
        self.bad(base(), "other.yaml: id", "file name 'other'",
                 source="/x/other.yaml")

    def test_bad_tier(self):
        for t in (0, 3, "1", True):
            sc = base()
            sc["tier"] = t
            self.bad(sc, "tier")

    def test_bad_title(self):
        sc = base()
        sc["title"] = "  "
        self.bad(sc, "title")

    def test_b_is_bot_bool(self):
        sc = base()
        sc["b_is_bot"] = "yes"
        self.bad(sc, "b_is_bot")

    def test_config_mapping(self):
        sc = base()
        sc["config"] = ["x"]
        self.bad(sc, "config")

    def test_tier2_needs_rubric(self):
        sc = base()
        sc["tier"] = 2
        self.bad(sc, "judge.rubric")
        sc["judge"] = {"rubric": " "}
        self.bad(sc, "judge.rubric")
        sc["judge"] = {"rubric": "ok", "extra": 1}
        self.bad(sc, "judge", "extra")
        sc["judge"] = {"rubric": "Pass if fine."}
        scenario.validate(sc)

    def test_tier2_may_have_no_checks(self):
        sc = base()
        sc["tier"] = 2
        sc["judge"] = {"rubric": "x"}
        sc["expect"]["checks"] = []
        scenario.validate(sc)

    def test_tier1_needs_checks(self):
        sc = base()
        sc["expect"]["checks"] = []
        self.bad(sc, "expect.checks", "at least one")

    def test_tier1_no_judge(self):
        sc = base()
        sc["judge"] = {"rubric": "x"}
        self.bad(sc, "judge", "only tier-2")

    def test_steps_empty(self):
        sc = base()
        sc["steps"] = []
        self.bad(sc, "steps", "non-empty")

    def test_step_two_keys(self):
        sc = base()
        sc["steps"].append({"wait": 1, "action": "gateway_on"})
        self.bad(sc, "steps[2]", "exactly one key")

    def test_unknown_step(self):
        sc = base()
        sc["steps"].append({"sleep": 1})
        self.bad(sc, "steps[2]", "unknown step 'sleep'")

    def test_send_bad_driver(self):
        sc = base()
        sc["steps"][0]["send"]["as"] = "c"
        self.bad(sc, "steps[0].send.as")

    def test_send_unknown_field(self):
        sc = base()
        sc["steps"][0]["send"]["reply"] = "q1"
        self.bad(sc, "steps[0].send", "unknown field(s) reply")

    def test_send_needs_text_or_attach(self):
        sc = base()
        sc["steps"][0]["send"]["text"] = ""
        self.bad(sc, "steps[0].send", "text or an attach")

    def test_send_bad_placeholders(self):
        sc = base()
        sc["steps"][0]["send"]["text"] = "<@C> hi"
        self.bad(sc, "steps[0].send.text", "<@C>")
        sc["steps"][0]["send"]["text"] = "hi {BOT_NAME}"
        self.bad(sc, "steps[0].send.text", "{BOT_NAME}")

    def test_send_reply_to_unknown_label(self):
        sc = base()
        sc["steps"].append({"send": {"as": "b", "text": "x",
                                     "reply_to": "q9"}})
        self.bad(sc, "steps[2].send.reply_to", "'q9'", "q1")

    def test_send_reply_to_later_label(self):
        sc = base()
        sc["steps"].insert(0, {"send": {"as": "b", "text": "x",
                                        "reply_to": "q1"}})
        self.bad(sc, "steps[0].send.reply_to", "defined so far: none")

    def test_send_reply_to_last_sut_ok(self):
        sc = base()
        sc["steps"].append({"send": {"as": "b", "text": "x",
                                     "reply_to": "last_sut"}})
        scenario.validate(sc)

    def test_label_rules(self):
        sc = base()
        sc["steps"].append({"send": {"as": "b", "text": "x", "label": "q1"}})
        self.bad(sc, "steps[2].send.label", "defined twice")
        sc["steps"][2]["send"]["label"] = "last_sut"
        self.bad(sc, "reserved")
        sc["steps"][2]["send"]["label"] = "Q 2"
        self.bad(sc, "steps[2].send.label")

    def test_attach_missing_fixture(self):
        sc = base()
        sc["steps"][0]["send"]["attach"] = "nope.png"
        self.bad(sc, "steps[0].send.attach", "not found")
        sc["steps"][0]["send"]["attach"] = "../monitor.py"
        self.bad(sc, "steps[0].send.attach", "bare file name")

    def test_attach_existing_fixture(self):
        sc = base()
        sc["steps"][0]["send"]["attach"] = "test.png"
        scenario.validate(sc)

    def test_wait(self):
        sc = base()
        sc["steps"].append({"wait": 0})
        self.bad(sc, "steps[2].wait", "> 0")
        sc["steps"][2] = {"wait": "5"}
        self.bad(sc, "steps[2].wait")

    def test_wait_for(self):
        sc = base()
        sc["steps"][1] = {"wait_for": {"timeout": 5}}
        self.bad(sc, "steps[1].wait_for", "exactly one of")
        sc["steps"][1] = {"wait_for": {"sut_messages": 1,
                                       "outbox_processing": True}}
        self.bad(sc, "steps[1].wait_for", "exactly one of")
        sc["steps"][1] = {"wait_for": {"outbox_processing": False}}
        self.bad(sc, "outbox_processing", "true")
        sc["steps"][1] = {"wait_for": {"sut_messages": 1, "timeout": -1}}
        self.bad(sc, "steps[1].wait_for.timeout")
        sc["steps"][1] = {"wait_for": {"sut_messages": 1.5}}
        self.bad(sc, "steps[1].wait_for.sut_messages", "integer")

    def test_action(self):
        sc = base()
        sc["steps"].append({"action": "reboot"})
        self.bad(sc, "steps[2].action", "unknown action 'reboot'")

    def test_converse(self):
        sc = base()
        sc["steps"].append({"converse": {"as": "b", "lines": []}})
        self.bad(sc, "steps[2].converse.lines")
        sc["steps"][2] = {"converse": {"as": "b", "lines": ["ok", ""]}}
        self.bad(sc, "steps[2].converse.lines[1]")
        sc["steps"][2] = {"converse": {"lines": ["ok"]}}
        self.bad(sc, "steps[2].converse", "as")
        sc["steps"][2] = {"converse": {"as": "b", "lines": ["ok"],
                                       "per_turn_timeout": 0}}
        self.bad(sc, "per_turn_timeout")

    def test_expect_shape(self):
        sc = base()
        sc["expect"]["settle"] = -1
        self.bad(sc, "expect.settle")
        sc = base()
        sc["expect"]["wait"] = 1
        self.bad(sc, "expect", "unknown field(s) wait")
        sc = base()
        sc["expect"]["checks"] = {"replies_to": "q1"}
        self.bad(sc, "expect.checks", "list")

    def test_unknown_check(self):
        sc = base()
        sc["expect"]["checks"].append({"reply_count": {"eq": 1}})
        self.bad(sc, "expect.checks[2]", "unknown check 'reply_count'")

    def test_check_two_keys(self):
        sc = base()
        sc["expect"]["checks"].append({"replies_to": "q1", "max_chars": 9})
        self.bad(sc, "expect.checks[2]", "exactly one key")

    def test_count_specs(self):
        for spec, frag in (({}, "needs eq"), ({"eq": 1, "max": 2}, "eq"),
                           ({"min": 3, "max": 1}, "min 3 > max 1"),
                           ({"eq": -1}, "eq"), ({"around": 1}, "around"),
                           (1, "mapping")):
            sc = base()
            sc["expect"]["checks"] = [{"sut_reply_count": spec}]
            self.bad(sc, "sut_reply_count", frag)

    def test_label_checks_need_defined_labels(self):
        for chk in ({"replies_to": "q2"}, {"no_sut_reply_to": "q2"},
                    {"no_sut_before": "q2"}, {"quarantined": "q2"},
                    {"replies_to_any": ["q1", "q2"]},
                    {"latency": {"from": "q2", "max": 5}},
                    {"mentions": {"label": "q2", "user": "a"}},
                    {"ledger_status": {"label": "q2", "status": "replied"}}):
            sc = base()
            sc["expect"]["checks"] = [chk]
            self.bad(sc, "unknown label 'q2'")

    def test_last_sut_not_a_check_label(self):
        sc = base()
        sc["expect"]["checks"] = [{"replies_to": "last_sut"}]
        self.bad(sc, "unknown label 'last_sut'")

    def test_check_args(self):
        cases = [
            ({"replies_to_any": []}, "non-empty list"),
            ({"latency": {"from": "q1"}}, "max"),
            ({"latency": {"from": "q1", "max": 0}}, "latency.max"),
            ({"max_chars": 0}, "max_chars"),
            ({"max_chars": "2000"}, "max_chars"),
            ({"no_mass_mentions": False}, "true"),
            ({"no_duplicate_replies": "yes"}, "true"),
            ({"mentions": {"label": "q1", "user": "sut"}}, "mentions.user"),
            ({"ledger_status": {"label": "q1", "status": "sent"}}, "status"),
            ({"converse_turns": {"max": 4}}, "needs a converse step"),
            ({"content_matches": "("}, "invalid regular expression"),
            ({"content_matches": ""}, "content_matches"),
            ({"total_chars": {"max": "x"}}, "total_chars.max"),
        ]
        for chk, frag in cases:
            sc = base()
            sc["expect"]["checks"] = [chk]
            self.bad(sc, frag)

    def test_converse_turns_with_converse_step(self):
        sc = base()
        sc["steps"].append({"converse": {"as": "b", "lines": ["ok"]}})
        sc["expect"]["checks"] = [{"converse_turns": {"max": 4}}]
        scenario.validate(sc)


class LoadAll(unittest.TestCase):
    def write(self, d, name, text):
        with open(os.path.join(d, name), "w") as f:
            f.write(text)

    def dump(self, sc):
        import yaml
        return yaml.safe_dump(sc)

    def test_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as d:
            sc = base()
            self.write(d, "sample.yaml", self.dump(sc))
            self.write(d, "sample.yml", self.dump(sc))
            with self.assertRaises(scenario.ScenarioError) as cm:
                scenario.load_all(d, fixtures_dir=scenario.FIXTURES_DIR)
            self.assertIn("already used by sample.yaml", str(cm.exception))

    def test_invalid_yaml_names_file(self):
        with tempfile.TemporaryDirectory() as d:
            self.write(d, "broken.yaml", "id: [unclosed\n")
            with self.assertRaises(scenario.ScenarioError) as cm:
                scenario.load_all(d)
            self.assertTrue(str(cm.exception).startswith("broken.yaml: "
                                                         "invalid YAML"))

    def test_error_names_file_and_field(self):
        with tempfile.TemporaryDirectory() as d:
            sc = base()
            sc["steps"][0]["send"]["reply_to"] = "nope"
            self.write(d, "sample.yaml", self.dump(sc))
            with self.assertRaises(scenario.ScenarioError) as cm:
                scenario.load_all(d)
            self.assertIn("sample.yaml: steps[0].send.reply_to",
                          str(cm.exception))

    def test_empty_dir_and_missing_dir(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(scenario.ScenarioError):
                scenario.load_all(d)
            with self.assertRaises(scenario.ScenarioError):
                scenario.load_all(os.path.join(d, "missing"))

    def test_fixtures_dir_is_sibling(self):
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, "scenarios"))
            sc = copy.deepcopy(base())
            sc["steps"][0]["send"]["attach"] = "pic.png"
            self.write(os.path.join(d, "scenarios"), "sample.yaml",
                       self.dump(sc))
            with self.assertRaises(scenario.ScenarioError):
                scenario.load_all(os.path.join(d, "scenarios"))
            os.mkdir(os.path.join(d, "fixtures"))
            self.write(os.path.join(d, "fixtures"), "pic.png", "x")
            self.assertEqual(
                len(scenario.load_all(os.path.join(d, "scenarios"))), 1)


if __name__ == "__main__":
    unittest.main()
