"""settings_ui (the /settings pages and writes), offline: fake
interactions for owners, server admins and strangers, in DMs and in a
server, against a real config.yaml + config.discord.yaml in a temp dir
(writes go through muse_settings.set_value/reset/undo, as the monitor's
Ctx does). Every response is checked against Discord's limits.
MUT_DIR = the package tree under test."""
import copy
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
from common import MUT_DIR as MUT, scratch  # noqa: E402

sys.path.insert(0, MUT)
import muse_settings as MS  # noqa: E402
import settings_ui as UI  # noqa: E402

PLUGINS = os.path.join(MUT, "plugins")

# 19-20 digit snowflakes, the longest real ids.
OWNER = "11100000000000000001"
ADMIN = "22200000000000000001"
STRANGER = "33300000000000000001"
U1, U2 = "44400000000000000001", "44400000000000000002"
ROLE = "55500000000000000001"
BOT = "99900000000000000001"
G1 = "60000000000000000001"     # Lounge, 1,240 members
G2 = "60000000000000000002"     # Big, 5,000 members
G3 = "60000000000000000003"     # Tiny, 12 members
CAT = "70000000000000000001"
CH = "70000000000000000002"     # in CAT
CH2 = "70000000000000000003"    # no category
CH_G2 = "70000000000000000009"
THREAD = "70000000000000000010"
MANAGE = str(1 << 5)
ADMINISTRATOR = str(1 << 3)

BASE = f"""version: 1
owners: ["{OWNER}"]
token_from: env:MUSE_TEST_TOKEN
everywhere:
  privacy:
    never_share: ["secret one", "secret two"]
all_servers:
  who:
    replies: when-addressed
servers:
  "{G1}":
    name: Lounge
    who:
      replies: conversational
"""
GUILDS = [{"id": G1, "name": "Lounge", "member_count": 1240},
          {"id": G2, "name": "Big", "member_count": 5000},
          {"id": G3, "name": "Tiny", "member_count": 12}]
CHANNELS = {G1: [{"id": CAT, "name": "Staff", "type": 4, "parent_id": None},
                 {"id": CH, "name": "general", "type": 0,
                  "parent_id": CAT},
                 {"id": CH2, "name": "random", "type": 0,
                  "parent_id": None}],
            G2: [{"id": CH_G2, "name": "chat", "type": 0,
                  "parent_id": None}]}


class FakeCtx:
    def __init__(self, text=BASE, guilds=GUILDS, channels=CHANNELS,
                 with_reload=False):
        d = scratch("muse-ui-cfg-")
        self.cfg = os.path.join(d, "config.yaml")
        with open(self.cfg, "w") as f:
            f.write(text)
        self.owners = {OWNER}
        self.bot_id = BOT
        self.now = time.time()
        self._guilds = guilds
        self._channels = channels
        self.writes, self.resets, self.undos = [], [], []
        self.notified, self.logs = [], []
        self.refresh()
        if with_reload:
            self.reload = self._reload

    def refresh(self):
        """What the monitor does before each interaction."""
        self.settings = MS.load(self.cfg, PLUGINS)

    def _reload(self):
        self.refresh()
        return self.settings

    def guilds(self):
        return copy.deepcopy(self._guilds)

    def channels(self, gid):
        return copy.deepcopy(self._channels.get(gid, []))

    def write(self, key, value, by):
        self.writes.append((key, value, by))
        MS.set_value(self.cfg, key, value, by, PLUGINS)

    def reset(self, key, by):
        self.resets.append((key, by))
        MS.reset(self.cfg, key, by, PLUGINS)

    def undo(self, prefix, by):
        k = MS.undo(self.cfg, prefix, by, PLUGINS)
        self.undos.append((prefix, by, k))
        return k

    def notify_owners_admin_change(self, guild_id, user_id, key, old, new):
        self.notified.append((guild_id, user_id, key, old, new))

    def log(self, msg):
        self.logs.append(msg)

    # -- reading back ----------------------------------------------------
    def value(self, key, scope):
        return MS.load(self.cfg, PLUGINS).get(key, scope)

    @property
    def changed(self):
        return self.writes + self.resets + self.undos


def _who(ix, user, guild, perms, channel=CH, ctype=0, parent=CAT):
    if guild:
        ix["guild_id"] = guild
        ix["member"] = {"user": {"id": user}, "permissions": perms or "0"}
        ix["channel_id"] = channel
        ix["channel"] = {"id": channel, "type": ctype, "parent_id": parent}
    else:
        ix["user"] = {"id": user}
        ix["channel_id"] = "1"
    return ix


def cmd(user, guild=None, perms=None, **kw):
    return _who({"type": 2, "id": "1", "token": "t",
                 "data": {"name": "settings", "type": 1}},
                user, guild, perms, **kw)


def click(user, custom_id, values=None, guild=None, perms=None):
    data = {"custom_id": custom_id, "component_type": 2}
    if values is not None:
        data["values"] = values
        data["component_type"] = 3
    return _who({"type": 3, "id": "1", "token": "t", "data": data,
                 "message": {"id": "9", "flags": 64}}, user, guild, perms)


def submit(user, custom_id, value, guild=None, perms=None, shape="label"):
    inner = {"type": 4, "custom_id": "v", "value": value}
    comp = {"type": 18, "component": inner} if shape == "label" \
        else {"type": 1, "components": [inner]}
    return _who({"type": 5, "id": "1", "token": "t",
                 "data": {"custom_id": custom_id, "components": [comp]},
                 "message": {"id": "9", "flags": 64}}, user, guild, perms)


# ---------------------------------------------------------------------------
# Payload validation (Discord's limits)
# ---------------------------------------------------------------------------
SELECTS = (3, 5, 6, 7, 8)


def components(resp):
    out = []
    for r in resp.get("data", {}).get("components") or []:
        if r.get("type") == 1:
            out += r["components"]
        elif r.get("type") == 18:
            out.append(r["component"])
    return out


def ids(resp):
    return [c["custom_id"] for c in components(resp) if "custom_id" in c]


def find(resp, prefix):
    for c in components(resp):
        if c.get("custom_id", "").startswith(prefix):
            return c
    return None


def option_values(comp):
    return [o["value"] for o in comp.get("options") or []]


