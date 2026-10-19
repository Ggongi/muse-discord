#!/usr/bin/env python3
"""muse-discord settings: schema, validation, scopes, the Discord overlay,
and the mapping of the old config layout.

One module, read by monitor.py, presence.py, bin/settings.py and (through
the small command line at the bottom) watchdog.sh and the wake hook.
Standard library plus PyYAML only.

Layers, least specific first (the most specific value that is set wins):

    built-in -> everywhere -> dms                                  (a DM)
    built-in -> everywhere -> all_servers -> servers.<id>
             -> category -> channel                         (a server)

Threads and forum posts use their parent channel. Lists replace, never
merge. Only style.notes combines: the most specific of everywhere /
dms / server, then the category's notes, then the channel's.

At every layer the overlay (config.discord.yaml, written by Discord-side
changes) replaces the file's value at that same layer, unless the file
was edited after the change (the overlay entry's base no longer matches
the file): then the file wins and the entry is retired into history.

Caps (limits.*, plugins.media.per_day) count within the scope WHERE THEY
ARE SET: everywhere = one total, a server = that server, dms = all DMs.
Every cap set along the chain is enforced (see Settings.cap_layers).

An old-layout config.yaml (bot:, behavior:, safety:, owners.user_ids,
...) still loads unchanged: it is mapped onto the new schema on every
load (Settings.notice says so in one line), and `bin/settings.py
migrate` converts the file for good.
"""
import copy
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone

import yaml

OVERLAY_NAME = "config.discord.yaml"
HISTORY_CAP = 100
NOTES_MAX = 500
DEFAULT_TOKEN_FILE = "~/.config/muse-discord/bot_token"
DEFAULT_PERSONA = "persona.md"
PRESENCE_KINDS = ("watching", "playing", "listening", "streaming",
                  "competing", "custom")
SCOPE_NAMES = ("everywhere", "dms", "all_servers", "server", "channel")
_LETTER = {"E": "everywhere", "D": "dms", "A": "all_servers",
           "S": "server", "C": "channel"}
# Short key names a channel/category entry uses.
CHANNEL_SHORT = {"replies": "who.replies", "notes": "style.notes"}
ENGINE_KEYS = {
    "rest_poll_seconds": None, "discovery_refresh_minutes": None,
    "coalesce_seconds": None, "max_reply_chars": None, "guild_ids": None,
    "sweep": {"every_cycles", "grace_seconds", "max_age_hours",
              "max_recoveries"},
    "backoff": {"initial_seconds", "max_seconds"},
}
_ID_RE = re.compile(r"^[0-9]{1,25}$")


class SettingsError(Exception):
    """An invalid hand-written config (the message names file, key, scope,
    reason and the valid values; several problems are one per line)."""


def _scopes(spec):
    if isinstance(spec, str):
        return tuple(_LETTER[c] for c in spec)
    return tuple(spec)


class Spec:
    """One setting's type, default and the scopes that may set it."""

    def __init__(self, type, default, scopes, values=(), hidden_values=(),
                 min=None, max=None, none=False, maxlen=None, steers=False,
                 risky=False, discord_editable=True, allow_names=False,
                 hidden=False, legacy=None, description=None):
        self.type = type
        self.default = default
        self.scopes = _scopes(scopes)
        self.values = tuple(values)
        self.hidden_values = tuple(hidden_values)
        self.min, self.max, self.none = min, max, none
        self.maxlen = maxlen
        self.steers, self.risky = steers, risky
        self.discord_editable = discord_editable
        self.allow_names = allow_names
        self.hidden = hidden
        self.legacy = legacy
        self.description = description

    def valid_text(self):
        t = self.type
        if t == "choice":
            return ", ".join(self.values)
        if t == "on-off":
            return "on, off"
        if t == "number":
            rng = f"{self.min}-{self.max}" if self.max is not None \
                else f"{self.min} or more"
            return ("none, " if self.none else "") + rng
        if t == "people":
            kw = "".join(f"{v}, or " for v in self.values)
            return kw + 'a list of "<user id>" / "role:<role id>"'
        if t == "channel":
            kw = ", ".join(self.values)
            return "a channel id" + (f", {kw}" if kw else "") + (
                ", or a channel name" if self.allow_names else "")
        if t == "text":
            return f"text up to {self.maxlen} characters" if self.maxlen \
                else "text"
        if t == "phrases":
            return "a list of words or phrases"
        return "any value"


CORE = {
    "who.replies": Spec("choice", "conversational", "DASC",
                        values=("off", "when-addressed", "conversational")),
    "who.dms_from": Spec("choice", "owners", "D",
                         values=("owners", "shared-servers", "anyone"),
                         risky=True),
    "who.allow": Spec("people", "everyone", "EDS", values=("everyone",)),
    "who.ignore": Spec("people", [], "EDS"),
    "who.bots": Spec("choice", "when-addressed", "EDS",
                     values=("ignore", "when-addressed")),
    "who.bot_exchange_limit": Spec("number", None, "ES", min=1, max=50,
                                   none=True),
    "style.length": Spec("choice", "normal", "EDS",
                         values=("brief", "normal", "detailed"),
                         steers=True),
    "style.notes": Spec("text", "", "EDSC", maxlen=NOTES_MAX, steers=True),
    "abilities.owner_commands": Spec("on-off", True, "ES"),
    "privacy.memory": Spec("choice", "public-only", "EDS",
                           values=("everything", "public-only", "nothing"),
                           steers=True, risky=True),
    "privacy.never_share": Spec("phrases", [], "E", discord_editable=False),
    # "answer" (no check at all) exists only so a migrated
    # injection_quarantine: false keeps behaving as it did.
    "privacy.suspicious": Spec("choice", "hold-for-review", "EDS",
                               values=("hold-for-review", "ignore"),
                               hidden_values=("answer",)),
    "limits.per_day": Spec("number", None, "EDS", min=0, max=100000,
                           none=True),
    "limits.per_person_per_day": Spec("number", None, "EDS", min=0,
                                      max=10000, none=True),
}
CORE_GROUPS = ("who", "style", "abilities", "privacy", "limits")
TOP = {
    "presence.status": Spec("text", "", "E", maxlen=128),
    "presence.kind": Spec("choice", "watching", "E", values=PRESENCE_KINDS),
    # Hidden: the old presence.enabled: false (no presence client).
    "presence.enabled": Spec("on-off", True, "E", hidden=True,
                             discord_editable=False),
    "updates.notify": Spec("on-off", True, "E"),
}
TOP_DISCORD_EDITABLE = ("presence.status", "presence.kind",
                        "updates.notify")


# ---------------------------------------------------------------------------
# Plugin manifests (plugins/<folder>/plugin.yaml)
# ---------------------------------------------------------------------------
class Manifest:
    def __init__(self, pid, folder, specs, safe_in_new_servers,
                 legacy_names=(), hooks=()):
        self.id = pid
        self.folder = folder
        self.specs = specs
        self.safe_in_new_servers = safe_in_new_servers
        self.legacy_names = tuple(legacy_names)
        self.hooks = tuple(hooks)

    def spec_for(self, key):
        """The spec for a settings key or an old param name."""
        if key in self.specs:
            return key, self.specs[key]
        for k, s in self.specs.items():
            if s.legacy == key:
                return k, s
        return None, None


_MANIFEST_TYPES = ("on-off", "choice", "number", "text", "channel",
                   "people", "phrases", "any")


def load_manifests(plugins_dir):
    """{plugin id: Manifest} for every plugins/<folder>/plugin.yaml. A
    manifest with a `settings:` block is used as is; an old one with only
    `params:` ({default, per_guild} shapes included) gets a derived,
    untyped manifest so its config keeps loading."""
    out = {}
    if not plugins_dir or not os.path.isdir(plugins_dir):
        return out
    for folder in sorted(os.listdir(plugins_dir)):
        path = os.path.join(plugins_dir, folder, "plugin.yaml")
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                meta = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as e:
            raise SettingsError(f"plugins/{folder}/plugin.yaml: unreadable "
                                f"({e})")
        pid = str(meta.get("id") or folder)
        specs = {}
        raw = meta.get("settings")
        if isinstance(raw, dict):
            for key, s in raw.items():
                if not isinstance(s, dict) \
                        or s.get("type") not in _MANIFEST_TYPES:
                    raise SettingsError(
                        f"plugins/{folder}/plugin.yaml: settings.{key}: "
                        f"type must be one of {', '.join(_MANIFEST_TYPES)}")
                default = s.get("default")
                if s["type"] == "on-off":
                    default = _norm_onoff(default)
                elif s["type"] == "number" and default == "none":
                    default = None
                specs[str(key)] = Spec(
                    s["type"], default,
                    s.get("scopes") or ["everywhere", "dms", "all_servers",
                                        "server", "channel"],
                    values=s.get("values") or (),
                    min=s.get("min", 0 if s["type"] == "number" else None),
                    max=s.get("max"), none=bool(s.get("none")),
                    maxlen=s.get("maxlen"),
                    steers=bool(s.get("steers_muse")),
                    risky=bool(s.get("risky")),
                    discord_editable=s.get("discord_editable", True)
                    is not False,
                    allow_names=bool(s.get("allow_names")),
                    hidden=bool(s.get("hidden")), legacy=s.get("legacy"),
                    description=(str(s["description"]).strip()
                                 if s.get("description") else None))
        else:
            for key, p in (meta.get("params") or {}).items():
                default = p.get("default") if isinstance(p, dict) else None
                specs[str(key)] = Spec("any", copy.deepcopy(default),
                                       "EDASC")
        if "enabled" not in specs:
            specs["enabled"] = Spec("on-off", False, "EDASC")
        safe = meta.get("safe_in_new_servers")
        out[pid] = Manifest(pid, folder, specs,
                            True if safe is None else bool(safe),
                            legacy_names=[folder] if folder != pid else [],
                            hooks=meta.get("hooks") or ())
    return out


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------
def _norm_onoff(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, str) and v.strip().lower() in ("on", "off", "true",
                                                    "false", "yes", "no"):
        return v.strip().lower() in ("on", "true", "yes")
    return None


