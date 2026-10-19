"""The e2e test configs against the real settings code, offline.

Every scenario's config (config.test.yaml in the settings layout, or
config.legacy.yaml for legacy_config scenarios, plus its overrides) is
rendered the way sut.py does and loaded with muse_settings.py; then
monitor.py is imported under it (see monitor_util.py) to confirm what
the settings scenarios assert is what the monitor's code does:
follow-up windows, who.replies when-addressed, who.ignore, the
never_share backstop, and the old-layout notice.

Run from the repo root: python3 -m unittest discover -s e2e/tests -t .
"""
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
E2E = os.path.dirname(HERE)
REPO = os.path.dirname(E2E)
sys.path.insert(0, E2E)
sys.path.insert(0, HERE)
sys.path.insert(0, REPO)

import discord_rest  # noqa: E402
import fake_worker  # noqa: E402
import muse_settings  # noqa: E402
import scenario  # noqa: E402
import sut  # noqa: E402
from monitor_util import SELF, load_monitor_with  # noqa: E402

GUILD = "100000000000000000"
A = "200000000000000001"          # driver A, the owner
B = "200000000000000002"          # driver B
CHAN = "300000000000000001"
PLUGINS = os.path.join(REPO, "plugins")


def scenario_by_id(sid):
    return next(s for s in scenario.load_all(scenario.SCENARIOS_DIR)
                if s["id"] == sid)


def effective(sc, tmp):
    """The config.yaml dict sut.SUT would write for scenario sc."""
    token = os.path.join(tmp, "sut_token")
    persona = os.path.join(tmp, "persona.md")
    for path, text in ((token, "not-a-real-token\n"),
                       (persona, "You are a test bot.\n")):
        with open(path, "w") as f:
            f.write(text)
    cfg = sut.scenario_config(sut.base_config(GUILD, A, B), sc, A, B)
    stand_in = types.SimpleNamespace(token_file=token, persona_file=persona)
    return sut.SUT.effective_config(stand_in, cfg)


def write_yaml(path, data):
    import yaml
    with open(path, "w") as f:
        yaml.safe_dump(data, f, sort_keys=False)


def scenario_regexes(sc, check):
    return [c[check] for c in sc["expect"]["checks"] if check in c]


class ScenarioConfigsLoad(unittest.TestCase):
    """Each scenario's config is valid for the real settings loader."""

    def test_every_scenario_config_loads(self):
        for sc in scenario.load_all(scenario.SCENARIOS_DIR):
            with self.subTest(sc["id"]):
                tmp = tempfile.mkdtemp(prefix="e2e-cfg-")
                try:
                    path = os.path.join(tmp, "config.yaml")
                    write_yaml(path, effective(sc, tmp))
                    st = muse_settings.load(path, PLUGINS)
                    legacy = bool(sc.get("legacy_config"))
                    self.assertEqual(st.layout, "old" if legacy else "new")
                    self.assertEqual(bool(st.notice), legacy)
                    self.assertEqual(st.owners, [A])
                    self.assertFalse(st.updates_notify)
                    self.assertFalse(st.presence_enabled)
                    self.assertEqual(st.guild_ids(), [GUILD])
                    self.assertEqual(st.engine.get("rest_poll_seconds"),
                                     sut.REST_POLL_SECONDS)
                    humans = [A] if sc.get("b_is_bot") else [A, B]
                    self.assertEqual(st.raw["testing"]["treat_as_human"],
                                     humans)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)

    def test_overrides_land_at_their_scope(self):
        here = muse_settings.Scope.server(GUILD, "e2e", CHAN)
        cases = {
            "follow_up_when_addressed": ("who.replies", "when-addressed"),
            "ignore_user": ("who.ignore", [B]),
            "never_share_hold": ("privacy.never_share", ["blue pelican"]),
            "mention_basic": ("who.replies", "conversational"),
        }
        for sid, (key, want) in cases.items():
            with self.subTest(sid):
                tmp = tempfile.mkdtemp(prefix="e2e-cfg-")
                try:
                    path = os.path.join(tmp, "config.yaml")
                    write_yaml(path, effective(scenario_by_id(sid), tmp))
                    st = muse_settings.load(path, PLUGINS)
                    self.assertEqual(st.resolve(here)[key], want)
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)

    def test_legacy_notice_matches_the_scenario_check(self):
        sc = scenario_by_id("legacy_config_mention")
        self.assertTrue(sc.get("legacy_config"))
        (pattern,) = scenario_regexes(sc, "log_contains")
        log = "Bot user id: 1\n" + muse_settings.NOTICE + "\nmore\n"
        self.assertRegex(log, re.compile(pattern, re.M))


