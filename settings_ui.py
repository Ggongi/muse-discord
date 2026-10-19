#!/usr/bin/env python3
"""/settings in Discord: the pages, drawers and writes, as one pure
function of an interaction and a Ctx (see handle). monitor.py owns the
plumbing (registering the command, routing "mdset:" interactions here,
building the Ctx, answering Discord); this module never touches the
network, so it answers well inside Discord's 3 seconds.

Every page is an ephemeral message edited in place, built from classic
components (action rows: at most 5 per message, 5 buttons or one select
per row). Modals use Label-wrapped text inputs; a submit is parsed in
either the Label or the older action-row shape.

custom_id grammar ("mdset:" + colon-separated parts, <= 100 chars):

    go | h:<page>                    Home (scope picker) and its pages
    p:<scope>                        a scope page
    d:<scope>:<drawer>               a drawer (who how may priv lim plug bot)
    pl:<scope>:<plugin> | pls:<scope>  a plugin's controls / pick a plugin
    cp:<scope>                       pick a channel or category (server page)
    f:<scope>:<key>                  one setting on its own (people, channel)
    s: v: r: rx: pu: pr: cs:         writes (select, value, reset, ...)
    mo:<scope>:<key> | ms:...        open / submit a modal
    y:<scope>:<key>:<value> | yu:    confirm a risky change / undo
    u: pz: rs: w:                    undo, pause, resume, why
    open:server:<guild id>           the owners' join DM "Configure" button

scope: e (everywhere), d (dms), a (all servers), s<guild id>,
c<guild id>.<channel or category id>. key: a short code for core keys,
~<plugin>.<key> for plugin keys. Nothing in a custom_id is trusted: who
may do what, and where, is checked again on every interaction, and every
write is checked again for direction (a server admin's write must never
loosen anything, whatever the custom_id says).
"""
import math
import re
import traceback
from datetime import datetime
from typing import Protocol

import muse_settings as ms

PREFIX = "mdset"
COMMAND = "settings"
EPHEMERAL = 1 << 6
MANAGE_GUILD = 1 << 5
ADMINISTRATOR = 1 << 3
CUSTOM_ID_MAX = 100
CONTENT_MAX = 2000
SERVERS_PER_PAGE = 22        # + Everywhere, DMs, All servers = 25 options
BIG_FOR_MEMORY = ms.BIG_FOR_MEMORY   # members: warn harder about memory
BIG_FOR_CONVERSATIONAL = ms.BIG_FOR_CONVERSATIONAL
MEDIA_PER_DAY_RISKY = ms.MEDIA_PER_DAY_RISKY
BOT_EXCHANGE_RISKY = ms.BOT_EXCHANGE_RISKY
THREAD_TYPES = (10, 11, 12)
CATEGORY = 4
PICKABLE_CHANNELS = (0, 2, 4, 5, 13, 15, 16)
TEXT_CHANNELS = (0, 5)

REFUSAL = "Only the bot's owners can change its settings."
APOLOGY = ("Sorry, something went wrong with /settings. Nothing was "
           "changed by this click; try again in a moment.")
STALE = "That control is no longer active. Run /settings again."
ADMIN_ONLY_QUIETER = ("Server admins can make the bot quieter here, never "
                      "louder. Ask one of the bot's owners for that.")


class Ctx(Protocol):
    """What the monitor provides (see the module docstring)."""
    settings: "ms.Settings"   # current, already reloaded
    owners: set
    bot_id: str
    now: float

    def guilds(self) -> list: ...           # [{"id","name","member_count"}]
    def channels(self, guild_id) -> list: ...  # [{"id","name","type","parent_id"}]
    def write(self, key: str, value, by: str) -> None: ...
    def reset(self, key: str, by: str) -> None: ...
    def undo(self, scope_prefix: str, by: str): ...  # -> key | None
    def notify_owners_admin_change(self, guild_id, user_id, key, old,
                                   new) -> None: ...
    def log(self, msg: str) -> None: ...


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------
LABELS = {
    "who.replies": ("Replies", "When it answers."),
    "who.dms_from": ("DMs from", "Whose direct messages it answers."),
    "who.allow": ("Allowed people", "Only these people and roles get "
                  "answers (owners always do)."),
    "who.ignore": ("Ignored people", "Never answers these (wins over "
                   "allowed)."),
    "who.bots": ("Other bots", "Whether it answers other bots."),
    "who.bot_exchange_limit": ("Bot exchange limit", "Replies in a row to "
                               "bots in one channel without a human; none "
                               "= no limit."),
    "style.length": ("Reply length", "How long replies tend to be."),
    "style.notes": ("House rules", "Free-text notes for the Muse here, "
                    "e.g. \"no swearing\"."),
    "abilities.owner_commands": ("Owner commands", "Pin, post and make "
                                 "threads when an owner asks."),
    "privacy.memory": ("Memory in replies", "How much of your saved "
                       "memory replies may draw on."),
    "privacy.suspicious": ("Suspicious messages", "What happens to likely "
                           "prompt injection."),
    "limits.per_day": ("Replies per day", "Total replies per day, counted "
                       "where it is set; none = no limit."),
    "limits.per_person_per_day": ("Replies per person per day",
                                  "Per non-owner, per day; none = no "
                                  "limit."),
    "presence.status": ("Status line", "The line under the bot's name; "
                        "empty for none."),
    "presence.kind": ("Status kind", "How Discord shows the status line."),
    "updates.notify": ("Update notices", "DM owners when a new version is "
                       "out."),
}
OPTION_HELP = {
    ("who.replies", "off"): "Never answers here",
    ("who.replies", "when-addressed"): "Only when @mentioned or replied to",
    ("who.replies", "conversational"): "When addressed, plus that "
                                       "person's follow-ups for 2 minutes",
    ("who.dms_from", "owners"): "Only the owners",
    ("who.dms_from", "shared-servers"): "Anyone who shares a server with "
                                        "the bot",
    ("who.dms_from", "anyone"): "Anyone on Discord",
    ("who.bots", "ignore"): "Never answers bots",
    ("who.bots", "when-addressed"): "Answers bots that @mention or reply "
                                    "to it",
    ("style.length", "brief"): "Short replies",
    ("style.length", "normal"): "The usual",
    ("style.length", "detailed"): "Longer, fuller replies",
    ("privacy.memory", "everything"): "May draw on all your saved memory",
    ("privacy.memory", "public-only"): "Only public facts: name, "
                                       "interests, public projects",
    ("privacy.memory", "nothing"): "Writes as if it had no memory of you",
    ("privacy.suspicious", "hold-for-review"): "Held for an owner to "
                                               "review first",
    ("privacy.suspicious", "ignore"): "Dropped silently",
}
DRAWERS = {
    "who": ("Who it talks to", ("who.replies", "who.dms_from", "who.allow",
                                "who.ignore", "who.bots",
                                "who.bot_exchange_limit")),
    "how": ("How it talks", ("style.length", "style.notes")),
    "may": ("What it may do", ("abilities.owner_commands",)),
    "priv": ("Privacy", ("privacy.memory", "privacy.suspicious")),
    "lim": ("Limits", ("limits.per_day", "limits.per_person_per_day")),
    "bot": ("Bot-wide", ("presence.status", "presence.kind",
                         "updates.notify")),
}
DRAWER_ORDER = ("who", "how", "may", "priv", "lim")
CHANNEL_KEYS = ("who.replies", "style.notes")
# What a server admin may touch (restrict-only, their own server).
ADMIN_REPLIES = ("off", "when-addressed")

