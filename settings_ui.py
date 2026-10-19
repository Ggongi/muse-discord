#!/usr/bin/env python3
"""/settings in Discord: every page, as one pure function of an
interaction and a Ctx (see handle). monitor.py owns the plumbing
(registering the commands, routing "mdset:" interactions here, building
the Ctx, answering Discord); this module never touches the network, so
it answers well inside Discord's 3 seconds, and it never raises.

Pages are Components V2 messages (flags 64|32768: no content, no
embeds; at most 40 components). A page opens on the place where
/settings was typed (a channel, or DMs in the bot's DM) and is edited in
place from then on. Words come from muse_settings (LABELS, describe_
change); every write goes through muse_settings.apply as one undo group,
after muse_settings.assess (risky outcomes show a confirm card first).

custom_id grammar ("mdset:2:" + colon-separated parts, <= 100 chars):

    place := x (Defaults) | d (DMs) | s<gid> | c<gid>.<cid>
    k     := a short code (CORE_CODES) | ~<plugin>.<key>

    pg:<place>:<page>        page: b basics, m more, e extras, w bot-wide,
                             y why, q quiet (server admins), o servers
    go:<place>               "Go to..." select: a place token, p<n>, o<n>
    gc:<place>               channel select (or its paged fallback)
    set:<place>:<k>          choice select: a value or "="
    val:<place>:<k>:<v>[:<page>]   a button with an absolute value
    ppl:<place>:<k>          a user / mentionable select
    rm:<place>:<k>           a "Remove someone..." select
    mod:<place>:<form>[:<page>]    open a modal (i c bx mpd wn st fc fs
                                   id.<k>)
    sub:<place>:<form>[:<page>]    its submit
    re:<place>:<form>:<tok>  reopen a modal from a stashed draft
    pre:<place>              preset select: preview
    ok:<place>:<tok>         apply what a confirm card or preview stashed
    un:<place>[:<grp>]       undo the newest group there (+ channels)
    pz:<place> | rs:<place>  pause / resume
    fx:<place>:<k>:<op>      a "Why no reply?" fix: = | v<value> |
                             -<id> | +<id>
    wy:<place>[:<id>]        DMs: check one person

The optional <page> says which page to show again afterwards. Nothing in
a custom_id is trusted: who may do what, and where, is checked again on
every interaction (server admins through muse_settings' admin rule), and
a stash token is bound to the user it was made for. Older "mdset:" ids
answer "out of date", except mdset:open:server:<gid> (the join DM's
Configure button), which still opens that server's page.
"""
import math
import re
import time
import traceback
from datetime import datetime, timezone
from typing import Protocol

import muse_settings as ms

PREFIX = "mdset"
VERSION = "2"
COMMAND = "settings"
# The message context command; Discord may refuse "?" in its name, in
# which case the monitor registers the second.
WHY_COMMANDS = ("Why no reply?", "Why no reply")
EPHEMERAL = 1 << 6
COMPONENTS_V2 = 1 << 15
NEW_FLAGS = EPHEMERAL | COMPONENTS_V2
MANAGE_GUILD = 1 << 5
ADMINISTRATOR = 1 << 3
CUSTOM_ID_MAX = 100
MAX_COMPONENTS = 40
TEXT_BUDGET = 3600
STASH_TTL = 900
BLURPLE, GREY, AMBER, RED = 0x5865F2, 0x80848E, 0xF0B232, 0xED4245
SERVERS_PER_PAGE = 22
CHANNELS_PER_PAGE = 23
FIND_AFTER_PAGES = 3
WHY_ENTRIES = 5
ADMIN_CHANGES_SHOWN = 3
# Whether a user select in the bot's DM searches people the DM does not
# include (verify on real clients; see e2e/README.md). While False, the
# DMs page in a DM offers Add by ID and a Remove select instead.
DM_USER_SELECT_SEARCHES = False
THREAD_TYPES = (10, 11, 12)
CATEGORY = 4
GO_CHANNEL_TYPES = [0, 5, 15, 16, 2, 13]
TEXT_CHANNEL_TYPES = [0, 5]

# Component types and button styles.
ROW, BUTTON, STRING_SELECT, TEXT_INPUT = 1, 2, 3, 4
USER_SELECT, ROLE_SELECT, MENTIONABLE_SELECT, CHANNEL_SELECT = 5, 6, 7, 8
SECTION, TEXT, SEPARATOR, CONTAINER, LABEL = 9, 10, 14, 17, 18
PRIMARY, SECONDARY, SUCCESS, DANGER = 1, 2, 3, 4

REFUSAL_SERVER = ("Only the bot's owners can change its settings. Server "
                  "admins (Manage Server) can make it quieter here.")
REFUSAL_DM = ("Only the bot's owners can use /settings in DMs. Server "
              "admins: run it in your server.")
REFUSAL_WHY = ("Only the bot's owners and this server's admins can see "
               "why the bot didn't reply.")
OUT_OF_DATE = ("This settings page is out of date. Run /settings for a "
               "fresh one. Nothing was changed.")
EXPIRED = "This confirmation expired, so nothing was changed."
WRONG_USER = ("That button was made for someone else, so nothing was "
              "changed.")
APOLOGY = ("Sorry, something went wrong with /settings. Nothing was "
           "changed by this click; try again in a moment.")
ADMIN_ONLY_QUIETER = ("Server admins can make the bot quieter here, never "
                      "louder. Ask one of the bot's owners for that.")
OVERLAY_BROKEN = ("⚠ config.discord.yaml can't be read, so changes from "
                  "Discord are paused. Fix it on the bot's machine.")
ROLES_ELSEWHERE = ("Roles only count in their own server, so the roles "
                   "you picked were left out. Run /settings in that server "
                   "to pick them.")
FIXED_NOTE = "-# Fixed. {who} must send again; refused messages stay " \
             "unanswered."


class Ctx(Protocol):
    """What the monitor provides (monitor.SettingsCtx)."""
    settings: "ms.Settings"   # current, already reloaded
    owners: set
    bot_id: str
    now: float

    def guilds(self) -> list: ...
    # [{"id", "name", "member_count", "system_channel_id"}]
    def channels(self, guild_id) -> list: ...
    # [{"id", "name", "type", "parent_id", "readable", "can_send"}]
    def apply(self, changes, by, role, group=None, admin_gid=None): ...
    def undo(self, prefix, by, **kw): ...    # children, admin_gid, expect
    def reload(self) -> "ms.Settings": ...
    def names(self, some_id) -> str: ...      # servers and channels
    def display_name(self, user_id) -> str: ...
    def recent_outcomes(self, place, limit) -> list: ...
    def outcome(self, message_id) -> dict: ...
    def stash_put(self, user_id, payload, ttl=STASH_TTL) -> str: ...
    def stash_take(self, user_id, token): ...  # None: gone; raises
    #                                           PermissionError: not yours
    def notify_owners_admin_change(self, guild_id, user_id, key, old,
                                   new) -> None: ...
    def log(self, msg: str) -> None: ...


# ---------------------------------------------------------------------------
# Keys in custom ids
# ---------------------------------------------------------------------------
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
_PLACE = re.compile(r"x|d|s(\d{1,25})|c(\d{1,25})\.(\d{1,25})")
_ID = re.compile(r"\d{1,25}")
_SNOWFLAKE = re.compile(r"\d{15,25}")
_PART = re.compile(r"[^:]{1,100}")
FORMS = ("i", "c", "bx", "mpd", "wn", "st", "fc", "fs")
PAGES = ("b", "m", "e", "w", "y", "q", "o")


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


class BadId(ValueError):
    pass


def cid(*parts):
    parts = [str(p) for p in parts]
    s = ":".join([PREFIX, VERSION] + parts)
    if len(s) > CUSTOM_ID_MAX or any(not _PART.fullmatch(p)
                                     for p in parts):
        raise BadId(s)
    return s


def ptok(place):
    if place.kind == "defaults":
        return "x"
    if place.kind == "dms":
        return "d"
    if place.kind == "server":
        return f"s{place.guild_id}"
    return f"c{place.guild_id}.{place.channel_id}"


# ---------------------------------------------------------------------------
# Components
# ---------------------------------------------------------------------------
def clip(text, n):
    text = str(text)
    return text if len(text) <= n else text[:n - 1] + "…"


_MD = re.compile(r"([\\*_~|>#\[\]()`])")
_UNMD = re.compile(r"\\([\\*_~|>#\[\]()`])")
ZWSP = "\u200b"


def esc(text):
    """A name (a server's, a channel's, a person's: anyone can choose
    them) made safe to put in a Text Display: markdown escaped, mentions
    and @everyone / @here neutralised, on one line. Labels and options
    are plain text in Discord; plain() undoes this for them."""
    t = " ".join(str(text).split())
    t = _MD.sub(r"\\\1", t)
    t = re.sub(r"<(?=[@#])", "<" + ZWSP, t)
    return re.sub(r"@(?=(everyone|here)\b)", "@" + ZWSP, t)


def plain(text):
    """esc() undone, for text Discord shows as is (labels, options,
    placeholders, modal titles)."""
    t = _UNMD.sub(r"\1", str(text)).replace(ZWSP, "")
    return t[:-2] + "…" if t.endswith("\\…") else t


def td(text):
    return {"type": TEXT, "content": clip(text, 3900)}


def sep():
    return {"type": SEPARATOR, "divider": True, "spacing": 1}


def btn(label, custom_id, style=SECONDARY, disabled=False):
    b = {"type": BUTTON, "style": style, "label": clip(label, 80),
         "custom_id": custom_id}
    if disabled:
        b["disabled"] = True
    return b


def section(text, accessory):
    return {"type": SECTION, "components": [td(text)],
            "accessory": accessory}


def row(*components):
    return {"type": ROW, "components": [c for c in components if c]}


def container(children, colour=None):
    c = {"type": CONTAINER, "components": [x for x in children if x]}
    if colour is not None:
        c["accent_color"] = colour
    return c


def opt(label, value, description=None, default=False):
    o = {"label": clip(label, 100), "value": clip(value, 100)}
    if description:
        o["description"] = clip(description, 100)
    if default:
        o["default"] = True
    return o


def sel(custom_id, options, placeholder, min_values=1, max_values=1):
    return {"type": STRING_SELECT, "custom_id": custom_id,
            "options": options[:25], "placeholder": clip(placeholder, 150),
            "min_values": min_values, "max_values": max_values}


def picker(kind, custom_id, placeholder, min_values=0, max_values=25,
           defaults=None, channel_types=None):
    s = {"type": kind, "custom_id": custom_id,
         "placeholder": clip(placeholder, 150),
         "min_values": min_values, "max_values": max_values}
    if defaults:
        s["default_values"] = defaults[:max_values]
    if channel_types:
        s["channel_types"] = list(channel_types)
    return s


def people_defaults(value):
    out = []
    for p in value if isinstance(value, list) else []:
        p = str(p)
        if p.startswith("role:"):
            out.append({"id": p[5:], "type": "role"})
        elif _ID.fullmatch(p):
            out.append({"id": p, "type": "user"})
    return out


def message(R, comps):
    data = {"flags": COMPONENTS_V2 | (EPHEMERAL if R.new else 0),
            "components": finish_text(comps),
            "allowed_mentions": {"parse": []}}
    return {"type": 4 if R.new else 7, "data": data}


LIMITS = {"label": 80, "placeholder": 150}


def _all(comps):
    for c in comps or []:
        if isinstance(c, dict):
            yield c
            yield from _all(c.get("components"))
            for k in ("accessory", "component"):
                if isinstance(c.get(k), dict):
                    yield from _all([c[k]])


def text_size(comps):
    n = 0
    for c in _all(comps):
        for k in ("content", "label", "placeholder", "description"):
            if isinstance(c.get(k), str):
                n += len(c[k])
        for o in c.get("options") or []:
            n += len(o.get("label", "")) + len(o.get("description", ""))
    return n


def finish_text(comps):
    """Labels, options and placeholders as plain text (names unescaped),
    then the page within TEXT_BUDGET: option labels shortened to 60,
    then option descriptions dropped, then long texts clipped."""
    for c in _all(comps):
        for k, n in LIMITS.items():
            if isinstance(c.get(k), str):
                c[k] = clip(plain(c[k]), n)
        if c.get("type") == LABEL and isinstance(c.get("description"), str):
            c["description"] = clip(plain(c["description"]), 100)
        for o in c.get("options") or []:
            o["label"] = clip(plain(o["label"]), 100)
            if "description" in o:
                o["description"] = clip(plain(o["description"]), 100)
    if text_size(comps) <= TEXT_BUDGET:
        return comps
    for c in _all(comps):
        for o in c.get("options") or []:
            o["label"] = clip(o["label"], 60)
    for c in sorted((c for c in _all(comps) if c.get("options")),
                    key=lambda c: -len(c["options"])):
        if text_size(comps) <= TEXT_BUDGET:
            return comps
        for o in c["options"]:
            o.pop("description", None)
    while text_size(comps) > TEXT_BUDGET:
        tds = [c for c in _all(comps) if c.get("type") == TEXT]
        big = max(tds, key=lambda c: len(c["content"]), default=None)
        if big is None or len(big["content"]) < 80:
            break
        over = text_size(comps) - TEXT_BUDGET
        big["content"] = clip(big["content"],
                              max(60, len(big["content"]) - over - 1))
    return comps


def card(text, colour=None, extra=()):
    return [container([td(text)] + list(extra), colour)]


def notice(text):
    """A new ephemeral message; the page it came from stays as it was."""
    return {"type": 4, "data": {"flags": NEW_FLAGS,
                                "components": card(text),
                                "allowed_mentions": {"parse": []}}}


def modal(custom_id, title, components):
    comps = finish_text(components[:5])
    for c in comps:
        if c.get("type") == LABEL:
            c["label"] = clip(plain(c["label"]), 45)
    return {"type": 9, "data": {"custom_id": custom_id,
                                "title": clip(plain(title), 45),
                                "components": comps}}


def labelled(text, component, description=None):
    c = {"type": LABEL, "label": clip(text, 45), "component": component}
    if description:
        c["description"] = clip(description, 100)
    return c


def text_input(custom_id, style, max_length, value=None, placeholder=None,
               required=False):
    t = {"type": TEXT_INPUT, "custom_id": custom_id, "style": style,
         "max_length": max_length, "required": required}
    if value:
        t["value"] = str(value)[:max_length]
    if placeholder:
        t["placeholder"] = clip(placeholder, 100)
    return t


def count_components(comps):
    n = 0
    for c in comps or []:
        if not isinstance(c, dict):
            continue
        n += 1
        n += count_components(c.get("components"))
        if isinstance(c.get("accessory"), dict):
            n += 1
        if isinstance(c.get("component"), dict):
            n += count_components([c["component"]])
    return n


# ---------------------------------------------------------------------------
# Who and where
# ---------------------------------------------------------------------------
def identify(ix, ctx):
    """(user id, "owner" | "admin" | None, the admin's server id)."""
    user = (ix.get("member") or {}).get("user") or ix.get("user") or {}
    uid = str(user.get("id") or "")
    if uid and uid in {str(o) for o in ctx.owners}:
        return uid, "owner", None
    gid = ix.get("guild_id")
    member = ix.get("member")
    if uid and gid and isinstance(member, dict):
        try:
            perms = int(member.get("permissions") or 0)
        except (TypeError, ValueError):
            perms = 0
        if perms & (MANAGE_GUILD | ADMINISTRATOR):
            return uid, "admin", str(gid)
    return uid, None, None


def _harvest_names(ix):
    """Names the interaction itself carries (the clicking user, resolved
    users and members, a context command's message author)."""
    out = {}
    people = set()

    def user(u, member=None):
        if not isinstance(u, dict) or not u.get("id"):
            return
        n = ((member or {}).get("nick") or u.get("global_name")
             or u.get("username"))
        if n:
            out[str(u["id"])] = str(n)
            people.add(str(u["id"]))
    m = ix.get("member") or {}
    user(m.get("user"), m)
    user(ix.get("user"))
    res = (ix.get("data") or {}).get("resolved") or {}
    members = res.get("members") or {}
    for uid, u in (res.get("users") or {}).items():
        user(dict(u, id=uid), members.get(uid))
    for rid, r in (res.get("roles") or {}).items():
        if isinstance(r, dict) and r.get("name"):
            out[str(rid)] = "@" + str(r["name"])
            people.add(str(rid))
    for msg in (res.get("messages") or {}).values():
        if isinstance(msg, dict):
            user(msg.get("author"), msg.get("member"))
    for chid, ch in (res.get("channels") or {}).items():
        if isinstance(ch, dict) and ch.get("name"):
            out[str(chid)] = str(ch["name"])
    return out, people


