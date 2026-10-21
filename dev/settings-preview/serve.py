#!/usr/bin/env python3
"""A local design preview for /settings (settings_ui.py).

    python3 dev/settings-preview/serve.py            # http://127.0.0.1:8740/
    python3 dev/settings-preview/serve.py --port 0   # any free port

Serves index.html (a Components V2 renderer in Discord's look) and a
small JSON API that answers each interaction with the real
settings_ui.handle(), the way monitor.py's on_settings_interaction does,
against a fake world: the fixtures and FakeCtx of tests/settingsui
(harness.py: three servers plus 60 fillers, channels, a category, a
thread, an owner, a server admin and a member, a ledger of refused
messages, a config.yaml plus its config.discord.yaml overlay in a temp
dir removed on exit). No bot token, no network.

settings_ui.py and muse_settings.py are re-imported whenever their files
change, so an edit shows on the next click (and the page re-runs its
clicks by itself when "Live" is on). MUT_DIR picks another package tree,
as for the offline suites.

API (all JSON):
    GET  /api/world      the fake world (servers, channels, people, ...)
    GET  /api/status     file versions (the page polls it)
    POST /api/reset      {"fixture": "default" | "plain" | "broken"}
    POST /api/interact   {"viewer", "where", "kind", ...}: see interact()
"""
import argparse
import copy
import importlib
import json
import os
import shutil
import signal
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.dont_write_bytecode = True   # nothing left behind in the tree
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(REPO, "tests", "settingsui"))
# harness puts tests/_lib on the path (MUT_DIR, scratch dirs) and imports
# muse_settings and settings_ui from MUT_DIR; crawl.check is the suites'
# check of a response against Discord's limits and the copy rules.
import harness as H  # noqa: E402
import crawl  # noqa: E402

MS, UI = H.MS, H.UI
INDEX = os.path.join(HERE, "index.html")
DM_CHANNEL = "40000000000000000077"
BOT_NAME = "Muse"
FIXTURES = {"default": "Default (config.yaml + overlay)",
            "plain": "Plain config.yaml, no overlay",
            "broken": "Unreadable config.discord.yaml"}
VIEWERS = {"owner": (H.OWNER, "Owner"),
           "admin": (H.ADMIN, "Server admin (Manage Server)"),
           "member": (H.MEMBER, "Member")}
THREAD_NAME = "muse-ideas"
ROLES = [{"id": H.ROLE, "name": "Mods", "guild_id": H.G1,
          "color": "#e67e22"}]
SELECT_KINDS = (5, 6, 7, 8)
_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# Module reloading
# ---------------------------------------------------------------------------
def _watched():
    return [MS.__file__, UI.__file__]


def _versions():
    out = {}
    for p in _watched():
        try:
            out[os.path.basename(p)] = os.stat(p).st_mtime_ns
        except OSError:
            out[os.path.basename(p)] = None
    return out


class Modules:
    """Re-imports muse_settings, then settings_ui (in place: every
    reference to the module objects stays good) when a file changed."""

    def __init__(self):
        self.loaded = _versions()
        self.error = None
        self.reloads = 0
        self.at = time.time()

    def fresh(self):
        """None when the modules are current (reloading them if needed),
        else the import error's traceback."""
        now = _versions()
        if now == self.loaded and self.error is None:
            return None
        self.loaded = now
        try:
            importlib.reload(MS)
            importlib.reload(UI)
        except Exception:
            self.error = traceback.format_exc()
            return self.error
        self.error = None
        self.reloads += 1
        self.at = time.time()
        if WORLD.get("w") is not None:
            WORLD["w"].patch()
        return None

    def status(self):
        return {"files": _versions(), "versions": self.loaded,
                "reloads": self.reloads,
                "loaded_at": self.at, "error": self.error,
                "mut_dir": H.MUT}


