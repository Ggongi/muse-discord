"""Offline tests for the e2e core: discord_rest, sut, fake_worker, run.

No network and no Discord tokens: Discord is a scripted fake, the
monitor is a stand-in script (sut tests) or a fake object (run tests),
and run.py's scenario / checks / judge modules are minimal stubs that
are put in sys.modules only while run.py is imported.
"""
import contextlib
import importlib
import io
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
E2E = os.path.dirname(HERE)
REPO = os.path.dirname(E2E)
sys.path.insert(0, E2E)

import discord_rest  # noqa: E402
import fake_worker  # noqa: E402
import sut  # noqa: E402

FAKE_TOKEN = "MTAwMDAwMDAwMDAwMDAwMDAx.GabcDE.abcdefghijklmnopqrstuvwxyz0123"


# ---------------------------------------------------------------------
# Stubs for the modules run.py imports but this branch does not own.

def _stub_modules():
    sc = types.ModuleType("scenario")

    class ScenarioError(Exception):
        pass
    sc.ScenarioError = ScenarioError
    sc.SCENARIOS = []

    def load_all(directory, tier=None):
        return [s for s in sc.SCENARIOS
                if tier is None or int(s.get("tier", 1)) == int(tier)]

    def render(text, ids):
        for k in ("SUT", "A", "B"):
            text = text.replace(f"<@{k}>", f"<@{ids[k]}>")
        return text.replace("{SUT_NAME}", ids.get("SUT_NAME", ""))
    sc.load_all, sc.render, sc.validate = load_all, render, lambda d: d

    ck = types.ModuleType("checks")

    def evaluate(scen, transcript):
        out = []
        for c in (scen.get("expect") or {}).get("checks") or []:
            name, arg = next(iter(c.items()))
            if name == "sut_reply_count":
                n = sum(1 for m in transcript["messages"]
                        if m["author"] == "sut")
                out.append({"name": name, "ok": n == arg["eq"],
                            "detail": f"{n} SUT messages"})
            else:
                out.append({"name": name, "ok": True, "detail": "stub"})
        return out
    ck.evaluate = evaluate

    jd = types.ModuleType("judge")
    jd.VERDICT = {"verdict": "pass", "reason": "stub judge"}
    jd.calls = []

    def judge(scen, transcript, timeout=300):
        jd.calls.append(scen["id"])
        return dict(jd.VERDICT)
    jd.judge = judge
    return sc, ck, jd


def load_run():
    """Import run.py against the stubs, then put sys.modules back so
    other test files see the real modules."""
    names = ("scenario", "checks", "judge", "run")
    saved = {n: sys.modules.get(n) for n in names}
    sc, ck, jd = _stub_modules()
    sys.modules.update(scenario=sc, checks=ck, judge=jd)
    sys.modules.pop("run", None)
    try:
        run = importlib.import_module("run")
    finally:
        for n, mod in saved.items():
            if mod is None:
                sys.modules.pop(n, None)
            else:
                sys.modules[n] = mod
    return run, sc, ck, jd


# ---------------------------------------------------------------------
# A scripted Discord.