CORE_CODES = {
    "who.replies": "r", "who.dms_from": "df", "who.allow": "al",
    "who.ignore": "ig", "who.bots": "b", "who.bot_exchange_limit": "bx",
    "style.length": "ln", "style.notes": "n",
    "abilities.owner_commands": "oc", "privacy.memory": "m",
    "privacy.suspicious": "su", "limits.per_day": "pd",
    "limits.per_person_per_day": "pp", "presence.status": "ps",
    "presence.kind": "pk", "updates.notify": "un",
}
CODE_KEYS = {v: k for k, v in CORE_CODES.items()}
_PLUGIN_CODE = re.compile(r"~([A-Za-z0-9_\-]{1,40})\.([A-Za-z0-9_\-]{1,40})")
_TOKEN = re.compile(r"(?:([eda])|s(\d{1,25})|c(\d{1,25})\.(\d{1,25}))")
_ID = re.compile(r"\d{1,25}")


def kcode(key):
    if key in CORE_CODES:
        return CORE_CODES[key]
    if key.startswith("plugins."):
        code = "~" + key[len("plugins."):]
        if _PLUGIN_CODE.fullmatch(code):
            return code
    return None


def kdecode(code):
    if code in CODE_KEYS:
        return CODE_KEYS[code]
    if _PLUGIN_CODE.fullmatch(code or ""):
        return "plugins." + code[1:]
    return None


def label(key):
    if key in LABELS:
        return LABELS[key][0]
    if key.startswith("plugins."):
        _p, pid, k = key.split(".", 2)
        name = pid.replace("_", " ").capitalize()
        return f"{name} plugin" if k == "enabled" \
            else f"{name}: {k.replace('_', ' ')}"
    return key


def help_text(st, key):
    if key in LABELS:
        return LABELS[key][1]
    spec = st.spec(key)
    desc = getattr(spec, "description", None) or ""
    return desc.split(". ")[0].rstrip(".") + "." if desc else ""


def enc(value):
    """A value as a custom_id part (no colons)."""
    if value is None:
        return "none"
    if value is True:
        return "on"
    if value is False:
        return "off"
    if isinstance(value, list):
        if not value:
            return "_"
        return ",".join("r" + v[5:] if str(v).startswith("role:") else str(v)
                        for v in value)
    return str(value)


def dec(spec, s):
    if spec.type == "people" and s not in spec.values:
        if s == "_":
            return []
        return ["role:" + p[1:] if p.startswith("r") else p
                for p in s.split(",")]
    return s


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------
class TooLong(ValueError):
    pass


def cid(*parts):
    s = ":".join([PREFIX] + [str(p) for p in parts])
    if len(s) > CUSTOM_ID_MAX:
        raise TooLong(s)
    return s


def clip(text, n):
    text = str(text)
    return text if len(text) <= n else text[:n - 1] + "…"


def btn(text, custom_id, style=2, disabled=False):
    b = {"type": 2, "style": style, "label": clip(text, 80),
         "custom_id": custom_id}
    if disabled:
        b["disabled"] = True
    return b


def opt(text, value, description=None, default=False):
    o = {"label": clip(text, 100), "value": clip(value, 100)}
    if description:
        o["description"] = clip(description, 100)
    if default:
        o["default"] = True
    return o


def sel(custom_id, options, placeholder):
    return {"type": 3, "custom_id": custom_id, "options": options[:25],
            "placeholder": clip(placeholder, 150), "min_values": 1,
            "max_values": 1}


def row(*components):
    return {"type": 1, "components": list(components)}


def message(content, rows, new):
    data = {"content": clip(content, CONTENT_MAX), "components": rows,
            "allowed_mentions": {"parse": []}}
    if new:
        data["flags"] = EPHEMERAL
        return {"type": 4, "data": data}
    return {"type": 7, "data": data}


def notice(text):
    """A new ephemeral message; the page it came from stays as it was."""
    return {"type": 4, "data": {"content": clip(text, CONTENT_MAX),
                                "flags": EPHEMERAL, "components": [],
                                "allowed_mentions": {"parse": []}}}


# ---------------------------------------------------------------------------
# Who and where
# ---------------------------------------------------------------------------
class Who:
    def __init__(self, uid, role, gid=None):
        self.uid, self.role, self.gid = uid, role, gid

    @property
    def owner(self):
        return self.role == "owner"

    @property
    def admin(self):
        return self.role == "admin"


def identify(ix, ctx):
    """Owner (any scope), admin of the interaction's server (Manage Server
    or Administrator in member.permissions), or nobody."""
    user = (ix.get("member") or {}).get("user") or ix.get("user") or {}
    uid = str(user.get("id") or "")
    if uid and uid in {str(o) for o in ctx.owners}:
        return Who(uid, "owner")
    gid = ix.get("guild_id")
    member = ix.get("member")
    if uid and gid and isinstance(member, dict):
        try:
            perms = int(member.get("permissions") or 0)
        except (TypeError, ValueError):
            perms = 0
        if perms & (MANAGE_GUILD | ADMINISTRATOR):
            return Who(uid, "admin", str(gid))
    return Who(uid, None)


class Where:
    """A scope a page shows: everywhere | dms | all_servers | server |
    channel (a channel or a category)."""

    def __init__(self, kind, gid=None, cid_=None):
        self.kind, self.gid, self.cid = kind, gid, cid_

    @property
    def token(self):
        return {"everywhere": "e", "dms": "d", "all_servers": "a"}.get(
            self.kind) or (f"s{self.gid}" if self.kind == "server"
                           else f"c{self.gid}.{self.cid}")

    @property
    def path(self):
        if self.kind == "server":
            return ("servers", self.gid)
        if self.kind == "channel":
            return ("servers", self.gid, "channels", self.cid)
        return (self.kind,)

    @property
    def server(self):
        return Where("server", self.gid)


def parse_where(tok):
    m = _TOKEN.fullmatch(tok or "")
    if not m:
        return None
    if m[1]:
        return Where({"e": "everywhere", "d": "dms",
                      "a": "all_servers"}[m[1]])
    if m[2]:
        return Where("server", m[2])
    return Where("channel", m[3], m[4])


def key_path(where, key):
    return ("top",) if key in ms.TOP else where.path


class Req:
    """One interaction being answered."""

    def __init__(self, ix, ctx):
        self.ix, self.ctx = ix, ctx
        self.st = ctx.settings
        self.who = identify(ix, ctx)
        self.new = False          # answer with a new message, not an edit
        self._guilds = None
        self._chans = {}

    def guilds(self):
        if self._guilds is None:
            out = {}
            for g in self.ctx.guilds() or []:
                if isinstance(g, dict) and g.get("id"):
                    out[str(g["id"])] = g
            self._guilds = out
        return self._guilds

    def channels(self, gid):
        if gid not in self._chans:
            out = {}
            for c in self.ctx.channels(gid) or []:
                if isinstance(c, dict) and c.get("id"):
                    out[str(c["id"])] = c
            self._chans[gid] = out
        return self._chans[gid]

    def reload(self):
        fn = getattr(self.ctx, "reload", None)
        try:
            self.st = fn() if callable(fn) else ms.load(self.st.path)
        except Exception as e:  # keep showing the old values; it was saved
            self.ctx.log(f"settings_ui: reload after a write failed: {e}")

    def scope(self, where):
        if where.kind == "everywhere":
            return ms.Scope.everywhere()
        if where.kind == "dms":
            return ms.Scope.dm()
        if where.kind == "all_servers":
            return ms.Scope("all_servers")
        name = (self.guilds().get(where.gid) or {}).get("name")
        if where.kind == "server":
            return ms.Scope.server(where.gid, name)
        ch = self.channels(where.gid).get(where.cid) or {}
        if ch.get("type") == CATEGORY:
            return ms.Scope.server(where.gid, name, channel_id=where.cid)
        parent = str(ch.get("parent_id") or "") or None
        if parent and (self.channels(where.gid).get(parent) or {}).get(
                "type") != CATEGORY:
            parent = None
        return ms.Scope.server(where.gid, name, channel_id=where.cid,
                               category_id=parent)

    def members(self, where):
        if where.kind not in ("server", "channel"):
            return None
        n = (self.guilds().get(where.gid) or {}).get("member_count")
        return n if isinstance(n, int) else None

    def here_channel(self, gid):
        """(channel id, category id) of where the command was typed (a
        thread counts as its parent channel), when in that server."""
        if str(self.ix.get("guild_id") or "") != gid:
            return None, None
        ch = self.ix.get("channel") or {}
        cid_ = str(self.ix.get("channel_id") or ch.get("id") or "")
        if not _ID.fullmatch(cid_):
            return None, None
        chans = self.channels(gid)
        parent = str(ch.get("parent_id") or "") or None
        if ch.get("type") in THREAD_TYPES and parent:
            cid_ = parent
            parent = None
        info = chans.get(cid_) or {}
        if info.get("type") == CATEGORY:
            return cid_, None
        parent = str(info.get("parent_id") or "") or parent
        if parent and chans and (chans.get(parent) or {}).get(
                "type") != CATEGORY:
            parent = None
        return cid_, parent