# ---------------------------------------------------------------------------
# The fake world
# ---------------------------------------------------------------------------
class World:
    """One FakeCtx (tests/settingsui/harness.py) and its fixtures. Time
    is the fixtures' fixed now, and history group ids count up from 1, so
    the same clicks give the same pages (the page re-runs them after an
    edit)."""

    def __init__(self, fixture="default"):
        if fixture not in FIXTURES:
            fixture = "default"
        self.fixture = fixture
        self.counter = {"n": 0}
        self.patch()
        if fixture == "plain":
            self.ctx = H.FakeCtx(text=H.PLAIN)
        elif fixture == "broken":
            self.ctx = H.FakeCtx(broken_overlay=True)
        else:
            self.ctx = H.FakeCtx()
        self.ctx.now = H.NOW
        self.logs_seen = len(self.ctx.logs)

    def patch(self):
        counter = self.counter

        def gid(prefix="g"):
            counter["n"] += 1
            return f"{prefix}{counter['n']:010x}"
        MS._now = lambda: H.FIXED_AT
        MS.new_group_id = gid

    def new_logs(self):
        out = self.ctx.logs[self.logs_seen:]
        self.logs_seen = len(self.ctx.logs)
        return out


WORLD = {"w": None}
MODULES = None


def world():
    if WORLD["w"] is None:
        WORLD["w"] = World()
    return WORLD["w"]


def _channel_index():
    out = {}
    for gid, chans in H.CHANNELS.items():
        for c in chans:
            out[c["id"]] = dict(c, guild_id=gid)
    out[H.THREAD] = {"id": H.THREAD, "name": THREAD_NAME, "type": 11,
                     "parent_id": H.MUSECHAT, "guild_id": H.G1,
                     "readable": True, "can_send": True}
    return out


def _join_notice():
    """The owner DM monitor.py sends on joining a server (its
    configure_button), for the Configure entry point."""
    g = next(g for g in H.GUILDS if g["id"] == H.G2)
    return {
        "id": "40000000000000000100", "author_id": H.BOT, "bot": True,
        "at": H.NOW - 3 * 3600,
        "content": (f"I just joined **{g['name']}** (server ID {g['id']}).\n"
                    "I'll answer when someone @mentions me or replies to "
                    "me. Extras on there: none.\nChannels I can see (1): "
                    "#homework\nTo change what I do there, press "
                    "**Configure** (or use /settings), or set "
                    f"servers.\"{g['id']}\" in config.yaml (no restart "
                    "needed)."),
        "components": [{"type": 1, "components": [
            {"type": 2, "style": 1, "label": "Configure",
             "custom_id": f"mdset:open:server:{g['id']}"}]}]}


def world_json():
    w = world()
    chans = {gid: [dict(c) for c in cs] for gid, cs in H.CHANNELS.items()}
    names = dict(H.NAMES)
    messages = []
    for o in w.ctx.outcomes:
        messages.append({
            "id": o["message_id"], "guild_id": o.get("guild_id"),
            "channel_id": o.get("channel_id"),
            "parent_id": o.get("parent_id"),
            "author_id": o.get("author_id"),
            "content": "hey <@" + H.BOT + "> what do you think?"
            if o.get("author_id") else "hello?",
            "at": o["at"], "code": o.get("code"),
            "status": o.get("status")})
    return {
        "bot": {"id": H.BOT, "name": BOT_NAME},
        "now": H.NOW,
        "guilds": [dict(g, filler=g["id"] not in (H.G1, H.G2, H.G3))
                   for g in w.ctx.guilds()],
        "channels": chans,
        "threads": {H.G1: [{"id": H.THREAD, "name": THREAD_NAME,
                            "type": 11, "parent_id": H.MUSECHAT}]},
        "users": [{"id": k, "name": v, "bot": k in (H.BOT, H.OTHERBOT)}
                  for k, v in names.items() if k != H.ROLE]
        + [{"id": H.BOT, "name": BOT_NAME, "bot": True}],
        "roles": ROLES,
        "messages": messages,
        "dm_channel": DM_CHANNEL,
        "dm_history": [_join_notice()],
        "viewers": {k: {"id": v[0], "name": H.NAMES[v[0]], "label": v[1]}
                    for k, v in VIEWERS.items()},
        "admin_guild": H.G1,
        "owners": sorted(w.ctx.owners),
        "fixture": w.fixture,
        "fixtures": FIXTURES,
        "limits": {"components": UI.MAX_COMPONENTS,
                   "text": UI.TEXT_BUDGET,
                   "custom_id": UI.CUSTOM_ID_MAX},
    }


