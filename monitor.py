#!/usr/bin/env python3
"""
muse-discord monitor: the engine of the muse-discord connector.

Watches Discord for messages that address the bot (mentions, replies to
the bot, and DMs per policy), coalesces bursts, and appends work items to
reply_queue.json. A separate worker (an agent session; see PROTOCOL.md)
drafts replies into reply_outbox.json, and this process sends them.
Gateway websocket ingest provides realtime delivery; a periodic REST poll
repairs gaps. A reconciliation sweep plus a mention ledger make silent
message loss structurally difficult: every mention reaches a recorded
terminal state (replied, quarantined, or skipped with a reason).

This process is deterministic (no LLM) and never drafts text itself.

Security model:
- Discord message content is UNTRUSTED input.
  1. Injection-pattern pre-filter: suspicious messages are quarantined
     for the owner's review instead of being queued for auto-reply
     (privacy.suspicious).
  2. Rate limits: daily reply caps, per scope and per person, bound the
     token budget an attacker could burn by spamming mentions
     (limits.*). Who is answered at all is enforced too (who.*).
  3. Output validation: worker-drafted replies are length-capped and
     mass mentions (@everyone/@here) are neutralized before sending;
     sends only ever ping users (allowed_mentions), never roles.
  4. Send dedup: never send two replies to the same message (protects
     against overlapping worker runs).
  5. The bot token is read from where token_from points (a file or an
     env var) at startup and is never written to any state file. The
     worker never holds the token; it only reads and writes the
     queue/outbox files.
  6. never_share: a reply containing one of the owner's listed phrases
     is held for the owners instead of posted (see hold_reply).

Configuration: config.yaml next to this file (or --config PATH, or the
MUSE_DISCORD_CONFIG env var), plus config.discord.yaml (changes made
from Discord), both read through muse_settings.py. See
config.example.yaml for the schema; an old-layout file still works.
"""
import contextlib
import fcntl
import hashlib
import hmac
import importlib.util
import ipaddress
import json
import os
import queue as pyqueue
import random
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import urllib.parse
from datetime import datetime, timezone

try:
    import yaml  # noqa: F401 (muse_settings needs it)
except ImportError:
    print("PyYAML is required: pip install -r requirements.txt",
          file=sys.stderr)
    sys.exit(1)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
QUEUE_FILE = os.path.join(BASE_DIR, "reply_queue.json")
OUTBOX_FILE = os.path.join(BASE_DIR, "reply_outbox.json")
QUARANTINE_FILE = os.path.join(BASE_DIR, "quarantine.json")
LEDGER_FILE = os.path.join(BASE_DIR, "mention_ledger.json")
# Touched by the main loop; the watchdog restarts a monitor whose
# heartbeat goes stale (a live process whose loop is stuck).
HEARTBEAT_FILE = os.path.join(BASE_DIR, "monitor.heartbeat")
# The monitor and the worker both edit the queue and the outbox. Every
# read-modify-write of either file happens under an exclusive flock on
# this file (see shared_lock and PROTOCOL.md), so neither side can write
# back a stale copy over the other's changes.
LOCK_FILE = os.path.join(BASE_DIR, "reply.lock")
# Outbox items the monitor has taken and is sending. If it dies mid-send
# they are merged back on the next pass (part nonces keep resends safe).
OUTBOX_PROCESSING_FILE = os.path.join(BASE_DIR,
                                      "reply_outbox.processing.json")
# Malformed outbox items, set aside for the owner's review (last 100).
OUTBOX_REJECTED_FILE = os.path.join(BASE_DIR, "reply_outbox.rejected.json")
MEDIA_DIR = os.path.join(BASE_DIR, "media")
GENERATED_DIR = os.path.join(BASE_DIR, "generated")
PLUGINS_DIR = os.path.join(BASE_DIR, "plugins")

def _config_path():
    if len(sys.argv) > 2 and sys.argv[1] == "--config":
        return sys.argv[2]
    return os.environ.get("MUSE_DISCORD_CONFIG") or os.path.join(
        BASE_DIR, "config.yaml")


def _import_settings_module():
    """muse_settings.py from this package (next to monitor.py); a copy
    elsewhere on sys.path otherwise."""
    here = os.path.join(BASE_DIR, "muse_settings.py")
    if os.path.isfile(here):
        cached = sys.modules.get("muse_settings")
        if cached is not None and os.path.abspath(
                getattr(cached, "__file__", "") or "") == here:
            return cached
        spec = importlib.util.spec_from_file_location("muse_settings", here)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["muse_settings"] = mod
        spec.loader.exec_module(mod)
        return mod
    import muse_settings
    return muse_settings


# ---------------------------------------------------------------------------
# Settings (muse_settings.py): config.yaml in the new layout or the old
# one (mapped, with a one-line notice), plus config.discord.yaml, the
# overlay of changes made from Discord. User-facing behavior is resolved
# per scope (DM / server / category / channel) through settings(), which
# re-reads either file when it changes (an mtime check, no restart).
# Engine internals (timings, the reply size ceiling, guild_ids) are read
# once here, from engine: (or their old locations).
# ---------------------------------------------------------------------------
MS = _import_settings_module()
try:
    _SETTINGS = MS.Watcher(_config_path(), PLUGINS_DIR)
except MS.SettingsError as e:
    print(f"CONFIG ERROR: config.yaml is invalid; not starting.\n{e}",
          file=sys.stderr, flush=True)
    sys.exit(1)
_S0 = _SETTINGS.current


def _import_nl():
    """settings_nl.py (plain-language settings) from this package."""
    here = os.path.join(BASE_DIR, "settings_nl.py")
    cached = sys.modules.get("settings_nl")
    if cached is not None and os.path.abspath(
            getattr(cached, "__file__", "") or "") == here:
        return cached
    if os.path.isfile(here):
        spec = importlib.util.spec_from_file_location("settings_nl", here)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["settings_nl"] = mod
        spec.loader.exec_module(mod)
        return mod
    import settings_nl
    return settings_nl


NL = _import_nl()

# Anonymous daily usage counts (telemetry.py; README, "Privacy and usage
# stats"). Off with telemetry.usage_stats: off or MUSE_DISCORD_TELEMETRY=off.
TELEMETRY_FILE = os.path.join(BASE_DIR, "telemetry.json")
TELEMETRY_ID_FILE = os.path.join(BASE_DIR, "telemetry_id")
TELEMETRY_WAKES_FILE = os.path.join(BASE_DIR, "telemetry.wakes")


class _NoTelemetry:
    def __getattr__(self, name):
        return lambda *a, **kw: ""


def _import_telemetry():
    """A telemetry.Recorder; a stand-in that does nothing when
    telemetry.py cannot be loaded (it never stops the bot)."""
    try:
        spec = importlib.util.spec_from_file_location(
            "muse_telemetry", os.path.join(BASE_DIR, "telemetry.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.Recorder(lambda: (TELEMETRY_FILE, TELEMETRY_ID_FILE,
                                     TELEMETRY_WAKES_FILE),
                            ms=MS, pkg_dir=BASE_DIR)
    except Exception as e:
        print(f"Telemetry not loaded ({type(e).__name__}); usage counts "
              f"are off.", flush=True)
        return _NoTelemetry()


TM = _import_telemetry()
# The raw file (test-only keys such as testing: live here).
CFG = _S0.raw
if _S0.notice:
    print(_S0.notice, flush=True)
if _S0.retired_notice:
    print(_S0.retired_notice, flush=True)


# The interaction thread (/settings) reads settings and writes the
# overlay while the main loop reads them: the reload is serialized by
# _SETTINGS_LOCK, and every read-modify-write of config.discord.yaml in
# this process (a /settings change, an undo, the conflict rule retiring
# entries) holds _OVERLAY_LOCK. A reload noticed off the main thread
# leaves its follow-up work (owner list, plugin loading) to the main
# loop's next settings() call.
_SETTINGS_LOCK = threading.RLock()
_OVERLAY_LOCK = threading.RLock()
_SETTINGS_PENDING = []  # settings before reloads the main loop owes hooks


def settings():
    """The current settings, reloaded when config.yaml or
    config.discord.yaml changed since the last call (a stat per call; an
    invalid edit keeps the previous settings and is logged once).
    Thread-safe."""
    with _SETTINGS_LOCK:
        prev = _SETTINGS.current
        changed, err = _SETTINGS.poll()
        cur = _SETTINGS.current
        if changed:
            _SETTINGS_PENDING.append(prev)
        pending = None
        if _SETTINGS_PENDING \
                and threading.current_thread() is threading.main_thread():
            pending = _SETTINGS_PENDING[0]
            del _SETTINGS_PENDING[:]
    if err:
        print(f"Config re-read failed; keeping the previous settings:\n"
              f"{err}", flush=True)
    if pending is not None:
        _on_settings_changed(pending, cur)
    return cur


def _on_settings_changed(prev, cur):
    global OWNER_USER_IDS
    print("Settings reloaded (config.yaml or config.discord.yaml "
          "changed).", flush=True)
    if cur.notice and not prev.notice:
        print(cur.notice, flush=True)
    if cur.retired_notice and not prev.retired_notice:
        print(cur.retired_notice, flush=True)
    if sorted(cur.owners) != sorted(prev.owners):
        OWNER_USER_IDS = set(cur.owners)
        print(f"Owners are now: {', '.join(sorted(OWNER_USER_IDS)) or 'none'}",
              flush=True)
    try:
        load_plugins()  # a plugin turned on somewhere for the first time
    except Exception as e:
        print(f"Plugin load after reload failed: {e}", flush=True)


def _cfg_num(section, key, default, cast, name, minimum=None,
             maximum=None):
    """Resolve one numeric setting. Only a missing or null value means
    "use the default": an explicit 0 is honored wherever 0 is a real
    setting (the old `value or default` silently turned it back into the
    default). Where a value would busy-loop, divide by zero, or blank
    every reply, it is clamped into [minimum, maximum] with a warning
    printed once, here at import, instead of being silently replaced."""
    raw = section.get(key)
    if raw is None:
        return cast(default)
    try:
        val = cast(raw)
    except (TypeError, ValueError):
        print(f"WARNING: config {name}={raw!r} is not a number; using "
              f"the default {default}.", flush=True)
        return cast(default)
    if minimum is not None and val < minimum:
        print(f"WARNING: config {name}={raw!r} is below the minimum "
              f"{minimum}; using {minimum}.", flush=True)
        return cast(minimum)
    if maximum is not None and val > maximum:
        print(f"WARNING: config {name}={raw!r} is above the maximum "
              f"{maximum}; using {maximum}.", flush=True)
        return cast(maximum)
    return val


# --- Read once at import: identity and engine internals ---
OWNER_USER_IDS = set(_S0.owners)
GUILD_ALLOWLIST = set(_S0.guild_ids())
_OLD_BEHAVIOR = (CFG.get("behavior") or {}) if _S0.layout == "old" else {}
# An @mention and a reply to the bot always count; an old config could
# narrow that (behavior.respond_to), and still does.
RESPOND_TO = set(_S0.respond_to)
if "name" in (_OLD_BEHAVIOR.get("respond_to") or []) \
        or _OLD_BEHAVIOR.get("call_names"):
    print("WARNING: the bare-name trigger was removed; \"name\" in "
          "behavior.respond_to and behavior.call_names are ignored.",
          flush=True)
if _OLD_BEHAVIOR.get("max_bot_turns") is not None:
    print("WARNING: behavior.max_bot_turns was removed and is ignored; "
          "the worker ends bot conversations by judgment.", flush=True)
# Informational (the resolved everywhere / DM values at import); the
# pipeline resolves the settings per scope through settings().
_E0 = _S0.resolve(MS.Scope.everywhere())
_D0 = _S0.resolve(MS.Scope.dm())
BOTS_POLICY = "direct" if _E0["who.bots"] == "when-addressed" else "off"
DM_POLICY = "off" if _D0["who.replies"] == "off" else (
    "owner_only" if _D0["who.dms_from"] == "owners" else "open")
INJECTION_QUARANTINE = _E0["privacy.suspicious"] != "answer"
DAILY_REPLY_CAP = _E0["limits.per_day"]                  # None = off
PER_USER_DAILY_CAP = _E0["limits.per_person_per_day"]    # None = off
# The conversational follow-up window (who.replies: conversational).
FOLLOW_UP_WINDOW_SECS = 120
# Test-only (the e2e harness's driver bots stand in for people): these
# author ids count as human everywhere the monitor checks author.bot
# (see is_bot_author). Absent or empty = no effect.
TREAT_AS_HUMAN = {str(u) for u in
                  (CFG.get("testing") or {}).get("treat_as_human") or []}
if TREAT_AS_HUMAN:
    print(f"WARNING: testing.treat_as_human is set: {len(TREAT_AS_HUMAN)} "
          f"bot account(s) are treated as humans. Test servers only.",
          flush=True)


def is_bot_author(author):
    """True when a message author is a bot (Discord's author.bot), unless
    testing.treat_as_human lists its id."""
    author = author or {}
    return bool(author.get("bot")) \
        and str(author.get("id")) not in TREAT_AS_HUMAN


# 0 = no coalescing: every trigger is queued on the next tick.
_ENGINE = _S0.engine or {}
_ENG = "behavior" if _S0.layout == "old" else "engine"
COALESCE_SECS = _cfg_num(_ENGINE, "coalesce_seconds", 5, float,
                         f"{_ENG}.coalesce_seconds", minimum=0)
# Bots running this package send long replies as 2-4 parts with a 2-4s
# typing beat between them, so a bot's burst gets a longer window to
# arrive whole before its trigger is queued.
BOT_COALESCE_SECS = max(COALESCE_SECS, 15.0)
# Every part is cut to this length too, so it may not exceed Discord's
# 2000-character message limit; 0 would blank every reply.
MAX_REPLY_CHARS = _cfg_num(_ENGINE, "max_reply_chars", 1800, int,
                           f"{_ENG}.max_reply_chars", minimum=1,
                           maximum=2000)
# Each full poll is a request per channel: 0 would busy-loop the main
# loop and hammer Discord's per-IP rate limit.
POLL_INTERVAL = _cfg_num(_ENGINE, "rest_poll_seconds", 30, int,
                         "engine.rest_poll_seconds", minimum=10)
_SWEEP_CFG = _ENGINE.get("sweep") or {}
# Used as a modulus: 0 would abort every poll cycle midway.
SWEEP_EVERY_CYCLES = _cfg_num(_SWEEP_CFG, "every_cycles", 6, int,
                              "engine.sweep.every_cycles", minimum=1)
SWEEP_GRACE_SECS = _cfg_num(_SWEEP_CFG, "grace_seconds", 240, float,
                            "engine.sweep.grace_seconds", minimum=0)
# 0 = no mention is young enough to sweep: recovery is off, by choice.
SWEEP_MAX_AGE_SECS = _cfg_num(_SWEEP_CFG, "max_age_hours", 6, float,
                              "engine.sweep.max_age_hours",
                              minimum=0) * 3600
# 0 = never requeue; a lost mention is ledgered as skipped instead.
SWEEP_MAX_RECOVERIES = _cfg_num(_SWEEP_CFG, "max_recoveries", 2, int,
                                "engine.sweep.max_recoveries", minimum=0)
_BACKOFF_CFG = _ENGINE.get("backoff") or {}
# A zero backoff would answer a 429 with more requests at once.
BACKOFF_INITIAL_SECS = _cfg_num(_BACKOFF_CFG, "initial_seconds", 60, float,
                                "engine.backoff.initial_seconds",
                                minimum=1)
BACKOFF_MAX_SECS = _cfg_num(_BACKOFF_CFG, "max_seconds", 900, float,
                            "engine.backoff.max_seconds", minimum=1)
# Discovery probes every channel; 0 would re-run it on every poll.
CHANNEL_REFRESH_CYCLES = max(
    1, round(_cfg_num(_ENGINE, "discovery_refresh_minutes", 30, float,
                      "engine.discovery_refresh_minutes", minimum=1) * 60
             / POLL_INTERVAL))


def _load_token():
    # argv (manual/transient use) wins; then token_from in order: an
    # env:NAME source when that variable is set, a file when readable.
    # (An old config's bot.token_env + bot.token_file map to both, the
    # variable first.) The watchdog never passes a token.
    if len(sys.argv) > 1 and sys.argv[1] != "--config":
        return sys.argv[1]
    for kind, where in _S0.token_sources():
        if kind == "env":
            if where and os.environ.get(where):
                return os.environ[where].strip()
            continue
        try:
            with open(os.path.expanduser(where)) as f:
                return f.read().strip()
        except OSError:
            continue
    return None


TOKEN = _load_token()
if not TOKEN:
    print("No bot token: pass one as argv[1], or point token_from (old "
          "layout: bot.token_env / bot.token_file) at an environment "
          "variable or a file holding it (mode 600).",
          file=sys.stderr)
    sys.exit(1)

API = "https://discord.com/api/v10"

# The bot's own identity, fetched at startup. Used to recognize mentions,
# skip the bot's own messages, and compute its channel permissions.
BOT_ID = None
BOT_NAME = None
BOT_USER_ID = None

# Media handling: the monitor downloads images/GIFs from mentions so the
# reply worker can actually see them (the worker has no network access).
MAX_MEDIA_BYTES = 8 * 1024 * 1024
MAX_MEDIA_PER_MESSAGE = 6

# Bot's own user id (fetched at startup); used to skip its own DM messages.
BOT_USER_ID = None
MEDIA_TTL_SECS = 3600  # downloaded media older than this is swept each poll
# Embeds are only downloaded from known media hosts (attachments always
# come from the chat CDN). Keeps a malicious link unfurl from becoming a
# download from an arbitrary server. A host matches a suffix exactly or
# as a subdomain ("media.tenor.com"), never as a lookalike
# ("evil-tenor.com"); see embed_host_allowed and _fetch_addrs, which
# _fetch_complete applies to every URL and every redirect hop.
EMBED_MEDIA_HOST_SUFFIXES = ("discordapp.com", "discordapp.net", "tenor.com", "giphy" + ".com", "klipy.com")
FETCH_MAX_REDIRECTS = 3
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
CTYPE_EXT = {"image/gif": ".gif", "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}

# Video: download the clip, extract a few frames for the worker to view.
# The worker sees still frames only: no motion between them, no audio.
MAX_VIDEO_BYTES = 32 * 1024 * 1024
VIDEO_FRAME_COUNT = 5
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".avi", ".mkv")
FFMPEG_TIMEOUT = 60
# ffprobe/ffmpeg pick a demuxer from the downloaded bytes, not the
# extension, so a crafted HLS or concat playlist saved as a "video" would
# make them read other local files or fetch URLs. Only the expected
# container demuxers may run, and only on a plain local file.
FFMPEG_INPUT_GUARD = ["-format_whitelist", "mov,mp4,matroska,webm,avi",
                      "-protocol_whitelist", "file"]

# Documents: download text-ish attachments and extract their text so the
# worker can read them. The extracted text is inlined on the queue item
# (truncated); the worker never opens these files. Anything executable or
# archive-like stays unviewable.
MAX_DOC_BYTES = 4 * 1024 * 1024
MAX_DOCS_PER_MESSAGE = 3
DOC_TEXT_CHARS = 6000  # max extracted chars per document
TEXT_EXTS = (".txt", ".md", ".markdown", ".csv", ".json", ".jsonl", ".log",
             ".py", ".js", ".ts", ".tsx", ".jsx", ".rs", ".go", ".java",
             ".c", ".cpp", ".h", ".hpp", ".cs", ".rb", ".sh", ".yaml",
             ".yml", ".toml", ".xml", ".html", ".css", ".sql")
PDF_EXTS = (".pdf",)
DOCX_EXTS = (".docx",)
PDFTOTEXT_TIMEOUT = 30

# Generated media: when the media settings grant permission on a queue
# item (media_permission), the worker may write generated files into
# GENERATED_DIR/<the item's message_id>/; the send path attaches a reply's
# files from its own item's directory only (validated in
# _validate_gen_paths), so one item's image can never ride on another
# item's reply. Generation approvals are single-use, keyed by the
# triggering message id and bound to its channel, because the worker's
# outbox item does not carry the permission flag.
GENERATED_TTL_SECS = 3600
DISCORD_MAX_BYTES = 8 * 1024 * 1024  # unboosted server upload cap

# A message "is an owner command" if it opens with an action verb, optionally
# Owner-command gating is ID-based, not keyword-based: the monitor stamps
# owner_command purely from the author's Discord user ID. Deciding whether
# the owner's message is actually asking the bot to DO something is the
# worker's job (natural-language understanding), not a regex's.
# Strangers never get the flag, so the discord-api.py tool stays locked
# for them no matter what their message text claims.

# Conversation history: rolling window of recent messages kept per
# channel/DM so the reply worker drafts with context. Stored in state.json,
# attached to queue items (minus the triggering message itself).
HISTORY_PER_CHANNEL = 12
HISTORY_TEXT_CHARS = 300
HISTORY_ATTACHED = 8

# Burst coalescing: a trigger (mention / DM) waits
# behavior.coalesce_seconds before entering the reply queue, so
# rapid-fire follow-ups from the same author in the same channel fold
# into ONE reply instead of one reply per message. (COALESCE_SECS is
# resolved from config in the header block.)

# Channels and guilds are discovered live at startup and refreshed
# periodically: every channel/thread the bot can read, across every
# guild it belongs to (restricted to bot.guild_ids when configured).
# CHANNELS: {channel_id: {"name", "guild_id", "guild_name", "can_send"}}
#   can_send is the bot's effective Send Messages permission for the
#   channel (Send Messages in Threads for threads), or None when it
#   could not be computed; callers then attempt the send and rely on
#   the 403 safety net in process_outbox.
# GUILDS: {guild_id: {"name", "joined_at"}}
CHANNELS = {}
GUILDS = {}

# Channel display for logs, history context, and queue items. Guild-qualified
# so the worker can tell one server's #general from another's.
def channel_display(info):
    return "%s #%s" % (info.get("guild_name", "?"), info.get("name", "?"))

# Channel types the bot can meaningfully read: 0 = text, 5 = announcement.
READABLE_CHANNEL_TYPES = (0, 5)
# Forum (15) and media (16) channels hold no messages themselves, only
# threads (posts), whose send permission comes from them.
THREAD_PARENT_TYPES = (15, 16)

# Re-check for brand-new threads this often (in poll cycles). Threads are
# where forum posts live; a new post should be picked up within a few
# minutes, not at the next full channel refresh.
THREAD_REFRESH_CYCLES = 5


def get_guilds():
    """Return [(guild_id, guild_name)] for every guild the bot is in
    (restricted to bot.guild_ids when that config list is non-empty), or
    None when the list could not be fetched: a failure is not "in no
    guilds", and must not unmonitor everything."""
    try:
        out = [(g["id"], g.get("name") or g["id"])
               for g in api_get("/users/@me/guilds")]
    except Exception as e:
        print(f"Guild discovery failed: {e}", flush=True)
        return None
    if GUILD_ALLOWLIST:
        out = [g for g in out if g[0] in GUILD_ALLOWLIST]
    return out


# Discord permission bits used by the talk-scope computation.
PERM_ADMINISTRATOR = 1 << 3
PERM_SEND_MESSAGES = 1 << 11
PERM_SEND_IN_THREADS = 1 << 38


def _guild_base_perms(gid, gobj):
    """(base_permissions, member_role_ids, joined_at) for the bot in one
    guild. base_permissions is None (with no roles) when it cannot be
    determined (callers then treat send permission as unknown and rely on
    the send-time 403 safety net); joined_at is the bot's ISO join
    timestamp from the member object, or None when the fetch failed."""
    if not BOT_ID:
        return None, [], None
    try:
        member = api_get(f"/guilds/{gid}/members/{BOT_ID}")
    except Exception:
        return None, [], None
    joined_at = member.get("joined_at")
    roles = gobj.get("roles") or []
    if not roles:
        return None, [], joined_at
    member_roles = set(member.get("roles") or [])
    perms = 0
    for r in roles:
        if r.get("id") == gid or r.get("id") in member_roles:
            try:
                perms |= int(r.get("permissions") or 0)
            except (TypeError, ValueError):
                pass
    return perms, member_roles, joined_at


def _channel_perms(gid, base_perms, overwrites, member_role_ids):
    """Discord's permission resolution for one channel: start from the
    guild base permissions, then apply overwrites in order: the @everyone
    overwrite (the @everyone role id equals the guild id), then all role
    overwrites combined, then the bot's member-specific overwrite."""
    if base_perms & PERM_ADMINISTRATOR:
        return (1 << 62) - 1  # effectively every permission
    allow = deny = 0
    role_deny = role_allow = 0
    member_deny = member_allow = 0
    have_member = False
    for ow in overwrites or []:
        try:
            o_allow = int(ow.get("allow") or 0)
            o_deny = int(ow.get("deny") or 0)
        except (TypeError, ValueError):
            continue
        if ow.get("type") == 0:  # role overwrite
            if ow.get("id") == gid:
                deny, allow = o_deny, o_allow
            elif ow.get("id") in member_role_ids:
                role_deny |= o_deny
                role_allow |= o_allow
        elif ow.get("type") == 1 and ow.get("id") == BOT_ID:  # member
            member_deny, member_allow = o_deny, o_allow
            have_member = True
    perms = (base_perms & ~deny) | allow
    perms = (perms & ~role_deny) | role_allow
    if have_member:
        perms = (perms & ~member_deny) | member_allow
    return perms


def _no_access(e):
    """True when a read probe failed because the bot cannot read the
    channel (403 Missing Access, 404 Unknown Channel). Anything else (429,
    timeout, 5xx) is transient and says nothing about access."""
    return getattr(e, "code", None) in (403, 404)


def run_to_end(gen):
    """Run a sliceable REST generator (one that yields after each
    request, see "Sliced REST work") to completion; its return value."""
    while True:
        try:
            next(gen)
        except StopIteration as e:
            return e.value


def discover_channels(previous=None):
    """Return ({channel_id: info}, {guild_id: guild_info}).

    Covers every guild text channel the bot can actually read right now,
    across every guild it belongs to. Read access is probed per channel
    (private channels answer 403 and are skipped quietly). Each channel
    info carries can_send: the bot's effective Send Messages permission
    (for threads, Send Messages in Threads on the parent channel),
    computed from guild roles + channel overwrites; None when the
    computation was unavailable.

    previous is what a refresh monitored so far (None at startup). Only
    a 403/404 means "no access": when the guild list, a guild's channel
    list, or one channel's probe fails transiently (or REST is backing
    off from a 429), the previous entries are kept as they were instead
    of unmonitoring those channels (no poll, no sweep) until the next
    refresh."""
    return run_to_end(discover_channels_iter(previous))


def discover_channels_iter(previous=None):
    """discover_channels as a generator that yields after each request,
    so the main loop can spread a refresh over many ticks."""
    previous = previous or {}
    found = {}
    guilds_out = {}
    guild_list = get_guilds()
    yield
    if guild_list is None:
        return dict(previous), dict(GUILDS)
    for gid, gname in guild_list:
        try:
            # with_counts: approximate_member_count, for /settings.
            gobj = api_get(f"/guilds/{gid}?with_counts=true")
        except Exception as e:
            print(f"Guild fetch failed for {gname}: {e}", flush=True)
            gobj = {}
        yield
        base_perms, member_roles, joined_at = _guild_base_perms(gid, gobj)
        yield
        guilds_out[gid] = {
            "name": gname,
            "joined_at": joined_at,
        }
        count = gobj.get("approximate_member_count")
        if isinstance(count, int):
            _MEMBER_COUNTS[gid] = count
        try:
            channels = api_get(f"/guilds/{gid}/channels")
        except Exception as e:
            print(f"Channel list failed for {gname}: {e}", flush=True)
            found.update({c: i for c, i in previous.items()
                          if i.get("guild_id") == gid})
            continue  # (its _GUILD_CHANNELS entry stays as it was)
        finally:
            yield
        # Every channel of the guild, categories included, for the
        # /settings channel and category pickers (threads are not
        # listed: they follow their parent).
        _GUILD_CHANNELS[gid] = [
            {"id": ch["id"], "name": ch.get("name") or ch["id"],
             "type": ch.get("type"), "parent_id": ch.get("parent_id"),
             "position": ch.get("position")}
            for ch in channels if isinstance(ch, dict) and ch.get("id")]
        names_by_id = {ch["id"]: ch.get("name", "?") for ch in channels}
        for ch in channels:
            if ch.get("parent_id"):
                CHANNEL_CATEGORY[ch["id"]] = ch["parent_id"]
        perms_by_id = {}
        for ch in channels:
            ctype = ch.get("type")
            if ctype not in READABLE_CHANNEL_TYPES + THREAD_PARENT_TYPES:
                continue
            cid = ch["id"]
            perms = None
            if base_perms is not None:
                perms = _channel_perms(gid, base_perms,
                                       ch.get("permission_overwrites"),
                                       member_roles)
                perms_by_id[cid] = perms
            if ctype not in READABLE_CHANNEL_TYPES:
                continue  # forum/media: only their threads are read
            if rest_backing_off():
                if cid in previous:
                    found[cid] = previous[cid]
                continue  # probing on would only extend the rate limit
            try:
                api_get(f"/channels/{cid}/messages?limit=1")
            except Exception as e:
                if not _no_access(e) and cid in previous:
                    found[cid] = previous[cid]  # transient: keep it
                continue  # no read access; skip quietly
            finally:
                yield
            can_send = None
            if perms is not None:
                can_send = bool(perms & PERM_SEND_MESSAGES)
            found[cid] = {"name": ch["name"], "guild_id": gid,
                          "guild_name": gname, "can_send": can_send}
        # Threads (forum posts, text-channel threads) never appear in the
        # guild channel list, so they need their own discovery or the bot
        # is blind inside every post.
        found.update((yield from discover_threads_iter(
            gid, gname, names_by_id, found, perms_by_id, previous)))
    return found, guilds_out


def discover_threads_for_guild(gid, gname, names_by_id, known_ids,
                               perms_by_id=None, previous=None):
    """Return {thread_id: info} for active threads in one guild that the
    bot can read and whose ids aren't in known_ids yet.

    Forum posts and text-channel threads are separate channels that never
    appear in /guilds/{id}/channels, so they are discovered via the
    active-threads endpoint instead. The display name is parent-qualified
    ("help > <post title>") so logs and queue items show where a message
    actually lives. Archived threads are skipped on purpose: a reply
    un-archives a thread, and the next refresh picks it back up.

    A thread's send permission comes from its parent channel: the bot
    needs Send Messages in Threads there (Send Messages on the parent is
    also accepted; the send-time 403 net catches any residual mismatch).
    perms_by_id maps parent channel id -> effective permissions; when it
    is None (light thread-only refresh), can_send is left unknown.
    previous: as in discover_channels, kept on transient failures."""
    return run_to_end(discover_threads_iter(gid, gname, names_by_id,
                                            known_ids, perms_by_id,
                                            previous))


# Brand-new threads. A thread created since the previous successful
# thread listing of its guild (or since the monitor started) is seeded at
# its own id, not at its newest message: a mention posted in it before
# the gateway event was drained would otherwise sit at or below the
# watermark, be skipped by the drain as "poll already handled", and wait
# for the sweep. Its whole (short) history is read by the poll instead;
# the gateway/poll dedup (gw_seen, the watermark) keeps that to one
# reply. Older threads seen for the first time keep the usual seeding:
# their history is history.
MONITOR_STARTED = time.time()
NEW_THREAD_SLACK_SECS = 60   # clock skew between us and Discord
_THREADS_LISTED = {}         # guild id -> when its threads were last listed
_NEW_THREADS = set()         # thread ids to seed at their start


def discover_threads_iter(gid, gname, names_by_id, known_ids,
                          perms_by_id=None, previous=None):
    """discover_threads_for_guild as a generator that yields after each
    request (see discover_channels_iter)."""
    out = {}
    previous = previous or {}
    listed_at = time.time()
    try:
        resp = api_get(f"/guilds/{gid}/threads/active")
    except Exception as e:
        print(f"Thread list failed for {gname}: {e}", flush=True)
        return {t: i for t, i in previous.items()
                if i.get("guild_id") == gid and i.get("thread")
                and t not in known_ids}
    finally:
        yield
    since = _THREADS_LISTED.get(gid, MONITOR_STARTED) \
        - NEW_THREAD_SLACK_SECS
    _THREADS_LISTED[gid] = listed_at
    for t in resp.get("threads", []):
        tid = t["id"]
        if tid in known_ids or tid in out:
            continue
        if rest_backing_off():
            if tid in previous:
                out[tid] = previous[tid]
            continue
        try:
            api_get(f"/channels/{tid}/messages?limit=1")
        except Exception as e:
            if not _no_access(e) and tid in previous:
                out[tid] = previous[tid]  # transient: keep it
            continue  # no read access; skip quietly
        finally:
            yield
        can_send = None
        if perms_by_id is not None:
            parent_perms = perms_by_id.get(t.get("parent_id"))
            if parent_perms is not None:
                can_send = bool(parent_perms
                                & (PERM_SEND_IN_THREADS | PERM_SEND_MESSAGES))
        parent = names_by_id.get(t.get("parent_id"), "thread")
        out[tid] = {"name": f"{parent} > {t.get('name', 'thread')}",
                    "guild_id": gid, "guild_name": gname,
                    "can_send": can_send,
                    "thread": True, "parent_id": t.get("parent_id")}
        if tid not in previous and _snowflake_ts(tid) >= since:
            _NEW_THREADS.add(tid)
    return out


def now_snowflake():
    """A message id for "now": every later message is newer than it."""
    return str((int(time.time() * 1000) - 1420070400000) << 22)