# ---------------------------------------------------------------------------
# Values, sources, permissions
# ---------------------------------------------------------------------------
def effective(R, where, key):
    if key in ms.TOP:
        return R.st.top(key)
    return R.st.get(key, R.scope(where))


def here(R, where, key):
    """(set at this scope?, value there)."""
    s, v, _src = R.st.layer(key_path(where, key), key)
    return s, v


def inherited(R, where, key):
    return R.st.value_without(R.scope(where), key, key_path(where, key))


def _date(at):
    try:
        d = datetime.strptime(str(at), "%Y-%m-%dT%H:%M:%SZ")
        return f"{d:%b} {d.day}, {d.year}"
    except ValueError:
        return str(at)


def _own_source(src):
    if not src or src[0] == "built-in":
        return "built-in"
    if src[0] == "file":
        return "set here in config.yaml"
    by = src[1] if len(src) > 1 else None
    who = f"<@{by}>" if by and _ID.fullmatch(str(by)) else "someone"
    return f"changed in Discord by {who} on {_date(src[2])}"


def layer_name(R, where, path, name):
    if path[0] in ("everywhere", "dms", "all_servers"):
        return {"everywhere": "Everywhere", "dms": "DMs",
                "all_servers": "All servers"}[path[0]]
    if path[0] == "builtin_all_servers":
        return "built-in for servers"
    if path[0] == "alias":
        return f"server \"{path[1]}\" (old config)"
    if path[0] == "servers" and len(path) == 2:
        return (R.guilds().get(path[1]) or {}).get("name") or "the server"
    if path[0] == "servers":
        ch = R.channels(path[1]).get(path[3]) or {}
        if ch.get("type") == CATEGORY:
            return f"category {ch.get('name') or path[3]}"
        return f"#{ch.get('name') or path[3]}"
    return name


def source_tag(R, where, key):
    path = key_path(where, key)
    if key in ms.TOP:
        s, _v, src = R.st.layer(path, key)
        return _own_source(src) if s else "built-in"
    _value, rows = R.st.trace(R.scope(where), key)
    for name, p, s, _v, src in reversed(rows):
        if not s:
            continue
        if p == path:
            return _own_source(src)
        if p[0] == "builtin":
            return "built-in"
        if p[0] == "builtin_all_servers":
            return "built-in for servers"
        return "from " + layer_name(R, where, p, name)
    return "built-in"


def show(spec, v, rich=True):
    if spec is not None and spec.type == "number" and v is None:
        return "none"
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, list):
        if not v:
            return "nobody"
        out = []
        for p in v:
            p = str(p)
            if p.startswith("role:"):
                out.append(f"<@&{p[5:]}>" if rich else f"role {p[5:]}")
            else:
                out.append(f"<@{p}>" if rich else p)
        return ", ".join(out[:10]) + (f" and {len(out) - 10} more"
                                      if len(out) > 10 else "")
    if spec is not None and spec.type == "channel" and v and \
            _ID.fullmatch(str(v)):
        return f"<#{v}>" if rich else f"channel {v}"
    if spec is not None and spec.type == "text":
        if not v:
            return "(empty)"
        return f"\"{clip(v, 80)}\""
    return str(v)


def may_view(R, where):
    if R.who.owner:
        return True
    if not R.who.admin or where.kind not in ("server", "channel") \
            or where.gid != R.who.gid:
        return False
    return where.kind == "server" or where.cid in R.channels(where.gid)


def may_edit(R, where, key):
    """May this person change key at where at all (direction aside)."""
    if not may_view(R, where):
        return False
    if R.who.owner:
        return True
    return key == "who.replies" or (key.startswith("plugins.")
                                    and key.endswith(".enabled")) \
        or (key == "who.ignore" and where.kind == "server")


def admin_block(key, old, new, write, spec):
    """Why a server admin may not make this change, or None. The only
    gate on an admin's writes: run on every write, reset and undo."""
    if key == "who.replies" and write != "inherit" \
            and write not in ADMIN_REPLIES:
        return ("Server admins can set replies to off or when-addressed "
                "only.")
    if key.endswith(".enabled") and write != "inherit" and new is not False:
        return "Server admins can only turn plugins off."
    if ms.loosens(key, old, new, spec) is not False:
        return ADMIN_ONLY_QUIETER
    return None


def editable_keys(R, where, keys):
    out = []
    for k in keys:
        spec = R.st.spec(k)
        if spec is None or spec.hidden or not spec.discord_editable:
            continue
        if k in ms.TOP and where.kind != "everywhere":
            continue
        try:
            ms.check_settable(R.st, key_path(where, k), k)
        except ms.SettingsError:
            continue
        if not may_edit(R, where, k) or kcode(k) is None:
            continue
        try:  # the longest custom_id this key's controls use
            cid("pu", where.token, kcode(k), "+" * 20)
        except TooLong:
            continue
        out.append(k)
    return out


def plugin_keys(R, pid):
    m = R.st.manifests.get(pid)
    if m is None:
        return []
    ks = ["enabled"] + [k for k in m.specs if k != "enabled"]
    return [f"plugins.{pid}.{k}" for k in ks]


def all_keys(R, where):
    if where.kind == "channel":
        keys = list(CHANNEL_KEYS)
        for pid in sorted(R.st.manifests):
            keys.append(f"plugins.{pid}.enabled")
        return editable_keys(R, where, keys)
    keys = [k for d in DRAWER_ORDER for k in DRAWERS[d][1]]
    if where.kind == "everywhere":
        keys += list(DRAWERS["bot"][1])
    for pid in sorted(R.st.manifests):
        keys += plugin_keys(R, pid)
    return editable_keys(R, where, keys)


def drawer_of(key):
    if key.startswith("plugins."):
        return "~" + key.split(".")[1]
    for d, (_t, keys) in DRAWERS.items():
        if key in keys:
            return d
    return None


def back_cid(R, where, key):
    """The page a setting's control lives on."""
    if where.kind == "channel":
        return cid("p", where.token)
    d = drawer_of(key)
    if d and d.startswith("~"):
        return cid("pl", where.token, d[1:])
    return cid("d", where.token, d)


