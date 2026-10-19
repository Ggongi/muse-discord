"""Small offline helpers for the settings engine tests: temp configs,
a fake /settings Ctx, and monitor.py imported with Discord stubbed.
MUT_DIR = the package tree under test (default: this repo)."""
import contextlib
import copy
import importlib.util
import io
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
MUT = os.path.abspath(os.environ.get("MUT_DIR")
                      or os.path.join(HERE, "..", ".."))
sys.path.insert(0, MUT)
sys.dont_write_bytecode = True
import muse_settings as MS  # noqa: E402

PLUGINS = os.path.join(MUT, "plugins")

OWNER = "11100000000000000001"
OWNER2 = "11100000000000000002"
ADMIN = "22200000000000000001"
ADMIN2 = "22200000000000000002"
SAM = "33300000000000000001"
ANN = "33300000000000000002"
SPAM = "33300000000000000009"
BOT = "99900000000000000001"
G1 = "60000000000000000001"     # Lounge, 1,240 members
G2 = "60000000000000000002"     # Pixel, 48,213 members
G3 = "60000000000000000003"     # Study, 9 members
CAT = "70000000000000000001"    # category Staff in G1
CH = "70000000000000000002"     # #general in G1 (in CAT)
CH2 = "70000000000000000003"    # #random in G1
CH_G2 = "70000000000000000009"  # #art in G2
CH_G3 = "70000000000000000010"  # #homework in G3

BASE = f"""version: 1
owners: ["{OWNER}", "{OWNER2}"]
token_from: env:MUSE_TEST_TOKEN
servers:
  "{G1}":
    name: Lounge
"""
GUILDS = [{"id": G1, "name": "Lounge", "member_count": 1240},
          {"id": G2, "name": "Pixel", "member_count": 48213},
          {"id": G3, "name": "Study", "member_count": 9}]
CHANNELS = {
    G1: [{"id": CAT, "name": "Staff", "type": 4, "parent_id": None},
         {"id": CH, "name": "general", "type": 0, "parent_id": CAT},
         {"id": CH2, "name": "random", "type": 0, "parent_id": None}],
    G2: [{"id": CH_G2, "name": "art", "type": 0, "parent_id": None}],
    G3: [{"id": CH_G3, "name": "homework", "type": 0, "parent_id": None}],
}
INFO = {"guilds": GUILDS, "channels": CHANNELS}
NAMES = {SAM: "Sam", ANN: "Ann", OWNER: "Zach", ADMIN: "Riley"}


def config(test, text=BASE, overlay=None, state=None):
    """A config.yaml (and optionally config.discord.yaml / state.json) in
    a temp dir removed after the test. Returns its path."""
    d = tempfile.mkdtemp(prefix="mdeng-")
    test.addCleanup(shutil.rmtree, d, True)
    path = os.path.join(d, "config.yaml")
    with open(path, "w") as f:
        f.write(text)
    if overlay is not None:
        with open(os.path.join(d, MS.OVERLAY_NAME), "w") as f:
            f.write(overlay)
    if state is not None:
        import json
        with open(os.path.join(d, "state.json"), "w") as f:
            json.dump(state, f)
    return path


def load(cfg):
    return MS.load(cfg, PLUGINS)


def doc(cfg):
    return MS._read_overlay_doc(MS.overlay_path_for(cfg))


