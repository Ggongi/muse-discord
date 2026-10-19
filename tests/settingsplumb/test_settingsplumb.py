"""/settings plumbing (settings PR 2), offline: command registration,
interaction routing to settings_ui.handle (a stub here), the 3s path
under a slow main loop, SettingsCtx (overlay writes, reset, undo, live
reload, thread safety, directory), the admin-change digest, the join
notice's Configure button, and a missing settings_ui.

Usage: [MUT_DIR=<tree>] python3 tests/settingsplumb/test_settingsplumb.py"""
import json
import os
import queue
import subprocess
import sys
import threading
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "_lib"))
from settings_harness import (CH, G1, G2, MS, OWNER, U1,  # noqa: E402
                              HTTPErr, load_monitor, quiet, state, tmpdir)

import yaml  # noqa: E402

CFG = f"""version: 1
owners: ["{OWNER}"]
token_from: env:MUSE_TEST_TOKEN
"""
CAT = "800000000000000009"


def overlay(mod):
    with open(os.path.join(mod.tmp, "config.discord.yaml")) as f:
        return yaml.safe_load(f)


def cmd_ix(user=OWNER, guild=None, iid="1"):
    d = {"id": iid, "token": "t", "type": 2, "application_id": "app",
         "data": {"name": "settings", "type": 1}}
    if guild:
        d["guild_id"] = guild
        d["member"] = {"user": {"id": user}, "permissions": "32"}
    else:
        d["user"] = {"id": user}
    return d


def comp_ix(custom_id, user=OWNER, t=3):
    data = {"custom_id": custom_id}
    if t == 3:
        data["component_type"] = 2
    else:
        data["components"] = []
    return {"id": "2", "token": "t", "type": t, "data": data,
            "user": {"id": user}}


class Stub:
    """A stand-in settings_ui: records calls, answers a fixed body."""

    def __init__(self, body=None):
        self.calls = []
        self.body = body

    def handle(self, ix, ctx):
        self.calls.append((ix, ctx, threading.current_thread().name))
        if self.body is not None:
            return self.body
        return {"type": 4 if ix["type"] == 2 else 7,
                "data": {"content": "page", "flags": 64}}


def with_stub(mod, stub=None):
    stub = stub or Stub()
    mod._SETTINGS_UI["mod"] = stub
    return stub


# ---------------------------------------------------------------- register
class FakeCommands:
    """GET/POST/PATCH of /applications/<app>/commands."""

    def __init__(self, mod, existing=None, app="777"):
        self.mod = mod
        self.app = app
        self.cmds = list(existing or [])
        self.log = []
        self.fail = 0
        mod.api_get = self.get
        mod.api_post = self.post
        mod.api_patch = self.patch

    def get(self, path):
        self.log.append(("GET", path))
        if self.fail:
            self.fail -= 1
            raise HTTPErr(500)
        if path == "/applications/@me":
            return {"id": self.app}
        if path == f"/applications/{self.app}/commands":
            return [dict(c) for c in self.cmds]
        raise HTTPErr(404)

    def post(self, path, data):
        self.log.append(("POST", path, data))
        assert path == f"/applications/{self.app}/commands", path
        c = dict(data, id="c1", application_id=self.app, version="1")
        self.cmds.append(c)
        return c

    def patch(self, path, data):
        self.log.append(("PATCH", path, data))
        return dict(data, id=path.rsplit("/", 1)[1])


def discord_copy(mod, **over):
    """The command as Discord's GET returns it: extra fields, its own
    key order, contexts in another order."""
    c = {"id": "c1", "application_id": "777", "version": "5",
         "nsfw": False, "description_localizations": None,
         "dm_permission": True, "integration_types": [0],
         "contexts": [1, 0], "type": 1, "name": "settings",
         "description": mod.SETTINGS_COMMAND["description"],
         "default_member_permissions": None}
    c.update(over)
    return c


def why_copy(mod, **over):
    """The "Why no reply?" message command as Discord's GET returns it."""
    c = {"id": "c2", "application_id": "777", "version": "5",
         "type": 3, "name": "Why no reply?", "description": "",
         "contexts": [1, 0], "integration_types": [0],
         "default_member_permissions": None}
    c.update(over)
    return c