def validate(tc, resp, where=""):
    tc.assertIsInstance(resp, dict, where)
    t = resp.get("type")
    tc.assertIn(t, (4, 7, 9), where)
    data = resp["data"]
    if t == 9:
        tc.assertTrue(data["custom_id"].startswith("mdset:"), where)
        tc.assertLessEqual(len(data["custom_id"]), 100, where)
        tc.assertTrue(1 <= len(data["title"]) <= 45, (where, data["title"]))
        tc.assertTrue(1 <= len(data["components"]) <= 5, where)
        for c in data["components"]:
            tc.assertEqual(c["type"], 18, where)
            tc.assertTrue(1 <= len(c["label"]) <= 45, where)
            tc.assertLessEqual(len(c.get("description", "")), 100, where)
            ti = c["component"]
            tc.assertEqual(ti["type"], 4)
            tc.assertIn(ti["style"], (1, 2))
            tc.assertLessEqual(len(ti["custom_id"]), 100)
            tc.assertLessEqual(len(ti.get("placeholder", "")), 100, where)
            tc.assertLessEqual(ti["max_length"], 4000)
            tc.assertLessEqual(len(ti.get("value", "")), ti["max_length"])
        return
    tc.assertLessEqual(len(data.get("content") or ""), 2000, where)
    tc.assertEqual(data.get("allowed_mentions"), {"parse": []}, where)
    if t == 4:
        tc.assertEqual(data.get("flags", 0) & 64, 64, where)
    rows = data.get("components") or []
    tc.assertLessEqual(len(rows), 5, (where, len(rows)))
    seen = set()
    for r in rows:
        tc.assertEqual(r["type"], 1, where)
        comps = r["components"]
        tc.assertTrue(1 <= len(comps) <= 5, where)
        if any(c["type"] in SELECTS for c in comps):
            tc.assertEqual(len(comps), 1, where)
        for c in comps:
            cid = c["custom_id"]
            tc.assertTrue(cid.startswith("mdset:"), (where, cid))
            tc.assertLessEqual(len(cid), 100, (where, cid))
            tc.assertNotIn(cid, seen, (where, cid))
            seen.add(cid)
            if c["type"] == 2:
                tc.assertIn(c["style"], (1, 2, 3, 4), where)
                tc.assertTrue(1 <= len(c["label"]) <= 80, (where, c))
            else:
                tc.assertIn(c["type"], SELECTS, where)
                tc.assertLessEqual(len(c.get("placeholder", "")), 150)
                mn, mx = c.get("min_values", 1), c.get("max_values", 1)
                tc.assertTrue(0 <= mn <= mx <= 25, where)
                tc.assertLessEqual(len(c.get("default_values") or []), mx)
                if c["type"] == 3:
                    opts = c["options"]
                    tc.assertTrue(1 <= len(opts) <= 25, (where, len(opts)))
                    vals = [o["value"] for o in opts]
                    tc.assertEqual(len(vals), len(set(vals)), where)
                    tc.assertLessEqual(sum(1 for o in opts
                                           if o.get("default")), mx)
                    for o in opts:
                        tc.assertTrue(1 <= len(o["label"]) <= 100, where)
                        tc.assertTrue(1 <= len(o["value"]) <= 100, where)
                        tc.assertLessEqual(len(o.get("description", "")),
                                           100, where)


NAV = ("h", "go", "p", "d", "pl", "pls", "cp", "f", "w", "mo")


def crawl(tc, ctx, start, user, guild=None, perms=None, limit=400):
    """Click every navigation control reachable from start (no writes);
    validate every page. Returns {custom_id: response}."""
    seen, out = set(), {}
    todo = [start]
    while todo and len(out) < limit:
        resp = todo.pop(0)
        validate(tc, resp)
        for c in components(resp):
            cid = c.get("custom_id", "")
            act = cid.split(":")[1] if ":" in cid else ""
            if act not in NAV or c.get("disabled"):
                continue
            if c["type"] == 3:
                clicks = [(cid, [v]) for v in option_values(c)]
            elif c["type"] == 2:
                clicks = [(cid, None)]
            else:
                continue
            for cc, vals in clicks:
                k = (cc, tuple(vals or ()))
                if k in seen:
                    continue
                seen.add(k)
                ctx.refresh()
                r = UI.handle(click(user, cc, vals, guild, perms), ctx)
                validate(tc, r, cc)
                out[cc + (f"={vals[0]}" if vals else "")] = r
                if r["type"] == 7:
                    todo.append(r)
    tc.assertEqual(ctx.changed, [], "navigation wrote something")
    tc.assertFalse(any("Error" in m for m in ctx.logs), ctx.logs)
    return out


def text(resp):
    return resp["data"].get("content") or ""


class Base(unittest.TestCase):
    def setUp(self):
        self.ctx = FakeCtx()

    def h(self, ix):
        self.ctx.refresh()
        r = UI.handle(ix, self.ctx)
        validate(self, r, ix.get("data", {}).get("custom_id"))
        return r

    def owner(self, cid, values=None, guild=None):
        return self.h(click(OWNER, cid, values, guild,
                            "0" if guild else None))

    def admin(self, cid, values=None, guild=G1, perms=MANAGE):
        return self.h(click(ADMIN, cid, values, guild, perms))

    def assertRefused(self, r):
        self.assertEqual(r["type"], 4)
        self.assertEqual(r["data"]["flags"], 64)
        self.assertEqual(r["data"]["components"], [])