def scope_name(R, where):
    if where.kind in ("everywhere", "dms", "all_servers"):
        return {"everywhere": "Everywhere", "dms": "DMs",
                "all_servers": "All servers"}[where.kind]
    gname = (R.guilds().get(where.gid) or {}).get("name") \
        or f"server {where.gid}"
    if where.kind == "server":
        return gname
    ch = R.channels(where.gid).get(where.cid) or {}
    if ch.get("type") == CATEGORY:
        return f"{gname} › category {ch.get('name') or where.cid}"
    return f"{gname} › #{ch.get('name') or where.cid}"


# ---------------------------------------------------------------------------
# Risky changes (owners): the consequence, or None
# ---------------------------------------------------------------------------
_EXPLICIT_RISK = {"privacy.memory", "who.dms_from", "who.allow",
                  "plugins.media.who", "plugins.media.per_day",
                  "who.bot_exchange_limit", "who.replies"}


def risk_text(R, where, key, old, new):
    spec = R.st.spec(key)
    lo = ms.loosens(key, old, new, spec)
    name = scope_name(R, where)
    members = R.members(where)
    if key == "privacy.memory" and lo:
        what = ("your job, location, finances and other saved memory"
                if new == "everything" else
                "public facts about you (name, interests, public projects, "
                "opinions)")
        if where.kind in ("server", "channel"):
            who = f"People in *{name}*" + (
                f" ({members:,} members, including strangers)"
                if members else " (including strangers)")
        elif where.kind == "dms":
            who = "People who DM the bot (as DMs from allows)"
        else:
            who = "People in every server and DM that does not set its own"
        text = f"{who} may see replies drawing on {what}."
        if members and members > BIG_FOR_MEMORY:
            text = (f"**This is a big server ({members:,} members).** "
                    + text + " Anyone there can ask the bot about you.")
        return text
    if key == "who.dms_from" and lo and new in ("anyone", "shared-servers"):
        if new == "anyone":
            return ("Anyone on Discord, strangers included, can DM the bot "
                    "and get answers. Each answer costs a Muse turn.")
        return ("Anyone who shares a server with the bot can DM it and get "
                "answers, not only the owners.")
    if key == "who.allow" and where.kind == "server" and new == "everyone" \
            and isinstance(old, list):
        return (f"Everyone in *{name}*"
                + (f" ({members:,} members)" if members else "")
                + " can get answers, not only the people on the list.")
    if key == "plugins.media.who" and new == "everyone" \
            and old != "everyone":
        return (f"Anyone in {name} can ask for generated images and video. "
                f"Generation can cost real money.")
    if key == "plugins.media.per_day" and lo and (
            new is None or new > MEDIA_PER_DAY_RISKY):
        return ("Up to " + ("an unlimited number of" if new is None
                            else str(new))
                + " generations a day may be sent. Generation can cost "
                  "real money.")
    if key == "who.bot_exchange_limit" and lo and old is not None and (
            new is None or new > BOT_EXCHANGE_RISKY):
        return ("Two bots can keep answering each other "
                + ("without any limit" if new is None
                   else f"up to {new} times in a row")
                + ", each answer a Muse turn.")
    if key == "who.replies" and new == "conversational" \
            and old != "conversational" \
            and where.kind in ("server", "channel") \
            and members and members > BIG_FOR_CONVERSATIONAL:
        return (f"*{name}* has {members:,} members. In conversational mode "
                f"anyone the bot answers can keep talking to it for 2 "
                f"minutes without @mentioning it.")
    if key not in _EXPLICIT_RISK and spec is not None and spec.risky \
            and lo is not False:
        return f"{label(key)} is marked risky by its plugin."
    return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def choice_select(R, where, key, v):
    spec = R.st.spec(key)
    values = list(spec.values)
    if R.who.admin:
        values = [x for x in values if (key != "who.replies"
                                        or x in ADMIN_REPLIES)
                  and ms.loosens(key, v, x, spec) is False]
        if values == [v] or not values:
            return None
    options = [opt(x, x, OPTION_HELP.get((key, x)), x == v) for x in values]
    return sel(cid("s", where.token, kcode(key)), options,
               f"{label(key)}: {show(spec, v, False)}")


def control_button(R, where, key, v):
    spec = R.st.spec(key)
    code = kcode(key)
    if spec.type == "on-off":
        if R.who.admin:
            return btn(f"Turn off {label(key)}",
                       cid("v", where.token, code, "off"), 4) if v else None
        return btn(f"{label(key)}: {'on' if v else 'off'}",
                   cid("v", where.token, code, "off" if v else "on"),
                   3 if v else 2)
    if spec.type in ("number", "text"):
        return btn(f"Edit {label(key)}…", cid("mo", where.token, code), 1)
    if spec.type in ("people", "channel"):
        return btn(f"{label(key)}…", cid("f", where.token, code), 1)
    return None


def engine_place(where):
    if where.kind == "server":
        return ms.Place.server(where.gid)
    if where.kind == "channel":
        return ms.Place.channel(where.gid, where.cid)
    return None


def engine_admin(R, where, key, write):
    """The engine's server-admin rule (muse_settings.admin_changes): the
    value to write (a reset of an admin's own entry becomes the owners'
    value), or raise SettingsError."""
    place = engine_place(where)
    if place is None:
        raise ms.SettingsError(ADMIN_ONLY_QUIETER)
    return ms.admin_changes(R.st, R.who.gid,
                            [ms.Change(place, key, write)], R.ctx)[0].value


def may_reset(R, where, key):
    if R.who.owner:
        return True
    if admin_block(key, effective(R, where, key), inherited(R, where, key),
                   "inherit", R.st.spec(key)) is not None:
        return False
    try:
        engine_admin(R, where, key, "inherit")
    except ms.SettingsError:
        return False
    return True


def reset_component(R, where, keys):
    if not keys:
        return None
    if len(keys) <= 5:
        return [btn(f"Reset {label(k)}", cid("r", where.token, kcode(k)))
                for k in keys]
    return [sel(cid("rx", where.token),
                [opt(f"Reset {label(k)}", kcode(k), "Back to the inherited "
                     "value") for k in keys], "Reset a setting here…")]


def pack(R, where, selects, buttons, reset, nav):
    """Rows for the controls, at most 5: when they do not fit, choice
    selects become buttons that open the setting on its own."""
    selects, buttons = list(selects), list(buttons)
    while True:
        rows = [row(s) for _k, s in selects]
        rows += [row(*buttons[i:i + 5]) for i in range(0, len(buttons), 5)]
        if reset:
            rows.append(row(*reset))
        if nav:
            rows.append(row(*nav))
        if len(rows) <= 5 or not selects:
            if len(rows) > 5:
                R.ctx.log(f"settings_ui: {len(rows)} rows on a page; "
                          f"showing 5")
                rows = rows[:4] + rows[-1:]
            return rows
        k, _s = selects.pop()
        buttons.insert(0, btn(f"{label(k)}…",
                              cid("f", where.token, kcode(k)), 1))


def render_controls(R, where, keys, header, nav, extra=(), status=None):
    lines = ([status, ""] if status else []) + [header]
    selects, buttons, resets = [], [], []
    for k in keys:
        spec = R.st.spec(k)
        v = effective(R, where, k)
        lines.append(f"- **{label(k)}**: {show(spec, v)} · "
                     f"*{source_tag(R, where, k)}*")
        if spec.type == "choice":
            s = choice_select(R, where, k, v)
            if s:
                selects.append((k, s))
        else:
            b = control_button(R, where, k, v)
            if b:
                buttons.append(b)
        if here(R, where, k)[0] and may_reset(R, where, k):
            resets.append(k)
    lines += list(extra)
    if not keys:
        lines.append("Nothing here you can change.")
    rows = pack(R, where, selects, buttons,
                reset_component(R, where, resets), nav)
    return "\n".join(lines), rows


