"""monitor.py's test-only testing.treat_as_human setting, offline.

monitor.py is copied into a temp dir and imported from there with a
temp config (MUSE_DISCORD_CONFIG), so every file it might write lands
in the temp dir. Import does no network I/O.
"""
import contextlib
import importlib.util
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
E2E = os.path.dirname(HERE)
REPO = os.path.dirname(E2E)
sys.path.insert(0, E2E)

HUMAN_BOT = "200000000000000001"   # a driver bot listed as human
OTHER_BOT = "200000000000000002"   # a bot that is not listed
PERSON = "200000000000000003"      # a real user
SELF = "200000000000000009"        # the monitor's own bot id
CHAN = "300000000000000001"


def load_monitor(tmp, testing=None, name="monitor_under_test", extra=None):
    """Import a copy of monitor.py with a config holding `testing` (and
    the top-level keys in `extra`). Returns (module, stdout printed at
    import)."""
    shutil.copy(os.path.join(REPO, "monitor.py"), tmp)
    shutil.copy(os.path.join(REPO, "muse_settings.py"), tmp)
    token = os.path.join(tmp, "token")
    with open(token, "w") as f:
        f.write("not-a-real-token\n")
    cfg = os.path.join(tmp, "config.yaml")
    import yaml
    data = {"bot": {"token_file": token}, "behavior": {"bots": "off"}}
    if testing is not None:
        data["testing"] = testing
    data.update(extra or {})
    with open(cfg, "w") as f:
        yaml.safe_dump(data, f)
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(tmp, "monitor.py"))
    mod = importlib.util.module_from_spec(spec)
    out = io.StringIO()
    old_env = os.environ.get("MUSE_DISCORD_CONFIG")
    old_argv = sys.argv
    os.environ["MUSE_DISCORD_CONFIG"] = cfg
    sys.argv = [sys.argv[0]]
    try:
        with contextlib.redirect_stdout(out):
            spec.loader.exec_module(mod)
    finally:
        sys.argv = old_argv
        if old_env is None:
            os.environ.pop("MUSE_DISCORD_CONFIG", None)
        else:
            os.environ["MUSE_DISCORD_CONFIG"] = old_env
    mod.BOT_ID = mod.BOT_USER_ID = SELF
    mod.BOT_NAME = "muse"
    return mod, out.getvalue()


def author(uid, bot):
    return {"id": uid, "username": f"u{uid[-1]}", "bot": bot}


def msg(mid, uid, bot, content="hi", mention=True):
    m = {"id": str(mid), "channel_id": CHAN, "author": author(uid, bot),
         "content": content, "type": 0,
         "timestamp": "2026-10-17T00:00:00+00:00"}
    m["mentions"] = [{"id": SELF}] if mention else []
    return m