class FakeCtx:
    """What monitor.py gives settings_ui.handle (reads/writes the real
    files through muse_settings)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.owners = {OWNER, OWNER2}
        self.bot_id = BOT
        self.now = 0.0
        self.logs, self.notified = [], []

    @property
    def settings(self):
        return MS.load(self.cfg, PLUGINS)

    def reload(self):
        return MS.load(self.cfg, PLUGINS)

    def guilds(self):
        return copy.deepcopy(GUILDS)

    def channels(self, gid):
        return copy.deepcopy(CHANNELS.get(str(gid), []))

    def write(self, key, value, by):
        MS.set_value(self.cfg, key, value, by, PLUGINS)

    def reset(self, key, by):
        MS.reset(self.cfg, key, by, PLUGINS)

    def undo(self, prefix, by, **kw):
        if kw.get("admin_gid"):
            kw.setdefault("info", self)
        return MS.undo(self.cfg, prefix, by, PLUGINS, **kw)

    def apply(self, changes, by, role, group=None, admin_gid=None):
        return MS.apply(self.cfg, changes, by, group, PLUGINS, role=role,
                        admin_gid=admin_gid, info=self)

    def names(self, some_id):
        for g in GUILDS:
            if g["id"] == str(some_id):
                return g["name"]
        for chans in CHANNELS.values():
            for c in chans:
                if c["id"] == str(some_id):
                    return c["name"]
        return None

    def display_name(self, uid):
        return NAMES.get(str(uid))

    def recent_outcomes(self, place, limit=20):
        return []

    def outcome(self, message_id):
        return None

    def stash_put(self, uid, payload, ttl=900):
        self.stash = getattr(self, "stash", {})
        tok = f"t{len(self.stash):09d}"
        self.stash[tok] = (str(uid), payload)
        return tok

    def stash_take(self, uid, tok):
        e = getattr(self, "stash", {}).get(tok)
        if e is None:
            return None
        if e[0] != str(uid):
            raise PermissionError("not yours")
        del self.stash[tok]
        return e[1]

    def notify_owners_admin_change(self, *a):
        self.notified.append(a)

    def log(self, msg):
        self.logs.append(msg)


class FakeAPI:
    def __init__(self):
        self.members = set()   # (guild, user) pairs that exist
        self.gets = []
        self.posts = []
        self.history = {}      # channel id -> [messages], oldest first

    def get(self, path):
        self.gets.append(path)
        if path.startswith("/channels/") and "/messages" in path:
            return list(reversed(self.history.get(path.split("/")[2], [])))
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[0] == "guilds" \
                and parts[2] == "members" and (parts[1], parts[3]) \
                in self.members:
            return {"roles": []}
        err = Exception("HTTP Error 404")
        err.code = 404
        raise err

    def post(self, path, data):
        self.posts.append((path, data))
        if path == "/users/@me/channels":
            # DM channel ids: "4" + the user id's tail
            return {"id": "4" + data["recipient_id"][1:],
                    "recipients": [{"username": NAMES.get(
                        data["recipient_id"], "u")}]}
        return {"id": "1"}


_seq = [0]


def load_monitor(test, text):
    """monitor.py imported against a temp config, Discord stubbed."""
    cfg = config(test, text)
    os.environ["MUSE_DISCORD_CONFIG"] = cfg
    argv = sys.argv
    sys.argv = ["monitor.py", "dummy-token"]
    _seq[0] += 1
    spec = importlib.util.spec_from_file_location(
        f"engine_mut_monitor_{_seq[0]}", os.path.join(MUT, "monitor.py"))
    mod = importlib.util.module_from_spec(spec)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            spec.loader.exec_module(mod)
    finally:
        sys.argv = argv
    d = os.path.dirname(cfg)
    for attr, fn in [("STATE_FILE", "state.json"),
                     ("QUEUE_FILE", "reply_queue.json"),
                     ("OUTBOX_FILE", "reply_outbox.json"),
                     ("OUTBOX_PROCESSING_FILE",
                      "reply_outbox.processing.json"),
                     ("OUTBOX_REJECTED_FILE", "reply_outbox.rejected.json"),
                     ("QUARANTINE_FILE", "quarantine.json"),
                     ("LEDGER_FILE", "mention_ledger.json"),
                     ("LOCK_FILE", "reply.lock"),
                     ("HEARTBEAT_FILE", "monitor.heartbeat"),
                     ("MEDIA_DIR", "media"), ("GENERATED_DIR", "generated")]:
        if hasattr(mod, attr):
            setattr(mod, attr, os.path.join(d, fn))
    api = FakeAPI()
    mod.api = api
    mod.api_get = api.get
    mod.api_post = api.post
    mod.send_typing = lambda cid: None
    mod.collect_media = lambda m: ([], [], [])
    mod.BOT_ID = mod.BOT_USER_ID = BOT
    mod.GUILDS = {G1: {"name": "Lounge"}}
    mod.cfg_path = cfg
    return mod