def everywhere_paused(R):
    return R.st.get("who.replies", ms.Scope.dm()) == "off" and \
        R.st.get("who.replies", ms.Scope("all_servers")) == "off"


def guild_label(g):
    name = str(g.get("name") or f"server {g.get('id')}")
    n = g.get("member_count")
    return f"{clip(name, 70)} · {n:,} members" if isinstance(n, int) \
        else clip(name, 100)


def set_here_count(R, path):
    n = 0
    for k in R.st.keys():
        if R.st.layer(path, k)[0]:
            n += 1
    return n


def page_home(R, page=0, status=None):
    guilds = sorted(R.guilds().values(),
                    key=lambda g: (str(g.get("name") or "").lower(),
                                   str(g["id"])))
    pages = max(1, math.ceil(len(guilds) / SERVERS_PER_PAGE))
    page = min(max(0, page), pages - 1)
    opts = [opt("Everywhere", "e", "DMs and every server"),
            opt("DMs", "d", "All direct messages"),
            opt("All servers", "a", "Every server, new ones included")]
    for g in guilds[page * SERVERS_PER_PAGE:(page + 1) * SERVERS_PER_PAGE]:
        n = set_here_count(R, ("servers", str(g["id"])))
        opts.append(opt(guild_label(g), f"s{g['id']}",
                        f"{n} setting{'s' if n != 1 else ''} set for this "
                        f"server" if n else "Follows All servers"))
    paused = everywhere_paused(R)
    lines = ([status, ""] if status else []) + [
        "**Settings** · choose where to look or change things.",
        f"The bot is in {len(guilds)} server{'s' if len(guilds) != 1 else ''}"
        + (f" (page {page + 1} of {pages})." if pages > 1 else ".")]
    if paused:
        lines.append("**Paused everywhere:** it is not answering anyone.")
    rows = [row(sel(cid("go"), opts, "Choose a scope…"))]
    if pages > 1:
        rows.append(row(
            btn("Previous servers", cid("h", max(page - 1, 0)),
                disabled=page == 0),
            btn("More servers", cid("h", page + 1),
                disabled=page >= pages - 1)))
    rows.append(row(btn("Resume everywhere", cid("rs", "e", "h"), 3)
                    if paused else
                    btn("Pause everywhere", cid("pz", "e", "h"), 4)))
    return "\n".join(lines), rows


def pause_button(R, where):
    if where.kind == "everywhere":
        if not R.who.owner:
            return None
        if everywhere_paused(R):
            return btn("Resume everywhere", cid("rs", "e"), 3)
        return btn("Pause everywhere", cid("pz", "e"), 4)
    if "who.replies" not in editable_keys(R, where, ["who.replies"]):
        return None
    s, v = here(R, where, "who.replies")
    if s and v == "off":
        return btn("Resume here", cid("rs", where.token), 3)
    return btn("Pause here", cid("pz", where.token), 4)


def nav_row(R, where, back):
    out = []
    p = pause_button(R, where)
    if p:
        out.append(p)
    out.append(btn("Why?", cid("w", where.token)))
    out.append(btn("Undo last change", cid("u", where.token)))
    if back:
        out.append(back)
    return out


def page_scope(R, where, status=None):
    if where.kind == "channel":
        return page_channel(R, where, status)
    name = scope_name(R, where)
    title = f"**{name}**"
    members = R.members(where)
    if members is not None:
        title += f" · {members:,} members"
    lines = ([status, ""] if status else []) + [title]
    if where.kind != "everywhere":
        v = effective(R, where, "who.replies")
        lines.append(f"Replies: **{v}** · "
                     f"*{source_tag(R, where, 'who.replies')}*")
    elif everywhere_paused(R):
        lines.append("**Paused everywhere.**")
    n = set_here_count(R, where.path)
    lines.append(f"{n} setting{'s' if n != 1 else ''} set at this scope; "
                 f"everything else is inherited." if n else
                 "Nothing is set at this scope; everything is inherited.")
    if R.who.admin:
        lines.append("You can make the bot quieter in this server (replies, "
                     "plugins off, ignore people), never louder.")
    drawers = []
    for d in DRAWER_ORDER:
        if editable_keys(R, where, DRAWERS[d][1]):
            drawers.append(btn(DRAWERS[d][0], cid("d", where.token, d), 1))
    if any(editable_keys(R, where, plugin_keys(R, pid))
           for pid in R.st.manifests):
        drawers.append(btn("Plugins", cid("d", where.token, "plug"), 1))
    if where.kind == "everywhere" and R.who.owner:
        drawers.append(btn("Bot-wide", cid("d", where.token, "bot"), 1))
    if where.kind == "server":
        ch, cat = R.here_channel(where.gid)
        if ch:
            drawers.append(btn("This channel",
                               cid("p", f"c{where.gid}.{ch}")))
        if cat:
            drawers.append(btn("This category",
                               cid("p", f"c{where.gid}.{cat}")))
    rows = [row(*drawers[i:i + 5]) for i in range(0, len(drawers), 5)]
    back = btn("All scopes", cid("h", 0)) if R.who.owner else None
    rows.append(row(*nav_row(R, where, back)))
    if where.kind == "server" and R.who.owner:
        picker = channel_picker(R, where)
        if picker:
            rows.append(row(picker))
    return "\n".join(lines), rows[:5]


def channel_picker(R, where):
    chans = [c for c in R.channels(where.gid).values()
             if c.get("type") in PICKABLE_CHANNELS]
    if not chans:
        return None
    listed = set(R.st.channels_listed(where.gid))
    chans.sort(key=lambda c: (str(c["id"]) not in listed,
                              c.get("type") != CATEGORY,
                              str(c.get("name") or "").lower()))
    opts = []
    for c in chans[:25]:
        is_cat = c.get("type") == CATEGORY
        opts.append(opt(("Category: " if is_cat else "#")
                        + str(c.get("name") or c["id"]), str(c["id"]),
                        "Has its own settings" if str(c["id"]) in listed
                        else None))
    return sel(cid("cp", where.token), opts, "A channel or category…")


def page_channel(R, where, status=None):
    keys = all_keys(R, where)
    plugin_toggles = [k for k in keys if k.startswith("plugins.")]
    nav = nav_row(R, where, btn("Back to server",
                                cid("p", where.server.token)))
    if len(plugin_toggles) > 3:
        keys = [k for k in keys if not k.startswith("plugins.")]
        nav.insert(-1, btn("Plugins", cid("d", where.token, "plug"), 1))
    ch = R.channels(where.gid).get(where.cid) or {}
    what = "category" if ch.get("type") == CATEGORY else "channel"
    header = (f"**{scope_name(R, where)}**\nA {what} can set replies, house "
              f"rules and plugins on/off; everything else follows the "
              f"server.")
    if what == "category":
        header += " Its channels inherit these."
    return render_controls(R, where, keys, header, nav[:5], status=status)


def page_drawer(R, where, d, status=None):
    if d == "plug":
        return page_plugins(R, where, status)
    if d not in DRAWERS or (d == "bot" and where.kind != "everywhere"):
        return None
    title, keys = DRAWERS[d]
    keys = editable_keys(R, where, keys)
    extra = []
    if d == "priv" and R.who.owner:
        n = len(R.st.get("privacy.never_share", ms.Scope.everywhere())
                or [])
        extra.append(f"- **Secret phrases (never share)**: {n} set in "
                     f"config.yaml · *edit the file to change them*")
    nav = [btn("Why?", cid("w", where.token, d)),
           btn("Back", cid("p", where.token))]
    return render_controls(R, where, keys,
                           f"**{scope_name(R, where)} › {title}**", nav,
                           extra, status)