class Registration(unittest.TestCase):
    def setUp(self):
        self.mod = load_monitor(CFG)

    def run_reg(self, status=None):
        with quiet() as out:
            r = self.mod.register_commands(status)
        return r, out.getvalue()

    def test_definition(self):
        c = self.mod.SETTINGS_COMMAND
        self.assertEqual(c["name"], "settings")
        self.assertEqual(c["contexts"], [0, 1])
        self.assertEqual(c["integration_types"], [0])
        self.assertIsNone(c["default_member_permissions"])
        self.assertNotIn("dm_permission", c)  # deprecated for contexts
        self.assertLessEqual(len(c["description"]), 100)

    def test_why_definition(self):
        c = self.mod.WHY_COMMAND
        self.assertEqual(c["type"], 3)              # a message command
        self.assertEqual(c["name"], "Why no reply?")
        self.assertEqual(self.mod.WHY_NAMES[1], "Why no reply")
        self.assertEqual(c["contexts"], [0, 1])
        self.assertEqual(c["integration_types"], [0])
        self.assertNotIn("description", c)          # none on menu commands
        self.assertLessEqual(len(c["name"]), 32)

    def test_created_when_missing(self):
        api = FakeCommands(self.mod)
        r, out = self.run_reg()
        self.assertEqual(r, "created")
        posts = [x for x in api.log if x[0] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0][2], self.mod.SETTINGS_COMMAND)
        self.assertEqual(posts[1][2], self.mod.WHY_COMMAND)
        self.assertIn("/settings command created", out)
        # Once per run: later ticks make no request.
        n = len(api.log)
        self.mod._COMMAND_REG["next_try"] = 0
        self.assertIsNone(self.run_reg()[0])
        self.assertEqual(len(api.log), n)

    def test_noop_when_equal(self):
        api = FakeCommands(self.mod, [discord_copy(self.mod),
                                      why_copy(self.mod),
                                      {"id": "x", "name": "other",
                                       "type": 1}])
        r, out = self.run_reg()
        self.assertEqual(r, "unchanged")
        self.assertEqual([x[0] for x in api.log], ["GET", "GET"])
        self.assertNotIn("/settings command", out)

    def test_patch_when_different(self):
        for over in ({"contexts": [0, 1, 2]}, {"description": "old"},
                     {"integration_types": [0, 1]},
                     {"default_member_permissions": "32"},
                     {"options": [{"type": 3, "name": "x"}]}):
            mod = load_monitor(CFG)
            api = FakeCommands(mod, [discord_copy(mod, **over),
                                     why_copy(mod)])
            with quiet():
                r = mod.register_commands()
            self.assertEqual(r, "updated", over)
            patches = [x for x in api.log if x[0] == "PATCH"]
            self.assertEqual(len(patches), 1)
            self.assertEqual(patches[0][1], "/applications/777/commands/c1")
            self.assertEqual(patches[0][2], mod.SETTINGS_COMMAND)
            self.assertFalse([x for x in api.log if x[0] == "POST"])

    def test_why_name_falls_back_when_refused(self):
        """Discord answers 400 when it does not take "?" in the name: the
        command is registered as "Why no reply" instead, and that name
        counts as registered from then on."""
        api = FakeCommands(self.mod, [discord_copy(self.mod)])
        post = api.post

        def picky(path, data):
            if "?" in data["name"]:
                api.log.append(("POST", path, data))
                raise HTTPErr(400)
            return post(path, data)
        self.mod.api_post = picky
        r, out = self.run_reg()
        self.assertEqual(r, "created")
        self.assertIn("Why no reply", [c["name"] for c in api.cmds])
        self.assertIn("refused the command name", out)
        # A later run finds it under the fallback name: nothing to do.
        api2 = FakeCommands(self.mod, [discord_copy(self.mod),
                                       why_copy(self.mod,
                                                name="Why no reply")])
        self.mod._COMMAND_REG.update(done=False, next_try=0)
        self.assertEqual(self.run_reg()[0], "unchanged")
        self.assertEqual([x[0] for x in api2.log], ["GET"])

    def test_why_patched_when_different(self):
        api = FakeCommands(self.mod, [discord_copy(self.mod),
                                      why_copy(self.mod, contexts=[0])])
        r, _ = self.run_reg()
        self.assertEqual(r, "updated")
        patches = [x for x in api.log if x[0] == "PATCH"]
        self.assertEqual(patches[0][1], "/applications/777/commands/c2")
        self.assertEqual(patches[0][2], self.mod.WHY_COMMAND)

    def test_retry_on_failure_never_fatal(self):
        api = FakeCommands(self.mod)
        api.fail = 2
        r, out = self.run_reg()
        self.assertIsNone(r)
        self.assertIn("registration failed", out)
        self.assertFalse(self.mod._COMMAND_REG["done"])
        # Not before the retry delay.
        n = len(api.log)
        self.assertIsNone(self.run_reg()[0])
        self.assertEqual(len(api.log), n)
        # Next try fails the same way: not logged again.
        self.mod._COMMAND_REG["next_try"] = 0
        r, out = self.run_reg()
        self.assertIsNone(r)
        self.assertEqual(out, "")
        self.mod._COMMAND_REG["next_try"] = 0
        self.assertEqual(self.run_reg()[0], "created")
        self.assertTrue(self.mod._COMMAND_REG["done"])

    def test_app_id_from_gateway_ready(self):
        api = FakeCommands(self.mod, app="555")
        r, _ = self.run_reg({"application": {"id": "555", "flags": 0}})
        self.assertEqual(r, "created")
        self.assertNotIn(("GET", "/applications/@me"), api.log)

    def test_waits_for_identity_and_backoff(self):
        api = FakeCommands(self.mod)
        self.mod.BOT_USER_ID = None
        self.assertIsNone(self.run_reg()[0])
        self.mod.BOT_USER_ID = "1"
        self.mod._rl_until = time.time() + 100
        self.assertIsNone(self.run_reg()[0])
        self.assertEqual(api.log, [])
        self.mod._rl_until = 0
        self.assertEqual(self.run_reg()[0], "created")

    def test_api_patch_request_shape(self):
        seen = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

        def fake_urlopen(req, timeout=None):
            seen.update(method=req.get_method(), url=req.full_url,
                        data=req.data, timeout=timeout)
            return Resp()
        orig = self.mod.urllib.request.urlopen
        self.mod.urllib.request.urlopen = fake_urlopen
        try:
            self.mod.api_patch("/applications/1/commands/2", {"a": 1})
        finally:
            self.mod.urllib.request.urlopen = orig
        self.assertEqual(seen["method"], "PATCH")
        self.assertTrue(seen["url"].endswith("/applications/1/commands/2"))
        self.assertEqual(json.loads(seen["data"]), {"a": 1})