class Req:
    """One interaction being answered."""

    def __init__(self, ix, ctx):
        self.ix, self.ctx = ix, ctx
        self.st = ctx.settings
        self.uid, self.role, self.admin_gid = identify(ix, ctx)
        self.guild_id = str(ix.get("guild_id") or "") or None
        self.data = ix.get("data") or {}
        self.new = False
        self.banner = None
        self.extra_names, self.extra_users = _harvest_names(ix)
        self._guilds = None
        self._chans = {}
        self.go_page = 0
        self.chan_page = 0
        self.chan_query = None
        self.server_page = 0
        self.server_query = None
        self.pending = None     # a "you and people you pick" with no one
        self.fixed = None       # whose refusal a Why fix just fixed
        self.check = None       # DMs: the person checked
        self.thread_note = False
        self.resolved_channel = None

    @property
    def owner(self):
        return self.role == "owner"

    @property
    def admin(self):
        return self.role == "admin"

    @property
    def now(self):
        try:
            return float(self.ctx.now)
        except (TypeError, ValueError, AttributeError):
            return time.time()

    def reload(self):
        fn = getattr(self.ctx, "reload", None)
        try:
            self.st = fn() if callable(fn) else ms.load(self.st.path)
        except Exception as e:  # keep showing the old values; it was saved
            self.ctx.log(f"settings_ui: reload after a write failed: {e}")

    # -- the directory ------------------------------------------------------
    def guilds(self):
        if self._guilds is None:
            out = {}
            try:
                raw = self.ctx.guilds() or []
            except Exception:
                raw = []
            for g in raw:
                if isinstance(g, dict) and g.get("id"):
                    out[str(g["id"])] = g
            self._guilds = out
        return self._guilds

    def channels(self, gid):
        gid = str(gid)
        if gid not in self._chans:
            out = {}
            try:
                raw = self.ctx.channels(gid) or []
            except Exception:
                raw = []
            for c in raw:
                if isinstance(c, dict) and c.get("id"):
                    out[str(c["id"])] = c
            self._chans[gid] = out
        return self._chans[gid]

    def chan(self, gid, cid_):
        return self.channels(gid).get(str(cid_))

    def category_of(self, gid, cid_):
        ch = self.chan(gid, cid_) or {}
        parent = str(ch.get("parent_id") or "") or None
        if parent and (self.chan(gid, parent) or {}).get("type") == CATEGORY:
            return parent
        return None

    def members(self, gid):
        n = (self.guilds().get(str(gid)) or {}).get("member_count")
        return n if isinstance(n, int) and not isinstance(n, bool) else None

    def can_see(self, gid, cid_):
        """False when the bot cannot read the channel (or it is not in
        the directory at all while the server's channels are known)."""
        chans = self.channels(gid)
        ch = chans.get(str(cid_))
        if ch is None:
            return not chans and self.resolved_channel is None
        return ch.get("readable") is not False

    def look(self, some_id):
        """A name for a server, channel, role or person (None: unknown),
        escaped for a Text Display (see esc)."""
        n = self._look(some_id)
        return esc(n) if n else None

    def _look(self, some_id):
        i = str(some_id)
        if i in self.extra_names:
            return self.extra_names[i]
        g = self.guilds().get(i)
        if g and g.get("name"):
            return str(g["name"])
        for chans in self._chans.values():
            if i in chans and chans[i].get("name"):
                return str(chans[i]["name"])
        for name in ("names", "display_name"):
            fn = getattr(self.ctx, name, None)
            if callable(fn):
                try:
                    n = fn(i)
                except Exception:
                    n = None
                if n:
                    return str(n)
        return (self.st.labels or {}).get(i)

    def person(self, uid):
        """A person's (or role's) name, escaped, or None: only from the
        interaction's resolved data and the people the monitor has seen,
        never a server's or channel's name for a forged id."""
        i = str(uid)
        n = self.extra_names.get(i) if i in self.extra_users else None
        if not n:
            fn = getattr(self.ctx, "display_name", None)
            try:
                n = fn(i) if callable(fn) else None
            except Exception:
                n = None
        if n and str(n).startswith("@"):
            n = str(n)[1:]
        return esc(n) if n else None

    def typed(self):
        """(channel id, in a thread?) where the interaction happened, a
        thread counting as its parent; (None, False) outside a server."""
        if not self.guild_id:
            return None, False
        ch = self.ix.get("channel") or {}
        c = str(self.ix.get("channel_id") or ch.get("id") or "")
        if not _ID.fullmatch(c):
            return None, False
        if ch.get("type") in THREAD_TYPES and ch.get("parent_id"):
            return str(ch["parent_id"]), True
        return c, False

    def in_guild(self, gid):
        return self.guild_id is not None and self.guild_id == str(gid)

    def place(self, tok):
        """The Place a token names, or None (malformed, a category)."""
        m = _PLACE.fullmatch(tok or "")
        if not m:
            return None
        if tok == "x":
            return ms.Place.defaults()
        if tok == "d":
            return ms.Place.dms()
        if m[1]:
            return ms.Place.server(m[1])
        gid, c = m[2], m[3]
        if (self.chan(gid, c) or {}).get("type") == CATEGORY:
            return None
        return ms.Place.channel(gid, c, self.category_of(gid, c))

    def name(self, place):
        return ms.place_name(place, self.look)

    def gname(self, gid):
        return self.look(gid) or "this server"


# ---------------------------------------------------------------------------
# Values at a place
# ---------------------------------------------------------------------------
class Val:
    """One setting at one place: path, set here?, value here, source,
    value in force, value without this layer, config.yaml's value."""

    def __init__(self, R, place, key):
        st = R.st
        self.key = key
        self.place = place
        self.spec = st.spec(key)
        self.path = ms.place_path(st, place, key)
        self.set, self.here, self.src = st.layer(self.path, key)
        self.value = ms.place_value(st, place, key)
        sc = _scope_at(place, key, self.path)
        self.inherited = copy_default(key) if sc is None \
            else st.value_without(sc, key, self.path)
        self.in_file = (self.path, key) in st.file_values
        self.file_value = st.file_values.get((self.path, key))


def copy_default(key):
    spec = ms.TOP.get(key) or ms.CORE.get(key)
    return spec.default if spec is not None else None


def _scope_at(place, key, path=None):
    """The Scope key resolves in at place (None: bot-wide)."""
    if path is not None and place.kind == "defaults":
        return {("everywhere",): ms.Scope.everywhere(),
                ("all_servers",): ms.Scope("all_servers"),
                ("dms",): ms.Scope.dm()}.get(path)
    return place.scope(key)


def value(R, place, key):
    sc = _scope_at(place, key)
    return R.st.top(key) if sc is None else R.st.get(key, sc)


def offered(R, place, key):
    try:
        return key in ms.place_keys(R.st, place, "basics") \
            or key in ms.place_keys(R.st, place, "more")
    except Exception:
        return False


def vl(R, key, v):
    """A value in plain words."""
    if v is None and R.st.spec(key) is not None \
            and R.st.spec(key).type == "number":
        return "No cap"
    if key == "plugins.welcome.channel" and isinstance(v, str) \
            and _ID.fullmatch(v):
        n = R.look(v)
        return f"#{n}" if n else "a channel"
    return ms.value_label(key, v, R.look)


def mention_list(value, limit=10):
    out = []
    for p in value if isinstance(value, list) else []:
        p = str(p)
        out.append(f"<@&{p[5:]}>" if p.startswith("role:") else f"<@{p}>")
    if not out:
        return "nobody"
    more = len(out) - limit
    out = out[:limit]
    return ", ".join(out) + (f" and {more} more" if more > 0 else "")


def short_date(at):
    try:
        d = datetime.strptime(str(at), "%Y-%m-%dT%H:%M:%SZ")
        return f"{d:%b} {d.day}"
    except ValueError:
        return "earlier"


def ago(R, when):
    """"4 min ago", "3 h ago", "Oct 19" for a unix time or ISO stamp."""
    if isinstance(when, str):
        try:
            when = datetime.strptime(when, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc).timestamp()
        except ValueError:
            return "earlier"
    try:
        secs = max(0, R.now - float(when))
    except (TypeError, ValueError):
        return "earlier"
    if secs < 60:
        return f"{int(secs)} s ago"
    if secs < 3600:
        return f"{int(secs // 60)} min ago"
    if secs < 86400:
        return f"{int(secs // 3600)} h ago"
    d = datetime.fromtimestamp(float(when), timezone.utc)
    return f"{d:%b} {d.day}"


def snowflake_time(mid):
    try:
        return ((int(mid) >> 22) + 1420070400000) / 1000.0
    except (TypeError, ValueError):
        return None


def who_text(R, by):
    """"@you", a mention, or a name such as muse.ai."""
    by = str(by or "")
    if by and by == R.uid:
        return "@you"
    if _ID.fullmatch(by):
        return f"<@{by}>"
    return esc(by) if by else "someone"


def tag(R, v):
    """The source tag on a setting's label line ("" when not set here)."""
    if not v.set:
        return ""
    if v.src and v.src[0] == "file":
        return " · ● set in config.yaml"
    by = v.src[1] if v.src and len(v.src) > 1 else None
    at = v.src[2] if v.src and len(v.src) > 2 else None
    t = f" · ● changed here by {who_text(R, by)}, {short_date(at)}"
    if v.in_file and v.file_value != v.here:
        t += f" (config.yaml says {vl(R, v.key, v.file_value)})"
    return t


def parent_name(R, place, key):
    """Who a place follows for key: the category (when it sets key), the
    server, or Defaults."""
    if place.kind in ("server", "dms"):
        return "Defaults"
    if place.kind != "channel":
        return None
    cat = place.category_id
    if cat:
        path = ("servers", place.guild_id, "channels", cat)
        if R.st.layer(path, key)[0]:
            return f"{R.look(cat) or 'its category'} (category)"
    return R.gname(place.guild_id)


# ---------------------------------------------------------------------------
# Selects for one setting
# ---------------------------------------------------------------------------
def option_help(R, place, key, v):
    where = R.name(place)
    if key == "who.replies":
        if v == "off":
            if place.kind == "server":
                return "Quiet in this server, except channels set otherwise"
            if place.kind == "defaults":
                return ("Quiet in every server, except places set "
                        "otherwise")
            return f"Never answers in {where}"
        return {"conversational": "Answers @mentions and replies, plus "
                                  "that person's follow-ups for 2 minutes",
                "when-addressed": "Answers only @mentions and replies to "
                                  "its messages"}.get(v)
    if key == "plugins.media.who" and v == "everyone":
        return (f"Anyone in {where}; generation can cost real money"
                if place.kind != "defaults" else
                "Anyone; generation can cost real money")
    if key == "who.dms_from" and v == "owners" and len(R.ctx.owners) > 1:
        return "The bot's owners"
    return OPTION_HELP.get((key, v))


OPTION_HELP = {
    ("privacy.memory", "everything"): "Your job, location and other saved "
                                      "memory; risky in big servers",
    ("privacy.memory", "public-only"): "Name, interests, public projects "
                                       "and opinions",
    ("privacy.memory", "nothing"): "Replies as if it had no memory of you",
    ("who.bots", "when-addressed"): "Answers bots that @mention or reply "
                                    "to it",
    ("who.bots", "ignore"): "Never answers bots",
    ("privacy.suspicious", "hold-for-review"): "Held until an owner looks; "
                                               "nothing is sent",
    ("privacy.suspicious", "ignore"): "Dropped silently",
    ("style.length", "brief"): "Short replies",
    ("style.length", "normal"): "The usual",
    ("style.length", "detailed"): "Longer, fuller replies",
    ("who.dms_from", "owners"): "Only the bot's owners",
    ("who.dms_from", "list"): "Owners, plus the people below",
    ("who.dms_from", "shared-servers"): "Anyone who shares a server with "
                                        "the bot",
    ("who.dms_from", "anyone"): "Anyone on Discord, strangers included",
    ("who.dms_from", "nobody"): "No DM gets an answer, not even yours",
    ("plugins.media.who", "owners"): "Only the bot's owners",
    ("plugins.media.who", "list"): "Owners plus the people below",
}


# Option order: the most open first, as people read it.
ORDER = {"who.replies": ("conversational", "when-addressed", "off"),
         "privacy.memory": ("everything", "public-only", "nothing"),
         "who.bots": ("when-addressed", "ignore"),
         "privacy.suspicious": ("hold-for-review", "ignore")}


def value_name(R, key, v):
    if key == "who.dms_from" and v == "owners" and len(R.ctx.owners) > 1:
        return "Only you and the other owners"
    if key == "plugins.media.who" and v == "list":
        return "You and people you pick"
    if key == "plugins.media.who" and v == "everyone":
        return "Everyone here"
    return ms.VALUE_LABELS.get((key, v)) or vl(R, key, v)


def has_same(place, key):
    return place.kind not in ("defaults",) and not (
        place.kind == "dms" and key == "who.dms_from")


def choice_select(R, place, key, custom_id, values=None, placeholder=None):
    """A choice select: "Same as <parent>: <value>" first (value "="),
    then each value; the one in force is selected."""
    v = Val(R, place, key)
    spec = v.spec
    same = has_same(place, key)
    opts = []
    if same:
        parent = parent_name(R, place, key)
        inh = v.inherited
        shown = value_name(R, key, "list") if isinstance(inh, list) \
            and key == "plugins.media.who" else value_name(R, key, inh)
        opts.append(opt(f"Same as {parent}: {shown}", "=",
                        f"Follows {parent} and changes when it does",
                        default=not v.set))
    cur = v.here if same else v.value
    if key == "plugins.media.who" and isinstance(cur, list):
        cur = "list"
    vals = list(values or ORDER.get(key) or spec.values)
    for x in vals:
        d = option_help(R, place, key, x)
        if place.kind == "defaults" and x == spec.default:
            d = "Built-in default" + (f" · {d}" if d else "")
        if v.in_file and v.set and v.src and v.src[0] == "discord" \
                and x == v.file_value:
            d = "What config.yaml says"
        opts.append(opt(value_name(R, key, x), x, d,
                        default=(v.set or not same) and cur == x))
    if (v.set or not same) and cur in spec.hidden_values:
        opts.append(opt("Answer everything (old setting from config.yaml)",
                        cur, "Pick another to change it", default=True))
    return sel(custom_id, opts, placeholder or f"{ms.label(key)}…")


def channel_select_or_fallback(R, gid, custom_id, origin_tok, placeholder,
                               types=GO_CHANNEL_TYPES, current=None,
                               findable=False):
    """A type 8 channel select when the page's server is the interaction's
    server; else a paged string select of that server's channels (23 a
    page, channels with their own settings first)."""
    if R.in_guild(gid):
        defaults = [{"id": str(current), "type": "channel"}] \
            if current and _ID.fullmatch(str(current)) else None
        return picker(CHANNEL_SELECT, custom_id, placeholder, 1, 1,
                      defaults, types), 1
    listed = set(R.st.channels_listed(gid))
    chans = [c for c in R.channels(gid).values() if c.get("type") in types]
    if R.chan_query:
        q = R.chan_query.lower()
        chans = [c for c in chans if q in str(c.get("name") or "").lower()]
    chans.sort(key=lambda c: (str(c["id"]) not in listed,
                              str(c.get("name") or "").lower(),
                              str(c["id"])))
    pages = max(1, math.ceil(len(chans) / CHANNELS_PER_PAGE))
    page = min(max(0, R.chan_page), pages - 1)
    opts = []
    if page > 0:
        opts.append(opt("Previous channels…", f"p{page - 1}",
                        f"Page {page} of {pages}"))
    for c in chans[page * CHANNELS_PER_PAGE:(page + 1) * CHANNELS_PER_PAGE]:
        parent = str(c.get("parent_id") or "")
        desc = "● has its own settings" if str(c["id"]) in listed else None
        if parent and R.chan(gid, parent):
            pn = R.chan(gid, parent).get("name") or "a category"
            desc = (desc + f" · in {pn}") if desc else f"in {pn}"
        opts.append(opt("#" + str(c.get("name") or "channel"), str(c["id"]),
                        desc, default=str(c["id"]) == str(current or "")))
    if page < pages - 1:
        opts.append(opt("More channels…", f"p{page + 1}",
                        f"Page {page + 2} of {pages}"))
    if findable and pages > FIND_AFTER_PAGES and page == 0 \
            and not R.chan_query:
        opts.append(opt("Find a channel by name…", "find",
                        "Type part of its name"))
    if not opts:
        return None, pages
    if R.chan_query:
        placeholder = f"Channels matching “{clip(R.chan_query, 40)}”…"
    elif pages > 1:
        placeholder = f"{placeholder} (page {page + 1} of {pages})"
    return sel(custom_id, opts, placeholder), pages


def server_options(R, page, head=(), more="p"):
    """Options for a "Go to..." select: head (Defaults, DMs) on page 1,
    servers by name (22 a page), then paging options."""
    guilds = sorted(R.guilds().values(),
                    key=lambda g: (str(g.get("name") or "").lower(),
                                   str(g["id"])))
    if R.server_query:
        q = R.server_query.lower()
        guilds = [g for g in guilds if q in str(g.get("name") or "").lower()]
    pages = max(1, math.ceil(len(guilds) / SERVERS_PER_PAGE))
    page = min(max(0, page), pages - 1)
    listed = set(R.st.servers_listed())
    opts = []
    if page > 0:
        opts.append(opt("Previous servers…", f"{more}{page - 1}",
                        f"Page {page} of {pages}"))
    else:
        opts += list(head)
    for g in guilds[page * SERVERS_PER_PAGE:(page + 1) * SERVERS_PER_PAGE]:
        n = g.get("member_count")
        desc = f"{n:,} members" if isinstance(n, int) else "Size unknown"
        if str(g["id"]) in listed:
            desc += " · has its own settings"
        opts.append(opt(str(g.get("name") or "A server"), f"s{g['id']}",
                        desc))
    if page < pages - 1:
        opts.append(opt("More servers…", f"{more}{page + 1}",
                        f"Page {page + 2} of {pages}"))
    return opts, page, pages, len(guilds)


def go_select(R, origin):
    head = []
    if origin.kind != "defaults":
        head.append(opt("Defaults", "x", "What every server and DM follows"))
    if origin.kind != "dms":
        head.append(opt("DMs", "d", "Direct messages with the bot"))
    opts, _page, _pages, _n = server_options(R, R.go_page, head)
    return sel(cid("go", ptok(origin)), opts, "Go to…")


# ---------------------------------------------------------------------------
# Banner and last-change slot
# ---------------------------------------------------------------------------
def banner_done(R, text, undo_place=None, group=None):
    text = clip(f"**Done:** {text}", 1500)
    if undo_place is not None and group:
        try:
            return [section(text, btn("Undo", cid("un", ptok(undo_place),
                                                  group)))]
        except BadId:
            return [section(text, btn("Undo", cid("un",
                                                  ptok(undo_place))))]
    return [td(text)]


def banner_error(text, retry=None):
    out = [td(clip(f"**Not saved:** {text}", 1500))]
    if retry:
        out.append(row(btn("Try again", retry, PRIMARY)))
    return out


def _undo_scope(R, place):
    """(place an Undo works on, include channels?, admin's server)."""
    if R.admin:
        return ms.Place.server(R.admin_gid), True, R.admin_gid
    return place, place.kind == "server", None