def page_plugins(R, where, status=None):
    lines = ([status, ""] if status else []) + [
        f"**{scope_name(R, where)} › Plugins**"]
    opts = []
    for pid in sorted(R.st.manifests):
        keys = editable_keys(R, where, plugin_keys(R, pid))
        if not keys:
            continue
        on = effective(R, where, f"plugins.{pid}.enabled")
        lines.append(f"- **{pid}**: {'on' if on else 'off'} · *"
                     f"{source_tag(R, where, f'plugins.{pid}.enabled')}*")
        opts.append(opt(pid, pid, help_text(R.st, f"plugins.{pid}.enabled")
                        or f"{len(keys)} setting(s)"))
    rows = []
    if opts:
        rows.append(row(sel(cid("pls", where.token), opts,
                            "Choose a plugin…")))
    else:
        lines.append("No plugin settings can be changed here.")
    rows.append(row(btn("Back", cid("p", where.token))))
    return "\n".join(lines), rows


def page_plugin(R, where, pid, status=None):
    if pid not in R.st.manifests:
        return None
    keys = editable_keys(R, where, plugin_keys(R, pid))
    m = R.st.manifests[pid]
    extra = []
    desc = (m.specs.get("enabled").description or "") if m.specs.get(
        "enabled") else ""
    if desc:
        extra.append(clip(desc, 300))
    nav = [btn("Why?", cid("w", where.token, "~" + pid)),
           btn("Back", cid("d", where.token, "plug")
               if where.kind != "channel" else cid("p", where.token))]
    return render_controls(R, where, keys,
                           f"**{scope_name(R, where)} › Plugins › {pid}**",
                           nav, extra, status)


def page_focus(R, where, key, status=None):
    spec = R.st.spec(key)
    v = effective(R, where, key)
    code = kcode(key)
    tok = where.token
    lines = ([status, ""] if status else []) + [
        f"**{scope_name(R, where)} › {label(key)}**"]
    h = help_text(R.st, key)
    if h:
        lines.append(h)
    lines.append(f"Now: {show(spec, v)} · *{source_tag(R, where, key)}*")
    rows = []
    if spec.type == "choice":
        s = choice_select(R, where, key, v)
        if s:
            rows.append(row(s))
    elif spec.type == "on-off":
        b = control_button(R, where, key, v)
        if b:
            rows.append(row(b))
    elif spec.type in ("number", "text"):
        bs = [btn(f"Edit {label(key)}…", cid("mo", tok, code), 1)]
        if spec.type == "number" and spec.none and R.who.owner:
            bs.append(btn("No limit (none)", cid("v", tok, code, "none")))
        rows.append(row(*bs))
    elif spec.type == "people":
        rows += people_rows(R, where, key, v)
    elif spec.type == "channel":
        rows += channel_rows(R, where, key, v)
    last = []
    if here(R, where, key)[0] and may_reset(R, where, key):
        last.append(btn("Reset to inherited", cid("r", tok, code)))
    last.append(btn("Back", back_cid(R, where, key)))
    rows.append(row(*last))
    return "\n".join(lines), rows


def _split_people(v):
    if not isinstance(v, list):
        return [], []
    users = [p for p in v if not str(p).startswith("role:")]
    roles = [p[5:] for p in v if str(p).startswith("role:")]
    return users, roles


def people_rows(R, where, key, v):
    spec = R.st.spec(key)
    tok, code = where.token, kcode(key)
    users, roles = _split_people(v)
    add_only = R.who.admin or len(users) > 25 or len(roles) > 25
    mode = ("+",) if add_only else ()
    us = {"type": 5, "custom_id": cid("pu", tok, code, *mode),
          "placeholder": "Add people…" if add_only else "People…",
          "min_values": 0 if not add_only else 1, "max_values": 25}
    rs = {"type": 6, "custom_id": cid("pr", tok, code, *mode),
          "placeholder": "Add roles…" if add_only else "Roles…",
          "min_values": 0 if not add_only else 1, "max_values": 25}
    if not add_only:
        if users:
            us["default_values"] = [{"id": u, "type": "user"} for u in users]
        if roles:
            rs["default_values"] = [{"id": r, "type": "role"} for r in roles]
    rows = [row(us), row(rs)]
    if R.who.owner:
        kws = [btn(kw.capitalize(), cid("v", tok, code, kw),
                   1 if v == kw else 2) for kw in spec.values]
        if not spec.values:
            kws.append(btn("Nobody", cid("v", tok, code, "_"),
                           1 if v == [] else 2))
        rows.append(row(*kws))
    return rows


def channel_rows(R, where, key, v):
    spec = R.st.spec(key)
    tok, code = where.token, kcode(key)
    rows = []
    if where.gid:
        if str(R.ix.get("guild_id") or "") == where.gid:
            cs = {"type": 8, "custom_id": cid("cs", tok, code),
                  "channel_types": list(TEXT_CHANNELS),
                  "placeholder": "Choose a channel…",
                  "min_values": 1, "max_values": 1}
            if v and _ID.fullmatch(str(v)):
                cs["default_values"] = [{"id": str(v), "type": "channel"}]
            rows.append(row(cs))
        else:
            chans = sorted((c for c in R.channels(where.gid).values()
                            if c.get("type") in TEXT_CHANNELS),
                           key=lambda c: str(c.get("name") or "").lower())
            if chans:
                rows.append(row(sel(
                    cid("cs", tok, code),
                    [opt("#" + str(c.get("name") or c["id"]), str(c["id"]),
                         default=str(c["id"]) == str(v))
                     for c in chans[:25]], "Choose a channel…")))
    kws = [btn(kw.capitalize(), cid("v", tok, code, kw), 1 if v == kw else 2)
           for kw in spec.values]
    if kws:
        rows.append(row(*kws[:5]))
    return rows


def page_confirm(R, where, key, old, new, write, risk, undo=False):
    spec = R.st.spec(key)
    tok, code = where.token, kcode(key)
    lines = ["**Are you sure?**", risk, "",
             f"{'Undo' if undo else 'Change'} **{label(key)}** in "
             f"{scope_name(R, where)}: {show(spec, old)} → {show(spec, new)}"]
    yes = cid("yu", tok, code) if undo else cid("y", tok, code, enc(write))
    cancel = cid("p", tok) if undo else back_cid(R, where, key)
    return "\n".join(lines), [row(btn("Yes, change it", yes, 4),
                                  btn("Cancel", cancel))]


def why(R, where, d=None):
    if d in DRAWERS:
        keys = editable_keys(R, where, DRAWERS[d][1])
    elif d and d.startswith("~"):
        keys = editable_keys(R, where, plugin_keys(R, d[1:]))
    else:
        keys = all_keys(R, where)
    lines = [f"**Why? {scope_name(R, where)}**",
             "Each value, then every layer that sets it (least specific "
             "first; the last one wins):"]
    for k in keys:
        spec = R.st.spec(k)
        v = effective(R, where, k)
        if k in ms.TOP:
            s, lv, src = R.st.layer(("top",), k)
            chain = [f"built-in {show(spec, spec.default, False)}"]
            if s:
                chain.append(f"{_own_source(src)}: {show(spec, lv, False)}")
        else:
            _val, rows = R.st.trace(R.scope(where), k)
            chain = []
            for name, p, s, lv, src in rows:
                if not s:
                    continue
                where_ = "built-in" if p[0] == "builtin" else \
                    layer_name(R, where, p, name)
                how = "" if p[0].startswith("builtin") else \
                    (" (config.yaml)" if src[0] == "file" else
                     f" (Discord, {_own_source(src)[19:]})")
                chain.append(f"{where_}{how}: {show(spec, lv, False)}")
        lines.append(f"**{label(k)}** = {show(spec, v)}\n  "
                     + " → ".join(chain))
    text = "\n".join(lines)
    if len(text) > CONTENT_MAX:
        tail = ("\n… more: run `python3 bin/settings.py show --explain` "
                "on the bot's machine.")
        text = text[:CONTENT_MAX - len(tail)].rsplit("\n", 1)[0] + tail
    return notice(text)