def _id_str(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str) and _ID_RE.match(v.strip()):
        return v.strip()
    return None


def normalize(spec, value):
    """(ok, normalized value or reason). Accepts YAML 1.1 booleans for
    on/off (and False for an "off" choice)."""
    t = spec.type
    if t == "any":
        return True, value
    if t == "on-off":
        v = _norm_onoff(value)
        return (True, v) if v is not None else (False, "expected on or off")
    if t == "choice":
        if value is False and "off" in spec.values:
            return True, "off"
        if value is True and "on" in spec.values:
            return True, "on"
        if isinstance(value, str):
            v = value.strip().lower()
            if v in spec.values or v in spec.hidden_values:
                return True, v
        return False, f"{_show(value)} is not a valid value"
    if t == "number":
        if value is None or (isinstance(value, str)
                             and value.strip().lower() == "none"):
            if spec.none:
                return True, None
            return False, "a number is required here"
        if isinstance(value, bool):
            return False, f"{_show(value)} is not a number"
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
            value = int(value.strip())
        if not isinstance(value, int):
            return False, f"{_show(value)} is not a whole number"
        if spec.min is not None and value < spec.min:
            return False, f"{value} is below the minimum {spec.min}"
        if spec.max is not None and value > spec.max:
            return False, f"{value} is above the maximum {spec.max}"
        return True, value
    if t == "text":
        if value is None:
            return True, ""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str):
            return False, "expected text"
        if spec.maxlen and len(value) > spec.maxlen:
            return False, (f"too long ({len(value)} characters; the limit "
                           f"is {spec.maxlen})")
        return True, value
    if t == "phrases":
        if value is None:
            return True, []
        if not isinstance(value, list):
            return False, "expected a list of phrases"
        out = []
        for p in value:
            if not isinstance(p, (str, int, float)) or isinstance(p, bool) \
                    or not str(p).strip():
                return False, f"{_show(p)} is not a phrase"
            out.append(str(p).strip())
        return True, out
    if t == "people":
        if isinstance(value, str) and value.strip().lower() in spec.values:
            return True, value.strip().lower()
        if value is None:
            value = []
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            value = [value]
        if not isinstance(value, list):
            return False, f"{_show(value)} is not a valid value"
        out = []
        for p in value:
            pid = _id_str(p)
            if pid is None and isinstance(p, str) \
                    and p.strip().lower().startswith("role:"):
                rid = _id_str(p.strip()[5:].strip())
                if rid:
                    out.append("role:" + rid)
                    continue
            if pid is None:
                return False, (f"{_show(p)} is not a user id or "
                               f"\"role:<id>\" (expected a Discord id)")
            out.append(pid)
        return True, out
    if t == "channel":
        if isinstance(value, str) and value.strip().lower() in spec.values:
            return True, value.strip().lower()
        if value is False and "off" in spec.values:
            return True, "off"
        cid = _id_str(value)
        if cid:
            return True, cid
        if spec.allow_names and isinstance(value, str) and value.strip():
            return True, value.strip()
        return False, f"{_show(value)}: expected a Discord channel id"
    return False, f"unknown type {t}"


def _show(v):
    if isinstance(v, str):
        return f'"{v}"'
    if v is True:
        return "on"
    if v is False:
        return "off"
    if v is None:
        return "none"
    return str(v)


def display(spec, v):
    """A value as owners write it (on/off, none, lists as is)."""
    if spec is not None and spec.type == "on-off" and isinstance(v, bool):
        return "on" if v else "off"
    if v is None and spec is not None and spec.type == "number":
        return "none"
    return v


def _dumpable(spec, v):
    return display(spec, v)


# ---------------------------------------------------------------------------
# Keys: overlay form 'servers."1".channels."2".replies'
# ---------------------------------------------------------------------------
def parse_key(s):
    """('scope path tuple', 'canonical key') for an overlay key, or raise
    ValueError. Scope paths: ("top",) ("everywhere",) ("dms",)
    ("all_servers",) ("servers", sid) ("servers", sid, "channels", cid)."""
    parts, buf, i = [], "", 0
    s = str(s)
    while i < len(s):
        c = s[i]
        if c == '"':
            j = s.find('"', i + 1)
            if j < 0:
                raise ValueError("unbalanced quote")
            buf += s[i + 1:j]
            i = j + 1
            continue
        if c == ".":
            parts.append(buf)
            buf = ""
        else:
            buf += c
        i += 1
    parts.append(buf)
    if not parts or any(p == "" for p in parts):
        raise ValueError("empty key part")
    head = parts[0]
    if head in ("presence", "updates"):
        return ("top",), ".".join(parts)
    if head in ("everywhere", "dms", "all_servers"):
        return (head,), _canon(parts[1:], False)
    if head == "servers" and len(parts) >= 3:
        sid = parts[1]
        if parts[2] == "channels" and len(parts) >= 5:
            return ("servers", sid, "channels", parts[3]), \
                _canon(parts[4:], True)
        return ("servers", sid), _canon(parts[2:], False)
    raise ValueError("unknown scope")


def _canon(rest, channel):
    key = ".".join(rest)
    if channel and key in CHANNEL_SHORT:
        return CHANNEL_SHORT[key]
    return key


def format_key(path, key):
    if path == ("top",):
        return key
    if path[0] == "servers":
        out = f'servers."{path[1]}"'
        if len(path) > 2:
            short = {v: k for k, v in CHANNEL_SHORT.items()}.get(key, key)
            return out + f'.channels."{path[3]}".{short}'
        return out + "." + key
    if path[0] == "alias":
        return f'servers."{path[1]}".{key}'
    return f"{path[0]}.{key}"


def _scope_kind(path):
    if path[0] in ("servers", "alias"):
        return "channel" if len(path) > 2 else "server"
    return path[0]


# ---------------------------------------------------------------------------
# Scope of an event
# ---------------------------------------------------------------------------
class Scope:
    """Where something happens. kind: everywhere | dms | all_servers |
    server. A server scope may name its channel (for a thread: the
    parent) and that channel's category."""

    def __init__(self, kind, guild_id=None, guild_name=None,
                 channel_id=None, category_id=None):
        self.kind = kind
        self.guild_id = str(guild_id) if guild_id else None
        self.guild_name = guild_name
        self.channel_id = str(channel_id) if channel_id else None
        self.category_id = str(category_id) if category_id else None

    @classmethod
    def dm(cls):
        return cls("dms")

    @classmethod
    def everywhere(cls):
        return cls("everywhere")

    @classmethod
    def server(cls, guild_id, guild_name=None, channel_id=None,
               category_id=None):
        return cls("server", guild_id, guild_name, channel_id, category_id)

    @property
    def is_dm(self):
        return self.kind == "dms"

    def key(self):
        return (self.kind, self.guild_id, self.guild_name, self.channel_id,
                self.category_id)

    def label(self):
        if self.kind != "server":
            return self.kind
        if not self.guild_id:
            return "all_servers (a server with no entry)"
        out = f"server {self.guild_id}"
        if self.guild_name:
            out += f" ({self.guild_name})"
        if self.category_id:
            out += f", category {self.category_id}"
        if self.channel_id:
            out += f", channel {self.channel_id}"
        return out


# ---------------------------------------------------------------------------
# Old layout
# ---------------------------------------------------------------------------
OLD_TOP = ("bot", "behavior", "safety", "plugins")
NEW_TOP = ("everywhere", "dms", "all_servers", "servers", "token_from",
           "version")
NOTICE = ("Note: config.yaml uses the old layout; it still works as is. "
          "Run `python3 bin/settings.py migrate` to convert it.")


def detect_layout(raw):
    """"old" or "new" (an empty file is new). Raises when it mixes both."""
    old, new = [], []
    for k in OLD_TOP:
        if k in raw:
            old.append(k)
    if isinstance(raw.get("owners"), dict):
        old.append("owners.user_ids")
    elif isinstance(raw.get("owners"), list):
        new.append("owners")
    if isinstance(raw.get("persona"), dict):
        old.append("persona.file")
    elif isinstance(raw.get("persona"), str):
        new.append("persona")
    pres = raw.get("presence")
    if isinstance(pres, dict):
        if "status_text" in pres or "status_kind" in pres:
            old.append("presence.status_text")
        if "status" in pres or "kind" in pres:
            new.append("presence.status")
    upd = raw.get("updates")
    if isinstance(upd, dict):
        if "check" in upd:
            old.append("updates.check")
        if "notify" in upd:
            new.append("updates.notify")
    new += [k for k in NEW_TOP if k in raw]
    if old and new:
        raise SettingsError(
            "config.yaml mixes the old layout (" + ", ".join(old)
            + ") and the new one (" + ", ".join(new) + "); use one. "
            "`bin/settings.py migrate` converts an old file.")
    return "old" if old else "new"


