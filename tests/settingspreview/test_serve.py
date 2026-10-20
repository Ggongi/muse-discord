"""dev/settings-preview: the local /settings design preview. Starts
serve.py on a free port and drives it over HTTP: the page is served,
/settings for an owner, a server admin, a member and in DMs, a select,
a modal and its submit, Undo, "Why no reply?", the join notice's
Configure button, reset, local-only requests, and edits to
settings_ui.py showing up without a restart (and an import error
reported, not fatal).
MUT_DIR = the package tree under test."""
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
from common import MUT_DIR, REPO, scratch  # noqa: E402

SERVE = os.path.join(REPO, "dev", "settings-preview", "serve.py")
G1 = "60000000000000000001"         # Muse Lounge
MUSECHAT = "70000000000000000003"
HERE = {"guild": G1, "channel": MUSECHAT}
DM = {"guild": None}


class Server:
    def __init__(self, mut=MUT_DIR):
        env = dict(os.environ, MUT_DIR=mut, PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            [sys.executable, SERVE, "--port", "0"], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, env=env)
        line = self.proc.stdout.readline()
        if "http://" not in line:
            self.stop()
            raise RuntimeError(f"serve.py did not start: {line!r}")
        self.url = line.split("http://", 1)[1].split()[0].rstrip("/")
        self.url = "http://" + self.url

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.proc.stdout.close()

    def get(self, path, headers=None):
        req = urllib.request.Request(self.url + path, headers=headers or {})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.headers.get("Content-Type"), r.read()

    def post(self, path, body):
        req = urllib.request.Request(
            self.url + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    def interact(self, viewer="owner", where=HERE, **kw):
        return self.post("/api/interact", dict(kw, viewer=viewer,
                                               where=where))


def walk(comps):
    for c in comps or []:
        yield c
        yield from walk(c.get("components"))
        for k in ("accessory", "component"):
            if isinstance(c.get(k), dict):
                yield from walk([c[k]])


def texts(res):
    return "\n".join(c["content"] for c in walk(
        res["response"]["data"].get("components")) if c.get("type") == 10)


def control(res, prefix=None, label=None):
    for c in walk(res["response"]["data"].get("components")):
        cid = c.get("custom_id") or ""
        if prefix and not cid.startswith(prefix):
            continue
        if label and c.get("label") != label:
            continue
        if cid:
            return c
    return None


def as_message(res, mid="90000000000000000001"):
    data = res["response"]["data"]
    return {"id": mid, "flags": data.get("flags", 0),
            "components": data["components"]}


class Preview(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s = Server()

    @classmethod
    def tearDownClass(cls):
        cls.s.stop()

    def setUp(self):
        self.assertTrue(self.s.post("/api/reset", {})["ok"])

    def test_page_served(self):
        status, ctype, body = self.s.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        page = body.decode()
        self.assertIn("<title>Settings Preview</title>", page)
        for needle in ("function renderComponent", "function markdown",
                       "/api/interact", "Only you can see this"):
            self.assertIn(needle, page)

    def test_world(self):
        _, ctype, body = self.s.get("/api/world")
        self.assertIn("application/json", ctype)
        w = json.loads(body)
        names = [g["name"] for g in w["guilds"] if not g["filler"]]
        self.assertEqual(sorted(names),
                         ["Muse Lounge", "Pixel Plaza", "Study Group"])
        self.assertEqual(set(w["viewers"]), {"owner", "admin", "member"})
        self.assertTrue(w["messages"])
        self.assertEqual(w["limits"]["components"], 40)
        self.assertEqual(w["limits"]["text"], 3600)
        st = json.loads(self.s.get("/api/status")[2])
        self.assertIn("settings_ui.py", st["files"])
        self.assertIsNone(st["error"])

    def test_owner_command_select_undo(self):
        r = self.s.interact(kind="command")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["response"]["type"], 4)
        self.assertEqual(r["response"]["data"]["flags"], 64 | 32768)
        self.assertTrue(texts(r).startswith("## #muse-chat"))
        self.assertEqual(r["checks"], [])
        self.assertLessEqual(r["stats"]["components"], 40)
        self.assertLessEqual(r["stats"]["text"], 3600)
        self.assertEqual(r["interaction"]["guild_id"], G1)
        sel = control(r, "mdset:2:set:")
        self.assertEqual(sel["type"], 3)
        r2 = self.s.interact(kind="component", custom_id=sel["custom_id"],
                             component_type=3, values=["off"],
                             message=as_message(r))
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(r2["response"]["type"], 7)
        self.assertIn("Done:", texts(r2))
        undo = control(r2, label="Undo")
        r3 = self.s.interact(kind="component", custom_id=undo["custom_id"],
                             component_type=2, message=as_message(r2))
        self.assertTrue(r3["ok"], r3)
        self.assertIn(r3["response"]["type"], (4, 7))
        if "Check before undoing" in texts(r3):   # a risky undo
            ok = control(r3, "mdset:2:ok:")
            r3 = self.s.interact(kind="component", custom_id=ok["custom_id"],
                                 component_type=2, message=as_message(r3))
            self.assertTrue(r3["ok"], r3)
        self.assertIn("Undone", texts(r3))

    def test_modal_and_submit(self):
        r = self.s.interact(kind="command")
        edit = control(r, "mdset:2:mod:", label="Edit")
        m = self.s.interact(kind="component", custom_id=edit["custom_id"],
                            component_type=2, message=as_message(r))
        self.assertTrue(m["ok"], m)
        self.assertEqual(m["response"]["type"], 9)
        modal = m["response"]["data"]
        self.assertEqual(m["checks"], [])
        box = next(c for c in walk(modal["components"])
                   if c.get("type") == 4)
        comps = [{"type": 18, "component": {
            "type": 4, "custom_id": box["custom_id"],
            "value": "Keep it short."}}]
        r2 = self.s.interact(kind="submit", custom_id=modal["custom_id"],
                             components=comps, message=as_message(r))
        self.assertTrue(r2["ok"], r2)
        self.assertEqual(r2["response"]["type"], 7)
        self.assertIn("Keep it short.", texts(r2))

    def test_admin_member_dm(self):
        r = self.s.interact("admin", kind="command")
        self.assertTrue(r["ok"], r)
        self.assertTrue(texts(r).startswith("## Quiet the bot in Muse "
                                            "Lounge"))
        self.assertEqual(r["interaction"]["member"]["permissions"], "32")
        r = self.s.interact("member", kind="command")
        self.assertIn("Only the bot's owners", texts(r))
        r = self.s.interact("owner", DM, kind="command")
        self.assertTrue(texts(r).startswith("## DMs"))
        self.assertNotIn("guild_id", r["interaction"])
        w = json.loads(self.s.get("/api/world")[2])
        btn = w["dm_history"][0]["components"][0]["components"][0]
        r = self.s.interact("owner", DM, kind="component",
                            custom_id=btn["custom_id"], component_type=2,
                            message={"id": w["dm_history"][0]["id"],
                                     "flags": 0,
                                     "components": w["dm_history"][0][
                                         "components"]})
        self.assertEqual(r["response"]["type"], 4)
        self.assertTrue(texts(r).startswith("## Study Group"))

    def test_why_no_reply(self):
        w = json.loads(self.s.get("/api/world")[2])
        m = next(m for m in w["messages"] if m["code"] == "not-allowed"
                 and m["channel_id"] == MUSECHAT)
        r = self.s.interact(kind="why", target=m["id"])
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["response"]["type"], 4)
        self.assertTrue(texts(r).startswith("## Why no reply?"))
        self.assertEqual(r["checks"], [])

    def test_user_select_resolved(self):
        r = self.s.interact("admin", kind="command")
        sel = next(c for c in walk(r["response"]["data"]["components"])
                   if c.get("type") in (5, 7))
        kai = "44400000000000000002"
        r2 = self.s.interact("admin", kind="component",
                             custom_id=sel["custom_id"],
                             component_type=sel["type"], values=[kai],
                             message=as_message(r))
        self.assertTrue(r2["ok"], r2)
        res = r2["interaction"]["data"]["resolved"]
        self.assertEqual(res["users"][kai]["global_name"], "Kai")
        self.assertIn("Kai", texts(r2))

    def test_local_only_and_errors(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.s.get("/api/world", {"Host": "evil.example"})
        self.assertEqual(e.exception.code, 403)
        e.exception.close()
        r = self.s.interact(kind="nonsense")
        self.assertFalse(r["ok"])
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.s.get("/nope")
        self.assertEqual(e.exception.code, 404)
        e.exception.close()


class LiveReload(unittest.TestCase):
    """An edit to settings_ui.py shows on the next request; a broken
    edit is reported, and fixing it recovers (no restart)."""

    def setUp(self):
        self.tree = scratch("muse-preview-tree-")
        for f in ("settings_ui.py", "muse_settings.py"):
            shutil.copy(os.path.join(MUT_DIR, f), self.tree)
        shutil.copytree(os.path.join(MUT_DIR, "plugins"),
                        os.path.join(self.tree, "plugins"))
        self.ui = os.path.join(self.tree, "settings_ui.py")
        with open(self.ui) as f:
            self.orig = f.read()
        self.s = Server(self.tree)

    def tearDown(self):
        self.s.stop()

    def write(self, text):
        with open(self.ui, "w") as f:
            f.write(text)
        later = time.time() + 5
        os.utime(self.ui, (later, later))

    def test_edit_shows_up(self):
        r = self.s.interact(kind="command")
        self.assertTrue(control(r, label="Other servers"))
        self.assertIn('"Other servers"', self.orig)
        self.write(self.orig.replace('"Other servers"',
                                     '"Every other server"'))
        r = self.s.interact(kind="command")
        self.assertTrue(r["ok"], r)
        self.assertTrue(control(r, label="Every other server"))
        self.assertEqual(r["reloads"], 1)
        self.write(self.orig + "\ndef broken(:\n")
        r = self.s.interact(kind="command")
        self.assertFalse(r["ok"])
        self.assertIn("SyntaxError", r["trace"])
        st = json.loads(self.s.get("/api/status")[2])
        self.assertIn("SyntaxError", st["error"])
        self.write(self.orig)
        r = self.s.interact(kind="command")
        self.assertTrue(r["ok"], r)
        self.assertTrue(control(r, label="Other servers"))


if __name__ == "__main__":
    unittest.main()