# ---------------------------------------------------------------------------
# Writes. Every change (set, reset, undo, pause, resume, confirm) goes
# through _apply or op_undo, which re-check who may do it, where, and for a
# server admin that it does not loosen anything.
# ---------------------------------------------------------------------------
def _apply(R, where, key, raw, confirmed=False):
    """None when written, else the response to send instead (a refusal,
    an error, or a confirmation screen)."""
    if not may_view(R, where):
        return notice(REFUSAL)
    spec = R.st.spec(key)
    if spec is None or (key in ms.TOP and where.kind != "everywhere"):
        return notice(STALE)
    path = key_path(where, key)
    try:
        ms.check_settable(R.st, path, key)
    except ms.SettingsError as e:
        return notice(f"That can't be changed here: {e}")
    if not may_edit(R, where, key):
        return notice(ADMIN_ONLY_QUIETER if R.who.admin else REFUSAL)
    old = effective(R, where, key)
    if raw == "inherit":
        new, write = inherited(R, where, key), "inherit"
    else:
        ok, norm = ms.normalize(spec, raw)
        if not ok:
            return notice(f"Not saved: {label(key)}: {norm}. Valid: "
                          f"{spec.valid_text()}.")
        if spec.type == "choice" and norm in spec.hidden_values:
            return notice(STALE)
        new, write = norm, ms.display(spec, norm)
    if R.who.admin:
        block = admin_block(key, old, new, write, spec)
        if block:
            return notice(block)
        try:
            write = engine_admin(R, where, key, write)
        except ms.SettingsError as e:
            return notice(str(e))
    elif not confirmed:
        risk = risk_text(R, where, key, old, new)
        if risk:
            try:
                return message(*page_confirm(R, where, key, old, new, write,
                                             risk), R.new)
            except TooLong:
                return notice("That value is too long to confirm here; "
                              "set it in config.yaml.")
    keystr = ms.format_key(path, key)
    try:
        if write == "inherit":
            R.ctx.reset(keystr, R.who.uid)
        else:
            R.ctx.write(keystr, write, R.who.uid)
    except ms.SettingsError as e:
        return notice(f"Not saved: {e}")
    if R.who.admin:
        R.ctx.notify_owners_admin_change(where.gid, R.who.uid, keystr,
                                         show(spec, old, False),
                                         show(spec, new, False))
    return None


def saved(R, where, key):
    spec = R.st.spec(key)
    return (f"**Saved.** {label(key)} in {scope_name(R, where)} is now "
            f"{show(spec, effective(R, where, key))}.")


def op_set(R, where, key, raw, confirmed=False):
    resp = _apply(R, where, key, raw, confirmed)
    if resp is not None:
        return resp
    R.reload()
    status = saved(R, where, key)
    spec = R.st.spec(key)
    if where.kind == "channel":
        page = page_channel(R, where, status)
    elif spec.type in ("people", "channel"):
        page = page_focus(R, where, key, status)
    else:
        d = drawer_of(key)
        page = page_plugin(R, where, d[1:], status) if d.startswith("~") \
            else page_drawer(R, where, d, status)
    return message(*page, R.new)


def op_undo(R, where, confirmed=False, expect=None):
    if not may_view(R, where):
        return notice(REFUSAL)
    prefix = ms.scope_prefix(where.path)
    admin_gid = R.who.gid if R.who.admin else None
    # Everything this Undo reverts (a whole group, wherever it landed);
    # a server admin's Undo sees only server admins' changes there.
    plan = ms.undo_plan(R.st, prefix, admin_gid=admin_gid)
    if not plan:
        return notice("Nothing to undo here: no change made in Discord at "
                      "this scope is still in effect.")
    first = plan[0]
    path, key, target = first.path, first.key, first.value
    if expect is not None and kcode(key) != expect:
        return notice("Something changed since; open the page again.")
    spec = R.st.spec(key)
    if spec is None or not may_edit(R, where, key):
        return notice("The last change here was to a setting you can't "
                      "change; ask one of the bot's owners.")
    old = effective(R, where, key)
    if target == "inherit":
        new = R.st.value_without(R.scope(where), key, path)
    else:
        ok, new = ms.normalize(spec, target)
        if not ok:
            return notice(f"Can't undo: {new}")
    if R.who.admin:
        # The engine's rule: an admin may undo admins' own changes, back
        # to (never past) what the owners set.
        try:
            ms.admin_check(R.st, R.who.gid, plan, R.ctx, revert=True)
        except ms.SettingsError:
            return notice("Undoing the last change here would make the bot "
                          "louder. " + ADMIN_ONLY_QUIETER)
    elif not confirmed:
        # Judged by the outcome of the whole Undo, not its first key.
        risk = ms.assess(R.st, plan, R.ctx)
        if risk.risky:
            return message(*page_confirm(R, where, key, old, new, None,
                                         " ".join(risk.warnings),
                                         undo=True), R.new)
    try:
        if R.who.admin:
            k = R.ctx.undo(prefix, R.who.uid, admin_gid=R.who.gid,
                           info=R.ctx)
        else:
            k = R.ctx.undo(prefix, R.who.uid)
    except ms.SettingsError as e:
        return notice(f"Can't undo: {e}")
    if k is None:
        return notice("Nothing to undo here.")
    if R.who.admin:
        R.ctx.notify_owners_admin_change(where.gid, R.who.uid, k,
                                         show(spec, old, False),
                                         show(spec, new, False))
    R.reload()
    status = (f"**Undone.** {label(key)} in {scope_name(R, where)} is "
              f"{show(spec, effective(R, where, key))} again.")
    return message(*page_scope(R, where, status), R.new)


def resume_value(R, where):
    """What who.replies was before it was paused here (from history)."""
    ks = ms.format_key(where.path, "who.replies")
    for h in reversed((R.st.overlay_doc or {}).get("history") or []):
        if not isinstance(h, dict) or "to" not in h:
            continue
        try:
            path, key = ms.parse_key(h.get("key"))
        except ValueError:
            continue
        if ms.format_key(path, key) != ks:
            continue
        if h["to"] == "off" and h.get("from") not in (None, "off"):
            return h["from"]
    return "inherit"