def undo_forward(R, plan):
    """The changes an Undo would revert, as "In #general, When it
    answers: Chatty → Off" (what was changed, not what Undo does)."""
    sim = ms.simulate(R.st, plan)
    parts = []
    for ch in plan:
        sc = _scope_at(ch.place, ch.key, ch.path)
        before = sim.top(ch.key) if sc is None else sim.get(ch.key, sc)
        now = R.st.top(ch.key) if sc is None else R.st.get(ch.key, sc)
        parts.append(ms.describe_change(ch.place, ch.key, before, now,
                                        R.look, ch.path))
    return "; ".join(parts)


def last_author(R, plan):
    """(who, when) of the change an Undo would revert."""
    if not plan:
        return None, None
    ks = ms.format_key(ms.change_path(R.st, plan[0]), plan[0].key)
    for h in reversed((R.st.overlay_doc or {}).get("history") or []):
        if isinstance(h, dict) and h.get("key") == ks and "to" in h \
                and not h.get("undone"):
            return h.get("by"), h.get("at")
    return None, None


def last_change_slot(R, place, label=None):
    """SEC "-# Last change here: ..." [Undo], or []."""
    if R.banner or R.st.overlay_error or R.role is None:
        return []
    up, children, admin_gid = _undo_scope(R, place)
    try:
        _text, gid = ms.undo_preview(R.st, up, include_children=children,
                                     names=R.look, admin_gid=admin_gid)
        if not gid:
            return []
        plan = ms.undo_plan(R.st, up, children, admin_gid)
        forward = undo_forward(R, plan)
    except Exception as e:
        R.ctx.log(f"settings_ui: last change: {e}")
        return []
    by, at = last_author(R, plan)
    when = f" ({who_text(R, by).replace('@you', 'you')}, {ago(R, at)})" \
        if by else ""
    head = label or ("Last change in " + R.name(up) + " or its channels"
                     if children else "Last change here")
    text = clip(f"-# {head}: {forward}{when}", 600)
    try:
        b = btn("Undo", cid("un", ptok(up), gid))
    except BadId:
        b = btn("Undo", cid("un", ptok(up)))
    return [section(text, b)]


def finish(R, place, inner, footer, colour=BLURPLE, slot=True,
           slot_label=None):
    """A page: banner, the container (with the last-change slot at its
    bottom), the rows below it."""
    inner = list(inner)
    if slot:
        inner += last_change_slot(R, place, slot_label)
    comps = list(R.banner or []) + [container(inner, colour)] + \
        [r for r in footer if r and r.get("components")]
    if R.st.overlay_error:
        comps = read_only(comps)
    return comps


WRITE_VERBS = ("set", "val", "ppl", "rm", "mod", "pre", "un", "pz", "rs",
               "fx", "re", "ok")


def _verb(c):
    parts = str(c.get("custom_id") or "").split(":")
    return parts[2] if len(parts) > 2 and parts[0] == PREFIX else None


def read_only(comps):
    """The overlay can't be read: write controls removed, a warning on
    top (navigation stays)."""
    out = [td(OVERLAY_BROKEN)]
    for c in comps:
        out.append(_strip(c))
    return [c for c in out if c]


def _strip(c):
    t = c.get("type")
    if t == SECTION:
        if _verb(c.get("accessory") or {}) in WRITE_VERBS:
            return c["components"][0]
        return c
    if t == ROW:
        kept = [x for x in c["components"] if _verb(x) not in WRITE_VERBS]
        if kept:
            return dict(c, components=kept)
        now = [o["label"] for x in c["components"]
               for o in x.get("options") or [] if o.get("default")]
        return td(f"-# Now: {now[0]}") if now and _verb(
            c["components"][0]) == "set" else None
    if t == CONTAINER:
        kept = [x for x in (_strip(y) for y in c["components"]) if x]
        return dict(c, components=kept)
    return c


# ---------------------------------------------------------------------------
# Shared pieces of pages
# ---------------------------------------------------------------------------
def toggle_button(R, place, key, page):
    """[Welcome: Off · Turn on] style button with an absolute value."""
    on = bool(value(R, place, key))
    name = EXTRA_SHORT.get(key) or ms.label(key)
    return btn(f"{name}: {'On · Turn off' if on else 'Off · Turn on'}",
               cid("val", ptok(place), kcode(key), "off" if on else "on",
                   page), SUCCESS if on else SECONDARY)


EXTRA_SHORT = {"plugins.welcome.enabled": "Welcome",
               "plugins.media.enabled": "Image and video"}


def extra_keys(keys):
    """The Extras' on/off keys, welcome first."""
    out = [k for k in keys if k.startswith("plugins.")
           and k.endswith(".enabled")]
    return sorted(out, key=lambda k: (k != "plugins.welcome.enabled", k))


def toggle_section(R, place, key, page):
    v = Val(R, place, key)
    on = bool(v.value)
    if v.set:
        state = ("On" if on else "Off") + " · ● changed here"
        if v.src and v.src[0] == "discord":
            state += f" by {who_text(R, v.src[1])}, {short_date(v.src[2])}"
        elif v.src:
            state = ("On" if on else "Off") + " · ● set in config.yaml"
    else:
        parent = parent_name(R, place, key) or "Defaults"
        state = ("On" if on else "Off") + (
            " · built-in default" if place.kind == "defaults"
            else f" · same as {parent}")
    return section(f"**{ms.label(key)}**\n{state}",
                   btn("Turn off" if on else "Turn on",
                       cid("val", ptok(place), kcode(key),
                           "off" if on else "on", page),
                       SUCCESS if not on else SECONDARY))


def notes_text(R, place):
    """The Instructions section's text."""
    if place.kind == "channel":
        srv = ms.Place.server(place.guild_id)
        base = value(R, srv, "style.notes")
        own = Val(R, place, "style.notes")
        gname = R.gname(place.guild_id)
        head = (f"{gname}'s instructions apply here." if base
                else f"{gname} has no instructions.")
        added = f"“{esc(clip(own.here, 120))}”" if own.set and own.here \
            else "nothing"
        return (f"**Instructions for the Muse**{tag(R, own)}\n{head} "
                f"Added for {R.name(place)}: {added}")
    v = Val(R, place, "style.notes")
    if v.set:
        notes = f"“{esc(clip(v.here, 160))}”" if v.here else "None set here"
    elif place.kind == "defaults":
        notes = "None yet"
    else:
        notes = f"Same as Defaults: “{esc(clip(v.value, 120))}”" if v.value \
            else "Same as Defaults: nothing"
    length = value(R, place, "style.length")
    return (f"**Instructions for the Muse**{tag(R, v)}\n{notes} · Reply "
            f"length: {value_name(R, 'style.length', length)}")


def cap_section(R, place, key, form, page, note=""):
    v = Val(R, place, key)
    if v.set:
        cur = "No cap" if v.here is None else f"{v.here:,} a day"
    elif place.kind == "defaults":
        cur = "No cap"
    else:
        cur = "No cap here"
    sc = _scope_at(place, key, v.path)
    others = [(c, n) for c, n in R.st.cap_layers(key, sc)
              if not _counter_is(c, place)] if sc is not None else []
    extra = ""
    if others:
        c, n = min(others, key=lambda x: x[1])
        extra = f"\n-# {_counter_name(R, c)} also caps " + (
            "everyone" if key == "limits.per_person_per_day"
            else "replies") + f" at {n:,}."
    return section(f"**{ms.label(key)}**{tag(R, v)}\n{cur}{note}{extra}",
                   btn("Change", cid("mod", ptok(place), form, page)))


def _counter_is(counter, place):
    if counter == "everywhere":
        return place.kind == "defaults"
    if counter == "dms":
        return place.kind == "dms"
    return place.kind == "server" and counter == f"server:{place.guild_id}"


def _counter_name(R, counter):
    if counter == "everywhere":
        return "Defaults"
    if counter == "dms":
        return "DMs"
    return R.gname(counter.split(":", 1)[1])


def counter_place(counter):
    if counter == "everywhere":
        return ms.Place.defaults()
    if counter == "dms":
        return ms.Place.dms()
    if str(counter).startswith("server:"):
        return ms.Place.server(counter.split(":", 1)[1])
    return None


def nav_row(R, place, *extra):
    """[Defaults] [DMs] [Other servers] (minus the page itself)."""
    out = list(extra)
    if place.kind != "defaults":
        out.append(btn("Defaults", cid("pg", "x", "b")))
    if place.kind != "dms":
        out.append(btn("DMs", cid("pg", "d", "b")))
    out.append(btn("Other servers", cid("pg", ptok(place), "o")))
    return row(*out[:5])


def channel_row(R, place, gid, placeholder):
    s, pages = channel_select_or_fallback(
        R, gid, cid("gc", ptok(place)), ptok(place), placeholder,
        findable=True)
    return (row(s) if s else None), pages


def pause_button(R, place):
    if ms.is_paused(R.st, place):
        return btn(f"Resume {R.name(place)}" if place.kind != "defaults"
                   else "Resume everywhere", cid("rs", ptok(place)), SUCCESS)
    return btn(ms.pause_label(place, R.look).replace("Pause in ", "Pause "),
               cid("pz", ptok(place)), DANGER)


def paused_line(R, place):
    """"**Paused:** Off in #x since Oct 19 (you)." when paused there."""
    if place.kind == "defaults" or not ms.is_paused(R.st, place):
        return None
    v = Val(R, place, "who.replies")
    who, when = "", ""
    if v.src and v.src[0] == "discord":
        who = f" ({who_text(R, v.src[1]).replace('@you', 'you')})"
        when = f" since {short_date(v.src[2])}"
    elif v.src and v.src[0] == "file":
        who = " (config.yaml)"
    return f"**Paused:** Off in {R.name(place)}{when}{who}."


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
def members_text(R, gid):
    n = R.members(gid)
    return f"{n:,} members" if n is not None else "size unknown"


def page_channel(R, place):
    """S1: one channel (owner)."""
    gid, tok = place.guild_id, ptok(place)
    gname, cname = R.gname(gid), R.name(place)
    paused = ms.is_paused(R.st, place)
    lines = [f"## {cname}",
             f"-# In {gname} · {members_text(R, gid)}. A channel can "
             f"change when it answers, add instructions and switch "
             f"extras; everything else follows {gname}."]
    if R.thread_note:
        lines.append(f"-# You ran this in a thread; threads follow "
                     f"{cname}.")
    p = paused_line(R, place)
    if p:
        lines.append(p)
    if not R.can_see(gid, place.channel_id):
        lines.append(f"-# The bot can't see {cname}, so it never answers "
                     f"there.")
    inner = [td("\n".join(lines)), sep()]
    keys = ms.place_keys(R.st, place, "basics")
    if "who.replies" in keys:
        v = Val(R, place, "who.replies")
        inner.append(td(f"**When it answers in {cname}**{tag(R, v)}"))
        inner.append(row(choice_select(R, place, "who.replies",
                                       cid("set", tok, "r"))))
    if "style.notes" in keys:
        inner.append(section(notes_text(R, place),
                             btn("Edit", cid("mod", tok, "i", "b"),
                                 PRIMARY)))
    for k in keys:
        if k.startswith("plugins.") and k.endswith(".enabled"):
            inner.append(toggle_section(R, place, k, "b"))
    chan_row, _pages = channel_row(R, place, gid,
                                  f"Go to another channel in {gname}…")
    nav = [btn(gname, cid("pg", f"s{gid}", "b"), PRIMARY)]
    footer = [row(pause_button(R, place),
                  btn("Why no reply?", cid("pg", tok, "y"))),
              chan_row]
    footer.append(nav_row(R, place, *nav))
    return finish(R, place, inner, footer, GREY if paused else BLURPLE)


def page_server(R, place):
    """S2: a server's basics (owner)."""
    gid, tok = place.guild_id, ptok(place)
    gname = R.gname(gid)
    preset = ms.matching_preset(R.st, place)
    sub = f"Matches the **{preset}** preset" if preset else \
        "Custom settings"
    lines = [f"## {gname}", f"-# {members_text(R, gid)} · {sub}"]
    p = paused_line(R, place)
    if p:
        lines.append(p)
    inner = [td("\n".join(lines)),
             row(sel(cid("pre", tok), [
                 opt("Friends", "friends", "Chatty, no caps, may use what "
                     "it remembers about you"),
                 opt("Community", "community", "Only when @mentioned, "
                     "public facts only, 20 replies per person a day"),
                 opt("Work", "work", "Only when @mentioned, nothing "
                     "personal, ignores other bots")], "Apply a preset…")),
             sep()]
    keys = ms.place_keys(R.st, place, "basics")
    if "who.replies" in keys:
        v = Val(R, place, "who.replies")
        inner.append(td(f"**When it answers in {gname}**{tag(R, v)}"))
        inner.append(row(choice_select(R, place, "who.replies",
                                       cid("set", tok, "r"))))
    if "privacy.memory" in keys:
        v = Val(R, place, "privacy.memory")
        inner.append(td(f"**What it may say about you**{tag(R, v)}"))
        inner.append(row(choice_select(R, place, "privacy.memory",
                                       cid("set", tok, "m"))))
    if "style.notes" in keys:
        inner.append(section(notes_text(R, place),
                             btn("Edit", cid("mod", tok, "i", "b"),
                                 PRIMARY)))
    if "limits.per_person_per_day" in keys:
        inner.append(cap_section(R, place, "limits.per_person_per_day",
                                 "c", "b"))
    extras = [toggle_button(R, place, k, "b") for k in extra_keys(keys)]
    if extras:
        inner.append(td("**Extras**"))
        inner.append(row(*(extras[:4] + [btn("Extras settings",
                                             cid("pg", tok, "e"))])))
    chan_row, _pages = channel_row(R, place, gid,
                                  f"Go to a channel in {gname}…")
    footer = [row(btn("More settings", cid("pg", tok, "m")),
                  btn("Why no reply?", cid("pg", tok, "y")),
                  pause_button(R, place)),
              chan_row]
    footer.append(nav_row(R, place))
    paused = ms.is_paused(R.st, place)
    return finish(R, place, inner, footer, GREY if paused else BLURPLE)


def roles_here(R, place):
    """Roles can be picked for place: it is a server, and the
    interaction is in that server (a role belongs to one server)."""
    return place.guild_id is not None and R.in_guild(place.guild_id)


def people_select(R, place, key, kind, placeholder, add_only=False,
                  value_=None):
    """A mentionable select in the place's own server; elsewhere a user
    select (roles of another server mean nothing; the roles already on
    the list are kept, see v_ppl)."""
    v = value(R, place, key) if value_ is None else value_
    if kind == MENTIONABLE_SELECT and not roles_here(R, place):
        kind = USER_SELECT
        v = [p for p in v if not str(p).startswith("role:")] \
            if isinstance(v, list) else v
        placeholder = placeholder.replace("people or roles", "people")
    s = picker(kind, cid("ppl", ptok(place), kcode(key)), placeholder,
               1 if add_only else 0, 25,
               None if add_only else people_defaults(v))
    return row(s)


def page_more(R, place):
    """S3: Only answer / Never answer / Other bots / caps / injection
    (a server, Defaults or DMs)."""
    tok = ptok(place)
    where = R.name(place)
    title = f"{where} › More settings"
    inner = [td(f"## {title}"), sep()]
    keys = ms.place_keys(R.st, place, "more")
    hint = ""
    if place.kind == "server" and not R.in_guild(place.guild_id):
        hint = (f"\n-# To pick roles, or search everyone there, run "
                f"/settings in "
                f"{where}.")
    if "who.allow" in keys and place.kind != "dms":
        v = Val(R, place, "who.allow")
        parent = parent_name(R, place, "who.allow")
        if v.set:
            state = f" · {mention_list(v.here)}{tag(R, v)}" \
                if isinstance(v.here, list) else f" · Everyone{tag(R, v)}"
        elif place.kind == "defaults":
            state = " · " + (mention_list(v.value)
                             if isinstance(v.value, list) else "everyone")
        else:
            state = f" · Same as {parent}: " + (
                mention_list(v.inherited)
                if isinstance(v.inherited, list) else "everyone")
        text = (f"**Only answer these people**{state}\n-# Pick people or "
                f"roles to answer only them in {where}. Owners always get "
                f"answers.{hint}")
        if isinstance(v.value, list):
            inner.append(section(text, btn(
                "Answer everyone", cid("val", tok, "al", "everyone", "m"))))
        else:
            inner.append(td(text))
        inner.append(people_select(R, place, "who.allow", MENTIONABLE_SELECT,
                                   "Everyone (pick to limit)…"))
    if "who.ignore" in keys:
        v = Val(R, place, "who.ignore")
        inner.append(td(f"**Never answer**{tag(R, v)}\n-# Wins over "
                        f"everything else. Owners can't be added."
                        + ("" if "who.allow" in keys
                           and place.kind != "dms" else hint)))
        inner.append(people_select(R, place, "who.ignore",
                                   MENTIONABLE_SELECT, "Nobody yet…"))
    if "who.bots" in keys:
        v = Val(R, place, "who.bots")
        inner.append(td(f"**Other bots**{tag(R, v)}"))
        inner.append(row(choice_select(R, place, "who.bots",
                                       cid("set", tok, "b"))))
    if "who.bot_exchange_limit" in keys:
        v = Val(R, place, "who.bot_exchange_limit")
        cur = "No cap" if v.value is None else f"{v.value:,} in a row"
        if not v.set and place.kind != "defaults":
            cur = f"Same as Defaults: {cur}"
        inner.append(section(
            f"**Replies in a row to other bots**{tag(R, v)}\n{cur}\n-# A "
            f"cap stops two bots answering each other forever.",
            btn("Change", cid("mod", tok, "bx", "m"))))
    if "limits.per_day" in keys:
        inner.append(cap_section(R, place, "limits.per_day", "c", "m"))
    if "privacy.suspicious" in keys:
        v = Val(R, place, "privacy.suspicious")
        inner.append(td(f"**Possible prompt injection**{tag(R, v)}"))
        inner.append(row(choice_select(R, place, "privacy.suspicious",
                                       cid("set", tok, "su"))))
    if place.kind == "defaults":
        n = len(R.st.get("privacy.never_share", ms.Scope.everywhere())
                or [])
        line = (f"-# Never share these phrases: {n} set in config.yaml. "
                f"Edit the file to change them.")
        if not R.st.get("abilities.owner_commands", ms.Scope.everywhere()):
            line += ("\n-# Owner requests (pin, post, react, threads) are "
                     "off in config.yaml.")
        inner.append(td(line))
    back = btn(f"Back to {where}", cid("pg", tok, "b"))
    footer = [back]
    if place.kind in ("server", "defaults"):
        footer.append(btn("Extras settings", cid("pg", tok, "e")))
    if place.kind != "defaults":
        footer.append(btn("Why no reply?", cid("pg", tok, "y")))
    return finish(R, place, inner, [row(*footer)])