def _old_cap(v, name):
    if v is None:
        return None
    if isinstance(v, bool):
        raise SettingsError(f"config.yaml (old layout): {name}: {v} is not "
                            f"a number; use a number or null")
    try:
        n = float(v)
    except (TypeError, ValueError):
        raise SettingsError(f"config.yaml (old layout): {name}: {v!r} is "
                            f"not a number; use a number or null")
    # A negative cap blocked every stranger, exactly like 0.
    return max(0, int(n)) if n == int(n) or n < 0 else int(n) + 1


def map_old(raw, manifests):
    """Map an old-layout config dict onto the new schema. Returns
    (new-layout dict, aliases, respond_to, lines). aliases maps a server
    key that is not an id (old per_guild keys could be server names) to
    its settings block; it matches a server by id or by name, below that
    server's own entry. lines describe the mapping in plain words."""
    lines = []
    out = {}
    bot = raw.get("bot") or {}
    beh = raw.get("behavior") or {}
    saf = raw.get("safety") or {}
    servers = {}
    aliases = {}

    def put(d, dotted, value):
        cur = d
        parts = dotted.split(".")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        cur[parts[-1]] = value

    def server_block(key):
        key = str(key)
        if _ID_RE.match(key):
            return servers.setdefault(key, {})
        return aliases.setdefault(key, {})

    owners = (raw.get("owners") or {}).get("user_ids") or []
    if owners:
        out["owners"] = [str(u) for u in owners]
        lines.append("owners.user_ids -> owners")
    pfile = (raw.get("persona") or {}).get("file")
    if pfile:
        out["persona"] = str(pfile)
        lines.append("persona.file -> persona")
    env, tfile = bot.get("token_env"), bot.get("token_file")
    if env:
        out["token_from"] = [f"env:{env}", str(tfile or DEFAULT_TOKEN_FILE)]
        lines.append("bot.token_env + bot.token_file -> token_from "
                     "(the variable first, then the file)")
    elif tfile:
        out["token_from"] = str(tfile)
        lines.append("bot.token_file -> token_from")
    engine = copy.deepcopy(raw.get("engine") or {})
    if not isinstance(engine, dict):
        engine = {}
    for k in ("coalesce_seconds", "max_reply_chars"):
        if k in beh:
            engine[k] = beh[k]
            lines.append(f"behavior.{k} -> engine.{k}")
    if bot.get("guild_ids"):
        engine["guild_ids"] = [str(g) for g in bot["guild_ids"]]
        lines.append("bot.guild_ids -> engine.guild_ids")
    if engine:
        out["engine"] = engine
    respond = set(beh.get("respond_to") or ["mention", "reply_to_bot"])
    respond.discard("name")
    if "respond_to" in beh:
        lines.append("behavior.respond_to: dropped (an @mention and a "
                     "reply to the bot always count)")
    bots = str(beh.get("bots") or "direct").lower()
    if bots != "direct":
        put(out, "everywhere.who.bots", "ignore")
        lines.append("behavior.bots: off -> everywhere.who.bots: ignore")
    elif beh.get("bots") is False:
        # YAML reads an unquoted off as false, which the old code took
        # for "direct": kept, so the bot behaves as it did.
        lines.append("behavior.bots: off (unquoted, so YAML read false, "
                     "which answered bots as direct) -> who.bots: "
                     "when-addressed (the default); write ignore to stop")
    elif "bots" in beh:
        lines.append("behavior.bots: direct -> who.bots: when-addressed "
                     "(the default)")
    dm = str(beh.get("dm_policy") or "owner_only").lower()
    if dm == "off":
        put(out, "dms.who.replies", "off")
        lines.append("behavior.dm_policy: off -> dms.who.replies: off")
    elif dm != "owner_only":
        put(out, "dms.who.dms_from", "anyone")
        lines.append("behavior.dm_policy: open -> dms.who.dms_from: anyone")
        if bots == "direct":
            # Bots' DMs were never answered, whatever the policy.
            put(out, "dms.who.bots", "ignore")
            lines.append("(bots' DMs were never answered) -> "
                         "dms.who.bots: ignore")
    elif beh.get("dm_policy") is False:
        lines.append("behavior.dm_policy: off (unquoted, so YAML read "
                     "false, which the old code took for owner_only) -> "
                     "who.dms_from: owners (the default)")
    elif "dm_policy" in beh:
        lines.append("behavior.dm_policy: owner_only -> who.dms_from: "
                     "owners (the default)")
    active = [str(g) for g in beh.get("active_guild_ids") or []]
    if not bool(saf.get("injection_quarantine", True)):
        put(out, "everywhere.privacy.suspicious", "answer")
        lines.append("safety.injection_quarantine: false -> "
                     "everywhere.privacy.suspicious: answer")
    for old, new in (("daily_reply_cap", "per_day"),
                     ("per_user_daily_cap", "per_person_per_day")):
        if saf.get(old) is not None:
            put(out, f"everywhere.limits.{new}",
                _old_cap(saf[old], f"safety.{old}"))
            lines.append(f"safety.{old} -> everywhere.limits.{new} (one "
                         f"total across DMs and all servers)")
    pres = raw.get("presence") or {}
    if pres.get("enabled") is False:
        put(out, "presence.enabled", "off")
        lines.append("presence.enabled: false -> presence.enabled: off")
    if str(pres.get("status_text") or "").strip():
        put(out, "presence.status", str(pres["status_text"]).strip())
        lines.append("presence.status_text -> presence.status")
    kind = str(pres.get("status_kind") or "watching").lower()
    if kind not in PRESENCE_KINDS:
        kind = "watching"  # what presence.py did with an unknown kind
    if kind != "watching":
        put(out, "presence.kind", kind)
        lines.append("presence.status_kind -> presence.kind")
    if (raw.get("updates") or {}).get("check") is False:
        put(out, "updates.notify", "off")
        lines.append("updates.check: false -> updates.notify: off")
    for name, block in (raw.get("plugins") or {}).items():
        man = None
        for m in manifests.values():
            if name == m.id or name in m.legacy_names:
                man = m
        if man is None or not isinstance(block, dict):
            continue  # the old loader ignored config for absent plugins
        pid = man.id
        label = f"plugins.{name}"
        if block.get("enabled"):
            put(out, f"everywhere.plugins.{pid}.enabled", "on")
            lines.append(f"{label}.enabled -> everywhere.plugins.{pid}"
                         f".enabled")
            if not man.safe_in_new_servers:
                # Off in every server (built in for all_servers) except
                # the old active ones: welcomes ran only there.
                for gid in active:
                    put(server_block(gid), f"plugins.{pid}.enabled", "on")
                lines.append(f"behavior.active_guild_ids -> servers.<id>."
                             f"plugins.{pid}.enabled: on for each of "
                             f"{len(active)} active server(s); other "
                             f"servers stay off (all_servers)")
        for key, val in block.items():
            if key in ("enabled", "per_guild"):
                continue
            skey, spec = man.spec_for(key)
            if spec is None:
                continue  # never read by the old loader either
            if isinstance(val, dict) and ("default" in val
                                          or "per_guild" in val):
                if "default" in val:
                    _put_plugin(out.setdefault("everywhere", {}), pid, skey,
                                spec, val["default"])
                for g, v in (val.get("per_guild") or {}).items():
                    _put_plugin(server_block(g), pid, skey, spec, v)
            else:
                _put_plugin(out.setdefault("everywhere", {}), pid, skey,
                            spec, val)
            if skey != key:
                lines.append(f"{label}.{key} -> plugins.{pid}.{skey}")
        for g, params in (block.get("per_guild") or {}).items():
            if not isinstance(params, dict):
                continue
            for key, v in params.items():
                skey, spec = man.spec_for(key)
                if spec is not None:
                    _put_plugin(server_block(g), pid, skey, spec, v)
        if name != pid:
            lines.append(f"plugins.{name} -> plugins.{pid}")
    if active and not any("active_guild_ids" in ln for ln in lines):
        lines.append("behavior.active_guild_ids: dropped (only welcomes "
                     "used it, and they are off)")
    if servers:
        out["servers"] = servers
    if isinstance(raw.get("testing"), dict):
        out["testing"] = copy.deepcopy(raw["testing"])
    return out, aliases, respond, lines


def _put_plugin(block, pid, key, spec, value):
    if spec.legacy == "allowed_user_ids" or key == "who" \
            and spec.type == "people" and isinstance(value, list) \
            and not value:
        # The old allowed_user_ids: [] meant the owners.
        if isinstance(value, list) and not value:
            value = "owners"
        elif isinstance(value, list):
            value = [str(v) for v in value]
    if value is None and spec.type != "any" and not spec.none:
        return  # null meant "the default" for these
    if value is None and spec.type == "number":
        value = "none"
    block.setdefault("plugins", {}).setdefault(pid, {})[key] = value