def op_pause(R, where, pause, home):
    if where.kind == "everywhere":
        if not R.who.owner:
            return notice(REFUSAL)
        targets = [Where("dms"), Where("all_servers")]
    else:
        targets = [where]
    for t in targets:
        value = "off" if pause else resume_value(R, t)
        if not pause and here(R, t, "who.replies") != (True, "off"):
            continue
        # Pausing never loosens; resuming restores what an owner had.
        resp = _apply(R, t, "who.replies", value, confirmed=True)
        if resp is not None:
            return resp
    R.reload()
    status = f"**{'Paused' if pause else 'Resumed'}** " + (
        "everywhere." if where.kind == "everywhere"
        else f"in {scope_name(R, where)}.")
    if home:
        return message(*page_home(R, 0, status), R.new)
    return message(*page_scope(R, where, status), R.new)


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------
def open_modal(R, where, key):
    if not may_view(R, where):
        return notice(REFUSAL)
    spec = R.st.spec(key)
    if spec is None or spec.type not in ("number", "text") or \
            key not in editable_keys(R, where, [key]):
        return notice(STALE)
    s, hv = here(R, where, key)
    eff = effective(R, where, key)
    if spec.type == "number":
        rng = f"{spec.min}-{spec.max}" if spec.max is not None \
            else f"{spec.min} or more"
        value = str(ms.display(spec, hv)) if s else ""
        placeholder = (f"Now {show(spec, eff, False)}. A whole number "
                       f"{rng}" + (", or none" if spec.none else ""))
        desc = "A whole number" + (", or none for no limit" if spec.none
                                   else "") + "."
        style, maxlen = 1, 12
    else:
        value = (hv or "") if s else ""
        inh = inherited(R, where, key) if key not in ms.TOP else ""
        placeholder = (f"Inherited: {inh}" if inh else "Empty")
        desc = help_text(R.st, key)
        style, maxlen = 2, spec.maxlen or 4000
    ti = {"type": 4, "custom_id": "v", "style": style, "required": False,
          "max_length": maxlen, "placeholder": clip(placeholder, 100)}
    if value:
        ti["value"] = str(value)[:maxlen]
    comp = {"type": 18, "label": clip(label(key), 45), "component": ti}
    if desc:
        comp["description"] = clip(desc, 100)
    return {"type": 9, "data": {
        "custom_id": cid("ms", where.token, kcode(key)),
        "title": clip(f"{label(key)} · {scope_name(R, where)}", 45),
        "components": [comp]}}


def modal_value(data, field="v"):
    for c in data.get("components") or []:
        if not isinstance(c, dict):
            continue
        inner = [c.get("component")] if c.get("type") == 18 \
            else (c.get("components") or [])
        for i in inner:
            if isinstance(i, dict) and i.get("custom_id") == field:
                return str(i.get("value") or "")
    return None


def submit_modal(R, where, key):
    spec = R.st.spec(key)
    if spec is None or spec.type not in ("number", "text"):
        return notice(STALE)
    text = modal_value(R.ix.get("data") or {})
    if text is None:
        return notice(STALE)
    if spec.type == "number":
        raw = text.strip().replace(",", "").replace("_", "")
        if not raw:
            return notice(f"Not saved: {label(key)} needs a whole number"
                          + (" or none." if spec.none else "."))
    else:
        raw = text.strip()
    return op_set(R, where, key, raw)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def handle(interaction, ctx):
    """The full interaction callback body ({"type": 4|7|9, "data": ...})
    for the settings command, or a component / modal submit whose
    custom_id starts with "mdset:". Never raises: an error is logged and
    answered with an ephemeral apology. No network."""
    try:
        return _handle(interaction, ctx)
    except Exception as e:
        try:
            ctx.log(f"settings_ui: {type(e).__name__}: {e}\n"
                    + traceback.format_exc())
        except Exception:
            pass
        return notice(APOLOGY)


def _handle(ix, ctx):
    R = Req(ix, ctx)
    data = ix.get("data") or {}
    t = ix.get("type")
    if t == 2:
        if data.get("name") != COMMAND:
            return notice(STALE)
        R.new = True
        if R.who.role is None:
            return notice(REFUSAL)
        gid = str(ix.get("guild_id") or "")
        if not gid:
            return message(*page_home(R), True)
        return message(*page_scope(R, Where("server", gid)), True)
    if t not in (3, 5):
        return notice(STALE)
    custom = str(data.get("custom_id") or "")
    parts = custom.split(":")
    if parts[0] != PREFIX or len(parts) < 2 or len(custom) > CUSTOM_ID_MAX:
        return notice(STALE)
    if R.who.role is None:
        return notice(REFUSAL)
    act, args = parts[1], parts[2:]
    values = [str(v) for v in data.get("values") or []]

    if act == "open":              # the owners' join DM: open:server:<id>
        if len(args) != 2 or args[0] != "server" or \
                not _ID.fullmatch(args[1]):
            return notice(STALE)
        if not R.who.owner:
            return notice(REFUSAL)
        R.new = True
        return message(*page_scope(R, Where("server", args[1])), True)
    if act in ("h", "go"):
        if not R.who.owner:
            return notice(REFUSAL)
        if act == "h":
            try:
                page = int(args[0]) if args else 0
            except ValueError:
                return notice(STALE)
            return message(*page_home(R, page), False)
        where = parse_where(values[0] if values else "")
        if where is None or where.kind == "channel":
            return notice(STALE)
        return message(*page_scope(R, where), False)

    where = parse_where(args[0] if args else "")
    if where is None:
        return notice(STALE)
    if not may_view(R, where):
        return notice(REFUSAL)
    rest = args[1:]
    key = kdecode(rest[0]) if rest else None
    if key is not None and R.st.spec(key) is None:
        return notice(STALE)

    if act == "p":
        return message(*page_scope(R, where), False)
    if act == "d" and rest:
        page = page_drawer(R, where, rest[0])
        return message(*page, False) if page else notice(STALE)
    if act == "pls" and values:
        page = page_plugin(R, where, values[0])
        return message(*page, False) if page else notice(STALE)
    if act == "pl" and rest:
        page = page_plugin(R, where, rest[0])
        return message(*page, False) if page else notice(STALE)
    if act == "cp" and values:
        if where.kind != "server" or not _ID.fullmatch(values[0]):
            return notice(STALE)
        target = Where("channel", where.gid, values[0])
        if not may_view(R, target):
            return notice(REFUSAL)
        return message(*page_channel(R, target), False)
    if act == "w":
        return why(R, where, rest[0] if rest else None)
    if act == "u":
        return op_undo(R, where)
    if act == "yu" and rest:
        if not R.who.owner:
            return notice(REFUSAL)
        return op_undo(R, where, confirmed=True, expect=rest[0])
    if act in ("pz", "rs"):
        return op_pause(R, where, act == "pz", rest[:1] == ["h"])
    if act == "rx" and values:
        k = kdecode(values[0])
        return op_set(R, where, k, "inherit") if k and R.st.spec(k) \
            else notice(STALE)
    if key is None:
        return notice(STALE)
    spec = R.st.spec(key)
    if act == "f":
        if key not in editable_keys(R, where, [key]):
            return notice(STALE if R.who.owner else ADMIN_ONLY_QUIETER)
        return message(*page_focus(R, where, key), False)
    if act == "s" and values:
        return op_set(R, where, key, values[0])
    if act == "v" and len(rest) == 2:
        return op_set(R, where, key, dec(spec, rest[1]))
    if act == "y" and len(rest) == 2:
        if not R.who.owner:
            return notice(REFUSAL)
        raw = rest[1] if rest[1] == "inherit" else dec(spec, rest[1])
        return op_set(R, where, key, raw, confirmed=True)
    if act == "r":
        return op_set(R, where, key, "inherit")
    if act in ("pu", "pr") and spec.type == "people":
        cur = effective(R, where, key)
        users, roles = _split_people(cur)
        picked = [v for v in values if _ID.fullmatch(v)]
        add = rest[1:2] == ["+"]
        if act == "pu":
            users = list(dict.fromkeys(users + picked)) if add else picked
        else:
            roles = list(dict.fromkeys(roles + picked)) if add else picked
        new = users + ["role:" + r for r in roles]
        if not new:
            if "owners" in spec.values:
                new = "owners"
            elif key == "who.allow":
                return notice("Pick at least one person or role, or choose "
                              "Everyone.")
        return op_set(R, where, key, new)
    if act == "cs" and values and spec.type == "channel":
        return op_set(R, where, key, values[0])
    if act == "mo":
        return open_modal(R, where, key)
    if act == "ms" and t == 5:
        return submit_modal(R, where, key)
    return notice(STALE)