def welcome_where(R, place):
    """The "Where welcomes go" text."""
    v = value(R, place, "plugins.welcome.channel")
    if place.kind == "defaults":
        if v == "off":
            return "Nowhere: welcomes are off unless a server picks one."
        if v == "system":
            return "Each server's system channel"
        return "A channel set for every server"
    gid = place.guild_id
    gname = R.gname(gid)
    if v == "off":
        return "Nowhere: welcomes are skipped. Pick a channel."
    if v == "system":
        g = R.guilds().get(gid) or {}
        if "system_channel_id" not in g:
            return "The server's system channel"
        sys_id = g.get("system_channel_id")
        if not sys_id:
            return (f"⚠ {gname} has no system channel, so welcomes are "
                    f"skipped. Pick a channel.")
        n = R.look(sys_id)
        text = "The server's system channel, " + (f"#{n}" if n
                                                   else "<#" + sys_id + ">")
        if (R.chan(gid, sys_id) or {}).get("can_send") is False:
            text += "\n-# The bot can't post there; welcomes are skipped."
        return text
    if _ID.fullmatch(str(v)):
        text = f"<#{v}>"
        if (R.chan(gid, v) or {}).get("can_send") is False:
            text += "\n-# The bot can't post there; welcomes are skipped."
        return text
    return f"#{esc(v)}"


def page_extras(R, place):
    """S4: Extras details (a server or Defaults)."""
    tok = ptok(place)
    where = R.name(place)
    inner = [td(f"## {where} › Extras"), sep()]
    st = R.st
    if "welcome" in st.manifests:
        inner.append(toggle_section(R, place, "plugins.welcome.enabled",
                                    "e"))
        inner.append(td(f"**Where welcomes go**\n{welcome_where(R, place)}"))
        cur = value(R, place, "plugins.welcome.channel")
        if place.kind == "server":
            s, _pages = channel_select_or_fallback(
                R, place.guild_id, cid("set", tok, "~welcome.channel"), tok,
                "Pick a channel…", TEXT_CHANNEL_TYPES, cur)
            if s:
                inner.append(row(s))
            inner.append(row(
                btn("Use the system channel",
                    cid("val", tok, "~welcome.channel", "system", "e")),
                btn("Edit welcome notes", cid("mod", tok, "wn", "e"))))
        else:
            inner.append(row(
                btn("Use each server's system channel",
                    cid("val", tok, "~welcome.channel", "system", "e")),
                btn("Off", cid("val", tok, "~welcome.channel", "off", "e")),
                btn("Edit welcome notes", cid("mod", tok, "wn", "e"))))
        sc = _scope_at(place, "plugins.welcome.daily_cap")
        cap = st.get("plugins.welcome.daily_cap", sc)
        age = st.get("plugins.welcome.max_age_hours", sc)
        line = ("-# " + (f"At most {cap:,} welcomes a day" if cap is not None
                         else "No daily limit on welcomes")
                + "; " + (f"joins older than {age:,} hours are skipped"
                          if age is not None else "old joins are welcomed "
                          "too") + ". Change these in config.yaml.")
        prompt = st.get("plugins.welcome.prompt", sc)
        spec = st.spec("plugins.welcome.prompt")
        if spec is not None and prompt != spec.default:
            line += (" A custom welcome prompt is set in config.yaml; your "
                     "notes are added to it.")
        inner.append(td(line))
    if "media" in st.manifests:
        if len(inner) > 2:
            inner.append(sep())
        inner.append(toggle_section(R, place, "plugins.media.enabled", "e"))
        v = Val(R, place, "plugins.media.who")
        pend = ("\nPick people below; nothing changes until you do."
                if R.pending == "plugins.media.who" else "")
        inner.append(td(f"**Who can ask for images and video**{tag(R, v)}"
                        f"{pend}"))
        inner.append(row(choice_select(
            R, place, "plugins.media.who", cid("set", tok, "~media.who"),
            values=["owners", "everyone", "list"])))
        if isinstance(v.value, list) or R.pending == "plugins.media.who":
            picked = [p for p in (v.value if isinstance(v.value, list)
                                  else []) if p not in R.ctx.owners]
            inner.append(people_select(R, place, "plugins.media.who",
                                       MENTIONABLE_SELECT, "Pick people…",
                                       value_=picked))
        vp = Val(R, place, "plugins.media.per_day")
        cur = "No cap" if vp.value is None else f"{vp.value:,} a day"
        if not vp.set and place.kind != "defaults":
            cur = f"Same as Defaults: {cur}"
        inner.append(section(f"**Generations per day**{tag(R, vp)}\n{cur}",
                             btn("Change", cid("mod", tok, "mpd", "e"))))
    return finish(R, place, inner,
                  [row(btn(f"Back to {where}", cid("pg", tok, "b")))])


def page_defaults(R):
    """S5: Defaults (the hub in a DM)."""
    place = ms.Place.defaults()
    guilds = R.guilds()
    own = [g for g in R.st.servers_listed() if g in guilds]
    n = len(guilds)
    lines = ["## Defaults",
             f"-# Every server and DM follows these unless it sets its own. "
             f"The bot is in {n:,} server{'s' if n != 1 else ''}; "
             + (f"{len(own):,} has its own settings." if len(own) == 1
                else f"{len(own):,} have their own settings.")]
    if ms.is_paused(R.st, place):
        lines.append("**Paused everywhere:** it isn't answering anyone "
                     "without its own setting.")
    inner = [td("\n".join(lines)), sep()]
    keys = ms.place_keys(R.st, place, "basics")
    if "who.replies" in keys:
        v = Val(R, place, "who.replies")
        inner.append(td(f"**When it answers in servers**{tag(R, v)}"))
        inner.append(row(choice_select(R, place, "who.replies",
                                       cid("set", "x", "r"))))
    access = ms.dm_access(R.st)
    inner.append(section(f"**Who can DM it**\n"
                         f"{dm_access_words(R, access)}",
                         btn("Change", cid("pg", "d", "b", "x"))))
    if "privacy.memory" in keys:
        v = Val(R, place, "privacy.memory")
        inner.append(td(f"**What it may say about you**{tag(R, v)}"))
        inner.append(row(choice_select(R, place, "privacy.memory",
                                       cid("set", "x", "m"))))
    if "style.notes" in keys:
        inner.append(section(notes_text(R, place),
                             btn("Edit", cid("mod", "x", "i", "b"),
                                 PRIMARY)))
    if "limits.per_person_per_day" in keys:
        inner.append(cap_section(
            R, place, "limits.per_person_per_day", "c", "b",
            "\n-# Counted per person across every server and DM together."))
    extras = [toggle_button(R, place, k, "b") for k in extra_keys(keys)]
    if extras:
        inner.append(td("**Extras in every server**"))
        inner.append(row(*(extras[:4] + [btn("Extras settings",
                                             cid("pg", "x", "e"))])))
    footer = [row(btn("More settings", cid("pg", "x", "m")),
                  btn("Bot-wide", cid("pg", "x", "w")),
                  pause_button(R, place))]
    here, _thread = R.typed()
    if R.guild_id:
        back = btn(f"Back to {R.gname(R.guild_id)}",
                   cid("pg", f"s{R.guild_id}", "b"))
        if here:
            back = btn(f"Back to #{R.look(here) or 'this channel'}",
                       cid("pg", f"c{R.guild_id}.{here}", "b"))
        footer.append(row(back, btn("DMs", cid("pg", "d", "b")),
                          btn("Other servers", cid("pg", "x", "o"))))
    else:
        footer.append(row(go_select(R, place)))
    return finish(R, place, inner, footer,
                  GREY if ms.is_paused(R.st, place) else BLURPLE)


def dm_access_words(R, access):
    if access == "list":
        people = value(R, ms.Place.dms(), "who.dms_from")
        return "You and " + mention_list(people, 5)
    return value_name(R, "who.dms_from", access)


def dm_people(R):
    v = value(R, ms.Place.dms(), "who.dms_from")
    return [p for p in v if p not in R.ctx.owners] \
        if isinstance(v, list) else []


def page_dms(R):
    """S6: DMs: who can DM it, and what it says there."""
    place = ms.Place.dms()
    inner = [td("## DMs\n-# Direct messages with the bot. Your own DMs "
                "always get answers unless Who can DM it is Nobody."), sep()]
    access = ms.dm_access(R.st)
    v = Val(R, place, "who.dms_from")
    tg = tag(R, v)
    if access == "nobody":
        tg = tag(R, Val(R, place, "who.replies"))
    inner.append(td(f"**Who can DM it**{tg}"))
    cur = "list" if R.pending == "who.dms_from" else access
    opts = []
    for x in ("owners", "list", "shared-servers", "anyone", "nobody"):
        d = option_help(R, place, "who.dms_from", x)
        if x == "owners":
            d = "Built-in default · " + d
        opts.append(opt(value_name(R, "who.dms_from", x)
                        if x != "list" else "You and people you pick", x, d,
                        default=x == cur))
    inner.append(row(sel(cid("set", "d", "df"), opts, "Who can DM it…")))
    people = dm_people(R)
    lines = [f"**People you picked:** {mention_list(people)}"]
    if R.pending == "who.dms_from":
        lines.append("Pick people below; nothing changes until you do.")
    else:
        lines.append("-# Picking someone switches Who can DM it to You and "
                     "people you pick.")
    allow = R.st.file_values.get((("dms",), "who.allow"))
    if isinstance(allow, list) and allow:
        lines.append(f"-# config.yaml also limits DMs to {len(allow)} "
                     f"{'person' if len(allow) == 1 else 'people'}; remove "
                     f"it there to use the list above alone.")
    fallback = not R.guild_id and not DM_USER_SELECT_SEARCHES
    if fallback:
        lines.append("-# To search by name, run /settings in a server you "
                     "share with them and press DMs.")
    inner.append(td("\n".join(lines)))
    if fallback:
        inner.append(row(btn("Add by ID", cid("mod", "d", "id.df", "b"),
                             PRIMARY)))
        if people:
            inner.append(row(sel(cid("rm", "d", "df"), [
                opt(R.person(p) or "Someone you added", p)
                for p in people[:25]], "Remove someone…", 1,
                min(25, len(people)))))
    else:
        inner.append(row(picker(USER_SELECT, cid("ppl", "d", "df"),
                                "Pick people who can DM it…", 0, 25,
                                people_defaults(people))))
    keys = ms.place_keys(R.st, place, "basics")
    if "privacy.memory" in keys:
        vm = Val(R, place, "privacy.memory")
        inner.append(td(f"**What it may say about you in DMs**{tag(R, vm)}"))
        inner.append(row(choice_select(R, place, "privacy.memory",
                                       cid("set", "d", "m"))))
    if "style.notes" in keys:
        inner.append(section(notes_text(R, place),
                             btn("Edit", cid("mod", "d", "i", "b"),
                                 PRIMARY)))
    if "limits.per_person_per_day" in keys:
        inner.append(cap_section(R, place, "limits.per_person_per_day",
                                 "c", "b"))
    for k in keys:
        if k.startswith("plugins.") and k.endswith(".enabled"):
            inner.append(toggle_section(R, place, k, "b"))
    footer = [row(btn("More settings", cid("pg", "d", "m")),
                  btn("Why no reply?", cid("pg", "d", "y"))),
              row(go_select(R, place))]
    return finish(R, place, inner, footer,
                  GREY if access == "nobody" else BLURPLE)


def page_botwide(R):
    """S7: status line and update notices."""
    place = ms.Place.defaults()
    st = R.st
    status = st.top("presence.status")
    kind = st.top("presence.kind")
    sv = Val(R, place, "presence.status")
    uv = Val(R, place, "updates.notify")
    text = (f"“{esc(clip(status, 128))}”" if status else "None") + \
        f"\n-# Shown as {str(kind).capitalize()}; the kind is set in " \
        f"config.yaml."
    on = bool(st.top("updates.notify"))
    inner = [td("## Bot-wide\n-# How the bot shows up on Discord, and what "
                "it tells you."), sep(),
             section(f"**Status line**{tag(R, sv)}\n{text}",
                     btn("Edit", cid("mod", "x", "st", "w"), PRIMARY)),
             section(f"**Update notices**{tag(R, uv)}\n"
                     + ("On: DMs you when a new version is out" if on
                        else "Off: no DM when a new version is out"),
                     btn("Turn off" if on else "Turn on",
                         cid("val", "x", "un", "off" if on else "on",
                             "w")))]
    return finish(R, place, inner,
                  [row(btn("Back to Defaults", cid("pg", "x", "b")))])


def page_servers(R, origin):
    """S15: Other servers (paged; Find a server past 3 pages)."""
    opts, page, pages, n = server_options(R, R.server_page, more="o")
    head = (f"## Other servers\n-# The bot is in {len(R.guilds()):,} "
            f"server{'s' if len(R.guilds()) != 1 else ''}"
            + (f" (page {page + 1} of {pages})." if pages > 1 else "."))
    if R.server_query:
        head += f"\n-# {n:,} matching “{esc(clip(R.server_query, 40))}”."
    inner = [td(head)]
    if opts:
        inner.append(row(sel(cid("go", ptok(origin)), opts,
                             "Pick a server…")))
    else:
        inner.append(td("No server matches."))
    footer = []
    if pages > FIND_AFTER_PAGES or R.server_query:
        footer.append(btn("Find a server",
                          cid("mod", ptok(origin), "fs", "o")))
    footer.append(btn("Back", cid("pg", ptok(origin), "b")))
    return [container(inner, BLURPLE), row(*footer)]


# ---------------------------------------------------------------------------
# Why no reply?
# ---------------------------------------------------------------------------
REFUSED = ("skipped", "quarantined", "enqueue_failed")


def msg_link(gid, channel_id, mid):
    return (f"https://discord.com/channels/{gid or '@me'}/{channel_id}/"
            f"{mid}")


def _setter(R, sc, key):
    """The Place whose layer decides key at scope sc (None: Defaults,
    a category or the built-in value: nothing the UI can fix there)."""
    _v, rows = R.st.trace(sc, key)
    for _name, path, s, _val, _src in reversed(rows):
        if not s:
            continue
        if path[0] in ("builtin", "builtin_all_servers", "alias"):
            return None, path
        return ms.place_of_path(path), path
    return None, None


def _outcome_scope(R, o):
    gid = o.get("guild_id")
    ch = str(o.get("parent_id") or o.get("channel_id") or "")
    if not gid:
        return ms.Scope.dm(), None
    if (R.chan(gid, ch) or {}).get("type") == CATEGORY:
        return ms.Scope.server(gid), None
    place = ms.Place.channel(gid, ch, R.category_of(gid, ch)) \
        if _ID.fullmatch(ch) else ms.Place.server(gid)
    return place.scope("who.replies"), place


def _may_fix(R, changes):
    """The viewer may make these changes (an admin only through the
    admin rule; a fix button is shown only then)."""
    if R.owner:
        return True
    if not R.admin:
        return False
    try:
        ms.admin_changes(R.st, R.admin_gid, changes, R.ctx)
        return True
    except ms.SettingsError:
        return False
    except Exception:
        return False


def is_category(R, place):
    return place is not None and place.kind == "channel" and (
        R.chan(place.guild_id, place.channel_id) or {}).get(
        "type") == CATEGORY


def setter_name(R, place):
    if place is None:
        return "Defaults"
    if is_category(R, place):
        return f"{R.look(place.channel_id) or 'its category'} (category)"
    return R.name(place)


def _fix_button(R, label, place, key, op, changes=None):
    if place is None or is_category(R, place):
        return None
    if changes is not None and not _may_fix(R, changes):
        return None
    try:
        return btn(label, cid("fx", ptok(place), kcode(key), op))
    except BadId:
        return None