def seed_channel_seen(cid, cname, state):
    """Mark a newly discovered channel's current messages as seen so its
    history is not treated as fresh mentions. Only a channel with no
    last_seen is seeded: one dropped and re-added by discovery, or an
    archived thread a new message brought back, keeps its watermark so
    the poll still reads what arrived in between. When the channel is
    empty or the read fails, the watermark is "now": the history stays
    seen and the next message is still new. A brand-new thread (see
    _NEW_THREADS) is seeded just below its own id instead, so the poll
    reads every message in it (a forum post's first message has the
    thread's id)."""
    brand_new = cid in _NEW_THREADS
    _NEW_THREADS.discard(cid)
    if cid in state["last_seen"]:
        return
    if brand_new and _is_snowflake(cid):
        state["last_seen"][cid] = str(int(cid) - 1)
        print(f"Tracking new thread #{cname} from its start.", flush=True)
        return
    try:
        msgs = api_get(f"/channels/{cid}/messages?limit=5")
    except Exception as e:
        print(f"Init error #{cname}: {e}", flush=True)
        msgs = None
    if msgs:
        newest = max(msgs, key=lambda m: int(m["id"]))
        state["last_seen"][cid] = newest["id"]
        print(f"Tracking new channel #{cname} from message "
              f"{newest['id']}.", flush=True)
    else:
        state["last_seen"][cid] = now_snowflake()
        print(f"Tracking new channel #{cname} from now.", flush=True)

# Prompt-injection / exfiltration patterns. A match means the message is
# quarantined for the owner's review instead of queued for auto-reply.
# Deliberately conservative: false positives just skip one auto-reply.
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+instructions",
    r"disregard\s+(all\s+)?(previous|prior|above)?\s*instructions",
    r"forget\s+(all\s+)?(previous|prior|above)?\s*instructions",
    r"\byour\s+system\s*prompt\b",
    r"(reveal|share|send|show|tell|give|leak|paste|print|post|drop|output).{0,40}\bsystem\s*prompt\b",
    r"\byou\s+are\s+now\b",
    r"\breveal\b.{0,50}\b(instructions|prompt|secrets?)\b",
    r"\bjailbreak\b",
    r"\bdeveloper\s+mode\b",
    r"pretend\s+(you'?re|you\s+are|to\s+be)\b",
    r"act\s+as\s+(a\s+)?(different|new|another)\b",
    r"output\s+(your|the)\s+(initial|original|system)\s+(prompt|instructions)",
    r"(read|cat|open|print)\s+~?/",
    r"MEMORY\.md",
    r"\.env\b",
    # Credential terms need solicitation context: the bare term
    # "api key" shows up constantly in legitimate dev talk, so it only
    # matches with a request verb aimed at the key, "your ... api key",
    # or an assignment.
    r"(reveal|share|send|show|tell|give|leak|paste|print|post|drop|output).{0,40}\bapi[\s_-]?key\b",
    r"\byour\b.{0,25}\bapi[\s_-]?key\b",
    r"\bapi[\s_-]?key\b\s*(is|=|:)",
]


def headers():
    return {
        "Authorization": f"Bot {TOKEN}",
        "User-Agent": "DiscordBot (muse-discord, 1.0)",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# REST rate-limit tracker (2026-10-15, per RATE_LIMIT_BACKOFF_SPEC.md).
# Discord 429s are per-IP (shared egress), and the old code answered every
# 429 with another ~40-request poll burst 30s later, keeping the limit
# tripped. Now any 429 from api_get/api_post records a backoff window
# (Retry-After honored, else exponential), and the main loop gates all
# REST-initiated work on it. The gateway path and outbox sends keep running.
# ---------------------------------------------------------------------------
_rl_until = 0.0
_rl_backoff_step = 0
_rl_storm_logged_until = 0.0


def rest_backing_off():
    return time.time() < _rl_until


def _note_rate_limit(exc):
    global _rl_until, _rl_backoff_step, _rl_storm_logged_until
    now = time.time()
    delay = None
    hdrs = getattr(exc, "headers", None)
    if hdrs is not None:
        try:
            delay = float(hdrs.get("Retry-After"))
        except (TypeError, ValueError):
            delay = None
    if delay is None:
        try:
            delay = float(json.loads(exc.read()).get("retry_after"))
        except Exception:
            delay = None
    if delay is None:
        delay = min(BACKOFF_MAX_SECS, BACKOFF_INITIAL_SECS * (2 ** _rl_backoff_step)) \
            + random.uniform(0, 5)
        _rl_backoff_step = min(_rl_backoff_step + 1, 5)
    delay = max(1.0, min(float(delay), 3600.0))
    if now + delay > _rl_until:
        _rl_until = now + delay
    if now >= _rl_storm_logged_until:
        print(f"RATE LIMITED: backing off REST for {delay:.0f}s.",
              flush=True)
        _rl_storm_logged_until = _rl_until


def _rl_note_clean_cycle():
    """Reset the exponential step after a poll cycle that stayed clean."""
    global _rl_backoff_step
    if not rest_backing_off():
        _rl_backoff_step = 0


def _api_call(fn):
    global _token_rejected
    try:
        return fn()
    except Exception as e:
        if getattr(e, "code", None) == 429:
            _note_rate_limit(e)
        elif getattr(e, "code", None) == 401:
            _token_rejected = "401 from the REST API"
        raise
    finally:
        # A call that returned (or failed) is progress: long but live
        # stretches (discovery, the sweep, an outbox backlog) never look
        # stuck to the watchdog; a call that hangs does.
        touch_heartbeat()


# ---------------------------------------------------------------------------
# Token rejection and liveness, for the watchdog. A reset token makes
# every REST call answer 401 and the gateway close with 4004; neither
# heals by retrying, and a monitor that keeps running deaf hides the
# problem from the watchdog (it only restarts dead processes). So the
# monitor prints the "TOKEN REJECTED" marker and exits; the watchdog
# greps for it and raises the token alert. The main loop also touches
# HEARTBEAT_FILE so the watchdog can tell a stuck loop from a live one.
# ---------------------------------------------------------------------------
EXIT_TOKEN_REJECTED = 3
_token_rejected = None  # set (to where) by a 401 on any REST call
_heartbeat_at = 0.0


def token_rejected_exit(where):
    print(f"TOKEN REJECTED: Discord refused the bot token ({where}); it "
          f"was probably reset in the developer portal. Exiting. Store a "
          f"fresh token and the watchdog starts the monitor again.",
          flush=True)
    sys.exit(EXIT_TOKEN_REJECTED)


def touch_heartbeat():
    """Mark the monitor as making progress (throttled to one touch per
    30s). Called from the main loop and after every REST call."""
    global _heartbeat_at
    now = time.time()
    if now - _heartbeat_at < 30:
        return
    _heartbeat_at = now
    try:
        with open(HEARTBEAT_FILE, "a"):
            pass
        os.utime(HEARTBEAT_FILE, None)
    except OSError:
        pass


# Every GET is a read on the main loop (poll, sweep, discovery, reply
# context), so it gets a shorter timeout than sends: a request a proxy
# leaves hanging must not stall the gateway drain, the burst flushes,
# and the outbox for 20s. Sends (api_post, uploads) keep their 20s+.
READ_TIMEOUT_SECS = 8


def api_get(path):
    req = urllib.request.Request(API + path, headers=headers())

    def _do():
        with urllib.request.urlopen(req, timeout=READ_TIMEOUT_SECS) as r:
            return json.load(r)
    return _api_call(_do)


def api_post(path, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(API + path, data=body, headers=headers())

    def _do():
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    return _api_call(_do)


def atomic_write(path, obj):
    # A unique temp file per write: with one fixed "<path>.tmp", two
    # writers (the monitor and the worker) could interleave on the same
    # temp file and rename each other's half-written JSON into place.
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        mode = 0o644
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                               prefix="." + os.path.basename(path) + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), mode)
            json.dump(obj, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# How long to wait for the shared lock before going ahead without it. The
# lock is only ever held for a quick re-read and write, so a holder past
# this is a hung process, and a hung worker tool must not stop the
# monitor from ingesting and sending.
LOCK_WAIT_SECS = 20


@contextlib.contextmanager
def shared_lock():
    """Hold the exclusive flock on LOCK_FILE: the lock both the monitor
    and the worker take around every read-modify-write of the queue and
    the outbox. Inside it, re-read the file, apply only your own change,
    and write it back atomically; never write a copy read earlier. Not
    reentrant: never nest it. Never hold it across network calls."""
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.time() + LOCK_WAIT_SECS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    print(f"WARNING: {os.path.basename(LOCK_FILE)} held "
                          f"for over {LOCK_WAIT_SECS}s; going ahead "
                          f"without it.", flush=True)
                    break
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the flock


def load_queue():
    """The queue as it is on disk now (a snapshot: writes are atomic
    renames, so reading needs no lock)."""
    if not os.path.exists(QUEUE_FILE):
        return []
    with open(QUEUE_FILE) as f:
        queue = json.load(f)
    return queue if isinstance(queue, list) else []


def _queue_apply(add=(), drop=None):
    """queue_edit's body, for callers already holding shared_lock."""
    queue = load_queue()
    kept = [q for q in queue
            if not (drop and isinstance(q, dict) and drop(q))]
    have = {q.get("message_id") for q in kept if isinstance(q, dict)}
    added = 0
    for item in add:
        if item.get("message_id") in have:
            continue
        kept.append(item)
        have.add(item.get("message_id"))
        added += 1
    if added or len(kept) != len(queue):
        atomic_write(QUEUE_FILE, kept)
    return added


def queue_edit(add=(), drop=None):
    """Apply a delta to reply_queue.json under the shared lock: re-read
    it, remove the items drop(item) is true for, append the items in add
    whose message_id is not queued yet. The main loop used to write back
    a copy loaded before slow work (media downloads, the sweep), which
    resurrected items the worker had removed and deleted items appended
    meanwhile. Returns the number of items appended."""
    with shared_lock():
        return _queue_apply(add, drop)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            state = json.load(f)
    else:
        state = {}
    state.setdefault("last_seen", {})
    state.setdefault("replied_to", [])
    state.setdefault("rate", {"day": "", "day_count": 0, "users": {}})
    state.setdefault("history", {})
    # Leftovers from the removed burst breaker.
    for key in ("breaker", "owner_triggers", "trigger_texts"):
        state.pop(key, None)
    state.pop("bot_runs", None)  # leftover from the removed bot turn cap
    # Bursts staged by the removed name trigger: not direct address.
    state["pending"] = {k: v for k, v in state.get("pending", {}).items()
                        if v.get("kind") != "name_drop"}
    return state


def save_state(state):
    # Bound the replied_to list so state.json doesn't grow forever
    state["replied_to"] = state["replied_to"][-200:]
    atomic_write(STATE_FILE, state)


def is_suspicious(content):
    """Check message against injection patterns. Returns matched pattern or None."""
    lowered = content.lower()
    for pat in INJECTION_PATTERNS:
        if re.search(pat, lowered):
            return pat
    return False


def quarantine(msg, channel_name, matched_pattern, fragments=()):
    """Hold a message (and its burst fragments: a split injection must
    not have its second half swept back in alone) for the owner."""
    entry = {
        "message_id": msg["id"],
        "channel_id": msg.get("channel_id"),
        "channel_name": channel_name,
        "author": msg.get("author", {}).get("username"),
        "author_id": msg.get("author", {}).get("id"),
        "content": msg.get("content", ""),
        "matched_pattern": matched_pattern,
        "timestamp": msg.get("timestamp"),
        "quarantined_at": datetime.now(timezone.utc).isoformat(),
    }
    if fragments:
        entry["fragments"] = [{"message_id": f["id"],
                               "content": f.get("content", "")}
                              for f in fragments]
    q = []
    if os.path.exists(QUARANTINE_FILE):
        with open(QUARANTINE_FILE) as f:
            q = json.load(f)
    q.append(entry)
    atomic_write(QUARANTINE_FILE, q[-100:])  # keep last 100
    ledger_record_many([msg["id"]] + [f["id"] for f in fragments],
                       "quarantined", matched_pattern,
                       channel_id=msg.get("channel_id"),
                       author=entry["author"],
                       author_id=(msg.get("author") or {}).get("id"))
    print(f"Quarantined suspicious message from {entry['author']} in #{channel_name} "
          f"(matched: {matched_pattern}): {entry['content'][:80]}", flush=True)


# ---------------------------------------------------------------------------
# Mention ledger (2026-10-15): the durable record that makes silent drops
# impossible. Every mention/DM reaching a terminal state (replied,
# quarantined, skipped with a reason) is written here, and the
# reconciliation sweep (sweep_mentions) diffs recent channel mentions
# against it and requeues anything unaccounted for. Before this, a mention
# could die at any pipeline stage (storm, kill mid-cycle, enqueue failure,
# worker drop) with no trace and no recovery.
# ---------------------------------------------------------------------------
LEDGER_TERMINAL = ("replied", "quarantined", "skipped")


def _ledger_load():
    if os.path.exists(LEDGER_FILE):
        try:
            with open(LEDGER_FILE) as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("entries"), dict):
                return d
        except Exception:
            pass
    return {"entries": {}}


def _ledger_save(d):
    items = sorted(d["entries"].items(),
                   key=lambda kv: kv[1].get("updated", 0))
    d["entries"] = dict(items[-1000:])
    atomic_write(LEDGER_FILE, d)


def ledger_record(mid, status, reason="", channel_id=None, author=None,
                  recoveries=None, author_id=None, detail=None):
    ledger_record_many([mid], status, reason, channel_id, author,
                       recoveries, author_id, detail)


def ledger_record_many(mids, status, reason="", channel_id=None,
                       author=None, recoveries=None, author_id=None,
                       detail=None):
    """ledger_record for several ids in one write: a burst's trigger and
    its fragments always share one outcome. Each entry also keeps the
    author's id and a reason code (ledger_code; "Why no reply?" in
    /settings reads them), plus detail (which cap, which limit)."""
    mids = [m for m in mids if m]
    if not mids:
        return
    d = _ledger_load()
    code = ledger_code(status, reason)
    TM.ledger(code, [d["entries"].get(m) for m in mids])
    # Where it happened, as the monitor knows it now (a thread's parent
    # only for a thread): /settings' Why reads it later, when the
    # channel may be gone from discovery.
    info = CHANNELS.get(str(channel_id)) if channel_id else None
    where = {}
    if info and info.get("guild_id"):
        where = {"guild_id": info["guild_id"],
                 "parent_id": info.get("parent_id") if info.get("thread")
                 else None}
    elif channel_id and (channel_id in DM_CHANNELS
                         or channel_id in DM_CHANNEL_USER):
        where = {"dm": True}
    for mid in mids:
        ent = d["entries"].get(mid, {})
        ent.update(where)
        ent.update({"status": status, "reason": reason,
                    "channel_id": channel_id or ent.get("channel_id"),
                    "author": author or ent.get("author"),
                    "author_id": str(author_id) if author_id
                    else ent.get("author_id"),
                    "code": code, "updated": time.time()})
        if detail is not None:
            ent["detail"] = detail
        else:
            ent.pop("detail", None)
        if recoveries is not None:
            ent["recoveries"] = recoveries
        d["entries"][mid] = ent
        if author_id and author:
            remember_name(author_id, author)
    _ledger_save(d)


# Reason codes for the ledger (2026-10-19), from the reason text every
# caller already writes: what /settings' "Why no reply?" explains.
_LEDGER_CODES = (
    ("settings: who.replies is off", "replies-off"),
    ("settings: who.bots", "bots-ignore"),
    ("settings: not allowed by who.allow", "not-allowed"),
    ("settings: on who.ignore", "not-allowed"),
    ("settings: not in who.allow", "not-allowed"),
    ("settings: who.dms_from", "dm-not-allowed"),
    ("settings: not in who.dms_from", "dm-not-allowed"),
    ("settings: shares no server", "dm-not-allowed"),
    ("daily cap reached", "cap"),
    ("bot exchange limit", "bot-exchange"),
    ("suspicious, ignored", "suspicious-ignored"),
    ("predates bot join", "predates-join"),
    ("no send permission", "no-send"),
    ("403 missing access", "no-send"),
    ("enqueue failed", "internal"),
    ("sweep enqueue failed", "internal"),
    ("recovery cap reached", "internal"),
    ("no reply (worker's call)", "worker-skipped"),
    ("send failed", "send-failed"),
    ("held for owner review (never_share)", "never-share"),
    ("never_share hold discarded", "never-share"),
)


def ledger_code(status, reason=""):
    """A ledger entry's reason code: replied, queued, quarantined, or why
    a message was skipped (replies-off, dm-off, bots-ignore, not-allowed,
    dm-not-allowed, cap, bot-exchange, suspicious-ignored, predates-join,
    no-send, internal, worker-skipped, send-failed, never-share, other)."""
    if status in ("replied", "queued", "quarantined"):
        return status
    if status == "enqueue_failed":
        return "internal"
    reason = str(reason or "")
    if reason.startswith("settings: who.replies is off for DMs"):
        return "dm-off"
    for prefix, code in _LEDGER_CODES:
        if reason.startswith(prefix):
            return code
    return "other"


# Display names of people the monitor has seen (ledger authors), for
# /settings' "Remove Sam" (bounded; ids only otherwise).
_USER_NAMES = {}


def remember_name(uid, name):
    if uid and name:
        _USER_NAMES[str(uid)] = str(name)
        if len(_USER_NAMES) > 2000:
            for k in list(_USER_NAMES)[:500]:
                _USER_NAMES.pop(k, None)


def _ledger_terminal(mid):
    return _ledger_load()["entries"].get(mid, {}).get("status") \
        in LEDGER_TERMINAL


def _quarantine_ids():
    if os.path.exists(QUARANTINE_FILE):
        try:
            with open(QUARANTINE_FILE) as f:
                ids = set()
                for e in json.load(f):
                    ids.add(e.get("message_id"))
                    ids.update(fr.get("message_id")
                               for fr in e.get("fragments") or [])
                return ids
        except Exception:
            pass
    return set()


def _snowflake_ts(mid):
    try:
        return ((int(mid) >> 22) + 1420070400000) / 1000.0
    except Exception:
        return 0.0


def rate_allows(state, user_id, channel_id=None, scope=None):
    """Enforce limits.per_day and limits.per_person_per_day (none = off).
    Each cap counts what happens in the scope WHERE IT IS SET (everywhere
    = one total across DMs and every server, a server = that server, dms
    = all DMs), and every cap set along the message's scope applies.
    Owners are never capped or counted: these limits bound abuse and
    token spend from strangers, not the owner's own use of the bot.
    Counted at queue time, one per burst."""
    return rate_block(state, user_id, channel_id, scope) is None