# ---------------------------------------------------------------------------
class Command(Base):
    def test_owner_dm_opens_home(self):
        r = self.h(cmd(OWNER))
        self.assertEqual(r["type"], 4)
        self.assertEqual(r["data"]["flags"], 64)
        go = find(r, "mdset:go")
        self.assertEqual(option_values(go)[:3], ["e", "d", "a"])
        self.assertIn(f"s{G1}", option_values(go))
        self.assertTrue(find(r, "mdset:pz:e"))
        self.assertIn("Lounge · 1,240 members",
                      [o["label"] for o in go["options"]])

    def test_owner_in_server_opens_that_server(self):
        r = self.h(cmd(OWNER, G1, "0"))
        self.assertEqual(r["type"], 4)
        self.assertIn("**Lounge** · 1,240 members", text(r))
        self.assertIn("Replies: **conversational**", text(r))
        self.assertIn(f"mdset:p:c{G1}.{CH}", ids(r))      # This channel
        self.assertIn(f"mdset:p:c{G1}.{CAT}", ids(r))     # This category
        self.assertIn("mdset:h:0", ids(r))                # All scopes
        for d in ("who", "how", "may", "priv", "lim", "plug"):
            self.assertIn(f"mdset:d:s{G1}:{d}", ids(r))
        self.assertTrue(find(r, f"mdset:cp:s{G1}"))

    def test_thread_counts_as_parent_channel(self):
        r = self.h(cmd(OWNER, G1, "0", channel=THREAD, ctype=11,
                       parent=CH))
        self.assertIn(f"mdset:p:c{G1}.{CH}", ids(r))
        self.assertIn(f"mdset:p:c{G1}.{CAT}", ids(r))

    def test_admin_gets_restricted_server_page(self):
        for perms in (MANAGE, ADMINISTRATOR, str((1 << 5) | (1 << 11))):
            r = self.h(cmd(ADMIN, G1, perms))
            self.assertEqual(r["type"], 4)
            got = ids(r)
            self.assertIn(f"mdset:d:s{G1}:who", got)
            self.assertIn(f"mdset:d:s{G1}:plug", got)
            for d in ("how", "may", "priv", "lim", "bot"):
                self.assertNotIn(f"mdset:d:s{G1}:{d}", got)
            self.assertNotIn("mdset:h:0", got)
            self.assertFalse(find(r, "mdset:cp:"))
            self.assertIn("quieter", text(r))

    def test_strangers_refused_everywhere(self):
        for ix in (cmd(STRANGER), cmd(STRANGER, G1, "0"),
                   cmd(STRANGER, G1, str(1 << 11)),   # send messages only
                   cmd(ADMIN)):                       # admin, but in a DM
            r = self.h(ix)
            self.assertRefused(r)
            self.assertIn("Only the bot's owners", text(r))
        for c in ("mdset:h:0", f"mdset:p:s{G1}", f"mdset:s:s{G1}:r",
                  "mdset:open:server:" + G1):
            self.assertRefused(self.h(click(STRANGER, c, ["off"])))
        self.assertEqual(self.ctx.changed, [])

    def test_join_dm_configure_button(self):
        r = self.owner(f"mdset:open:server:{G2}")
        self.assertEqual(r["type"], 4)             # a new ephemeral page
        self.assertEqual(r["data"]["flags"], 64)
        self.assertIn("**Big** · 5,000 members", text(r))
        self.assertNotIn(f"mdset:p:c{G2}.{CH_G2}", ids(r))  # no guild here
        self.assertRefused(self.admin(f"mdset:open:server:{G1}"))
        self.assertRefused(self.h(click(STRANGER,
                                        f"mdset:open:server:{G1}")))
        self.assertIn("no longer active",
                      text(self.owner("mdset:open:server:abc")))

    def test_other_command_names_and_junk(self):
        ix = cmd(OWNER)
        ix["data"]["name"] = "other"
        self.assertIn("no longer active", text(self.h(ix)))
        for c in ("mdset", "mdset:", "mdset:zz:e", "mdset:p:x", "mdset:p",
                  "mdset:s:e:nope", "mdset:d:e:nope", "x:p:e",
                  "mdset:p:s123:" + "9" * 100):
            self.assertIn("no longer active", text(self.owner(c)), c)


class Pagination(Base):
    def test_sixty_servers(self):
        guilds = [{"id": str(61000000000000000000 + i),
                   "name": f"Server {i:02d}", "member_count": i * 10}
                  for i in range(60)]
        self.ctx = FakeCtx(guilds=guilds)
        r = self.h(cmd(OWNER))
        got, page, pages = [], 0, 0
        while True:
            pages += 1
            sel = find(r, "mdset:go")
            self.assertLessEqual(len(sel["options"]), 25)
            got += [v for v in option_values(sel) if v.startswith("s")]
            nxt = find(r, "mdset:h:" + str(page + 1))
            if nxt is None or nxt.get("disabled"):
                break
            page += 1
            r = self.owner(nxt["custom_id"])
            self.assertEqual(r["type"], 7)
        self.assertEqual(pages, 3)
        self.assertEqual(sorted(got), sorted("s" + g["id"] for g in guilds))
        self.assertIn("page 3 of 3", text(r))
        # out-of-range page numbers clamp
        self.assertIn("page 3 of 3", text(self.owner("mdset:h:99")))
        r = self.owner("mdset:go", ["s" + guilds[59]["id"]])
        self.assertIn("Server 59", text(r))