# ----------------------------------------------------------------- routing
class Routing(unittest.TestCase):
    def setUp(self):
        self.mod = load_monitor(CFG)
        self.stub = with_stub(self.mod)
        self.recs = []

    def dispatch(self, ix):
        with quiet() as out:
            r = self.mod.dispatch_interaction(ix, self.recs.append)
        self.out = out.getvalue()
        return r

    def test_command_component_modal_reach_handle(self):
        why = cmd_ix()
        why["data"] = {"name": "Why no reply?", "type": 3,
                       "target_id": "1"}
        why2 = cmd_ix(guild=G1)
        why2["data"] = {"name": "Why no reply", "type": 3,
                        "target_id": "1"}
        for ix in (cmd_ix(), cmd_ix(guild=G1, user=U1), why, why2,
                   comp_ix("mdset:open:server:" + G1),
                   comp_ix("mdset:2:pg:x:b"),
                   comp_ix("mdset:m:abc", t=5)):
            r = self.dispatch(ix)
            got, ctx, _ = self.stub.calls[-1]
            self.assertIs(got, ix)
            self.assertIsInstance(ctx, self.mod.SettingsCtx)
            self.assertEqual(r["type"], 4 if ix["type"] == 2 else 7)

    def test_not_owner_only(self):
        # Permissions are settings_ui's: strangers reach it too.
        self.dispatch(cmd_ix(user=U1))
        self.dispatch(comp_ix("mdset:x", user=U1))
        self.assertEqual(len(self.stub.calls), 2)

    def test_other_prefixes_and_commands_untouched(self):
        r = self.dispatch(comp_ix("mdsetx:1"))
        self.assertEqual(r["data"]["content"],
                         "That control is no longer active.")
        ix = cmd_ix()
        ix["data"]["name"] = "other"
        self.dispatch(ix)
        self.assertEqual(self.stub.calls, [])
        # Autocomplete (4) is not answered.
        self.assertIsNone(self.dispatch({"id": "1", "token": "t",
                                         "type": 4, "data": {}}))

    def test_components_v2_forwarded_verbatim(self):
        """A Components V2 page (flags 64|32768, no content) reaches
        Discord as settings_ui made it, for a new message and an edit."""
        for t, flags in ((4, 64 | 32768), (7, 32768)):
            body = {"type": t, "data": {
                "flags": flags, "allowed_mentions": {"parse": []},
                "components": [{"type": 17, "accent_color": 0x5865F2,
                                "components": [{"type": 10,
                                                "content": "## Page"}]}]}}
            self.stub.body = body
            r = self.dispatch(comp_ix("mdset:2:pg:x:b"))
            self.assertEqual(r, body)
            self.assertNotIn("content", r["data"])

    def test_body_forwarded_verbatim(self):
        body = {"type": 9, "data": {"custom_id": "mdset:m:1",
                                    "title": "Edit", "components": []}}
        self.stub.body = body
        self.assertEqual(self.dispatch(comp_ix("mdset:b")), body)

    def test_bad_body_and_crash_get_ephemeral(self):
        self.stub.body = "nope"
        r = self.dispatch(cmd_ix())
        self.assertEqual(r["type"], 4)
        self.assertEqual(r["data"]["flags"], 64)
        self.assertIn("no callback body", self.out)

        def boom(ix, ctx):
            raise RuntimeError("kaput")
        self.mod._SETTINGS_UI["mod"] = types.SimpleNamespace(handle=boom)
        r = self.dispatch(cmd_ix())
        self.assertEqual(r["data"]["flags"], 64)
        self.assertIn("Something went wrong", r["data"]["content"])
        self.assertIn("kaput", self.out)

    def test_responder_thread_posts_callback(self):
        q = queue.Queue()
        sent = []
        self.mod.interaction_callback = lambda ix, resp: sent.append(
            (ix["id"], resp))
        t = threading.Thread(target=self.mod._interaction_thread_main,
                             args=(q, self.recs.append), daemon=True,
                             name="interactions")
        t.start()
        q.put(cmd_ix(iid="41"))
        q.put(comp_ix("mdset:z", t=5))
        deadline = time.time() + 5
        while len(sent) < 2 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual([s[0] for s in sent], ["41", "2"])
        self.assertEqual(sent[0][1]["type"], 4)
        self.assertEqual(sent[1][1]["type"], 7)
        self.assertEqual({c[2] for c in self.stub.calls}, {"interactions"})