# ---------------------------------------------------------------------------
# Parsing a new-layout dict into layer values
# ---------------------------------------------------------------------------
class _Parser:
    def __init__(self, file_label, manifests, old=False):
        self.file = file_label
        self.manifests = manifests
        self.old = old
        self.errors = []
        self.values = {}       # (path, key) -> value
        self.labels = {}       # server/channel id -> name label

    def err(self, where, msg):
        self.errors.append(f"{self.file}: {where}: {msg}")

    def spec(self, key):
        if key in CORE:
            return CORE[key]
        if key.startswith("plugins."):
            parts = key.split(".", 2)
            if len(parts) == 3 and parts[1] in self.manifests:
                return self.manifests[parts[1]].specs.get(parts[2])
        return None

    def set(self, path, key, value, where):
        spec = self.spec(key)
        kind = _scope_kind(path)
        if spec is None:
            if key.startswith("plugins."):
                parts = key.split(".", 2)
                if len(parts) < 3:
                    self.err(where, "expected plugins.<plugin>.<setting>")
                elif parts[1] not in self.manifests:
                    self.err(where, f"unknown plugin \"{parts[1]}\"; "
                             f"installed: "
                             f"{', '.join(sorted(self.manifests)) or 'none'}")
                else:
                    m = self.manifests[parts[1]]
                    self.err(where, f"plugin {parts[1]} has no setting "
                             f"\"{parts[2]}\"; valid: "
                             f"{', '.join(k for k, s in m.specs.items() if not s.hidden)}")
            else:
                group = key.split(".")[0]
                valid = sorted(k for k in CORE if k.startswith(group + "."))
                self.err(where, "unknown setting" + (
                    f"; valid in {group}: {', '.join(valid)}" if valid
                    else ""))
            return
        allowed = spec.scopes
        if kind == "all_servers" and not (key == "who.replies"
                                          or key.endswith(".enabled")):
            allowed = ()
        if kind not in allowed:
            where_ok = ", ".join(s for s in allowed if not (
                s == "all_servers" and not (key == "who.replies"
                                            or key.endswith(".enabled"))))
            self.err(where, f"cannot be set at {kind} scope (it can be "
                     f"set in: {where_ok or 'nowhere'})")
            return
        ok, v = normalize(spec, value)
        if not ok:
            self.err(where, f"{v}; valid: {spec.valid_text()}")
            return
        self.values[(path, key)] = v

    def scope_block(self, path, block, where):
        if block is None:
            return
        if not isinstance(block, dict):
            self.err(where, "expected a block of settings")
            return
        kind = _scope_kind(path)
        for top, sub in block.items():
            w = f"{where}.{top}"
            if top == "name" and kind in ("server", "channel"):
                self.labels[path[-1]] = str(sub)
            elif top == "channels" and kind == "server":
                if not isinstance(sub, dict):
                    self.err(w, "expected channel ids, each with a block")
                    continue
                for cid, cblock in sub.items():
                    cid_s = _id_str(cid)
                    cw = f'{where}.channels."{cid}"'
                    if cid_s is None:
                        self.err(cw, "expected a Discord channel or "
                                 "category id (digits) as the key; put a "
                                 "name in `name:` inside")
                        continue
                    self.scope_block(path + ("channels", cid_s), cblock, cw)
            elif top == "plugins":
                if not isinstance(sub, dict):
                    self.err(w, "expected plugin blocks")
                    continue
                for pid, pblock in sub.items():
                    pw = f"{w}.{pid}"
                    if not isinstance(pblock, dict):
                        self.err(pw, "expected a block like "
                                 "{enabled: on}")
                        continue
                    for k, v in pblock.items():
                        if kind == "channel" and k != "enabled":
                            self.err(f"{pw}.{k}", "a channel or category "
                                     "may set only replies, notes and "
                                     "plugins.<plugin>.enabled")
                            continue
                        self.set(path, f"plugins.{pid}.{k}", v, f"{pw}.{k}")
            elif kind == "channel":
                if top in CHANNEL_SHORT:
                    self.set(path, CHANNEL_SHORT[top], sub, w)
                else:
                    self.err(w, "a channel or category may set only "
                             "replies, notes and plugins.<plugin>.enabled")
            elif top in CORE_GROUPS:
                if not isinstance(sub, dict):
                    self.err(w, f"expected settings under {top}:")
                    continue
                for k, v in sub.items():
                    self.set(path, f"{top}.{k}", v, f"{w}.{k}")
            else:
                self.err(w, "unknown section; valid: "
                         + ", ".join(CORE_GROUPS + ("plugins",)
                                     + (("name", "channels")
                                        if kind == "server" else ())))

    def top_value(self, key, value, where):
        spec = TOP[key]
        ok, v = normalize(spec, value)
        if not ok:
            self.err(where, f"{v}; valid: {spec.valid_text()}")
            return
        self.values[(("top",), key)] = v


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def overlay_path_for(config_path):
    return os.path.join(os.path.dirname(os.path.abspath(config_path)),
                        OVERLAY_NAME)


def _read_yaml(path):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        data = yaml.safe_load(f)
    return data if data is not None else {}