class Crawl(Base):
    def test_owner_from_dm_every_page_is_valid(self):
        out = crawl(self, self.ctx, self.h(cmd(OWNER)), OWNER)
        self.assertGreater(len(out), 60)
        for k, r in out.items():
            for c in ids(r):
                self.assertNotIn("never_share", c)
                self.assertNotIn("presence.enabled", c)
        # every drawer at every scope rendered
        for tok in ("e", "d", "a", f"s{G1}", f"c{G1}.{CH}", f"c{G1}.{CAT}"):
            pages = [r for r in out.values()
                     if any(f":{tok}:" in c or c.endswith(f":{tok}")
                            for c in ids(r))]
            self.assertTrue(pages, tok)
        for d in ("who", "how", "may", "priv", "lim", "plug", "bot"):
            self.assertIn(f"mdset:d:e:{d}" if d != "bot" else "mdset:d:e:bot",
                          out)
        modals = [r for r in out.values() if r["type"] == 9]
        self.assertTrue(modals)

    def test_owner_from_server_every_page_is_valid(self):
        crawl(self, self.ctx, self.h(cmd(OWNER, G1, "0")), OWNER, G1, "0")

    def test_admin_every_page_is_valid_and_restricted(self):
        out = crawl(self, self.ctx, self.h(cmd(ADMIN, G1, MANAGE)), ADMIN,
                    G1, MANAGE)
        for k, r in out.items():
            for c in ids(r):
                tok = c.split(":")[2] if c.count(":") >= 2 else ""
                if tok and not c.startswith("mdset:pls") and tok[0] in "sc":
                    self.assertTrue(tok[1:].startswith(G1), c)
                for bad in (":m", ":pd", ":pp", ":n", ":ln", ":al", ":df",
                            ":bx", ":oc"):
                    self.assertFalse(c.endswith(bad), c)

    def test_drawer_contents(self):
        r = self.owner("mdset:d:d:who")
        self.assertTrue(find(r, "mdset:s:d:df"))           # dms_from select
        r = self.owner(f"mdset:d:s{G1}:who")
        self.assertIsNone(find(r, f"mdset:s:s{G1}:df"))    # D only
        self.assertTrue(find(r, f"mdset:s:s{G1}:r"))
        self.assertTrue(find(r, f"mdset:f:s{G1}:al"))      # people page
        self.assertTrue(find(r, f"mdset:mo:s{G1}:bx"))     # number modal
        r = self.owner(f"mdset:d:s{G1}:priv")
        self.assertIn("Secret phrases (never share)**: 2 set in config.yaml",
                      text(r))
        opts = find(r, f"mdset:s:s{G1}:m")["options"]
        self.assertEqual([o["value"] for o in opts],
                         ["everything", "public-only", "nothing"])
        self.assertTrue(all(o.get("description") for o in opts))
        self.assertIn("built-in", text(r))
        r = self.owner("mdset:d:a:who")                    # all_servers
        self.assertEqual([c for c in ids(r) if ":s:" in c], ["mdset:s:a:r"])
        self.assertIn("set here in config.yaml", text(r))
        r = self.owner("mdset:d:e:bot")
        self.assertTrue(find(r, "mdset:mo:e:ps"))
        self.assertTrue(find(r, "mdset:v:e:un:off"))
        self.assertIn("no longer active", text(self.owner(
            f"mdset:d:s{G1}:bot")))

    def test_channel_page_only_channel_settings(self):
        r = self.owner(f"mdset:p:c{G1}.{CH}")
        got = ids(r)
        self.assertIn(f"mdset:s:c{G1}.{CH}:r", got)
        self.assertIn(f"mdset:mo:c{G1}.{CH}:n", got)
        self.assertIn(f"mdset:v:c{G1}.{CH}:~welcome.enabled:on", got)
        self.assertIn(f"mdset:p:s{G1}", got)               # back to server
        for c in got:
            self.assertFalse(c.endswith((":m", ":ln", ":pd", ":al")), c)
        self.assertIn("general", text(r))
        r = self.owner(f"mdset:p:c{G1}.{CAT}")
        self.assertIn("category Staff", text(r))
        self.assertIn("inherit", text(r))

    def test_plugins_drawer_from_manifests(self):
        r = self.owner(f"mdset:d:s{G1}:plug")
        self.assertEqual(sorted(option_values(find(r, "mdset:pls:"))),
                         ["media", "welcome"])
        r = self.owner(f"mdset:pls:s{G1}", ["welcome"])
        got = ids(r)
        self.assertIn(f"mdset:v:s{G1}:~welcome.enabled:on", got)
        self.assertIn(f"mdset:f:s{G1}:~welcome.channel", got)
        self.assertIn(f"mdset:mo:s{G1}:~welcome.notes", got)
        for hidden in ("prompt", "daily_cap", "max_age_hours"):
            self.assertFalse(any(hidden in c for c in got), hidden)
        self.assertIn("built-in for servers", text(r))     # not safe in new
        # channel picker: a channel select inside that server, a string
        # select of its text channels from a DM
        r = self.h(click(OWNER, f"mdset:f:s{G1}:~welcome.channel", None, G1,
                         "0"))
        self.assertEqual(find(r, f"mdset:cs:s{G1}")["type"], 8)
        r = self.owner(f"mdset:f:s{G1}:~welcome.channel")
        c = find(r, f"mdset:cs:s{G1}")
        self.assertEqual(c["type"], 3)
        self.assertEqual(sorted(option_values(c)), sorted([CH, CH2]))
        self.assertTrue(find(r, f"mdset:v:s{G1}:~welcome.channel:system"))
        r = self.owner(f"mdset:pls:s{G1}", ["media"])
        self.assertIn(f"mdset:f:s{G1}:~media.who", ids(r))
        self.assertIn(f"mdset:mo:s{G1}:~media.per_day", ids(r))

    def test_custom_ids_fit_with_longest_ids(self):
        r = self.owner(f"mdset:f:c{G1}.{CH}:r")
        for c in ids(r):
            self.assertLessEqual(len(c), 100)
        self.assertLessEqual(len(f"mdset:pu:c{G1}.{CH}:~welcome.enabled:+"),
                             100)