def explain_outcome(R, o):
    """(copy, fix button or None, still holds?) for one refused message,
    re-checked against the settings in force now."""
    code = o.get("code") or "other"
    aid = str(o.get("author_id") or "")
    who_m = f"<@{aid}>" if _ID.fullmatch(aid) else (
        f"@{esc(o['author'])}" if o.get("author") else "They")
    short = R.person(aid) if aid else None
    short = short or (esc(o["author"]) if o.get("author") else "them")
    sc, place = _outcome_scope(R, o)
    where = R.name(place) if place else "DMs"
    detail = o.get("detail") if isinstance(o.get("detail"), dict) else {}
    if code in ("replies-off", "dm-off"):
        cur = R.st.get("who.replies", sc)
        if cur != "off":
            return (f"When it answers was Off in {where}.\n-# No longer "
                    f"applies: it's {vl(R, 'who.replies', cur)} now."), \
                None, False
        setter, path = _setter(R, sc, "who.replies")
        if setter is None or setter.kind == "defaults":
            return ("When it answers is Off by default in servers (Pause "
                    "everywhere)." if path and path[0] == "all_servers"
                    else f"When it answers is Off in {where}."), None, True
        s, _v, src = R.st.layer(path, "who.replies")
        by = f" (by {who_text(R, src[1])})" if src and src[0] == \
            "discord" else ""
        parent = parent_name(R, setter, "who.replies") or "Defaults"
        if setter.kind == "dms":
            parent = "Defaults"
        ch = [ms.Change(setter, "who.replies", "inherit")]
        return (f"When it answers is Off in {setter_name(R, setter)}{by}.",
                _fix_button(R, f"Same as {parent}", setter, "who.replies",
                            "=", ch), True)
    if code == "bots-ignore":
        cur = R.st.get("who.bots", sc)
        if cur != "ignore":
            return ("Other bots were ignored then.\n-# No longer applies: "
                    "it answers bots that address it now."), None, False
        setter, _p = _setter(R, sc, "who.bots")
        ch = [ms.Change(setter, "who.bots", "when-addressed")] \
            if setter else None
        return ("Other bots: Ignore other bots"
                + (f" (set in {setter_name(R, setter)})." if setter
                   else "."),
                _fix_button(R, "Answer bots that address it", setter,
                            "who.bots", "vwhen-addressed", ch)
                if setter else None, True)
    if code in ("not-allowed", "dm-not-allowed") and _ID.fullmatch(aid):
        ign = R.st.get("who.ignore", sc) or []
        if aid in ign:
            setter, path = _setter(R, sc, "who.ignore")
            added = ""
            if path:
                _s, _v, src = R.st.layer(path, "who.ignore")
                entry = R.st.overlay.get((path, "who.ignore"))
                if src and src[0] == "discord":
                    role = ", admin" if ms.entry_role(entry) == "admin" \
                        else ""
                    added = (f" (added by {who_text(R, src[1])}{role}, "
                             f"{short_date(src[2])})")
                elif src and src[0] == "file":
                    added = " (in config.yaml)"
            fix = None
            if setter is not None:
                ch = fix_changes(R, setter, "who.ignore", f"-{aid}")
                fix = _fix_button(R, f"Remove {short}", setter,
                                  "who.ignore", f"-{aid}", ch)
            return (f"{who_m} is on **Never answer** in "
                    f"{setter_name(R, setter)}{added}.",
                    fix, True)
        allow = R.st.get("who.allow", sc)
        if isinstance(allow, list) and aid not in allow \
                and aid not in R.ctx.owners:
            setter, _p = _setter(R, sc, "who.allow")
            fix = None
            if setter is not None:
                ch = [ms.Change(setter, "who.allow", list(allow) + [aid])]
                fix = _fix_button(R, f"Add {short}", setter, "who.allow",
                                  f"+{aid}", ch)
            return (f"Only answer these people is set in "
                    f"{setter_name(R, setter)} and "
                    f"{who_m} isn't on it.", fix, True)
        if code == "dm-not-allowed":
            return person_check_text(R, aid, short)[0], None, True
        return (f"{who_m} wasn't allowed then.\n-# No longer applies: "
                f"they can get answers now."), None, False
    if code in ("not-allowed", "dm-not-allowed"):
        return ("They weren't allowed by Only answer these people or Never "
                "answer."), None, True
    if code == "cap":
        key = detail.get("key")
        cp = counter_place(detail.get("counter"))
        n = detail.get("cap")
        if key not in ("limits.per_day", "limits.per_person_per_day") \
                or cp is None:
            return "It hit a daily reply cap.", None, True
        owner = "the Defaults" if cp.kind == "defaults" else \
            f"{R.name(cp)}'s"
        what = (f"{who_m} hit {owner} cap of {n:,} replies per person per "
                f"day." if key == "limits.per_person_per_day" else
                f"The bot hit {owner} cap of {n:,} replies per day (total).") \
            if isinstance(n, int) else f"It hit {owner} daily reply cap."
        fix = None
        if R.owner:
            fix = btn("Change cap", cid("mod", ptok(cp), "c", "b"))
        return what, fix, True
    if code == "bot-exchange":
        lim = detail.get("limit")
        setter, _p = _setter(R, sc, "who.bot_exchange_limit")
        fix = btn("Change limit", cid("mod", ptok(setter), "bx", "m")) \
            if setter is not None and R.owner else None
        return ("It had answered other bots "
                + (f"{lim} times" if isinstance(lim, int) else "too often")
                + f" in a row in {where} (Replies in a row to other bots)."
                ), fix, True
    if code == "suspicious-ignored":
        cur = R.st.get("privacy.suspicious", sc)
        if cur != "ignore":
            return ("It looked like prompt injection and was dropped.\n-# "
                    "No longer applies: such messages are held for your "
                    "review now."), None, False
        setter, _p = _setter(R, sc, "privacy.suspicious")
        ch = [ms.Change(setter, "privacy.suspicious", "hold-for-review")] \
            if setter else None
        return ("It looked like prompt injection, and Possible prompt "
                "injection is Drop it.",
                _fix_button(R, "Hold for my review instead", setter,
                            "privacy.suspicious", "vhold-for-review", ch)
                if setter else None, True)
    if R.admin and code in ("quarantined", "never-share"):
        return ("Held for the owners' review." if code == "quarantined"
                else "The reply was held for the owners."), None, True
    text = {
        "quarantined": "Held for your review; look for the bot's DM.",
        "predates-join": "It was sent before the bot joined.",
        "no-send": f"The bot can't send in {where}; fix it in Discord's "
                   f"channel permissions.",
        "internal": "The bot hit an internal error; see monitor.log.",
        "worker-skipped": "The Muse read it and chose not to answer.",
        "send-failed": "Sending the reply failed; see monitor.log.",
        "never-share": "The reply was held because it contained a phrase "
                       "you never share.",
    }.get(code, "It wasn't answered.")
    return text, None, True


def page_why(R, place):
    """S8a: the last refused messages that addressed the bot here."""
    if place.kind == "dms":
        return page_why_dms(R)
    tok = ptok(place)
    where = R.name(place)
    inner = [td(f"## Why no reply? · {where}\n-# The last {WHY_ENTRIES} "
                f"messages here that addressed the bot and got no answer, "
                f"newest first. Ordinary chatter isn't recorded."), sep()]
    try:
        got = R.ctx.recent_outcomes(place, 50) or []
    except Exception as e:
        R.ctx.log(f"settings_ui: recent_outcomes: {e}")
        got = []
    shown = 0
    # Four with a banner on top, so the page stays within 21 components.
    most = WHY_ENTRIES - (1 if R.banner else 0)
    for o in got:
        if not isinstance(o, dict) or o.get("status") not in REFUSED:
            continue
        if shown >= most:
            break
        if (o.get("guild_id") or place.guild_id) != place.guild_id:
            continue
        o = dict(o, guild_id=place.guild_id)
        shown += 1
        aid = str(o.get("author_id") or "")
        who_m = f"<@{aid}>" if _ID.fullmatch(aid) else (
            f"@{esc(o['author'])}" if o.get("author") else "Someone")
        mid = str(o.get("message_id") or "")
        link = msg_link(o.get("guild_id"), o.get("channel_id"), mid)
        when = ago(R, o.get("at") or snowflake_time(mid))
        text, fix, holds = explain_outcome(R, o)
        if not aid:
            fix = None   # an older entry: no fix
        head = f"**{who_m}** · [message]({link}) · {when}\n"
        body = head + text
        if R.fixed and R.fixed == aid and not holds:
            body += "\n" + FIXED_NOTE.format(who=who_m)
        inner.append(section(body, fix) if fix else td(body))
    if not shown:
        if place.kind == "channel":
            tail = (f"check that the bot can see and send messages in "
                    f"{where}.")
        else:
            tail = "check that the bot can see and send messages there."
        inner.append(td(f"Nothing was refused here recently. If someone "
                        f"@mentioned the bot and got no answer, {tail}"))
    back = cid("pg", tok, "q" if R.admin else "b")
    return finish(R, place, inner,
                  [row(btn(f"Back to {where}", back),
                       btn("Refresh", cid("pg", tok, "y")))], slot=False)


def person_check_text(R, uid, short):
    """(copy, changes that would let them DM it or None)."""
    st = R.st
    access = ms.dm_access(st)
    who_m = f"<@{uid}>"
    if uid in R.ctx.owners:
        if access == "nobody":
            return (f"{who_m} is an owner, but Who can DM it is Nobody, so "
                    f"no DM gets an answer."), None
        return f"{who_m} is an owner and can always DM it.", None
    people = dm_people(R)
    let = ms.dms_from_changes(st, "list", people + [uid])
    if access == "nobody":
        return (f"{short} can't DM it: Who can DM it is Nobody."), let
    if access == "owners":
        return (f"{short} can't DM it: Who can DM it is "
                f"{value_name(R, 'who.dms_from', 'owners')}."), let
    if access == "list" and uid not in people:
        return (f"{short} can't DM it: they aren't on your list of people "
                f"who can."), let
    r = st.resolve(ms.Scope.dm())
    ign = r["who.ignore"] or []
    if uid in ign:
        return f"{short} can't DM it: they're on Never answer in DMs.", None
    allow = r["who.allow"]
    if isinstance(allow, list) and uid not in allow:
        return (f"{short} can't DM it: Only answer these people is set for "
                f"DMs and they aren't on it."), None
    if access == "shared-servers":
        return (f"{short} can DM it if they share a server with the "
                f"bot."), None
    return f"{short} can DM it.", None


def page_why_dms(R):
    """S8a, DMs: strangers' DMs aren't recorded, so check a person."""
    place = ms.Place.dms()
    inner = [td("## Why no reply? · DMs\n-# DMs from people who aren't "
                "allowed aren't recorded. Check a person instead."), sep()]
    if R.guild_id or DM_USER_SELECT_SEARCHES:
        inner.append(row(picker(USER_SELECT, cid("wy", "d"),
                                "Check a person…", 1, 1)))
    else:
        inner.append(row(btn("Check by ID", cid("mod", "d", "id.wy", "y"),
                             PRIMARY)))
    if R.check:
        uid = R.check
        short = R.person(uid) or "They"
        text, let = person_check_text(R, uid, short)
        b = None
        if let and R.owner:
            try:
                b = btn(f"Let {R.person(uid) or 'them'} DM it",
                        cid("fx", "d", "df", f"+{uid}"), PRIMARY)
            except BadId:
                b = None
        inner.append(section(text, b) if b else td(text))
    return finish(R, place, inner,
                  [row(btn("Back to DMs", cid("pg", "d", "b")),
                       btn("Refresh", cid("pg", "d", "y")))], slot=False)


def why_card(R):
    """S8b: the message context command's card for one message."""
    data = R.data
    tid = str(data.get("target_id") or "")
    msg = ((data.get("resolved") or {}).get("messages") or {}).get(tid) \
        or {}
    gid = R.guild_id
    if not (R.owner or (R.admin and gid == R.admin_gid)):
        return notice(REFUSAL_WHY)
    ch = str(msg.get("channel_id") or R.ix.get("channel_id") or "")
    here, _thread = R.typed()
    author = msg.get("author") or {}
    aid = str(author.get("id") or "")
    chan = None
    if gid:
        chan = here or ch
        place = ms.Place.channel(gid, chan, R.category_of(gid, chan))
    else:
        place = ms.Place.dms()
    where = R.name(place)
    when = ago(R, snowflake_time(tid))
    link = msg_link(gid, ch, tid)
    who_m = f"<@{aid}>" if aid else "Someone"
    inner = [td(f"## Why no reply?\n-# {who_m}'s [message]({link}) in "
                f"{where}, {when}"), sep()]
    o = None
    fn = getattr(R.ctx, "outcome", None)
    try:
        if callable(fn):
            o = fn(tid)
    except Exception as e:
        R.ctx.log(f"settings_ui: outcome: {e}")
    if isinstance(o, dict):
        # Where the message is comes from the interaction, never from the
        # ledger: an entry for another server (a forged or stale id), or a
        # server message seen from a DM, counts as not recorded.
        if (o.get("guild_id") or None) not in (None, gid) \
                or (not gid and o.get("guild_id")):
            o = None
        elif gid:
            o = dict(o, guild_id=gid, channel_id=ch, parent_id=chan)
        else:
            o = dict(o, guild_id=None, parent_id=None)
    tail = None
    if aid and aid == str(R.ctx.bot_id):
        inner.append(td("That's the bot's own message."))
    elif o and o.get("status") == "replied":
        inner.append(td("It replied."))
    elif o and o.get("status") == "queued":
        inner.append(td(f"Still working on it (queued "
                        f"{ago(R, o.get('at')).replace(' ago', '')} ago)."))
    elif o and o.get("status") in REFUSED:
        o = dict(o, author_id=aid or o.get("author_id"))
        text, fix, holds = explain_outcome(R, o)
        inner.append(section(text, fix) if fix else td(text))
        if fix:
            short = R.person(aid) or "them"
            tail = (f"-# Removing {short} doesn't answer this message; ask "
                    f"{short} to send it again."
                    if fix.get("label", "").startswith("Remove") else
                    f"-# Fixing this doesn't answer this message; ask "
                    f"{short} to send it again.")
    else:
        inner.append(td(not_recorded(R, msg, place)))
    if tail:
        inner.append(td(tail))
    if place.kind == "dms":
        open_id = cid("pg", "d", "b")
        label = "Open DMs settings"
    else:
        open_id = cid("pg", ptok(place), "q" if R.admin else "b")
        label = f"Open {where} settings"
    return [container(inner, BLURPLE), row(btn(label, open_id))]


def not_recorded(R, msg, place):
    bot = str(R.ctx.bot_id)
    mentions = [str((m or {}).get("id")) for m in msg.get("mentions") or []]
    ref = (msg.get("referenced_message") or {}).get("author") or {}
    addressed = bot in mentions or str(ref.get("id") or "") == bot
    if place.kind == "dms":
        return ("Not recorded: DMs from people who may not DM it aren't "
                "kept. Use Why no reply? on the DMs page to check them.")
    where = R.name(place)
    if addressed:
        return ("Not recorded: the bot has no record of it, maybe because "
                "it's older than what the bot keeps.")
    rep = value(R, place, "who.replies")
    how = {"off": f"When it answers is Off in {where}",
           "when-addressed": f"in {where} it answers only when @mentioned",
           "conversational": f"in {where} it answers @mentions, replies and "
                             f"follow-ups within 2 minutes of its reply"
           }.get(rep, "")
    return (f"Not recorded: it didn't @mention the bot or reply to it, and "
            f"{how}.")


# ---------------------------------------------------------------------------
# Server admins: "Quiet the bot"
# ---------------------------------------------------------------------------
QUIETER = ("when-addressed", "off")


def quieter_values(cur):
    r = ("off", "when-addressed", "conversational")
    if cur not in r:
        return []
    return [v for v in QUIETER if r.index(v) < r.index(cur)]


def page_quiet(R, gid):
    """S12: the admin panel for one server (here = where it was typed)."""
    st = R.st
    server = ms.Place.server(gid)
    gname = R.gname(gid)
    here, _thread = R.typed() if R.in_guild(gid) else (None, False)
    if here and (R.chan(gid, here) or {}).get("type") == CATEGORY:
        here = None
    inner = [td(f"## Quiet the bot in {gname}\n-# You're a server admin "
                f"here, so you can make the bot quieter and undo what "
                f"admins changed. The bot's owners get a summary of every "
                f"change."), sep()]
    if here:
        hp = ms.Place.channel(gid, here, R.category_of(gid, here))
        v = Val(R, hp, "who.replies")
        src = "set here" if v.set else \
            f"same as {parent_name(R, hp, 'who.replies')}"
        cname = R.name(hp)
        inner.append(td(f"**In {cname}** · now "
                        f"{vl(R, 'who.replies', v.value)} ({src})"))
        bs = [btn(f"{value_name(R, 'who.replies', x)} in {cname}",
                  cid("val", ptok(hp), "r", x, "q"),
                  DANGER if x == "off" else SECONDARY)
              for x in quieter_values(v.value)]
        if bs:
            inner.append(row(*bs))
    cur = value(R, server, "who.replies")
    inner.append(td(f"**In all of {gname}** · now "
                    f"{vl(R, 'who.replies', cur)}"))
    bs = [btn(f"{value_name(R, 'who.replies', x)} in all of {gname}",
              cid("val", f"s{gid}", "r", x, "q"),
              DANGER if x == "off" else SECONDARY)
          for x in quieter_values(cur)]
    if bs:
        inner.append(row(*bs))
    inner.append(sep())
    ign = value(R, server, "who.ignore") or []
    owners_list = ms.owners_view(st, gid).get("who.ignore",
                                              ms.Scope.server(gid)) or []
    entry = st.overlay.get((("servers", gid), "who.ignore"))
    by = (entry or {}).get("by") if ms.entry_role(entry) == "admin" \
        else None
    _pin, adds = admin_ignore(R, gid)
    added = [p for p in adds if p not in owners_list]
    parts = []
    for p in ign[:10]:
        m = f"<@&{p[5:]}>" if str(p).startswith("role:") else f"<@{p}>"
        if p not in added:
            parts.append(f"{m} (set by the owners)")
        else:
            parts.append(f"{m} (added by {who_text(R, by)})" if by
                         else f"{m} (added by an admin)")
    if len(ign) > 10:
        parts.append(f"and {len(ign) - 10} more")
    inner.append(td(f"**Never answer** in {gname}\n"
                    + (", ".join(parts) if parts else "Nobody yet.")))
    inner.append(row(picker(MENTIONABLE_SELECT, cid("ppl", f"s{gid}", "ig"),
                            "Add to Never answer…", 1, 25)))
    if added:
        inner.append(row(sel(cid("rm", f"s{gid}", "ig"), [
            opt((R.person(p[5:]) if str(p).startswith("role:")
                 else R.person(p)) or "Someone an admin added", p)
            for p in added[:25]], "Remove someone admins added…", 1,
            min(25, len(added)))))
    offs = []
    for key, label in (("plugins.welcome.enabled", "Turn off welcome"),
                       ("plugins.media.enabled",
                        "Turn off image and video")):
        if st.spec(key) is not None and value(R, server, key):
            offs.append(btn(label, cid("val", f"s{gid}", kcode(key), "off",
                                       "q"), DANGER))
    if offs:
        inner.append(row(*offs))
    inner.append(sep())
    inner.append(td("**Changes admins made here**"))
    changes = []
    owners = ms.owners_view(st, gid)
    for (path, key), e in st.overlay.items():
        if path[0] != "servers" or path[1] != gid \
                or ms.entry_role(e) != "admin" or kcode(key) is None:
            continue
        pl = ms.place_of_path(path)
        sc = ms.Scope.server(gid, channel_id=pl.channel_id,
                             category_id=R.category_of(gid, pl.channel_id)
                             if pl.channel_id else None)
        if e.get("_norm", e.get("value")) == owners.get(key, sc):
            continue    # back at the owners' value (an undone change)
        changes.append((str(e.get("at") or ""), path, key, e))
    changes.sort(key=lambda c: c[0], reverse=True)
    for _at, path, key, e in changes[:ADMIN_CHANGES_SHOWN]:
        pl = ms.place_of_path(path)
        if pl.kind == "channel":
            pl = ms.Place.channel(gid, pl.channel_id,
                                  R.category_of(gid, pl.channel_id))
        sc = pl.scope(key)
        where = R.name(pl) if pl.kind == "channel" else f"All of {gname}"
        new = e.get("_norm", e.get("value"))
        what = f"{ms.label(key)} → {vl(R, key, new)}"
        if key == "who.ignore":
            what = (f"{ms.label(key)}: added "
                    f"{mention_list(admin_ignore(R, gid)[1], 5)}")
        line = (f"{where}: {what} · "
                f"{who_text(R, e.get('by'))}, {ago(R, e.get('at'))}\n-# Undo "
                f"goes back to the owners' setting: "
                f"{vl(R, key, owners.get(key, sc))}")
        try:
            b = btn("Undo", cid("val", ptok(pl), kcode(key), "=", "q"))
        except BadId:
            b = None
        inner.append(section(line, b) if b else td(line))
    if not changes:
        inner.append(td("-# No changes by admins are in effect here."))
    elif len(changes) > ADMIN_CHANGES_SHOWN:
        inner.append(td(f"-# and {len(changes) - ADMIN_CHANGES_SHOWN} more; "
                        f"the owners can see them all."))
    tok = f"c{gid}.{here}" if here else f"s{gid}"
    comps = list(R.banner or []) + [container(inner, AMBER),
                                    row(btn("Why no reply?",
                                            cid("pg", tok, "y")))]
    if st.overlay_error:
        comps = read_only(comps)
    return comps