def rate_block(state, user_id, channel_id=None, scope=None):
    """rate_allows, saying which cap stops it: None (allowed) or {"key",
    "counter" ("everywhere", "server:<id>", "dms": where the cap is set),
    "cap"}."""
    if user_id in OWNER_USER_IDS:
        return None
    if scope is None:
        scope = scope_of_channel(channel_id)
    st = settings()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rate = state["rate"]

    if rate["day"] != today:
        rate["day"] = today
        rate["day_count"] = 0
        rate["users"] = {}
        rate["scopes"] = {}
    scopes = rate.setdefault("scopes", {})

    for cid, cap in st.cap_layers("limits.per_day", scope):
        n = rate["day_count"] if cid == "everywhere" \
            else (scopes.get(cid) or {}).get("count", 0)
        if n >= cap:
            where = "" if cid == "everywhere" else f" for {cid}"
            print(f"Rate limit: daily cap ({cap}){where} reached, "
                  f"skipping.", flush=True)
            return {"key": "limits.per_day", "counter": cid, "cap": cap}

    day_start = datetime.now(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()
    for cid, cap in st.cap_layers("limits.per_person_per_day", scope):
        users = rate["users"] if cid == "everywhere" \
            else scopes.setdefault(cid, {}).setdefault("users", {})
        user_hits = [t for t in users.get(user_id, []) if t >= day_start]
        users[user_id] = user_hits
        if len(user_hits) >= cap:
            print(f"Rate limit: user {user_id} hit per-user daily cap"
                  f"{'' if cid == 'everywhere' else ' for ' + cid}, "
                  f"skipping.", flush=True)
            return {"key": "limits.per_person_per_day", "counter": cid,
                    "cap": cap}

    return None


def rate_record(state, user_id, scope=None):
    """Count one queued item against every counter of its scope (see
    rate_allows). Owners are not counted."""
    if user_id in OWNER_USER_IDS:
        return
    rate = state["rate"]
    now = time.time()
    rate["day_count"] += 1
    rate["users"].setdefault(user_id, []).append(now)
    for cid in MS.Settings.counter_ids(scope or MS.Scope.everywhere())[1:]:
        c = rate.setdefault("scopes", {}).setdefault(cid, {})
        c["count"] = c.get("count", 0) + 1
        c.setdefault("users", {}).setdefault(user_id, []).append(now)


def _iso_ts(iso):
    try:
        return datetime.fromisoformat(
            str(iso).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Join watermark. A message that predates the bot's arrival in a guild is
# never a trigger: not on catch-up after a restart, not in a channel
# discovered late, not on a sweep. The watermark is the bot's own
# joined_at for that guild (member object at discovery, GUILD_CREATE on
# the gateway), kept in state["guild_joined"] and enforced in
# classification and again at enqueue, independent of last_seen seeding.
# Backlog still reaches the worker as history on a post-join trigger.
# ---------------------------------------------------------------------------
def note_guild_joined(state, gid, joined_at):
    ts = _iso_ts(joined_at) if joined_at else None
    if ts is not None:
        state.setdefault("guild_joined", {})[gid] = ts


def join_watermark(state, gid):
    """The bot's join time in a guild. When Discord has not told us yet,
    fall back to the moment this monitor first saw the guild: later than
    the real join, so it can only err toward silence."""
    joined = state.get("guild_joined", {}).get(gid)
    if joined is not None:
        return joined
    first_seen = state.setdefault("guild_first_seen", {})
    if gid not in first_seen:
        first_seen[gid] = time.time()
        print(f"Join time unknown for guild {gid}; ignoring messages "
              f"older than now until Discord reports it.", flush=True)
    return first_seen[gid]


def predates_join(state, msg, gid):
    if not gid:
        return False  # DMs have no join
    return _snowflake_ts(msg.get("id")) < join_watermark(state, gid)


# ---------------------------------------------------------------------------
# Scopes and who gets answered. Every decision about a message resolves
# the settings for where it happened: a DM, or a server channel (a thread
# uses its parent channel; a channel its category). A new server has no
# entry of its own and resolves through all_servers, which ships careful
# (plugins not marked safe_in_new_servers stay off).
# When the bot joins a new server, the owners get a DM summary.
# ---------------------------------------------------------------------------
_FORCE_DISCOVERY = False  # set when the gateway reports a new guild
# channel id -> its category id (from discovery), for category settings.
CHANNEL_CATEGORY = {}


def scope_for(guild_id=None, channel_id=None, dm=False):
    """The settings scope of a message or event."""
    if dm:
        return MS.Scope.dm()
    info = CHANNELS.get(channel_id) or {}
    gid = guild_id or info.get("guild_id")
    chan = channel_id
    if info.get("thread") and info.get("parent_id"):
        chan = info["parent_id"]  # threads and forum posts: the parent
    cat = CHANNEL_CATEGORY.get(chan) \
        or (CHANNELS.get(chan) or {}).get("category_id")
    gname = (GUILDS.get(gid) or {}).get("name") or info.get("guild_name")
    return MS.Scope.server(gid, gname, chan, cat)


def scope_of_channel(channel_id):
    """The scope of a channel a reply goes to: a monitored server
    channel, else a DM."""
    if channel_id in CHANNELS:
        return scope_for(None, channel_id)
    return MS.Scope.dm()


# (guild, user) -> (role ids, fetched at): roles of authors whose messages
# carry no member object (the REST poll), for role entries in who.allow /
# who.ignore. Only fetched when such a list names a role.
_MEMBER_ROLES = {}
MEMBER_ROLES_TTL_SECS = 600


def member_roles(guild_id, user_id):
    if not guild_id or not user_id:
        return set()
    key = (str(guild_id), str(user_id))
    hit = _MEMBER_ROLES.get(key)
    if hit and time.time() - hit[1] < MEMBER_ROLES_TTL_SECS:
        return hit[0]
    if rest_backing_off():
        return hit[0] if hit else set()
    try:
        roles = set((api_get(f"/guilds/{guild_id}/members/{user_id}")
                     or {}).get("roles") or [])
    except Exception:
        roles = hit[0] if hit else set()
    _MEMBER_ROLES[key] = (roles, time.time())
    if len(_MEMBER_ROLES) > 2000:
        for k in sorted(_MEMBER_ROLES,
                        key=lambda k: _MEMBER_ROLES[k][1])[:500]:
            del _MEMBER_ROLES[k]
    return roles


def people_match(value, user_id, roles=lambda: set()):
    """True when user_id is in a people value: "everyone", "owners", or
    a list of user ids and "role:<id>" entries (roles() is only called
    when the list names a role)."""
    user_id = str(user_id or "")
    if value == "everyone":
        return True
    if value == "owners":
        return user_id in OWNER_USER_IDS
    if not isinstance(value, list) or not value:
        return False
    if user_id in value:
        return True
    want = {v[5:] for v in value if v.startswith("role:")}
    return bool(want and want & set(roles()))


def who_allows(msg, guild_id, resolved):
    """who.allow / who.ignore for a message's author (owners always
    pass; ignore wins over allow)."""
    author = msg.get("author", {}) or {}
    uid = str(author.get("id") or "")
    if uid in OWNER_USER_IDS:
        return True

    def roles():
        member = msg.get("member") or {}
        if "roles" in member:
            return set(member.get("roles") or [])
        return member_roles(guild_id, uid)
    if people_match(resolved["who.ignore"], uid, roles):
        return False
    return people_match(resolved["who.allow"], uid, roles)


# user id -> (shares a server with the bot?, checked at), for
# who.dms_from: shared-servers. Positive answers last an hour, negative
# ones ten minutes.
_SHARED_SERVER = {}


def shares_server(user_id):
    user_id = str(user_id)
    hit = _SHARED_SERVER.get(user_id)
    now = time.time()
    if hit and now - hit[1] < (3600 if hit[0] else 600):
        return hit[0]
    if rest_backing_off():
        return bool(hit and hit[0])
    for gid in list(GUILDS):
        try:
            api_get(f"/guilds/{gid}/members/{user_id}")
        except Exception as e:
            if getattr(e, "code", None) in (403, 404):
                continue  # not a member there
            return bool(hit and hit[0])  # transient: decide later
        _SHARED_SERVER[user_id] = (True, now)
        return True
    _SHARED_SERVER[user_id] = (False, now)
    return False


def dm_refusal(author):
    """Why a DM from author is not read and answered, or None when it is:
    who.replies (off = no DMs at all), who.dms_from (owners |
    shared-servers | anyone | a list of user ids: the owners plus those
    people), who.bots, and who.allow / who.ignore at the DM scope."""
    res = settings().resolve(MS.Scope.dm())
    if res["who.replies"] == "off":
        return "who.replies is off for DMs"
    uid = str((author or {}).get("id") or "")
    bot = is_bot_author(author)
    if uid in OWNER_USER_IDS and not bot:
        return None
    if bot and res["who.bots"] == "ignore":
        return "who.bots is ignore"
    policy = res["who.dms_from"]
    if policy == "owners":
        return "who.dms_from is owners"
    if isinstance(policy, list) and uid not in policy:
        return "not in who.dms_from"
    if people_match(res["who.ignore"], uid):
        return "on who.ignore"
    if not people_match(res["who.allow"], uid):
        return "not in who.allow"
    if policy == "shared-servers" and not shares_server(uid):
        return "shares no server with the bot"
    return None


def dm_sender_allowed(author):
    """Whether a DM from author is read and answered (see dm_refusal)."""
    return dm_refusal(author) is None


def retire_overlay_changes():
    """Discord-side settings changes that config.yaml has overtaken (the
    file was edited at that spot afterwards) or that no longer validate:
    move them to the overlay's history, one owner DM each (the conflict
    rule: the newest intent wins). Waits out a REST backoff so the DMs
    can land."""
    if not settings().retire or rest_backing_off():
        return 0
    with _OVERLAY_LOCK:
        # Re-checked under the lock: a /settings change written since
        # may have replaced a stale entry, which must not be retired.
        notes = settings().commit_retirements()
    for note in notes:
        print(note, flush=True)
        notify_owners(note)
    return len(notes)


def dms_enabled():
    return settings().get("who.replies", MS.Scope.dm()) != "off"


# ---------------------------------------------------------------------------
# Conversational follow-ups (who.replies: conversational, the default).
# After the bot's reply to a HUMAN is actually sent in a channel, that
# person's next messages there within FOLLOW_UP_WINDOW_SECS count as
# addressed, without an @mention or a Discord reply. Only that person;
# only the bot's own replies (re)open the window, so it cannot run on
# without the bot; a message that addresses someone else (a reply to
# another person's message, or an @mention of another user or a role)
# closes it and is not a follow-up. Threads are their own channels here.
# Follow-ups are coalesced, gated and ledgered like mentions, and the
# queue item says follow_up: true; the reconciliation sweep never
# requeues them (a missed follow-up is not a dropped mention).
# state["follow_up"]: {"<channel>:<user>": time the window opened}
# ---------------------------------------------------------------------------
def open_follow_up(state, channel_id, user_id):
    fu = state.setdefault("follow_up", {})
    now = time.time()
    fu[f"{channel_id}:{user_id}"] = now
    for k in [k for k, t in fu.items()
              if now - t > FOLLOW_UP_WINDOW_SECS]:
        del fu[k]


def addresses_someone_else(msg):
    """A Discord reply to someone else's message, or an @mention of
    another user or a role (the bot's own mention is direct address)."""
    if msg.get("message_reference"):
        rm = msg.get("referenced_message") or {}
        if (rm.get("author", {}) or {}).get("id") != BOT_ID:
            return True
    if any((m or {}).get("id") != BOT_ID for m in msg.get("mentions") or []):
        return True
    return bool(msg.get("mention_roles") or msg.get("mention_everyone"))


def follow_up_open(state, msg):
    """True when msg falls in its author's open follow-up window in its
    channel (and does not address someone else, which closes it)."""
    fu = (state or {}).get("follow_up") or {}
    author = msg.get("author", {}) or {}
    key = f"{msg.get('channel_id')}:{author.get('id')}"
    opened = fu.get(key)
    if opened is None or is_bot_author(author):
        return False
    ts = _snowflake_ts(msg.get("id")) or time.time()
    if ts > opened + FOLLOW_UP_WINDOW_SECS:
        fu.pop(key, None)
        return False
    if ts < opened - 2:
        return False  # written before the bot's reply landed
    if addresses_someone_else(msg):
        fu.pop(key, None)
        return False
    return True


def own_replies_in_run(history, msg):
    """How many of the bot's own messages are in the run of bot messages
    that ends just before msg (since a human last spoke in the channel):
    the bot's consecutive replies in a bot-to-bot exchange."""
    n = 0
    for h in reversed([h for h in history
                       if int(h["id"]) < int(msg["id"])]):
        if not h.get("bot"):
            break
        if h.get("author_id") == BOT_ID:
            n += 1
    return n


def notify_owners(text, components=None):
    """DM every owner (with message components, e.g. buttons, when
    given). Returns True when at least one DM landed (or there is nobody
    to tell). Never raises."""
    if not OWNER_USER_IDS:
        print(f"Owner notice (no owners configured): {text}", flush=True)
        return True
    payload = {"content": text[:1990], "allowed_mentions": {"parse": []}}
    if components:
        payload["components"] = components
    delivered = False
    for uid in sorted(OWNER_USER_IDS):
        if dm_owner(uid, payload):
            delivered = True
    return delivered


def dm_owner(uid, payload):
    """Send one message payload (content, components, ...) to one
    owner's DM channel. True when it landed. Never raises."""
    try:
        c = api_post("/users/@me/channels", {"recipient_id": uid})
        api_post(f"/channels/{c['id']}/messages", payload)
        return True
    except Exception as e:
        print(f"Owner DM to {uid} failed: {e}", flush=True)
        return False


def _join_summary(gid, ginfo, channels):
    name = ginfo.get("name") or gid
    visible = sorted(
        (info.get("name", "?"), info.get("can_send"))
        for info in channels.values()
        if info.get("guild_id") == gid and not info.get("thread"))
    shown = ", ".join("#" + n + (" (read-only)" if s is False else "")
                      for n, s in visible[:40])
    if len(visible) > 40:
        shown += f", and {len(visible) - 40} more"
    st = settings()
    res = st.resolve(MS.Scope.server(gid, name))
    replies = {"off": "I stay quiet there",
               "when-addressed": "I answer @mentions and replies to my "
                                 "messages there",
               "conversational": "I answer @mentions and replies to my "
                                 "messages there, and the follow-ups of "
                                 "someone I just answered"}
    on = (["image and video generation"] if res.get("media.enabled")
          else []) + [pid for pid in sorted(st.manifests)
                      if res.get(f"plugins.{pid}.enabled")]
    how = (f"set servers.\"{gid}\" in config.yaml (no restart needed)"
           if st.layout == "new" else
           f"add \"{gid}\" to behavior.active_guild_ids in config.yaml "
           f"(no restart needed)")
    return (f"I just joined **{name}** (server ID {gid}).\n"
            f"{replies.get(res['who.replies'], replies['when-addressed'])}"
            f". Extras on there: {', '.join(on) or 'none'}.\n"
            f"Channels I can see ({len(visible)}): {shown or 'none'}\n"
            f"To change what I do there, press "
            f"**Configure** (or use /settings), or {how}.")


def configure_button(gid):
    """The join notice's button: opens that server's /settings page
    (handled by settings_ui, custom_id mdset:open:server:<id>)."""
    return [{"type": 1, "components": [
        {"type": 2, "style": 1, "label": "Configure",
         "custom_id": f"{SETTINGS_PREFIX}:open:server:{gid}"}]}]


def sync_guilds(state, guilds, channels):
    """Record each guild's join watermark and greet newly joined guilds
    (observe mode, owner DM). The first run seeds the known set silently:
    the owner just set the bot up there."""
    if not guilds:
        return  # discovery failed; a partial view must not look like joins
    first_run = "known_guilds" not in state
    known = state.setdefault("known_guilds", {})
    notices = state.setdefault("join_notices", [])
    # Approximate member counts, for bin/settings.py's risk checks.
    state["guild_members"] = {gid: _MEMBER_COUNTS[gid] for gid in guilds
                              if isinstance(_MEMBER_COUNTS.get(gid), int)}
    for gid, ginfo in guilds.items():
        note_guild_joined(state, gid, ginfo.get("joined_at"))
        is_new = gid not in known
        known[gid] = ginfo.get("name") or gid
        if is_new and not first_run:
            print(f"Joined server {known[gid]} ({gid}): its settings "
                  f"come from all_servers until it has its own.",
                  flush=True)
            if gid not in notices:
                notices.append(gid)
    flush_join_notices(state, guilds, channels)
    try:
        warn_stale_labels()
    except Exception as e:
        print(f"Label check failed: {e}", flush=True)


_stale_labels = set()


def warn_stale_labels():
    """A `name:` label in config.yaml is only for people; say so once
    when Discord calls that server or channel something else now."""
    for sid, label in settings().labels.items():
        now = (GUILDS.get(sid) or {}).get("name") \
            or (CHANNELS.get(sid) or {}).get("name")
        if not now or not label or str(label).lstrip("#") == now \
                or (sid, label) in _stale_labels:
            continue
        _stale_labels.add((sid, label))
        print(f"Note: config.yaml labels {sid} \"{label}\", but Discord "
              f"calls it \"{now}\" now (the label is only for you; "
              f"the id is what counts).", flush=True)


def flush_join_notices(state, guilds, channels):
    """Send owed join summaries; anything undelivered is retried later."""
    notices = state.get("join_notices") or []
    if not notices or rest_backing_off():
        return
    left = []
    for gid in notices:
        if gid not in state.get("known_guilds", {}):
            continue  # removed again before the notice went out
        if not notify_owners(_join_summary(gid, guilds.get(gid) or {},
                                           channels),
                             components=configure_button(gid)):
            left.append(gid)
    state["join_notices"] = left


def on_guild_event(state, ev):
    """Gateway GUILD_CREATE / GUILD_DELETE. A create for an unknown guild
    is a join: record its watermark and request a discovery pass, which
    then sends the owner summary via sync_guilds."""
    global _FORCE_DISCOVERY
    gid = ev.get("id")
    if not gid or (GUILD_ALLOWLIST and gid not in GUILD_ALLOWLIST):
        return
    if ev.get("op") == "create":
        note_guild_joined(state, gid, ev.get("joined_at"))
        if isinstance(ev.get("member_count"), int):
            _MEMBER_COUNTS[gid] = ev["member_count"]
            publish_directory()
        known = state.get("known_guilds")
        if known is not None and gid not in known:
            _FORCE_DISCOVERY = True
    else:
        name = state.get("known_guilds", {}).pop(gid, None)
        state.get("guild_joined", {}).pop(gid, None)
        state.get("guild_first_seen", {}).pop(gid, None)
        print(f"Removed from server {name or gid}.", flush=True)


# ---------------------------------------------------------------------------
# Update notices and interactions. Versions are releases: semver tags
# vX.Y.Z. watchdog.sh records a new release in update_check.json
# (latest, installed, entries, action_needed; see PROTOCOL.md). The
# monitor DMs each owner about it once, with [Update now] / [Later]
# buttons. A click arrives as a gateway INTERACTION_CREATE and is
# answered on its own thread, never behind the main loop's REST work:
# Discord fails an interaction that is not acknowledged within 3 seconds.
# "Update now" writes update_request.json, which hooks/update-watch.sh
# turns into a wake of the owner's Muse in its main chat (it follows
# UPDATING.md to that exact release). "Later" only declines: a version is
# offered once, and a newer one is offered again.
# ---------------------------------------------------------------------------
UPDATE_CHECK_FILE = os.path.join(BASE_DIR, "update_check.json")
UPDATE_REQUEST_FILE = os.path.join(BASE_DIR, "update_request.json")
UPDATE_ENTRIES_SHOWN = 5
UPDATE_DM_TRIES = 10  # owed DMs retried once per cycle, then given up
# The newest announced version, mirrored from state.json for the
# interaction thread (only the main loop touches state).
_update_announced = {"version": None}
_updates_cfg = {"mtime": None, "check": _S0.updates_notify}


def updates_enabled():
    """updates.notify (old layout: updates.check), re-read when
    config.yaml or the overlay changes, so turning it off stops the DMs
    without a restart."""
    path = _config_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return _updates_cfg["check"]
    try:
        mtime = (mtime, os.path.getmtime(_SETTINGS.overlay_path))
    except OSError:
        pass  # no overlay: the file's mtime alone
    if mtime != _updates_cfg["mtime"]:
        _updates_cfg["mtime"] = mtime
        # An invalid edit keeps the last known settings.
        _updates_cfg["check"] = settings().updates_notify
    return _updates_cfg["check"]


def release_version(value):
    """(major, minor, patch) for a release tag "vX.Y.Z", else None
    (pre-releases such as v1.2.0-rc.1 and other tags are not releases)."""
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value)
    return tuple(int(x) for x in m.groups()) if m else None


def _read_json_dict(path):
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _version_installed(tag, timeout=5):
    """True when HEAD already contains the release tag (updated by hand
    before the notice went out, or since). False when git cannot tell,
    e.g. the tag was never fetched."""
    try:
        r = subprocess.run(["git", "-C", BASE_DIR, "merge-base",
                            "--is-ancestor", f"refs/tags/{tag}", "HEAD"],
                           capture_output=True, timeout=timeout)
    except Exception:
        return False
    return r.returncode == 0


def _entry_line(heading):
    """A CHANGELOG release heading "vX.Y.Z (YYYY-MM-DD): Title" as
    "vX.Y.Z: Title"; anything else as is."""
    m = re.fullmatch(r"(v\S+)\s+\([^)]*\):\s*(.+)", heading)
    return f"{m.group(1)}: {m.group(2)}" if m else heading


def update_notice_text(info):
    version = info["latest"]
    installed = info.get("installed")
    on = (f"you're on {installed}" if release_version(installed)
          else "you're on an unreleased version")
    lines = [f"**muse-discord {version} is out** ({on})."]
    entries = [_entry_line(str(e))[:150]
               for e in info.get("entries") or [] if e]
    if entries:
        lines.append("What's new:")
        lines += ["- " + e for e in entries[:UPDATE_ENTRIES_SHOWN]]
        if len(entries) > UPDATE_ENTRIES_SHOWN:
            lines.append(f"- +{len(entries) - UPDATE_ENTRIES_SHOWN} more")
    else:
        lines.append("What's new is in CHANGELOG.md.")
    if info.get("action_needed"):
        lines.append("Some changes need a step from you; your Muse walks "
                     "you through them.")
    lines.append(f"**Update now**: your Muse updates to {version} in its "
                 "main chat by following UPDATING.md, and asks before "
                 "touching anything you customized. **Later**: skip "
                 f"{version}; you are asked again when a newer version "
                 "ships.")
    return "\n".join(lines)[:1990]


def update_buttons(version):
    return [{"type": 1, "components": [
        {"type": 2, "style": 1, "label": "Update now",
         "custom_id": f"mdupd:now:{version}"},
        {"type": 2, "style": 2, "label": "Later",
         "custom_id": f"mdupd:later:{version}"}]}]


def announce_update(state):
    """DM the owners about the release in update_check.json, once per
    version: only a version newer than the last one announced is
    offered. Owners whose DM failed are retried each cycle."""
    upd = state.setdefault("update_notice", {})
    _update_announced["version"] = upd.get("version")
    if not updates_enabled():
        return
    info = _read_json_dict(UPDATE_CHECK_FILE)
    version = info.get("latest")
    new = release_version(version)
    last = release_version(upd.get("version"))
    if new and (last is None or new > last):
        upd.clear()
        upd.update(version=version, at=int(time.time()), owed=[], tries=0)
        if _version_installed(version):
            print(f"Update {version} is already installed; no notice.",
                  flush=True)
        elif not OWNER_USER_IDS:
            print(f"Update {version} available (no owners configured to "
                  f"tell).", flush=True)
        else:
            upd.update(owed=sorted(OWNER_USER_IDS),
                       text=update_notice_text(info))
            print(f"Update {version} available: DMing the owners.",
                  flush=True)
        _update_announced["version"] = version
    owed = upd.get("owed") or []
    if not owed or rest_backing_off():
        return
    payload = {"content": upd.get("text") or "", "allowed_mentions":
               {"parse": []}, "components": update_buttons(upd["version"])}
    upd["owed"] = [uid for uid in owed if not dm_owner(uid, payload)]
    upd["tries"] = upd.get("tries", 0) + 1
    if upd["owed"] and upd["tries"] >= UPDATE_DM_TRIES:
        print(f"Update notice undeliverable to {', '.join(upd['owed'])}; "
              f"giving up.", flush=True)
        upd["owed"] = []


def apply_record(state, rec):
    """Main-loop side of an interaction: persist what the interaction
    thread decided (it never writes state.json itself)."""
    if rec.get("kind") == "nl":   # plain-language settings buttons
        NL.apply_record(state, rec)
        if rec.get("journal"):
            # Recorded for good: the crash journal entry can go.
            save_state(state)
            nl_journal(drop=rec["journal"])
    elif rec.get("kind") == "update_choice":
        upd = state.setdefault("update_notice", {})
        if rec.get("version") == upd.get("version"):
            upd["choice"] = {k: rec.get(k) for k in ("action", "by", "at")}
        print(f"Owner {rec.get('by')} chose "
              f"{'Update now' if rec.get('action') == 'now' else 'Later'} "
              f"for update {rec.get('version')}.", flush=True)
    elif rec.get("kind") == "hold_choice":
        hid = rec.get("hold")
        hold = state.get("holds", {}).pop(hid, None)
        _holds_live.pop(hid, None)
        if not hold:
            return  # expired meanwhile
        where = hold.get("where") or "?"
        if rec.get("action") == "send":
            # Sent through the outbox like any reply (retries, the
            # attachment pipeline, settling); the hold id lets this one
            # item past the never_share check.
            state.setdefault("hold_released", {})[hid] = time.time()
            state.setdefault("released", []).append(
                dict(hold["item"], _hold=hid))
            print(f"Owner {rec.get('by')} chose Send anyway for the held "
                  f"reply in {where}.", flush=True)
        else:
            _settle(state, hold["item"], "skipped",
                    "never_share hold discarded by an owner")
            print(f"Owner {rec.get('by')} discarded the held reply in "
                  f"{where}.", flush=True)


# Interaction dispatch: one handler per key, the custom_id prefix (the
# text before the first ":") for buttons and other components, or
# "/<name>" for a slash command. A handler gets (interaction, the rest of
# the custom_id, the clicking user's id, record) and returns the callback
# response; record(dict) hands a state change to the main loop.
INTERACTION_HANDLERS = {}  # key -> (handler, owner_only)
INTERACTION_CALLBACK_TIMEOUT = 2.5  # inside Discord's 3s deadline


def register_interaction(key, handler, owner_only=True):
    INTERACTION_HANDLERS[key] = (handler, owner_only)


def ephemeral(text):
    """A callback response only the clicking user sees."""
    return {"type": 4, "data": {"content": text, "flags": 64,
                                "allowed_mentions": {"parse": []}}}


def interaction_user_id(ix):
    user = (ix.get("member") or {}).get("user") or ix.get("user") or {}
    return str(user.get("id") or "")


def dispatch_interaction(ix, record):
    data = ix.get("data") or {}
    if ix.get("type") in (3, 5):  # MESSAGE_COMPONENT, MODAL_SUBMIT
        key, _, rest = str(data.get("custom_id") or "").partition(":")
    elif ix.get("type") == 2:    # APPLICATION_COMMAND
        key, rest = "/" + str(data.get("name") or ""), ""
    else:
        return None              # nothing to answer (e.g. autocomplete)
    entry = INTERACTION_HANDLERS.get(key)
    if entry is None:
        return ephemeral("That control is no longer active.")
    handler, owner_only = entry
    uid = interaction_user_id(ix)
    if owner_only and uid not in OWNER_USER_IDS:
        return ephemeral("Only this bot's owners can use that.")
    return handler(ix, rest, uid, record)


def interaction_callback(ix, response):
    """POST the interaction response. Deliberately not api_post: the
    interaction token authorizes this call (a 401 is an expired
    interaction, not a revoked bot token), it is outside the REST
    rate-limit backoff, and it answers 204 with no body."""
    path = (f"/interactions/{urllib.parse.quote(str(ix['id']), safe='')}/"
            f"{urllib.parse.quote(str(ix['token']), safe='')}/callback")
    req = urllib.request.Request(
        API + path, data=json.dumps(response).encode(),
        headers={"User-Agent": "DiscordBot (muse-discord, 1.0)",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req,
                                timeout=INTERACTION_CALLBACK_TIMEOUT) as r:
        r.read()


def _interaction_thread_main(ix_inbox, record):
    """Answer interactions as they arrive (fed by the gateway thread)."""
    while True:
        ix = ix_inbox.get()
        try:
            response = dispatch_interaction(ix, record)
        except Exception as e:
            print(f"Interaction handler error: {e}", flush=True)
            response = ephemeral("Something went wrong; try again.")
        if response is None:
            continue
        try:
            interaction_callback(ix, response)
        except Exception as e:
            print(f"Interaction response failed: {e}", flush=True)


def on_update_button(ix, rest, uid, record):
    action, _, version = rest.partition(":")
    if action not in ("now", "later") or not release_version(version):
        return ephemeral("That control is no longer active.")
    announced = _update_announced["version"]
    if version != announced:
        if release_version(announced) \
                and release_version(version) < release_version(announced):
            return ephemeral(f"That update prompt is out of date: "
                             f"{announced} is out since. Use the newest "
                             f"prompt.")
        return ephemeral("That control is no longer active.")
    # Short timeout: the whole answer must land within Discord's 3s.
    if _version_installed(version, timeout=1):
        return ephemeral(f"{version} is already installed; nothing to "
                         f"do.")
    now = int(time.time())
    if action == "now":
        if _read_json_dict(UPDATE_REQUEST_FILE).get("version") != version:
            try:
                atomic_write(UPDATE_REQUEST_FILE, {
                    "version": version, "requested_at": now, "by": uid})
            except OSError as e:
                print(f"Could not write update_request.json: {e}",
                      flush=True)
                return ephemeral("I could not record the request; try "
                                 "again in a moment.")
        line = (f"**Update now** chosen. Your Muse is starting the update "
                f"to {version} in its main chat.")
    else:
        line = (f"**Later** chosen. {version} will not be offered again; "
                f"a newer version will be.")
    record({"kind": "update_choice", "version": version, "action": action,
            "by": uid, "at": now})
    original = (ix.get("message") or {}).get("content") or ""
    return {"type": 7, "data": {
        "content": original[:1990 - len(line)] + "\n\n" + line,
        "components": [], "allowed_mentions": {"parse": []}}}


register_interaction("mdupd", on_update_button)


# ---------------------------------------------------------------------------
# never_share backstop. Before a reply goes anywhere but an owner's own
# DM, its text is checked against privacy.never_share (case-insensitive,
# whole phrases). A hit is not sent: the draft is held in
# state["holds"] and the owners get it in a DM with [Send anyway] /
# [Discard] (owners only). Holding settles the queue item (it leaves the
# queue, so the wake hook never hands it to a worker again, and is
# ledgered as skipped, so the sweep leaves it alone). Send anyway puts
# it back through the outbox as the original reply; Discard and the
# 24-hour expiry drop it. The phrase list is never logged.
# ---------------------------------------------------------------------------
HOLD_TTL_SECS = 24 * 3600
# hold id -> held at, mirrored from state.json for the interaction thread
# (only the main loop touches state).
_holds_live = {}


def never_share_hit(state, item, qid, parts):
    """True when this reply must be held (see above)."""
    phrases = settings().get("privacy.never_share", MS.Scope.everywhere())
    if not phrases or not parts:
        return False
    hid = item.get("_hold")
    if hid and hid in state.get("hold_released", {}):
        return False  # an owner chose Send anyway
    meta = state.get("item_meta", {}).get(qid) if qid else None
    if meta:
        owner_dm = bool(meta.get("dm")) \
            and str(meta.get("author_id")) in OWNER_USER_IDS
    else:
        owner_dm = DM_CHANNEL_USER.get(item.get("channel_id")) \
            in OWNER_USER_IDS
    if owner_dm:
        return False
    text = "\n".join(parts)
    return any(re.search(r"(?<!\w)" + re.escape(str(p)) + r"(?!\w)", text,
                         re.IGNORECASE) for p in phrases if str(p).strip())


def hold_buttons(hid):
    return [{"type": 1, "components": [
        {"type": 2, "style": 4, "label": "Send anyway",
         "custom_id": f"mdhold:send:{hid}"},
        {"type": 2, "style": 2, "label": "Discard",
         "custom_id": f"mdhold:discard:{hid}"}]}]


def hold_reply(state, item, qid, parts):
    """Hold a reply that hit never_share and DM the owners about it."""
    hid = secrets.token_hex(6)
    where = item.get("channel_name") or item.get("channel_id")
    to = item.get("author") or "someone"
    state.setdefault("holds", {})[hid] = {
        "item": {k: v for k, v in item.items() if k != "_retry"},
        "at": time.time(), "qid": qid, "where": where, "to": to}
    _holds_live[hid] = time.time()
    _settle(state, item, "skipped", "held for owner review (never_share)")
    head = (f"**Held a reply** to {to} in {where}: it contains a phrase "
            f"from your never_share list, so nothing was posted.\n")
    tail = ("\n**Send anyway** posts it as written; **Discard** drops "
            "it. Undecided holds are discarded after 24 hours.")
    body = "> " + "\n".join(parts).replace("\n", "\n> ")
    room = 1990 - len(head) - len(tail)
    if len(body) > room:
        body = body[:room - 1] + "\u2026"
    payload = {"content": head + body + tail,
               "allowed_mentions": {"parse": []},
               "components": hold_buttons(hid)}
    delivered = [uid for uid in sorted(OWNER_USER_IDS)
                 if dm_owner(uid, payload)]
    print(f"Held a reply to {to} in {where}: it contains a never_share "
          f"phrase; {'owners DMed' if delivered else 'no owner reachable'}"
          f" (hold {hid}).", flush=True)


def expire_holds(state):
    now = time.time()
    holds = state.get("holds") or {}
    for hid, hold in list(holds.items()):
        if now - hold.get("at", 0) >= HOLD_TTL_SECS:
            del holds[hid]
            _holds_live.pop(hid, None)
            _settle(state, hold["item"], "skipped",
                    "never_share hold expired (24 h)")
            print(f"Held reply in {hold.get('where')} expired undecided; "
                  f"discarded.", flush=True)
    released = state.get("hold_released") or {}
    for hid in [h for h, t in released.items() if now - t > 2 * 86400]:
        del released[hid]


def on_hold_button(ix, rest, uid, record):
    action, _, hid = rest.partition(":")
    at = _holds_live.get(hid)
    if action not in ("send", "discard") or at is None \
            or time.time() - at >= HOLD_TTL_SECS:
        return ephemeral("That held reply is no longer waiting (already "
                         "sent or discarded, or it expired after 24 "
                         "hours).")
    _holds_live.pop(hid, None)  # one decision per hold
    record({"kind": "hold_choice", "hold": hid, "action": action,
            "by": uid, "at": int(time.time())})
    line = ("**Send anyway** chosen: posting it now." if action == "send"
            else "**Discarded**: it will not be posted.")
    original = (ix.get("message") or {}).get("content") or ""
    return {"type": 7, "data": {
        "content": original[:1990 - len(line)] + "\n\n" + line,
        "components": [], "allowed_mentions": {"parse": []}}}


register_interaction("mdhold", on_hold_button)


# ---------------------------------------------------------------------------
# /settings: the slash command and its pages. settings_ui.py (next to
# this file) renders every page: settings_ui.handle(interaction, ctx)
# returns the whole interaction callback body and never touches the
# network. This side registers the command, routes the command and every
# mdset:* component or modal to it on the interaction thread (inside
# Discord's 3s deadline, whatever the main loop is doing), and gives it a
# SettingsCtx: the current settings, the server and channel directory
# from discovery, and overlay writes. Permissions (owner / server admin /
# anyone else) are settings_ui's to check, on every interaction; so the
# routes are not owner-only here.
# ---------------------------------------------------------------------------
SETTINGS_PREFIX = "mdset"
SETTINGS_COMMAND = {
    "name": "settings",
    "type": 1,  # CHAT_INPUT
    "description": "See or change this bot's settings (owners; server "
                   "admins can make it quieter)",
    # Interaction context types: 0 GUILD, 1 BOT_DM (not 2, group DMs).
    "contexts": [0, 1],
    # Installation contexts: 0 GUILD_INSTALL only (no user installs).
    "integration_types": [0],
    # Visible to every member: owners need it in any server, server
    # admins in theirs; settings_ui refuses everyone else.
    "default_member_permissions": None,
}
# The message context command (right-click a message, Apps): why the
# bot did not answer it. Discord may refuse "?" in the name; then the
# second name is registered (WHY_NAMES[1]).
WHY_NAMES = ("Why no reply?", "Why no reply")
WHY_COMMAND = {
    "name": WHY_NAMES[0],
    "type": 3,  # MESSAGE
    "contexts": [0, 1],
    "integration_types": [0],
    # Manage Server: ordinary members don't see it in the menu (owners
    # use Why no reply? on the /settings pages anyway).
    "default_member_permissions": "32",
}
ADMIN_DIGEST_SECS = 60

# Directory for SettingsCtx, built on the main loop from discovery and
# the gateway, published as one immutable snapshot under _DIRECTORY_LOCK.
_MEMBER_COUNTS = {}   # guild id -> member count (approximate)
_GUILD_CHANNELS = {}  # guild id -> [{"id","name","type","parent_id",..}]
_DIRECTORY = {"guilds": [], "channels": {}, "where": {}}
_DIRECTORY_LOCK = threading.Lock()
# The same snapshot on disk, for bin/settings.py (places, names in its
# sentences): written atomically, only when it changed.
DIRECTORY_FILE = os.path.join(BASE_DIR, "directory.json")
_DIRECTORY_WRITTEN = {"body": None}


def publish_directory():
    """Snapshot GUILDS, member counts and channel lists for /settings
    (main loop only; the interaction thread reads the snapshot). A
    channel carries whether the bot can read it (readable: a text
    channel the monitor polls) and send there (can_send, None when
    unknown); where maps every monitored channel and thread to (guild,
    parent)."""
    guilds = []
    for gid, info in GUILDS.items():
        g = {"id": gid, "name": (info or {}).get("name") or gid,
             "member_count": _MEMBER_COUNTS.get(gid)}
        guilds.append(g)
    guilds.sort(key=lambda g: (str(g["name"]).lower(), g["id"]))
    channels = {}
    for gid in GUILDS:
        out = []
        for c in _GUILD_CHANNELS.get(gid) or []:
            c = dict(c)
            mon = CHANNELS.get(c.get("id"))
            if c.get("type") in READABLE_CHANNEL_TYPES:
                c["readable"] = mon is not None
            c["can_send"] = (mon or {}).get("can_send")
            out.append(c)
        channels[gid] = out
    where = {cid: (info.get("guild_id"),
                   info.get("parent_id") if info.get("thread") else None)
             for cid, info in CHANNELS.items()}
    global _DIRECTORY
    with _DIRECTORY_LOCK:
        _DIRECTORY = {"guilds": guilds, "channels": channels,
                      "where": where}
    body = {"guilds": guilds, "channels": channels}
    if body != _DIRECTORY_WRITTEN["body"]:
        try:
            atomic_write(DIRECTORY_FILE, dict(body, updated=int(time.time())))
            _DIRECTORY_WRITTEN["body"] = body
        except OSError as e:
            print(f"Could not write directory.json: {e}", flush=True)


# /settings' stash: what a confirm card, a preset preview or a failed
# modal is waiting on, in memory only (a restart forgets it: the page
# then says it expired). token -> (user id, payload, expires at).
_STASH = {}
_STASH_LOCK = threading.Lock()
STASH_MAX = 500


# Pending admin-change digests: guild id -> {"first": t, "changes": []}.
_ADMIN_CHANGES = {}
_ADMIN_LOCK = threading.Lock()


class SettingsCtx:
    """What settings_ui.handle gets: safe to use from the interaction
    thread. write/reset/undo raise muse_settings.SettingsError for an
    invalid key, value or scope."""

    def __init__(self):
        self.now = time.time()
        self.bot_id = str(BOT_ID or "")
        self.owners = set(str(o) for o in self.settings.owners)

    @property
    def settings(self):
        """The current muse_settings.Settings (reloaded on change, so a
        write made through this ctx is visible right away)."""
        return settings()

    def guilds(self):
        with _DIRECTORY_LOCK:
            return [dict(g) for g in _DIRECTORY["guilds"]]

    def channels(self, guild_id):
        with _DIRECTORY_LOCK:
            return [dict(c) for c in
                    _DIRECTORY["channels"].get(str(guild_id)) or []]

    def write(self, key, value, by):
        with _OVERLAY_LOCK:
            MS.set_value(_SETTINGS.config_path, key, value, by, PLUGINS_DIR)
        settings()

    def reset(self, key, by):
        with _OVERLAY_LOCK:
            MS.reset(_SETTINGS.config_path, key, by, PLUGINS_DIR)
        settings()

    def undo(self, scope_prefix, by, **kw):
        """muse_settings.undo: revert the newest change (and the rest of
        its group) at one scope (scope_prefix as muse_settings.
        scope_prefix gives it, or a Place); the key reverted, or None.
        kw: children, admin_gid, expect (see muse_settings.undo)."""
        fn = getattr(MS, "undo", None)
        if fn is None:
            raise MS.SettingsError("undo is not available in this version")
        if kw.get("admin_gid"):
            kw.setdefault("info", self)
        with _OVERLAY_LOCK:
            out = fn(_SETTINGS.config_path, scope_prefix, by, PLUGINS_DIR,
                     **kw)
        settings()
        return out

    def apply(self, changes, by, role, group=None, admin_gid=None):
        """muse_settings.apply: several changes in one write and one undo
        group. role ("owner" or "admin") is required; an admin's changes
        are checked against the admin rule with admin_gid and this ctx's
        channel lists."""
        with _OVERLAY_LOCK:
            out = MS.apply(_SETTINGS.config_path, changes, by, group,
                           PLUGINS_DIR, role=role, admin_gid=admin_gid,
                           info=self)
        settings()
        return out

    def undo_group(self, group, by, undo_id=None):
        """muse_settings.undo_group: undo one specific group (owners
        only)."""
        with _OVERLAY_LOCK:
            out = MS.undo_group(_SETTINGS.config_path, group, by,
                                PLUGINS_DIR, role="owner", undo_id=undo_id)
        settings()
        return out

    def display_name(self, user_id):
        """A person's name the monitor has seen (None when unknown)."""
        return _USER_NAMES.get(str(user_id))

    def stash_put(self, user_id, payload, ttl=900):
        """Keep payload for user_id (single use, ttl seconds); a 10
        character token."""
        tok = secrets.token_urlsafe(8)[:10]
        now = time.time()
        with _STASH_LOCK:
            for k in [k for k, v in _STASH.items() if v[2] < now]:
                del _STASH[k]
            while len(_STASH) >= STASH_MAX:
                _STASH.pop(next(iter(_STASH)))
            _STASH[tok] = (str(user_id), payload, now + ttl)
        return tok

    def stash_take(self, user_id, token):
        """The payload stash_put kept, once; None when it is gone
        (expired, used, or the monitor restarted). PermissionError when
        it is someone else's (it stays theirs)."""
        with _STASH_LOCK:
            e = _STASH.get(str(token))
            if e is None or e[2] < time.time():
                _STASH.pop(str(token), None)
                return None
            if e[0] != str(user_id):
                raise PermissionError("not this user's")
            del _STASH[str(token)]
        return e[1]

    def recent_outcomes(self, place, limit=20):
        """The newest mention-ledger entries for a place (a Place from
        muse_settings: a server, a channel and its threads, or DMs),
        newest first: message_id, channel_id, guild_id, parent_id,
        author_id (None in entries older than 2026-10-19), author,
        status, code, detail, reason, at."""
        with _DIRECTORY_LOCK:
            where = dict(_DIRECTORY.get("where") or {})
            for gid, chans in _DIRECTORY["channels"].items():
                for c in chans:
                    where.setdefault(str(c.get("id")), (gid, None))
        out = []
        for mid, e in _ledger_load()["entries"].items():
            if not isinstance(e, dict):
                continue
            o = _outcome(mid, e, where)
            kind = getattr(place, "kind", None)
            if kind == "dms":
                ok = o["guild_id"] is None and o["dm"]
            elif kind == "server":
                ok = o["guild_id"] == place.guild_id
            elif kind == "channel":
                ok = (o["parent_id"] or o["channel_id"]) == place.channel_id
            else:
                ok = True
            if ok:
                out.append(o)
        out.sort(key=lambda o: o["at"], reverse=True)
        return out[:max(0, int(limit))]

    def outcome(self, message_id):
        """recent_outcomes' entry for one message, or None."""
        e = _ledger_load()["entries"].get(str(message_id))
        if not isinstance(e, dict):
            return None
        with _DIRECTORY_LOCK:
            where = dict(_DIRECTORY.get("where") or {})
        return _outcome(str(message_id), e, where)

    def names(self, some_id):
        """A server's or channel's name for muse_settings' sentences."""
        some_id = str(some_id)
        with _DIRECTORY_LOCK:
            for g in _DIRECTORY["guilds"]:
                if g["id"] == some_id:
                    return g.get("name")
            for chans in _DIRECTORY["channels"].values():
                for c in chans:
                    if str(c.get("id")) == some_id:
                        return c.get("name")
        return settings().labels.get(some_id)

    def reload(self):
        """A fresh Settings through the monitor's own watcher, so the
        pages and the main loop see the same state."""
        return settings()

    def notify_owners_admin_change(self, guild_id, user_id, key, old, new):
        """Queue one line for the owners' digest of server-admin changes
        in that server (sent by the main loop, one DM per server per
        ADMIN_DIGEST_SECS)."""
        gid = str(guild_id)
        with _ADMIN_LOCK:
            entry = _ADMIN_CHANGES.setdefault(
                gid, {"first": time.time(), "changes": []})
            entry["changes"].append({"user": str(user_id), "key": key,
                                     "old": old, "new": new,
                                     "at": time.time()})

    def log(self, msg):
        print(f"/settings: {msg}", flush=True)


def _outcome(mid, e, where):
    """One ledger entry for /settings. Its server comes from the entry
    (recorded since 2026-10-19), else from discovery; an entry is a DM's
    only when it says so or its channel is a known DM channel (an
    unmapped server channel is never shown under DMs)."""
    cid = str(e.get("channel_id") or "")
    code = e.get("code") or ledger_code(e.get("status"), e.get("reason"))
    if e.get("guild_id"):
        gid, parent = e["guild_id"], e.get("parent_id")
    else:
        gid, parent = where.get(cid, (None, None))
        info = CHANNELS.get(cid)
        if gid is None and info:
            gid = info.get("guild_id")
            parent = info.get("parent_id") if info.get("thread") else None
    dm = gid is None and bool(
        e.get("dm") or cid in DM_CHANNELS or cid in DM_CHANNEL_USER
        or code in ("dm-off", "dm-not-allowed"))
    return {"message_id": mid, "channel_id": cid or None,
            "guild_id": gid, "parent_id": parent, "dm": dm,
            "author_id": e.get("author_id"), "author": e.get("author"),
            "status": e.get("status"),
            "code": code,
            "detail": e.get("detail") or {}, "reason": e.get("reason"),
            "at": e.get("updated") or _snowflake_ts(mid)}


def _show_value(v):
    if v is True:
        return "on"
    if v is False:
        return "off"
    if v is None:
        return "none"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v) or "(empty)"
    return str(v)


def _digest_name(some_id):
    """A server's, channel's or person's name for the digest's words."""
    some_id = str(some_id)
    with _DIRECTORY_LOCK:
        for g in _DIRECTORY["guilds"]:
            if g["id"] == some_id:
                return g.get("name")
        for chans in _DIRECTORY["channels"].values():
            for c in chans:
                if str(c.get("id")) == some_id:
                    return c.get("name")
    return _USER_NAMES.get(some_id)


def _digest_words(c):
    """"In #general, When it answers: Chatty → Off" for a change whose
    key is an overlay key (what /settings sends), else None."""
    try:
        path, key = MS.parse_key(c["key"])
        place = MS.place_of_path(path)
        if place is None or MS.CORE.get(key) is None and not \
                key.startswith("plugins."):
            return None
        return MS.describe_change(place, key, c["old"], c["new"],
                                  _digest_name, path)
    except Exception:
        return None


def admin_digest_text(gid, changes):
    with _DIRECTORY_LOCK:
        known = _DIRECTORY["guilds"]
    name = next((g["name"] for g in known if g["id"] == gid), None) \
        or (GUILDS.get(gid) or {}).get("name") or gid
    head = (f"**Server admin changes in {name}** (server ID {gid}), made "
            f"with /settings:\n")
    tail = ("\nAdmins can only make me quieter there. To undo one, run "
            "/settings there and press Undo, or edit config.yaml.")
    lines = []
    for c in changes:
        when = datetime.fromtimestamp(c["at"], timezone.utc) \
            .strftime("%H:%M UTC")
        words = _digest_words(c)
        if words:
            lines.append(f"- <@{c['user']}>: {words}, {when}")
            continue
        lines.append(f"- <@{c['user']}> set `{c['key']}` to "
                     f"{_show_value(c['new'])} (was "
                     f"{_show_value(c['old'])}), {when}")
    kept = []
    room = 1900 - len(head) - len(tail) - len("- and 9999 more")
    for line in lines:
        if sum(len(x) + 1 for x in kept) + len(line) > room:
            kept.append(f"- and {len(lines) - len(kept)} more")
            break
        kept.append(line)
    return head + "\n".join(kept) + tail


def flush_admin_digests(force=False):
    """Main loop: DM the owners one digest per server whose first queued
    admin change is ADMIN_DIGEST_SECS old. Undelivered ones are kept and
    retried. Returns how many went out."""
    now = time.time()
    with _ADMIN_LOCK:
        due = {gid: e for gid, e in _ADMIN_CHANGES.items()
               if force or now - e["first"] >= ADMIN_DIGEST_SECS}
    if not due or rest_backing_off():
        return 0
    sent = 0
    for gid, entry in due.items():
        if notify_owners(admin_digest_text(gid, entry["changes"])):
            sent += 1
            with _ADMIN_LOCK:
                cur = _ADMIN_CHANGES.get(gid)
                if cur is entry:
                    del _ADMIN_CHANGES[gid]
        else:
            print(f"Admin digest for server {gid} undelivered; retrying.",
                  flush=True)
    return sent


_SETTINGS_UI = {"mod": None, "failed": None}


def load_settings_ui():
    """settings_ui.py next to monitor.py (else one on sys.path), imported
    once; None (logged once per distinct error) when it is missing or
    fails to import."""
    if _SETTINGS_UI["mod"] is not None:
        return _SETTINGS_UI["mod"]
    try:
        here = os.path.join(BASE_DIR, "settings_ui.py")
        if os.path.isfile(here):
            spec = importlib.util.spec_from_file_location("settings_ui",
                                                          here)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        else:
            import settings_ui as mod
        if not callable(getattr(mod, "handle", None)):
            raise ImportError("settings_ui has no handle()")
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        if _SETTINGS_UI["failed"] != err:
            _SETTINGS_UI["failed"] = err
            print(f"/settings UI unavailable ({err}); /settings answers "
                  f"\"Settings UI unavailable\".", flush=True)
        return None
    _SETTINGS_UI["mod"] = mod
    _SETTINGS_UI["failed"] = None
    return mod


def on_settings_interaction(ix, rest, uid, record):
    ui = load_settings_ui()
    if ui is None:
        return ephemeral("Settings UI unavailable.")
    t0 = time.time()
    try:
        response = ui.handle(ix, SettingsCtx())
    except Exception as e:
        print(f"/settings handler error: {type(e).__name__}: {e}",
              flush=True)
        return ephemeral("Something went wrong; try again.")
    spent = time.time() - t0
    if spent > 1.5:
        print(f"/settings took {spent:.1f}s to answer (Discord allows 3s).",
              flush=True)
    if not isinstance(response, dict) \
            or not isinstance(response.get("type"), int):
        print("/settings handler returned no callback body.", flush=True)
        return ephemeral("Something went wrong; try again.")
    return response


register_interaction(SETTINGS_PREFIX, on_settings_interaction,
                     owner_only=False)
register_interaction("/" + SETTINGS_COMMAND["name"], on_settings_interaction,
                     owner_only=False)
for _name in WHY_NAMES:
    register_interaction("/" + _name, on_settings_interaction,
                         owner_only=False)


# ---------------------------------------------------------------------------
# Settings requests: an owner DMs "only answer in #general when I @ you".
# The worker attaches a `settings` action to its outbox item for that DM;
# settings_nl.py decides it (who may, what it means, Apply first or not)
# and writes the reply. This side gives it a ctx, sends its outcome
# (with the usual nonce and retries), routes its mdnl:* buttons, and
# tells the owners about changes their Muse made with bin/settings.py.
# ---------------------------------------------------------------------------
_nl_live = NL.Live()
_NL_PEOPLE = {}        # user id -> name, from the conversation history
# Capability nonces of open owner-DM items (state["nl_nonces"]), for the
# wake hook only: mode 600, never read by a worker (like the token).
OWNER_DM_NONCES_FILE = os.path.join(BASE_DIR, "owner_dm_nonces.json")
_NL_NONCES_WRITTEN = {"body": None}
# Apply clicks written down before their overlay write (interaction
# thread), recovered at startup (settings_nl.recover).
NL_JOURNAL_FILE = os.path.join(BASE_DIR, "nl_journal.json")
_NL_JOURNAL_LOCK = threading.Lock()
_NL_UPKEEP = {"settings": None}


def _write_private(path, obj):
    """Atomic JSON write, mode 600 whatever the file had."""
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".",
                               prefix="." + os.path.basename(path) + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(obj, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def nl_journal(put=None, drop=None):
    """nl_journal.json: read it, or add (put=(token, entry)) or remove
    (drop=token) one entry. Any thread."""
    with _NL_JOURNAL_LOCK:
        try:
            with open(NL_JOURNAL_FILE) as f:
                data = json.load(f)
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        if put is not None:
            data[put[0]] = put[1]
        elif drop is not None and drop in data:
            del data[drop]
        else:
            return data
        _write_private(NL_JOURNAL_FILE, data)
        return data


def nl_mint_nonce(state, mid, channel_id=None, fragments=()):
    """A capability for one owner-DM queue item (see owner_dm_gate and
    settings_nl): 128 random bits, kept in state and the hook's private
    file, never in reply_queue.json. channel_id and fragments (its burst
    fragments' ids): so the gate knows the DM's channel and fragments
    after item_meta and reply_aliases (both bounded, fed by everyone's
    messages) have moved on."""
    n = state.setdefault("nl_nonces", {})
    n[str(mid)] = {"nonce": secrets.token_hex(16), "at": time.time()}
    if channel_id:
        n[str(mid)]["channel_id"] = str(channel_id)
    if fragments:
        n[str(mid)]["fragments"] = [str(f) for f in fragments]
    for old in list(n)[:-300]:
        del n[old]
    nl_write_nonces(state)


def nl_write_nonces(state):
    """owner_dm_nonces.json = the nonces of open owner-DM items (written
    only when that changed; expired ones dropped)."""
    n = state.get("nl_nonces") or {}
    now = time.time()
    for k in [k for k, v in n.items()
              if not isinstance(v, dict) or now - v.get("at", 0)
              > NL.MAX_AGE]:
        del n[k]
    body = {k: v["nonce"] for k, v in n.items()}
    if body == _NL_NONCES_WRITTEN["body"]:
        return
    try:
        _write_private(OWNER_DM_NONCES_FILE, body)
        _NL_NONCES_WRITTEN["body"] = body
    except OSError as e:
        print(f"Could not write owner_dm_nonces.json: {e}", flush=True)


# ---------------------------------------------------------------------------
# Owner DMs are bound to their wake. Any wake can be started by anyone's
# message, so a worker may be reading a stranger's prompt injection. The
# owner-DM nonce (above) is handed only to a wake that holds nothing but
# owner DMs (hooks/mention-watch.sh), so it proves that the outbox item
# was written in such a run. Every outbox item that answers an owner's
# DM, settles it (silent, reactions, a reply under one of its
# fragments) or posts in an owner's DM channel must carry that item's
# nonce as owner_dm_nonce (a settings action's settings.nonce counts
# too). Without it the item is refused before anything else looks at
# it: set aside in reply_outbox.rejected.json, logged, and the queue
# item stays open for the owner-DM-only wake that really holds it.
# Other items are untouched.
# ---------------------------------------------------------------------------
OWNER_DM_NONCE_FIELD = "owner_dm_nonce"
OWNER_DM_UPKEEP_SECS = 15
_OWNER_DM_UPKEEP = {"next": 0.0}


def _meta_owner_dm(meta):
    """item_meta says this item is an owner's own DM (an older record
    without the owner_dm flag: judged from its fields, failing closed)."""
    if not isinstance(meta, dict):
        return False
    if "owner_dm" in meta:
        return bool(meta["owner_dm"])
    return bool(meta.get("dm")) \
        and str(meta.get("author_id")) in OWNER_USER_IDS


def is_owner_dm_item(state, mid):
    """True when queue item mid (a trigger's message id) is an owner's
    DM: the monitor minted it a nonce, or its record says so."""
    if not mid:
        return False
    mid = str(mid)
    if mid in (state.get("nl_nonces") or {}):
        return True
    return _meta_owner_dm((state.get("item_meta") or {}).get(mid))


def owner_dm_channels(state):
    """The owners' DM channels: resolved ones, and those of owner-DM
    items the monitor recorded."""
    out = {c for c, u in DM_CHANNEL_USER.items() if u in OWNER_USER_IDS}
    for meta in (state.get("item_meta") or {}).values():
        if _meta_owner_dm(meta) and meta.get("channel_id"):
            out.add(str(meta["channel_id"]))
    for e in (state.get("nl_nonces") or {}).values():
        if isinstance(e, dict) and e.get("channel_id"):
            out.add(str(e["channel_id"]))
    return out


def _owner_dm_channel_of(state, qid):
    meta = (state.get("item_meta") or {}).get(qid) or {}
    e = (state.get("nl_nonces") or {}).get(qid) or {}
    cid = meta.get("channel_id") or (e.get("channel_id")
                                     if isinstance(e, dict) else None)
    return str(cid) if cid else None


def item_nonce(item):
    """The owner-DM nonce an outbox item carries: owner_dm_nonce, else
    its settings action's nonce. (value, problem)."""
    top = item.get(OWNER_DM_NONCE_FIELD)
    s = item.get("settings")
    sub = s.get("nonce") if isinstance(s, dict) else None
    if top is not None and not isinstance(top, str):
        return None, f"{OWNER_DM_NONCE_FIELD} is not a string"
    if top is not None and sub is not None and top != sub:
        return None, f"{OWNER_DM_NONCE_FIELD} and settings.nonce differ"
    got = top if top is not None else sub
    return (got if isinstance(got, str) else None), None


def queue_index(state):
    """What the queue file says, read once per outbox pass: "frags"
    {fragment id: trigger id} for every queued burst, "channels"
    {message id: channel id} for every queued item, and "owners" the
    same for every queued owner DM. The queue is the
    one record of a queued item that nothing else evicts (item_meta and
    reply_aliases are bounded, and lost with state.json)."""
    try:
        queue = load_queue()
    except (OSError, ValueError):
        queue = []
    frags, owners, channels = {}, {}, {}
    for q in queue:
        if not isinstance(q, dict) or not q.get("message_id"):
            continue
        mid = str(q["message_id"])
        channels[mid] = str(q.get("channel_id") or "")
        for f in q.get("burst_fragments") or []:
            if isinstance(f, dict) and f.get("message_id"):
                frags[str(f["message_id"])] = mid
        if _queued_owner_dm(state, q):
            owners[mid] = str(q.get("channel_id") or "")
    return {"frags": frags, "owners": owners, "channels": channels}


def trigger_of(state, mid, frags=None):
    """The queue item (trigger) a message id belongs to: a burst fragment
    maps to its trigger (reply_aliases, the queue's bursts, an owner
    DM's nonce entry), anything else to itself."""
    mid = str(mid)
    t = (state.get("reply_aliases") or {}).get(mid) \
        or (frags or {}).get(mid)
    if t:
        return str(t)
    for k, e in (state.get("nl_nonces") or {}).items():
        if isinstance(e, dict) and mid in (e.get("fragments") or ()):
            return str(k)
    return mid


# Channels looked up from the API (owner_dm_gate): channel id -> (is a
# DM, its recipients). Neither changes; who the owners are can.
_CHANNEL_LOOKUP = {}


def channel_kind(cid):
    """What the API says channel cid is: "owner_dm", "dm" or "guild", or
    None when it cannot tell. A DM with no recipients listed counts as
    an owner's (fail closed)."""
    if cid not in _CHANNEL_LOOKUP:
        try:
            ch = api_get(f"/channels/{cid}")
        except Exception as e:
            print(f"Could not look up channel {cid}: {e}", flush=True)
            return None
        if not isinstance(ch, dict):
            return None
        _CHANNEL_LOOKUP[cid] = (
            ch.get("type") in (1, 3),
            frozenset(str((r or {}).get("id"))
                      for r in ch.get("recipients") or []))
    dm, recips = _CHANNEL_LOOKUP[cid]
    if not dm:
        return "guild"
    return "owner_dm" if not recips or recips & OWNER_USER_IDS else "dm"


def _unknown_channel_problem(state, triggers, cid, index):
    """An outbox item that touches no owner DM, in a channel that is not
    a known owner DM channel: fine in a guild channel the bot reads, in
    the channel of the (non-owner) item it answers, or in a known
    non-owner DM. Anything else is looked up (the monitor's records of
    the owners' DM channels are best effort), failing closed."""
    if cid in CHANNELS or any(str(c.get("id")) == cid
                              for cs in _GUILD_CHANNELS.values()
                              for c in cs):
        return None
    if len(triggers) == 1:
        t = next(iter(triggers))
        meta = (state.get("item_meta") or {}).get(t) or {}
        if cid in (str(meta.get("channel_id")),
                   index["channels"].get(t)):
            return None
    if cid in DM_CHANNEL_USER:
        return None   # a listed person's DM (owners' were caught above)
    kind = channel_kind(cid)
    if kind in ("guild", "dm"):
        return None
    if kind == "owner_dm":
        return ("owner DM channel: the item names no owner DM that holds "
                "a live nonce")
    return f"channel {cid}: unknown, and could not be looked up"


def owner_dm_gate(state, item, index=None):
    """None when this (well-formed) outbox item may go on; else why it is
    refused. It must carry the nonce when it touches an owner's DM: its
    item, or its reply_to (or that fragment's trigger), is an owner-DM
    item, or its channel_id is an owner's DM channel. Then item and
    reply_to must all name that one owner DM (item exactly; reply_to it
    or one of its fragments), channel_id must be its channel, and the
    nonce must match (constant time). Never consumes the nonce: settling
    the item does (process_outbox), so retries and resends keep it.
    index: queue_index(), read once per pass."""
    index = index or {"frags": {}, "owners": {}, "channels": {}}
    named = [str(item[f]) for f in ("item", "reply_to") if item.get(f)]
    triggers = {trigger_of(state, m, index["frags"]) for m in named}
    owner = {t for t in triggers
             if is_owner_dm_item(state, t) or t in index["owners"]}
    cid = str(item.get("channel_id") or "")
    if not owner:
        if cid not in owner_dm_channels(state) \
                and cid not in index["owners"].values():
            return _unknown_channel_problem(state, triggers, cid, index)
        # An item may always answer in its own channel: one queued in
        # this DM before its author became an owner. (Nobody else's
        # item lives in an owner's 1:1 DM.)
        meta = (state.get("item_meta") or {}).get(next(iter(triggers))) \
            if len(triggers) == 1 else None
        if meta and str(meta.get("channel_id")) == cid:
            return None
        return ("owner DM channel: the item names no owner DM that holds "
                "a live nonce")
    if len(owner) != 1 or len(triggers) != 1:
        return ("owner DM: the item must answer exactly one owner DM, "
                "named by item and reply_to, in its own channel")
    qid = owner.pop()
    if item.get("item") and str(item["item"]) != qid:
        return "owner DM: item must be the DM's own message_id"
    if cid != (_owner_dm_channel_of(state, qid)
               or index["owners"].get(qid)):
        return f"owner DM {qid}: wrong channel"
    got, problem = item_nonce(item)
    if problem:
        return f"owner DM {qid}: bad nonce ({problem})"
    want = ((state.get("nl_nonces") or {}).get(qid) or {}).get("nonce")
    if not want:
        return (f"owner DM {qid}: bad nonce (none live: answered "
                f"already, or not handed out yet)")
    if not got:
        return (f"owner DM {qid}: bad nonce (missing: copy the item's "
                f"{OWNER_DM_NONCE_FIELD} from the wake payload)")
    if not hmac.compare_digest(str(want).encode(),
                               got.encode("utf-8", "replace")):
        return f"owner DM {qid}: bad nonce (wrong)"
    return None


def redact_nonces(item):
    """A copy of an outbox item for the rejected file and the log, its
    nonces blanked (a refused item may still carry a live one)."""
    if not isinstance(item, dict):
        return item
    out = dict(item)
    if OWNER_DM_NONCE_FIELD in out:
        out[OWNER_DM_NONCE_FIELD] = "(redacted)"
    if isinstance(out.get("settings"), dict) \
            and "nonce" in out["settings"]:
        out["settings"] = dict(out["settings"], nonce="(redacted)")
    return out


def _queued_owner_dm(state, q):
    """A queue item that is an owner's own DM: from the monitor's record
    of it, else (state.json lost) from the item the monitor wrote, the
    same test the wake hook uses to hand it out alone."""
    mid = str(q.get("message_id") or "")
    meta = (state.get("item_meta") or {}).get(mid)
    if meta is not None:
        return _meta_owner_dm(meta)
    card = q.get("settings") if isinstance(q.get("settings"), dict) else {}
    return q.get("kind") == "dm" and card.get("speaker") == "owner" \
        and str(q.get("author_id")) in OWNER_USER_IDS


def owner_dm_upkeep(state, force=False):
    """Main loop (every OWNER_DM_UPKEEP_SECS): every owner DM still in
    the queue holds a live nonce. Kept fresh while it is queued (the
    24-hour prune is for spent ones), and minted again when it is
    missing (state.json lost, or an item queued before nonces existed),
    so an owner DM never sits unanswerable: its next owner-DM-only wake
    carries the new one."""
    now = time.time()
    if not force and now < _OWNER_DM_UPKEEP["next"]:
        return
    _OWNER_DM_UPKEEP["next"] = now + OWNER_DM_UPKEEP_SECS
    try:
        queue = load_queue()
    except (OSError, ValueError):
        return
    n = state.setdefault("nl_nonces", {})
    for q in queue:
        if not isinstance(q, dict) or not q.get("message_id") \
                or not _queued_owner_dm(state, q):
            continue
        mid = str(q["message_id"])
        frags = [str(f["message_id"]) for f in q.get("burst_fragments")
                 or [] if isinstance(f, dict) and f.get("message_id")]
        e = n.get(mid)
        if isinstance(e, dict) and e.get("nonce"):
            e["at"] = now
            if not e.get("channel_id") and q.get("channel_id"):
                e["channel_id"] = str(q["channel_id"])
            if frags and not e.get("fragments"):
                e["fragments"] = frags
            continue
        n[mid] = {"nonce": secrets.token_hex(16), "at": now,
                  "channel_id": str(q.get("channel_id") or "")}
        if frags:
            n[mid]["fragments"] = frags
        print(f"Owner DM {mid} had no nonce; minted one for its next "
              f"wake.", flush=True)
    nl_write_nonces(state)


def is_forwarded(msg):
    """A forwarded message (Discord's message_reference type 1, with the
    forwarded content in message_snapshots)."""
    ref = msg.get("message_reference") or {}
    return ref.get("type") == 1 or bool(msg.get("message_snapshots"))


class _NlCtx(SettingsCtx):
    """What settings_nl gets (see its docstring); usable from the main
    loop (with state) and the interaction thread (without)."""

    def __init__(self, state=None):
        super().__init__()
        self.state = state
        self.live = _nl_live

    def info(self):
        with _DIRECTORY_LOCK:
            return {"guilds": [dict(g) for g in _DIRECTORY["guilds"]],
                    "channels": {g: [dict(c) for c in cs] for g, cs in
                                 _DIRECTORY["channels"].items()}}

    def thread_parent(self, cid):
        info = CHANNELS.get(str(cid)) or {}
        return info.get("parent_id") if info.get("thread") else None

    def dm_channel_user(self, cid):
        cid = str(cid)
        return DM_CHANNEL_USER.get(cid) if cid in DM_CHANNELS else None

    def person_names(self):
        return dict(_NL_PEOPLE)

    def channel_label(self, cid):
        """A place name the monitor knows (never an outbox field)."""
        cid = str(cid)
        if cid in DM_CHANNELS:
            return "a DM with me"
        info = CHANNELS.get(cid)
        if info:
            name = str(info.get("name") or cid).lstrip("#")
            g = info.get("guild_id")
            gname = (GUILDS.get(g) or {}).get("name") \
                or info.get("guild_name") or g
            return f"#{name} in {gname}" if gname else f"#{name}"
        return f"channel {cid}"

    @contextlib.contextmanager
    def write_lock(self):
        """The overlay write lock (this process, then the file), held
        across a decision and its write. Re-entrant."""
        with _OVERLAY_LOCK:
            with MS.overlay_lock(MS.overlay_path_for(_SETTINGS.config_path)):
                yield

    def journal_all(self):
        return nl_journal()

    def journal_put(self, tok, entry):
        nl_journal(put=(tok, entry))

    def journal_drop(self, tok):
        nl_journal(drop=tok)

    def undo_changes(self, place, by, children, expect, undo_id):
        with _OVERLAY_LOCK:
            out = MS.undo_changes(_SETTINGS.config_path, place, by,
                                  PLUGINS_DIR, children=children,
                                  role="owner", expect=expect,
                                  undo_id=undo_id)
        settings()
        return out

    def save(self, state):
        save_state(state)

    def log(self, msg):
        print(msg, flush=True)


def _nl_note_people(state):
    for hist in (state.get("history") or {}).values():
        for h in hist or []:
            if h.get("author_id") and h.get("author"):
                _NL_PEOPLE[str(h["author_id"])] = str(h["author"])


def nl_outbox_item(state, item):
    """Decide and answer one settings request from the outbox. Returns
    "settled", "retry" (a failed send waits for its next try) or
    ("rejected", reason): refused without settling its queue item (a
    forged or misdirected action), set aside for review."""
    retry = item.get("_retry") or {}
    if retry.get("next", 0) > time.time():
        return "retry"
    _nl_note_people(state)
    try:
        res = NL.process(_NlCtx(state), state, item)
    except Exception as e:
        # Never wedge the outbox on one item.
        print(f"Settings request for item {item.get('item')} failed: "
              f"{type(e).__name__}: {e}", flush=True)
        return ("rejected", f"settings: internal error "
                            f"({type(e).__name__})")
    finally:
        nl_write_nonces(state)
    for line in res.logs:
        print(line, flush=True)
    if res.rec is None:
        return ("rejected", res.reject)
    if res.alert:
        ok = notify_owners(res.alert)
        print("Settings refusal alert: "
              + ("owners alerted" if ok else "no owner reachable"),
              flush=True)
    rec = res.rec
    if rec.get("sent"):
        print(f"Skipping duplicate settings action for item "
              f"{item.get('item')}", flush=True)
        _settle(state, item, "replied", "duplicate suppressed")
        return "settled"
    if not rec.get("text"):
        _settle(state, item, rec["status"], rec["reason"], replied=True)
        rec["sent"] = True
        return "settled"
    cid, rt = rec["channel_id"], rec.get("reply_to")
    try:
        msg = send_reply(cid, rec["text"], rt,
                         nonce=send_nonce(cid, rt, [rec["text"]], 0),
                         components=rec.get("components"))
    except Exception as e:
        now = time.time()
        first = retry.get("first", now)
        if getattr(e, "code", None) == 403 or now - first >= SEND_RETRY_SECS:
            print(f"Giving up on the settings reply for item "
                  f"{item.get('item')}: {e}", flush=True)
            _settle(state, item, "skipped", f"send failed: {e}"[:200])
            return "settled"
        n = retry.get("n", 0) + 1
        item["_retry"] = {"first": first, "n": n,
                          "next": now + min(30, 2 ** n)}
        print(f"Failed to send the settings reply (attempt {n}; retrying, "
              f"same nonce): {e}", flush=True)
        return "retry"
    NL.sent(state, item["item"], (msg or {}).get("id"))
    _settle(state, item, rec["status"], rec["reason"], replied=True)
    return "settled"


def on_nl_button(ix, rest, uid, record):
    return NL.on_button(_NlCtx(), ix, rest, uid, record)


register_interaction("mdnl", on_nl_button, owner_only=True)


def nl_note_changes(state):
    """Main loop: change groups made with bin/settings.py since the
    position saved in state.json wait for the owners' notice (so ones
    made while the monitor was down are noticed too)."""
    cur = settings()
    if _NL_UPKEEP["settings"] is cur and "nl_history_mark" in state:
        return
    _NL_UPKEEP["settings"] = cur
    ctx = _NlCtx(state)
    names = NL.names_for(ctx, MS._Info(ctx.info()), cur)
    NL.queue_notices(state, NL.new_cli_groups(cur, state, names),
                     time.time())


def nl_recover(state):
    """Startup: settle Apply clicks a stop interrupted (settings_nl)."""
    n = NL.recover(_NlCtx(state), state)
    if n:
        print(f"Settings requests: recovered {n} interrupted Apply "
              f"click(s).", flush=True)


def nl_upkeep(state):
    """Main loop, every tick: expire Apply cards (edited to say so, best
    effort), prune, and send the batched notice of changes the owners'
    Muse made (silent DMs, with Undo)."""
    try:
        # First, and alone: nothing else here may stop owner DMs from
        # getting their nonces.
        owner_dm_upkeep(state)
    except Exception as e:
        print(f"Owner DM nonce upkeep error: {type(e).__name__}: {e}",
              flush=True)
    ctx = _NlCtx(state)
    nl_note_changes(state)
    nl_write_nonces(state)
    edits, logs = NL.expire(ctx, state)
    for line in logs:
        print(line, flush=True)
    for cid, mid, text in edits:
        try:
            api_patch(f"/channels/{cid}/messages/{mid}",
                      {"content": text, "components": []})
        except Exception as e:
            print(f"Could not edit an expired settings card: {e}",
                  flush=True)
    if rest_backing_off():
        return
    due = NL.due_notice(ctx, state)
    if due is None:
        return
    text, components, groups = due
    payload = {"content": text, "allowed_mentions": {"parse": []},
               "flags": NL.SUPPRESS_NOTIFICATIONS}
    if components:
        payload["components"] = components
    delivered = not OWNER_USER_IDS
    for uid in sorted(OWNER_USER_IDS):
        if dm_owner(uid, payload):
            delivered = True
    dropped = NL.notice_done(state, delivered)
    for g in groups:
        print(f"Settings (CLI, by {g['by']}): applied "
              f"{g.get('group') or 'a change'}: " + "; ".join(g["lines"])
              + ("; owners notified" if delivered else
                 "; no owner reachable" + ("" if dropped else
                                           " (retrying)")), flush=True)


# Registering the command: a global application command, checked once per
# run (GET, then POST when missing or PATCH when it differs; never a bulk
# PUT, which would delete any other command). A failure is retried on a
# later cycle and never stops the monitor.
_APP_ID = {"id": None}
_COMMAND_REG = {"done": False, "next_try": 0.0, "last_error": None}
COMMAND_RETRY_SECS = 60


def api_patch(path, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(API + path, data=body, headers=headers(),
                                 method="PATCH")

    def _do():
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    return _api_call(_do)


def _command_differs(have, want):
    for k, v in want.items():
        got = have.get(k)
        if isinstance(v, list):
            got = sorted(got) if isinstance(got, list) else got
            v = sorted(v)
        if got != v:
            return True
    return bool(have.get("options"))


def ensure_settings_command(app_id):
    """Make the global /settings command match SETTINGS_COMMAND, and the
    "Why no reply?" message command WHY_COMMAND. Returns "created",
    "updated" or "unchanged" (the first that applies: /settings, then
    the message command); raises on failure."""
    base = f"/applications/{urllib.parse.quote(str(app_id), safe='')}" \
        "/commands"
    have = api_get(base)
    if not isinstance(have, list):
        raise ValueError(f"unexpected command list: {str(have)[:80]}")
    r1 = _ensure_command(base, have, SETTINGS_COMMAND,
                         (SETTINGS_COMMAND["name"],))
    r2 = _ensure_command(base, have, WHY_COMMAND, WHY_NAMES)
    return r1 if r1 != "unchanged" else r2


def _ensure_command(base, have, want, names):
    """Create or update one command (names: what it may be called; the
    one already registered is kept). A message command name Discord
    refuses (400) is retried with the next name."""
    mine = [c for c in have if isinstance(c, dict)
            and c.get("name") in names
            and c.get("type", 1) == want["type"]]
    if mine:
        want = dict(want, name=mine[0]["name"])
        if not _command_differs(mine[0], want):
            return "unchanged"
        cid = urllib.parse.quote(str(mine[0].get("id")), safe="")
        api_patch(f"{base}/{cid}", want)
        return "updated"
    for i, name in enumerate(names):
        try:
            api_post(base, dict(want, name=name))
            return "created"
        except Exception as e:
            if getattr(e, "code", None) != 400 or i == len(names) - 1:
                raise
            print(f"Discord refused the command name {name!r}; trying "
                  f"{names[i + 1]!r}.", flush=True)
    return "unchanged"


def register_commands(gateway_status=None):
    """Main loop, every tick until it succeeds once (throttled to one
    try per COMMAND_RETRY_SECS). Never raises."""
    reg = _COMMAND_REG
    now = time.time()
    if reg["done"] or now < reg["next_try"] or BOT_USER_ID is None \
            or rest_backing_off():
        return None
    reg["next_try"] = now + COMMAND_RETRY_SECS
    try:
        if not _APP_ID["id"]:
            app = (gateway_status or {}).get("application") or {}
            _APP_ID["id"] = app.get("id") if isinstance(app, dict) \
                else None
        if not _APP_ID["id"]:
            _APP_ID["id"] = (api_get("/applications/@me") or {}).get("id")
        if not _APP_ID["id"]:
            raise ValueError("application id unknown")
        result = ensure_settings_command(_APP_ID["id"])
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        if err != reg["last_error"]:
            hint = (" (the bot's invite may lack the applications.commands "
                    "scope; see SETUP.md)"
                    if getattr(e, "code", None) == 403 else "")
            print(f"/settings command registration failed ({err}){hint}; "
                  f"retrying in {COMMAND_RETRY_SECS}s.", flush=True)
        reg["last_error"] = err
        return None
    reg["done"] = True
    reg["last_error"] = None
    if result != "unchanged":
        print(f"/settings command {result} (global; it can take a moment "
              f"to show up in Discord).", flush=True)
    return result


def is_mention(msg):
    """True when the message @mentions the bot on the platform. Plain
    text containing the bot's name is NOT a mention: in a server whose
    topic shares the bot's name, that made every casual "my muse ..." a
    summons. Whether a bot author may trigger is trigger_kind's call."""
    author = msg.get("author", {}) or {}
    if author.get("id") == BOT_ID:
        return False
    return any(m.get("id") == BOT_ID for m in msg.get("mentions", []))


def trigger_kind(msg, guild_id=None, state=None):
    """Classify how a server message addresses the bot, with the settings
    of its scope: "mention" (a platform @mention or a Discord reply to
    the bot: direct address), "follow_up" (only when state is given: the
    author's conversational follow-up window in this channel is open), or
    None. who.replies: off answers nobody; other bots trigger by direct
    address only, and never under who.bots: ignore; who.allow /
    who.ignore apply to everyone but the owners. The bot's own messages
    never trigger."""
    return trigger_verdict(msg, guild_id, state)[0]


def addresses_bot(msg):
    """Direct address as bot.respond_to defines it: a platform @mention
    or a Discord reply to the bot."""
    return ("mention" in RESPOND_TO and is_mention(msg)) \
        or ("reply_to_bot" in RESPOND_TO and is_reply_to_bot(msg))


def trigger_verdict(msg, guild_id=None, state=None):
    """trigger_kind plus why: (kind, refusal). refusal is set only when
    the message addresses the bot directly (addresses_bot) and the
    settings of its scope decline it; ordinary chatter gets None."""
    author = msg.get("author", {}) or {}
    if author.get("id") == BOT_ID:
        return None, None
    gid = guild_id or msg.get("guild_id") \
        or (CHANNELS.get(msg.get("channel_id")) or {}).get("guild_id")
    here = settings().resolve(scope_for(gid, msg.get("channel_id")))
    replies = here["who.replies"]
    direct = addresses_bot(msg)
    if replies == "off":
        return None, "who.replies is off here" if direct else None
    if is_bot_author(author) and here["who.bots"] == "ignore":
        return None, "who.bots: ignore" if direct else None
    if not direct and not (state is not None
                           and replies == "conversational"
                           and follow_up_open(state, msg)):
        return None, None
    if not who_allows(msg, gid, here):
        return None, ("not allowed by who.allow / who.ignore"
                      if direct else None)
    return ("mention" if direct else "follow_up"), None


def note_refused(msg, cname, reason):
    """A message that addressed the bot directly but that the settings
    decline: ledgered as skipped, so the reconciliation sweep (which
    judges by the settings in force when it runs) never answers it
    later, after a setting is relaxed. A deliberately ignored message
    stays ignored. Ordinary chatter is never ledgered."""
    author = msg.get("author", {}) or {}
    ledger_record(msg["id"], "skipped", f"settings: {reason}",
                  channel_id=msg.get("channel_id"),
                  author=author.get("username"), author_id=author.get("id"))
    print(f"Not answering {author.get('username')} in {cname}: "
          f"{reason}.", flush=True)


def is_reply_to_bot(msg):
    """True when the message is a Discord reply to one of the bot's own
    messages. Replying to the bot is addressing the bot, even with no
    @mention, so it counts as a mention trigger. Uses the embedded
    referenced_message only (no extra API call); the mention check still
    catches @mentions."""
    rm = msg.get("referenced_message")
    if not rm:
        return False
    return (rm.get("author", {}) or {}).get("id") == BOT_ID


def get_reply_context(msg):
    """If msg used Discord's reply feature, return the referenced message as
    {author, author_id, content, is_bot} so the worker knows whose
    conversation this message belongs to. Prefers the embedded
    referenced_message; falls back to one API fetch. Returns None when the
    message is not a reply or the referenced message is gone."""
    ref = msg.get("message_reference") or {}
    ref_id = ref.get("message_id")
    if not ref_id:
        return None
    rm = msg.get("referenced_message")
    if rm is None:
        try:
            rm = api_get(f"/channels/{msg['channel_id']}/messages/{ref_id}")
        except Exception:
            return None
    if not rm:
        return None
    author = rm.get("author", {}) or {}
    return {
        "author": author.get("username", "?"),
        "author_id": author.get("id"),
        "content": (rm.get("content") or "")[:500],
        "is_bot": author.get("id") == BOT_ID,
    }


def hold_fragment(state, channel_id, msg):
    """If (channel, author) already has a burst being coalesced, append this
    message to it as a fragment and return True (caller must not classify it
    independently). The trigger message itself is never a fragment."""
    author_id = (msg.get("author", {}) or {}).get("id")
    key = f"{channel_id}:{author_id}"
    entry = state.get("pending", {}).get(key)
    if entry and msg["id"] != entry["first_id"]:
        entry["fragments"].append(msg)
        print(f"Holding fragment from "
              f"{(msg.get('author', {}) or {}).get('username')} in "
              f"{entry['channel_name']} for one coalesced reply.",
              flush=True)
        return True
    return False


def stage_trigger(state, msg, cname, guild_id, kind, dm=False):
    """Stage a trigger (mention / DM message) in the
    pending buffer instead of queueing it immediately. Fragments arriving
    within COALESCE_SECS fold into the same burst; flush_pending() turns
    each matured burst into a single queue item. A second trigger from the
    same author while a burst is open becomes a fragment of it."""
    author_id = (msg.get("author", {}) or {}).get("id")
    key = f"{msg['channel_id']}:{author_id}"
    pending = state.setdefault("pending", {})
    if key in pending:
        entry = pending[key]
        if msg["id"] == entry["first_id"] or any(
                f["id"] == msg["id"] for f in entry["fragments"]):
            return  # already in this burst (staged twice): nothing to add
        if kind == "mention" and entry.get("kind") == "follow_up":
            entry["kind"] = "mention"  # direct address beats a follow-up
        entry["fragments"].append(msg)
        print(f"Additional trigger from "
              f"{(msg.get('author', {}) or {}).get('username')} folded into "
              f"pending burst in {cname}.", flush=True)
        return
    pending[key] = {
        "first_id": msg["id"],
        "channel_id": msg["channel_id"],
        "channel_name": cname,
        "guild_id": guild_id,
        "author_id": author_id,
        "kind": kind,  # "mention" | "follow_up" | "dm"
        "dm": dm,
        "first_seen": time.time(),
        "window": BOT_COALESCE_SECS
        if is_bot_author(msg.get("author")) else COALESCE_SECS,
        "trigger": msg,
        "fragments": [],
    }
    note_ingested(state, msg["id"])
    print(f"Staged {kind} from {(msg.get('author', {}) or {}).get('username')} "
          f"in {cname} for coalescing.", flush=True)


def stage_found(state, msg, cname, guild_id, kind):
    """Stage one classified guild trigger the moment it is classified, in
    arrival order, so a non-trigger follow-up later in the same poll
    batch or gateway drain finds the open burst (hold_fragment) instead
    of being classified before its trigger was staged. Talk scope: a
    trigger in a channel the bot cannot send in is never staged; it is
    logged once per channel and ledgered as skipped so the sweep leaves
    it alone."""
    info = CHANNELS.get(msg["channel_id"])
    if info is not None and info.get("can_send") is False:
        _note_no_send(msg, cname)
        return
    stage_trigger(state, msg, cname, guild_id, kind)


def alias_fragments(state, trigger_id, fragments):
    """Remember which trigger each burst fragment belongs to. The worker
    replies under the LAST fragment while the ledger and the sweep track
    the trigger, so a reply (or a silent pass) under any fragment settles
    the whole burst, and the sweep never requeues a fragment alone."""
    if not fragments:
        return
    aliases = state.setdefault("reply_aliases", {})
    for f in fragments:
        aliases[f["id"]] = trigger_id
    for mid in list(aliases)[:-500]:
        del aliases[mid]
# ---------------------------------------------------------------------------
# Early burst flush. The coalesce window (behavior.coalesce_seconds, or
# BOT_COALESCE_SECS for bot authors) is the MAXIMUM wait. With the
# gateway's typing events, a burst flushes as soon as its author has
# visibly stopped: their newest message arrived over the gateway at least
# BURST_QUIET_SECS ago and no TYPING_START from them in that channel has
# arrived since. Gateway events are drained in order, so a sequence
# number orders typing against messages exactly. When the gateway is
# down (or missed the newest message) the burst waits the full window as
# before. Bots get a longer quiet gap: a Muse sends multi-part replies
# with 2-4s beats, and a bot may not send typing between parts. A
# human's DM gets a shorter one: a DM is all addressed to the bot, and
# the typing hold still keeps a burst open while they write the next
# line.
# ---------------------------------------------------------------------------
BURST_QUIET_SECS = 1.5
DM_BURST_QUIET_SECS = 0.5
BOT_BURST_QUIET_SECS = 5.0
_activity = {}  # "<channel>:<author>" -> {msg_id, msg_at, msg_seq, typing_seq}
_activity_seq = 0


def note_activity(channel_id, user_id, msg_id=None):
    """Record a gateway MESSAGE_CREATE (msg_id set) or TYPING_START from
    user_id in channel_id, in drain order."""
    global _activity_seq
    if not channel_id or not user_id:
        return
    _activity_seq += 1
    key = f"{channel_id}:{user_id}"
    act = dict(_activity.get(key) or {})
    if msg_id:
        if act.get("msg_id") and int(act["msg_id"]) >= int(msg_id):
            return  # an older message arriving late changes nothing
        act.update(msg_id=msg_id, msg_at=time.time(), msg_seq=_activity_seq)
    else:
        act["typing_seq"] = _activity_seq
    _activity[key] = act
    if len(_activity) > 1000:
        cutoff = time.time() - 600
        for k in [k for k, a in _activity.items()
                  if a.get("msg_at", 0) < cutoff]:
            del _activity[k]


def burst_quiet(entry, now):
    """True once a pending burst's author has stopped (see above)."""
    act = _activity.get(f"{entry['channel_id']}:{entry['author_id']}")
    if not act or not act.get("msg_id"):
        return False
    newest = max([entry["trigger"]] + entry["fragments"],
                 key=lambda m: int(m["id"]))
    if act["msg_id"] != newest["id"]:
        return False  # the gateway did not see the newest message
    if act.get("typing_seq", 0) > act["msg_seq"]:
        return False  # typing again since their newest message
    quiet = BOT_BURST_QUIET_SECS \
        if is_bot_author(entry["trigger"].get("author")) \
        else BURST_QUIET_SECS
    if entry.get("dm") and quiet == BURST_QUIET_SECS:
        quiet = DM_BURST_QUIET_SECS  # a human's DM
    return now - act["msg_at"] >= quiet


def pending_wait(state, now):
    """How long the main loop may sleep before a staged burst could
    flush: until its window ends or its author's quiet gap (as in
    burst_quiet) passes, at most 0.5s (typing events do not wake the
    loop). None when no burst is staged."""
    due = []
    for entry in state.get("pending", {}).values():
        if entry.get("next_try", 0) > now:
            continue  # enqueue back-off: its retry is minutes away
        due.append(entry.get("first_seen", now)
                   + entry.get("window", COALESCE_SECS))
        act = _activity.get(f"{entry.get('channel_id')}:"
                            f"{entry.get('author_id')}") or {}
        if act.get("msg_at"):
            due.append(act["msg_at"] + (DM_BURST_QUIET_SECS
                                        if entry.get("dm")
                                        else BURST_QUIET_SECS))
    if not due:
        return None
    ahead = [d - now for d in due if d > now]
    return max(0.02, min(ahead + [0.5]))


def flush_pending(state, queue, force=False):
    """Turn pending bursts whose author has stopped (burst_quiet) or whose
    coalesce window has elapsed into single queue items. Returns True when
    anything was queued."""
    now = time.time()
    pending = state.get("pending", {})
    flushed = False
    for key in list(pending.keys()):
        entry = pending[key]
        if not force and entry.get("next_try", 0) > now:
            continue  # enqueue failed recently; backing off before retry
        early = burst_quiet(entry, now)
        if force or early or now - entry["first_seen"] >= entry.get(
                "window", COALESCE_SECS):
            trigger = entry["trigger"]
            ok = enqueue_message(trigger, entry["channel_name"], state,
                                 queue, dm=entry["kind"] == "dm",
                                 fragments=entry["fragments"],
                                 guild_id=entry["guild_id"],
                                 follow_up=entry["kind"] == "follow_up")
            n_frag = len(entry["fragments"])
            why = " (author stopped typing)" if early and not force else ""
            print(f"Flushed {entry['kind']} burst "
                  f"({1 + n_frag} message(s)) from "
                  f"{(trigger.get('author', {}) or {}).get('username')} in "
                  f"{entry['channel_name']}: queued={ok}{why}", flush=True)
            if ok:
                del pending[key]
                flushed = True
            elif any(q["message_id"] == trigger["id"] for q in queue):
                # Already queued by another path (e.g. the sweep): the
                # enqueue "failure" was just its duplicate guard. Its
                # fragments ride on that item.
                alias_fragments(state, trigger["id"], entry["fragments"])
                del pending[key]
            elif trigger["id"] in _quarantine_ids() \
                    or _ledger_terminal(trigger["id"]):
                # quarantine() or an enqueue gate (join watermark)
                # already ledgered it.
                del pending[key]
            else:
                # No silent drops: retry a few times, then record the
                # loss in the ledger where the sweep can see it.
                entry["tries"] = entry.get("tries", 0) + 1
                if entry["tries"] >= 3:
                    ledger_record_many(
                        [trigger["id"]]
                        + [f["id"] for f in entry["fragments"]],
                        "skipped",
                        f"enqueue failed {entry['tries']}x",
                        channel_id=entry["channel_id"],
                        author=(trigger.get('author', {}) or {})
                        .get('username'),
                        author_id=(trigger.get('author', {}) or {})
                        .get('id'))
                    print(f"Dropping burst from "
                          f"{(trigger.get('author', {}) or {}).get('username')} "
                          f"in {entry['channel_name']} after 3 failed "
                          f"enqueues (recorded in mention ledger).",
                          flush=True)
                    del pending[key]
                else:
                    entry["next_try"] = now + 300
                    print(f"Enqueue failed for burst in "
                          f"{entry['channel_name']}; retry "
                          f"{entry['tries']}/3 in 5 min.", flush=True)
    return flushed


def send_nonce(channel_id, reply_to, parts, index):
    """Idempotency key for one message part. A POST can fail on our side
    (timeout, dropped connection, 5xx) after Discord already created the
    message; with enforce_nonce, Discord answers a repeat of the same
    nonce from the same bot within a few minutes with the message it
    already made instead of posting it again. Derived from the reply
    itself, so retries and even a second monitor process agree on it.
    Discord caps nonces at 25 characters."""
    key = json.dumps([channel_id, reply_to, parts, index])
    return hashlib.sha256(key.encode()).hexdigest()[:25]


# What a monitor send may ping: users (the worker addresses people with
# <@id>) and the author of the message replied to. Never roles,
# @everyone or @here, whatever the text says; the zero-width rewrite in
# sanitize_reply is a second layer, not the only one.
SEND_ALLOWED_MENTIONS = {"parse": ["users"], "replied_user": True}
DISCORD_MAX_CHARS = 2000  # Discord's hard per-message content limit


def _is_snowflake(value):
    """True for a plain Discord id in its one canonical form: ASCII
    digits, no leading zero. Ids from the worker-written outbox are
    interpolated into API paths, where anything else ("1/../../guilds/2")
    could turn a send into a different API call, and compared as strings
    (owner_dm_gate), where "0" + an id would be a different string for
    the same channel."""
    return isinstance(value, (str, int)) and not isinstance(value, bool) \
        and re.fullmatch(r"[1-9][0-9]{0,19}", str(value)) is not None


def reply_reference(message_id):
    """The message_reference for a reply. fail_if_not_exists: false makes
    Discord post it as a plain message when the message replied to was
    deleted meanwhile, instead of answering 400 on every retry."""
    return {"message_id": message_id, "fail_if_not_exists": False}


def send_reply(channel_id, text, reply_to=None, nonce=None,
               components=None):
    data = {"content": text, "allowed_mentions": SEND_ALLOWED_MENTIONS}
    if components:
        data["components"] = components
    if reply_to:
        data["message_reference"] = reply_reference(reply_to)
    if nonce:
        data["nonce"] = nonce
        data["enforce_nonce"] = True
    return api_post(f"/channels/{channel_id}/messages", data)


def send_typing(channel_id):
    """Show the typing indicator for ~10s. Signals responsiveness while
    the reply worker drafts the real reply. Best-effort, and skipped
    during REST back-off: an indicator is never worth rate-limit
    pressure."""
    if rest_backing_off():
        return
    with _typing_lock:
        _typing_last[channel_id] = time.time()
    try:
        api_post(f"/channels/{channel_id}/typing", {})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Typing indicator. Discord shows one typing ping for ~10s. The main loop
# used to re-send it every 8s itself, but one iteration can take much
# longer (a full REST poll, the sweep, discovery, media downloads, a
# multi-part send), so the indicator kept lapsing and users saw it
# flicker until the reply arrived. A daemon thread now pings every
# active channel on its own clock. The main loop only says which
# channels are active (typing_refresh): a channel types while it has a
# pending burst (not in enqueue back-off) or a queue item, for at most
# TYPING_MAX_SECS per burst or item so a dead worker does not type
# forever. process_outbox stops an item's typing right before its reply
# goes out (sending a message ends the indicator; the beats in
# send_fragmented cover multi-part replies).
# ---------------------------------------------------------------------------
TYPING_EVERY_SECS = 8
TYPING_MAX_SECS = 180
_typing_lock = threading.Lock()
_typing_sources = {}  # "burst:<key>" / "item:<id>" -> (channel_id, start)
_typing_last = {}     # channel_id -> time of the last typing ping


def typing_sync(prefix, current):
    """Make current ({key: (channel_id, start_ts)}) the full set of typing
    sources whose keys start with prefix."""
    with _typing_lock:
        for key in [k for k in _typing_sources
                    if k.startswith(prefix) and k not in current]:
            del _typing_sources[key]
        _typing_sources.update(current)


def typing_clear(keys, sent_in=None):
    """Drop typing sources (their reply is going out or was settled).
    sent_in: a channel a message was just posted in, which ended its
    indicator, so any other source there is re-pinged promptly."""
    with _typing_lock:
        for key in keys:
            _typing_sources.pop(key, None)
        if sent_in:
            _typing_last.pop(sent_in, None)


def typing_refresh(state):
    """Publish the channels that are waiting on a reply (see above)."""
    now = time.time()
    bursts = {}
    for key, entry in state.get("pending", {}).items():
        # A burst backing off after a failed enqueue does not type (a
        # rate cap would otherwise keep it typing for the ~15 min of
        # retries).
        if entry.get("next_try", 0) <= now:
            bursts["burst:" + key] = (entry["channel_id"],
                                      entry.get("first_seen", now))
    typing_sync("burst:", bursts)
    items = {}
    for item in load_queue():
        if not isinstance(item, dict) or not item.get("channel_id") \
                or not item.get("message_id"):
            continue
        start = item.get("queued_at") or _snowflake_ts(item["message_id"])
        items["item:" + str(item["message_id"])] = (item["channel_id"],
                                                    start)
    typing_sync("item:", items)


def _typing_thread_main():
    while True:
        try:
            time.sleep(1.0)
            if rest_backing_off():
                continue
            now = time.time()
            with _typing_lock:
                active = {cid for cid, start in _typing_sources.values()
                          if now - start < TYPING_MAX_SECS}
                due = [cid for cid in active
                       if now - _typing_last.get(cid, 0)
                       >= TYPING_EVERY_SECS]
                for cid in [c for c, t in _typing_last.items()
                            if now - t > 60]:
                    del _typing_last[cid]
            for cid in due:
                send_typing(cid)
        except Exception as e:
            print(f"Typing thread error: {e}", flush=True)


def add_reaction(channel_id, message_id, emoji):
    """Add a Unicode emoji reaction to a message (PUT .../@me). Used for
    reactions drafted by the worker via the outbox 'reactions' list."""
    encoded = urllib.parse.quote(emoji, safe="")  # "/" stays in the segment
    try:
        req = urllib.request.Request(
            API + f"/channels/{channel_id}/messages/{message_id}"
                 f"/reactions/{encoded}/@me",
            data=b"", headers=headers(), method="PUT")
        with urllib.request.urlopen(req, timeout=20):
            pass
        return True
    except Exception as e:
        print(f"Reaction {emoji!r} failed on {message_id}: {e}", flush=True)
        return False


def sanitize_reply(text):
    """Apply output guardrails to a drafted reply."""
    text = (text or "").strip()[:MAX_REPLY_CHARS]
    # Neutralize mass mentions with a zero-width space
    text = text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    return text


MAX_PARTS = 4  # fragmented delivery: never more than 4 messages per reply


def normalize_parts(item):
    """Return a reply as a list of message parts for fragmented delivery.

    The worker drafts long replies as "parts" (2-4 short messages, like a
    person thinking aloud) instead of one "text" blob. Short replies stay a
    single-element list. Enforces the part cap and the configured total budget
    (behavior.max_reply_chars).
    """
    raw = item.get("parts")
    clean = ([p for p in raw if isinstance(p, str) and p.strip()]
             if isinstance(raw, list) else [])
    if clean:
        parts = [sanitize_reply(p) for p in clean[:MAX_PARTS]]
        if len(clean) > MAX_PARTS:
            # Fold overflow into the last part rather than dropping thoughts.
            rest = " ".join(sanitize_reply(p) for p in clean[MAX_PARTS:])
            parts[-1] = sanitize_reply((parts[-1] + " " + rest).strip())
    else:
        parts = [sanitize_reply(item.get("text", ""))]
    parts = [p for p in parts if p]
    total = sum(len(p) for p in parts)
    if total > MAX_REPLY_CHARS:
        overflow = total - MAX_REPLY_CHARS
        parts[-1] = parts[-1][:max(0, len(parts[-1]) - overflow)].rstrip()
        parts = [p for p in parts if p]
    return parts


def send_fragmented(channel_id, parts, reply_to=None, attachments=()):
    """Send a reply as sequential messages, like a person thinking aloud.

    The first part is threaded under reply_to; follow-ups go out as plain
    messages with a typing beat between them. Attachments ride on the last
    part. Raises on the first failure; the caller retries the whole
    reply, and each part's nonce makes Discord return the parts that
    already landed instead of posting them again.
    """
    last_idx = len(parts) - 1
    for i, part in enumerate(parts):
        if i > 0:
            send_typing(channel_id)
            time.sleep(random.uniform(2.0, 4.0))
        nonce = send_nonce(channel_id, reply_to, parts, i)
        if i == last_idx and attachments:
            send_reply_with_files(channel_id, part, attachments,
                                  reply_to if i == 0 else None, nonce)
        elif i == 0:
            send_reply(channel_id, part, reply_to, nonce)
        else:
            send_reply(channel_id, part, nonce=nonce)


ATTACH_CTYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".mp4": "video/mp4",
}


def send_reply_with_files(channel_id, text, files, reply_to=None,
                          nonce=None):
    """Send a message with file attachments via multipart/form-data.
    files: list of (local_path, filename)."""
    boundary = "----MuseDiscord%d" % int(time.time() * 1000)
    payload = {"content": text, "allowed_mentions": SEND_ALLOWED_MENTIONS}
    if reply_to:
        payload["message_reference"] = reply_reference(reply_to)
    if nonce:
        payload["nonce"] = nonce
        payload["enforce_nonce"] = True
    body = b""
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="payload_json"\r\n'
             f'Content-Type: application/json\r\n\r\n').encode()
    body += json.dumps(payload).encode() + b"\r\n"
    for i, (path, fname) in enumerate(files):
        ext = os.path.splitext(fname)[1].lower()
        ctype = ATTACH_CTYPES.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            data = f.read()
        body += (f'--{boundary}\r\nContent-Disposition: form-data; '
                 f'name="files[{i}]"; filename="{fname}"\r\n'
                 f'Content-Type: {ctype}\r\n\r\n').encode()
        body += data + b"\r\n"
    body += f'--{boundary}--\r\n'.encode()
    req = urllib.request.Request(
        f"{API}/channels/{channel_id}/messages", data=body,
        headers={"Authorization": f"Bot {TOKEN}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}",
                 "User-Agent": headers()["User-Agent"]})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def fit_for_discord(path):
    """Return (send_path, filename) with the file under Discord's upload cap,
    compressing with ffmpeg if needed. Returns (None, None) when it can't
    be made to fit."""
    if os.path.getsize(path) <= DISCORD_MAX_BYTES:
        return path, os.path.basename(path)
    ext = os.path.splitext(path)[1].lower()
    if ext in (".png", ".jpg", ".jpeg", ".webp"):
        out = path + ".fit.jpg"
        cmd = ["ffmpeg", "-v", "error", "-i", path, "-vf",
               "scale='min(1600,iw)':-2", "-q:v", "5", "-y", out]
    elif ext in (".mp4", ".mov", ".webm"):
        out = path + ".fit.mp4"
        cmd = ["ffmpeg", "-v", "error", "-i", path, "-vf",
               "scale='min(1280,iw)':-2", "-c:v", "libx264", "-pix_fmt",
               "yuv420p", "-crf", "28", "-preset", "veryfast",
               "-c:a", "aac", "-y", out]
    else:
        return None, None
    try:
        subprocess.run(cmd, capture_output=True, timeout=120)
    except Exception as e:
        print(f"Compression failed for {path}: {e}", flush=True)
        return None, None
    if os.path.exists(out) and os.path.getsize(out) <= DISCORD_MAX_BYTES:
        return out, os.path.basename(out)
    try:
        os.remove(out)
    except OSError:
        pass
    return None, None


def sweep_generated():
    """Delete generated files older than GENERATED_TTL_SECS (already sent),
    including those in the per-item generated/<message_id>/ dirs, then
    item dirs left empty for that long."""
    if os.path.isdir(GENERATED_DIR):
        now = time.time()
        for fname in os.listdir(GENERATED_DIR):
            fpath = os.path.join(GENERATED_DIR, fname)
            try:
                if os.path.isdir(fpath) and not os.path.islink(fpath):
                    removed = False
                    for sub in os.listdir(fpath):
                        spath = os.path.join(fpath, sub)
                        try:
                            if now - os.lstat(spath).st_mtime \
                                    > GENERATED_TTL_SECS:
                                os.remove(spath)
                                removed = True
                        except OSError:
                            pass
                    # An empty dir only goes once it held expired files
                    # or is old itself: the worker may have just created
                    # it and not written its file yet.
                    if not os.listdir(fpath) and (
                            removed or now - os.path.getmtime(fpath)
                            > GENERATED_TTL_SECS):
                        os.rmdir(fpath)
                elif now - os.path.getmtime(fpath) > GENERATED_TTL_SECS:
                    os.remove(fpath)
            except OSError:
                pass


def sweep_gen_state(state):
    """Drop generation approvals older than GENERATED_TTL_SECS (they are
    single-use and short-lived by design) and bound the sent-file record.
    Approvals from before channel binding (a bare timestamp) go too."""
    approved = state.get("gen_approved", {})
    now = time.time()
    for mid in [m for m, a in approved.items()
                if not isinstance(a, dict)
                or now - a.get("ts", 0) > GENERATED_TTL_SECS]:
        approved.pop(mid, None)
    if len(state.get("gen_sent", [])) > 50:
        state["gen_sent"] = state["gen_sent"][-50:]


def media_permission(state, scope, here, author_matches):
    """may_generate for a message in scope (here: its resolved
    settings): media.enabled is on there, the author is in media.who
    there (an empty list counts as the owners), and no media.per_day
    cap set along the scope is reached (each counts in its own scope,
    see note_media_sent)."""
    if not here["media.enabled"]:
        return False
    if not author_matches(here["media.who"] or "owners"):
        return False
    counts = _media_counts(state)
    for counter, cap in settings().cap_layers("media.per_day", scope):
        sent = counts.get("sent_today", 0) if counter == "everywhere" \
            else (counts.get("counts") or {}).get(counter, 0)
        if sent >= cap:
            print(f"Media generation: daily cap ({cap}) reached; not "
                  f"granting may_generate", flush=True)
            return False
    return True


def note_media_sent(state, scope):
    """Count one reply with generated files sent in scope, for every
    media.per_day counter it adds to (everywhere, its server, or DMs)."""
    TM.count("media_sent")
    counts = _media_counts(state)
    counts["sent_today"] = counts.get("sent_today", 0) + 1
    for counter in MS.Settings.counter_ids(scope):
        if counter != "everywhere":
            c = counts.setdefault("counts", {})
            c[counter] = c.get(counter, 0) + 1


def _media_counts(state):
    """Today's media.per_day counts in state ("media_sent": day,
    sent_today = the everywhere total, counts by server / dms), reset
    each UTC day. Takes over the old media_generation plugin's counts
    once, so a cap reached today stays reached across the update."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    g = state.get("media_sent")
    if not isinstance(g, dict):
        old = (state.get("plugins") or {}).pop("media_generation", None)
        g = old if isinstance(old, dict) else {}
        if state.get("plugins") == {}:
            state.pop("plugins")
        state["media_sent"] = g
    if g.get("day") != today:
        g.clear()
        g.update(day=today, sent_today=0, counts={})
    return g


def _gen_item_dir(item_id):
    """generated/<item_id>/, the only place a queue item's generated files
    may come from, or None for anything but a plain message id (no path
    tricks). Only the generated/ root is realpath'd: an item dir that is
    a symlink to another item's dir must fail the containment check."""
    if not isinstance(item_id, str) or not re.fullmatch(r"[0-9]+", item_id):
        return None
    return os.path.join(os.path.realpath(GENERATED_DIR), item_id)


def _validate_gen_paths(paths, item_id):
    """Keep only paths that exist and resolve inside generated/<item_id>/."""
    item_root = _gen_item_dir(item_id)
    candidates = []
    for p in paths or []:
        if not isinstance(p, str):
            continue
        # Workers sometimes hand over paths relative to $HOME.
        ap = p if os.path.isabs(p) else os.path.join(
            os.path.expanduser("~"), p)
        rp = os.path.realpath(ap)
        if item_root is None or not rp.startswith(item_root + os.sep):
            print(f"Dropping attachment outside generated/{item_id}/: {p}",
                  flush=True)
            continue
        if not os.path.isfile(rp):
            print(f"Dropping missing attachment: {p}", flush=True)
            continue
        candidates.append(rp)
    return candidates


def _discover_generated(state, item_id):
    """Find the newest unsent generated file in generated/<item_id>/. The
    worker often isn't told the local filename (the media tool returns a
    remote URL/handle), so the monitor matches the file itself, but only
    in the item's own directory: a file made for another item (a DM, say)
    is never a candidate. Returns a local path or None."""
    item_root = _gen_item_dir(item_id)
    if item_root is None or not os.path.isdir(item_root):
        return None
    sent = set(state.get("gen_sent", []))
    best = None
    for fname in os.listdir(item_root):
        if fname.endswith(".json") or ".fit." in fname:
            continue  # sidecars and compressed copies, not originals
        fpath = os.path.realpath(os.path.join(item_root, fname))
        if not fpath.startswith(item_root + os.sep):
            continue  # a symlink out of the item dir
        if fpath in sent or not os.path.isfile(fpath):
            continue
        try:
            mt = os.path.getmtime(fpath)
        except OSError:
            continue
        if best is None or mt > best[1]:
            best = (fpath, mt)
    return best[0] if best else None


def _gen_item_id(state, item):
    """The queue item an outbox item answers, as its trigger message id:
    reply_to, resolved through reply_aliases (the worker replies under
    the LAST burst fragment, while approvals and generated/<id>/ use the
    trigger). An explicit "item" field is honored only when it agrees,
    so a reply can never claim another item's approval and files."""
    reply_to = item.get("reply_to")
    resolved = None
    if reply_to is not None:
        resolved = str(state.get("reply_aliases", {}).get(str(reply_to),
                                                          reply_to))
    claimed = item.get("item")
    if claimed is None:
        return resolved
    if resolved is not None and str(claimed) != resolved:
        print(f"Note: outbox item names item {claimed} but replies to "
              f"{reply_to} (item {resolved}); no attachments", flush=True)
        return None
    return str(claimed)


def _gen_approval(state, item_id):
    """The live generation approval for a queue item ({"ts",
    "channel_id"}), or None."""
    a = state.get("gen_approved", {}).get(item_id) if item_id else None
    if not isinstance(a, dict) \
            or time.time() - a.get("ts", 0) > GENERATED_TTL_SECS:
        return None
    return a


def _fetch_complete(url, max_bytes, timeout, kind):
    """Fetch a URL, returning (body_bytes, content_type), or (None, None).

    This network's egress proxy intermittently truncates HTTP/1.1 response
    bodies fetched with Python's urllib: the connection just ends early with
    no error, so a naive read loop mistakes a partial file for a complete
    one. Mitigations: prefer curl when available (reliable here), and
    otherwise verify the byte count against Content-Length, resuming with
    Range requests when short.
    Never sends the bot token: attachment URLs are pre-signed, and the CDN
    drops connections carrying an Authorization header.
    Every URL, and every redirect hop, must pass _fetch_addrs (https, an
    allowlisted media host, public addresses only): message content
    picks these URLs, and a fetch must not become a probe of the LAN or
    the cloud metadata service.
    """
    ua = headers()["User-Agent"]
    # Path 1: curl (proven reliable on this network). No -L: redirects
    # are followed here one hop at a time, so each hop gets the same
    # checks as the first URL, and --resolve pins curl to the addresses
    # just checked (no DNS swap between check and connect). -f makes an
    # HTTP error a failure instead of content: an error page must not be
    # extracted as the "document".
    hop = url
    try:
        for _ in range(FETCH_MAX_REDIRECTS + 1):
            addrs = _fetch_addrs(hop)
            if not addrs:
                print("Refusing %s download from %s: not https, not an "
                      "allowlisted media host, or not a public address" %
                      (kind, hop[:120]), flush=True)
                return None, None
            p = urllib.parse.urlparse(hop)
            pin = "%s:%d:%s" % (p.hostname, p.port or 443, ",".join(
                "[%s]" % a if ":" in a else a for a in addrs))
            r = subprocess.run(
                ["curl", "-sS", "-f", "--proto", "=https", "--resolve", pin,
                 "--max-time", str(timeout),
                 "--max-filesize", str(max_bytes), "-A", ua,
                 "-w", "\n%{http_code}\n%{content_type}\n%{redirect_url}",
                 hop],
                capture_output=True, timeout=timeout + 10)
            if r.returncode != 0 or not r.stdout:
                print("curl %s download failed: %s" %
                      (kind, r.stderr.decode(errors="replace")[:150]),
                      flush=True)
                break
            body, code, ctype, nxt = r.stdout.rsplit(b"\n", 3)
            code = code.decode(errors="replace").strip()
            nxt = nxt.decode(errors="replace").strip()
            if code.startswith("3") and nxt:
                hop = nxt
                continue
            ctype = ctype.decode(errors="replace").split(";")[0].strip().lower()
            if code.startswith("2") and body and len(body) <= max_bytes:
                return bytes(body), ctype
            print("curl %s download failed: HTTP %s" % (kind, code),
                  flush=True)
            break
        else:
            print("curl %s download failed: more than %d redirects" %
                  (kind, FETCH_MAX_REDIRECTS), flush=True)
            return None, None
    except FileNotFoundError:
        pass  # no curl installed; fall through to urllib
    except Exception as e:
        print("curl %s download failed: %s" % (kind, e), flush=True)
    # Path 2: urllib with length verification + Range resume. HTTP errors
    # raise; redirects go through _FetchRedirectHandler's checks.
    if not _fetch_addrs(url):
        return None, None
    opener = urllib.request.build_opener(_FetchRedirectHandler)
    data = bytearray()
    expected = None
    ctype = ""
    for _ in range(4):
        try:
            h = {"User-Agent": ua}
            if data:
                h["Range"] = "bytes=%d-" % len(data)
            req = urllib.request.Request(url, headers=h)
            with opener.open(req, timeout=timeout) as resp:
                if resp.status == 200 and data:
                    data = bytearray()  # server ignored Range; start over
                    expected = None
                ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if resp.status == 206:
                    try:
                        expected = int(resp.headers.get("Content-Range", "").rsplit("/", 1)[1])
                    except (ValueError, IndexError):
                        pass
                elif expected is None:
                    try:
                        expected = int(resp.headers.get("Content-Length") or 0) or None
                    except ValueError:
                        pass
                if expected and expected > max_bytes:
                    return None, None
                data += resp.read(max_bytes + 1 - len(data))
                if len(data) > max_bytes:
                    return None, None
                if expected and len(data) >= expected:
                    break
                if not expected:
                    break  # length unknown; take what we got
        except Exception as e:
            print("urllib %s download attempt failed: %s" % (kind, e), flush=True)
    if expected and len(data) < expected:
        return None, None  # truncated even after resume attempts
    if not data:
        return None, None
    return bytes(data), ctype


def download_media(url, dest_path):
    """Download a media URL to dest_path. Returns the content_type if it is
    an image within the size cap, else None (partial files are removed)."""
    try:
        data, ctype = _fetch_complete(url, MAX_MEDIA_BYTES, 20, "image")
    except Exception as e:
        print("Media download failed: %s" % e, flush=True)
        return None
    if not data or not ctype.startswith("image/"):
        return None
    with open(dest_path, "wb") as f:
        f.write(data)
    return ctype


def download_video(url, dest_path):
    """Download a video URL to dest_path. Returns content_type if video/* and
    within the size cap, else None (partial files are removed)."""
    try:
        data, ctype = _fetch_complete(url, MAX_VIDEO_BYTES, 60, "video")
    except Exception as e:
        print("Video download failed: %s" % e, flush=True)
        return None
    if not data or not ctype.startswith("video/"):
        return None
    with open(dest_path, "wb") as f:
        f.write(data)
    return ctype


def download_document(url, dest_path):
    """Download a document URL to dest_path. Returns the content_type if the
    download completed within the size cap, else None (partial files are
    removed). Content type is not filtered here; the caller decides what to
    extract based on type/extension."""
    try:
        data, ctype = _fetch_complete(url, MAX_DOC_BYTES, 20, "document")
    except Exception as e:
        print("Document download failed: %s" % e, flush=True)
        return None
    if not data:
        return None
    with open(dest_path, "wb") as f:
        f.write(data)
    return ctype or "application/octet-stream"


def extract_document_text(path, content_type, filename):
    """Extract readable text from a downloaded document. Returns the text
    (truncated) or None when nothing usable could be extracted. Runs only
    local subprocess/library extraction: never executes the document."""
    ctype = (content_type or "").lower()
    ext = os.path.splitext(filename.lower())[1]

    def _truncate(t):
        t = (t or "").strip()
        if len(t) > DOC_TEXT_CHARS:
            t = t[:DOC_TEXT_CHARS] + "\n[...truncated...]"
        return t or None

    # Plain text and friends: read directly.
    if ctype.startswith("text/") or ext in TEXT_EXTS or ctype.endswith("+json") \
            or ctype in ("application/json", "application/x-sh"):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return _truncate(f.read())
        except Exception as e:
            print("Text read failed (%s): %s" % (filename, e), flush=True)
            return None

    # PDF: pdftotext (poppler) via subprocess; no Python PDF parsing.
    if ctype == "application/pdf" or ext in PDF_EXTS:
        try:
            proc = subprocess.run(
                ["pdftotext", "-layout", path, "-"],
                capture_output=True, text=True, timeout=PDFTOTEXT_TIMEOUT)
            if proc.returncode == 0 and proc.stdout.strip():
                return _truncate(proc.stdout)
            print("pdftotext found no text in %s" % filename, flush=True)
            return None
        except Exception as e:
            print("pdftotext failed (%s): %s" % (filename, e), flush=True)
            return None

    # DOCX: python-docx paragraph text.
    if ctype == "application/vnd.openxmlformats-officedocument.wordprocessingml.document" \
            or ext in DOCX_EXTS:
        try:
            import docx
            doc = docx.Document(path)
            text = "\n".join(p.text for p in doc.paragraphs)
            return _truncate(text)
        except Exception as e:
            print("docx read failed (%s): %s" % (filename, e), flush=True)
            return None

    return None


def extract_frames(video_path, out_prefix, count=VIDEO_FRAME_COUNT):
    """Extract `count` evenly spaced frames across the clip as PNGs.
    Returns the list of frame paths (may be empty on failure)."""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", *FFMPEG_INPUT_GUARD,
             "-show_entries", "format=duration", "-of", "csv=p=0",
             video_path],
            capture_output=True, text=True, timeout=30)
        duration = float((probe.stdout or "").strip() or 0)
    except Exception:
        duration = 0
    fracs = [(i + 0.5) / count for i in range(count)]
    timestamps = [0.5] if duration <= 0 else [duration * f for f in fracs]
    frames = []
    for i, t in enumerate(timestamps):
        out = "%s_frame%d.png" % (out_prefix, i)
        try:
            subprocess.run(
                ["ffmpeg", "-v", "error", "-ss", str(t),
                 *FFMPEG_INPUT_GUARD, "-i", video_path,
                 "-frames:v", "1", "-y", out],
                capture_output=True, timeout=FFMPEG_TIMEOUT)
            if os.path.exists(out) and os.path.getsize(out) > 0:
                frames.append(out)
        except Exception as e:
            print("Frame extraction failed @%ss: %s" % (t, e), flush=True)
    return frames