class Settings:
    """A loaded, validated configuration (immutable; reload to change)."""

    def __init__(self):
        self.path = None
        self.overlay_path = None
        self.layout = "new"
        self.notice = None
        self.raw = {}
        self.mapping = []
        self.manifests = {}
        self.file_values = {}
        self.overlay = {}         # (path, key) -> entry dict (active ones)
        self.overlay_doc = {}
        self.retire = []          # [(keystr, entry, reason)]
        self.aliases = {}         # name-or-id -> True (old layout only)
        self.labels = {}
        self.respond_to = {"mention", "reply_to_bot"}
        self.engine = {}
        self.owners = []
        self.persona = DEFAULT_PERSONA
        self.token_from = DEFAULT_TOKEN_FILE
        self.legacy_defaults = False
        self._cache = {}

    # -- top level ---------------------------------------------------------
    def top(self, key):
        v = self._layer(("top",), key)
        return v[1] if v[0] else TOP[key].default

    @property
    def presence_status(self):
        return self.top("presence.status")

    @property
    def presence_kind(self):
        return self.top("presence.kind")

    @property
    def presence_enabled(self):
        return self.top("presence.enabled")

    @property
    def updates_notify(self):
        return self.top("updates.notify")

    def token_sources(self):
        """[("env", NAME) | ("file", path)], tried in order."""
        srcs = self.token_from
        if isinstance(srcs, str):
            srcs = [srcs]
        out = []
        for s in srcs:
            s = str(s)
            if s.startswith("env:"):
                out.append(("env", s[4:].strip()))
            else:
                out.append(("file", s))
        return out

    def guild_ids(self):
        return [str(g) for g in self.engine.get("guild_ids") or []]

    # -- layers ------------------------------------------------------------
    def _layer(self, path, key):
        """(set?, value, source) for one layer."""
        if path == ("builtin",):
            spec = self.spec(key)
            default = spec.default if spec is not None else None
            if key == "who.replies" and self.legacy_defaults:
                default = "when-addressed"
            return True, copy.deepcopy(default), ("built-in",)
        if path == ("builtin_all_servers",):
            if key.startswith("plugins.") and key.endswith(".enabled"):
                pid = key.split(".")[1]
                m = self.manifests.get(pid)
                if m is not None and not m.safe_in_new_servers:
                    return True, False, ("built-in", "all_servers")
            return False, None, None
        ov = self.overlay.get((path, key))
        if ov is not None:
            if ov.get("value") == "inherit":
                return False, None, None
            return True, ov["_norm"], ("discord", ov.get("by"), ov.get("at"))
        if (path, key) in self.file_values:
            return True, self.file_values[(path, key)], ("file",)
        return False, None, None

    def spec(self, key):
        if key in CORE:
            return CORE[key]
        if key in TOP:
            return TOP[key]
        if key.startswith("plugins."):
            parts = key.split(".", 2)
            if len(parts) == 3 and parts[1] in self.manifests:
                return self.manifests[parts[1]].specs.get(parts[2])
        return None

    def keys(self):
        out = list(CORE)
        for pid, m in sorted(self.manifests.items()):
            out += [f"plugins.{pid}.{k}" for k in m.specs]
        return out

    def chain(self, scope):
        """The layer paths for a scope, least specific first, each with a
        display name."""
        out = [(("builtin",), "built-in"), (("everywhere",), "everywhere")]
        if scope.kind == "everywhere":
            return out
        if scope.kind == "dms":
            return out + [(("dms",), "dms")]
        out += [(("builtin_all_servers",), "all_servers (built-in)"),
                (("all_servers",), "all_servers")]
        if scope.kind == "all_servers" or not scope.guild_id:
            return out
        gid = scope.guild_id
        for alias in sorted(self.aliases):
            if alias in (gid, scope.guild_name):
                out.append((("alias", alias), f'server "{alias}"'))
        out.append((("servers", gid), f"server {gid}"))
        if scope.category_id and scope.category_id != scope.channel_id:
            out.append((("servers", gid, "channels", scope.category_id),
                        f"category {scope.category_id}"))
        if scope.channel_id:
            out.append((("servers", gid, "channels", scope.channel_id),
                        f"channel {scope.channel_id}"))
        return out

    def resolve(self, scope):
        """{key: value} for every core and plugin key at this scope."""
        ck = scope.key()
        if ck in self._cache:
            return self._cache[ck]
        chain = self.chain(scope)
        out = {}
        for key in self.keys():
            out[key] = self._resolve_key(chain, key)[0]
        self._cache[ck] = out
        return out

    def get(self, key, scope):
        return self.resolve(scope)[key]

    def _resolve_key(self, chain, key):
        """(value, [(name, set?, value, source)] for every layer)."""
        rows = []
        for path, name in chain:
            s, v, src = self._layer(path, key)
            rows.append((name, path, s, v, src))
        if key == "style.notes":
            base, extra = "", []
            for name, path, s, v, src in rows:
                if not s:
                    continue
                if _scope_kind(path) == "channel":
                    if v:
                        extra.append(v)
                else:
                    base = v
            return "\n".join(p for p in [base] + extra if p), rows
        value = None
        for name, path, s, v, src in reversed(rows):
            if s:
                value = v
                break
        return value, rows

    def explain(self, scope):
        """[(key, value, source, chain rows)] for every key at scope.
        source: "built-in", "config.yaml", "Discord change by <id> at
        <time>", or "inherited from <scope> (...)"."""
        chain = self.chain(scope)
        here = chain[-1][0]
        out = []
        for key in self.keys():
            value, rows = self._resolve_key(chain, key)
            src = "built-in"
            for name, path, s, v, srctag in reversed(rows):
                if not s:
                    continue
                desc = _source_text(srctag)
                if path == here or path[0] == "builtin":
                    src = desc
                else:
                    src = f"inherited from {name} ({desc})"
                if key == "style.notes":
                    parts = [r for r in rows if r[2] and r[3]
                             and _scope_kind(r[1]) == "channel"]
                    base = [r for r in rows if r[2]
                            and _scope_kind(r[1]) != "channel"]
                    if parts and value:
                        names = ([base[-1][0]] if base and base[-1][3]
                                 and base[-1][1][0] != "builtin" else [])
                        src = "combined: " + ", ".join(
                            names + [r[0] for r in parts])
                break
            out.append((key, value, src, rows))
        return out

    def layer(self, path, key):
        """(set?, value, source) for one layer path (see chain())."""
        return self._layer(path, key)

    def trace(self, scope, key):
        """(value, [(name, path, set?, value, source)] for every layer of
        scope's chain, least specific first)."""
        return self._resolve_key(self.chain(scope), key)

    def value_without(self, scope, key, path):
        """What key would resolve to at scope if the layer `path` did not
        set it (what a reset to inherit there would give)."""
        if path == ("top",):
            return copy.deepcopy(TOP[key].default)
        chain = [(p, n) for p, n in self.chain(scope) if p != path]
        return self._resolve_key(chain, key)[0]

    def cap_layers(self, key, scope):
        """[(counter id, cap)] for every layer along the chain that sets a
        cap (not none) for key: each counts only what happens in its own
        scope (everywhere = all of it, "server:<id>", "dms")."""
        out = []
        for path, name in self.chain(scope):
            if path[0] in ("builtin", "builtin_all_servers"):
                continue
            s, v, _src = self._layer(path, key)
            if not s or v is None:
                continue
            if path[0] == "everywhere":
                cid = "everywhere"
            elif path[0] == "dms":
                cid = "dms"
            elif path[0] in ("servers", "alias"):
                cid = f"server:{scope.guild_id}"
            else:
                continue
            out.append((cid, v))
        return out

    @staticmethod
    def counter_ids(scope):
        """The counters a counted event in this scope adds to."""
        if scope.kind == "dms":
            return ["everywhere", "dms"]
        if scope.guild_id:
            return ["everywhere", f"server:{scope.guild_id}"]
        return ["everywhere"]

    def plugin_enabled(self, pid, scope):
        return bool(self.get(f"plugins.{pid}.enabled", scope))

    def plugin_values(self, pid, scope):
        m = self.manifests.get(pid)
        if m is None:
            return {}
        res = self.resolve(scope)
        return {k: res.get(f"plugins.{pid}.{k}") for k in m.specs}

    def plugin_enabled_anywhere(self, pid):
        """True when some layer turns the plugin on (it must be loaded)."""
        key = f"plugins.{pid}.enabled"
        m = self.manifests.get(pid)
        if m is not None and m.specs["enabled"].default:
            return True
        for (path, k), v in self.file_values.items():
            if k == key and v and (path, k) not in self.overlay:
                return True
        for (path, k), e in self.overlay.items():
            if k == key and e.get("value") != "inherit" and e["_norm"]:
                return True
        return False

    def servers_listed(self):
        """{server id: label} for every server the files mention."""
        out = {}
        for (path, _k) in list(self.file_values) + list(self.overlay):
            if path[0] == "servers":
                out.setdefault(path[1], self.labels.get(path[1]))
        return out

    def channels_listed(self, gid):
        out = set()
        for (path, _k) in list(self.file_values) + list(self.overlay):
            if path[0] == "servers" and path[1] == gid and len(path) > 2:
                out.add(path[3])
        return sorted(out)

    # -- overlay retirements -----------------------------------------------
    def commit_retirements(self):
        """Apply the conflict rule for real: move retired overlay entries
        into history (rewriting config.discord.yaml) and return one owner
        notice per entry. Only the monitor calls this; readers (the CLI)
        just see the file winning."""
        if not self.retire:
            return []
        doc = _read_overlay_doc(self.overlay_path)
        values = doc.get("values") or {}
        notices = []
        now = _now()
        for keystr, entry, reason in self.retire:
            if keystr not in values:
                continue
            values.pop(keystr, None)
            doc.setdefault("history", []).append({
                "key": keystr, "from": entry.get("value"), "to": None,
                "by": entry.get("by"), "at": now, "retired": reason})
            notices.append(_retire_notice(keystr, entry, reason))
        doc["values"] = values
        doc["history"] = (doc.get("history") or [])[-HISTORY_CAP:]
        _write_overlay_doc(self.overlay_path, doc)
        self.retire = []
        return notices


def _source_text(src):
    if not src:
        return "not set"
    if src[0] == "built-in":
        return "built-in" if len(src) == 1 else "built-in for servers"
    if src[0] == "file":
        return "config.yaml"
    return f"Discord change by {src[1]} at {src[2]}"


def _retire_notice(keystr, entry, reason):
    if reason == "file changed":
        return (f"settings: you edited `{keystr}` in config.yaml after it "
                f"was changed in Discord (to {_show(entry.get('value'))} by "
                f"{entry.get('by')} at {entry.get('at')}). The file wins; "
                f"the Discord change is undone and kept in "
                f"{OVERLAY_NAME} history.")
    return (f"settings: the Discord change to `{keystr}` "
            f"({_show(entry.get('value'))}) no longer applies ({reason}); "
            f"it was moved to {OVERLAY_NAME} history.")


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_overlay_doc(path):
    try:
        doc = _read_yaml(path)
    except (OSError, yaml.YAMLError):
        return {"version": 1, "values": {}, "history": []}
    if not isinstance(doc, dict):
        doc = {}
    if not isinstance(doc.get("values"), dict):
        doc["values"] = {}
    if not isinstance(doc.get("history"), list):
        doc["history"] = []
    doc["version"] = 1
    return doc


OVERLAY_HEADER = (
    "# Written by muse-discord for settings changed from Discord. Safe to\n"
    "# delete: everything then follows config.yaml again. Each value names\n"
    "# the file's value it replaced (base); edit config.yaml at that spot\n"
    "# and the file wins again. `bin/settings.py fold` copies these into\n"
    "# config.yaml.\n")


def atomic_write_text(path, text):
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0o600
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(dir=d, prefix="." + os.path.basename(path)
                               + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), mode)
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_overlay_doc(path, doc):
    body = yaml.safe_dump({"version": 1, "values": doc.get("values") or {},
                           "history": doc.get("history") or []},
                          sort_keys=False, default_flow_style=False,
                          allow_unicode=True, width=1000)
    atomic_write_text(path, OVERLAY_HEADER + body)