# ---------------------------------------------------------------------------
# Interactions, as Discord sends them
# ---------------------------------------------------------------------------
def _where(where):
    """(guild id, channel id, channel type, parent id) for a "where"
    {"guild": id | null, "channel": id}."""
    where = where or {}
    gid = where.get("guild") or None
    if not gid:
        return None, DM_CHANNEL, 1, None
    cid = str(where.get("channel") or H.MUSECHAT)
    info = _channel_index().get(cid) or {}
    ctype = info.get("type", 0)
    parent = info.get("parent_id") if ctype in (10, 11, 12) else None
    return str(gid), cid, ctype, parent


def _who(viewer, gid):
    """(user id, permissions) for a viewer in a server: the server admin
    has Manage Server in whichever server they are looking at."""
    uid = VIEWERS.get(viewer, VIEWERS["owner"])[0]
    perms = H.MANAGE if viewer == "admin" and gid else "0"
    return uid, perms


def _base(ix, uid, gid, perms, cid, ctype, parent):
    """Who and where on an interaction (as harness._who, plus the
    clicking user's global name and a thread's parent)."""
    user = {"id": uid, "username": H.NAMES.get(uid, "user").lower(),
            "global_name": H.NAMES.get(uid)}
    ix["channel_id"] = cid
    ix["channel"] = {"id": cid, "type": ctype}
    if gid:
        ix["guild_id"] = gid
        ix["member"] = {"user": user, "permissions": perms, "nick": None}
        ix["channel"]["parent_id"] = parent
    else:
        ix["user"] = user
    ix.update({"id": "1", "token": "t", "application_id": H.BOT,
               "version": 1})
    return ix


def resolved_for(comps_or_values, gid, kind=None):
    """Resolved data for select values: users (and members in a server),
    roles, channels, from the fake world."""
    chans = _channel_index()
    users = {u: n for u, n in H.NAMES.items() if u != H.ROLE}
    users[H.BOT] = BOT_NAME
    roles = {r["id"]: r for r in ROLES}
    out = {}

    def add(v, k):
        v = str(v)
        if k in (5, 7) and v in users:
            out.setdefault("users", {})[v] = {
                "id": v, "username": users[v].lower(),
                "global_name": users[v], "bot": v in (H.BOT, H.OTHERBOT)}
            if gid:
                out.setdefault("members", {})[v] = {"nick": None,
                                                    "roles": []}
        elif k in (6, 7) and v in roles:
            out.setdefault("roles", {})[v] = {"id": v,
                                              "name": roles[v]["name"]}
        elif k == 8 and v in chans:
            c = chans[v]
            out.setdefault("channels", {})[v] = {
                "id": v, "name": c["name"], "type": c.get("type", 0),
                "parent_id": c.get("parent_id"),
                "permissions": "2147483647"}
    if kind is not None:
        for v in comps_or_values:
            add(v, kind)
        return out
    for c in H.walk(comps_or_values):
        if c.get("type") in SELECT_KINDS:
            for v in c.get("values") or []:
                add(v, c["type"])
    return out


def build(req):
    """The interaction for a request from the page:

    kind "command":   /settings where the viewer is
    kind "why":       "Why no reply?" on message req["target"]
    kind "component": a click: custom_id, component_type, values,
                      message (the clicked message: id, flags, components)
    kind "submit":    a modal submit: custom_id, components (as Discord
                      sends them), message
    """
    gid, cid, ctype, parent = _where(req.get("where"))
    uid, perms = _who(req.get("viewer"), gid)
    kind = req.get("kind")
    if kind == "command":
        ix = {"type": 2, "data": {"id": "2", "name": UI.COMMAND,
                                  "type": 1}}
    elif kind == "why":
        target = str(req.get("target") or "")
        o = world().ctx.outcome(target) or {}
        author = o.get("author_id") or H.SAM
        msg = {"id": target, "channel_id": o.get("channel_id") or cid,
               "author": {"id": author, "username": H.NAMES.get(
                   author, "someone").lower(),
                   "global_name": H.NAMES.get(author)},
               "mentions": [{"id": H.BOT}],
               "content": "hey"}
        ix = {"type": 2, "data": {"id": "3", "name": req.get("name")
                                  or UI.WHY_COMMANDS[0], "type": 3,
                                  "target_id": target,
                                  "resolved": {"messages": {target: msg}}}}
    elif kind == "component":
        ctype_ = int(req.get("component_type") or 2)
        data = {"custom_id": str(req.get("custom_id") or ""),
                "component_type": ctype_}
        if ctype_ != 2:
            data["values"] = [str(v) for v in req.get("values") or []]
        if ctype_ in SELECT_KINDS:
            data["resolved"] = resolved_for(data["values"], gid, ctype_)
        ix = {"type": 3, "data": data,
              "message": req.get("message") or {"id": "9", "flags": 64}}
    elif kind == "submit":
        comps = req.get("components") or []
        data = {"custom_id": str(req.get("custom_id") or ""),
                "components": comps}
        res = resolved_for(comps, gid)
        if res:
            data["resolved"] = res
        ix = {"type": 5, "data": data}
        if req.get("message"):
            ix["message"] = req["message"]
    else:
        raise ValueError(f"unknown kind {kind!r}")
    return _base(ix, uid, gid, perms, cid, ctype, parent)