def tenor_gif_variant(url):
    """media.tenor.com serves GIF-picker GIFs as .mp4; the .gif variant is
    the same animation as an image the worker can view."""
    base, _, qs = url.partition("?")
    if "media.tenor.com" in base and base.lower().endswith(".mp4"):
        return base[:-4] + ".gif" + ("?" + qs if qs else "")
    return None


def embed_host_allowed(url):
    """True when url's host is an allowlisted media host or a subdomain
    of one. A bare suffix match would also accept lookalikes such as
    evil-tenor.com or attackerdiscordapp.net."""
    try:
        host = (urllib.parse.urlparse(url).hostname or "").rstrip(".")
    except ValueError:
        return False
    return any(host == s or host.endswith("." + s)
               for s in EMBED_MEDIA_HOST_SUFFIXES)


def _fetch_addrs(url):
    """The addresses a media fetch of url may connect to, or None when it
    must not be fetched at all: not https, not an allowlisted media host,
    unresolvable, or resolving to ANY non-public address (private,
    loopback, link-local such as 169.254.169.254, reserved, multicast)."""
    try:
        p = urllib.parse.urlparse(url)
        port = p.port or 443
    except ValueError:
        return None
    if p.scheme != "https" or not embed_host_allowed(url):
        return None
    try:
        infos = socket.getaddrinfo(p.hostname, port,
                                   proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return None
    addrs = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            return None
        if not ip.is_global or ip.is_multicast:
            return None
        if str(ip) not in addrs:
            addrs.append(str(ip))
    return addrs or None


class _FetchRedirectHandler(urllib.request.HTTPRedirectHandler):
    """urllib's side of _fetch_complete's redirect policy: at most
    FETCH_MAX_REDIRECTS hops, each to a URL that passes _fetch_addrs."""
    max_redirections = FETCH_MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        if not _fetch_addrs(newurl):
            raise urllib.error.HTTPError(
                newurl, code, "redirect to a disallowed URL", hdrs, fp)
        return super().redirect_request(req, fp, code, msg, hdrs, newurl)


def collect_media(msg):
    """Download viewable media (images/GIFs) from a message's attachments
    and GIF embeds, and extract text from document attachments (txt, pdf,
    docx, ...). Returns (media, documents, unviewable): media is a list of
    {"path", "content_type", "source"} dicts (video attachments become
    sampled frames with source "video-frame:<name>"); documents is a list
    of {"name", "content_type", "text"} dicts with extracted text inlined
    (truncated); unviewable lists skipped files so the worker can be honest
    about limits."""
    os.makedirs(MEDIA_DIR, exist_ok=True)
    media = []
    documents = []
    unviewable = []
    candidates = []

    video_candidates = []
    doc_candidates = []

    for att in msg.get("attachments", []):
        url = att.get("url")
        if not url:
            continue
        ctype = (att.get("content_type") or "").lower()
        name = att.get("filename") or "attachment"
        base = url.lower().split("?")[0]
        if ctype.startswith("image/") or base.endswith(IMAGE_EXTS):
            candidates.append((url, "attachment:" + name))
        elif ctype.startswith("video/") or base.endswith(VIDEO_EXTS):
            video_candidates.append((url, name))
        elif (ctype.startswith("text/") or base.endswith(TEXT_EXTS)
                or ctype == "application/pdf" or base.endswith(PDF_EXTS)
                or "wordprocessingml" in ctype or base.endswith(DOCX_EXTS)
                or ctype in ("application/json", "application/x-sh")
                or ctype.endswith("+json")):
            doc_candidates.append((url, name, ctype))
        else:
            unviewable.append(name + " (" + (ctype or "unknown type") + ")")

    for emb in msg.get("embeds", []):
        etype = str(emb.get("type", "?"))
        for key in ("image", "video", "thumbnail"):
            u = (emb.get(key) or {}).get("url")
            if not (u and embed_host_allowed(u)):
                continue
            # GIF-picker embeds (type "gifv") carry the animation as an mp4
            # under the "video" key. Route those through the video path
            # (frame extraction): the .gif variant is usually far over the
            # 8MB image cap, while the mp4 is small.
            if key == "video" and (etype == "gifv"
                                   or u.lower().split("?")[0].endswith(".mp4")):
                video_candidates.append((u, "embed:" + etype))
            else:
                candidates.append((u, "embed:" + etype))

    for url, name in video_candidates:
        if len(media) >= MAX_MEDIA_PER_MESSAGE:
            break
        vdest = os.path.join(MEDIA_DIR, "%s_%d.vid" % (msg["id"], len(media)))
        try:
            ctype = download_video(url, vdest)
        except Exception as e:
            print("Video download failed (%s): %s" % (name, e), flush=True)
            continue
        if not ctype:
            unviewable.append(name + " (video: download failed or too large)")
            continue
        frames = extract_frames(vdest, os.path.join(MEDIA_DIR, "%s_%d" % (msg["id"], len(media))))
        try:
            os.remove(vdest)  # keep only the frames
        except OSError:
            pass
        if frames:
            for fp in frames:
                if len(media) >= MAX_MEDIA_PER_MESSAGE:
                    break
                media.append({"path": fp, "content_type": "image/png",
                              "source": "video-frame:" + name})
        else:
            unviewable.append(name + " (video: could not extract frames)")

    for url, name, ctype in doc_candidates:
        if len(documents) >= MAX_DOCS_PER_MESSAGE:
            break
        dest = os.path.join(MEDIA_DIR, "%s_%d.doc" % (msg["id"],
                                                      len(documents)))
        try:
            got_ctype = download_document(url, dest)
        except Exception as e:
            print("Document download failed (%s): %s" % (name, e),
                  flush=True)
            continue
        if not got_ctype:
            unviewable.append(name + " (document: download failed or too large)")
            try:
                os.remove(dest)
            except OSError:
                pass
            continue
        # The extracted text is inlined on the queue item; the downloaded
        # file stays until the hourly sweep. The worker never opens it.
        text = extract_document_text(dest, got_ctype, name)
        if text:
            documents.append({"name": name, "content_type": got_ctype,
                              "text": text})
        else:
            unviewable.append(name + " (document: could not extract text)")

    seen = set()
    for url, label in candidates:
        if len(media) >= MAX_MEDIA_PER_MESSAGE:
            break
        urls_to_try = [url]
        gif_variant = tenor_gif_variant(url)
        if gif_variant:
            urls_to_try.insert(0, gif_variant)
        for u in urls_to_try:
            if u in seen:
                continue
            seen.add(u)
            dest = os.path.join(MEDIA_DIR, "%s_%d.img" % (msg["id"], len(media)))
            try:
                ctype = download_media(u, dest)
            except Exception as e:
                print("Media download failed (%s): %s" % (label, e), flush=True)
                continue
            if ctype:
                final = dest + CTYPE_EXT.get(ctype, ".img")
                os.rename(dest, final)
                media.append({"path": final, "content_type": ctype, "source": label})
                break
    return media, documents, unviewable


def sweep_media():
    """Delete downloaded media older than MEDIA_TTL_SECS."""
    if not os.path.isdir(MEDIA_DIR):
        return
    now = time.time()
    for fname in os.listdir(MEDIA_DIR):
        fpath = os.path.join(MEDIA_DIR, fname)
        try:
            if now - os.path.getmtime(fpath) > MEDIA_TTL_SECS:
                os.remove(fpath)
        except OSError:
            pass


def record_history(state, channel_id, messages):
    """Keep a rolling window of recent messages per channel/DM so the reply
    worker can draft with conversational context instead of starting cold.
    The triggering message itself is recorded here too; enqueue_message
    excludes it when attaching history to a queue item."""
    hist = state.setdefault("history", {}).setdefault(channel_id, [])
    seen_ids = {h["id"] for h in hist}
    for m in sorted(messages, key=lambda x: int(x["id"])):
        if m["id"] in seen_ids:
            continue
        author = m.get("author", {}) or {}
        text = (m.get("content") or "")[:HISTORY_TEXT_CHARS]
        if not text and not (m.get("attachments") or m.get("embeds")):
            continue
        entry = {
            "id": m["id"],
            "author": author.get("username", "?"),
            "author_id": author.get("id"),
            "bot": is_bot_author(author),
            "text": text or "[shared media]",
            "ts": m.get("timestamp"),
        }
        # The name people see (server nickname or global display name),
        # when it differs from the username: what someone typing "@Name"
        # by hand is most likely to use.
        display = ((m.get("member") or {}).get("nick")
                   or author.get("global_name"))
        if display and display != entry["author"]:
            entry["display"] = display
        hist.append(entry)
        seen_ids.add(m["id"])
    # Sorted: the poll re-reads messages the gateway already recorded,
    # and an old one re-added must not land after newer ones.
    hist.sort(key=lambda h: int(h["id"]))
    state["history"][channel_id] = hist[-HISTORY_PER_CHANNEL:]


# ---------------------------------------------------------------------------
# REST watermark vs gateway dedupe. state["last_seen"][cid] is the REST
# poll's watermark and only the poll advances it (or seeding, the first
# time a channel is seen). The gateway used to advance it too, so after a
# session that could not be resumed lost events, the first message past
# the gap moved the watermark beyond the missed ones and the poll never
# read them. Now messages the gateway handled above the watermark are
# remembered in state["gw_seen"][cid], the poll skips those, and both
# paths skip anything at or below the watermark: every message is
# processed once, by whichever path sees it first.
# ---------------------------------------------------------------------------
GW_SEEN_PER_CHANNEL = 1000
GW_SEEN_MAX_AGE_SECS = 24 * 3600


def gateway_seen(state, cid, mid):
    return mid in state.get("gw_seen", {}).get(cid, ())


def note_gateway_seen(state, cid, mid):
    """Record a message the gateway path handled. The first message ever
    seen in a channel seeds its REST watermark instead (history before it
    counts as seen, as with seed_channel_seen)."""
    if cid not in state["last_seen"]:
        state["last_seen"][cid] = mid
        return
    ids = state.setdefault("gw_seen", {}).setdefault(cid, [])
    ids.append(mid)
    del ids[:-GW_SEEN_PER_CHANNEL]


def advance_rest_watermark(state, cid, mid):
    """Move a channel's REST watermark to mid; gateway ids at or below it
    are covered by the watermark now and are dropped."""
    state["last_seen"][cid] = mid
    ids = state.get("gw_seen", {}).get(cid)
    if ids is not None:
        ids[:] = [i for i in ids if int(i) > int(mid)]
        if not ids:
            del state["gw_seen"][cid]


def prune_gateway_seen(state):
    """Drop gateway ids no poll will ever re-read (channels nobody polls,
    such as DMs under dm_policy: open)."""
    now = time.time()
    for cid in list(state.get("gw_seen", {})):
        ids = [i for i in state["gw_seen"][cid]
               if now - _snowflake_ts(i) < GW_SEEN_MAX_AGE_SECS]
        if ids:
            state["gw_seen"][cid] = ids
        else:
            del state["gw_seen"][cid]


def check_channel(channel_id, channel_name, state, guild_id=None):
    """Check for new messages in a channel and stage its triggers (see
    stage_found). Returns the (msg, kind) pairs staged (kind as in
    trigger_kind). Messages the gateway already handled are skipped."""
    last_seen = state["last_seen"].get(channel_id)
    if not last_seen:
        # Never seeded (discovery normally does it): history is history.
        seed_channel_seen(channel_id, channel_name, state)
        return []

    # 100 (Discord's max) so the poll keeps up with busy channels: it now
    # re-reads every message the gateway delivered since the last poll.
    params = {"limit": 100, "after": last_seen}

    query = urllib.parse.urlencode(params)
    try:
        messages = api_get(f"/channels/{channel_id}/messages?{query}")
    except Exception as e:
        print(f"Error reading #{channel_name}: {e}", flush=True)
        return []

    if not messages:
        return []

    record_history(state, channel_id, messages)

    newest = max(messages, key=lambda m: int(m["id"]))
    gw_done = set(state.get("gw_seen", {}).get(channel_id, ()))
    advance_rest_watermark(state, channel_id, newest["id"])

    triggers = []
    for msg in sorted(messages, key=lambda m: int(m["id"])):
        if msg["id"] in gw_done:
            continue  # the gateway path already handled it
        kind = classify_message(state, msg, channel_name, guild_id)
        if kind:
            stage_found(state, msg, channel_name, guild_id, kind)
            triggers.append((msg, kind))

    return triggers


# Message types people write: DEFAULT and REPLY. Everything else in a
# channel is a system notice ("X joined the server", a pin, ...).
CONVERSATION_TYPES = (0, 19)


def classify_message(state, msg, channel_name, guild_id=None):
    """Classify a single message exactly the way check_channel's loop does.

    Shared by the REST poll path and the gateway fast path so both ingest
    routes behave identically. Returns "mention", "follow_up", or None
    (folded into a burst, skipped, or not actionable).
    """
    if predates_join(state, msg, guild_id):
        # Older than the bot's arrival in this guild: never a trigger,
        # never a fragment. It can still reach the worker as history.
        return None
    if msg.get("type", 0) not in CONVERSATION_TYPES:
        # Discord's system notices ("X pinned a message", "X started a
        # thread", ...) carry the acting user as author: never a
        # trigger, a follow-up or a fragment.
        return None
    author = msg.get("author", {}) or {}
    if author.get("id") == BOT_ID:
        return None  # the bot's own messages: never triggers or fragments
    TM.count("messages_seen")
    if is_bot_author(author) and settings().get(
            "who.bots", scope_for(guild_id, msg.get("channel_id"))) \
            == "ignore":
        if addresses_bot(msg):
            note_refused(msg, channel_name, "who.bots: ignore")
        return None
    if hold_fragment(state, msg.get("channel_id"), msg):
        return None  # folded into a burst already being coalesced
    kind, refusal = trigger_verdict(msg, guild_id, state)
    if refusal:
        note_refused(msg, channel_name, refusal)
    return kind




# Discord does not let bots list DM channels: GET /users/@me/channels always
# returns an empty list for bot tokens. Instead, resolve each known user's DM
# channel directly with POST /users/@me/channels (create-or-get: returns the
# existing channel if there is one, creates an empty one otherwise, and never
# sends a message).
#
# The owners' DM channels, and those of the people who.dms_from names
# (a list of user ids), are resolved for REST polling (gap repair and
# the sweep) unless DMs are off (dms.who.replies: off). Anyone else's DMs
# cannot be enumerated (there is no DM list endpoint for bots), so they
# arrive via the gateway only, and only as who.dms_from allows: under the
# default (owners) the bot never even records a stranger's DM, so there
# is nothing to decline or deflect.
def dm_listed_people():
    """The people who.dms_from names (a list of user ids), else []."""
    v = settings().get("who.dms_from", MS.Scope.dm())
    return [str(u) for u in v] if isinstance(v, list) else []


def dm_poll_user_ids():
    if not dms_enabled():
        return []
    return sorted(set(OWNER_USER_IDS) | set(dm_listed_people()))


def dm_swept_author(uid):
    """Whose DMs the poll and the sweep cover (and so whose refused DMs
    are ledgered): the owners and the people who.dms_from names."""
    return str(uid) in OWNER_USER_IDS or str(uid) in dm_listed_people()


# The DM channels the poll has resolved, {channel_id: recipient name}:
# the reconciliation sweep covers these as well as server channels.
# DM_CHANNEL_USER: {channel_id: recipient user id} (an owner's DM or a
# listed person's: never_share treats only the owners' as private).
DM_CHANNELS = {}
DM_CHANNEL_USER = {}


def list_dm_channels():
    """Return [(channel_id, recipient_username)] for known users' DM channels."""
    out = []
    for uid in dm_poll_user_ids():
        try:
            c = api_post("/users/@me/channels", {"recipient_id": uid})
        except Exception as e:
            print(f"DM channel resolve failed for {uid}: {e}", flush=True)
            continue
        if c and c.get("id"):
            recips = c.get("recipients") or []
            name = recips[0].get("username", uid) if recips else uid
            out.append((c["id"], name))
            DM_CHANNELS[c["id"]] = name
            DM_CHANNEL_USER[c["id"]] = str(uid)
    return out


def check_dm_channel(dm_id, dm_name, state):
    """Return unseen non-bot messages in a DM channel. In a DM every message
    is addressed to the bot, so no mention filtering is needed. The first
    time a DM channel is read (first run, or state.json deleted) its
    history counts as already seen, like a new channel's: the backlog is
    never one big fresh trigger. Messages the gateway already handled
    are skipped."""
    try:
        messages = api_get(f"/channels/{dm_id}/messages?limit=20")
    except Exception as e:
        print(f"DM poll error #{dm_name}: {e}", flush=True)
        return []
    last_seen = state["last_seen"].get(dm_id)
    if not messages:
        if not last_seen:
            state["last_seen"][dm_id] = now_snowflake()
        return []

    record_history(state, dm_id, messages)

    if not last_seen:
        newest = max(messages, key=lambda m: int(m["id"]))
        state["last_seen"][dm_id] = newest["id"]
        print(f"Tracking DM with {dm_name} from message {newest['id']}.",
              flush=True)
        return []
    new = [m for m in messages if int(m["id"]) > int(last_seen)]
    gw_done = set(state.get("gw_seen", {}).get(dm_id, ()))
    if new:
        newest = max(new, key=lambda m: int(m["id"]))
        advance_rest_watermark(state, dm_id, newest["id"])

    out = []
    for m in sorted(new, key=lambda m: int(m["id"])):
        if m.get("author", {}).get("id") == BOT_USER_ID \
                or m["id"] in gw_done \
                or m.get("type", 0) not in CONVERSATION_TYPES:
            continue
        TM.count("messages_seen")
        if not dm_sender_allowed(m.get("author")):
            note_dm_refused(m)
            continue
        if not hold_fragment(state, dm_id, m):
            out.append(m)
    return out


def note_dm_refused(msg):
    """A DM the DM settings decline (dm_sender_allowed). Only an owner's
    or a who.dms_from-listed person's is ledgered (see note_refused): the
    sweep covers only their DM channels, and a stranger's DMs stay
    unrecorded."""
    author = msg.get("author", {}) or {}
    if dm_swept_author(author.get("id")) and not is_bot_author(author):
        note_refused(msg, "DM with " + (author.get("username") or "?"),
                     dm_refusal(author) or "who.replies is off for DMs")


def bot_run_length(history, msg):
    """How many consecutive bot-authored messages (any bot, this one
    included) end at msg in this channel's history, with no human in
    between. 0 when msg is from a human."""
    run = 0
    older = [h for h in history if int(h["id"]) < int(msg["id"])]
    for h in reversed(older + [{"bot": is_bot_author(msg.get("author"))}]):
        if not h.get("bot"):
            break
        run += 1
    return run


def conversation_participants(history, msg, reply_ctx):
    """Everyone visible in the conversation, as [{name, display?, id, bot,
    you?}], so the worker can address them with <@id>: a bare "@Name" in
    Discord text notifies nobody."""
    people = {}

    def add(pid, name, bot, display=None):
        if not pid or pid in people:
            return
        p = {"name": name or "?", "id": pid, "bot": bool(bot)}
        if display:
            p["display"] = display
        if pid == BOT_ID:
            p["you"] = True
        people[pid] = p

    author = msg.get("author", {}) or {}
    add(author.get("id"), author.get("username"), is_bot_author(author),
        (msg.get("member") or {}).get("nick") or author.get("global_name"))
    for h in reversed(history):
        add(h.get("author_id"), h.get("author"), h.get("bot"),
            h.get("display"))
    if reply_ctx:  # only when not already known from history
        add(reply_ctx.get("author_id"), reply_ctx.get("author"),
            reply_ctx.get("is_bot"))
    add(BOT_ID, BOT_NAME, True)
    return list(people.values())


def link_mentions(text, channel_id, state):
    """Safety net for addressing: turn a bare "@Name" for someone in this
    channel's recent history into a real <@id> mention, so the person or
    agent the reply addresses actually gets notified. Matches usernames
    and display names, case-insensitively, longest first."""
    names = {}
    for h in state.get("history", {}).get(channel_id, []):
        pid = h.get("author_id")
        if not pid or pid == BOT_ID:
            continue
        for n in (h.get("author"), h.get("display")):
            if n and n.lower() not in ("everyone", "here"):
                names[n.lower()] = pid
    if not names or "@" not in text:
        return text
    pattern = re.compile(
        r"(?<![\w<])@(" + "|".join(re.escape(n) for n in
                                   sorted(names, key=len, reverse=True))
        + r")(?![\w])", re.IGNORECASE)
    return pattern.sub(lambda m: f"<@{names[m.group(1).lower()]}>", text)


def settings_card(scope, here, author_id, from_bot, channel_id,
                  exchange=0):
    """The per-message settings card for the worker (item "settings"):
    where it is, who is speaking, who can read the reply, and the style
    and privacy settings that steer it. In an owner's DM, memory is
    always "everything"."""
    dm = scope.is_dm
    owner = str(author_id) in OWNER_USER_IDS
    card = {"where": "dm" if dm else "server"}
    if not dm:
        info = CHANNELS.get(channel_id) or {}
        card["server"] = (GUILDS.get(scope.guild_id) or {}).get("name") \
            or info.get("guild_name") or scope.guild_id
        card["channel"] = info.get("name") or channel_id
    card["speaker"] = "owner" if owner else ("bot" if from_bot
                                             else "member")
    card["audience"] = "private" if dm else "public"
    card["memory"] = "everything" if (dm and owner) \
        else here["privacy.memory"]
    card["length"] = here["style.length"]
    card["notes"] = here["style.notes"]
    if dm and owner and not from_bot:
        # Settings requests (SETTINGS REQUESTS in the worker prompt):
        # the places the owner can name, with their ids.
        with _DIRECTORY_LOCK:
            card["places"] = NL.card_places(_DIRECTORY)
    if from_bot:
        card["bot_exchange"] = {"count": exchange + 1,
                                "limit": here["who.bot_exchange_limit"]}
    return card


def note_item_meta(state, mid, **meta):
    """Remember who a queued item answers (bounded): the send path opens
    follow-up windows and applies the never_share backstop from it."""
    m = state.setdefault("item_meta", {})
    m[mid] = meta
    for old in list(m)[:-300]:
        del m[old]


def enqueue_message(msg, cname, state, queue, dm=False, fragments=None,
                    guild_id=None, follow_up=False):
    """Run one message (plus any burst fragments coalesced with it) through
    the quarantine/rate-limit/media pipeline and append a SINGLE item to
    the queue. follow_up: staged from a conversational follow-up window
    (the item says follow_up: true unless some message of the burst
    addresses the bot directly). Returns True if queued."""
    fragments = [f for f in (fragments or []) if f["id"] != msg["id"]]
    if any(q["message_id"] == msg["id"] for q in queue):
        return False
    author_id = msg.get("author", {}).get("id")
    author_name = msg.get("author", {}).get("username")
    # Every outcome below is recorded for the trigger AND each fragment:
    # a fragment can be a mention too, and one with no ledger entry would
    # be requeued alone by the sweep.
    burst_ids = [msg["id"]] + [f["id"] for f in fragments]

    # 0. Hard gates, re-checked here because every path (poll, gateway,
    #    restart flush, sweep) funnels through this function.
    if predates_join(state, msg, guild_id):
        ledger_record_many(burst_ids, "skipped", "predates bot join",
                           channel_id=msg.get("channel_id"),
                           author=author_name, author_id=author_id)
        return False
    chan_hist = state.get("history", {}).get(msg["channel_id"], [])
    from_bot = is_bot_author(msg.get("author"))
    bot_turns = bot_run_length(chan_hist, msg)
    scope = scope_for(guild_id, msg["channel_id"], dm)
    here = settings().resolve(scope)
    # who.bot_exchange_limit: the bot's consecutive replies to bots in
    # this channel since a human spoke.
    exchange = own_replies_in_run(chan_hist, msg) if from_bot else 0
    limit = here["who.bot_exchange_limit"]
    if from_bot and limit is not None and exchange >= limit:
        ledger_record_many(burst_ids, "skipped",
                           f"bot exchange limit ({limit}) reached",
                           channel_id=msg.get("channel_id"),
                           author=author_name, author_id=author_id,
                           detail={"limit": limit})
        print(f"Not answering bot {author_name} in {cname}: the bot "
              f"exchange limit ({limit}) is reached.", flush=True)
        return False
    suspicious = here["privacy.suspicious"]

    def hold_suspicious(matched):
        if suspicious == "ignore":
            ledger_record_many(burst_ids, "skipped",
                               f"suspicious, ignored: {matched}",
                               channel_id=msg.get("channel_id"),
                               author=author_name, author_id=author_id)
            print(f"Ignored a suspicious message from {author_name} in "
                  f"{cname} (privacy.suspicious: ignore).", flush=True)
        else:
            quarantine(msg, cname, matched, fragments)
    # The worker sees the whole burst as one message: fragments joined in
    # order under the trigger's text.
    parts = [msg.get("content", "")]
    parts += [f.get("content", "") for f in fragments]
    content = "\n".join(p for p in parts if p)

    # 1. Injection pre-filter -> quarantine (privacy.suspicious:
    #    hold-for-review) or drop (ignore), no auto-reply (combined text).
    if suspicious != "answer":
        matched = is_suspicious(content)
        if matched:
            hold_suspicious(matched)
            return False

    # 2. Rate limits (also bound token spend). One charge per burst, not
    #    per fragment.
    #    A capped burst is dropped at once, not retried: ledgered as
    #    skipped (terminal), so flush_pending drops its pending entry
    #    and the author's later messages are judged on their own.
    capped = rate_block(state, author_id, msg["channel_id"], scope)
    if capped:
        ledger_record_many(burst_ids, "skipped", "daily cap reached",
                           channel_id=msg.get("channel_id"),
                           author=author_name, author_id=author_id,
                           detail=capped)
        return False
    rate_record(state, author_id, scope)

    # 3. Download viewable media so the worker can see it, and extract
    #    document text so the worker can read attachments (trigger + every
    #    fragment).
    media, documents, unviewable = [], [], []
    for m in [msg] + fragments:
        md, dc, uv = collect_media(m)
        media += md
        documents += dc
        unviewable += uv

    # 3b. Reply context: when the trigger used Discord's reply feature, say
    #     whose message it was replying to so the worker knows whether the
    #     message was actually addressed to the bot or to someone else.
    reply_ctx = get_reply_context(msg)

    # 3c. The worker reads extracted document text and the replied-to
    #     message as well as the message itself, so they get the same
    #     injection pre-filter. The bot's own replied-to message is
    #     skipped: everyday phrasing in it ("you are now set up") would
    #     quarantine every reply to it.
    if suspicious != "answer":
        extra_texts = [(f"document {d.get('name')}", d.get("text") or "")
                       for d in documents]
        if reply_ctx and not reply_ctx.get("is_bot"):
            extra_texts.append(("replied-to message",
                                reply_ctx.get("content") or ""))
        for where, text in extra_texts:
            matched = is_suspicious(text)
            if matched:
                hold_suspicious(f"{matched} (in {where})")
                return False

    # 4. Attach recent conversation history (excluding the trigger and its
    #    fragments) so the worker replies with context.
    history = [h for h in chan_hist
               if h["id"] not in burst_ids][-HISTORY_ATTACHED:]

    # 5. Generation permission (media.*) and plugin gates. may_generate
    #    is PERMISSION, not a decision: the worker still judges from the
    #    full conversation context whether generation is actually wanted,
    #    and generates at most once per queue item. Queue time only
    #    records the permission, plus the single-use approval below
    #    (keyed by message id, because the worker's outbox item does not
    #    carry the flag); it must not claim anywhere that generation is
    #    starting. Plugins may annotate the item via the gate_message
    #    hook, but never grant may_generate.
    def author_matches(who):
        return people_match(who, author_id, lambda: (
            set((msg.get("member") or {}).get("roles") or [])
            if "roles" in (msg.get("member") or {})
            else member_roles(guild_id, author_id)))
    may_generate = media_permission(state, scope, here, author_matches)
    extra = {}
    for res in call_hook("gate_message",
                         {"state": state, "owners": OWNER_USER_IDS,
                          "author_matches": author_matches},
                         msg, guild_id, scope=scope):
        if isinstance(res, dict):
            extra.update(res)
    extra.pop("may_generate", None)
    if may_generate:
        # Bound to the channel too: the file may only go back where the
        # request came from (an image made in a DM never posts publicly).
        state.setdefault("gen_approved", {})[msg["id"]] = {
            "ts": time.time(), "channel_id": msg["channel_id"]}

    # 6. Owner commands: stamped for the owners' messages only, from the
    #    verified user ID in code. The worker decides with natural-language
    #    understanding whether the message asks the bot to DO something.
    owner_command = author_id in OWNER_USER_IDS \
        and bool(here["abilities.owner_commands"])
    # The worker replies under the LAST fragment, but the ledger and the
    # sweep track the trigger: remember which trigger each fragment
    # belongs to so a reply (or a silent pass) settles the whole burst.
    alias_fragments(state, msg["id"], fragments)

    item = {
        "message_id": msg["id"],
        "channel_id": msg["channel_id"],
        "channel_name": cname,
        "guild_id": guild_id,
        "kind": "dm" if dm else "mention",
        "author": msg["author"].get("username"),
        "author_id": author_id,
        "content": content,
        "timestamp": msg.get("timestamp"),
        "media": media,
        "documents": documents,
        "unviewable": unviewable,
        "history": history,
        "may_generate": may_generate,
        "owner_command": owner_command,
        "burst": len(fragments) > 0,
        "burst_fragments": [
            {"message_id": f["id"], "content": f.get("content", ""),
             "timestamp": f.get("timestamp")} for f in fragments
        ],
        "reply_to": reply_ctx,
        # Another agent addressed the bot. bot_turns counts consecutive
        # bot messages (this one included) since a human last spoke.
        "from_bot": from_bot,
        "bot_turns": bot_turns,
        "participants": conversation_participants(chan_hist, msg,
                                                  reply_ctx),
        # When the monitor queued it: the typing cap and the latency log
        # measure from here.
        "queued_at": note_queued(state, msg["id"]),
        # Triggered only by the conversational follow-up window.
        "follow_up": follow_up and not any(
            is_mention(m) or is_reply_to_bot(m) for m in [msg] + fragments),
        "settings": settings_card(scope, here, author_id, from_bot,
                                  msg["channel_id"], exchange),
    }
    item.update(extra)
    queue.append(item)
    burst_note = f" (+{len(fragments)} fragment(s))" if fragments else ""
    # An owner's own DM: every outbox item that answers it, settles it
    # or posts in its channel must carry its nonce (owner_dm_gate).
    # (The wake hook's test too: settings.speaker "owner" in a DM.)
    owner_dm = bool(dm and str(author_id) in OWNER_USER_IDS)
    note_item_meta(state, msg["id"], author_id=author_id,
                   channel_id=msg["channel_id"], bot=from_bot, dm=dm,
                   kind=item["kind"], owner_dm=owner_dm,
                   # Settings requests: never from a forwarded message;
                   # attachments or a reply to someone else's message
                   # always need the owner's Apply.
                   forwarded=any(is_forwarded(m) for m in [msg] + fragments),
                   has_inputs=bool(media or documents) or bool(
                       reply_ctx and not reply_ctx.get("is_bot")))
    if owner_dm:
        # The capability for this owner DM (written for the wake hook
        # before the item reaches the queue file).
        nl_mint_nonce(state, msg["id"], msg["channel_id"],
                      [f["id"] for f in fragments])
    print(f"Queued {item['kind']}{' (follow-up)' if item['follow_up'] else ''}"
          f" from {msg['author'].get('username')} in "
          f"{cname}{burst_note}: {content[:80]}", flush=True)
    # (Typing comes from the typing thread, via typing_refresh.)
    ledger_record_many(burst_ids, "queued",
                       channel_id=msg.get("channel_id"),
                       author=msg.get("author", {}).get("username"),
                       author_id=author_id)
    return True


def note_ingested(state, mid):
    """Remember when the monitor first processed trigger mid (staged
    it), bounded like queued_at, for the latency log's ingest/hold
    split."""
    seen = state.setdefault("ingested_at", {})
    seen.setdefault(mid, time.time())
    for old in list(seen)[:-200]:
        del seen[old]


def note_queued(state, mid):
    """Remember when mid was queued (bounded), for the latency log even
    when a worker removes the queue item itself. Returns the time."""
    now = time.time()
    queued = state.setdefault("queued_at", {})
    queued[mid] = now
    for old in list(queued)[:-200]:
        del queued[old]
    return now


def _settle(state, item, status, reason="", replied=False):
    """Record an outbox item's outcome under its reply_to AND every other
    message of the burst it belongs to (reply_aliases): the worker
    replies under the last fragment, while the ledger and the sweep track
    the trigger, and a fragment left unsettled would be requeued alone.
    replied=True also adds all those ids to replied_to (send dedup)."""
    reply_to = item.get("reply_to")
    ids = []
    if reply_to:
        aliases = state.get("reply_aliases", {})
        trigger = aliases.get(reply_to, reply_to)
        ids = [reply_to]
        ids += [m for m in [trigger] + [f for f, t in aliases.items()
                                        if t == trigger]
                if m not in ids]
    # The queue item it answers, so a second worker's draft for the
    # same item is suppressed too.
    if item.get("item") and item["item"] not in ids:
        ids.append(item["item"])
    if not ids:
        return
    for mid in ids:
        if replied and mid not in state["replied_to"]:
            state["replied_to"].append(mid)
    ledger_record_many(ids, status, reason,
                       channel_id=item.get("channel_id"),
                       author=item.get("author"),
                       author_id=item.get("author_id"))


def _answered_id(state, item):
    """The queue item (its message_id) an outbox item answers: its "item"
    field, else the burst trigger its reply_to belongs to, else reply_to.
    None when it names neither."""
    if item.get("item"):
        return item["item"]
    reply_to = item.get("reply_to")
    return state.get("reply_aliases", {}).get(reply_to) or reply_to


def _outbox_problem(item):
    """Why an outbox item cannot be processed, or None. Ids given as
    numbers are normalized to strings in place. A wrong type used to
    raise out of process_outbox before its final write, which blocked the
    outbox and re-sent the earlier items with no reply_to every cycle."""
    if not isinstance(item, dict):
        return "not a JSON object"
    for field in ("channel_id", "reply_to", "item"):
        value = item.get(field)
        if value in (None, "") and field != "channel_id":
            item.pop(field, None)
            continue
        if not _is_snowflake(value):
            return f"{field} is not a Discord id"
        item[field] = str(value)
    if item.get("text") is not None and not isinstance(item["text"], str):
        return "text is not a string"
    for field in ("parts", "reactions", "attachments"):
        value = item.get(field)
        if value is not None and not (
                isinstance(value, list)
                and all(isinstance(v, str) for v in value)):
            return f"{field} is not a list of strings"
    if item.get("_retry") is not None \
            and not isinstance(item["_retry"], dict):
        return "_retry was edited (leave it alone)"
    if "settings" in item or any(str(k).startswith("_nl") for k in item):
        # A settings request (settings_nl.py): its own shape rules; the
        # text is an optional lead-in.
        problems = NL.shape_problems(item, PLUGINS_DIR)
        return "settings: " + "; ".join(problems) if problems else None
    if item.get("silent"):
        if not (item.get("reply_to") or item.get("item")):
            return "silent item names no message (reply_to or item)"
        return None
    # Only silent: true (or a reactions-only item) is a deliberate
    # no-reply. Anything else that would send nothing (say, the reply in
    # a "content" field) was a mistake: settling it as silence would
    # leave the mention unanswered for good.
    reactions = [e for e in item.get("reactions") or [] if e.strip()]
    if not normalize_parts(item):
        if not reactions:
            return ("nothing to send: no text, parts, or reactions, and "
                    "not silent: true")
        if not item.get("reply_to"):
            return "reactions without reply_to"
    return None


# An outbox file that does not parse but changed this recently is most
# likely a worker writing it in place (not via a rename): wait for it.
OUTBOX_SETTLE_SECS = 10


def _outbox_load(path):
    """The items in an outbox file, or None while it is mid-write (see
    OUTBOX_SETTLE_SECS). A file that is not a JSON array (a bad write) is
    set aside in the rejected file instead of blocking every later
    reply."""
    if not os.path.exists(path):
        return []
    with open(path, errors="replace") as f:
        raw = f.read()
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
        reason = "outbox file is not a JSON array"
    except ValueError as e:
        try:
            if time.time() - os.path.getmtime(path) < OUTBOX_SETTLE_SECS:
                return None
        except OSError:
            return None
        data, reason = raw[:20000], f"outbox file is not valid JSON ({e})"
    print(f"Rejected {os.path.basename(path)}: {reason}", flush=True)
    _reject_outbox([(data, reason)])
    return []


_NONCE_LIKE = re.compile(r"[0-9a-f]{32,64}")


def _scrub_nonces(value):
    """value (any JSON, or raw text) with every nonce-like string (32 to
    64 hex digits) blanked: what goes to the rejected file never holds a
    live owner-DM nonce, however the outbox was mangled."""
    if isinstance(value, str):
        return _NONCE_LIKE.sub("(redacted)", value)
    if isinstance(value, list):
        return [_scrub_nonces(v) for v in value]
    if isinstance(value, dict):
        return {k: _scrub_nonces(v) for k, v in value.items()}
    return value


def _reject_outbox(rejects):
    """Set malformed outbox items aside for the owner's review."""
    try:
        with open(OUTBOX_REJECTED_FILE) as f:
            kept = json.load(f)
        kept = kept if isinstance(kept, list) else []
    except (OSError, ValueError):
        kept = []
    now = datetime.now(timezone.utc).isoformat()
    kept += [{"rejected_at": now, "reason": reason,
              "item": _scrub_nonces(item)} for item, reason in rejects]
    atomic_write(OUTBOX_REJECTED_FILE, kept[-100:])


def _log_latency(state, qid, picked, sent):
    """One line per answered item: where the time went, from the trigger
    message (its snowflake time) to the monitor first processing it
    (ingest: the gateway or the poll delivering it, and the main loop
    getting to it), to queued (hold: the burst coalescing), to the
    outbox pickup (worker), to sent. An item that was never staged (a
    sweep recovery) has no hold. Our clock can trail Discord's
    snowflake time by a fraction of a second, so t0 is clamped to the
    first local timestamp: ingest shows 0, never negative, and total
    stays the sum of the parts."""
    t0 = _snowflake_ts(qid)
    queued = state.get("queued_at", {}).pop(qid, None)
    ingested = state.get("ingested_at", {}).pop(qid, None)
    if not t0:
        return
    if queued:
        ingested = min(ingested or queued, queued)
        t0 = min(t0, ingested)
        print(f"Latency {qid}: ingest {ingested - t0:.1f}s, hold "
              f"{queued - ingested:.1f}s, worker {picked - queued:.1f}s, "
              f"send {sent - picked:.1f}s, total {sent - t0:.1f}s",
              flush=True)
    else:
        t0 = min(t0, picked)
        print(f"Latency {qid}: send {sent - picked:.1f}s, total "
              f"{sent - t0:.1f}s", flush=True)
    TM.latency(sent - t0)


# A failed send is retried with growing gaps, but only for this long:
# Discord's nonce check (send_nonce) only remembers a nonce for a few
# minutes, and past that a retry of a send that actually landed would
# post it twice.
SEND_RETRY_SECS = 90


def process_outbox(state):
    """Send replies drafted by the worker, with output guardrails + dedup.

    Under the shared lock, the items are taken out of reply_outbox.json
    into OUTBOX_PROCESSING_FILE (merged back on the next pass if the
    monitor dies mid-send). They are sent with no lock held, since a
    multi-part send takes several seconds. Then, under the lock again,
    only the items still retrying go back, ahead of whatever the worker
    appended meanwhile: writing back the copy read before the sends
    deleted those appends. Settled items (sent, silent, or skipped) also
    leave the queue here, so the worker never has to rewrite it."""
    try:
        with open(OUTBOX_FILE) as f:
            idle = json.load(f) == []
    except FileNotFoundError:
        idle = True
    except (OSError, ValueError):
        idle = False  # unreadable: _outbox_load sets it aside
    if idle and not os.path.exists(OUTBOX_PROCESSING_FILE) \
            and not state.get("released"):
        return
    picked = time.time()
    with shared_lock():
        fresh = _outbox_load(OUTBOX_FILE)
        if fresh is None:
            return  # mid-write; the next pass gets it
        # Held replies an owner chose to send ride along (see hold_reply).
        outbox = (_outbox_load(OUTBOX_PROCESSING_FILE) or []) + fresh \
            + state.pop("released", [])
        atomic_write(OUTBOX_PROCESSING_FILE, outbox)
        atomic_write(OUTBOX_FILE, [])

    remaining = []
    rejects = []
    settled = []
    index = queue_index(state)
    frags = index["frags"]
    for item in outbox:
        problem = _outbox_problem(item)
        if problem:
            where = item.get("channel_name") if isinstance(item, dict) \
                else None
            print(f"Rejected outbox item for {where or '?'}: {problem} "
                  f"(set aside in "
                  f"{os.path.basename(OUTBOX_REJECTED_FILE)})", flush=True)
            rejects.append((redact_nonces(item), problem))
            continue
        # Owner DMs first, before dedup, holds or anything that settles:
        # only the owner-DM-only wake holding the DM's nonce may touch it.
        refused = owner_dm_gate(state, item, index)
        if refused:
            print(f"Refused outbox item for {item.get('channel_name') or '?'}"
                  f": {refused}; the owner DM stays open (set aside in "
                  f"{os.path.basename(OUTBOX_REJECTED_FILE)})", flush=True)
            rejects.append((redact_nonces(item), refused))
            continue
        reply_to = item.get("reply_to")
        qid = _answered_id(state, item)
        typing_keys = ["item:" + qid] if qid else []
        # Dedup: never touch the same message twice (overlapping workers)
        if (reply_to and reply_to in state["replied_to"]) \
                or (item.get("item") and item["item"] in state["replied_to"]):
            print(f"Skipping duplicate reply to message "
                  f"{reply_to or item.get('item')}", flush=True)
            _settle(state, item, "replied", "duplicate suppressed")
            settled.append(item)
            continue
        if "settings" in item:
            # A settings request: the monitor decides and answers it
            # (settings_nl.py); the worker's text is a lead-in at most.
            outcome = nl_outbox_item(state, item)
            if outcome == "retry":
                remaining.append(item)
            elif isinstance(outcome, tuple):
                # The item stays open.
                rejects.append((redact_nonces(item), outcome[1]))
            else:
                typing_clear(typing_keys)
                settled.append(item)
            continue
        # Clamped to Discord's per-message limit AFTER every rewrite: the
        # @everyone neutralization and link_mentions both lengthen text
        # normalize_parts already trimmed, and a part over the limit
        # would fail (400) on every retry.
        parts = [link_mentions(p, item.get("channel_id"),
                               state)[:DISCORD_MAX_CHARS]
                 for p in normalize_parts(item)]
        # Worker-drafted emoji reactions: applied to the reply_to
        # message. A react-only item may carry no text.
        reactions = [e for e in (item.get("reactions") or [])
                     if isinstance(e, str) and e.strip()][:5]
        if item.get("silent"):
            # The worker decided no reply is warranted (a conversation
            # that has run its course). Recording it keeps the sweep
            # from waking the worker again for the same message.
            _settle(state, item, "skipped", "no reply (worker's call)",
                    replied=True)
            typing_clear(typing_keys)
            settled.append(item)
            continue
        if not _is_snowflake(item.get("channel_id")) or (
                reply_to and not _is_snowflake(reply_to)):
            print(f"Dropping outbox item with a malformed channel_id or "
                  f"reply_to: {item.get('channel_id')!r} "
                  f"{reply_to!r}", flush=True)
            continue
        retry = item.get("_retry") or {}
        if retry.get("next", 0) > time.time():
            remaining.append(item)  # waiting out the gap after a failure
            continue
        if never_share_hit(state, item, qid, parts):
            hold_reply(state, item, qid, parts)
            typing_clear(typing_keys)
            settled.append(item)
            continue
        # Attachments: only generated files bound to the queue item this
        # reply answers (generated/<its message_id>/), under the
        # monitor's live may_generate approval for that item (tracked in
        # gen_approved; the worker's outbox item doesn't carry the flag),
        # and only into the channel the item came from. There is no
        # fallback for files without an approval: generation is gated in
        # code. Nothing is committed before the send succeeds: a retry
        # re-attaches the same files (kept in _retry), and the approval
        # (single-use), gen_sent and the media.per_day count settle once.
        gen_id = _gen_item_id(state, item)
        approval = _gen_approval(state, gen_id)
        if approval and approval.get("channel_id") != item.get("channel_id"):
            print(f"Note: generation approval for item {gen_id} belongs to "
                  f"channel {approval.get('channel_id')}, not "
                  f"{item.get('channel_id')}; no attachments", flush=True)
            approval = None
        attachments = []  # (send_path, filename) for the send
        gen_files = []    # [send_path, original] pairs, for _retry
        if approval and "attachments" in retry:
            # A retry: the files the first attempt resolved. Re-checked,
            # because the outbox (and so _retry) is worker-writable.
            for pair in retry.get("attachments") or []:
                ok = isinstance(pair, list) and len(pair) == 2 \
                    and len(_validate_gen_paths(pair, gen_id)) == 2
                if ok:
                    attachments.append((pair[0], os.path.basename(pair[0])))
                    gen_files.append(pair)
        elif approval:
            candidates = _validate_gen_paths(item.get("attachments"), gen_id)
            if not candidates:
                # The media tool usually returns a remote URL/handle, not the
                # local filename, so the worker can't name the file. Find it.
                found = _discover_generated(state, gen_id)
                if found:
                    print(f"Attaching discovered generated file: {found}", flush=True)
                    candidates.append(found)
                else:
                    print(f"Note: approved generation for {gen_id} but no "
                          f"file in generated/{gen_id}/", flush=True)
            for rp in candidates:
                send_path, fname = fit_for_discord(rp)
                if send_path:
                    attachments.append((send_path, fname))
                    gen_files.append([send_path, rp])
                else:
                    print(f"Attachment too large even compressed: {rp}", flush=True)
        elif item.get("attachments"):
            print(f"Note: reply to {reply_to} listed attachments without a "
                  f"live generation approval for its item; dropped",
                  flush=True)
        # The reply is about to land, which ends the indicator: stop
        # typing for this item now, or a ping right after the send would
        # show typing again for ~10s.
        typing_clear(typing_keys)
        try:
            if parts:
                # standalone: post as a plain message, not a Discord
                # reply, so it notifies nobody and does not count as
                # addressing the other side (a closing remark that
                # doesn't ask for an answer).
                send_fragmented(item["channel_id"], parts,
                                None if item.get("standalone") else reply_to,
                                attachments)
                print(f"Sent reply to {item.get('author')} in {item.get('channel_name')} "
                      f"({len(parts)} part(s)): {parts[0][:60]}", flush=True)
            for emoji in reactions:
                if reply_to and add_reaction(item["channel_id"], reply_to, emoji):
                    print(f"Added reaction {emoji} for {item.get('author')} "
                          f"in #{item.get('channel_name')}", flush=True)
            if approval:
                state.get("gen_approved", {}).pop(gen_id, None)
            if parts and gen_files:
                # A generation really ran and its file went out: count
                # it for media.per_day here, at send time, never at
                # queue time, and tell plugins (generation_sent).
                state.setdefault("gen_sent", []).extend(
                    rp for _, rp in gen_files)
                sent_scope = scope_of_channel(item["channel_id"])
                note_media_sent(state, sent_scope)
                call_hook("generation_sent", {"state": state}, item,
                          scope=sent_scope, only_where_on=False)
            _settle(state, item, "replied", replied=True)
            if item.get("_hold"):
                state.get("hold_released", {}).pop(item["_hold"], None)
            meta = state.get("item_meta", {}).get(qid) if qid else None
            if parts and meta and not meta.get("bot") \
                    and not meta.get("dm") \
                    and meta.get("channel_id") == item["channel_id"]:
                # A reply to a human actually went out: (re)open their
                # conversational follow-up window here.
                open_follow_up(state, item["channel_id"],
                               meta.get("author_id"))
            settled.append(item)
            typing_clear([], sent_in=item["channel_id"] if parts else None)
            if qid:
                _log_latency(state, qid, retry.get("picked", picked),
                             time.time())
        except Exception as e:
            code = getattr(e, "code", None)
            if code == 403:
                # Permanent permission failure (the bot cannot post in
                # that channel): drop it, don't retry forever.
                print(f"Dropping outbox item for #{item.get('channel_name')}: "
                      f"403 Missing Access ({e})", flush=True)
                _settle(state, item, "skipped", "403 missing access")
                settled.append(item)
                continue
            now = time.time()
            first = retry.get("first", now)
            if now - first >= SEND_RETRY_SECS:
                # The send may well have landed (a timeout after Discord
                # created the message looks exactly like a failure), so
                # stop before the nonce window closes rather than risk a
                # duplicate.
                print(f"Giving up on reply to {item.get('author')} in "
                      f"{item.get('channel_name')} after "
                      f"{SEND_RETRY_SECS}s of send errors: {e}", flush=True)
                _settle(state, item, "skipped", f"send failed: {e}"[:200])
                settled.append(item)
                continue
            n = retry.get("n", 0) + 1
            item["_retry"] = {"first": first, "n": n,
                              "next": now + min(30, 2 ** n),
                              "attachments": gen_files,
                              "picked": retry.get("picked", picked)}
            print(f"Failed to send reply (attempt {n}; retrying, same "
                  f"nonce): {e}", flush=True)
            remaining.append(item)

    # The queue items the settled replies answer. Matched on the outbox
    # item's "item" first; an older worker's item without it matches on
    # reply_to, its burst trigger, or a burst fragment it replied under.
    done_ids, done_frags = set(), set()
    for item in settled:
        qid = _answered_id(state, item)
        if qid:
            done_ids.add(frags.get(qid, qid))
        if not item.get("item") and item.get("reply_to"):
            done_ids.add(item["reply_to"])
            done_frags.add(item["reply_to"])

    nonces = state.get("nl_nonces") or {}
    for qid in done_ids & set(nonces):
        del nonces[qid]   # answered: its capability is spent
    nl_write_nonces(state)

    def answered(q):
        return q.get("message_id") in done_ids or any(
            isinstance(f, dict) and f.get("message_id") in done_frags
            for f in q.get("burst_fragments") or [])

    if rejects:
        _reject_outbox(rejects)
    with shared_lock():
        # Queue first, outbox second: the wake hook reads the outbox
        # before the queue, so it never sees an answered item in the
        # queue without its outbox entry.
        if done_ids:
            _queue_apply(drop=answered)
        fresh = _outbox_load(OUTBOX_FILE)
        if fresh is not None:
            atomic_write(OUTBOX_FILE, remaining + fresh)
            os.remove(OUTBOX_PROCESSING_FILE)
        elif remaining:
            # Mid-write: the retries wait in the processing file, which
            # the next pass merges back in first.
            atomic_write(OUTBOX_PROCESSING_FILE, remaining)
        else:
            os.remove(OUTBOX_PROCESSING_FILE)


PIDFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.pid")


_PIDFILE_LOCK = None  # fd holding the pidfile lock for the process lifetime


def _acquire_singleton():
    """Ensure only one monitor runs. The pidfile is locked with flock for
    as long as this process lives, so two monitors started at the same
    moment (say, overlapping watchdog runs) cannot both pass the check;
    the lock disappears with the process, so there is no stale state.
    Two monitors would each send everything in the outbox, posting every
    reply twice. Exits the process if a live peer holds the lock."""
    global _PIDFILE_LOCK
    mypid = os.getpid()
    fd = os.open(PIDFILE, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Another monitor holds the pidfile lock; exiting.", flush=True)
        sys.exit(0)
    # A monitor from before this lock existed holds no flock, so also
    # check whether the recorded pid is still a live monitor.
    try:
        oldpid = int(os.read(fd, 32).decode().strip() or 0)
    except ValueError:
        oldpid = 0
    if oldpid and oldpid != mypid:
        try:
            with open(f"/proc/{oldpid}/cmdline", "rb") as f:
                alive = "monitor.py" in f.read().decode(errors="replace")
        except OSError:
            alive = False
        if alive:
            print(f"Another monitor (pid {oldpid}) is already running; "
                  f"exiting.", flush=True)
            sys.exit(0)
        print(f"Replacing stale monitor pidfile (pid {oldpid} gone).",
              flush=True)
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, str(mypid).encode())
    _PIDFILE_LOCK = fd  # never closed: the lock lives as long as we do


# ---------------------------------------------------------------------------
# Gateway fast path (realtime ingest).
#
# Discord's Gateway is a websocket that pushes MESSAGE_CREATE events the
# instant they happen, for every guild the bot is in plus DMs, instead of
# waiting for the next REST poll.
#
# The gateway thread ONLY enqueues raw message dicts into _gateway_inbox;
# the main loop drains it and runs the exact same classify/stage path as
# polled messages, so behavior is identical. REST polling still covers
# every channel too: it acts as gap repair (anything the gateway missed
# while reconnecting) and as the full fallback if the gateway is down. The
# REST watermark plus the gateway-seen ids (see note_gateway_seen) prevent
# double-processing without letting the gateway skip the poll past a gap.
#
# Requires the Message Content privileged intent (Discord dev portal -> Bot
# -> Privileged Gateway Intents). Without the toggle, Discord closes the
# connection with code 4014 and we fall back to polling with a loud log.
# ---------------------------------------------------------------------------
GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
GATEWAY_QUERY = "/?v=10&encoding=json"
# GUILDS | GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT, plus
# GUILD_MESSAGE_TYPING | DIRECT_MESSAGE_TYPING for the early burst flush
# (burst_quiet). The typing intents are not privileged.
GATEWAY_INTENTS = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15) \
    | (1 << 11) | (1 << 14)
