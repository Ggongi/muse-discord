"""dev/settings-preview: the local /settings design preview. Starts
serve.py on a free port and drives it over HTTP: the page is served,
/settings and a click are answered, only local requests are served, and
edits to settings_ui.py show up without a restart (a broken one is
reported, not fatal). Nothing here depends on the pages' words.
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

    def test_serves_and_answers(self):
        """The page is served; /settings and a click on its first select
        are answered as settings_ui would in Discord."""
        self.assertTrue(self.s.post("/api/reset", {})["ok"])
        status, ctype, _ = self.s.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", ctype)
        r = self.s.interact(kind="command")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["response"]["type"], 4)
        self.assertEqual(r["checks"], [])
        sel = next(c for c in walk(r["response"]["data"]["components"])
                   if c.get("type") == 3 and c.get("options"))
        r2 = self.s.interact(kind="component", custom_id=sel["custom_id"],
                             component_type=3,
                             values=[sel["options"][-1]["value"]],
                             message=as_message(r))
        self.assertTrue(r2["ok"], r2)
        self.assertIn(r2["response"]["type"], (4, 7, 9))

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


MARKER = "preview-live-reload-marker"
OVERRIDE = f"""

_preview_test_handle = handle


def handle(interaction, ctx):
    r = _preview_test_handle(interaction, ctx)
    r["data"]["components"].append({{"type": 10, "content": "{MARKER}"}})
    return r
"""


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

    def marked(self, r):
        return any(c.get("content") == MARKER for c in walk(
            r["response"]["data"].get("components")))

    def test_edit_shows_up(self):
        r = self.s.interact(kind="command")
        self.assertTrue(r["ok"], r)
        self.assertFalse(self.marked(r))
        self.write(self.orig + OVERRIDE)
        r = self.s.interact(kind="command")
        self.assertTrue(r["ok"], r)
        self.assertTrue(self.marked(r))
        self.write(self.orig + "\ndef broken(:\n")
        r = self.s.interact(kind="command")
        self.assertFalse(r["ok"])
        self.assertIn("SyntaxError", r["trace"])
        self.write(self.orig)
        r = self.s.interact(kind="command")
        self.assertTrue(r["ok"], r)
        self.assertFalse(self.marked(r))


if __name__ == "__main__":
    unittest.main()