class Writes(Base):
    def test_select_writes_overlay_key(self):
        r = self.owner(f"mdset:s:s{G1}:r", ["off"])
        self.assertEqual(r["type"], 7)
        self.assertEqual(self.ctx.writes,
                         [(f'servers."{G1}".who.replies', "off", OWNER)])
        self.assertIn("**Saved.** Replies in Lounge is now off", text(r))
        self.assertIn("changed in Discord by <@" + OWNER, text(r))
        self.assertEqual(self.ctx.value("who.replies", MS.Scope.server(G1)),
                         "off")
        self.assertIn(f"mdset:r:s{G1}:r", ids(r))          # Reset offered

    def test_toggle_and_number_and_text(self):
        self.owner("mdset:v:e:oc:off")
        self.assertEqual(self.ctx.writes[-1],
                         ("everywhere.abilities.owner_commands", "off",
                          OWNER))
        r = self.owner("mdset:mo:e:pd")
        self.assertEqual(r["type"], 9)
        self.assertEqual(r["data"]["custom_id"], "mdset:ms:e:pd")
        n = len(self.ctx.writes)
        for bad in ("abc", "-3", "100001", "", "1.5"):
            r = self.h(submit(OWNER, "mdset:ms:e:pd", bad))
            self.assertEqual(r["type"], 4, bad)
            self.assertIn("Not saved", text(r))
        self.assertEqual(len(self.ctx.writes), n)
        r = self.h(submit(OWNER, "mdset:ms:e:pd", " 1,500 "))
        self.assertEqual(r["type"], 7)
        self.assertEqual(self.ctx.writes[-1],
                         ("everywhere.limits.per_day", 1500, OWNER))
        self.h(submit(OWNER, "mdset:ms:e:pd", "None", shape="row"))
        self.assertIsNone(self.ctx.value("limits.per_day",
                                         MS.Scope.everywhere()))
        # number modal prefilled with the value set here
        self.owner("mdset:v:e:pd:40")
        r = self.owner("mdset:mo:e:pd")
        self.assertEqual(r["data"]["components"][0]["component"]["value"],
                         "40")
        # text: channel notes, prefilled; inherited shown as placeholder
        self.owner(f"mdset:v:s{G1}:ln:brief")
        r = self.h(submit(OWNER, f"mdset:ms:s{G1}:n", "Be kind."))
        self.assertEqual(self.ctx.writes[-1],
                         (f'servers."{G1}".style.notes', "Be kind.", OWNER))
        r = self.h(submit(OWNER, f"mdset:ms:c{G1}.{CH}:n", "Long ok."))
        self.assertEqual(self.ctx.writes[-1],
                         (f'servers."{G1}".channels."{CH}".notes', "Long ok.",
                          OWNER))
        r = self.owner(f"mdset:mo:c{G1}.{CH}:n")
        ti = r["data"]["components"][0]["component"]
        self.assertEqual(ti["value"], "Long ok.")
        self.assertEqual(ti["placeholder"], "Inherited: Be kind.")
        self.assertEqual(ti["style"], 2)
        r = self.h(submit(OWNER, "mdset:ms:e:n", "x" * 501))
        self.assertIn("too long", text(r))
        # presence status (bot-wide)
        self.h(submit(OWNER, "mdset:ms:e:ps", "Thinking about tea"))
        self.assertEqual(self.ctx.writes[-1],
                         ("presence.status", "Thinking about tea", OWNER))

    def test_people(self):
        r = self.owner(f"mdset:f:s{G1}:al")
        self.assertEqual(find(r, f"mdset:pu:s{G1}:al")["type"], 5)
        self.assertEqual(find(r, f"mdset:pr:s{G1}:al")["type"], 6)
        self.owner(f"mdset:pu:s{G1}:al", [U1, U2])
        self.assertEqual(self.ctx.writes[-1],
                         (f'servers."{G1}".who.allow', [U1, U2], OWNER))
        r = self.owner(f"mdset:pr:s{G1}:al", [ROLE])
        self.assertEqual(self.ctx.writes[-1][1], [U1, U2, "role:" + ROLE])
        self.assertEqual(len(find(r, f"mdset:pu:s{G1}:al")
                             ["default_values"]), 2)
        self.assertIn(f"<@&{ROLE}>", text(r))
        r = self.owner(f"mdset:pu:s{G1}:al", [])
        self.assertEqual(self.ctx.writes[-1][1], ["role:" + ROLE])
        n = len(self.ctx.writes)
        self.assertIn("at least one", text(self.owner(
            f"mdset:pr:s{G1}:al", [])))
        self.assertEqual(len(self.ctx.writes), n)
        self.owner("mdset:pu:e:ig", [U1])
        self.assertEqual(self.ctx.writes[-1],
                         ("everywhere.who.ignore", [U1], OWNER))
        self.owner("mdset:v:e:ig:_")
        self.assertEqual(self.ctx.writes[-1][1], [])
        # media.who: an emptied list means the owners
        self.owner(f"mdset:pu:s{G1}:~media.who", [U1])
        self.owner(f"mdset:pu:s{G1}:~media.who", [])
        self.assertEqual(self.ctx.writes[-1],
                         (f'servers."{G1}".plugins.media.who', "owners",
                          OWNER))

    def test_channel_setting(self):
        self.owner(f"mdset:cs:s{G1}:~welcome.channel", [CH2])
        self.assertEqual(self.ctx.writes[-1],
                         (f'servers."{G1}".plugins.welcome.channel', CH2,
                          OWNER))
        self.owner(f"mdset:v:s{G1}:~welcome.channel:off")
        self.assertEqual(self.ctx.writes[-1][1], "off")

    def test_reset_writes_inherit(self):
        self.owner(f"mdset:s:s{G1}:ln", ["detailed"])
        r = self.owner(f"mdset:r:s{G1}:ln")
        self.assertEqual(self.ctx.resets,
                         [(f'servers."{G1}".style.length', OWNER)])
        self.assertNotIn(f"mdset:r:s{G1}:ln", ids(r))
        self.assertEqual(self.ctx.value("style.length", MS.Scope.server(G1)),
                         "normal")
        # a file value reset from Discord: the server inherits all_servers
        r = self.owner(f"mdset:r:s{G1}:r")
        self.assertEqual(self.ctx.value("who.replies", MS.Scope.server(G1)),
                         "when-addressed")

    def test_undo(self):
        self.assertIn("Nothing to undo", text(self.owner(f"mdset:u:s{G1}")))
        self.owner(f"mdset:s:s{G1}:ln", ["brief"])
        self.owner(f"mdset:s:s{G1}:ln", ["detailed"])
        self.owner(f"mdset:s:s{G1}:r", ["off"])
        self.owner("mdset:s:e:ln", ["brief"])            # another scope
        sc = MS.Scope.server(G1)
        r = self.owner(f"mdset:u:s{G1}")
        # back to conversational in a 1,240-member server: confirmed first
        self.assertIn("Are you sure", text(r))
        r = self.owner(find(r, "mdset:yu:")["custom_id"])
        self.assertIn("**Undone.** Replies", text(r))
        self.assertEqual(self.ctx.value("who.replies", sc), "conversational")
        self.owner(f"mdset:u:s{G1}")
        self.assertEqual(self.ctx.value("style.length", sc), "brief")
        self.owner(f"mdset:u:s{G1}")
        # back to inheriting everywhere's brief
        st = MS.load(self.ctx.cfg, PLUGINS)
        self.assertFalse(st.layer(("servers", G1), "style.length")[0])
        self.assertIn("Nothing to undo", text(self.owner(f"mdset:u:s{G1}")))
        self.assertEqual(self.ctx.value("style.length",
                                        MS.Scope.everywhere()), "brief")
        self.assertEqual([u[0] for u in self.ctx.undos],
                         [f'servers."{G1}"'] * 3)

    def test_undo_everywhere_covers_bot_wide(self):
        self.owner("mdset:v:e:un:off")
        self.owner("mdset:u:e")
        self.assertTrue(MS.load(self.ctx.cfg, PLUGINS).updates_notify)

    def test_why(self):
        self.owner(f"mdset:s:s{G1}:ln", ["brief"])
        r = self.owner(f"mdset:w:s{G1}:who")
        self.assertEqual(r["type"], 4)
        self.assertEqual(r["data"]["flags"], 64)
        t = text(r)
        self.assertIn("**Replies** = conversational", t)
        self.assertIn("built-in: conversational → All servers (config.yaml):"
                      " when-addressed → Lounge (config.yaml): "
                      "conversational", t)
        r = self.owner(f"mdset:w:s{G1}")
        self.assertIn("Lounge (Discord, by <@", text(r))
        self.assertLessEqual(len(text(r)), 2000)
        r = self.owner(f"mdset:w:c{G1}.{CH}")
        self.assertIn("**House rules**", text(r))
        self.assertNotIn("**Reply length**", text(r))

    def test_pause_and_resume_everywhere(self):
        r = self.owner("mdset:pz:e:h")
        self.assertIn("**Paused** everywhere", text(r))
        self.assertTrue(find(r, "mdset:rs:e:h"))
        self.assertEqual(self.ctx.value("who.replies", MS.Scope.dm()), "off")
        self.assertEqual(self.ctx.value("who.replies", MS.Scope.server(G2)),
                         "off")
        r = self.owner("mdset:rs:e:h")
        self.assertTrue(find(r, "mdset:pz:e:h"))
        self.assertEqual(self.ctx.value("who.replies", MS.Scope.dm()),
                         "conversational")
        self.assertEqual(self.ctx.value("who.replies", MS.Scope.server(G2)),
                         "when-addressed")

    def test_pause_here_and_resume_restores_previous(self):
        self.owner(f"mdset:s:s{G2}:r", ["when-addressed"])
        r = self.owner(f"mdset:pz:s{G2}")
        self.assertTrue(find(r, f"mdset:rs:s{G2}"))
        self.owner(f"mdset:rs:s{G2}")
        self.assertEqual(self.ctx.value("who.replies", MS.Scope.server(G2)),
                         "when-addressed")
        self.assertEqual(self.ctx.writes[-1][1], "when-addressed")

    def test_works_with_ctx_reload(self):
        self.ctx = FakeCtx(with_reload=True)
        r = self.owner(f"mdset:s:s{G1}:r", ["off"])
        self.assertIn("now off", text(r))