# ---------------------------------------------------------------------------
# Confirm card and preset preview
# ---------------------------------------------------------------------------
def page_confirm(R, origin, lines, warnings, tok, undo=False):
    """S9: a risky outcome; Apply or Cancel (back to the origin page)."""
    place, page = origin
    head = "## Check before undoing" if undo else "## Check before applying"
    body = "\n".join([head] + [clip(x, 300) for x in lines[:8]])
    inner = [td(body), td("**What this means**\n" + "\n".join(
        f"- {clip(w, 400)}" for w in warnings[:6]))]
    return [container(inner, RED),
            row(btn("Undo" if undo else "Apply",
                    cid("ok", ptok(place), tok), DANGER),
                btn("Cancel", cid("pg", ptok(place), page)))]


PRESET_SKIP_WORDS = {
    "plugins.media.per_day": "{label} (image and video is off here)",
    "style.notes": "{label} ({where} has its own)",
}


def page_preset(R, place, name):
    """S10: a preset's preview, which is also its confirmation."""
    title, values = ms.PRESETS[name]
    gid = place.guild_id
    gname = R.gname(gid)
    members = R.members(gid)
    big = title == "Friends" and members is not None \
        and members > ms.BIG_FOR_MEMORY
    try:
        changes = ms.preset_changes(R.st, name, place, R.ctx, big="skip")
        planned = ms.plan_changes(R.st, changes)
    except ms.SettingsError as e:
        R.banner = banner_error(str(e))
        return page_server(R, place)
    if not planned:
        text = f"## {title} preset\n{gname} already matches {title}."
        if big:
            cur = vl(R, "privacy.memory", R.st.get(
                "privacy.memory", ms.Scope.server(gid)))
            text = (f"## {title} preset\n{gname} already matches {title}, "
                    f"except What it may say about you: it stays {cur}, "
                    f"because {gname} has more than {ms.BIG_FOR_MEMORY} "
                    f"members. Change it yourself if you're sure.")
        inner = [td(text)]
        return [container(inner, BLURPLE),
                row(btn("Back", cid("pg", ptok(place), "b")))]
    lines = []
    for p in planned:
        lines.append(f"- {ms.label(p.key)}: {vl(R, p.key, p.old)} → "
                     f"{vl(R, p.key, p.new)}")
    sc = ms.Scope.server(gid)
    changed = {p.key for p in planned}
    already, skipped = [], []
    for key, v in values.items():
        if key in changed or R.st.spec(key) is None:
            continue
        if key == "privacy.memory" and big:
            continue
        if isinstance(v, tuple):
            cond, _val = v
            if cond == "if-media-on" and not R.st.get(
                    "plugins.media.enabled", sc):
                skipped.append(PRESET_SKIP_WORDS[key].format(
                    label=ms.label(key), where=gname))
                continue
            if cond == "if-empty" and R.st.get(key, sc):
                skipped.append(PRESET_SKIP_WORDS[key].format(
                    label=ms.label(key), where=gname))
                continue
        already.append(ms.label(key))
    notes = []
    if already:
        notes.append("Already matches: " + ", ".join(already) + ".")
    if skipped:
        notes.append("Skipped: " + ", ".join(skipped) + ".")
    tail = "-# " + " ".join(notes) if notes else ""
    if big:
        cur = vl(R, "privacy.memory", R.st.get("privacy.memory", sc))
        tail += ("\n" if tail else "") + (
            f"Not included: What it may say about you stays {cur}, because "
            f"{gname} has more than {ms.BIG_FOR_MEMORY} members. Change it "
            f"yourself if you're sure.")
    try:
        a = ms.assess(R.st, changes, R.ctx, R.look)
    except ms.SettingsError as e:
        R.banner = banner_error(str(e))
        return page_server(R, place)
    inner = [td(f"## Apply the {title} preset to {gname}?\n-# One change "
                f"you can undo in one click. Channels keep their own "
                f"settings."), sep(),
             td("**Changes**\n" + "\n".join(lines))]
    if tail:
        inner.append(td(tail))
    if a.risky:
        inner.append(td("**What this means**\n" + "\n".join(
            f"- {clip(w, 400)}" for w in a.warnings[:6])))
    tok = R.ctx.stash_put(R.uid, {
        "kind": "apply", "changes": [c.to_dict() for c in changes],
        "origin": [ptok(place), "b"], "preset": title}, STASH_TTL)
    return [container(inner, RED if a.risky else BLURPLE),
            row(btn(f"Apply {title}", cid("ok", ptok(place), tok),
                    DANGER if a.risky else PRIMARY),
                btn("Cancel", cid("pg", ptok(place), "b")))]


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------
CAP_FIELDS = {"pp": "limits.per_person_per_day", "pd": "limits.per_day",
              "bx": "who.bot_exchange_limit", "mpd": "plugins.media.per_day"}
CAP_WORDS = {"limits.per_person_per_day": "Replies per person per day",
             "limits.per_day": "Replies per day",
             "who.bot_exchange_limit": "Replies in a row to other bots",
             "plugins.media.per_day": "Generations per day"}
NO_CAP = ("", "none", "no cap", "nocap", "unlimited", "-", "no limit",
          "nolimit")
FORM_KEYS = {"i": ("style.notes",), "c": ("limits.per_person_per_day",
                                          "limits.per_day"),
             "bx": ("who.bot_exchange_limit",),
             "mpd": ("plugins.media.per_day",),
             "wn": ("plugins.welcome.notes",), "st": ("presence.status",)}


def form_ok(R, place, form):
    """The modal form is offered at this place."""
    if form in ("fc",):
        return place.kind in ("server", "channel")
    if form == "fs":
        return True
    if form == "id.df":
        return place.kind == "dms"
    if form == "id.wy":
        return place.kind == "dms"
    keys = FORM_KEYS.get(form)
    if not keys:
        return False
    return all(offered(R, place, k) for k in keys) if form != "c" else \
        any(offered(R, place, k) for k in keys)


def draft_value(draft, name, default):
    if draft is None:
        return default
    v = (draft.get("values") or {}).get(name)
    return default if v is None else v


def open_modal(R, place, form, page, draft=None):
    tok = ptok(place)
    where = R.name(place)
    sub = cid("sub", tok, form, page)
    st = R.st
    if form == "i":
        v = Val(R, place, "style.notes")
        if place.kind == "channel":
            gname = R.gname(place.guild_id)
            comps = [td(f"Added after {gname}'s instructions in {where}. "
                        f"Blank = nothing extra."),
                     labelled("Instructions for the Muse", text_input(
                         "n", 2, ms.NOTES_MAX,
                         draft_value(draft, "n", v.here if v.set else ""),
                         "Example: Keep it friendly; no swearing."),
                         f"Up to {ms.NOTES_MAX} characters. Blank = nothing "
                         f"extra.")]
            return modal(sub, f"Instructions · {where}", comps)
        blank = "no instructions" if place.kind == "defaults" \
            else "same as Defaults"
        ph = (f"Same as Defaults: “{clip(v.inherited, 70)}”"
              if place.kind != "defaults" and v.inherited
              else "Example: Keep it friendly; no swearing.")
        intro = (f"The Muse reads these with every reply in {where}. They "
                 f"replace the Defaults instructions here; channel "
                 f"instructions are added after them."
                 if place.kind == "server" else
                 f"The Muse reads these with every reply in {where}."
                 + (" They replace the Defaults instructions here."
                    if place.kind == "dms" else
                    " A server or DMs can replace them; channel "
                    "instructions are added after them."))
        lv = Val(R, place, "style.length")
        cur_len = draft_value(draft, "ln", None)
        cur_len = cur_len[0] if isinstance(cur_len, list) and cur_len \
            else (lv.here if lv.set else ("=" if place.kind != "defaults"
                                          else lv.value))
        opts = []
        if place.kind != "defaults":
            opts.append(opt(f"Same as Defaults: "
                            f"{value_name(R, 'style.length', lv.inherited)}",
                            "=", "Follows Defaults", cur_len == "="))
        for x in ("brief", "normal", "detailed"):
            d = OPTION_HELP[("style.length", x)]
            if place.kind == "defaults" and x == "normal":
                d = "Built-in default · " + d
            opts.append(opt(value_name(R, "style.length", x), x, d,
                            cur_len == x))
        length = {"type": STRING_SELECT, "custom_id": "ln", "options": opts,
                  "min_values": 1, "max_values": 1, "required": True,
                  "placeholder": "Reply length…"}
        comps = [td(intro),
                 labelled("Instructions for the Muse", text_input(
                     "n", 2, ms.NOTES_MAX,
                     draft_value(draft, "n", v.here if v.set else ""), ph),
                     f"Up to {ms.NOTES_MAX} characters. Blank = {blank}."),
                 labelled("Reply length", length,
                          "How long replies tend to be.")]
        return modal(sub, f"Instructions · {where}", comps)
    if form == "c":
        comps = []
        intro = ("Leave a box blank for no cap here. Owners are never "
                 "capped. 0 means only owners get replies.")
        for name, key in (("pp", "limits.per_person_per_day"),
                          ("pd", "limits.per_day")):
            if not offered(R, place, key):
                continue
            v = Val(R, place, key)
            sc = _scope_at(place, key, v.path)
            others = [(c, n) for c, n in st.cap_layers(key, sc)
                      if not _counter_is(c, place)]
            if others and name == "pp":
                c, n = min(others, key=lambda x: x[1])
                intro += (f"\n-# {_counter_name(R, c)} also caps each "
                          f"person at {n:,} a day; that still applies.")
            counted = ("across every server and DM together"
                       if place.kind == "defaults" else f"in {where}")
            desc = (f"Per person, counted {counted}. Blank = no cap."
                    if name == "pp" else
                    f"All replies {counted} except the owners'. Blank = no "
                    f"cap.")
            cur = str(v.here) if v.set and v.here is not None else ""
            comps.append(labelled(CAP_WORDS[key] if name == "pp"
                                  else "Replies per day (total)",
                                  text_input(name, 1, 7,
                                             draft_value(draft, name, cur),
                                             "No cap"), desc))
        return modal(sub, f"Reply caps · {where}", [td(intro)] + comps)
    if form in ("bx", "mpd"):
        key = CAP_FIELDS[form]
        v = Val(R, place, key)
        cur = str(v.here) if v.set and v.here is not None else ""
        spec = v.spec
        desc = (f"{spec.min:,} to {spec.max:,}. Blank = no cap"
                + ("" if place.kind == "defaults" else " here") + ".")
        title = ("Bot replies in a row" if form == "bx"
                 else "Generations per day")
        intro = ("How many times in a row it answers other bots in one "
                 "channel without a human." if form == "bx" else
                 "Replies with generated images or video it sends a day, "
                 "counted where this is set.")
        return modal(sub, f"{title} · {where}", [
            td(intro),
            labelled(CAP_WORDS[key], text_input(
                "v", 1, 7, draft_value(draft, "v", cur), "No cap"), desc)])
    if form == "wn":
        v = Val(R, place, "plugins.welcome.notes")
        blank = "nothing extra" if place.kind == "defaults" \
            else "same as Defaults"
        return modal(sub, f"Welcome notes · {where}", [
            td("What to mention to newcomers; added to the welcome prompt."),
            labelled("Welcome notes", text_input(
                "v", 2, 500, draft_value(draft, "v", v.here if v.set
                                         else ""),
                "Example: Point them to #rules and #introductions."),
                f"Up to 500 characters. Blank = {blank}.")])
    if form == "st":
        cur = st.top("presence.status") or ""
        return modal(sub, "Status line", [
            labelled("Status line", text_input(
                "v", 1, 128, draft_value(draft, "v", cur),
                "Example: Helping out in #general"),
                "Up to 128 characters. Blank = no status line.")])
    if form in ("id.df", "id.wy"):
        title = "Add people by ID" if form == "id.df" else "Check a person"
        return modal(sub, title, [
            td("In Discord: Settings › Advanced › Developer Mode, then "
               "right-click a person › Copy User ID."),
            labelled("User IDs" if form == "id.df" else "User ID",
                     text_input("ids", 2 if form == "id.df" else 1, 600,
                                draft_value(draft, "ids", "")),
                     "One or more, separated by spaces or commas. "
                     "@mentions work too." if form == "id.df" else
                     "One ID or @mention.")])
    if form in ("fc", "fs"):
        what = "channel" if form == "fc" else "server"
        return modal(sub, f"Find a {what}", [
            labelled(f"{what.capitalize()} name", text_input(
                "q", 1, 100, draft_value(draft, "q", ""), None, True),
                "Part of the name is enough.")])
    return None


def modal_fields(data):
    """{custom_id: text or [values]} from a modal submit, in the Label
    shape or the older action-row shape."""
    out = {}

    def walk(c):
        if not isinstance(c, dict):
            return
        if c.get("type") == LABEL:
            walk(c.get("component"))
            return
        if c.get("type") == ROW:
            for x in c.get("components") or []:
                walk(x)
            return
        if c.get("custom_id"):
            if "values" in c:
                out[str(c["custom_id"])] = [str(v) for v in
                                            c.get("values") or []]
            else:
                out[str(c["custom_id"])] = str(c.get("value") or "")
    for c in data.get("components") or []:
        walk(c)
    return out


def parse_cap(R, key, text):
    """(value to write | "inherit", error text or None)."""
    spec = R.st.spec(key)
    raw = str(text or "").strip()
    t = re.sub(r"[\s,_]", "", raw.lower())
    if raw.lower() in NO_CAP or t in NO_CAP:
        return "inherit", None
    rng = f"{spec.min:,} to {spec.max:,}, or blank for no cap"
    if re.fullmatch(r"-\d+", t):
        return None, (f"“{clip(raw, 30)}” can't be negative. "
                      f"{CAP_WORDS[key]} takes {rng}.")
    if not re.fullmatch(r"\d+", t):
        return None, (f"“{clip(raw, 30)}” isn't a whole number. "
                      f"{CAP_WORDS[key]} takes {rng}.")
    n = int(t)
    if n < spec.min or n > spec.max:
        return None, (f"{n:,} is out of range. {CAP_WORDS[key]} takes "
                      f"{rng}.")
    return n, None


def parse_ids(text):
    return list(dict.fromkeys(re.findall(r"\d{15,25}", str(text or ""))))


# ---------------------------------------------------------------------------
# Rendering a place's page
# ---------------------------------------------------------------------------
def page_of(place, key):
    """The page a key's control lives on."""
    if key in ms.BOT_WIDE_KEYS or key in ms.TOP:
        return "w"
    if key.startswith("plugins.") and not key.endswith(".enabled"):
        return "e"
    if key in ms.MORE_KEYS and place.kind != "channel":
        return "m"
    return "b"


def render(R, place, page):
    """The components of one page (owners; server admins get the panel,
    or Why)."""
    if R.admin:
        if page == "y":
            return page_why(R, place)
        return page_quiet(R, R.admin_gid)
    k = place.kind
    if page == "o":
        return page_servers(R, place)
    if page == "y" and k in ("channel", "server", "dms"):
        return page_why(R, place)
    if page == "m" and k in ("server", "defaults", "dms"):
        return page_more(R, place)
    if page == "e" and k in ("server", "defaults"):
        return page_extras(R, place)
    if page == "w" and k == "defaults":
        return page_botwide(R)
    if k == "channel":
        return page_channel(R, place)
    if k == "server":
        return page_server(R, place)
    if k == "dms":
        return page_dms(R)
    return page_defaults(R)


def show(R, place, page):
    return message(R, render(R, place, page))