def snowflake(at=None):
    return discord_rest.time_snowflake(time.time() if at is None else at)


def message(uid, content="hello", mention=False, reply_to_author=None,
            mentions=(), at=None):
    m = {"id": snowflake(at), "channel_id": CHAN, "guild_id": GUILD,
         "type": 0, "content": content,
         "author": {"id": uid, "username": f"u{uid[-1]}", "bot": True},
         "timestamp": "2026-10-18T00:00:00+00:00",
         "mentions": [{"id": u} for u in mentions], "mention_roles": []}
    if mention:
        m["mentions"].append({"id": SELF})
    if reply_to_author:
        m["type"] = 19
        m["message_reference"] = {"message_id": snowflake(at)}
        m["referenced_message"] = {"id": snowflake(at),
                                   "author": {"id": reply_to_author}}
    return m


class MonitorUnder(unittest.TestCase):
    """Base: monitor.py imported under one scenario's config."""
    scenario_id = "mention_basic"

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="e2e-mon-")
        cfg = effective(scenario_by_id(cls.scenario_id), cls.tmp)
        cls.mon, cls.out = load_monitor_with(
            cls.tmp, cfg, name="monitor_" + cls.scenario_id)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def kind(self, msg, state):
        with contextlib.redirect_stdout(io.StringIO()):
            return self.mon.trigger_kind(msg, GUILD, state)


class BaseConfigMonitor(MonitorUnder):
    def test_startup(self):
        m = self.mon
        self.assertNotIn("old layout", self.out)
        self.assertIn("testing.treat_as_human", self.out)
        self.assertEqual(m.OWNER_USER_IDS, {A})
        self.assertEqual(m.TREAT_AS_HUMAN, {A, B})
        self.assertEqual(m.COALESCE_SECS, 5)
        self.assertEqual(m.GUILD_ALLOWLIST, {GUILD})
        self.assertFalse(m.updates_enabled())

    def test_no_window_without_a_sent_reply(self):
        self.assertIsNone(self.kind(message(A, "plain"), {}))
        self.assertEqual(self.kind(message(A, mention=True), {}),
                         "mention")

    def test_follow_up_answered(self):
        state = {}
        self.mon.open_follow_up(state, CHAN, A)
        self.assertEqual(self.kind(message(A, "and a dog?"), state),
                         "follow_up")

    def test_follow_up_not_for_others(self):
        state = {}
        self.mon.open_follow_up(state, CHAN, A)
        self.assertIsNone(self.kind(message(B, "me too"), state))
        # B's message left A's window open.
        self.assertEqual(self.kind(message(A, "history please"), state),
                         "follow_up")

    def test_follow_up_window_expires(self):
        """follow_up_expires is not a live scenario (a 120 s wait); the
        expiry is checked here instead."""
        m = self.mon
        state = {}
        m.open_follow_up(state, CHAN, A)
        state["follow_up"][f"{CHAN}:{A}"] -= m.FOLLOW_UP_WINDOW_SECS + 5
        self.assertIsNone(self.kind(message(A, "still there?"), state))
        m.open_follow_up(state, CHAN, A)  # the next reply reopens it
        self.assertEqual(self.kind(message(A, "now?"), state), "follow_up")

    def test_follow_up_closed_by_reply_to_someone_else(self):
        state = {}
        self.mon.open_follow_up(state, CHAN, A)
        self.assertIsNone(self.kind(
            message(A, "count me in", reply_to_author=B), state))
        self.assertIsNone(self.kind(message(A, "where to?"), state))

    def test_follow_up_closed_by_mention_of_someone_else(self):
        state = {}
        self.mon.open_follow_up(state, CHAN, A)
        self.assertIsNone(self.kind(
            message(A, f"<@{B}> you coming?", mentions=[B]), state))
        self.assertIsNone(self.kind(message(A, "anyone?"), state))

    def test_follow_up_queue_item_and_log_line(self):
        """What follow_up_answered checks: the item says follow_up, its
        card is the owner's in a public server channel, and the log line
        matches the scenario's regex."""
        sc = scenario_by_id("follow_up_answered")
        (pattern,) = scenario_regexes(sc, "log_contains")
        m = self.mon
        state, queue = {"guild_joined": {GUILD: 0}}, []
        msg = message(A, "and one for a dog, too")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            ok = m.enqueue_message(msg, "#e2e-x-follow_up_answered", state,
                                   queue, guild_id=GUILD, follow_up=True)
        self.assertTrue(ok, out.getvalue())
        (item,) = queue
        self.assertIs(item["follow_up"], True)
        self.assertEqual(item["settings"]["where"], "server")
        self.assertEqual(item["settings"]["speaker"], "owner")
        self.assertEqual(item["settings"]["audience"], "public")
        self.assertRegex(out.getvalue(), re.compile(pattern, re.M))
        rec = fake_worker.item_record(item)
        self.assertIs(rec["follow_up"], True)
        self.assertEqual(rec["message_id"], msg["id"])

    def test_mention_item_is_not_a_follow_up(self):
        m = self.mon
        state, queue = {"guild_joined": {GUILD: 0}}, []
        with contextlib.redirect_stdout(io.StringIO()):
            m.enqueue_message(message(A, mention=True), "#x", state, queue,
                              guild_id=GUILD)
        self.assertIs(queue[0]["follow_up"], False)