class TreatAsHumanSet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="tah-on-")
        # An int id in YAML must match the string id Discord sends.
        cls.mon, cls.out = load_monitor(
            cls.tmp, {"treat_as_human": [HUMAN_BOT, int(PERSON)]},
            name="monitor_tah_on")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_startup_warning(self):
        lines = [ln for ln in self.out.splitlines()
                 if "testing.treat_as_human" in ln]
        self.assertEqual(len(lines), 1, self.out)
        self.assertTrue(lines[0].startswith("WARNING:"))

    def test_is_bot_author(self):
        m = self.mon
        self.assertFalse(m.is_bot_author(author(HUMAN_BOT, True)))
        self.assertTrue(m.is_bot_author(author(OTHER_BOT, True)))
        self.assertFalse(m.is_bot_author(author(PERSON, False)))
        self.assertFalse(m.is_bot_author(None))

    def test_bots_off_policy_ignores_only_real_bots(self):
        m = self.mon
        self.assertEqual(m.BOTS_POLICY, "off")
        self.assertEqual(m.trigger_kind(msg(10, HUMAN_BOT, True)),
                         "mention")
        self.assertIsNone(m.trigger_kind(msg(11, OTHER_BOT, True)))
        state = {"pending": {}}
        self.assertEqual(m.classify_message(state, msg(12, HUMAN_BOT, True),
                                            "#t"), "mention")
        self.assertIsNone(m.classify_message(state, msg(13, OTHER_BOT, True),
                                             "#t"))

    def test_coalesce_window_and_burst_quiet(self):
        m = self.mon
        state = {}
        with contextlib.redirect_stdout(io.StringIO()):
            m.stage_trigger(state, msg(20, HUMAN_BOT, True), "#t", None,
                            "mention")
            m.stage_trigger(state, msg(21, OTHER_BOT, True), "#t", None,
                            "mention")
        p = state["pending"]
        self.assertEqual(p[f"{CHAN}:{HUMAN_BOT}"]["window"],
                         m.COALESCE_SECS)
        self.assertEqual(p[f"{CHAN}:{OTHER_BOT}"]["window"],
                         m.BOT_COALESCE_SECS)
        # 3s after the last message: past the human quiet (1.5s), short
        # of the bot quiet (5s).
        now = time.time()
        for uid, mid in ((HUMAN_BOT, "20"), (OTHER_BOT, "21")):
            m._activity[f"{CHAN}:{uid}"] = {"msg_id": mid,
                                            "msg_at": now - 3,
                                            "msg_seq": 1, "typing_seq": 0}
        self.assertTrue(m.burst_quiet(p[f"{CHAN}:{HUMAN_BOT}"], now))
        self.assertFalse(m.burst_quiet(p[f"{CHAN}:{OTHER_BOT}"], now))

    def test_dm_burst_quiet(self):
        """A DM burst flushes after DM_BURST_QUIET_SECS when its author
        counts as human, after BOT_BURST_QUIET_SECS when it is a bot."""
        m = self.mon
        state = {}
        dm_chan = "300000000000000077"
        with contextlib.redirect_stdout(io.StringIO()):
            for mid, uid in (("30", HUMAN_BOT), ("31", OTHER_BOT)):
                dm = dict(msg(mid, uid, True), channel_id=dm_chan)
                m.stage_trigger(state, dm, "DM", None, "dm", dm=True)
        p = state["pending"]
        # 1s after the last message: past the DM quiet (0.5s), short of
        # the server quiet (1.5s) and the bot quiet (5s).
        now = time.time()
        for uid, mid in ((HUMAN_BOT, "30"), (OTHER_BOT, "31")):
            m._activity[f"{dm_chan}:{uid}"] = {"msg_id": mid,
                                               "msg_at": now - 1,
                                               "msg_seq": 1, "typing_seq": 0}
        self.assertLess(m.DM_BURST_QUIET_SECS, 1)
        self.assertLess(1, m.BURST_QUIET_SECS)
        self.assertTrue(m.burst_quiet(p[f"{dm_chan}:{HUMAN_BOT}"], now))
        self.assertFalse(m.burst_quiet(p[f"{dm_chan}:{OTHER_BOT}"], now))

    def test_history_turns_and_participants(self):
        m = self.mon
        state = {}
        m.record_history(state, CHAN, [msg(30, OTHER_BOT, True),
                                       msg(31, HUMAN_BOT, True)])
        hist = state["history"][CHAN]
        self.assertEqual([h["bot"] for h in hist], [True, False])
        # A listed bot is a human: it breaks a bot run and has none.
        self.assertEqual(m.bot_run_length(hist, msg(32, HUMAN_BOT, True)),
                         0)
        self.assertEqual(m.bot_run_length(hist, msg(32, OTHER_BOT, True)),
                         1)
        people = {p["id"]: p for p in m.conversation_participants(
            hist, msg(33, HUMAN_BOT, True), None)}
        self.assertFalse(people[HUMAN_BOT]["bot"])
        self.assertTrue(people[OTHER_BOT]["bot"])

    def test_join_messages(self):
        m = self.mon
        join = msg(40, HUMAN_BOT, True, mention=False)
        join["type"] = 7
        self.assertTrue(m.is_join_message(join))
        join["author"] = author(OTHER_BOT, True)
        self.assertFalse(m.is_join_message(join))

    def test_queue_item_from_bot(self):
        m = self.mon
        m.BOTS_POLICY = "direct"
        try:
            items = []
            for mid, uid in ((50, HUMAN_BOT), (51, OTHER_BOT)):
                queue = []
                with contextlib.redirect_stdout(io.StringIO()):
                    state = m.load_state()
                    m.enqueue_message(msg(mid, uid, True, "hello"), "#t",
                                      state, queue, dm=True)
                self.assertEqual(len(queue), 1)
                items.append(queue[0])
        finally:
            m.BOTS_POLICY = "off"
        self.assertFalse(items[0]["from_bot"])
        self.assertTrue(items[1]["from_bot"])