# Close codes after which reconnecting cannot help. 4004: the token was
# rejected (exit; the watchdog alerts). 4010-4014: shard, API version, or
# intents problems that need a code or portal fix: realtime ingest
# stops and REST polling carries on (the watchdog surfaces the
# "GATEWAY DISABLED" line). 4007 / 4009: the session cannot be resumed,
# so the next connect identifies fresh.
GATEWAY_TOKEN_REJECTED = 4004
GATEWAY_FATAL_CODES = (4010, 4011, 4012, 4013, 4014)
GATEWAY_FRESH_SESSION_CODES = (4007, 4009)


def _close_code(exc):
    """The close code of a websockets ConnectionClosed, across versions
    (newer ones carry it on .rcvd; .code is the deprecated spelling)."""
    rcvd = getattr(exc, "rcvd", None)
    if rcvd is not None:
        return rcvd.code
    return getattr(exc, "code", None)


async def _gateway_heartbeat(ws, interval_secs, status):
    import asyncio
    try:
        while True:
            await asyncio.sleep(interval_secs)
            await ws.send(json.dumps({"op": 1, "d": status.get("seq")}))
    except asyncio.CancelledError:
        pass


async def _gateway_run(token, inbox, status):
    """Connect, identify, and forward MESSAGE_CREATE events (all guilds + DMs)."""
    import asyncio
    import websockets
    backoff = 5
    session_id = None
    resume_url = None
    while not status.get("stop"):
        resuming = bool(session_id and status.get("seq") is not None)
        # A resume goes to the session's own resume_gateway_url (from
        # READY); the default URL is for fresh identifies.
        url = GATEWAY_URL
        if resuming and resume_url:
            url = resume_url.rstrip("/") + GATEWAY_QUERY
        try:
            async with websockets.connect(url, max_size=2 ** 22) as ws:
                hello = json.loads(await ws.recv())
                hb_interval = hello["d"]["heartbeat_interval"] / 1000.0
                if resuming:
                    await ws.send(json.dumps({"op": 6, "d": {
                        "token": token,
                        "session_id": session_id,
                        "seq": status.get("seq"),
                    }}))
                else:
                    await ws.send(json.dumps({"op": 2, "d": {
                        "token": token,
                        "intents": GATEWAY_INTENTS,
                        "properties": {"os": "linux",
                                       "browser": "muse-discord",
                                       "device": "muse-discord"},
                    }}))
                status["connected"] = True
                backoff = 5
                hb = asyncio.create_task(
                    _gateway_heartbeat(ws, hb_interval, status))
                try:
                    async for raw in ws:
                        pkt = json.loads(raw)
                        op = pkt.get("op")
                        if pkt.get("s") is not None:
                            status["seq"] = pkt["s"]
                        if op == 1:      # Discord wants a heartbeat now
                            await ws.send(json.dumps(
                                {"op": 1, "d": status.get("seq")}))
                            continue
                        if op == 7:      # Discord asks us to reconnect
                            break
                        if op == 9:      # invalid session
                            # d is true when the session can still be
                            # resumed; otherwise re-identify fresh.
                            if not pkt.get("d"):
                                session_id = None
                                status["seq"] = None
                            break
                        if op != 0:
                            continue     # hello / heartbeat ack / etc.
                        t = pkt.get("t")
                        if t == "READY":
                            session_id = pkt["d"]["session_id"]
                            resume_url = pkt["d"].get("resume_gateway_url")
                            # READY carries the bot's identity: the main
                            # loop adopts it when /users/@me failed (a
                            # REST rate-limit storm at boot), so mentions
                            # are recognized without waiting for REST.
                            status["user"] = pkt["d"].get("user")
                            # The application id, for registering the
                            # /settings command.
                            status["application"] = \
                                pkt["d"].get("application")
                            print("Gateway READY: realtime ingest live.",
                                  flush=True)
                        elif t == "RESUMED":
                            print("Gateway session resumed.", flush=True)
                        elif t in ("GUILD_CREATE", "GUILD_DELETE"):
                            # Join watermark + new-server detection. A
                            # GUILD_DELETE with unavailable set is an
                            # outage, not a removal.
                            d = pkt["d"]
                            if t == "GUILD_DELETE" and d.get("unavailable"):
                                continue
                            inbox.put({"_guild_event": {
                                "op": "create" if t == "GUILD_CREATE"
                                else "delete",
                                "id": d.get("id"),
                                "joined_at": d.get("joined_at"),
                                "member_count": d.get("member_count")}})
                        elif t == "INTERACTION_CREATE":
                            # A button click (or command): answered by
                            # the interaction thread at once, within
                            # Discord's 3s deadline, whatever the main
                            # loop is doing.
                            ix_inbox = status.get("interactions")
                            if ix_inbox is not None:
                                ix_inbox.put(pkt["d"])
                        elif t == "TYPING_START":
                            # Someone is typing: lets a pending burst
                            # flush as soon as its author stops. Rides
                            # the inbox so it is ordered against the
                            # messages; no wake, the loop is ticking.
                            d = pkt["d"]
                            inbox.put({"_typing": {
                                "channel_id": d.get("channel_id"),
                                "user_id": d.get("user_id")}})
                        elif t == "MESSAGE_CREATE":
                            # Realtime ingest for every guild the bot is in
                            # plus DMs (no guild_id). drain_gateway() in the
                            # main loop classifies and dedupes; the REST poll
                            # remains the gap-repair fallback.
                            m = pkt["d"]
                            inbox.put(m)
                            status["last_event"] = time.time()
                            wake = status.get("wake")
                            if wake is not None:
                                wake.set()
                finally:
                    hb.cancel()
                    status["connected"] = False
        except websockets.ConnectionClosed as e:
            code = _close_code(e)
            if code == GATEWAY_TOKEN_REJECTED:
                # The main loop prints the TOKEN REJECTED marker and
                # exits; reconnecting with a rejected token never heals.
                status["token_rejected"] = True
                wake = status.get("wake")
                if wake is not None:
                    wake.set()
                return
            if code == 4014:
                print("GATEWAY DISABLED (code 4014): the Message Content "
                      "privileged intent is not enabled for this bot "
                      "application (Discord developer portal -> Bot -> "
                      "Privileged Gateway Intents). Realtime ingest stays "
                      "off; REST polling continues.",
                      flush=True)
                status["intent_denied"] = True
                return
            if code in GATEWAY_FATAL_CODES:
                print(f"GATEWAY DISABLED (code {code}): Discord refused "
                      f"this connection for a reason reconnecting cannot "
                      f"fix (shard, API version, or intents). Realtime "
                      f"ingest stays off; REST polling continues.",
                      flush=True)
                return
            if code in GATEWAY_FRESH_SESSION_CODES:
                session_id = None
                status["seq"] = None
            print(f"Gateway closed (code {code}); reconnecting...",
                  flush=True)
        except Exception as e:
            print(f"Gateway error ({e}); reconnecting...", flush=True)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 120)