class FakeHTTP:
    """Responses for Bot._open: (status, body dict/list/None, headers)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, req):
        self.requests.append(req)
        status, body, headers = self.responses.pop(0)
        raw = json.dumps(body).encode() if body is not None else b""
        if status >= 400:
            raise urllib.error.HTTPError(req.full_url, status, "err",
                                         headers or {}, io.BytesIO(raw))
        return _Resp(raw)


class _Resp:
    def __init__(self, raw):
        self.raw = raw

    def read(self):
        return self.raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Guild:
    """In-memory channels shared by FakeBots; the "SUT" answers every
    message that mentions it or replies to it, up to sut_limit replies
    per channel."""

    def __init__(self, sut_id="900", sut_limit=99):
        self.n = 1000
        self.channels = {}
        self.deleted = []
        self.deleted_messages = []  # (deleting bot's id, message id)
        self.sut_id = sut_id
        self.sut_limit = sut_limit
        self.sut_replies = {}

    def new_id(self):
        self.n += 1
        return str(self.n)

    def post(self, cid, author_id, content, reply_to=None, files=None):
        m = {"id": self.new_id(), "channel_id": cid, "type": 0,
             "author": {"id": author_id, "username": f"u{author_id}",
                        "bot": True},
             "content": content, "timestamp": "2026-10-17T00:00:00Z",
             "mentions": [{"id": u} for u in
                          re.findall(r"<@(\d+)>", content)],
             "mention_everyone": False, "mention_roles": [],
             "attachments": [{"filename": os.path.basename(f)}
                             for f in files or []]}
        if reply_to:
            m["type"] = 19
            m["message_reference"] = {"message_id": reply_to}
        self.channels[cid].append(m)
        if author_id != self.sut_id:
            self._maybe_answer(m)
        return m

    def _maybe_answer(self, m):
        cid = m["channel_id"]
        ref = (m.get("message_reference") or {}).get("message_id")
        to_sut = any(x["id"] == ref and x["author"]["id"] == self.sut_id
                     for x in self.channels[cid])
        mentioned = any(u["id"] == self.sut_id for u in m["mentions"])
        if (to_sut or mentioned) \
                and self.sut_replies.get(cid, 0) < self.sut_limit:
            self.sut_replies[cid] = self.sut_replies.get(cid, 0) + 1
            self.post(cid, self.sut_id, "ack 1", reply_to=m["id"])


class FakeBot:
    def __init__(self, guild, uid):
        self.guild, self.uid = guild, uid

    def me(self):
        return {"id": self.uid, "username": f"bot{self.uid}"}

    def send(self, cid, content, reply_to=None, files=None):
        return self.guild.post(cid, self.uid, content, reply_to, files)

    def history(self, cid, after=None):
        return [m for m in self.guild.channels.get(cid, [])
                if int(m["id"]) > int(after or 0)]

    def create_channel(self, gid, name):
        cid = self.guild.new_id()
        self.guild.channels[cid] = []
        return {"id": cid, "name": name}

    def delete_channel(self, cid):
        self.guild.deleted.append(cid)

    def delete_message(self, cid, mid):
        self.guild.channels[cid] = [m for m in self.guild.channels[cid]
                                    if m["id"] != mid]
        self.guild.deleted_messages.append((self.uid, mid))

    def guild_channels(self, gid):
        return []


class FakeSUT:
    """Stands in for sut.SUT in run.py tests."""
    fail_ready = False
    instances = []

    def __init__(self, tree=None, run_dir=None, base_config=None,
                 token=None):
        self.pkg_dir = tempfile.mkdtemp(prefix="fakesut-")
        self.gateway = True
        self.config = base_config
        self.running = False
        self.calls = []
        self.die_after = None  # alive() turns False after N checks
        self.cleaned = False
        FakeSUT.instances.append(self)

    def effective_config(self, cfg):
        return cfg

    def alive(self):
        if self.die_after is not None:
            self.die_after -= 1
            if self.die_after < 0:
                self.running = False
        return self.running

    def start(self, gateway=True, config=None):
        self.calls.append(("start", gateway))
        self.gateway = gateway
        if config is not None:
            self.config = config
        self.running = True

    def stop(self, kill=False):
        self.calls.append(("kill" if kill else "stop",))
        self.running = False

    def restart(self, gateway=None, config=None, kill=False):
        self.stop(kill)
        self.start(self.gateway if gateway is None else gateway, config)

    def wait_ready(self, timeout=180):
        self.calls.append(("ready",))
        if FakeSUT.fail_ready:
            raise sut.SUTError("not ready (fake)")
        return True

    def read_json(self, name):
        return {"entries": {}} if name == "mention_ledger.json" else None

    def log_tail(self, n=200):
        return f"log line; token={FAKE_TOKEN}"

    def cleanup(self):
        self.cleaned = True
        self.running = False
        shutil.rmtree(self.pkg_dir, ignore_errors=True)


# ---------------------------------------------------------------------

class DiscordRestTests(unittest.TestCase):
    def bot(self, responses):
        b = discord_rest.Bot(FAKE_TOKEN, sleep=lambda s: self.slept.append(s))
        b._open = FakeHTTP(responses)
        return b

    def setUp(self):
        self.slept = []

    def test_429_waits_retry_after(self):
        b = self.bot([(429, {"retry_after": 1.5}, {}),
                      (200, {"id": "1", "username": "x"}, {})])
        self.assertEqual(b.me()["id"], "1")
        self.assertEqual(len(self.slept), 1)
        self.assertAlmostEqual(self.slept[0], 1.6, places=3)
        req = b._open.requests[0]
        self.assertEqual(req.get_header("Authorization"), f"Bot {FAKE_TOKEN}")
        self.assertIn("DiscordBot", req.get_header("User-agent"))

    def test_5xx_retries_then_transient_error(self):
        b = self.bot([(502, {"message": "bad gateway"}, {})] * 5)
        with self.assertRaises(discord_rest.DiscordError) as cm:
            b.me()
        self.assertTrue(cm.exception.transient)
        self.assertEqual(cm.exception.status, 502)
        self.assertEqual(len(self.slept), discord_rest.MAX_5XX_RETRIES)

    def test_4xx_raises_without_token(self):
        b = self.bot([(403, {"message": "Missing Permissions"}, {})])
        with self.assertRaises(discord_rest.DiscordError) as cm:
            b.create_channel("1", "e2e-x")
        self.assertFalse(cm.exception.transient)
        self.assertIn("403", str(cm.exception))
        self.assertNotIn(FAKE_TOKEN, str(cm.exception))
        self.assertNotIn(FAKE_TOKEN, repr(b))

    def test_history_paginates_oldest_first(self):
        page1 = [{"id": str(i)} for i in range(200, 100, -1)]  # newest 1st
        page2 = [{"id": str(i)} for i in range(205, 200, -1)]
        b = self.bot([(200, page1, {}), (200, page2, {})])
        msgs = b.history("55", after="100")
        self.assertEqual([m["id"] for m in msgs],
                         [str(i) for i in range(101, 206)])
        urls = [r.full_url for r in b._open.requests]
        self.assertIn("after=100&limit=100", urls[0])
        self.assertIn("after=200&limit=100", urls[1])

    def test_send_reply_and_files(self):
        b = self.bot([(200, {"id": "9"}, {}), (200, {"id": "10"}, {})])
        b.send("55", "hi", reply_to="7")
        body = json.loads(b._open.requests[0].data)
        self.assertEqual(body["message_reference"]["message_id"], "7")
        # Replies never ping the replied-to author (reply != mention).
        self.assertEqual(body["allowed_mentions"],
                         {"parse": ["users"], "replied_user": False})
        with tempfile.NamedTemporaryFile(suffix=".png") as f:
            f.write(b"\x89PNG")
            f.flush()
            b.send("55", "look", files=[f.name])
            req = b._open.requests[1]
            self.assertIn("multipart/form-data",
                          req.get_header("Content-type"))
            self.assertIn(b'name="payload_json"', req.data)
            self.assertIn(os.path.basename(f.name).encode(), req.data)

    def test_delete_missing_channel_is_fine(self):
        b = self.bot([(404, {"message": "Unknown Channel"}, {})])
        self.assertIsNone(b.delete_channel("5"))

    def test_delete_message(self):
        b = self.bot([(204, None, {}),
                      (404, {"message": "Unknown Message"}, {}),
                      (403, {"message": "Missing Permissions"}, {})])
        self.assertIsNone(b.delete_message("55", "7"))
        req = b._open.requests[0]
        self.assertEqual(req.get_method(), "DELETE")
        self.assertTrue(req.full_url.endswith("/channels/55/messages/7"))
        self.assertIsNone(b.delete_message("55", "7"))  # already gone
        with self.assertRaises(discord_rest.DiscordError):
            b.delete_message("55", "8")

    def test_snowflake_roundtrip(self):
        t = 1760000000.0
        self.assertAlmostEqual(
            discord_rest.snowflake_time(discord_rest.time_snowflake(t)), t,
            places=2)


class ConfigTests(unittest.TestCase):
    def test_template_renders_fully(self):
        cfg = sut.base_config("10", "20", "30")
        text = json.dumps(cfg)
        self.assertNotIn("{{", text.replace("{{TOKEN_FILE}}", "")
                         .replace("{{PERSONA_FILE}}", ""))
        self.assertEqual(cfg["owners"]["user_ids"], ["20"])
        self.assertEqual(cfg["bot"]["guild_ids"], ["10"])
        self.assertEqual(cfg["behavior"]["active_guild_ids"], ["10"])
        self.assertEqual(cfg["testing"]["treat_as_human"], ["20", "30"])
        self.assertFalse(cfg["presence"]["enabled"])
        self.assertFalse(cfg["updates"]["check"])
        self.assertEqual(cfg["engine"]["rest_poll_seconds"], 10)

    def test_missing_list_placeholder_dropped(self):
        cfg = sut.render_config({"l": ["{{A}}", "{{B}}"], "s": "{{B}}"},
                                {"A": 1})
        self.assertEqual(cfg, {"l": ["1"], "s": None})

    def test_scenario_config(self):
        base = sut.base_config("10", "20", "30")
        c = sut.scenario_config(base, {"b_is_bot": True,
                                       "config": {"behavior": {"bots": "off"}}},
                                "20", "30")
        self.assertEqual(c["testing"]["treat_as_human"], ["20"])
        self.assertEqual(c["behavior"]["bots"], "off")
        self.assertEqual(c["behavior"]["coalesce_seconds"], 5)
        self.assertEqual(base["behavior"]["bots"], "direct")  # untouched
        c2 = sut.scenario_config(base, {}, "20", "30")
        self.assertEqual(c2["testing"]["treat_as_human"], ["20", "30"])
        c3 = sut.scenario_config(
            base, {"config": {"testing": {"treat_as_human": []}}}, "20", "30")
        self.assertEqual(c3["testing"]["treat_as_human"], [])


FAKE_MONITOR = r'''
import json, os, sys, time
import yaml
cfg = yaml.safe_load(open(os.environ["MUSE_DISCORD_CONFIG"]))
here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(here, "env_seen.json"), "w") as f:
    json.dump(sorted(k for k in os.environ if k.startswith("E2E_")
                     or k == "CLAUDE_CODE_OAUTH_TOKEN"), f)
if (cfg.get("testing") or {}).get("crash"):
    print("boom", flush=True)
    sys.exit(2)
print("Bot user id: 1", flush=True)
print("muse-discord monitor starting. Polling 1 channels.", flush=True)
try:
    import websockets  # noqa: F401
    print("Gateway READY: realtime ingest live.", flush=True)
except ImportError as e:
    if "e2e harness" in str(e):
        print(f"GATEWAY DISABLED (thread died: {e})", flush=True)
    else:  # not installed here: pretend the gateway came up
        print("Gateway READY: realtime ingest live.", flush=True)
while True:
    time.sleep(0.2)
'''


class SUTTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="e2e-sut-")
        self.tree = os.path.join(self.tmp, "tree")
        os.makedirs(os.path.join(self.tree, ".git"))
        os.makedirs(os.path.join(self.tree, "bin"))
        with open(os.path.join(self.tree, "monitor.py"), "w") as f:
            f.write(FAKE_MONITOR)
        for name in ("state.json", "config.yaml", ".git/HEAD",
                     "bin/outbox-append.py", "update_check.json",
                     "update_request.json", "watchdog.lock"):
            with open(os.path.join(self.tree, name), "w") as f:
                f.write("{}\n")
        self.cfg = sut.scenario_config(sut.base_config("10", "20", "30"),
                                       {}, "20", "30")
        os.environ["E2E_SUT_TOKEN"] = FAKE_TOKEN
        self.s = sut.SUT(self.tree, os.path.join(self.tmp, "run"), self.cfg,
                         token=FAKE_TOKEN)

    def tearDown(self):
        os.environ.pop("E2E_SUT_TOKEN", None)
        self.s.cleanup()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_copy_token_and_config(self):
        pkg = self.s.pkg_dir
        self.assertTrue(os.path.exists(os.path.join(pkg, "monitor.py")))
        self.assertTrue(os.path.exists(os.path.join(pkg, "bin",
                                                    "outbox-append.py")))
        for gone in (".git", "state.json", "update_check.json",
                     "update_request.json", "watchdog.lock"):
            self.assertFalse(os.path.exists(os.path.join(pkg, gone)), gone)
        mode = stat.S_IMODE(os.stat(self.s.token_file).st_mode)
        self.assertEqual(mode, 0o600)
        self.assertFalse(self.s.token_file.startswith(pkg))
        with open(self.s.token_file) as f:
            self.assertEqual(f.read().strip(), FAKE_TOKEN)
        import yaml
        with open(os.path.join(pkg, "config.yaml")) as f:
            cfg = yaml.safe_load(f)
        self.assertEqual(cfg["bot"]["token_file"], self.s.token_file)
        self.assertEqual(cfg["engine"]["rest_poll_seconds"], 10)
        self.assertFalse(cfg["presence"]["enabled"])
        self.assertFalse(cfg["updates"]["check"])
        self.assertTrue(os.path.exists(cfg["persona"]["file"]))
        self.assertNotIn(FAKE_TOKEN, json.dumps(cfg))

    def test_lifecycle_and_gateway_off(self):
        s = self.s
        s.start()
        self.assertTrue(s.wait_ready(20))
        self.assertTrue(s.alive())
        with open(os.path.join(s.pkg_dir, "env_seen.json")) as f:
            self.assertEqual(json.load(f), [])  # no secrets in its env
        s.restart(gateway=False)
        self.assertTrue(s.wait_ready(20))
        self.assertIn("GATEWAY DISABLED", s.log_tail(5))
        self.assertFalse(s.gateway)
        s.restart(gateway=True)
        self.assertTrue(s.wait_ready(20))
        self.assertIn("Gateway READY", s.log_tail(2))
        s.stop(kill=True)
        self.assertFalse(s.alive())
        s.start(config=sut.deep_merge(self.cfg, {"behavior": {"bots": "off"}}))
        s.wait_ready(20)
        self.assertEqual(s.config["behavior"]["bots"], "off")
        s.stop()
        self.assertFalse(s.alive())

    def test_wait_ready_reports_death(self):
        s = self.s
        s.start(config=sut.deep_merge(self.cfg, {"testing": {"crash": True}}))
        with self.assertRaises(sut.SUTError):
            s.wait_ready(20)

    def test_read_json_and_cleanup(self):
        with open(os.path.join(self.s.pkg_dir, "quarantine.json"), "w") as f:
            json.dump([{"id": "1"}], f)
        self.assertEqual(self.s.read_json("quarantine.json"), [{"id": "1"}])
        self.assertIsNone(self.s.read_json("mention_ledger.json"))
        self.s.cleanup()
        self.assertFalse(os.path.exists(self.s.token_file))


A_ID = "20"


def queue_item(mid, content, fragments=(), kind="mention"):
    return {"message_id": mid, "channel_id": "77", "channel_name": "#t",
            "guild_id": "10", "kind": kind, "author": "a", "author_id": A_ID,
            "content": "\n".join([content] + [c for _, c in fragments]),
            "burst": bool(fragments),
            "burst_fragments": [{"message_id": i, "content": c}
                                for i, c in fragments]}


class FakeWorkerTests(unittest.TestCase):
    def setUp(self):
        self.pkg = tempfile.mkdtemp(prefix="e2e-fw-")
        os.makedirs(os.path.join(self.pkg, "bin"))
        shutil.copy(os.path.join(REPO, "bin", "outbox-append.py"),
                    os.path.join(self.pkg, "bin"))
        with open(os.path.join(self.pkg, "config.yaml"), "w") as f:
            f.write(f'owners: {{user_ids: ["{A_ID}"]}}\n')

    def tearDown(self):
        shutil.rmtree(self.pkg, ignore_errors=True)

    def write_queue(self, items):
        with open(os.path.join(self.pkg, "reply_queue.json"), "w") as f:
            json.dump(items, f)

    def outbox(self):
        return fake_worker.load_json(
            os.path.join(self.pkg, "reply_outbox.json"), [])

    def test_directives_end_to_end(self):
        self.write_queue([
            queue_item("101", "<@900> hello"),
            queue_item("102", "first", [("103", "second"), ("104", "third")]),
            queue_item("105", "shh [[silent]]"),
            queue_item("106", "[[long]] please"),
            queue_item("107", "ping [[everyone]]"),
            queue_item("108", "who? [[mention_a]]"),
            queue_item("109", "hi", [("110", "[[silent]]")]),
            queue_item("111", "welcome!", kind="welcome"),
        ])
        p = subprocess.Popen([sys.executable,
                              os.path.join(E2E, "fake_worker.py"),
                              "--pkg", self.pkg, "--interval", "0.1"],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True)
        try:
            deadline = time.time() + 20
            while len(self.outbox()) < 8 and time.time() < deadline:
                time.sleep(0.1)
            time.sleep(0.5)  # nothing more may arrive (no double answers)
        finally:
            p.send_signal(signal.SIGTERM)
            out, _ = p.communicate(timeout=10)
        self.assertEqual(p.returncode, 0, out)
        got = {o["item"]: o for o in self.outbox()}
        self.assertEqual(len(self.outbox()), 8, out)
        self.assertEqual(got["101"]["text"], "ack 1")
        self.assertEqual(got["101"]["reply_to"], "101")
        self.assertEqual(got["101"]["channel_id"], "77")
        self.assertEqual(got["102"]["text"], "ack 3")
        self.assertEqual(got["102"]["reply_to"], "104")  # last fragment
        self.assertTrue(got["105"]["silent"])
        self.assertNotIn("text", got["105"])
        self.assertEqual(len(got["106"]["text"]), 3500)
        self.assertIn("@everyone", got["107"]["text"])
        self.assertIn("<@&10>", got["107"]["text"])
        self.assertIn(f"<@{A_ID}>", got["108"]["text"])
        self.assertTrue(got["109"]["silent"])  # directive in a fragment
        self.assertEqual(got["109"]["reply_to"], "110")
        self.assertNotIn("reply_to", got["111"])  # welcomes: no reply_to

    def test_slow_and_never_twice(self):
        self.write_queue([queue_item("201", "take your time [[slow]]"),
                          queue_item("202", "quick")])
        now = [1000.0]
        w = fake_worker.Worker(self.pkg, clock=lambda: now[0])
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(w.tick(), 1)          # 202 now, 201 later
            now[0] += 14
            self.assertEqual(w.tick(), 0)
            now[0] += 1
            self.assertEqual(w.tick(), 1)          # 15 s later
            self.assertEqual(w.tick(), 0)          # never twice
        self.assertEqual([o["item"] for o in self.outbox()], ["202", "201"])
        # A restarted worker skips items the outbox already answers.
        with contextlib.redirect_stdout(io.StringIO()):
            w2 = fake_worker.Worker(self.pkg, clock=lambda: now[0])
            self.assertEqual(w2.tick(), 0)


class RunHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.R, cls.sc, cls.ck, cls.jd = load_run()

    def test_stubs_did_not_leak(self):
        for name, stub in (("scenario", self.sc), ("checks", self.ck),
                           ("judge", self.jd)):
            self.assertIsNot(sys.modules.get(name), stub)
        self.assertIs(self.R.scenario, self.sc)

    def test_redact(self):
        os.environ["E2E_DRIVER_B_TOKEN"] = "plain-secret-value"
        try:
            out = self.R.redact({"a": [f"x {FAKE_TOKEN} y",
                                         "plain-secret-value"], "n": 3})
        finally:
            os.environ.pop("E2E_DRIVER_B_TOKEN")
        self.assertEqual(out, {"a": ["x [REDACTED] y", "[REDACTED]"],
                               "n": 3})

    def test_status_and_exit_code(self):
        st = self.R.scenario_status
        ok = [{"name": "x", "ok": True}]
        bad = [{"name": "x", "ok": False}]
        self.assertEqual(st({"tier": 1}, ok, None), "pass")
        self.assertEqual(st({"tier": 1}, bad, None), "fail")
        self.assertEqual(st({"tier": 2}, ok, {"verdict": "fail"}), "warn")
        self.assertEqual(st({"tier": 2}, ok, {"verdict": "error"}), "warn")
        self.assertEqual(st({"tier": 2}, bad, {"verdict": "pass"}), "fail")
        self.assertEqual(st({"tier": 1}, ok, None, flake=True), "flake")
        ec = self.R.exit_code
        self.assertEqual(ec([{"status": s} for s in
                             ("pass", "warn", "flake", "skip")]), 0)
        self.assertEqual(ec([{"status": "pass"}, {"status": "fail"}]), 1)

    def test_summary(self):
        results = [
            {"scenario": "a_b", "tier": 1, "status": "fail",
             "checks": [{"name": "max_chars", "ok": False,
                         "detail": "2100 | too long"}],
             "judge": None, "duration": 3.0, "error": None},
            {"scenario": "c", "tier": 2, "status": "warn", "checks": [],
             "judge": {"verdict": "fail", "reason": "rude"},
             "duration": 9.0, "error": None},
            {"scenario": "d", "tier": 1, "status": "flake", "checks": [],
             "judge": None, "duration": 0.0, "error": "Discord: 502"},
        ]
        md = self.R.render_summary(results, 1)
        self.assertTrue(md.startswith("<!-- muse-discord-e2e -->\n"))
        self.assertIn("| scenario | tier | status | notes |", md)
        self.assertIn("| a_b | 1 | **fail** | max_chars: 2100 \\| too long |",
                      md)
        self.assertIn("FLAKED", md)
        self.assertIn("**c**: fail: rude", md)
        self.assertIn("transcripts", md)
        self.assertIn(": FAIL", md.splitlines()[1])

    def test_transcript_message(self):
        ids = {"SUT": "1", "A": "2", "B": "3", "SUT_NAME": "m"}
        m = {"id": "9", "author": {"id": "1"}, "content": "x", "type": 19,
             "message_reference": {"message_id": "8"},
             "timestamp": "t", "attachments": [{"filename": "c.png"}],
             "mentions": [{"id": "2"}], "mention_everyone": False,
             "mention_roles": ["5"]}
        t = self.R.transcript_message(m, ids)
        self.assertEqual(t, {"id": "9", "author": "sut", "author_id": "1",
                             "ts": "t", "content": "x", "reply_to": "8",
                             "attachments": ["c.png"], "mentions": ["2"],
                             "mention_everyone": False,
                             "mention_roles": ["5"]})
        m2 = {"id": "10", "author": {"id": "77"}, "type": 0}
        t2 = self.R.transcript_message(m2, ids)
        self.assertEqual((t2["author"], t2["reply_to"]), ("other", None))

    def test_channel_name(self):
        name = self.R.channel_name("123", "x" * 200)
        self.assertEqual(len(name), 100)
        self.assertTrue(name.startswith("e2e-123-x"))


class RunFlowTests(unittest.TestCase):
    """run.py end to end against a fake Discord and a fake SUT."""

    ENV = {"E2E_GUILD_ID": "10", "E2E_SUT_TOKEN": FAKE_TOKEN,
           "E2E_DRIVER_A_TOKEN": "tokA-secret", "E2E_DRIVER_B_TOKEN":
           "tokB-secret"}

    def setUp(self):
        self.R, self.sc, self.ck, self.jd = load_run()
        self.out = tempfile.mkdtemp(prefix="e2e-out-")
        self.guild = Guild(sut_id="900")
        ids = {FAKE_TOKEN: "900", "tokA-secret": "20", "tokB-secret": "30"}
        guild = self.guild
        self.patches = [
            (self.R.discord_rest, "Bot",
             lambda token: FakeBot(guild, ids[token])),
            (self.R.sut, "SUT", FakeSUT),
            (self.R, "start_worker", lambda *a: types.SimpleNamespace(
                poll=lambda: None, pid=-1)),
            (self.R, "stop_worker", lambda proc: None),
            (self.R.time, "sleep", lambda s: None),
        ]
        self.saved = [(o, n, getattr(o, n)) for o, n, _ in self.patches]
        for o, n, v in self.patches:
            setattr(o, n, v)
        self.saved_env = {k: os.environ.get(k) for k in self.ENV}
        os.environ.update(self.ENV)
        FakeSUT.fail_ready = False
        FakeSUT.instances = []

    def tearDown(self):
        for o, n, v in self.saved:
            setattr(o, n, v)
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.out, ignore_errors=True)

    def main(self, *extra, tier=1):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = self.R.main(["--tree", REPO, "--tier", str(tier),
                                  "--out", self.out, *extra])
        return code, buf.getvalue()

    def outputs(self):
        with open(os.path.join(self.out, "results.json")) as f:
            results = json.load(f)
        with open(os.path.join(self.out, "summary.md")) as f:
            summary = f.read()
        return results, summary

    def assert_no_tokens(self):
        for root, _, files in os.walk(self.out):
            for name in files:
                with open(os.path.join(root, name)) as f:
                    text = f.read()
                for tok in self.ENV.values():
                    if tok != "10":
                        self.assertNotIn(tok, text, name)

    def test_full_run(self):
        self.sc.SCENARIOS = [
            {"id": "mention_basic", "tier": 1, "title": "t",
             "steps": [{"send": {"as": "a", "text": "<@SUT> hi",
                                 "label": "q1"}},
                       {"wait_for": {"sut_messages": 1, "timeout": 30}},
                       {"send": {"as": "b", "text": "and me <@SUT>",
                                 "reply_to": "last_sut", "label": "q2"}},
                       {"action": "gateway_off"},
                       {"action": "kill_monitor"},
                       {"wait": 1},
                       {"action": "start_monitor"}],
             "expect": {"settle": 1,
                        "checks": [{"sut_reply_count": {"eq": 2}}]}},
            {"id": "too_many", "tier": 1, "title": "t", "b_is_bot": True,
             "config": {"behavior": {"bots": "off"}},
             "steps": [{"send": {"as": "a", "text": "<@SUT> hi"}}],
             "expect": {"checks": [{"sut_reply_count": {"eq": 5}}]}},
            {"id": "tier_two", "tier": 2, "title": "t", "steps": []},
        ]
        code, out = self.main()
        self.assertEqual(code, 1, out)  # too_many fails its check
        results, summary = self.outputs()
        by = {r["scenario"]: r for r in results}
        self.assertEqual(set(by), {"mention_basic", "too_many"})
        self.assertEqual(by["mention_basic"]["status"], "pass", by)
        self.assertEqual(by["too_many"]["status"], "fail")
        self.assertIsNone(by["mention_basic"]["judge"])
        with open(os.path.join(self.out, "transcripts",
                               "mention_basic.json")) as f:
            tr = json.load(f)
        self.assertEqual(set(tr["labels"]), {"q1", "q2"})
        self.assertEqual(tr["ids"]["SUT"], "900")
        self.assertEqual([m["author"] for m in tr["messages"]],
                         ["a", "sut", "b", "sut"])
        self.assertEqual(tr["messages"][2]["reply_to"], tr["messages"][1]["id"])
        self.assertIn("[REDACTED]", tr["monitor_log_tail"])
        self.assertEqual(sorted(self.guild.deleted),
                         sorted(self.guild.channels))
        fs = FakeSUT.instances[0]
        self.assertTrue(fs.cleaned)
        self.assertIn(("start", False), fs.calls)   # gateway_off
        self.assertIn(("kill",), fs.calls)
        # too_many ran under its own config, B not treated as human.
        cfgs = [c for c in [fs.config]]
        self.assertEqual(cfgs[0]["behavior"]["bots"], "off")
        self.assertEqual(cfgs[0]["testing"]["treat_as_human"], ["20"])
        self.assertTrue(summary.startswith("<!-- muse-discord-e2e -->"))
        self.assert_no_tokens()

    def test_delete_step(self):
        self.sc.SCENARIOS = [
            {"id": "deleted", "tier": 1, "title": "t",
             "steps": [{"send": {"as": "a", "text": "plain", "label": "c1"}},
                       {"send": {"as": "b", "text": "<@SUT> hi",
                                 "label": "q1"}},
                       {"delete": "q1"},
                       {"delete": "c1"}],
             "expect": {"checks": [{"sut_reply_count": {"eq": 1}},
                                   {"no_sut_reply_to": "c1"}]}},
        ]
        code, out = self.main()
        self.assertEqual(code, 0, out)
        results, _ = self.outputs()
        self.assertEqual(results[0]["status"], "pass", results)
        with open(os.path.join(self.out, "transcripts",
                               "deleted.json")) as f:
            tr = json.load(f)
        # Each message deleted by the driver that sent it; the labels
        # keep the ids, the history no longer has the messages.
        self.assertEqual(self.guild.deleted_messages,
                         [("30", tr["labels"]["q1"]),
                          ("20", tr["labels"]["c1"])])
        self.assertEqual([m["author"] for m in tr["messages"]], ["sut"])

    def test_tier2_judge_and_converse(self):
        self.guild.sut_limit = 3
        self.jd.VERDICT = {"verdict": "fail", "reason": "too chatty"}
        self.sc.SCENARIOS = [
            {"id": "loop", "tier": 2, "title": "t", "b_is_bot": True,
             "steps": [{"send": {"as": "b", "text": "<@SUT> hey"}},
                       {"converse": {"as": "b", "lines": ["1", "2", "3",
                                                          "4", "5"],
                                     "per_turn_timeout": 5}}],
             "expect": {"checks": []}, "judge": {"rubric": "x"}},
        ]
        code, out = self.main(tier=2)
        self.assertEqual(code, 0, out)
        results, summary = self.outputs()
        self.assertEqual(results[0]["status"], "warn")
        self.assertEqual(self.jd.calls, ["loop"])
        with open(os.path.join(self.out, "transcripts", "loop.json")) as f:
            tr = json.load(f)
        self.assertEqual(tr["converse_turns"], 3)
        self.assertIn("too chatty", summary)

    def test_not_ready_is_flake_and_cleans_up(self):
        FakeSUT.fail_ready = True
        self.sc.SCENARIOS = [{"id": "s1", "tier": 1, "title": "t",
                              "steps": []},
                             {"id": "s2", "tier": 1, "title": "t",
                              "steps": []}]
        code, out = self.main()
        self.assertEqual(code, 0, out)
        results, summary = self.outputs()
        self.assertEqual([r["status"] for r in results], ["flake", "flake"])
        self.assertIn("FLAKED", summary)
        self.assertEqual(len(self.guild.deleted), 2)
        self.assertTrue(FakeSUT.instances[0].cleaned)

    def test_sut_death_mid_scenario_is_flake(self):
        orig_start = FakeSUT.start

        def start(fs, gateway=True, config=None):
            orig_start(fs, gateway, config)
            fs.die_after = 3
        self.sc.SCENARIOS = [{"id": "s1", "tier": 1, "title": "t",
                              "steps": [{"wait": 1}] * 10}]
        FakeSUT.start = start
        try:
            code, out = self.main()
        finally:
            FakeSUT.start = orig_start
        results, _ = self.outputs()
        self.assertEqual(results[0]["status"], "flake", results)
        self.assertIn("exited unexpectedly", results[0]["error"])

    def test_wait_for_timeouts_continue(self):
        self.guild.sut_limit = 0  # the SUT never answers
        clock = [0.0]
        self.R.time.sleep = lambda s: clock.__setitem__(0, clock[0] + s)
        orig = self.R.Runner.__init__

        def init(runner, *a, **kw):
            orig(runner, *a, **kw)
            runner.clock = lambda: clock[0]
            runner.sleep = self.R.time.sleep
        self.R.Runner.__init__ = init
        self.sc.SCENARIOS = [{"id": "s1", "tier": 1, "title": "t",
                              "steps": [
                                  {"send": {"as": "a", "text": "<@SUT> x",
                                            "label": "q1"}},
                                  {"wait_for": {"outbox_processing": True,
                                                "timeout": 1}},
                                  {"wait_for": {"sut_messages": 1,
                                                "timeout": 10}},
                                  {"send": {"as": "b", "text": "after"}}],
                              "expect": {"checks": [
                                  {"sut_reply_count": {"eq": 1}}]}}]
        try:
            code, out = self.main()
        finally:
            self.R.Runner.__init__ = orig
        self.assertEqual(code, 1)
        results, summary = self.outputs()
        r = results[0]
        self.assertEqual(r["status"], "fail")
        self.assertIsNone(r["error"])
        self.assertEqual(len(r["notes"]), 2, r)
        self.assertIn("timed out", summary)
        with open(os.path.join(self.out, "transcripts", "s1.json")) as f:
            tr = json.load(f)
        # The scenario carried on after both timeouts.
        self.assertEqual([m["author"] for m in tr["messages"]], ["a", "b"])

    def test_keep_channels(self):
        self.sc.SCENARIOS = [{"id": "s1", "tier": 1, "title": "t",
                              "steps": []}]
        self.main("--keep-channels")
        self.assertEqual(self.guild.deleted, [])

    def test_missing_secrets_skip(self):
        os.environ.pop("E2E_DRIVER_B_TOKEN")
        self.sc.SCENARIOS = [{"id": "s1", "tier": 1, "title": "t",
                              "steps": []}]
        code, out = self.main()
        self.assertEqual(code, 0)
        results, summary = self.outputs()
        self.assertEqual(results[0]["status"], "skip")
        self.assertIn("skipped: secrets missing", summary)
        self.assertEqual(self.guild.channels, {})

    def test_dry_run(self):
        self.sc.SCENARIOS = [{"id": "s1", "tier": 1, "title": "hello",
                              "config": {"x": 1},
                              "steps": [{"wait": 1}],
                              "expect": {"checks": [{"max_chars": 2000}]}}]
        for k in self.ENV:
            os.environ.pop(k)
        code, out = self.main("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("treat_as_human", out)
        self.assertIn("s1 (tier 1): hello", out)
        self.assertIn("max_chars", out)
        self.assertEqual(self.guild.channels, {})
        self.assertFalse(os.path.exists(os.path.join(self.out,
                                                     "results.json")))


class DryRunProcessTest(unittest.TestCase):
    """--dry-run as a real process with an empty environment."""

    def test_no_env_no_network(self):
        tmp = tempfile.mkdtemp(prefix="e2e-dry-")
        try:
            stubs = os.path.join(tmp, "stubs")
            os.makedirs(stubs)
            # Used only while scenario.py is not in e2e/ (this branch);
            # once it is, the real loader reads the same file.
            with open(os.path.join(stubs, "scenario.py"), "w") as f:
                f.write("import glob, os, yaml\n"
                        "class ScenarioError(Exception):\n    pass\n"
                        "def load_all(d, tier=None):\n"
                        "    return [yaml.safe_load(open(p)) for p in\n"
                        "            glob.glob(os.path.join(d, '*.yaml'))]\n"
                        "def render(t, ids):\n    return t\n")
            scen = os.path.join(tmp, "scenarios")
            os.makedirs(scen)
            with open(os.path.join(scen, "dry_sample.yaml"), "w") as f:
                f.write("id: dry_sample\ntier: 1\ntitle: sample\n"
                        "steps:\n"
                        "  - send: {as: a, text: '<@SUT> hi', label: q1}\n"
                        "expect:\n  settle: 1\n  checks:\n"
                        "    - sut_reply_count: {eq: 1}\n")
            for name in ("checks", "judge"):
                with open(os.path.join(stubs, f"{name}.py"), "w") as f:
                    f.write("")
            env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": stubs}
            p = subprocess.run([sys.executable, os.path.join(E2E, "run.py"),
                                "--tree", REPO, "--tier", "1", "--dry-run",
                                "--scenario-dir", scen],
                               env=env, capture_output=True, text=True,
                               timeout=60)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            self.assertIn("Plan: tier 1, worker fake, 1 scenario", p.stdout)
            self.assertIn("dry_sample (tier 1): sample", p.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