def load(config_path, plugins_dir=None, overlay_path=None,
         legacy_defaults=False, text=None):
    """Load and validate config.yaml (+ its overlay). Raises SettingsError
    on an invalid hand-written file; overlay problems never raise (the
    entries are queued for retirement instead). text: parse this instead
    of reading config_path (migrate's check)."""
    st = Settings()
    st.path = config_path
    st.overlay_path = overlay_path or overlay_path_for(config_path)
    st.legacy_defaults = legacy_defaults
    if plugins_dir is None:
        plugins_dir = os.path.join(os.path.dirname(os.path.abspath(
            __file__)), "plugins")
    st.manifests = load_manifests(plugins_dir)
    label = os.path.basename(config_path)
    try:
        raw = yaml.safe_load(text) if text is not None \
            else _read_yaml(config_path)
    except yaml.YAMLError as e:
        raise SettingsError(f"{label}: not valid YAML ({e})")
    except OSError as e:
        raise SettingsError(f"{label}: unreadable ({e})")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SettingsError(f"{label}: expected settings (key: value "
                            f"lines), got {type(raw).__name__}")
    st.raw = raw
    st.layout = detect_layout(raw)
    data = raw
    if st.layout == "old":
        st.notice = NOTICE
        data, aliases, st.respond_to, st.mapping = map_old(raw,
                                                           st.manifests)
        label += " (old layout, mapped)"
    else:
        aliases = {}
    p = _Parser(label, st.manifests, old=st.layout == "old")
    # Server names the bot has seen (state.json), for "did you mean".
    p.known = lambda: _known_guilds(os.path.join(os.path.dirname(
        os.path.abspath(config_path)), "state.json"))
    _parse_new(p, data, aliases, st)
    if p.errors:
        raise SettingsError("\n".join(p.errors))
    st.file_values = p.values
    st.labels = p.labels
    st.aliases = {a: True for a in aliases}
    _apply_overlay(st)
    return st


def _parse_new(p, data, aliases, st):
    allowed = {"version", "owners", "persona", "token_from", "presence",
               "updates", "everywhere", "dms", "all_servers", "servers",
               "engine", "testing"}
    for k in data:
        if k not in allowed:
            p.err(str(k), "unknown top-level key; valid: "
                  + ", ".join(sorted(allowed - {"testing"})))
    v = data.get("version")
    if v is not None and v != 1:
        p.err("version", f"{v!r} is not supported; valid: 1")
    owners = data.get("owners")
    if owners is not None:
        if not isinstance(owners, list):
            p.err("owners", 'expected a list of user ids, like ["1234"]')
        else:
            for o in owners:
                if _id_str(o) is None:
                    p.err("owners", f"{_show(o)}: expected a Discord user "
                          f"id (digits)")
            st.owners = [str(o) for o in owners]
    persona = data.get("persona")
    if persona is not None:
        if not isinstance(persona, str) or not persona.strip():
            p.err("persona", "expected a file path, like persona.md")
        else:
            st.persona = persona.strip()
    tf = data.get("token_from")
    if tf is not None:
        items = tf if isinstance(tf, list) else [tf]
        if not items or not all(isinstance(i, str) and i.strip()
                                and i.strip() != "env:" for i in items):
            p.err("token_from", "expected env:NAME or a file path (never "
                  "the token itself)")
        elif any(re.fullmatch(r"[A-Za-z0-9_.\-]{50,}", i.strip())
                 for i in items):
            p.err("token_from", "this looks like a token itself; store the "
                  "token in a file (mode 600) and give its path")
        else:
            st.token_from = [i.strip() for i in items] \
                if isinstance(tf, list) else tf.strip()
    for sect, keys in (("presence", ("status", "kind", "enabled")),
                       ("updates", ("notify",))):
        block = data.get(sect)
        if block is None:
            continue
        if not isinstance(block, dict):
            p.err(sect, "expected a block")
            continue
        for k, val in block.items():
            if k not in keys or (k == "enabled" and False):
                p.err(f"{sect}.{k}", "unknown setting; valid: "
                      + ", ".join(x for x in keys if x != "enabled"))
                continue
            p.top_value(f"{sect}.{k}", val, f"{sect}.{k}")
    for scope in ("everywhere", "dms", "all_servers"):
        p.scope_block((scope,), data.get(scope), scope)
    servers = data.get("servers")
    if servers is not None:
        if not isinstance(servers, dict):
            p.err("servers", "expected server ids, each with a block")
        else:
            for sid, block in servers.items():
                sid_s = _id_str(sid)
                where = f'servers."{sid}"'
                if sid_s is None:
                    if p.old:
                        sid_s = str(sid)
                    else:
                        ids = [g for g, n in p.known().items()
                               if n == str(sid)]
                        hint = (f'; did you mean "{ids[0]}" ({sid})?'
                                if len(ids) == 1 else "")
                        p.err(where, "expected a Discord server id (digits) "
                              "as the key; put the server's name in "
                              "`name:` inside its block" + hint)
                        continue
                p.scope_block(("servers", sid_s), block, where)
    for alias, block in aliases.items():
        p.scope_block(("alias", alias), block, f'servers."{alias}"')
    eng = data.get("engine")
    if eng is not None:
        if not isinstance(eng, dict):
            p.err("engine", "expected a block")
        else:
            if not p.old:
                for k, sub in eng.items():
                    if k not in ENGINE_KEYS:
                        p.err(f"engine.{k}", "unknown engine setting; "
                              "valid: " + ", ".join(ENGINE_KEYS))
                    elif isinstance(ENGINE_KEYS[k], set):
                        if not isinstance(sub, dict):
                            p.err(f"engine.{k}", "expected a block")
                            continue
                        for kk in sub:
                            if kk not in ENGINE_KEYS[k]:
                                p.err(f"engine.{k}.{kk}", "unknown; valid: "
                                      + ", ".join(sorted(ENGINE_KEYS[k])))
            gids = eng.get("guild_ids")
            if gids is not None and not isinstance(gids, list):
                p.err("engine.guild_ids", "expected a list of server ids")
            st.engine = eng


def _apply_overlay(st):
    try:
        doc = _read_yaml(st.overlay_path)
    except (OSError, yaml.YAMLError) as e:
        st.overlay_doc = {}
        st.retire = []
        st.overlay_error = f"{OVERLAY_NAME} unreadable ({e}); ignored"
        return
    st.overlay_error = None
    if not isinstance(doc, dict):
        doc = {}
    st.overlay_doc = doc
    values = doc.get("values") or {}
    if not isinstance(values, dict):
        values = {}
    for keystr, entry in values.items():
        if not isinstance(entry, dict) or "value" not in entry:
            st.retire.append((str(keystr), entry if isinstance(entry, dict)
                              else {"value": entry}, "not a valid entry"))
            continue
        reason = None
        try:
            path, key = parse_key(keystr)
        except ValueError as e:
            path, key, reason = None, None, f"not a settings key ({e})"
        if reason is None:
            spec = st.spec(key)
            kind = _scope_kind(path)
            if spec is None:
                reason = "no such setting anymore"
            elif path == ("top",):
                if key not in TOP_DISCORD_EDITABLE:
                    reason = "not editable from Discord"
            elif kind not in spec.scopes or (
                    kind == "all_servers" and not (
                        key == "who.replies" or key.endswith(".enabled"))):
                reason = f"cannot be set at {kind} scope"
            elif path[0] == "servers" and (
                    _id_str(path[1]) is None
                    or (len(path) > 2 and _id_str(path[3]) is None)):
                reason = "expected Discord ids in the key"
            elif spec is not None and not spec.discord_editable:
                reason = "not editable from Discord"
        if reason is None and entry.get("value") != "inherit":
            ok, norm = normalize(spec, entry.get("value"))
            if not ok:
                reason = f"invalid value: {norm}"
            else:
                entry = dict(entry, _norm=norm)
        if reason is None:
            cur = st.file_values.get((path, key))
            base = entry.get("base")
            if base is not None or (path, key) in st.file_values:
                okb, nbase = normalize(spec, base) if base is not None \
                    else (True, None)
                if not okb or nbase != cur:
                    reason = "file changed"
        if reason is not None:
            st.retire.append((str(keystr), entry, reason))
            continue
        st.overlay[(path, key)] = entry


# ---------------------------------------------------------------------------
# Write API (overlay)
# ---------------------------------------------------------------------------
def check_settable(st, path, key):
    """The spec of key if Discord may set it at the scope path, else raise
    SettingsError saying why. The one rule set_value and /settings share."""
    keystr = format_key(path, key) if path else key
    spec = st.spec(key)
    if spec is None:
        raise SettingsError(f"{keystr}: unknown setting")
    kind = _scope_kind(path)
    if path == ("top",):
        if key not in TOP_DISCORD_EDITABLE:
            raise SettingsError(f"{keystr}: not editable from Discord")
    elif key in TOP:
        raise SettingsError(f"{keystr}: a bot-wide setting has no scope")
    elif kind not in spec.scopes or (kind == "all_servers" and not (
            key == "who.replies" or key.endswith(".enabled"))):
        raise SettingsError(f"{keystr}: cannot be set at {kind} scope")
    elif kind == "channel" and key.startswith("plugins.") \
            and not key.endswith(".enabled"):
        raise SettingsError(f"{keystr}: a channel or category may set only "
                            f"replies, notes and plugins.<plugin>.enabled")
    if path[0] in ("servers", "alias") and (_id_str(path[1]) is None or (
            len(path) > 2 and _id_str(path[3]) is None)):
        raise SettingsError(f"{keystr}: expected Discord ids")
    if not spec.discord_editable or spec.hidden:
        raise SettingsError(f"{keystr}: not editable from Discord")
    return spec