class MissingUI(unittest.TestCase):
    def test_ephemeral_and_logged_once(self):
        mod = load_monitor(CFG)
        mod.BASE_DIR = tmpdir("noui_")
        saved = list(sys.path)
        sys.modules.pop("settings_ui", None)
        sys.path[:] = [p for p in sys.path
                       if not os.path.isfile(os.path.join(
                           p or ".", "settings_ui.py"))]
        try:
            with quiet() as out:
                r1 = mod.dispatch_interaction(cmd_ix(), lambda r: None)
                r2 = mod.dispatch_interaction(comp_ix("mdset:x"),
                                              lambda r: None)
        finally:
            sys.path[:] = saved
        for r in (r1, r2):
            self.assertEqual(r["type"], 4)
            self.assertEqual(r["data"]["flags"], 64)
            self.assertEqual(r["data"]["content"], "Settings UI unavailable.")
        self.assertEqual(out.getvalue().count("/settings UI unavailable ("), 1)

    def test_found_next_to_monitor(self):
        mod = load_monitor(CFG)
        d = tmpdir("ui_")
        with open(os.path.join(d, "settings_ui.py"), "w") as f:
            f.write("PREFIX = 'mdset'\n"
                    "def handle(interaction, ctx):\n"
                    "    return {'type': 4, 'data': {'content': "
                    "'hi ' + ctx.bot_id, 'flags': 64}}\n")
        mod.BASE_DIR = d
        r = mod.dispatch_interaction(cmd_ix(), lambda r: None)
        self.assertEqual(r["data"]["content"], "hi " + mod.BOT_ID)