def _gateway_thread_main(token, inbox, status):
    import asyncio
    try:
        asyncio.run(_gateway_run(token, inbox, status))
    except Exception as e:
        print(f"GATEWAY DISABLED (thread died: {e}): realtime ingest is "
              f"off; REST polling continues.", flush=True)
        status["dead"] = True


def drain_gateway(state, inbox, channels):
    """Pull gateway-delivered messages (all guilds + DMs) and classify them
    exactly like polled messages. Returns (triggers, dms): guild
    triggers as (msg, cname, guild_id, kind) tuples (already staged, see
    stage_found), DMs as (msg, cname).
    Guild join/leave events are applied to state on the way, and typing
    events and message arrivals are recorded for the early burst flush
    (note_activity). Skips anything the REST poll already processed (at
    or below its last_seen watermark), and leaves that watermark alone
    (see note_gateway_seen). Messages from servers outside a non-empty
    bot.guild_ids are ignored."""
    mentions, dms = [], []
    guild_names = {}
    for info in channels.values():
        if info.get("guild_id"):
            guild_names[info["guild_id"]] = info.get("guild_name",
                                                     info["guild_id"])
    while True:
        try:
            m = inbox.get_nowait()
        except pyqueue.Empty:
            break
        if "_guild_event" in m:
            on_guild_event(state, m["_guild_event"])
            continue
        if "_typing" in m:
            note_activity(m["_typing"].get("channel_id"),
                          m["_typing"].get("user_id"))
            continue
        if "_record" in m:
            apply_record(state, m["_record"])
            continue
        cid = m.get("channel_id")
        if not cid:
            continue
        # Recorded even when the poll got here first: the burst holding
        # this message needs to know when its author last spoke.
        note_activity(cid, (m.get("author", {}) or {}).get("id"), m["id"])
        last = state["last_seen"].get(cid)
        if last and int(m["id"]) <= int(last):
            continue  # the poll loop already handled it
        if gateway_seen(state, cid, m["id"]):
            continue  # delivered twice (e.g. replayed on resume)
        gid = m.get("guild_id")
        if gid and GUILD_ALLOWLIST and gid not in GUILD_ALLOWLIST:
            continue  # bot.guild_ids: other servers are ignored entirely
        if gid:
            info = channels.get(cid)
            if info is None:
                # Channel/thread discovery hasn't registered yet: resolve it
                # once via the API so the message isn't missed. A failure
                # (e.g. 403 private channel) skips it here only; last_seen
                # is untouched, so the REST poll can still pick it up.
                try:
                    ch = api_get(f"/channels/{cid}")
                except Exception:
                    continue
                if not ch or not ch.get("id"):
                    continue
                info = {"name": ch.get("name") or "?",
                        "guild_id": ch.get("guild_id") or gid,
                        "guild_name": guild_names.get(
                            ch.get("guild_id") or gid,
                            ch.get("guild_id") or gid),
                        "can_send": None,
                        "thread": ch.get("type") in (10, 11, 12),
                        "parent_id": ch.get("parent_id")}
                if not info["thread"] and ch.get("parent_id"):
                    CHANNEL_CATEGORY[cid] = ch["parent_id"]
                channels[cid] = info
            cname = channel_display(info)
            record_history(state, cid, [m])
            note_gateway_seen(state, cid, m["id"])
            kind = classify_message(state, m, cname, gid)
            if kind:
                stage_found(state, m, cname, gid, kind)
                mentions.append((m, cname, gid, kind))
        else:
            # DM event (no guild_id). The DM settings decide whose DMs
            # are even read (dm_sender_allowed): under the default
            # (dms_from: owners) everyone else's DMs are dropped unread
            # and unrecorded; dms.who.replies: off drops all of them.
            author = m.get("author", {}) or {}
            if author.get("id") == BOT_ID:
                record_history(state, cid, [m])
                note_gateway_seen(state, cid, m["id"])
                continue  # own messages are context only
            TM.count("messages_seen",
                     int(m.get("type", 0) in CONVERSATION_TYPES))
            if not dm_sender_allowed(author):
                note_dm_refused(m)
                continue
            record_history(state, cid, [m])
            note_gateway_seen(state, cid, m["id"])
            if m.get("type", 0) not in CONVERSATION_TYPES:
                continue  # a system notice (a pin): context only
            if hold_fragment(state, cid, m):
                continue  # folded into a burst already being coalesced
            dms.append((m, "DM with " + (author.get("username") or cid)))
    return mentions, dms