# ---------------------------------------------------------------------------
# Writes: every change goes through do_changes (or do_undo), which re-checks
# who may do it, runs assess (owners: a risky outcome shows a confirm card
# first) and writes one undo group with muse_settings.apply.
# ---------------------------------------------------------------------------
def plain_error(e):
    text = str(e).strip()
    return text[0].upper() + text[1:] if text else "That couldn't be saved."


def written_place(res, fallback):
    """The place whose Undo sees the group just written."""
    places = {p.change.place for p in res.written}
    if len(places) == 1:
        return next(iter(places))
    if all(p.kind in ("defaults", "dms") for p in places):
        return ms.Place.defaults()
    servers = {p.guild_id for p in places}
    if len(servers) == 1 and None not in servers:
        return ms.Place.server(next(iter(servers)))
    return fallback


def done_notes(R, res):
    """Extra words for the Done banner."""
    out = []
    for p in res.written:
        place, key = p.change.place, p.key
        if key == "plugins.welcome.enabled" and p.new is True \
                and place.kind == "server":
            w = welcome_where(R, place)
            out.append(w if w.startswith("⚠") else
                       f"Welcomes go to: {w.splitlines()[0]}.")
        if key in ("limits.per_day", "limits.per_person_per_day") \
                and p.new == 0 and place.kind != "defaults":
            out.append(f"0 means only the owners get replies in "
                       f"{R.name(place)}.")
        if key == "who.replies" and p.new == "off" \
                and place.kind == "server":
            loud = []
            for c in R.st.channels_listed(place.guild_id):
                s, v, _src = R.st.layer(("servers", place.guild_id,
                                         "channels", c), "who.replies")
                if s and v != "off":
                    loud.append("#" + (R.look(c) or "a channel"))
            if loud:
                one = len(loud) == 1
                out.append(f"-# {', '.join(loud[:5])} still answer"
                           f"{'s' if one else ''}; "
                           f"{'it has its' if one else 'they have their'} "
                           f"own setting.")
    return out


def do_changes(R, origin, changes, confirmed=False, named=(), notes=()):
    """Apply changes (one group) and show the origin page with a banner,
    or a confirm card, or the error in the banner."""
    place, page = origin
    if changes and not confirmed and R.owner:
        skipped = [c for c in changes if ui_noop(R, c)]
        if len(skipped) == len(changes):
            R.banner = [td(clip("**No change:** " + "; ".join(
                no_change_words(R, c) for c in skipped), 800))]
            return show(R, place, page)
        changes = [c for c in changes if c not in skipped]
    if not changes:
        R.banner = [td("**No change.**")]
        return show(R, place, page)
    if R.st.overlay_error:
        R.banner = banner_error(OVERLAY_BROKEN.lstrip("⚠ "))
        return show(R, place, page)
    if R.owner:
        try:
            a = ms.assess(R.st, changes, R.ctx, R.look, named=named)
        except ms.SettingsError as e:
            R.banner = banner_error(plain_error(e))
            return show(R, place, page)
        if a.risky and not confirmed:
            try:
                planned = ms.plan_changes(R.st, changes)
            except ms.SettingsError as e:
                R.banner = banner_error(plain_error(e))
                return show(R, place, page)
            tok = R.ctx.stash_put(R.uid, {
                "kind": "apply",
                "changes": [dict(c.to_dict(), revert=c.revert)
                            for c in changes],
                "named": list(named), "notes": list(notes),
                "origin": [ptok(place), page]}, STASH_TTL)
            return message(R, page_confirm(
                R, origin, [p.describe(R.look) for p in planned
                            if not p.noop] or ["No change"], a.warnings,
                tok))
    try:
        if R.admin:
            res = R.ctx.apply(changes, R.uid, "admin",
                              admin_gid=R.admin_gid)
        elif R.owner:
            res = R.ctx.apply(changes, R.uid, "owner")
        else:
            return notice(REFUSAL_SERVER)
    except ms.SettingsError as e:
        if R.admin:
            return notice(ADMIN_ONLY_QUIETER)
        R.banner = banner_error(plain_error(e))
        return show(R, place, page)
    R.reload()
    if not res.changed:
        R.banner = [td(clip("**No change:** " + "; ".join(
            no_change_text(R, p) for p in res.unchanged), 800))]
        return show(R, place, page)
    if R.admin:
        for p in res.written:
            try:
                R.ctx.notify_owners_admin_change(
                    R.admin_gid, R.uid, p.keystr, p.old, p.new)
            except Exception as e:
                R.ctx.log(f"settings_ui: admin notice: {e}")
    text = "; ".join(res.describe(R.look))
    extra = list(notes) + done_notes(R, res)
    if extra:
        text += "\n" + "\n".join(extra)
    up = ms.Place.server(R.admin_gid) if R.admin \
        else written_place(res, place)
    R.banner = banner_done(R, text, up, res.group)
    return show(R, place, page)


def ui_noop(R, change):
    """A change that would change nothing as this page sees it: the
    place sets nothing and the value equals what it follows (the engine
    cannot tell a channel with no category from an unknown one, so it
    writes such a value; the page knows)."""
    if change.path is not None or change.revert:
        return False
    try:
        v = Val(R, change.place, change.key)
    except ms.SettingsError:
        return False
    if v.set:
        if change.value == "inherit":
            return False
        ok, norm = ms.normalize(v.spec, change.value)
        return ok and norm == v.here and ms.entry_role(
            R.st.overlay.get((v.path, change.key))) != "admin"
    if change.value == "inherit":
        return True
    ok, norm = ms.normalize(v.spec, change.value)
    if not ok:
        return False
    if isinstance(norm, list) and isinstance(v.inherited, list):
        return sorted(norm) == sorted(v.inherited)
    return norm == v.inherited


def no_change_words(R, change):
    pl, key = change.place, change.key
    parent = parent_name(R, pl, key) or "Defaults"
    v = Val(R, pl, key)
    if change.value == "inherit":
        return f"{R.name(pl)} already follows {parent} for {ms.label(key)}."
    if not v.set and pl.kind != "defaults":
        return (f"{ms.describe_change(pl, key, v.value, v.value, R.look)}, "
                f"same as {parent}.")
    return ms.describe_change(pl, key, v.value, v.value, R.look) + "."


def no_change_text(R, p):
    pl = p.change.place
    if p.change.value == "inherit":
        parent = parent_name(R, pl, p.key) or "Defaults"
        return f"{R.name(pl)} already follows {parent} for {ms.label(p.key)}."
    return ms.describe_change(pl, p.key, p.old, p.new, R.look, p.path) + "."


def do_undo(R, place, expect=None, origin=None, confirmed=False):
    origin = origin or (place, "b")
    if R.st.overlay_error:
        R.banner = banner_error(OVERLAY_BROKEN.lstrip("⚠ "))
        return show(R, *origin)
    if R.admin:
        if place.kind not in ("server", "channel") \
                or place.guild_id != R.admin_gid:
            return notice(ADMIN_ONLY_QUIETER)
        up, children, admin_gid = ms.Place.server(R.admin_gid), True, \
            R.admin_gid
    else:
        up, children, admin_gid = place, place.kind == "server", None
    try:
        text, gid = ms.undo_preview(R.st, up, include_children=children,
                                    names=R.look, admin_gid=admin_gid)
        plan = ms.undo_plan(R.st, up, children, admin_gid)
    except ms.SettingsError as e:
        R.banner = banner_error(plain_error(e))
        return show(R, *origin)
    if not gid:
        R.banner = [td("Nothing to undo here.")]
        return show(R, *origin)
    if expect and expect != gid:
        try:
            b = btn("Undo that", cid("un", ptok(up), gid))
        except BadId:
            b = btn("Undo that", cid("un", ptok(up)))
        R.banner = [section(clip(f"Something changed since. Undo now would: "
                                 f"{text[len('Undo: '):]}", 900), b)]
        return show(R, *origin)
    if R.owner and not confirmed:
        try:
            a = ms.assess(R.st, plan, R.ctx, R.look)
        except ms.SettingsError:
            a = None
        if a is not None and a.risky:
            tok = R.ctx.stash_put(R.uid, {
                "kind": "undo", "place": ptok(up), "expect": gid,
                "origin": [ptok(origin[0]), origin[1]]}, STASH_TTL)
            lines = ["Undo would make: " + clip(text[len("Undo: "):], 600)]
            return message(R, page_confirm(R, origin, lines, a.warnings,
                                           tok, undo=True))
    if R.admin:
        text = text.replace(" (this includes a change in a channel of this "
                            "server)", "")
    try:
        kw = {"children": children, "expect": gid}
        if admin_gid:
            kw["admin_gid"] = admin_gid
        keys = R.ctx.undo(up, R.uid, **kw)
    except ms.SettingsError as e:
        if R.admin:
            return notice(ADMIN_ONLY_QUIETER)
        R.banner = banner_error(plain_error(e))
        return show(R, *origin)
    if not keys:
        R.banner = [td("Nothing to undo here.")]
        return show(R, *origin)
    if R.admin:
        before = R.st
        R.reload()
        for ch in plan:
            try:
                sc = _scope_at(ch.place, ch.key, ch.path)
                R.ctx.notify_owners_admin_change(
                    R.admin_gid, R.uid,
                    ms.format_key(ms.change_path(before, ch), ch.key),
                    before.get(ch.key, sc), R.st.get(ch.key, sc))
            except Exception as e:
                R.ctx.log(f"settings_ui: admin notice: {e}")
    R.reload()
    R.banner = [td(clip(f"**Undone:** {text[len('Undo: '):]}", 900))]
    return show(R, *origin)


# ---------------------------------------------------------------------------
# Building changes from a click
# ---------------------------------------------------------------------------
def people_from(R, values, place):
    """([ids and "role:<id>"], roles dropped?) from a user / mentionable
    select's values. Only ids Discord resolved count (a forged value is
    dropped), and a role only for the server the interaction is in: a
    role id means nothing in another server."""
    res = R.data.get("resolved") or {}
    users = {str(u) for u in (res.get("users") or {})} | \
        {str(u) for u in (res.get("members") or {})}
    roles = {str(r) for r in (res.get("roles") or {})}
    own = place.guild_id is not None and R.in_guild(place.guild_id)
    out, dropped = [], False
    for v in values:
        if v in roles:
            if own:
                out.append(f"role:{v}")
            else:
                dropped = True
        elif v in users and _ID.fullmatch(v):
            out.append(v)
    return list(dict.fromkeys(out)), dropped


def admin_ignore(R, gid):
    """(the owners' own Never answer list at the server's layer, the
    admins' additions) for a server: an admin's entry holds only those
    two (the engine adds what the owners set elsewhere)."""
    path = ("servers", str(gid))
    s_, pin, _src = ms.owners_view(R.st, gid).layer(path, "who.ignore")
    pin = list(pin) if s_ and isinstance(pin, list) else []
    e = R.st.overlay.get((path, "who.ignore"))
    own = e.get("_norm") if isinstance(e, dict) \
        and ms.entry_role(e) == "admin" else None
    adds = [p for p in (own if isinstance(own, list) else [])
            if p not in pin]
    return pin, adds


def list_change(R, place, key, new):
    """A Change setting a people list, or a reset when it matches what
    the place would inherit anyway."""
    v = Val(R, place, key)
    if key == "who.allow" and not new:
        return ms.Change(place, key, "inherit")
    if key == "who.ignore" and not new and not v.inherited:
        return ms.Change(place, key, "inherit")
    if isinstance(v.inherited, list) and sorted(v.inherited) == sorted(new):
        return ms.Change(place, key, "inherit")
    return ms.Change(place, key, new)


def owners_set(R):
    return {str(o) for o in R.ctx.owners}


def changes_for_people(R, place, key, picked, add_only=False):
    """(changes, notes, named) for a people select."""
    notes = []
    owners = owners_set(R)
    if key == "who.dms_from":
        people = [p for p in picked if not p.startswith("role:")
                  and p not in owners]
        if len(people) < len([p for p in picked
                              if not p.startswith("role:")]):
            notes.append("Owners can always DM it.")
        if not people:
            return ms.dms_from_changes(R.st, "owners"), notes, ()
        return ms.dms_from_changes(R.st, "list", people), notes, people
    if key == "who.ignore":
        kept = [p for p in picked if p not in owners]
        if len(kept) < len(picked):
            notes.append("Owners can't be added to Never answer.")
        if add_only:
            pin, adds = admin_ignore(R, place.guild_id)
            new = list(dict.fromkeys(pin + adds + kept))
            return [ms.Change(place, key, new)], notes, ()
        return [list_change(R, place, key, kept)], notes, ()
    if key == "plugins.media.who":
        if not picked:
            return [ms.Change(place, key, "owners")], notes, ()
        full = list(dict.fromkeys(sorted(owners) + picked))
        return [list_change(R, place, key, full)], notes, ()
    return [list_change(R, place, key, picked)], notes, ()


def fix_changes(R, place, key, op):
    """The changes a Why fix makes (None: malformed)."""
    if op == "=":
        return [ms.Change(place, key, "inherit")]
    if op.startswith("v") and len(op) > 1:
        return [ms.Change(place, key, op[1:])]
    if op[:1] in "+-" and _ID.fullmatch(op[1:]):
        uid = op[1:]
        if key == "who.dms_from":
            if place.kind != "dms" or op[0] != "+":
                return None
            return ms.dms_from_changes(R.st, "list", dm_people(R) + [uid])
        spec = R.st.spec(key)
        if spec is None or spec.type != "people":
            return None
        if R.admin and key == "who.ignore" and op[0] == "-" \
                and place.kind == "server":
            pin, adds = admin_ignore(R, place.guild_id)
            left = pin + [p for p in adds if p != uid]
            return [ms.Change(place, key, left if left != pin or not adds
                              else "inherit")]
        cur = value(R, place, key)
        cur = list(cur) if isinstance(cur, list) else []
        new = cur + [uid] if op[0] == "+" else [p for p in cur if p != uid]
        return [list_change(R, place, key, list(dict.fromkeys(new)))]
    return None


def why_origin(R, fallback):
    """The Why page a fix was clicked on (from the message's own Refresh
    or Open button), else fallback."""
    msg = R.ix.get("message") or {}

    def walk(comps):
        for c in comps or []:
            if not isinstance(c, dict):
                continue
            parts = str(c.get("custom_id") or "").split(":")
            if len(parts) == 5 and parts[:3] == [PREFIX, VERSION, "pg"] \
                    and parts[4] in ("y", "b", "q"):
                p = R.place(parts[3])
                if p is not None:
                    return p
            for sub in (c.get("components"), [c.get("accessory")]):
                got = walk(sub)
                if got is not None:
                    return got
        return None
    return walk(msg.get("components")) or fallback


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def handle(interaction, ctx):
    """The full interaction callback body ({"type": 4|7|9, "data": ...})
    for /settings, the "Why no reply?" message command, or a component /
    modal submit whose custom_id starts with "mdset:". Never raises: an
    error is logged and answered with an ephemeral apology. No network."""
    try:
        return _handle(interaction, ctx)
    except Exception as e:
        try:
            ctx.log(f"settings_ui: {type(e).__name__}: {e}\n"
                    + traceback.format_exc())
        except Exception:
            pass
        return notice(APOLOGY)


def refusal(R):
    return notice(REFUSAL_SERVER if R.guild_id else REFUSAL_DM)


def _handle(ix, ctx):
    R = Req(ix, ctx)
    data = R.data
    t = ix.get("type")
    if t == 2:
        R.new = True
        if data.get("type") == 3 or data.get("name") in WHY_COMMANDS:
            if data.get("name") not in WHY_COMMANDS:
                return notice(OUT_OF_DATE)
            got = why_card(R)
            return got if isinstance(got, dict) else message(R, got)
        if data.get("name") != COMMAND:
            return notice(OUT_OF_DATE)
        return on_command(R)
    if t not in (3, 5):
        return notice(OUT_OF_DATE)
    custom = str(data.get("custom_id") or "")
    parts = custom.split(":")
    if parts[0] != PREFIX or len(parts) < 2 or len(custom) > CUSTOM_ID_MAX:
        return notice(OUT_OF_DATE)
    if parts[1] == "open":
        # The join DM's Configure button (kept: join DMs are durable).
        if len(parts) != 4 or parts[2] != "server" \
                or not _ID.fullmatch(parts[3]):
            return notice(OUT_OF_DATE)
        if not R.owner:
            return refusal(R)
        R.new = True
        return show(R, ms.Place.server(parts[3]), "b")
    if parts[1] != VERSION or len(parts) < 4:
        return notice(OUT_OF_DATE)
    if R.role is None:
        return refusal(R)
    verb, args = parts[2], parts[3:]
    place = R.place(args[0])
    if place is None:
        return notice(OUT_OF_DATE)
    if R.admin and not admin_may(R, verb, place, args):
        return notice(ADMIN_ONLY_QUIETER)
    fn = VERBS.get(verb)
    if fn is None:
        return notice(OUT_OF_DATE)
    values = [str(v) for v in data.get("values") or []]
    return fn(R, place, args[1:], values)


def admin_may(R, verb, place, args):
    """What a server admin may even try (the engine's admin rule checks
    every write again): their own server's panel, Why, and quieter
    writes there."""
    if place.kind not in ("server", "channel") \
            or place.guild_id != R.admin_gid:
        return False
    if place.kind == "channel" and R.chan(place.guild_id,
                                          place.channel_id) is None:
        return False
    if verb == "pg":
        return len(args) > 1 and args[1] in ("q", "y", "b")
    return verb in ("val", "set", "ppl", "rm", "un", "fx", "pz", "ok")


def on_command(R):
    """/settings: owners get the place they typed it in; server admins
    the "Quiet the bot" panel; anyone else a refusal."""
    if R.role is None:
        return refusal(R)
    if not R.guild_id:
        return show(R, ms.Place.dms(), "b")
    if R.admin:
        return message(R, page_quiet(R, R.admin_gid))
    here, thread = R.typed()
    if not here:
        return show(R, ms.Place.server(R.guild_id), "b")
    R.thread_note = thread
    if (R.chan(R.guild_id, here) or {}).get("type") == CATEGORY:
        return show(R, ms.Place.server(R.guild_id), "b")
    place = ms.Place.channel(R.guild_id, here,
                             R.category_of(R.guild_id, here))
    return show(R, place, "b")