class TreatAsHumanAbsent(unittest.TestCase):
    """Absent or empty: exactly the old behavior, and no warning."""

    def test_absent_and_empty(self):
        for testing in (None, {}, {"treat_as_human": []}):
            tmp = tempfile.mkdtemp(prefix="tah-off-")
            try:
                mon, out = load_monitor(tmp, testing, name="monitor_tah_off")
                self.assertNotIn("treat_as_human", out)
                self.assertTrue(mon.is_bot_author(author(HUMAN_BOT, True)))
                self.assertIsNone(mon.trigger_kind(msg(1, HUMAN_BOT, True)))
                self.assertFalse(mon.is_bot_author(author(PERSON, False)))
            finally:
                shutil.rmtree(tmp, ignore_errors=True)


class SutContract(unittest.TestCase):
    """What sut.py relies on in the real monitor.py: its ready and fatal
    log markers, and updates.check false keeping the update DMs off."""

    def test_log_markers_exist(self):
        import sut
        with open(os.path.join(REPO, "monitor.py")) as f:
            src = f.read()
        for marker in (sut.READY_IDENTITY, sut.READY_DISCOVERY,
                       sut.READY_GATEWAY_ON, sut.READY_GATEWAY_OFF) \
                + sut.FATAL_MARKERS:
            self.assertIn(marker, src)

    def test_updates_check_false_sends_no_update_dm(self):
        tmp = tempfile.mkdtemp(prefix="upd-off-")
        try:
            mon, _ = load_monitor(
                tmp, {"treat_as_human": [HUMAN_BOT]}, name="monitor_upd_off",
                extra={"updates": {"check": False},
                       "owners": {"user_ids": [HUMAN_BOT]}})
            with open(mon.UPDATE_CHECK_FILE, "w") as f:
                f.write('{"latest": "v9.9.9", "installed": "v1.0.0", '
                        '"entries": ["v9.9.9 (2026-10-18): Big"]}')
            sent = []
            mon.dm_owner = lambda uid, payload: sent.append(uid) or True
            mon.rest_backing_off = lambda: False
            state = {}
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertFalse(mon.updates_enabled())
                mon.announce_update(state)
            self.assertEqual(sent, [])
            self.assertFalse(state["update_notice"].get("owed"))
            # Control: the same file with updates on is announced.
            mon._updates_cfg.update(check=True, mtime=os.path.getmtime(
                os.path.join(tmp, "config.yaml")))
            mon._version_installed = lambda tag, timeout=5: False
            with contextlib.redirect_stdout(io.StringIO()):
                mon.announce_update(state)
            self.assertEqual(sent, [HUMAN_BOT])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class GatewayOffStub(unittest.TestCase):
    """sut.py's gateway_off mechanism against the real monitor code: with
    the stub `websockets` first on PYTHONPATH, the gateway thread dies
    with the GATEWAY DISABLED marker (REST polling carries on)."""

    def test_stub_disables_gateway(self):
        import sut
        tmp = tempfile.mkdtemp(prefix="gwoff-")
        try:
            stub = os.path.join(tmp, "stub", "websockets")
            os.makedirs(stub)
            with open(os.path.join(stub, "__init__.py"), "w") as f:
                f.write(sut.STUB_WEBSOCKETS)
            shutil.copy(os.path.join(REPO, "monitor.py"), tmp)
            shutil.copy(os.path.join(REPO, "muse_settings.py"), tmp)
            with open(os.path.join(tmp, "token"), "w") as f:
                f.write("x\n")
            with open(os.path.join(tmp, "config.yaml"), "w") as f:
                f.write(f"bot: {{token_file: {tmp}/token}}\n")
            code = ("import monitor, queue\n"
                    "st = {}\n"
                    "monitor._gateway_thread_main('x', queue.Queue(), st)\n"
                    "print('dead' if st.get('dead') else 'alive')\n")
            env = dict(os.environ, PYTHONPATH=os.path.join(tmp, "stub"),
                       MUSE_DISCORD_CONFIG=os.path.join(tmp, "config.yaml"))
            p = subprocess.run([sys.executable, "-c", code], cwd=tmp,
                               env=env, capture_output=True, text=True,
                               timeout=60)
            self.assertIn(sut.READY_GATEWAY_OFF, p.stdout, p.stderr)
            self.assertIn("e2e harness", p.stdout)
            self.assertTrue(p.stdout.strip().endswith("dead"), p.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