def set_value(config_path, keystr, value, by, plugins_dir=None):
    """Record a Discord-side change: overlay entry {value, base, by, at}
    plus a history line (last HISTORY_CAP kept). value "inherit" resets
    the key at that scope. Raises SettingsError for an invalid key/value.
    Returns the normalized display value written."""
    st = load(config_path, plugins_dir)
    try:
        path, key = parse_key(keystr)
    except ValueError as e:
        raise SettingsError(f"{keystr}: not a settings key ({e})")
    spec = check_settable(st, path, key)
    if value != "inherit":
        ok, norm = normalize(spec, value)
        if not ok:
            raise SettingsError(f"{keystr}: {norm}; valid: "
                                f"{spec.valid_text()}")
        value = _dumpable(spec, norm)
    keystr = format_key(path, key)
    base = st.file_values.get((path, key))
    s, before, _src = st._layer(path, key)
    doc = _read_overlay_doc(st.overlay_path)
    now = _now()
    doc["values"][keystr] = {"value": value,
                             "base": _dumpable(spec, base),
                             "by": str(by), "at": now}
    doc["history"].append({"key": keystr,
                           "from": _dumpable(spec, before) if s
                           else "inherit",
                           "to": value, "by": str(by), "at": now})
    doc["history"] = doc["history"][-HISTORY_CAP:]
    _write_overlay_doc(st.overlay_path, doc)
    return value


def reset(config_path, keystr, by, plugins_dir=None):
    """Act as if this scope did not set the key (writes inherit)."""
    return set_value(config_path, keystr, "inherit", by, plugins_dir)


# Which way a change goes: the most permissive value last. Used by
# /settings for the server-admin view (restrict-only) and risky changes.
_RANKS = {
    "who.replies": ("off", "when-addressed", "conversational"),
    "who.dms_from": ("owners", "shared-servers", "anyone"),
    "who.bots": ("ignore", "when-addressed"),
    "privacy.memory": ("nothing", "public-only", "everything"),
    "privacy.suspicious": ("ignore", "hold-for-review", "answer"),
}


def loosens(key, old, new, spec=None):
    """True when new lets the bot answer more people, share more, do more
    or reply more than old; False when it is the same or stricter; None
    when the two cannot be ordered (style, text, channels). Effective
    (resolved) values in, as Settings.resolve gives them."""
    if old == new:
        return False
    if key in _RANKS:
        r = _RANKS[key]
        if old in r and new in r:
            return r.index(new) > r.index(old)
        return None
    if spec is None:
        spec = CORE.get(key)
    if spec is None:
        return None
    if spec.type == "on-off":
        return bool(new) and not bool(old)
    if spec.type == "number" and spec.none:
        # A cap: none = no cap at all.
        if new is None:
            return old is not None
        if old is None:
            return False
        return new > old
    if spec.type == "people":
        if key.endswith(".ignore") or key == "who.ignore":
            if not isinstance(old, list) or not isinstance(new, list):
                return None
            return bool(set(old) - set(new))
        if new == "everyone":
            return old != "everyone"
        if old == "everyone":
            return False

        def people(v):
            return set() if v == "owners" else set(v or [])
        return bool(people(new) - people(old))
    return None


def scope_prefix(path):
    """The overlay key prefix of a scope path: "everywhere", "dms",
    "all_servers", 'servers."<id>"', 'servers."<id>".channels."<id>"'."""
    if path[0] == "servers":
        out = f'servers."{path[1]}"'
        return out + (f'.channels."{path[3]}"' if len(path) > 2 else "")
    return path[0]


def parse_scope_prefix(prefix):
    """The scope path of an overlay key prefix (inverse of scope_prefix)."""
    path, _key = parse_key(str(prefix) + ".x")
    if path == ("top",):
        raise ValueError("not a scope")
    return path


def _undo_paths(prefix):
    path = parse_scope_prefix(prefix)
    # The Everywhere page also holds the bot-wide settings.
    return {path, ("top",)} if path == ("everywhere",) else {path}


def _undo_pick(doc, paths):
    """(history index, key string, path, key, value to restore) for the
    newest change at one of paths that is still in effect and not already
    undone, or None."""
    hist = doc.get("history") or []
    values = doc.get("values") or {}
    for i in range(len(hist) - 1, -1, -1):
        h = hist[i]
        if not isinstance(h, dict) or "to" not in h or h.get("undo") \
                or h.get("undone") or h.get("retired") or h.get("folded"):
            continue
        try:
            path, key = parse_key(h.get("key"))
        except ValueError:
            continue
        if path not in paths:
            continue
        ks = format_key(path, key)
        cur = values.get(ks)
        if h["to"] == "inherit":
            live = cur is None or (isinstance(cur, dict)
                                   and cur.get("value") == "inherit")
        else:
            live = isinstance(cur, dict) and cur.get("value") == h["to"]
        if live:
            return i, ks, path, key, h.get("from", "inherit")
    return None


def undo_candidate(st, prefix):
    """(key string, path, key, value it would restore) for what undo()
    would revert at this scope, or None (nothing to undo)."""
    pick = _undo_pick(st.overlay_doc or {}, _undo_paths(prefix))
    return None if pick is None else pick[1:]


def undo(config_path, prefix, by, plugins_dir=None):
    """Revert the newest Discord change made at one scope (prefix as
    scope_prefix gives; "everywhere" includes the bot-wide settings) that
    is still in effect. Repeated calls step further back. Returns the key
    string reverted, or None when there is nothing to undo."""
    st = load(config_path, plugins_dir)
    doc = _read_overlay_doc(st.overlay_path)
    pick = _undo_pick(doc, _undo_paths(prefix))
    if pick is None:
        return None
    i, ks, path, key, target = pick
    spec = check_settable(st, path, key)
    in_file = (path, key) in st.file_values
    cur = st.file_values.get((path, key))
    now = _now()
    entry = {"value": "inherit", "base": _dumpable(spec, cur),
             "by": str(by), "at": now}
    if target == "inherit":
        if in_file:
            doc["values"][ks] = entry
        else:
            doc["values"].pop(ks, None)
    else:
        ok, norm = normalize(spec, target)
        if not ok:
            raise SettingsError(f"{ks}: cannot restore {_show(target)}: "
                                f"{norm}")
        if in_file and norm == cur:
            doc["values"].pop(ks, None)   # the file says it already
        else:
            doc["values"][ks] = dict(entry, value=_dumpable(spec, norm))
    hist = doc["history"]
    hist[i] = dict(hist[i], undone=True)
    hist.append({"key": ks, "from": hist[i]["to"], "to": target,
                 "by": str(by), "at": now, "undo": True})
    doc["history"] = hist[-HISTORY_CAP:]
    _write_overlay_doc(st.overlay_path, doc)
    return ks