def gone(R, place):
    """The parent page when place no longer exists (a deleted channel, a
    server the bot left), else None."""
    if place.kind == "channel":
        chans = R.channels(place.guild_id)
        if chans and place.channel_id not in chans \
                and R.resolved_channel != place.channel_id \
                and place.channel_id not in R.st.channels_listed(
                    place.guild_id):
            R.banner = [td("That channel no longer exists.")]
            return show(R, ms.Place.server(place.guild_id), "b")
    if place.kind in ("server", "channel"):
        gs = R.guilds()
        if gs and place.guild_id not in gs \
                and place.guild_id not in R.st.servers_listed():
            R.banner = [td("The bot isn't in that server any more.")]
            return show(R, ms.Place.defaults(), "b")
    return None


def v_pg(R, place, args, values):
    # pg:<place>:<page>[:<from>]: <from> (a place token) only tells two
    # buttons to the same page apart.
    if not args or args[0] not in PAGES or len(args) > 2 \
            or len(args) == 2 and R.place(args[1]) is None:
        return notice(OUT_OF_DATE)
    page = args[0]
    if R.admin:
        if page == "b":
            page = "q"
        return show(R, place, page)
    if page == "q":
        page = "b"
    return gone(R, place) or show(R, place, page)


def v_go(R, place, args, values):
    if not R.owner or not values:
        return refusal(R) if not R.owner else notice(OUT_OF_DATE)
    v = values[0]
    m = re.fullmatch(r"([po])(\d{1,4})", v)
    if m:
        if m[1] == "o":
            R.server_page = int(m[2])
            return show(R, place, "o")
        R.go_page = int(m[2])
        return show(R, place, "b")
    target = R.place(v)
    if target is None or target.kind == "channel":
        return notice(OUT_OF_DATE)
    return gone(R, target) or show(R, target, "b")


def v_gc(R, place, args, values):
    if not R.owner or not values or place.kind not in ("server", "channel"):
        return refusal(R) if not R.owner else notice(OUT_OF_DATE)
    gid = place.guild_id
    v = values[0]
    m = re.fullmatch(r"p(\d{1,4})", v)
    if m:
        R.chan_page = int(m[1])
        return show(R, place, "b")
    if v == "find":
        return open_modal(R, place, "fc", "b")
    if not _ID.fullmatch(v):
        return notice(OUT_OF_DATE)
    res = (R.data.get("resolved") or {}).get("channels") or {}
    if v in res:
        R.resolved_channel = v
        info = res[v] if isinstance(res[v], dict) else {}
        if info.get("type") == CATEGORY:
            return notice(OUT_OF_DATE)
    elif R.chan(gid, v) is None:
        return notice(OUT_OF_DATE)
    target = ms.Place.channel(gid, v, R.category_of(gid, v))
    return show(R, target, "b")


def _key(R, place, code):
    key = kdecode(code)
    if key is None or R.st.spec(key) is None:
        return None
    return key


def v_set(R, place, args, values):
    if len(args) != 1 or not values:
        return notice(OUT_OF_DATE)
    key = _key(R, place, args[0])
    if key is None or not offered(R, place, key):
        return notice(OUT_OF_DATE)
    v = values[0]
    page = page_of(place, key)
    if key == "who.dms_from":
        if not R.owner:
            return refusal(R)
        if v == "list":
            if ms.dm_access(R.st) == "list":
                R.banner = [td("**No change:** you already picked people "
                               "who can DM it.")]
                return show(R, place, page)
            R.pending = "who.dms_from"
            return show(R, place, page)
        if v not in ("owners", "shared-servers", "anyone", "nobody"):
            return notice(OUT_OF_DATE)
        return do_changes(R, (place, page),
                          ms.dms_from_changes(R.st, v))
    if key == "plugins.media.who" and v == "list":
        if isinstance(value(R, place, key), list):
            R.banner = [td("**No change:** pick people below.")]
            return show(R, place, page)
        R.pending = key
        return show(R, place, page)
    if key == "plugins.welcome.channel":
        m = re.fullmatch(r"p(\d{1,4})", v)
        if m:
            R.chan_page = int(m[1])
            return show(R, place, page)
        if not _ID.fullmatch(v):
            return notice(OUT_OF_DATE)
    write = "inherit" if v == "=" else v
    spec = R.st.spec(key)
    if spec.type == "choice" and write != "inherit" \
            and write not in spec.values:
        if write in spec.hidden_values:
            R.banner = [td("**No change.**")]
            return show(R, place, page)
        return notice(OUT_OF_DATE)
    return do_changes(R, (place, page), [ms.Change(place, key, write)])


def v_val(R, place, args, values):
    if len(args) not in (2, 3):
        return notice(OUT_OF_DATE)
    key = _key(R, place, args[0])
    if key is None:
        return notice(OUT_OF_DATE)
    page = args[2] if len(args) == 3 else page_of(place, key)
    if page not in PAGES:
        return notice(OUT_OF_DATE)
    if not offered(R, place, key) and not R.admin:
        return notice(OUT_OF_DATE)
    v = args[1]
    write = "inherit" if v == "=" else v
    spec = R.st.spec(key)
    if write != "inherit":
        ok, norm = ms.normalize(spec, write)
        if not ok:
            return notice(OUT_OF_DATE)
    return do_changes(R, (place, page), [ms.Change(place, key, write)])


def v_ppl(R, place, args, values):
    if len(args) != 1:
        return notice(OUT_OF_DATE)
    key = _key(R, place, args[0])
    if key is None or not offered(R, place, key) and not R.admin:
        return notice(OUT_OF_DATE)
    spec = R.st.spec(key)
    if key != "who.dms_from" and (spec is None or spec.type != "people"):
        return notice(OUT_OF_DATE)
    picked, dropped = people_from(R, values, place)
    if R.admin and (key != "who.ignore" or place.kind != "server"):
        return notice(ADMIN_ONLY_QUIETER)
    if R.admin and not picked:
        return notice(OUT_OF_DATE)
    if dropped and not picked:
        R.banner = banner_error(ROLES_ELSEWHERE)
        return show(R, place, "q" if R.admin else page_of(place, key))
    if not roles_here(R, place) and key != "who.dms_from":
        # A user select (see people_select): keep the roles on the list.
        cur = value(R, place, key)
        picked = picked + [p for p in (cur if isinstance(cur, list)
                                       else []) if p.startswith("role:")]
    changes, notes, named = changes_for_people(R, place, key, picked,
                                               add_only=R.admin)
    if dropped:
        notes = list(notes) + [ROLES_ELSEWHERE]
    return do_changes(R, (place, page_of(place, key)), changes,
                      named=named, notes=notes)


def v_rm(R, place, args, values):
    if len(args) != 1 or not values:
        return notice(OUT_OF_DATE)
    key = _key(R, place, args[0])
    if key not in ("who.dms_from", "who.ignore"):
        return notice(OUT_OF_DATE)
    gone_ = set(values)
    if key == "who.dms_from":
        if not R.owner or place.kind != "dms":
            return refusal(R) if not R.owner else notice(OUT_OF_DATE)
        left = [p for p in dm_people(R) if p not in gone_]
        changes = ms.dms_from_changes(R.st, "list", left) if left \
            else ms.dms_from_changes(R.st, "owners")
        return do_changes(R, (place, "b"), changes)
    if place.kind != "server" and not (R.owner and place.kind in
                                       ("defaults", "dms")):
        return notice(OUT_OF_DATE)
    if R.admin:
        pin, adds = admin_ignore(R, place.guild_id)
        if gone_ - set(adds):
            return notice(ADMIN_ONLY_QUIETER)
        left = pin + [p for p in adds if p not in gone_]
        return do_changes(R, (place, "q"), [ms.Change(
            place, key, left if left != pin or not adds else "inherit")])
    cur = value(R, place, key) or []
    left = [p for p in cur if p not in gone_]
    return do_changes(R, (place, "q" if R.admin else page_of(place, key)),
                      [list_change(R, place, key, left)])


def v_mod(R, place, args, values):
    if not R.owner:
        return refusal(R)
    if not args or len(args) > 2:
        return notice(OUT_OF_DATE)
    form = args[0]
    page = args[1] if len(args) == 2 else "b"
    if page not in PAGES or not form_ok(R, place, form):
        return notice(OUT_OF_DATE)
    m = open_modal(R, place, form, page)
    return m or notice(OUT_OF_DATE)


def v_re(R, place, args, values):
    if not R.owner:
        return refusal(R)
    if len(args) != 2 or not form_ok(R, place, args[0]):
        return notice(OUT_OF_DATE)
    try:
        draft = R.ctx.stash_take(R.uid, args[1])
    except PermissionError:
        return notice(WRONG_USER)
    if not isinstance(draft, dict) or draft.get("kind") != "draft":
        draft = None
    page = (draft or {}).get("page") or "b"
    return open_modal(R, place, args[0], page if page in PAGES else "b",
                      draft) or notice(OUT_OF_DATE)


def v_sub(R, place, args, values):
    if not R.owner:
        return refusal(R)
    if R.ix.get("type") != 5 or not args or len(args) > 2:
        return notice(OUT_OF_DATE)
    form = args[0]
    page = args[1] if len(args) == 2 else "b"
    if page not in PAGES or not form_ok(R, place, form):
        return notice(OUT_OF_DATE)
    fields = modal_fields(R.data)
    origin = (place, page)

    def fail(text):
        tok = R.ctx.stash_put(R.uid, {"kind": "draft", "form": form,
                                      "values": fields, "page": page},
                              STASH_TTL)
        try:
            retry = cid("re", ptok(place), form, tok)
        except BadId:
            retry = None
        R.banner = banner_error(text, retry)
        return show(R, place, page)

    if form == "fc":
        R.chan_query = (fields.get("q") or "").strip()[:100] or None
        return show(R, place, "b")
    if form == "fs":
        R.server_query = (fields.get("q") or "").strip()[:100] or None
        return show(R, place, "o")
    if form == "id.wy":
        ids = parse_ids(fields.get("ids"))
        if not ids:
            return fail("No user ID found. Copy one with Developer Mode on.")
        R.check = ids[0]
        return show(R, place, "y")
    if form == "id.df":
        ids = parse_ids(fields.get("ids"))
        if not ids:
            return fail("No user ID found. Copy one with Developer Mode on.")
        owners = owners_set(R)
        new = [i for i in ids if i not in owners]
        notes = ["Owners can always DM it."] if len(new) < len(ids) else []
        if not new:
            R.banner = [td("**No change:** owners can always DM it.")]
            return show(R, place, page)
        people = list(dict.fromkeys(dm_people(R) + new))
        return do_changes(R, origin,
                          ms.dms_from_changes(R.st, "list", people),
                          named=new, notes=notes)
    changes = []
    if form == "i":
        if "n" not in fields:
            return notice(OUT_OF_DATE)
        text = fields["n"].strip()
        if len(text) > ms.NOTES_MAX:
            return fail(f"Instructions take up to {ms.NOTES_MAX} "
                        f"characters; that was {len(text):,}.")
        changes.append(ms.Change(place, "style.notes", text or "inherit"))
        ln = fields.get("ln")
        if isinstance(ln, list) and ln and place.kind != "channel":
            if ln[0] == "=" and place.kind != "defaults":
                changes.append(ms.Change(place, "style.length", "inherit"))
            elif ln[0] in ("brief", "normal", "detailed"):
                changes.append(ms.Change(place, "style.length", ln[0]))
    elif form == "c":
        for name, key in (("pp", "limits.per_person_per_day"),
                          ("pd", "limits.per_day")):
            if name not in fields or not offered(R, place, key):
                continue
            v, err = parse_cap(R, key, fields[name])
            if err:
                return fail(err)
            changes.append(ms.Change(place, key, v))
    elif form in ("bx", "mpd"):
        key = CAP_FIELDS[form]
        if "v" not in fields:
            return notice(OUT_OF_DATE)
        v, err = parse_cap(R, key, fields["v"])
        if err:
            return fail(err)
        changes.append(ms.Change(place, key, v))
    elif form == "wn":
        text = (fields.get("v") or "").strip()
        if len(text) > 500:
            return fail(f"Welcome notes take up to 500 characters; that was "
                        f"{len(text):,}.")
        changes.append(ms.Change(place, "plugins.welcome.notes",
                                 text or "inherit"))
    elif form == "st":
        text = (fields.get("v") or "").strip()
        if len(text) > 128:
            return fail(f"The status line takes up to 128 characters; that "
                        f"was {len(text):,}.")
        changes.append(ms.Change(place, "presence.status", text))
    if not changes:
        return notice(OUT_OF_DATE)
    return do_changes(R, origin, changes)


def v_pre(R, place, args, values):
    if not R.owner:
        return refusal(R)
    if args or place.kind != "server" or not values \
            or values[0] not in ms.PRESETS:
        return notice(OUT_OF_DATE)
    return message(R, page_preset(R, place, values[0]))


def v_ok(R, place, args, values):
    if len(args) != 1:
        return notice(OUT_OF_DATE)
    try:
        payload = R.ctx.stash_take(R.uid, args[0])
    except PermissionError:
        return notice(WRONG_USER)
    if not R.owner:
        return refusal(R) if not R.admin else notice(ADMIN_ONLY_QUIETER)
    if not isinstance(payload, dict) or payload.get("kind") not in (
            "apply", "undo"):
        return message(R, [container([td(EXPIRED)], BLURPLE),
                           row(btn(f"Open {R.name(place)} settings",
                                   cid("pg", ptok(place), "b")))])
    otok, opage = (payload.get("origin") or [ptok(place), "b"])[:2]
    oplace = R.place(otok) or place
    opage = opage if opage in PAGES else "b"
    if payload["kind"] == "undo":
        up = R.place(payload.get("place") or "")
        if up is None:
            return notice(OUT_OF_DATE)
        return do_undo(R, up, payload.get("expect"), (oplace, opage),
                       confirmed=True)
    try:
        changes = []
        for d in payload.get("changes") or []:
            c = ms.Change.from_dict(d, R.ctx)
            c.revert = bool(d.get("revert"))
            changes.append(c)
    except ms.SettingsError:
        return notice(OUT_OF_DATE)
    return do_changes(R, (oplace, opage), changes, confirmed=True,
                      named=payload.get("named") or (),
                      notes=payload.get("notes") or ())


def v_un(R, place, args, values):
    if len(args) > 1:
        return notice(OUT_OF_DATE)
    origin = (place, "q" if R.admin else "b")
    msg = R.ix.get("message") or {}
    # Undo from the Why page or Extras: stay on that page.
    page = page_in_message(R, msg)
    if page and not R.admin:
        origin = page
    return do_undo(R, place, args[0] if args else None, origin)


def page_in_message(R, msg):
    """(place, page) the clicked message showed, from its own controls:
    Why has a Refresh button; the other pages' val / mod ids end with
    their page. None when it can't tell."""
    best = None
    for c in _walk(msg.get("components") or []):
        parts = str(c.get("custom_id") or "").split(":")
        if len(parts) < 4 or parts[:2] != [PREFIX, VERSION]:
            continue
        if parts[2] == "pg" and len(parts) == 5 and parts[4] == "y" \
                and c.get("label") == "Refresh":
            p = R.place(parts[3])
            if p is not None:
                return p, "y"
        if parts[2] in ("val", "mod") and len(parts) >= 6 \
                and parts[-1] in PAGES and best is None:
            p = R.place(parts[3])
            if p is not None:
                best = (p, parts[-1])
    return best


def _walk(comps):
    for c in comps:
        if not isinstance(c, dict):
            continue
        yield c
        yield from _walk(c.get("components") or [])
        if isinstance(c.get("accessory"), dict):
            yield c["accessory"]


def v_pz(R, place, args, values, resume=False):
    if args:
        return notice(OUT_OF_DATE)
    if resume:
        changes = ms.resume_changes(R.st, place)
        if not changes:
            R.banner = [td(f"**No change:** {R.name(place)} isn't "
                           f"paused.")]
            return show(R, place, "b")
    else:
        changes = ms.pause_changes(place)
    return do_changes(R, (place, "q" if R.admin else "b"), changes)


def v_rs(R, place, args, values):
    if R.admin:
        return notice(ADMIN_ONLY_QUIETER)
    return v_pz(R, place, args, values, resume=True)


def v_fx(R, place, args, values):
    if len(args) != 2:
        return notice(OUT_OF_DATE)
    key = _key(R, place, args[0])
    if key is None:
        return notice(OUT_OF_DATE)
    if R.admin and key == "who.ignore" and args[1].startswith("-") \
            and args[1][1:] not in admin_ignore(R, place.guild_id)[1]:
        return notice(ADMIN_ONLY_QUIETER)
    changes = fix_changes(R, place, key, args[1])
    if not changes:
        return notice(OUT_OF_DATE)
    origin = why_origin(R, place)
    if args[1][:1] in "+-":
        R.fixed = args[1][1:]
    if place.kind == "dms" and key == "who.dms_from":
        R.check = args[1][1:]
        return do_changes(R, (ms.Place.dms(), "y"), changes,
                          named=[args[1][1:]])
    return do_changes(R, (origin, "y"), changes)


def v_wy(R, place, args, values):
    if not R.owner:
        return refusal(R)
    if place.kind != "dms" or len(args) > 1:
        return notice(OUT_OF_DATE)
    uid = args[0] if args else (values[0] if values else None)
    if not uid or not _ID.fullmatch(uid):
        return notice(OUT_OF_DATE)
    R.check = uid
    return show(R, place, "y")


VERBS = {"pg": v_pg, "go": v_go, "gc": v_gc, "set": v_set, "val": v_val,
         "ppl": v_ppl, "rm": v_rm, "mod": v_mod, "sub": v_sub, "re": v_re,
         "pre": v_pre, "ok": v_ok, "un": v_un, "pz": v_pz, "rs": v_rs,
         "fx": v_fx, "wy": v_wy}