# ---------------------------------------------------------------------------
# Reconciliation sweep. Every engine.sweep.every_cycles poll cycles,
# independently re-read recent channel history (and the owners' polled DM
# channels) and diff every mention / reply-to-bot / owner DM against the
# durable records (replied_to, queue, outbox, quarantine, ledger; a burst
# fragment counts as its trigger). Anything older than the grace period with no
# terminal state is requeued on the spot. This is the backstop that turns
# every silent failure mode (storm loss, mid-cycle kill, worker drop)
# into a delayed reply instead of a permanent miss. Aborts on the first
# 429: during a rate-limit storm the sweep must not add request pressure.
# (The SWEEP_* constants are resolved from config in the header block.)
# ---------------------------------------------------------------------------
def sweep_mentions(state, queue, channels, dms=True):
    """Requeue mentions that no durable record accounts for. Returns True
    when the queue changed. dms=False leaves out the owners' DM channels
    (the main loop's sliced pass, SweepPass, sweeps them as a step of
    their own)."""
    if rest_backing_off():
        return False
    entries = _ledger_load()["entries"]
    q_ids = {q.get("message_id") for q in queue}
    outbox_targets = set()
    for path in (OUTBOX_FILE, OUTBOX_PROCESSING_FILE):
        try:
            with open(path) as f:
                for i in json.load(f):
                    if isinstance(i, dict):
                        outbox_targets.update((i.get("reply_to"),
                                               i.get("item")))
        except Exception:
            pass
    quar_ids = _quarantine_ids()
    replied = set(state.get("replied_to", []))
    pending_ids = set()
    for entry in state.get("pending", {}).values():
        pending_ids.add(entry.get("first_id"))
        for frag in entry.get("fragments", []):
            pending_ids.add(frag.get("id"))
    # Burst fragment id -> trigger id. A fragment is answered by its
    # trigger's queue item, so it is accounted for whenever its trigger
    # is (and an outbox reply under a fragment accounts for the trigger).
    aliases = state.get("reply_aliases", {})
    outbox_targets |= {aliases[t] for t in outbox_targets if t in aliases}
    # The owners' DM channels are swept too: a DM lost in a gateway gap
    # is as lost as a mention.
    dm_channels = [(dm_id, {"dm": True, "name": name})
                   for dm_id, name in DM_CHANNELS.items()] if dms else []
    now = time.time()
    changed = False
    for cid, info in list(channels.items()) + dm_channels:
        dm = bool(info.get("dm"))
        cname = "DM with " + info["name"] if dm else channel_display(info)
        try:
            msgs = api_get(f"/channels/{cid}/messages?limit=20")
        except Exception as e:
            if getattr(e, "code", None) == 429:
                print("Sweep aborted: rate limited.", flush=True)
                return changed
            continue
        if not isinstance(msgs, list):
            continue
        for m in sorted(msgs, key=lambda x: int(x["id"])):
            mid = m["id"]
            author = m.get("author", {}) or {}
            if author.get("id") == BOT_ID:
                continue  # (other bots: trigger_kind applies behavior.bots)
            if m.get("type", 0) not in CONVERSATION_TYPES:
                continue  # a system notice (a pin, a new thread)
            if dm:
                # Owner DM channels: every owner message is a trigger.
                if is_bot_author(author) \
                        or not dm_swept_author(author.get("id")) \
                        or not dm_sender_allowed(author):
                    continue
            # Direct address only: follow-ups are never requeued.
            elif trigger_kind(m, info.get("guild_id")) != "mention":
                continue
            if predates_join(state, m, info.get("guild_id")):
                continue
            age = now - _snowflake_ts(mid)
            if age < SWEEP_GRACE_SECS or age > SWEEP_MAX_AGE_SECS:
                continue
            if mid in replied:
                if entries.get(mid, {}).get("status") != "replied":
                    ledger_record(mid, "replied", "sweep sync", cid,
                                  author.get("username"),
                                  author_id=author.get("id"))
                continue
            ids = {mid, aliases.get(mid, mid)}
            if any(ids & s for s in (replied, quar_ids, q_ids,
                                     outbox_targets, pending_ids)):
                continue  # accounted for and in flight
            ent = entries.get(mid)
            if any(entries.get(i, {}).get("status") in LEDGER_TERMINAL
                   for i in ids):
                continue
            if info.get("can_send") is False:
                ledger_record(mid, "skipped",
                              "no send permission in channel", cid,
                              author.get("username"),
                              author_id=author.get("id"))
                continue
            recs = (ent or {}).get("recoveries", 0)
            if recs >= SWEEP_MAX_RECOVERIES:
                if ent and ent.get("status") != "skipped":
                    ledger_record(mid, "skipped", "recovery cap reached",
                                  cid, author.get("username"),
                                  author_id=author.get("id"))
                    print(f"Sweep: giving up on unrecoverable mention "
                          f"{mid} from {author.get('username')} in "
                          f"{cname} (recorded in mention ledger).",
                          flush=True)
                continue
            ok = enqueue_message(m, cname, state, queue, dm=dm,
                                 fragments=[],
                                 guild_id=info.get("guild_id"))
            if ok:
                changed = True
                q_ids.add(mid)
                ledger_record(mid, "queued",
                              f"sweep recovery #{recs + 1}", cid,
                              author.get("username"),
                              recoveries=recs + 1,
                              author_id=author.get("id"))
                print(f"Sweep recovered lost mention {mid} from "
                      f"{author.get('username')} in {cname}.", flush=True)
            elif not _ledger_terminal(mid):
                # (A gate inside enqueue, e.g. quarantine or the join
                # watermark, may already have recorded a terminal state.)
                ledger_record(mid, "enqueue_failed",
                              "sweep enqueue failed", cid,
                              author.get("username"),
                              recoveries=recs + 1,
                              author_id=author.get("id"))
    return changed