class Risky(Base):
    def confirm(self, cid, values=None, expect=None):
        n = len(self.ctx.changed)
        r = self.owner(cid, values)
        self.assertEqual(r["type"], 7, cid)
        self.assertIn("**Are you sure?**", text(r))
        yes = [c for c in components(r) if c.get("style") == 4]
        self.assertEqual(len(yes), 1)
        self.assertEqual(yes[0]["label"], "Yes, change it")
        self.assertEqual(len(self.ctx.changed), n, "wrote before confirming")
        if expect:
            self.assertIn(expect, text(r))
        r2 = self.owner(yes[0]["custom_id"])
        self.assertIn("Saved", text(r2))
        self.assertEqual(len(self.ctx.changed), n + 1)
        return r

    def direct(self, cid, values=None):
        r = self.owner(cid, values)
        self.assertNotIn("Are you sure", text(r), cid)
        self.assertIn("Saved", text(r), cid)

    def test_memory(self):
        self.confirm(f"mdset:s:s{G1}:m", ["everything"],
                     "**This is a big server (1,240 members).**")
        self.direct(f"mdset:s:s{G1}:m", ["nothing"])      # stricter
        r = self.confirm(f"mdset:s:s{G3}:m", ["everything"],
                         "People in *Tiny* (12 members")
        self.assertNotIn("big server", text(r))
        self.confirm("mdset:s:d:m", ["everything"])
        # a reset that loosens is confirmed too
        self.confirm(f"mdset:r:s{G1}:m")

    def test_dms_from(self):
        self.confirm("mdset:s:d:df", ["anyone"], "Anyone on Discord")
        self.direct("mdset:s:d:df", ["shared-servers"])  # stricter now
        self.direct("mdset:s:d:df", ["owners"])
        self.confirm("mdset:s:d:df", ["shared-servers"])

    def test_allow_everyone_in_restricted_server(self):
        self.direct(f"mdset:pu:s{G1}:al", [U1])
        self.confirm(f"mdset:v:s{G1}:al:everyone", None,
                     "Everyone in *Lounge* (1,240 members)")

    def test_media(self):
        self.confirm(f"mdset:v:s{G1}:~media.who:everyone")
        self.direct(f"mdset:v:s{G1}:~media.who:owners")
        self.direct(f"mdset:v:s{G1}:~media.per_day:5")
        self.direct(f"mdset:v:s{G1}:~media.per_day:20")
        n = len(self.ctx.changed)
        r = self.h(submit(OWNER, f"mdset:ms:s{G1}:~media.per_day", "30"))
        self.assertIn("Are you sure", text(r))
        self.assertEqual(len(self.ctx.changed), n)
        self.confirm(f"mdset:v:s{G1}:~media.per_day:none")

    def test_bot_exchange_limit(self):
        self.direct("mdset:v:e:bx:5")       # none -> a number: stricter
        self.direct("mdset:v:e:bx:10")
        self.confirm("mdset:v:e:bx:11")
        self.confirm("mdset:v:e:bx:none")
        self.direct("mdset:v:e:bx:40")      # none -> 40: stricter

    def test_conversational_in_big_server(self):
        self.direct(f"mdset:s:s{G2}:r", ["off"])
        self.confirm(f"mdset:s:s{G2}:r", ["conversational"], "5,000 members")
        self.direct(f"mdset:s:s{G3}:r", ["conversational"])
        self.direct(f"mdset:s:c{G2}.{CH_G2}:r", ["off"])
        self.confirm(f"mdset:s:c{G2}.{CH_G2}:r", ["conversational"])

    def test_undo_that_loosens_is_confirmed(self):
        self.direct(f"mdset:s:s{G1}:m", ["nothing"])
        r = self.owner(f"mdset:u:s{G1}")
        self.assertIn("Are you sure", text(r))
        self.assertEqual(self.ctx.undos, [])
        yes = find(r, "mdset:yu:")
        self.owner(yes["custom_id"])
        self.assertEqual(len(self.ctx.undos), 1)

    def test_forged_confirm_from_non_owner(self):
        for who in (ADMIN, STRANGER):
            r = self.h(click(who, f"mdset:y:s{G1}:m:everything", None, G1,
                             MANAGE))
            self.assertRefused(r)
        self.assertEqual(self.ctx.changed, [])