def backup(path):
    """Copy path to path.bak-<UTC timestamp>; returns the backup path."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = f"{path}.bak-{stamp}"
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = f"{path}.bak-{stamp}-{n}"
    shutil.copy2(path, dest)
    return dest


def dump_config(data):
    return yaml.safe_dump(data, sort_keys=False, default_flow_style=False,
                          allow_unicode=True, width=78)


def fold(config_path, plugins_dir=None):
    """Copy the overlay's values into config.yaml and clear them. The file
    is rewritten (comments are lost); a timestamped backup is kept.
    Returns (backup path, [folded keys])."""
    st = load(config_path, plugins_dir)
    if st.layout == "old":
        raise SettingsError("config.yaml uses the old layout; run "
                            "`bin/settings.py migrate` first")
    if not st.overlay:
        return None, []
    data = copy.deepcopy(st.raw)
    folded = []
    for (path, key), entry in sorted(st.overlay.items(),
                                     key=lambda kv: format_key(*kv[0])):
        spec = st.spec(key)
        if path == ("top",):
            node_path = key.split(".")
        else:
            node_path = list(_node_prefix(path))
            if len(path) > 2:
                short = {v: k for k, v in CHANNEL_SHORT.items()}
                node_path += (short[key].split(".") if key in short
                              else key.split("."))
            else:
                node_path += key.split(".")
        if entry.get("value") == "inherit":
            _del_path(data, node_path)
        else:
            _set_path(data, node_path, _dumpable(spec, entry["_norm"]))
        folded.append(format_key(path, key))
    bak = backup(config_path)
    atomic_write_text(config_path, dump_config(data))
    doc = _read_overlay_doc(st.overlay_path)
    now = _now()
    for k in folded:
        doc["values"].pop(k, None)
        doc["history"].append({"key": k, "folded": now})
    doc["history"] = doc["history"][-HISTORY_CAP:]
    _write_overlay_doc(st.overlay_path, doc)
    return bak, folded


def _node_prefix(path):
    if path[0] == "servers":
        out = ["servers", path[1]]
        if len(path) > 2:
            out += ["channels", path[3]]
        return out
    return [path[0]]


def _set_path(d, parts, value):
    cur = d
    for p in parts[:-1]:
        if not isinstance(cur.get(p), dict):
            cur[p] = {}
        cur = cur[p]
    cur[parts[-1]] = value


def _del_path(d, parts):
    stack = []
    cur = d
    for p in parts[:-1]:
        if not isinstance(cur.get(p), dict):
            return
        stack.append((cur, p))
        cur = cur[p]
    cur.pop(parts[-1], None)
    for parent, p in reversed(stack):
        if parent[p] == {}:
            del parent[p]


# ---------------------------------------------------------------------------
# Migration: old layout -> new layout, refusing any behavior change
# ---------------------------------------------------------------------------
def _known_guilds(state_path):
    try:
        import json
        with open(state_path) as f:
            known = (json.load(f) or {}).get("known_guilds") or {}
        return {str(k): str(v) for k, v in known.items()}
    except (OSError, ValueError, AttributeError):
        return {}


def build_migrated(config_path, plugins_dir=None, state_path=None):
    """(new config text, mapping lines, notes) for an old-layout file, or
    raise SettingsError explaining why it cannot be converted as is."""
    st = load(config_path, plugins_dir)
    if st.layout != "old":
        raise SettingsError("config.yaml already uses the new layout")
    if st.respond_to != {"mention", "reply_to_bot"}:
        raise SettingsError(
            "behavior.respond_to is " + str(sorted(st.respond_to)) + "; the "
            "new layout always answers both an @mention and a reply to the "
            "bot. Remove behavior.respond_to (accepting that) and run "
            "migrate again.")
    data, aliases, _r, lines = map_old(st.raw, st.manifests)
    known = _known_guilds(state_path or os.path.join(
        os.path.dirname(os.path.abspath(config_path)), "state.json"))
    by_name = {}
    for gid, name in known.items():
        by_name.setdefault(name, []).append(gid)
    servers = data.setdefault("servers", {})
    for sid in list(servers):
        if _id_str(sid) is None:
            raise SettingsError(
                f'server "{sid}" (behavior.active_guild_ids) is not a '
                f'Discord server id; fix it in the old file first')
    for alias, block in aliases.items():
        ids = by_name.get(alias) or []
        if len(ids) != 1:
            raise SettingsError(
                f'plugins per_guild key "{alias}" is a server name the bot '
                f'cannot match to one id (from state.json); replace it '
                f'with the server id and run migrate again')
        gid = ids[0]
        tgt = servers.setdefault(gid, {})
        _merge_below(tgt, block)
        tgt.setdefault("name", alias)
        lines.append(f'per_guild "{alias}" -> servers."{gid}"')
    if not servers:
        data.pop("servers", None)
    for gid, block in list(servers.items()):
        name = block.pop("name", None) or known.get(gid)
        servers[gid] = ({"name": name} if name else {}) | block
    order = ["version", "owners", "persona", "token_from", "presence",
             "updates", "everywhere", "dms", "all_servers", "servers",
             "engine", "testing"]
    data["version"] = 1
    out = {k: data[k] for k in order if k in data}
    header = ("# muse-discord settings (converted from the old layout by "
              "bin/settings.py migrate).\n# See config.example.yaml for "
              "every setting and its default.\n")
    text = header + dump_config(out)
    notes = ["who.replies: conversational (new default; an intended "
             "change): after the bot answers someone, their next messages "
             "in that channel within 2 minutes count as addressed. Set "
             "all_servers.who.replies: when-addressed to keep the old "
             "behavior."]
    return text, lines, notes, st


def _merge_below(tgt, src):
    """Merge src into tgt, tgt's existing values winning (an id entry
    beats a name entry)."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(tgt.get(k), dict):
            _merge_below(tgt[k], v)
        elif k not in tgt:
            tgt[k] = copy.deepcopy(v)


def compare(old_st, new_st, known=None):
    """[(scope label, key, old, new)] for every resolved difference over
    every scope either file knows about (plus top-level values and the
    caps that apply). Exactly one difference is expected and filtered
    out: who.replies when-addressed -> conversational (old_st loaded
    with legacy_defaults)."""
    known = known or {}
    scopes = [Scope.everywhere(), Scope.dm(), Scope.server(None)]
    gids = set(old_st.servers_listed()) | set(new_st.servers_listed()) \
        | set(known)
    for gid in sorted(gids):
        name = known.get(gid)
        scopes.append(Scope.server(gid, name))
        chans = set(old_st.channels_listed(gid)) \
            | set(new_st.channels_listed(gid))
        for cid in sorted(chans):
            scopes.append(Scope.server(gid, name, channel_id=cid))
    diffs = []
    for sc in scopes:
        a, b = old_st.resolve(sc), new_st.resolve(sc)
        for key in sorted(set(a) | set(b)):
            if a.get(key) == b.get(key):
                continue
            if key == "who.replies" and a.get(key) == "when-addressed" \
                    and b.get(key) == "conversational":
                continue
            diffs.append((sc.label(), key, a.get(key), b.get(key)))
        for cap in ("limits.per_day", "limits.per_person_per_day",
                    "plugins.media.per_day"):
            if cap.startswith("plugins.") and "media" not in new_st.manifests:
                continue
            ca, cb = old_st.cap_layers(cap, sc), new_st.cap_layers(cap, sc)
            if ca != cb:
                diffs.append((sc.label(), cap + " (counted where set)",
                              ca, cb))
    for name, fa, fb in (
            ("owners", old_st.owners, new_st.owners),
            ("persona", old_st.persona, new_st.persona),
            ("token_from", old_st.token_sources(), new_st.token_sources()),
            ("engine", old_st.engine, new_st.engine),
            ("respond_to", sorted(old_st.respond_to),
             sorted(new_st.respond_to))):
        if fa != fb:
            diffs.append(("top level", name, fa, fb))
    for key in TOP:
        if old_st.top(key) != new_st.top(key):
            diffs.append(("top level", key, old_st.top(key),
                          new_st.top(key)))
    return diffs


def migrate(config_path, plugins_dir=None, state_path=None, write=True):
    """Convert an old-layout config.yaml. Verifies (by resolving both for
    every known scope) that nothing but the announced who.replies default
    changes, refusing otherwise. Writes a timestamped backup, then the new
    file. Returns (backup path or None, text, lines, notes)."""
    text, lines, notes, _st = build_migrated(config_path, plugins_dir,
                                             state_path)
    legacy = load(config_path, plugins_dir, legacy_defaults=True)
    try:
        new = load(config_path, plugins_dir, text=text)
    except SettingsError as e:
        raise SettingsError("the converted file would not validate (a bug; "
                            "nothing was written):\n" + str(e))
    known = _known_guilds(state_path or os.path.join(
        os.path.dirname(os.path.abspath(config_path)), "state.json"))
    diffs = compare(legacy, new, known)
    if diffs:
        rows = "\n".join(f"  {where}: {key}: {a!r} -> {b!r}"
                         for where, key, a, b in diffs[:40])
        raise SettingsError("refusing to migrate: the converted file would "
                            "behave differently:\n" + rows)
    bak = None
    if write:
        bak = backup(config_path)
        atomic_write_text(config_path, text)
    return bak, text, lines, notes


# ---------------------------------------------------------------------------
# Watching the files (live reload)
# ---------------------------------------------------------------------------
class Watcher:
    """Holds the current Settings and reloads when config.yaml or the
    overlay changes (an mtime/size/inode check per call; no reading).
    A reload that fails validation keeps the previous settings."""

    def __init__(self, config_path, plugins_dir=None):
        self.config_path = config_path
        self.plugins_dir = plugins_dir
        self.overlay_path = overlay_path_for(config_path)
        self._sig = self._signature()
        self.current = load(config_path, plugins_dir)
        self.error = None

    def _signature(self):
        out = []
        for p in (self.config_path, self.overlay_path):
            try:
                s = os.stat(p)
                out.append((s.st_mtime_ns, s.st_size, s.st_ino))
            except OSError:
                out.append(None)
        return tuple(out)

    def poll(self):
        """(changed?, error text or None)."""
        sig = self._signature()
        if sig == self._sig:
            return False, None
        self._sig = sig
        try:
            new = load(self.config_path, self.plugins_dir)
        except SettingsError as e:
            self.error = str(e)
            return False, self.error
        self.error = None
        self.current = new
        return True, None


# ---------------------------------------------------------------------------
# Command line for shell scripts: muse_settings.py [--config P] get NAME
# ---------------------------------------------------------------------------
def shell_value(st, name, pkg_dir):
    if name == "token_file":
        files = [p for k, p in st.token_sources() if k == "file"]
        return os.path.expanduser(files[0]) if files else ""
    if name == "token_env":
        envs = [p for k, p in st.token_sources() if k == "env"]
        return envs[0] if envs else ""
    if name == "presence_enabled":
        return "true" if st.presence_enabled else "false"
    if name == "updates_notify":
        return "true" if st.updates_notify else "false"
    if name == "persona_file":
        return os.path.join(pkg_dir, os.path.expanduser(st.persona))
    raise KeyError(name)


def _main(argv):
    pkg = os.path.dirname(os.path.abspath(__file__))
    cfg = os.environ.get("MUSE_DISCORD_CONFIG") or os.path.join(
        pkg, "config.yaml")
    if len(argv) > 2 and argv[1] == "--config":
        cfg = argv[2]
        argv = argv[:1] + argv[3:]
    if len(argv) != 3 or argv[1] != "get":
        print("usage: muse_settings.py [--config PATH] get NAME",
              file=sys.stderr)
        return 2
    try:
        st = load(cfg, os.path.join(pkg, "plugins"))
        print(shell_value(st, argv[2], pkg))
    except (SettingsError, KeyError) as e:
        print(f"settings: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