# ---------------------------------------------------------------------------
# Plugins. A plugin is a folder under plugins/ with a plugin.py exposing
# register(api) -> {hook_name: callable}, plus a plugin.yaml whose
# `settings:` manifest declares its settings (type, default, scopes; see
# muse_settings.load_manifests; an old `params:` list still works). A
# plugin is loaded once some scope turns it on (plugins.<id>.enabled),
# and its hooks run only where it is on. No plugin ships today (see
# plugins/README.md); the core calls these hooks:
#
#   gate_message(ctx, msg, guild_id) -> dict | None
#       During enqueue: returned dicts are merged into the queue item
#       (never may_generate: generation permission is the core's,
#       media.*).
#   generation_sent(ctx, outbox_item)
#       A generated attachment was actually sent for an outbox item
#       (runs wherever the reply went, on or off).
#
# ctx carries "state" (and "owners" for gate_message) plus, for the
# event's scope: "scope", "settings" (the plugin's own resolved values
# there, nothing else), "cap_layers" (key -> [(counter id, cap)]: the
# caps set along the scope, each counting in its own scope),
# "counter_ids" (the counters an event there adds to) and, for
# gate_message, "author_matches" (people value -> bool). api.param(key,
# guild_id, guild_name) still resolves one value for one server (old
# param names too, see Manifest.spec_for).
# ---------------------------------------------------------------------------
PLUGIN_HOOKS = {}
_HOOK_OWNER = {}        # id(hook fn) -> plugin id
_LOADED_PLUGINS = set()  # plugin folders already imported


def param_for(params, key, guild_id=None, guild_name=None):
    """Resolve one plugin param for one guild, following the old global
    + per_guild override pattern (for a plugin the settings know nothing
    about). Two shapes are supported:

    1. The param itself is {"default": value, "per_guild": {gid-or-name:
       value}}: a per_guild hit wins, else the default.
    2. The whole params block has a "per_guild" map of param overrides:
       params["per_guild"][gid-or-name][key] wins over params[key].

    per_guild keys may be guild IDs or guild (server) names; IDs are
    tried first. Returns None when the param is unset."""
    if not isinstance(params, dict):
        return None
    pg = params.get("per_guild")
    if isinstance(pg, dict):
        for k in (guild_id, guild_name):
            if k and isinstance(pg.get(k), dict) and key in pg[k]:
                return pg[k][key]
    val = params.get(key)
    if isinstance(val, dict) and ("default" in val or "per_guild" in val):
        inner = val.get("per_guild") or {}
        for k in (guild_id, guild_name):
            if k and k in inner:
                return inner[k]
        return val.get("default")
    return val


class PluginAPI:
    """The facade handed to a plugin's register() at load time."""

    _pid = None

    def __init__(self, name, params):
        self.name = name
        self.params = params or {}

    def log(self, *args):
        print(*args, flush=True)

    def param(self, key, guild_id=None, guild_name=None):
        """One setting for one server (guild_id None = everywhere). Old
        param names map to their settings (allowed_user_ids -> who, as
        a list: [] = the owners; daily_cap -> per_day)."""
        st = settings()
        man = st.manifests.get(self._pid or self.name)
        if man is None:
            return param_for(self.params, key, guild_id, guild_name)
        skey, spec = man.spec_for(key)
        if spec is None:
            return None
        scope = MS.Scope.server(guild_id, guild_name) if guild_id \
            else MS.Scope.everywhere()
        value = st.get(f"plugins.{man.id}.{skey}", scope)
        if skey != key and spec.type == "people":
            return [] if value == "owners" else value
        return value

    def setting(self, key, scope):
        """One of this plugin's settings at a scope (ctx["scope"])."""
        return settings().get(f"plugins.{self._pid or self.name}.{key}",
                              scope)


def load_plugins():
    """Import every plugin under plugins/ that some scope turns on, once,
    and collect its hooks. Called at startup and after a settings
    reload."""
    global _SETTINGS
    if not os.path.isdir(PLUGINS_DIR):
        return
    if os.path.abspath(_SETTINGS.plugins_dir or "") \
            != os.path.abspath(PLUGINS_DIR):
        _SETTINGS = MS.Watcher(_config_path(), PLUGINS_DIR)
    st = _SETTINGS.current
    by_folder = {m.folder: m for m in st.manifests.values()}
    for name in sorted(os.listdir(PLUGINS_DIR)):
        pfile = os.path.join(PLUGINS_DIR, name, "plugin.py")
        if not os.path.isfile(pfile) or name in _LOADED_PLUGINS:
            continue
        man = by_folder.get(name)
        pid = man.id if man else name
        if not st.plugin_enabled_anywhere(pid):
            continue
        params = {k: MS.display(man.specs.get(k), v) for k, v in
                  st.plugin_values(pid, MS.Scope.everywhere()).items()} \
            if man else {}
        api = PluginAPI(name, params)
        api._pid = pid
        try:
            spec = importlib.util.spec_from_file_location(
                f"muse_discord_plugin_{name}", pfile)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            hooks = mod.register(api) or {}
        except Exception as e:
            print(f"Plugin {name} failed to load: {e}", flush=True)
            continue
        _LOADED_PLUGINS.add(name)
        for hook_name, fn in hooks.items():
            if callable(fn):
                PLUGIN_HOOKS.setdefault(hook_name, []).append(fn)
                _HOOK_OWNER[id(fn)] = pid
        print(f"Plugin loaded: {name} "
              f"(hooks: {', '.join(sorted(hooks))})", flush=True)


def call_hook(name, ctx, *args, scope=None, only_where_on=True):
    """Run every plugin's callable for a hook; returns their results.
    With scope, a plugin's hook runs only where the plugin is on (unless
    only_where_on is False) and its ctx carries its settings there. A
    failing plugin never breaks the pipeline."""
    out = []
    st = settings() if scope is not None else None
    for fn in PLUGIN_HOOKS.get(name, []):
        pid = _HOOK_OWNER.get(id(fn))
        c = ctx
        if pid is not None and st is not None:
            if only_where_on and not st.plugin_enabled(pid, scope):
                continue
            c = dict(ctx, scope=scope,
                     settings=st.plugin_values(pid, scope),
                     cap_layers=lambda key, pid=pid: st.cap_layers(
                         f"plugins.{pid}.{key}", scope),
                     counter_ids=MS.Settings.counter_ids(scope))
        try:
            out.append(fn(c, *args))
        except Exception as e:
            print(f"Plugin hook {name} failed: {e}", flush=True)
    return out


_no_send_logged = set()


def _note_no_send(msg, cname):
    """Record a mention that cannot be answered because the bot has no
    send permission in the channel: ledgered as skipped (terminal, so the
    sweep won't resurrect it) and logged once per channel per run."""
    cid = msg.get("channel_id")
    if cid not in _no_send_logged:
        _no_send_logged.add(cid)
        print(f"Skipping replies in {cname}: the bot cannot send "
              f"messages there.", flush=True)
    ledger_record(msg["id"], "skipped", "no send permission in channel",
                  channel_id=cid,
                  author=(msg.get("author", {}) or {}).get("username"),
                  author_id=(msg.get("author", {}) or {}).get("id"))



# ---------------------------------------------------------------------------
# Sliced REST work (2026-10-18). The main loop used to run the whole REST
# poll (a request per channel), the sweep, the thread refresh, and
# discovery inline. Behind a slow proxy (0.6s a request, ~40 channels)
# one poll blocked the loop for ~23s of every 30, so a DM the gateway had
# delivered at once sat in the inbox for 25-95s before it was even
# staged. Now each tick spends about REST_SLICE_SECS on REST reads (at
# least one request when any is due) and the fast path (gateway drain,
# staging, burst flushes, typing, the outbox) runs between slices.
#
# The poll is a rotation over its targets: the owners' DM channels, then
# every monitored channel. A rotation starts when the previous one has
# finished and POLL_INTERVAL has passed since that one started; its
# requests are spread over the first POLL_SPREAD of the interval, and
# when Discord (or a proxy) is slower than that, it polls flat out, still
# in slices, so every channel is read about once per interval. The first
# rotation (the startup catch-up) is not paced. A cycle
# (the unit of every cadence: sweep.every_cycles, the thread and channel
# refreshes, the identity retry, the cleanup sweeps) is a completed
# rotation, so the cadences mean what they meant. A channel discovered
# mid-rotation joins the next one; one removed mid-rotation is skipped.
#
# The sweep pass, the thread refresh, and discovery are background jobs,
# run one at a time in the order they were due, in slices that share the
# tick's budget with the poll (alternating ticks when both have work).
# Discovery and the thread refresh are generators that yield after each
# request. Everything runs on the main thread: state and the rate-limit
# globals keep one writer. While REST backs off (429), nothing is sliced
# and the rotation waits; a sweep pass ends, as it always has.
# ---------------------------------------------------------------------------
REST_SLICE_SECS = 0.4
POLL_SPREAD = 0.5
POLL_TICK_SECS = 1.0  # pacing granularity: due targets go out together


def poll_targets():
    """The targets of one rotation. Empty until the bot knows its own id:
    nothing can be classified before that, so watermarks stay put."""
    if BOT_USER_ID is None:
        return []
    targets = [("dms",)] if dm_poll_user_ids() else []
    return targets + [("channel", cid) for cid in CHANNELS]


class PollRotation:
    """One pass of the REST poll over its targets, done in slices."""

    def __init__(self, targets, started, spread=POLL_SPREAD):
        self.targets = list(targets)
        self.pos = 0
        self.started = started
        self.spread = spread

    def done(self):
        return self.pos >= len(self.targets)

    def next_due(self):
        """When the next target is due on the pacing schedule."""
        return self.started + (POLL_INTERVAL * self.spread * self.pos
                               / max(1, len(self.targets)))

    def due(self, now):
        return not self.done() and now >= self.next_due()

    def run_slice(self, state, deadline, dms_out):
        """Poll due targets until deadline, at least one. Polled guild
        triggers are staged inside (stage_found); DMs are appended to
        dms_out for the staging phase. Returns the number of targets
        polled."""
        n = 0
        while self.due(time.time()) and not rest_backing_off():
            if n and time.time() >= deadline:
                break
            target = self.targets[self.pos]
            self.pos += 1
            if self._poll(state, target, dms_out):
                n += 1
        return n

    def _poll(self, state, target, dms_out):
        if target[0] == "dms":
            # DMs: every message not from the bot is addressed to the
            # bot. Resolving the channels is a step; each DM channel is
            # polled as the next ones.
            self.targets[self.pos:self.pos] = [
                ("dm", dm_id, dm_name)
                for dm_id, dm_name in list_dm_channels()]
            return True
        if target[0] == "dm":
            _, dm_id, dm_name = target
            dms_out.extend((m, "DM with " + dm_name)
                           for m in check_dm_channel(dm_id, dm_name, state))
            return True
        cid = target[1]
        info = CHANNELS.get(cid)
        if info is None:
            return False  # unmonitored since the rotation began
        cname = channel_display(info)
        gid = info["guild_id"]
        try:
            # Triggers are staged inside, in arrival order.
            check_channel(cid, cname, state, gid)
        except Exception as e:
            print(f"Poll error in {cname}: {e}", flush=True)
        return True


class SweepPass:
    """One reconciliation sweep pass (sweep_mentions), done in slices: a
    step sweeps one channel (or the owners' DM channels), each against
    the durable records as they are at that moment."""
    name = "Sweep"

    def __init__(self, channel_ids):
        self.targets = [None] + list(channel_ids)  # None: the DMs
        self.pos = 0

    def done(self):
        return self.pos >= len(self.targets)

    def run_slice(self, state, deadline, queue_ok):
        if not queue_ok:
            # The queue could not be read this tick: the sweep must not
            # write it. The pass ends, as a skipped sweep did.
            self.pos = len(self.targets)
            return 0
        queue = load_queue()  # after this tick's sends' removals
        n_before = len(queue)
        changed = False
        n = 0
        while not self.done():
            if rest_backing_off():
                # A 429 (here or elsewhere) ends the pass: the sweep must
                # not add request pressure (sweep_mentions logs it).
                self.pos = len(self.targets)
                break
            if n and time.time() >= deadline:
                break
            cid = self.targets[self.pos]
            self.pos += 1
            if cid is None:
                changed |= sweep_mentions(state, queue, {})
            elif cid in CHANNELS:  # (else unmonitored since it began)
                changed |= sweep_mentions(state, queue,
                                          {cid: CHANNELS[cid]}, dms=False)
            else:
                continue
            n += 1
        if changed:
            queue_edit(add=queue[n_before:])
            save_state(state)
        return n


class GenJob:
    """A background REST job written as a generator that yields after
    each request (thread refresh, discovery)."""

    def __init__(self, name, gen):
        self.name = name
        self.gen = gen
        self.finished = False

    def done(self):
        return self.finished

    def run_slice(self, state, deadline, queue_ok):
        n = 0
        while not self.finished and not rest_backing_off():
            if n and time.time() >= deadline:
                break
            try:
                next(self.gen)
            except StopIteration:
                self.finished = True
                break
            n += 1
        return n


def thread_refresh_iter(state):
    """Light thread-only refresh: pick up brand-new posts and threads
    minutes after they appear instead of waiting for the full channel
    refresh. Only threads not already monitored cost an API probe."""
    guilds = get_guilds()
    yield
    for gid, gname in guilds or []:
        try:
            chans = api_get(f"/guilds/{gid}/channels")
        except Exception:
            continue
        finally:
            yield
        names_by_id = {c["id"]: c.get("name", "?") for c in chans}
        for c in chans:
            if c.get("parent_id"):
                CHANNEL_CATEGORY[c["id"]] = c["parent_id"]
        new_threads = yield from discover_threads_iter(
            gid, gname, names_by_id, CHANNELS)
        for tid, info in new_threads.items():
            seed_channel_seen(tid, channel_display(info), state)
            CHANNELS[tid] = info
            print(f"Now monitoring {channel_display(info)}.", flush=True)
            yield
        if new_threads:
            save_state(state)


def discovery_iter(state):
    """Periodic re-discovery of readable channels, so permission changes
    (grants or revocations) take effect without a restart. Applied when
    complete; a channel the gateway or the thread refresh registered
    while it ran is kept."""
    global CHANNELS, GUILDS
    before = dict(CHANNELS)
    fresh, fresh_guilds = yield from discover_channels_iter(before)
    GUILDS = fresh_guilds
    for cid, info in fresh.items():
        if cid not in CHANNELS:
            # Seeds only a channel with no last_seen yet.
            seed_channel_seen(cid, channel_display(info), state)
            yield
    for cid, info in CHANNELS.items():
        if cid not in before and cid not in fresh:
            fresh[cid] = info
    if set(fresh) != set(CHANNELS):
        added = [channel_display(n) for i, n in fresh.items()
                 if i not in CHANNELS]
        removed = [channel_display(n)
                   for i, n in CHANNELS.items()
                   if i not in fresh]
        if added:
            print(f"Now monitoring {', '.join(added)}.",
                  flush=True)
        if removed:
            print(f"Lost access to {', '.join(removed)}; "
                  f"unmonitoring.", flush=True)
        CHANNELS = fresh
    publish_directory()
    sync_guilds(state, fresh_guilds, fresh)
    save_state(state)


def outbox_waiting(since):
    """True when reply_outbox.json holds a reply written after since (the
    last outbox pass): a cheap stat, as the monitor leaves the file as
    "[]". A file the last pass could not take (mid-write, or a pass that
    failed) does not count, so it cannot make the loop spin."""
    try:
        st = os.stat(OUTBOX_FILE)
    except OSError:
        return False
    # (50ms of slack: file times can trail time.time() slightly.)
    return st.st_size > 4 and st.st_mtime > since - 0.05


def main():
    global BOT_USER_ID, BOT_ID, BOT_NAME, CHANNELS, GUILDS, _FORCE_DISCOVERY
    _acquire_singleton()
    TM.start()
    touch_heartbeat()
    # Identity FIRST, discovery second: the watchdog's health check waits
    # for the identity marker, and full discovery (a probe per channel
    # and thread) took ~37s by 2026-10-13, past the watchdog's old 30s
    # window, so it kept killing healthy monitors mid-startup.
    #
    # Marker contract with watchdog.sh:
    #   "Bot user id: <id>"    identity confirmed (here, in-loop, or from
    #                          the gateway's READY).
    #   "Bot identity pending" /users/@me failed for a reason that can
    #                          heal (429 storm, network): the monitor is
    #                          up, keeps retrying, and the gateway still
    #                          delivers. The watchdog counts it as a
    #                          start; it used to print the "Bot user id"
    #                          marker here so a 429 storm would not get a
    #                          gateway-live monitor killed (2026-10-15),
    #                          which also hid a revoked token.
    #   "TOKEN REJECTED"       a 401: the monitor exits (status 3) and the
    #                          watchdog raises the token alert.
    try:
        _me = api_get("/users/@me")
        BOT_USER_ID = _me.get("id")
        BOT_ID = BOT_USER_ID
        BOT_NAME = _me.get("username")
        print(f"Bot user id: {BOT_USER_ID}", flush=True)
    except Exception as e:
        if getattr(e, "code", None) == 401:
            token_rejected_exit("401 from /users/@me")
        print(f"Bot identity pending: /users/@me failed at boot ({e}); "
              f"retrying in-loop.", flush=True)
    CHANNELS, GUILDS = discover_channels()
    publish_directory()
    print(f"muse-discord monitor starting. Polling {len(CHANNELS)} "
          f"channels + DMs every {POLL_INTERVAL}s.", flush=True)
    if not CHANNELS:
        print("WARNING: no readable channels found (check the bot's "
              "guild membership and channel permissions); discovery "
              "keeps retrying on the refresh cadence.", flush=True)
    state = load_state()
    load_plugins()
    load_settings_ui()  # imported now, not on the first /settings
    # Join watermarks for every guild, plus the owner summary for any
    # guild joined while the monitor was down.
    sync_guilds(state, GUILDS, CHANNELS)

    # Seed last_seen for any monitored channel not tracked yet, so its
    # history is never mistaken for fresh mentions (first run, or a
    # newly discovered channel after a permission grant).
    for cid, info in CHANNELS.items():
        if cid not in state["last_seen"]:
            seed_channel_seen(cid, channel_display(info), state)

    # Gateway fast path (realtime MESSAGE_CREATE for all guilds + DMs).
    # Daemon thread: it only enqueues raw messages and sets the wake event;
    # the main loop below classifies and stages them. REST polling keeps
    # covering everything as gap repair and full fallback, so a dead
    # gateway degrades silently to the old 30s cadence.
    token = TOKEN
    _gateway_inbox = pyqueue.Queue()
    _ix_inbox = pyqueue.Queue()  # interactions, see below
    _gateway_status = {"wake": threading.Event(),
                       "interactions": _ix_inbox}
    gw_thread = threading.Thread(
        target=_gateway_thread_main,
        args=(token, _gateway_inbox, _gateway_status),
        daemon=True, name="discord-gateway")
    gw_thread.start()
    print("Gateway thread started (realtime ingest).", flush=True)
    # Typing indicator on its own clock (see typing_refresh).
    threading.Thread(target=_typing_thread_main, daemon=True,
                     name="typing").start()
    # Interactions (button clicks) on their own thread, fed straight by
    # the gateway thread: the 3s acknowledgement deadline never waits on
    # the main loop. What they change in state comes back through the
    # gateway inbox ("_record", applied by drain_gateway).
    _holds_live.update({hid: h.get("at", 0) for hid, h in
                        (state.get("holds") or {}).items()})
    nl_recover(state)
    _nl_live.load(state)
    _update_announced["version"] = \
        (state.get("update_notice") or {}).get("version")
    threading.Thread(
        target=_interaction_thread_main,
        args=(_ix_inbox, lambda rec: _gateway_inbox.put({"_record": rec})),
        daemon=True, name="interactions").start()

    # A restart must not drop bursts staged before it: their messages are
    # already marked seen, so flush them immediately instead of waiting out
    # a coalesce window that mostly elapsed while the monitor was down.
    if state.get("pending"):
        queue = load_queue()
        n_before = len(queue)
        if flush_pending(state, queue, force=True):
            queue_edit(add=queue[n_before:])
    save_state(state)

    cycle = 0
    rotation = None          # the poll rotation in progress
    rotation_started = None  # when the latest rotation began
    backoff_seen = False     # REST backed off since the last cycle ended
    jobs = []                # background REST jobs (SweepPass, GenJob)
    poll_turn_next = True    # poll and jobs alternate when both are due
    outbox_pass_at = 0.0     # when process_outbox last ran
    while True:
        # Each phase below has its own try/except: one failing phase (a
        # poison message, a corrupt file, a plugin) must not skip the
        # sweep, discovery, cleanup, and state saves of every cycle.
        if _token_rejected or _gateway_status.get("token_rejected"):
            save_state(state)
            token_rejected_exit(_token_rejected
                                or "gateway closed with code 4004")
        touch_heartbeat()
        if BOT_USER_ID is None and _gateway_status.get("user"):
            _me = _gateway_status["user"]
            BOT_USER_ID = _me.get("id")
            BOT_ID = BOT_USER_ID
            BOT_NAME = _me.get("username")
            print(f"Bot user id: {BOT_USER_ID} (from gateway READY)",
                  flush=True)
        now = time.time()
        backing = rest_backing_off()
        backoff_seen = backoff_seen or backing
        if rotation is None and not backing and (
                rotation_started is None
                or now - rotation_started >= POLL_INTERVAL):
            # The first rotation is the startup catch-up: it reads what
            # arrived while the monitor was down, as fast as the slices
            # allow instead of paced.
            rotation = PollRotation(
                poll_targets(), now,
                spread=0.0 if rotation_started is None else POLL_SPREAD)
            rotation_started = now
        cycle_done = False
        dm_items = []
        queue = None  # read fresh below; None = unknown this cycle

        try:
            # Gateway fast path first: messages pushed in realtime are
            # classified (and triggers staged) like polled ones. Anything
            # the poll already processed is skipped. Guild join/leave
            # events ride the same inbox. Waits for the bot's identity,
            # like the poll.
            if BOT_USER_ID is not None:
                gw_mentions, gw_dms = drain_gateway(
                    state, _gateway_inbox, CHANNELS)
                if gw_mentions or gw_dms:
                    print(f"Gateway delivered {len(gw_mentions)} "
                          f"mention(s), {len(gw_dms)} DM(s).", flush=True)
                for msg, cname in gw_dms:
                    stage_trigger(state, msg, cname, None, "dm", dm=True)
        except Exception as e:
            print(f"Gateway drain error: {e}", flush=True)

        try:
            retire_overlay_changes()
        except Exception as e:
            print(f"Settings overlay error: {e}", flush=True)

        try:
            # /settings: the slash command (until registered once) and
            # the owners' digest of server-admin changes.
            register_commands(_gateway_status)
            flush_admin_digests()
        except Exception as e:
            print(f"/settings upkeep error: {e}", flush=True)

        try:
            nl_upkeep(state)  # settings requests: expiry, notices
        except Exception as e:
            print(f"Settings request upkeep error: {e}", flush=True)

        # One REST slice per tick (see "Sliced REST work"): the poll's,
        # or the background job's, alternating when both are due.
        rest_spent = 0.0
        poll_ready = rotation is not None and rotation.due(now) \
            and not backing
        jobs_ready = bool(jobs) and not backing
        poll_turn = poll_ready and (poll_turn_next or not jobs_ready)
        if poll_ready and jobs_ready:
            poll_turn_next = not poll_turn_next
        try:
            if poll_turn:
                t0 = time.time()
                rotation.run_slice(state, t0 + REST_SLICE_SECS, dm_items)
                rest_spent = time.time() - t0
        except Exception as e:
            print(f"Poll error: {e}", flush=True)
        if rotation is not None and rotation.done():
            rotation = None
            cycle += 1
            cycle_done = True

        try:
            # Stage polled DMs into the coalescing buffer (bursts from
            # the same author fold into one pending entry; guild triggers
            # were staged as they were classified). Matured bursts flush
            # into single queue items.
            # The queue snapshot only serves the duplicate checks: what
            # gets written is just the items appended to it, applied to a
            # fresh read under the lock (queue_edit), because the flush
            # can take tens of seconds (media downloads, ffmpeg) while the
            # monitor's own sends remove answered items.
            queue = load_queue()
            n_before = len(queue)
            for msg, cname in dm_items:
                stage_trigger(state, msg, cname, None, "dm", dm=True)

            dirty = flush_pending(state, queue)
            if dirty:
                queue_edit(add=queue[n_before:])
            save_state(state)
        except Exception as e:
            queue = None  # not trustworthy: the sweep must not write it
            print(f"Queue/staging error: {e}", flush=True)

        try:
            # Keep the typing indicator alive while replies are pending:
            # staged bursts too, so the user sees it while the burst is
            # being "read", not after the hold. The typing thread pings;
            # this only tells it which channels are waiting.
            typing_refresh(state)
        except Exception as e:
            print(f"Typing error: {e}", flush=True)

        try:
            # Send any replies drafted by the worker
            outbox_pass_at = time.time()
            process_outbox(state)
            save_state(state)
        except Exception as e:
            print(f"Outbox error: {e}", flush=True)

        # A rotation (cycle) just completed: the per-cycle work.
        recovery_cycle = False
        if cycle_done:
            # A cycle that saw a rate-limit backoff is a light recovery
            # (no discovery refresh) so recovery doesn't open with the
            # request burst that helped trip the limit.
            recovery_cycle = backoff_seen
            backoff_seen = rest_backing_off()

        try:
            if cycle_done:
                _rl_note_clean_cycle()
                if BOT_USER_ID is None:
                    try:
                        _me = api_get("/users/@me")
                        BOT_USER_ID = _me.get("id")
                        BOT_ID = BOT_USER_ID
                        BOT_NAME = _me.get("username")
                        print(f"Bot user id: {BOT_USER_ID} "
                              f"(recovered in-loop)", flush=True)
                    except Exception as e:
                        if getattr(e, "code", None) == 401:
                            token_rejected_exit("401 from /users/@me")
        except Exception as e:
            print(f"Identity retry error: {e}", flush=True)

        try:
            # Background REST jobs, due by cycle (POLL_INTERVAL each).
            # One of each kind at a time; they run in this order.
            if cycle_done:
                kinds = {j.name for j in jobs}
                if cycle % SWEEP_EVERY_CYCLES == 0 \
                        and BOT_USER_ID is not None \
                        and "Sweep" not in kinds:
                    jobs.append(SweepPass(list(CHANNELS)))
                # Periodically re-discover readable channels so
                # permission changes take effect without a restart.
                if (cycle % CHANNEL_REFRESH_CYCLES == 0
                        or _FORCE_DISCOVERY) and not recovery_cycle:
                    _FORCE_DISCOVERY = False
                    if "Channel refresh" not in kinds:
                        jobs.append(GenJob("Channel refresh",
                                           discovery_iter(state)))
                elif (cycle % THREAD_REFRESH_CYCLES == 0
                        and not recovery_cycle
                        and not kinds & {"Thread refresh",
                                         "Channel refresh"}):
                    jobs.append(GenJob("Thread refresh",
                                       thread_refresh_iter(state)))
        except Exception as e:
            print(f"Job scheduling error: {e}", flush=True)

        if jobs and not rest_backing_off():
            job = jobs[0]
            try:
                # On the poll's turn, only what is left of its budget.
                if not poll_turn or rest_spent < REST_SLICE_SECS:
                    job.run_slice(state, time.time() + REST_SLICE_SECS
                                  - rest_spent, queue is not None)
                if job.done():
                    jobs.pop(0)
            except Exception as e:
                print(f"{job.name} error: {e}", flush=True)
                jobs.pop(0)
        elif jobs and isinstance(jobs[0], SweepPass):
            jobs.pop(0)  # a backoff ends a sweep pass (see SweepPass)

        try:
            if cycle_done:
                flush_join_notices(state, GUILDS, CHANNELS)
                announce_update(state)
                expire_holds(state)
                sweep_media()
                sweep_generated()
                sweep_gen_state(state)
                prune_gateway_seen(state)
                save_state(state)
        except Exception as e:
            print(f"Cleanup error: {e}", flush=True)

        TM.tick(settings, GUILDS, _MEMBER_COUNTS)  # never raises
        # Event-driven wait: a gateway MESSAGE_CREATE wakes the loop at
        # once. Otherwise it idles in ticks of at most 2s, shorter while
        # REST slices are due (none while they are behind), a burst is
        # about to flush, or the outbox holds a reply.
        now = time.time()
        if rest_backing_off():
            wait_for = 2.0  # keep the fast path ticking during REST backoff
        elif jobs or (rotation is not None and rotation.due(now)):
            wait_for = 0.0  # REST work is due: the next slice right away
        elif rotation is not None:
            wait_for = min(2.0, max(POLL_TICK_SECS,
                                    rotation.next_due() - now))
        elif rotation_started is not None:
            wait_for = min(2.0, max(
                0.0, POLL_INTERVAL - (now - rotation_started)))
        else:
            wait_for = 2.0
        burst_wait = pending_wait(state, now)
        if burst_wait is not None:
            # A burst can flush early (burst_quiet): wake when it may.
            wait_for = min(wait_for, burst_wait)
        # A reply the worker writes goes out within ~0.25s, even when it
        # lands mid-wait: the wait is cut into 0.25s steps that stat the
        # outbox.
        until = now + (0.0 if outbox_waiting(outbox_pass_at)
                       else wait_for)
        while True:
            left = until - time.time()
            if left <= 0 or _gateway_status["wake"].wait(
                    timeout=min(0.25, left)):
                break
            if outbox_waiting(outbox_pass_at):
                break
        _gateway_status["wake"].clear()

if __name__ == "__main__":
    try:
        main()
    except BaseException as e:
        TM.crashed(e)  # its class name only; SystemExit is not a crash
        raise