class Admin(Base):
    def test_can_quiet(self):
        r = self.admin(f"mdset:d:s{G1}:who")
        sel = find(r, f"mdset:s:s{G1}:r")
        self.assertEqual(option_values(sel), ["off", "when-addressed"])
        self.assertIsNone(find(r, f"mdset:f:s{G1}:al"))
        self.assertTrue(find(r, f"mdset:f:s{G1}:ig"))
        r = self.admin(f"mdset:s:s{G1}:r", ["when-addressed"])
        self.assertIn("Saved", text(r))
        self.assertEqual(self.ctx.notified,
                         [(G1, ADMIN, f'servers."{G1}".who.replies',
                           "conversational", "when-addressed")])
        self.admin(f"mdset:s:c{G1}.{CH}:r", ["off"])
        self.assertEqual(self.ctx.writes[-1][0],
                         f'servers."{G1}".channels."{CH}".replies')
        self.admin(f"mdset:pz:s{G1}")
        self.assertEqual(self.ctx.value("who.replies", MS.Scope.server(G1)),
                         "off")
        self.assertEqual(len(self.ctx.notified), 3)

    def test_cannot_loosen_even_with_forged_ids(self):
        self.owner(f"mdset:s:s{G1}:r", ["off"])
        self.owner(f"mdset:v:s{G1}:~welcome.enabled:on")
        self.owner(f"mdset:pu:s{G1}:ig", [U1, U2])
        before = list(self.ctx.changed)
        forged = [
            (f"mdset:s:s{G1}:r", ["when-addressed"]),     # off -> louder
            (f"mdset:s:s{G1}:r", ["conversational"]),
            (f"mdset:v:s{G1}:r:conversational", None),
            (f"mdset:y:s{G1}:r:conversational", None),
            (f"mdset:r:s{G1}:r", None),                   # reset = louder
            (f"mdset:rx:s{G1}", ["r"]),
            (f"mdset:rs:s{G1}", None),                    # resume = louder
            (f"mdset:v:s{G1}:~welcome.enabled:on", None),
            (f"mdset:v:s{G1}:~media.enabled:on", None),
            (f"mdset:pu:s{G1}:ig", [U1]),                 # drops U2
            (f"mdset:v:s{G1}:ig:_", None),
            (f"mdset:u:s{G1}", None),                     # undo = louder
            (f"mdset:s:s{G1}:m", ["everything"]),         # privacy
            (f"mdset:s:s{G1}:m", ["nothing"]),            # even stricter
            (f"mdset:v:s{G1}:pd:none", None),             # limits
            (f"mdset:v:s{G1}:pd:1", None),
            (f"mdset:v:s{G1}:al:everyone", None),
            (f"mdset:pu:s{G1}:al", [U1]),
            (f"mdset:s:s{G1}:ln", ["brief"]),
            (f"mdset:mo:s{G1}:n", None),
            (f"mdset:v:s{G1}:oc:off", None),
            (f"mdset:cs:s{G1}:~welcome.channel", [CH]),
            (f"mdset:s:s{G2}:r", ["off"]),                # other server
            (f"mdset:p:s{G2}", None),
            (f"mdset:s:c{G2}.{CH_G2}:r", ["off"]),
            (f"mdset:s:c{G1}.{CH_G2}:r", ["off"]),        # not G1's channel
            ("mdset:s:e:r", ["off"]), ("mdset:s:a:r", ["off"]),
            ("mdset:s:d:r", ["off"]), ("mdset:p:e", None),
            ("mdset:h:0", None), ("mdset:go", ["e"]),
            ("mdset:pz:e", None), (f"mdset:w:s{G2}", None),
            (f"mdset:f:s{G1}:m", None),
        ]
        for c, vals in forged:
            r = self.admin(c, vals)
            self.assertEqual(r["type"], 4, c)
            self.assertNotIn("Saved", text(r), c)
        self.assertEqual(self.ctx.changed, before)
        self.assertEqual(self.ctx.notified, [])
        # a modal submit forged for a privacy setting
        r = self.h(submit(ADMIN, f"mdset:ms:s{G1}:pd", "1", G1, MANAGE))
        self.assertEqual(r["type"], 4)
        self.assertEqual(self.ctx.changed, before)
        # an admin of another server has no power here
        r = self.h(click(ADMIN, f"mdset:s:s{G1}:r", ["off"], G2, MANAGE))
        self.assertRefused(r)

    def test_ignore_add_only_and_plugins_off(self):
        self.owner(f"mdset:pu:s{G1}:ig", [U1])
        r = self.admin(f"mdset:f:s{G1}:ig")
        us = find(r, f"mdset:pu:s{G1}:ig:+")
        self.assertIsNotNone(us)
        self.assertNotIn("default_values", us)
        self.assertIsNone(find(r, f"mdset:v:s{G1}:ig:_"))  # no "Nobody"
        self.admin(f"mdset:pu:s{G1}:ig:+", [U2])
        self.assertEqual(self.ctx.writes[-1],
                         (f'servers."{G1}".who.ignore', [U1, U2], ADMIN))
        self.admin(f"mdset:pr:s{G1}:ig:+", [ROLE])
        self.assertEqual(self.ctx.writes[-1][1], [U1, U2, "role:" + ROLE])
        # plugins: only "turn off" is offered, and only when on
        r = self.admin(f"mdset:pls:s{G1}", ["welcome"])
        self.assertIsNone(find(r, f"mdset:v:s{G1}:~welcome.enabled"))
        self.owner(f"mdset:v:s{G1}:~welcome.enabled:on")
        r = self.admin(f"mdset:pls:s{G1}", ["welcome"])
        self.assertTrue(find(r, f"mdset:v:s{G1}:~welcome.enabled:off"))
        self.assertIsNone(find(r, f"mdset:f:s{G1}:~welcome.channel"))
        self.admin(f"mdset:v:s{G1}:~welcome.enabled:off")
        self.assertFalse(self.ctx.value("plugins.welcome.enabled",
                                        MS.Scope.server(G1)))

    def test_admin_undo_and_reset_when_quieter(self):
        self.admin(f"mdset:s:s{G1}:r", ["when-addressed"])
        self.owner(f"mdset:s:s{G1}:r", ["off"])
        # undoing the owner's "off" would be louder: refused
        r = self.admin(f"mdset:u:s{G1}")
        self.assertIn("louder", text(r))
        # the admin's own change to a channel, undone (it was quieter
        # before? no: undo restores louder) -> refused; a reset that is
        # quieter is fine
        self.owner(f"mdset:s:c{G1}.{CH}:r", ["conversational"])
        r = self.admin(f"mdset:r:c{G1}.{CH}:r")
        self.assertIn("Saved", text(r))
        self.assertEqual(self.ctx.value(
            "who.replies", MS.Scope.server(G1, channel_id=CH,
                                           category_id=CAT)), "off")