def stats(resp):
    data = (resp or {}).get("data") or {}
    comps = data.get("components")
    return {"components": UI.count_components(comps),
            "text": crawl.budget(resp),
            "max_components": UI.MAX_COMPONENTS,
            "text_budget": UI.TEXT_BUDGET}


def interact(req):
    """Answer one interaction with settings_ui.handle (what monitor.py's
    on_settings_interaction does, minus the network)."""
    with _LOCK:
        err = MODULES.fresh()
        if err:
            return {"ok": False, "error": "settings_ui.py or "
                    "muse_settings.py failed to import", "trace": err}
        w = world()
        try:
            ix = build(req)
        except (ValueError, TypeError) as e:
            return {"ok": False, "error": f"bad request: {e}"}
        w.ctx.refresh()
        t0 = time.perf_counter()
        try:
            resp = UI.handle(copy.deepcopy(ix), w.ctx)
        except Exception:
            return {"ok": False, "error": "settings_ui.handle raised",
                    "trace": traceback.format_exc(), "interaction": ix}
        spent = (time.perf_counter() - t0) * 1000
        logs = w.new_logs()
        if not isinstance(resp, dict) or not isinstance(resp.get("type"),
                                                        int):
            return {"ok": False, "error": "settings_ui.handle returned no "
                    "callback body (the monitor answers 'Something went "
                    "wrong')", "interaction": ix, "response": resp,
                    "logs": logs}
        try:
            checks = crawl.check(resp, "response")
        except Exception as e:
            checks = [f"response: check failed: {type(e).__name__}: {e}"]
        checks = [c.removeprefix("response: ") for c in checks]
        return {"ok": True, "response": resp, "interaction": ix,
                "checks": checks, "stats": stats(resp), "logs": logs,
                "ms": round(spent, 1), "reloads": MODULES.reloads}


def reset(req):
    with _LOCK:
        err = MODULES.fresh()
        if err:
            return {"ok": False, "error": "import failed", "trace": err}
        old = WORLD["w"]
        WORLD["w"] = World((req or {}).get("fixture") or "default")
        if old is not None:
            shutil.rmtree(old.ctx.dir, ignore_errors=True)
        return {"ok": True, "world": world_json()}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "settings-preview/1"

    def log_message(self, fmt, *args):
        if self.server.verbose:
            super().log_message(fmt, *args)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ("127.0.0.1", "localhost", "[::1]")

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "local only"})
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            with open(INDEX, "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if path == "/api/world":
            with _LOCK:
                MODULES.fresh()
                return self._send(200, world_json())
        if path == "/api/status":
            with _LOCK:
                return self._send(200, MODULES.status())
        if path == "/favicon.ico":
            return self._send(204, b"", "image/x-icon")
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "local only"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, OSError):
            return self._send(400, {"ok": False, "error": "bad JSON"})
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/interact":
                return self._send(200, interact(req))
            if path == "/api/reset":
                return self._send(200, reset(req))
        except Exception:
            return self._send(500, {"ok": False, "error": "server error",
                                    "trace": traceback.format_exc()})
        return self._send(404, {"error": "not found"})


def main(argv=None):
    global MODULES
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8740,
                    help="0: any free port (default 8740)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="log every request")
    a = ap.parse_args(argv)
    MODULES = Modules()
    world()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    srv.daemon_threads = True
    srv.verbose = a.verbose
    # SIGTERM exits like Ctrl-C, so the temp dirs are removed (atexit).
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    print(f"settings preview: http://{a.host}:{srv.server_port}/ "
          f"(settings_ui.py from {H.MUT})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