# ------------------------------------------------------------- main loop
class MainLoop(unittest.TestCase):
    def test_settings_ack_not_behind_slow_tick(self):
        p = subprocess.run([sys.executable,
                            os.path.join(HERE, "main_scenario.py")],
                           capture_output=True, text=True, timeout=60,
                           env=os.environ.copy())
        try:
            r = json.loads(p.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise AssertionError(p.stdout + p.stderr)
        acks = {a["id"]: a for a in r["acks"]}
        self.assertEqual(sorted(acks), ["900", "901", "902"], r["out"])
        for a in acks.values():
            self.assertLess(a["after"], 1.0, r)
        self.assertEqual(acks["900"]["resp"]["type"], 4)
        self.assertEqual(acks["901"]["resp"]["type"], 7)
        self.assertEqual(acks["902"]["resp"]["type"], 7)
        self.assertEqual(acks["900"]["resp"]["data"]["seen_guilds"], ["G1"])
        self.assertEqual(set(r["handled_on"]), {"interactions"})


# ------------------------------------------------------------------- ctx
class Ctx(unittest.TestCase):
    def setUp(self):
        self.mod = load_monitor(CFG)
        with quiet():
            self.mod.settings()

    def ctx(self):
        with quiet():
            return self.mod.SettingsCtx()

    def test_basics(self):
        c = self.ctx()
        self.assertEqual(c.owners, {OWNER})
        self.assertEqual(c.bot_id, self.mod.BOT_ID)
        self.assertLess(abs(c.now - time.time()), 5)
        self.assertIsInstance(c.settings, MS.Settings)
        with quiet() as out:
            c.log("hello")
        self.assertIn("/settings: hello", out.getvalue())

    def test_write_visible_at_once_and_to_main_loop(self):
        c = self.ctx()
        key = f'servers."{G1}".who.replies'
        with quiet():
            c.write(key, "off", OWNER)
        e = overlay(self.mod)["values"][key]
        self.assertEqual(e["value"], "off")
        self.assertEqual(e["by"], OWNER)
        sc = MS.Scope.server(G1, "Lounge", CH)
        self.assertEqual(c.settings.get("who.replies", sc), "off")
        with quiet():
            self.assertEqual(self.mod.settings().get("who.replies", sc),
                             "off")

    def test_invalid_write_raises(self):
        c = self.ctx()
        for key, val in (("everywhere.style.length", "huge"),
                         ("owners", [U1]),
                         ("dms.who.bot_exchange_limit", 3)):
            with self.assertRaises(MS.SettingsError):
                c.write(key, val, OWNER)

    def test_reset_writes_inherit(self):
        c = self.ctx()
        with quiet():
            c.write("everywhere.style.length", "brief", OWNER)
            c.reset("everywhere.style.length", OWNER)
        self.assertEqual(
            overlay(self.mod)["values"]["everywhere.style.length"]["value"],
            "inherit")
        self.assertEqual(c.settings.get("style.length",
                                        MS.Scope.everywhere()), "normal")

    def test_undo_delegates_to_muse_settings_under_lock(self):
        c = self.ctx()
        seen = []

        def fake_undo(config_path, prefix, by, plugins_dir=None):
            seen.append((config_path, prefix, by, plugins_dir,
                         self.mod._OVERLAY_LOCK._is_owned()))
            return f"{prefix}.style.length"
        had = getattr(self.mod.MS, "undo", None)
        self.mod.MS.undo = fake_undo
        try:
            with quiet():
                r = c.undo(f'servers."{G1}"', OWNER)
        finally:
            if had is None:
                del self.mod.MS.undo
            else:
                self.mod.MS.undo = had
        self.assertEqual(r, f'servers."{G1}".style.length')
        self.assertEqual(seen, [(self.mod._SETTINGS.config_path,
                                 f'servers."{G1}"', OWNER,
                                 self.mod.PLUGINS_DIR, True)])

    def test_undo_without_muse_settings_undo(self):
        had = getattr(self.mod.MS, "undo", None)
        if had is not None:
            del self.mod.MS.undo
        try:
            with self.assertRaises(MS.SettingsError):
                self.ctx().undo("everywhere", OWNER)
        finally:
            if had is not None:
                self.mod.MS.undo = had

    @unittest.skipUnless(hasattr(MS, "undo"),
                         "muse_settings.undo comes with settings_ui")
    def test_undo_real(self):
        c = self.ctx()
        srv = f'servers."{G1}"'
        sc = MS.Scope.server(G1, "Lounge")
        with quiet():
            c.write(f"{srv}.style.length", "brief", OWNER)
            c.write(f"{srv}.style.length", "detailed", OWNER)
            c.write(f'{srv}.channels."{CH}".replies', "off", OWNER)
            self.assertEqual(c.undo(srv, OWNER), f"{srv}.style.length")
            self.assertEqual(c.settings.get("style.length", sc), "brief")
            self.assertEqual(c.undo(srv, OWNER), f"{srv}.style.length")
            self.assertEqual(c.settings.get("style.length", sc), "normal")
            self.assertIsNone(c.undo(srv, OWNER))
            self.assertIsNone(c.undo("dms", OWNER))

    def test_reload_is_the_monitors_watcher(self):
        c = self.ctx()
        with quiet():
            MS.set_value(self.mod._SETTINGS.config_path,
                         "everywhere.style.length", "brief", OWNER)
            st = c.reload()
        self.assertIs(st, self.mod._SETTINGS.current)
        self.assertEqual(st.get("style.length", MS.Scope.everywhere()),
                         "brief")

    def test_reload_hooks_run_on_main_thread_only(self):
        ran = []
        self.mod._on_settings_changed = \
            lambda prev, cur: ran.append(threading.current_thread().name)
        c = self.ctx()

        def worker():
            with quiet():
                c.write("everywhere.style.length", "brief", OWNER)
        t = threading.Thread(target=worker, name="interactions")
        t.start()
        t.join()
        self.assertEqual(ran, [])
        with quiet():
            self.mod.settings()
        self.assertEqual(ran, ["MainThread"])
        with quiet():
            self.mod.settings()
        self.assertEqual(ran, ["MainThread"])

    def test_concurrent_writes_and_main_loop_reads(self):
        c = self.ctx()
        errors = []
        n = 15
        sc = MS.Scope.server(G1, "Lounge", CH)

        def writer(key, values):
            try:
                for i in range(n):
                    c.write(key, values[i % 2], OWNER)
            except Exception as e:  # pragma: no cover
                errors.append(e)

        def reader(stop):
            try:
                while not stop.is_set():
                    st = self.mod.settings()
                    st.resolve(sc)
                    self.mod.retire_overlay_changes()
            except Exception as e:  # pragma: no cover
                errors.append(e)
        stop = threading.Event()
        with quiet():
            threads = [
                threading.Thread(target=writer, args=(
                    "everywhere.style.length", ("brief", "detailed"))),
                threading.Thread(target=writer, args=(
                    f'servers."{G1}".who.replies', ("off", "conversational"))),
                threading.Thread(target=writer, args=(
                    "dms.who.dms_from", ("anyone", "owners")))]
            rd = threading.Thread(target=reader, args=(stop,))
            rd.start()
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            stop.set()
            rd.join()
        self.assertEqual(errors, [])
        doc = overlay(self.mod)
        # No write lost: every change is in history.
        self.assertEqual(len(doc["history"]), 3 * n)
        self.assertEqual(doc["values"]["everywhere.style.length"]["value"],
                         "detailed" if n % 2 == 0 else "brief")
        with quiet():
            self.assertEqual(self.mod.settings().get(
                "dms.who.dms_from".split(".", 1)[1], MS.Scope.dm()),
                "owners" if n % 2 == 0 else "anyone")

    def test_fresh_write_not_retired_by_stale_conflict(self):
        # The file moved (conflict pending); /settings writes the key
        # again before the main loop commits the retirement.
        mod = load_monitor(CFG + "everywhere:\n  style:\n    length: brief\n")
        with open(os.path.join(mod.tmp, "config.discord.yaml"), "w") as f:
            yaml.safe_dump({"version": 1, "values": {
                "everywhere.style.length": {
                    "value": "detailed", "base": "normal", "by": OWNER,
                    "at": "2026-10-01T00:00:00Z"}}, "history": []}, f)
        with quiet():
            stale = mod.settings()
        self.assertTrue(stale.retire)
        with quiet():
            mod.SettingsCtx().write("everywhere.style.length", "detailed",
                                    OWNER)
            n = mod.retire_overlay_changes()
        self.assertEqual(n, 0)
        e = overlay(mod)["values"]["everywhere.style.length"]
        self.assertEqual((e["value"], e["base"]), ("detailed", "brief"))
        self.assertEqual(mod.api.dm_posts(), [])


# ------------------------------------------------------------- directory
class Directory(unittest.TestCase):
    def test_discovery_feeds_guilds_and_channels(self):
        mod = load_monitor(CFG, channels=False)
        mod.BOT_ID = "999000000000000001"
        routes = {
            "/users/@me/guilds": [{"id": G1, "name": "Lounge"},
                                  {"id": G2, "name": "Atrium"}],
            f"/guilds/{G1}?with_counts=true": {
                "id": G1, "roles": [], "approximate_member_count": 1240,
                "system_channel_id": CH},
            f"/guilds/{G2}?with_counts=true": {"id": G2, "roles": []},
            f"/guilds/{G1}/channels": [
                {"id": CAT, "type": 4, "name": "Text", "position": 0},
                {"id": CH, "type": 0, "name": "general",
                 "parent_id": CAT, "position": 1}],
            f"/guilds/{G2}/channels": [],
            f"/guilds/{G1}/threads/active": {"threads": []},
            f"/guilds/{G2}/threads/active": {"threads": []},
        }

        def get(path):
            if path in routes:
                return routes[path]
            if path.startswith("/channels/"):
                return []
            raise HTTPErr(404)
        mod.api_get = get
        with quiet():
            mod.CHANNELS, mod.GUILDS = mod.discover_channels()
            mod.publish_directory()
            ctx = mod.SettingsCtx()
        self.assertEqual(ctx.guilds(), [
            {"id": G2, "name": "Atrium", "member_count": None,
             "system_channel_id": None},
            {"id": G1, "name": "Lounge", "member_count": 1240,
             "system_channel_id": CH}])
        chans = ctx.channels(G1)
        self.assertEqual([(c["id"], c["type"], c["parent_id"])
                          for c in chans],
                         [(CAT, 4, None), (CH, 0, CAT)])
        # The bot reads #general; its send permission is unknown here (no
        # roles), and a category is neither readable nor not.
        self.assertTrue(chans[1]["readable"])
        self.assertIsNone(chans[1]["can_send"])
        self.assertNotIn("readable", chans[0])
        self.assertEqual(ctx.channels(G2), [])
        self.assertEqual(ctx.channels("123"), [])
        # GUILD_CREATE's member_count refreshes the cache.
        st = state(mod)
        with quiet():
            mod.on_guild_event(st, {"op": "create", "id": G2,
                                    "member_count": 7})
        self.assertEqual(ctx.guilds()[0]["member_count"], 7)
        # A failed channel list keeps the previous one.
        routes.pop(f"/guilds/{G1}/channels")
        with quiet():
            mod.CHANNELS, mod.GUILDS = mod.discover_channels(mod.CHANNELS)
            mod.publish_directory()
        self.assertEqual(len(ctx.channels(G1)), 2)
        # Callers get copies.
        ctx.channels(G1)[0]["name"] = "x"
        ctx.guilds()[0]["name"] = "x"
        self.assertEqual(ctx.channels(G1)[0]["name"], "Text")
        self.assertEqual(ctx.guilds()[0]["name"], "Atrium")


# ------------------------------------------------- data for settings_ui
class UIData(unittest.TestCase):
    """What the redesigned /settings reads through SettingsCtx: the
    ledger's outcomes (with author ids and reason codes), names, the
    stash, the directory's system channel and send flags."""

    def setUp(self):
        self.mod = load_monitor(CFG)
        with quiet():
            self.mod.publish_directory()
            self.ctx = self.mod.SettingsCtx()

    def test_ledger_keeps_author_id_code_and_detail(self):
        m = self.mod
        msg = {"id": "1001", "channel_id": CH,
               "author": {"id": U1, "username": "sam"}}
        with quiet():
            m.note_refused(msg, "#general", "who.replies is off here")
        e = m._ledger_load()["entries"]["1001"]
        self.assertEqual((e["author_id"], e["author"], e["code"]),
                         (U1, "sam", "replies-off"))
        # A later outcome keeps the author id and drops stale detail.
        m.ledger_record_many(["1001"], "skipped", "daily cap reached", CH,
                             "sam", detail={"key": "limits.per_day",
                                            "counter": "everywhere",
                                            "cap": 3})
        m.ledger_record("1001", "replied", "sweep sync", CH, "sam")
        e = m._ledger_load()["entries"]["1001"]
        self.assertEqual((e["author_id"], e["code"]), (U1, "replied"))
        self.assertNotIn("detail", e)
        self.assertEqual(self.ctx.display_name(U1), "sam")
        for status, reason, code in (
                ("skipped", "settings: who.replies is off for DMs",
                 "dm-off"),
                ("skipped", "settings: who.bots: ignore", "bots-ignore"),
                ("skipped", "settings: who.dms_from is owners",
                 "dm-not-allowed"),
                ("skipped", "settings: not allowed by who.allow / "
                 "who.ignore", "not-allowed"),
                ("skipped", "bot exchange limit (4) reached",
                 "bot-exchange"),
                ("skipped", "suspicious, ignored: x", "suspicious-ignored"),
                ("skipped", "predates bot join", "predates-join"),
                ("skipped", "403 missing access", "no-send"),
                ("skipped", "enqueue failed 3x", "internal"),
                ("enqueue_failed", "sweep enqueue failed", "internal"),
                ("skipped", "no reply (worker's call)", "worker-skipped"),
                ("skipped", "send failed: boom", "send-failed"),
                ("quarantined", "ignore previous", "quarantined"),
                ("skipped", "something new", "other")):
            self.assertEqual(m.ledger_code(status, reason), code, reason)

    def test_rate_block_says_which_cap(self):
        m = load_monitor(CFG + f'servers:\n  "{G1}":\n    limits:\n'
                         f'      per_person_per_day: 1\n')
        st = state(m)
        scope = MS.Scope.server(G1)
        with quiet():
            self.assertIsNone(m.rate_block(st, U1, CH, scope))
            m.rate_record(st, U1, scope)
            got = m.rate_block(st, U1, CH, scope)
            self.assertFalse(m.rate_allows(st, U1, CH, scope))
        self.assertEqual(got, {"key": "limits.per_person_per_day",
                               "counter": f"server:{G1}", "cap": 1})
        self.assertIsNone(m.rate_block(st, OWNER, CH, scope))

    def test_recent_outcomes_by_place(self):
        m = self.mod
        thread = "703000000000000009"
        m.CHANNELS[thread] = {"name": "general > t", "guild_id": G1,
                              "thread": True, "parent_id": CH}
        with quiet():
            m.publish_directory()
        m.ledger_record("2001", "skipped", "settings: who.replies is off "
                        "here", CH, "sam", author_id=U1)
        m.ledger_record("2002", "skipped", "daily cap reached", thread,
                        "kai", author_id="222000000000000009",
                        detail={"key": "limits.per_day",
                                "counter": f"server:{G1}", "cap": 5})
        m.ledger_record("2003", "skipped", "settings: who.dms_from is "
                        "owners", "400000000000000077", "ann",
                        author_id="222000000000000008")
        m.ledger_record("2004", "replied", "", "704000000000000001", "x")
        ch = MS.Place.channel(G1, CH)
        got = self.ctx.recent_outcomes(ch, 10)
        self.assertEqual([o["message_id"] for o in got], ["2002", "2001"])
        self.assertEqual(got[0]["parent_id"], CH)
        self.assertEqual(got[0]["detail"]["cap"], 5)
        self.assertEqual(got[0]["code"], "cap")
        self.assertEqual(got[1]["guild_id"], G1)
        self.assertEqual(
            [o["message_id"] for o in self.ctx.recent_outcomes(
                MS.Place.server(G1), 10)], ["2002", "2001"])
        self.assertEqual(
            [o["message_id"] for o in self.ctx.recent_outcomes(
                MS.Place.dms(), 10) if o["status"] == "skipped"], ["2003"])
        self.assertEqual(self.ctx.recent_outcomes(ch, 1)[0]["message_id"],
                         "2002")
        self.assertEqual(self.ctx.outcome("2001")["author_id"], U1)
        self.assertIsNone(self.ctx.outcome("9999"))

    def test_stash_single_use_and_bound_to_its_user(self):
        c = self.ctx
        tok = c.stash_put(OWNER, {"kind": "apply", "n": 1})
        self.assertEqual(len(tok), 10)
        self.assertNotIn(":", tok)
        with self.assertRaises(PermissionError):
            c.stash_take(U1, tok)
        # another interaction's Ctx sees the same stash
        with quiet():
            other = self.mod.SettingsCtx()
        self.assertEqual(other.stash_take(OWNER, tok),
                         {"kind": "apply", "n": 1})
        self.assertIsNone(c.stash_take(OWNER, tok))     # used
        tok = c.stash_put(OWNER, {"x": 1}, ttl=-1)
        self.assertIsNone(c.stash_take(OWNER, tok))     # expired
        self.assertIsNone(c.stash_take(OWNER, "nope"))

    def test_directory_send_flags(self):
        m = self.mod
        m.GUILDS = {G1: {"name": "Lounge", "system_channel_id": None}}
        m._GUILD_CHANNELS[G1] = [
            {"id": CH, "name": "general", "type": 0, "parent_id": None},
            {"id": "702000000000000001", "name": "random", "type": 0,
             "parent_id": None},
            {"id": "709000000000000001", "name": "secret", "type": 0,
             "parent_id": None}]
        m.CHANNELS = {CH: {"name": "general", "guild_id": G1,
                           "can_send": True},
                      "702000000000000001": {"name": "random",
                                             "guild_id": G1,
                                             "can_send": False}}
        with quiet():
            m.publish_directory()
        g = self.ctx.guilds()[0]
        self.assertIn("system_channel_id", g)
        self.assertIsNone(g["system_channel_id"])
        got = {c["name"]: (c["readable"], c["can_send"])
               for c in self.ctx.channels(G1)}
        self.assertEqual(got, {"general": (True, True),
                               "random": (True, False),
                               "secret": (False, None)})


# ---------------------------------------------------------- admin digest
class AdminDigest(unittest.TestCase):
    def setUp(self):
        self.mod = load_monitor(CFG)
        with quiet():
            self.mod.publish_directory()
            self.ctx = self.mod.SettingsCtx()

    def age(self, secs):
        for e in self.mod._ADMIN_CHANGES.values():
            e["first"] -= secs

    def test_batched_per_guild_per_minute(self):
        c = self.ctx
        c.notify_owners_admin_change(G1, U1, "who.replies",
                                     "conversational", "off")
        c.notify_owners_admin_change(G1, U1, f'channels."{CH}".replies',
                                     "conversational", "off")
        c.notify_owners_admin_change(G1, "222000000000000009", "who.ignore",
                                     [], [U1])
        c.notify_owners_admin_change(G2, U1, "plugins.welcome.enabled",
                                     True, False)
        with quiet():
            self.assertEqual(self.mod.flush_admin_digests(), 0)
        self.assertEqual(self.mod.api.dm_posts(), [])
        self.age(61)
        with quiet():
            self.assertEqual(self.mod.flush_admin_digests(), 2)
        dms = self.mod.api.dm_posts()
        self.assertEqual(len(dms), 2)
        g1 = next(d for d in dms if "Lounge" in d["content"])
        self.assertEqual(g1["content"].count("\n- "), 3)
        self.assertIn(f"<@{U1}> set `who.replies` to off (was "
                      f"conversational)", g1["content"])
        self.assertIn(f"to {U1} (was (empty))", g1["content"])
        self.assertEqual(g1["allowed_mentions"], {"parse": []})
        with quiet():
            self.assertEqual(self.mod.flush_admin_digests(force=True), 0)

    def test_change_during_window_joins_it(self):
        c = self.ctx
        c.notify_owners_admin_change(G1, U1, "who.replies", "a", "off")
        self.age(30)
        c.notify_owners_admin_change(G1, U1, "who.replies", "off", "a")
        with quiet():
            self.assertEqual(self.mod.flush_admin_digests(), 0)
        self.age(31)
        with quiet():
            self.assertEqual(self.mod.flush_admin_digests(), 1)
        self.assertEqual(len(self.mod.api.dm_posts()), 1)

    def test_undelivered_kept_and_retried(self):
        self.ctx.notify_owners_admin_change(G1, U1, "who.replies", "a", "b")
        self.age(61)
        self.mod.dm_owner = lambda uid, payload: False
        with quiet():
            self.assertEqual(self.mod.flush_admin_digests(), 0)
        self.assertIn(G1, self.mod._ADMIN_CHANGES)
        self.mod.dm_owner = lambda uid, payload: True
        with quiet():
            self.assertEqual(self.mod.flush_admin_digests(), 1)
        self.assertEqual(self.mod._ADMIN_CHANGES, {})

    def test_long_digest_capped(self):
        for i in range(80):
            self.ctx.notify_owners_admin_change(
                G1, U1, "who.ignore", [], [str(10 ** 17 + i)] * 3)
        text = self.mod.admin_digest_text(
            G1, self.mod._ADMIN_CHANGES[G1]["changes"])
        self.assertLessEqual(len(text), 1990)
        self.assertRegex(text, r"- and \d+ more")


# ---------------------------------------------------------- join notice
class JoinNotice(unittest.TestCase):
    def test_configure_button(self):
        mod = load_monitor(CFG)
        st = state(mod)
        st["known_guilds"] = {G1: "Lounge"}
        with quiet():
            mod.sync_guilds(st, mod.GUILDS, mod.CHANNELS)
        dms = mod.api.dm_posts()
        self.assertEqual(len(dms), 1)
        self.assertIn("I just joined **Other**", dms[0]["content"])
        self.assertIn("**Configure**", dms[0]["content"])
        rows = dms[0]["components"]
        self.assertEqual(len(rows), 1)
        btn = rows[0]["components"][0]
        self.assertEqual(btn["type"], 2)
        self.assertEqual(btn["label"], "Configure")
        self.assertEqual(btn["custom_id"], f"mdset:open:server:{G2}")
        self.assertLessEqual(len(btn["custom_id"]), 100)
        # The click is routed to settings_ui.
        stub = with_stub(mod)
        with quiet():
            mod.dispatch_interaction(comp_ix(btn["custom_id"]),
                                     lambda r: None)
        self.assertEqual(stub.calls[0][0]["data"]["custom_id"],
                         f"mdset:open:server:{G2}")

    def test_plain_notices_have_no_components(self):
        mod = load_monitor(CFG)
        with quiet():
            mod.notify_owners("hello")
        self.assertNotIn("components", mod.api.dm_posts()[0])


if __name__ == "__main__":
    unittest.main(verbosity=1)