class WhenAddressedMonitor(MonitorUnder):
    scenario_id = "follow_up_when_addressed"

    def test_no_follow_up(self):
        state = {}
        self.mon.open_follow_up(state, CHAN, A)
        self.assertIsNone(self.kind(message(A, "soft one?"), state))
        self.assertEqual(self.kind(message(A, mention=True), state),
                         "mention")


class IgnoreUserMonitor(MonitorUnder):
    scenario_id = "ignore_user"

    def test_ignored_and_not(self):
        self.assertIsNone(self.kind(message(B, mention=True), {}))
        self.assertEqual(self.kind(message(A, mention=True), {}),
                         "mention")


class NeverShareMonitor(MonitorUnder):
    scenario_id = "never_share_hold"

    def reply_parts(self, label):
        sc = scenario_by_id(self.scenario_id)
        send = next(s["send"] for s in sc["steps"]
                    if s.get("send", {}).get("label") == label)
        text = scenario.render(send["text"], {"SUT": "1", "A": A, "B": B,
                                              "SUT_NAME": "muse"})
        out, _ = fake_worker.build_reply(
            {"message_id": "5", "channel_id": CHAN, "content": text}, A)
        return out, [out["text"]]

    def test_q1_reply_is_held_and_q2_is_not(self):
        m = self.mon
        out, parts = self.reply_parts("q1")
        self.assertTrue(m.never_share_hit({}, out, None, parts), parts)
        out, parts = self.reply_parts("q2")
        self.assertEqual(parts, ["ack 1"])
        self.assertFalse(m.never_share_hit({}, out, None, parts))

    def test_hold_log_line_matches_the_scenario(self):
        sc = scenario_by_id(self.scenario_id)
        (pattern,) = scenario_regexes(sc, "log_contains")
        m = self.mon
        out_item, parts = self.reply_parts("q1")
        out_item.update(channel_name="#e2e-x", author="ua")
        state = {"replied_to": []}
        # Discord refuses bot-to-bot DMs: the owner (driver A) is a bot.
        m.dm_owner = lambda uid, payload: False
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            m.hold_reply(state, out_item, "5", parts)
        self.assertRegex(log.getvalue(), re.compile(pattern, re.M))
        self.assertIn("no owner reachable", log.getvalue())
        self.assertNotIn("pelican", log.getvalue().lower())
        (hold,) = state["holds"].values()
        self.assertEqual(hold["item"]["item"], "5")


class LegacyMonitor(MonitorUnder):
    scenario_id = "legacy_config_mention"

    def test_notice_and_mapping(self):
        sc = scenario_by_id(self.scenario_id)
        (pattern,) = scenario_regexes(sc, "log_contains")
        self.assertRegex(self.out, re.compile(pattern, re.M))
        self.assertEqual(len(re.findall("uses the old layout", self.out)),
                         1)
        m = self.mon
        self.assertEqual(m.OWNER_USER_IDS, {A})
        self.assertEqual(m.TREAT_AS_HUMAN, {A, B})
        self.assertEqual(m.GUILD_ALLOWLIST, {GUILD})
        self.assertFalse(m.updates_enabled())
        self.assertEqual(self.kind(message(A, mention=True), {}),
                         "mention")


if __name__ == "__main__":
    unittest.main()