class Errors(Base):
    def test_errors_become_an_ephemeral_apology(self):
        class Boom(FakeCtx):
            def guilds(self):
                raise RuntimeError("cache exploded")
        ctx = Boom()
        r = UI.handle(cmd(OWNER), ctx)
        validate(self, r)
        self.assertEqual(r["type"], 4)
        self.assertEqual(r["data"]["flags"], 64)
        self.assertIn("Sorry", text(r))
        self.assertTrue(any("cache exploded" in m for m in ctx.logs))
        for bad in ({}, {"type": 3}, {"type": 3, "data": None},
                    {"type": 2, "data": {"name": "settings"}},
                    {"type": 5, "data": {"custom_id": "mdset:ms:e:pd"},
                     "user": {"id": OWNER}},
                    {"type": 3, "data": {"custom_id": "mdset:s:e:r",
                                         "values": ["off"]},
                     "member": {"user": {"id": ADMIN},
                                "permissions": "lots"}, "guild_id": G1},
                    None):
            r = UI.handle(bad, self.ctx)
            validate(self, r, repr(bad))
            self.assertEqual(r["type"], 4)

    def test_write_failure_is_reported(self):
        class Fails(FakeCtx):
            def write(self, key, value, by):
                raise MS.SettingsError(f"{key}: disk full")
        self.ctx = Fails()
        r = self.owner(f"mdset:s:s{G1}:r", ["off"])
        self.assertIn("Not saved", text(r))


class SettingsHelpers(unittest.TestCase):
    def test_loosens(self):
        L = MS.loosens
        self.assertTrue(L("who.replies", "off", "when-addressed"))
        self.assertFalse(L("who.replies", "conversational", "off"))
        self.assertTrue(L("privacy.memory", "nothing", "public-only"))
        self.assertFalse(L("privacy.memory", "everything", "nothing"))
        self.assertTrue(L("who.dms_from", "owners", "anyone"))
        self.assertTrue(L("limits.per_day", 5, None))
        self.assertTrue(L("limits.per_day", 5, 6))
        self.assertFalse(L("limits.per_day", None, 6))
        self.assertTrue(L("who.ignore", [U1, U2], [U1]))
        self.assertFalse(L("who.ignore", [U1], [U1, U2]))
        self.assertTrue(L("who.allow", [U1], "everyone"))
        self.assertTrue(L("who.allow", [U1], [U1, U2]))
        self.assertFalse(L("who.allow", "everyone", [U1]))
        self.assertTrue(L("abilities.owner_commands", False, True))
        self.assertFalse(L("abilities.owner_commands", True, False))
        self.assertIsNone(L("style.length", "brief", "detailed"))
        self.assertIsNone(L("style.notes", "a", "b"))
        st = MS.load(FakeCtx().cfg, PLUGINS)
        self.assertTrue(L("plugins.media.who", "owners", [U1],
                          st.spec("plugins.media.who")))
        self.assertTrue(L("plugins.welcome.enabled", False, True,
                          st.spec("plugins.welcome.enabled")))

    def test_check_settable(self):
        st = MS.load(FakeCtx().cfg, PLUGINS)
        ok = [(("everywhere",), "style.length"), (("top",), "updates.notify"),
              (("servers", G1, "channels", CH), "plugins.welcome.enabled"),
              (("all_servers",), "who.replies")]
        bad = [(("everywhere",), "privacy.never_share"),
               (("everywhere",), "presence.status"),
               (("top",), "presence.enabled"),
               (("everywhere",), "plugins.welcome.prompt"),
               (("servers", G1, "channels", CH), "plugins.welcome.channel"),
               (("all_servers",), "style.length"),
               (("servers", "abc"), "who.replies"),
               (("dms",), "who.bot_exchange_limit")]
        for p, k in ok:
            MS.check_settable(st, p, k)
        for p, k in bad:
            with self.assertRaises(MS.SettingsError, msg=(p, k)):
                MS.check_settable(st, p, k)

    def test_value_without_and_description(self):
        st = MS.load(FakeCtx().cfg, PLUGINS)
        sc = MS.Scope.server(G1)
        self.assertEqual(st.value_without(sc, "who.replies",
                                          ("servers", G1)),
                         "when-addressed")
        self.assertEqual(st.value_without(MS.Scope.everywhere(),
                                          "updates.notify", ("top",)), True)
        self.assertIn("Where welcomes are posted",
                      st.spec("plugins.welcome.channel").description)

    def test_undo_api(self):
        ctx = FakeCtx()
        cfg = ctx.cfg
        self.assertIsNone(MS.undo(cfg, "dms", OWNER, PLUGINS))
        MS.set_value(cfg, "dms.style.length", "brief", OWNER, PLUGINS)
        MS.set_value(cfg, f'servers."{G1}".who.replies', "off", OWNER,
                     PLUGINS)
        st = MS.load(cfg, PLUGINS)
        self.assertEqual(MS.undo_candidate(st, f'servers."{G1}"'),
                         (f'servers."{G1}".who.replies', ("servers", G1),
                          "who.replies", "conversational"))
        self.assertEqual(MS.undo(cfg, f'servers."{G1}"', OWNER, PLUGINS),
                         f'servers."{G1}".who.replies')
        st = MS.load(cfg, PLUGINS)
        # equal to the file value: the overlay entry is simply dropped
        self.assertNotIn(f'servers."{G1}".who.replies',
                         st.overlay_doc["values"])
        self.assertEqual(st.get("who.replies", MS.Scope.server(G1)),
                         "conversational")
        hist = st.overlay_doc["history"]
        self.assertTrue(hist[-1]["undo"])
        self.assertTrue(hist[-2]["undone"])
        self.assertIsNone(MS.undo(cfg, f'servers."{G1}"', OWNER, PLUGINS))
        self.assertEqual(MS.undo(cfg, "dms", OWNER, PLUGINS),
                         "dms.style.length")
        self.assertEqual(MS.load(cfg, PLUGINS).get("style.length",
                                                   MS.Scope.dm()), "normal")
        # a change the file has since overridden is not undone
        MS.set_value(cfg, "everywhere.style.length", "brief", OWNER, PLUGINS)
        with open(cfg, "a") as f:
            f.write("dms:\n  style:\n    length: detailed\n")
        st = MS.load(cfg, PLUGINS)
        st.commit_retirements()
        self.assertEqual(MS.undo(cfg, "dms", OWNER, PLUGINS), None)
        self.assertEqual(MS.scope_prefix(("servers", G1, "channels", CH)),
                         f'servers."{G1}".channels."{CH}"')
        self.assertEqual(MS.parse_scope_prefix(
            f'servers."{G1}".channels."{CH}"'),
            ("servers", G1, "channels", CH))


if __name__ == "__main__":
    unittest.main(verbosity=1)
