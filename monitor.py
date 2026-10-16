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
     (config: safety.injection_quarantine).
  2. Rate limits: configurable global daily and per-user daily reply
     caps bound the token budget an attacker could burn by spamming
     mentions (config: safety.*).
  3. Output validation: worker-drafted replies are length-capped and
     mass mentions (@everyone/@here) are neutralized before sending.
  4. Send dedup: never send two replies to the same message (protects
     against overlapping worker runs).
  5. The bot token is read from the configured token file (or env var)
     at startup and is never written to any state file. The worker never
     holds the token; it only reads and writes the queue/outbox files.

Configuration: config.yaml next to this file (or --config PATH, or the
MUSE_DISCORD_CONFIG env var). See config.example.yaml for the schema.
"""
import copy
import importlib.util
import json
import os
import queue as pyqueue
import random
import re
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

try:
    import yaml
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
MEDIA_DIR = os.path.join(BASE_DIR, "media")
GENERATED_DIR = os.path.join(BASE_DIR, "generated")
PLUGINS_DIR = os.path.join(BASE_DIR, "plugins")

DEFAULT_CONFIG = {
    "bot": {"token_file": "~/.config/muse-discord/bot_token",
            "token_env": None, "guild_ids": []},
    "owners": {"user_ids": []},
    "behavior": {"respond_to": ["mention", "reply_to_bot"],
                 "call_names": [], "active_guild_ids": [],
                 "dm_policy": "owner_only", "coalesce_seconds": 5,
                 "max_reply_chars": 1800},
    "safety": {"injection_quarantine": True, "daily_reply_cap": 30,
               "per_user_daily_cap": None,
               "burst_breaker": {"max_replies": 5, "window_minutes": 10,
                                 "cooldown_minutes": 30}},
    "engine": {"rest_poll_seconds": 30, "discovery_refresh_minutes": 30,
               "sweep": {"every_cycles": 6, "grace_seconds": 240,
                         "max_age_hours": 6, "max_recoveries": 2},
               "backoff": {"initial_seconds": 60, "max_seconds": 900}},
    "presence": {"enabled": True, "status_text": "for mentions",
                 "status_kind": "watching"},
    "persona": {"file": "persona.md"},
    "plugins": {"welcome": {"enabled": False},
                "media_generation": {"enabled": False}},
}


def _deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _config_path():
    if len(sys.argv) > 2 and sys.argv[1] == "--config":
        return sys.argv[2]
    return os.environ.get("MUSE_DISCORD_CONFIG") or os.path.join(
        BASE_DIR, "config.yaml")


def load_config():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    path = _config_path()
    if os.path.exists(path):
        with open(path) as f:
            user_cfg = yaml.safe_load(f) or {}
        cfg = _deep_merge(cfg, user_cfg)
    return cfg


CFG = load_config()

# --- Resolved settings (read once at import) ---
BOT_CFG = CFG.get("bot") or {}
OWNER_USER_IDS = {str(u) for u in
                  (CFG.get("owners") or {}).get("user_ids") or []}
GUILD_ALLOWLIST = {str(g) for g in BOT_CFG.get("guild_ids") or []}
_BEHAVIOR = CFG.get("behavior") or {}
RESPOND_TO = set(_BEHAVIOR.get("respond_to") or ["mention", "reply_to_bot"])
# Names people address the bot by in chat. Only used by the opt-in
# "name" trigger; deliberately separate from the Discord username.
CALL_NAMES = [str(n).strip() for n in _BEHAVIOR.get("call_names") or []
              if str(n).strip()]
CALL_NAME_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(n.lower()) for n in CALL_NAMES) + r")\b"
) if CALL_NAMES else None
DM_POLICY = str(_BEHAVIOR.get("dm_policy") or "owner_only").lower()
COALESCE_SECS = float(_BEHAVIOR.get("coalesce_seconds") or 5)
MAX_REPLY_CHARS = int(_BEHAVIOR.get("max_reply_chars") or 1800)
_SAFETY = CFG.get("safety") or {}
INJECTION_QUARANTINE = bool(_SAFETY.get("injection_quarantine", True))
DAILY_REPLY_CAP = _SAFETY.get("daily_reply_cap")        # None = off
PER_USER_DAILY_CAP = _SAFETY.get("per_user_daily_cap")  # None = off
_BREAKER_CFG = _SAFETY.get("burst_breaker")             # None = off
BREAKER_ENABLED = isinstance(_BREAKER_CFG, dict) \
    and _BREAKER_CFG.get("enabled", True) is not False
_BREAKER_CFG = _BREAKER_CFG if isinstance(_BREAKER_CFG, dict) else {}
BREAKER_MAX_REPLIES = int(_BREAKER_CFG.get("max_replies") or 5)
BREAKER_WINDOW_SECS = float(_BREAKER_CFG.get("window_minutes") or 10) * 60
BREAKER_COOLDOWN_SECS = float(_BREAKER_CFG.get("cooldown_minutes") or 30) * 60
_ENGINE = CFG.get("engine") or {}
POLL_INTERVAL = int(_ENGINE.get("rest_poll_seconds") or 30)
_SWEEP_CFG = _ENGINE.get("sweep") or {}
SWEEP_EVERY_CYCLES = int(_SWEEP_CFG.get("every_cycles") or 6)
SWEEP_GRACE_SECS = float(_SWEEP_CFG.get("grace_seconds") or 240)
SWEEP_MAX_AGE_SECS = float(_SWEEP_CFG.get("max_age_hours") or 6) * 3600
SWEEP_MAX_RECOVERIES = int(_SWEEP_CFG.get("max_recoveries") or 2)
_BACKOFF_CFG = _ENGINE.get("backoff") or {}
BACKOFF_INITIAL_SECS = float(_BACKOFF_CFG.get("initial_seconds") or 60)
BACKOFF_MAX_SECS = float(_BACKOFF_CFG.get("max_seconds") or 900)
CHANNEL_REFRESH_CYCLES = max(
    1, round(float(_ENGINE.get("discovery_refresh_minutes") or 30) * 60
             / POLL_INTERVAL))


def _load_token():
    # argv (manual/transient use) wins; then the configured env var; then
    # the configured token file. The watchdog never passes a token.
    if len(sys.argv) > 1 and sys.argv[1] != "--config":
        return sys.argv[1]
    env_name = BOT_CFG.get("token_env")
    if env_name and os.environ.get(env_name):
        return os.environ[env_name].strip()
    token_file = BOT_CFG.get("token_file") \
        or "~/.config/muse-discord/bot_token"
    try:
        with open(os.path.expanduser(token_file)) as f:
            return f.read().strip()
    except OSError:
        return None


TOKEN = _load_token()
if not TOKEN:
    print("No bot token: pass one as argv[1], set bot.token_env, or store "
          "it in the configured bot.token_file (mode 600).",
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
# download from an arbitrary server.
EMBED_MEDIA_HOST_SUFFIXES = ("discordapp.com", "discordapp.net", "tenor.com", "giphy" + ".com", "klipy.com")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
CTYPE_EXT = {"image/gif": ".gif", "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}

# Video: download the clip, extract a few frames for the worker to view.
# The worker sees still frames only: no motion between them, no audio.
MAX_VIDEO_BYTES = 32 * 1024 * 1024
VIDEO_FRAME_COUNT = 5
VIDEO_EXTS = (".mp4", ".mov", ".webm", ".avi", ".mkv")
FFMPEG_TIMEOUT = 60

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

# Generated media: when the media_generation plugin grants permission
# on a queue item, the worker may write generated files into
# GENERATED_DIR; the send path attaches files from this directory only
# (validated in _validate_gen_paths). Generation approvals are
# single-use and keyed by the triggering message id, because the
# worker's outbox item does not carry the permission flag.
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
# GUILDS: {guild_id: {"name", "system_channel_id", "joined_at"}}
CHANNELS = {}
GUILDS = {}

# Channel display for logs, history context, and queue items. Guild-qualified
# so the worker can tell one server's #general from another's.
def channel_display(info):
    return "%s #%s" % (info.get("guild_name", "?"), info.get("name", "?"))

# Channel types the bot can meaningfully read: 0 = text, 5 = announcement.
READABLE_CHANNEL_TYPES = (0, 5)

# Re-check for brand-new threads this often (in poll cycles). Threads are
# where forum posts live; a new post should be picked up within a few
# minutes, not at the next full channel refresh.
THREAD_REFRESH_CYCLES = 5


def get_guilds():
    """Return [(guild_id, guild_name)] for every guild the bot is in
    (restricted to bot.guild_ids when that config list is non-empty)."""
    try:
        out = [(g["id"], g.get("name") or g["id"])
               for g in api_get("/users/@me/guilds")]
    except Exception as e:
        print(f"Guild discovery failed: {e}", flush=True)
        return []
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


def discover_channels():
    """Return ({channel_id: info}, {guild_id: guild_info}).

    Covers every guild text channel the bot can actually read right now,
    across every guild it belongs to. Read access is probed per channel
    (private channels answer 403 and are skipped quietly). Each channel
    info carries can_send: the bot's effective Send Messages permission
    (for threads, Send Messages in Threads on the parent channel),
    computed from guild roles + channel overwrites; None when the
    computation was unavailable."""
    found = {}
    guilds_out = {}
    for gid, gname in get_guilds():
        try:
            gobj = api_get(f"/guilds/{gid}")
        except Exception as e:
            print(f"Guild fetch failed for {gname}: {e}", flush=True)
            gobj = {}
        base_perms, member_roles, joined_at = _guild_base_perms(gid, gobj)
        guilds_out[gid] = {
            "name": gname,
            "system_channel_id": gobj.get("system_channel_id"),
            "joined_at": joined_at,
        }
        try:
            channels = api_get(f"/guilds/{gid}/channels")
        except Exception as e:
            print(f"Channel list failed for {gname}: {e}", flush=True)
            continue
        names_by_id = {ch["id"]: ch.get("name", "?") for ch in channels}
        perms_by_id = {}
        for ch in channels:
            if ch.get("type") not in READABLE_CHANNEL_TYPES:
                continue
            cid = ch["id"]
            try:
                api_get(f"/channels/{cid}/messages?limit=1")
            except Exception:
                continue  # no read access; skip quietly
            can_send = None
            if base_perms is not None:
                perms = _channel_perms(gid, base_perms,
                                       ch.get("permission_overwrites"),
                                       member_roles)
                perms_by_id[cid] = perms
                can_send = bool(perms & PERM_SEND_MESSAGES)
            found[cid] = {"name": ch["name"], "guild_id": gid,
                          "guild_name": gname, "can_send": can_send}
        # Threads (forum posts, text-channel threads) never appear in the
        # guild channel list, so they need their own discovery or the bot
        # is blind inside every post.
        found.update(discover_threads_for_guild(gid, gname, names_by_id,
                                                found, perms_by_id))
    return found, guilds_out


def discover_threads_for_guild(gid, gname, names_by_id, known_ids,
                               perms_by_id=None):
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
    is None (light thread-only refresh), can_send is left unknown."""
    out = {}
    try:
        resp = api_get(f"/guilds/{gid}/threads/active")
    except Exception as e:
        print(f"Thread list failed for {gname}: {e}", flush=True)
        return out
    for t in resp.get("threads", []):
        tid = t["id"]
        if tid in known_ids or tid in out:
            continue
        try:
            api_get(f"/channels/{tid}/messages?limit=1")
        except Exception:
            continue  # no read access; skip quietly
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
    return out


def seed_channel_seen(cid, cname, state):
    """Mark a newly discovered channel's current messages as seen so its
    history is not treated as fresh mentions."""
    try:
        msgs = api_get(f"/channels/{cid}/messages?limit=5")
        if msgs:
            newest = max(msgs, key=lambda m: int(m["id"]))
            state["last_seen"][cid] = newest["id"]
            print(f"Tracking new channel #{cname} from message "
                  f"{newest['id']}.", flush=True)
    except Exception as e:
        print(f"Init error #{cname}: {e}", flush=True)

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
    try:
        return fn()
    except Exception as e:
        if getattr(e, "code", None) == 429:
            _note_rate_limit(e)
        raise


def api_get(path):
    req = urllib.request.Request(API + path, headers=headers())

    def _do():
        with urllib.request.urlopen(req, timeout=20) as r:
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
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


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


def quarantine(msg, channel_name, matched_pattern):
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
    q = []
    if os.path.exists(QUARANTINE_FILE):
        with open(QUARANTINE_FILE) as f:
            q = json.load(f)
    q.append(entry)
    atomic_write(QUARANTINE_FILE, q[-100:])  # keep last 100
    ledger_record(msg["id"], "quarantined", matched_pattern,
                  channel_id=msg.get("channel_id"),
                  author=entry["author"])
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
                  recoveries=None):
    if not mid:
        return
    d = _ledger_load()
    ent = d["entries"].get(mid, {})
    ent.update({"status": status, "reason": reason,
                "channel_id": channel_id or ent.get("channel_id"),
                "author": author or ent.get("author"),
                "updated": time.time()})
    if recoveries is not None:
        ent["recoveries"] = recoveries
    d["entries"][mid] = ent
    _ledger_save(d)


def _ledger_terminal(mid):
    return _ledger_load()["entries"].get(mid, {}).get("status") \
        in LEDGER_TERMINAL


def _quarantine_ids():
    if os.path.exists(QUARANTINE_FILE):
        try:
            with open(QUARANTINE_FILE) as f:
                return {e.get("message_id") for e in json.load(f)}
        except Exception:
            pass
    return set()


def _snowflake_ts(mid):
    try:
        return ((int(mid) >> 22) + 1420070400000) / 1000.0
    except Exception:
        return 0.0


def rate_allows(state, user_id, channel_id=None):
    """Enforce the configured reply caps (safety.daily_reply_cap and
    safety.per_user_daily_cap; either may be null = off). Owners are
    never capped: these limits bound abuse and token spend from
    strangers, not the owner's own use of the bot."""
    if user_id in OWNER_USER_IDS:
        return True
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rate = state["rate"]

    if rate["day"] != today:
        rate["day"] = today
        rate["day_count"] = 0
        rate["users"] = {}

    if DAILY_REPLY_CAP is not None and rate["day_count"] >= DAILY_REPLY_CAP:
        print(f"Rate limit: daily cap ({DAILY_REPLY_CAP}) reached, "
              f"skipping.", flush=True)
        return False

    if PER_USER_DAILY_CAP is not None:
        day_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp()
        user_hits = [t for t in rate["users"].get(user_id, [])
                     if t >= day_start]
        rate["users"][user_id] = user_hits
        if len(user_hits) >= PER_USER_DAILY_CAP:
            print(f"Rate limit: user {user_id} hit per-user daily cap, "
                  f"skipping.", flush=True)
            return False

    return True


def rate_record(state, user_id):
    rate = state["rate"]
    rate["day_count"] += 1
    rate["users"].setdefault(user_id, []).append(time.time())


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
# Observe mode. Every server starts in observe mode: direct @mentions,
# replies to the bot, and DMs work, while ambient behaviors (name-drop
# triggers, welcomes) stay off until the owner lists the server in
# behavior.active_guild_ids. That list is re-read whenever config.yaml
# changes, so activating a server needs no restart. When the bot joins a
# new server, the owners get a DM summary.
# ---------------------------------------------------------------------------
_active_guilds = {"mtime": None,
                  "ids": {str(g) for g in
                          _BEHAVIOR.get("active_guild_ids") or []}}
_FORCE_DISCOVERY = False  # set when the gateway reports a new guild


def active_guild_ids():
    path = _config_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return _active_guilds["ids"]
    if mtime == _active_guilds["mtime"]:
        return _active_guilds["ids"]
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
        ids = {str(g) for g in
               (cfg.get("behavior") or {}).get("active_guild_ids") or []}
    except Exception as e:
        print(f"Config re-read failed ({e}); active servers unchanged.",
              flush=True)
        _active_guilds["mtime"] = mtime  # retry on the next save only
        return _active_guilds["ids"]
    if _active_guilds["mtime"] is not None and ids != _active_guilds["ids"]:
        for gid in sorted(ids ^ _active_guilds["ids"]):
            name = (GUILDS.get(gid) or {}).get("name", gid)
            print(f"Server {name} ({gid}) is now "
                  f"{'active' if gid in ids else 'in observe mode'}.",
                  flush=True)
    _active_guilds.update(mtime=mtime, ids=ids)
    return ids


def guild_active(gid):
    return bool(gid) and str(gid) in active_guild_ids()


def notify_owners(text):
    """DM every owner. Returns True when at least one DM landed (or there
    is nobody to tell). Never raises."""
    if not OWNER_USER_IDS:
        print(f"Owner notice (no owners configured): {text}", flush=True)
        return True
    delivered = False
    for uid in sorted(OWNER_USER_IDS):
        try:
            c = api_post("/users/@me/channels", {"recipient_id": uid})
            api_post(f"/channels/{c['id']}/messages",
                     {"content": text[:1990],
                      "allowed_mentions": {"parse": []}})
            delivered = True
        except Exception as e:
            print(f"Owner DM to {uid} failed: {e}", flush=True)
    return delivered


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
    return (f"I just joined **{name}** (server ID {gid}).\n"
            f"Mode: **observe**. I answer @mentions, replies to my "
            f"messages, and DMs there. Ambient behavior (name-drop "
            f"replies, welcomes) stays off.\n"
            f"Channels I can see ({len(visible)}): {shown or 'none'}\n"
            f"To activate that server, add \"{gid}\" to "
            f"behavior.active_guild_ids in config.yaml. No restart "
            f"needed.")


def sync_guilds(state, guilds, channels):
    """Record each guild's join watermark and greet newly joined guilds
    (observe mode, owner DM). The first run seeds the known set silently:
    the owner just set the bot up there."""
    if not guilds:
        return  # discovery failed; a partial view must not look like joins
    first_run = "known_guilds" not in state
    known = state.setdefault("known_guilds", {})
    notices = state.setdefault("join_notices", [])
    for gid, ginfo in guilds.items():
        note_guild_joined(state, gid, ginfo.get("joined_at"))
        is_new = gid not in known
        known[gid] = ginfo.get("name") or gid
        if is_new and not first_run:
            print(f"Joined server {known[gid]} ({gid}): observe mode until "
                  f"activated.", flush=True)
            if gid not in notices:
                notices.append(gid)
    flush_join_notices(state, guilds, channels)


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
                                           channels)):
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
        known = state.get("known_guilds")
        if known is not None and gid not in known:
            _FORCE_DISCOVERY = True
    else:
        name = state.get("known_guilds", {}).pop(gid, None)
        state.get("guild_joined", {}).pop(gid, None)
        state.get("guild_first_seen", {}).pop(gid, None)
        print(f"Removed from server {name or gid}.", flush=True)


# ---------------------------------------------------------------------------
# Burst breaker. If the bot sends more than safety.burst_breaker.max_replies
# replies within window_minutes in one server channel, it goes quiet there
# for cooldown_minutes and DMs the owners the triggering examples. A fuse
# against trigger misconfigurations, pile-ons, and loops even when every
# single trigger looked legitimate. Replies to the owners' own messages
# neither count nor get blocked. State: state["breaker"][channel_id] =
# {"sends": [{ts, author, trigger, reply}], "quiet_until"}.
# ---------------------------------------------------------------------------
def _owner_triggered(state, item):
    return item.get("reply_to") in state.get("owner_triggers", [])


def channel_quiet(state, cid):
    entry = state.get("breaker", {}).get(cid)
    return bool(entry) and entry.get("quiet_until", 0) > time.time()


def _trigger_text(state, cid, mid):
    """What the message a reply answers said: the excerpt kept at enqueue
    (trigger_texts), else the channel's rolling history."""
    text = state.get("trigger_texts", {}).get(mid)
    if text is None:
        text = next((h["text"] for h in state.get("history", {}).get(cid, [])
                     if h["id"] == mid), None)
    return text if text is not None else "(text unavailable)"


def breaker_admit(state, item):
    """Gate one outbox reply. Returns False when the channel is quiet or
    this reply would exceed the burst limit (which trips the breaker)."""
    cid = item.get("channel_id")
    if not BREAKER_ENABLED or cid not in CHANNELS \
            or _owner_triggered(state, item):
        return True
    now = time.time()
    entry = state.setdefault("breaker", {}).setdefault(cid, {"sends": []})
    entry["sends"] = [s for s in entry["sends"]
                      if now - s["ts"] < BREAKER_WINDOW_SECS]
    if entry.get("quiet_until", 0) > now:
        return False
    if len(entry["sends"]) < BREAKER_MAX_REPLIES:
        return True
    entry["quiet_until"] = now + BREAKER_COOLDOWN_SECS
    cname = channel_display(CHANNELS[cid])
    until = datetime.fromtimestamp(entry["quiet_until"], timezone.utc)
    print(f"BURST BREAKER tripped in {cname}: {len(entry['sends'])} "
          f"replies in {BREAKER_WINDOW_SECS / 60:.0f} min; quiet until "
          f"{until:%H:%M} UTC.", flush=True)
    examples = [f"- {s['author']}: \"{s['trigger'][:120]}\""
                for s in entry["sends"]]
    blocked = _trigger_text(state, cid, item.get("reply_to"))
    examples.append(f"- {item.get('author')} (not answered): "
                    f"\"{blocked[:120]}\"")
    notify_owners(
        f"Heads-up: I sent {len(entry['sends'])} replies in {cname} "
        f"within {BREAKER_WINDOW_SECS / 60:.0f} minutes, so I've stopped "
        f"replying there until {until:%H:%M} UTC. Your own messages "
        f"still get answers.\nWhat triggered me:\n" + "\n".join(examples))
    return False


def breaker_record(state, item, parts):
    cid = item.get("channel_id")
    if not BREAKER_ENABLED or cid not in CHANNELS \
            or _owner_triggered(state, item):
        return
    entry = state.setdefault("breaker", {}).setdefault(cid, {"sends": []})
    entry["sends"].append({
        "ts": time.time(),
        "author": item.get("author") or "?",
        "trigger": _trigger_text(state, cid, item.get("reply_to")),
        "reply": (parts[0] if parts else "")[:120],
    })


def sweep_breaker(state):
    """Forget channels with no recent sends and no active cooldown."""
    now = time.time()
    breaker = state.get("breaker", {})
    for cid in list(breaker):
        entry = breaker[cid]
        if entry.get("quiet_until", 0) <= now and not any(
                now - s["ts"] < BREAKER_WINDOW_SECS
                for s in entry.get("sends", [])):
            del breaker[cid]


def is_mention(msg):
    """True when the message @mentions the bot on the platform. Plain
    text containing the bot's name is NOT a mention: in a server whose
    topic shares the bot's name, that made every casual "my muse ..." a
    summons. Bot authors never count: they cannot trigger replies, which
    also prevents bot-to-bot loops."""
    author = msg.get("author", {}) or {}
    if author.get("id") == BOT_ID:
        return False
    if author.get("bot"):
        return False
    return any(m.get("id") == BOT_ID for m in msg.get("mentions", []))


def is_name_drop(msg):
    """True when the text contains one of behavior.call_names as a
    standalone word. Only consulted when "name" is in respond_to, and
    even then the hit is only a "maybe addressed to you": the worker
    decides, and its default verdict is silence."""
    return bool(CALL_NAME_RE
                and CALL_NAME_RE.search((msg.get("content") or "").lower()))


def trigger_kind(msg):
    """Classify how a message addresses the bot, per behavior.respond_to:
    "mention" (a platform @mention or a reply to the bot: direct address),
    "name_drop" (opt-in bare call-name hit: low priority, worker decides),
    or None. Bot authors never trigger."""
    author = msg.get("author", {}) or {}
    if author.get("id") == BOT_ID or author.get("bot"):
        return None
    if "mention" in RESPOND_TO and is_mention(msg):
        return "mention"
    if "reply_to_bot" in RESPOND_TO and is_reply_to_bot(msg):
        return "mention"
    if "name" in RESPOND_TO and is_name_drop(msg):
        return "name_drop"
    return None


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


def _upgrade_burst(entry, kind):
    """A direct mention folding into a name-drop burst makes the whole
    burst direct address."""
    if kind == "mention" and entry.get("kind") == "name_drop":
        entry["kind"] = "mention"


def hold_fragment(state, channel_id, msg, kind=None):
    """If (channel, author) already has a burst being coalesced, append this
    message to it as a fragment and return True (caller must not classify it
    independently). The trigger message itself is never a fragment."""
    author_id = (msg.get("author", {}) or {}).get("id")
    key = f"{channel_id}:{author_id}"
    entry = state.get("pending", {}).get(key)
    if entry and msg["id"] != entry["first_id"]:
        entry["fragments"].append(msg)
        _upgrade_burst(entry, kind)
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
        pending[key]["fragments"].append(msg)
        _upgrade_burst(pending[key], kind)
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
        "kind": kind,  # "mention" | "name_drop" | "dm"
        "dm": dm,
        "first_seen": time.time(),
        "trigger": msg,
        "fragments": [],
    }
    print(f"Staged {kind} from {(msg.get('author', {}) or {}).get('username')} "
          f"in {cname} for coalescing.", flush=True)


def flush_pending(state, queue, force=False):
    """Turn pending bursts whose coalesce window has elapsed into single
    queue items. Returns True when anything was queued."""
    now = time.time()
    pending = state.get("pending", {})
    flushed = False
    for key in list(pending.keys()):
        entry = pending[key]
        if not force and entry.get("next_try", 0) > now:
            continue  # enqueue failed recently; backing off before retry
        if force or now - entry["first_seen"] >= COALESCE_SECS:
            trigger = entry["trigger"]
            if entry["kind"] == "dm":
                ok = enqueue_message(trigger, entry["channel_name"], state,
                                     queue, dm=True,
                                     fragments=entry["fragments"],
                                     guild_id=entry["guild_id"])
            else:
                ok = enqueue_message(trigger, entry["channel_name"], state,
                                     queue, fragments=entry["fragments"],
                                     guild_id=entry["guild_id"],
                                     kind=entry["kind"])
            n_frag = len(entry["fragments"])
            print(f"Flushed {entry['kind']} burst "
                  f"({1 + n_frag} message(s)) from "
                  f"{(trigger.get('author', {}) or {}).get('username')} in "
                  f"{entry['channel_name']}: queued={ok}", flush=True)
            if ok:
                del pending[key]
                flushed = True
            elif any(q["message_id"] == trigger["id"] for q in queue):
                # Already queued by another path (e.g. the sweep): the
                # enqueue "failure" was just its duplicate guard.
                del pending[key]
            elif trigger["id"] in _quarantine_ids() \
                    or _ledger_terminal(trigger["id"]):
                # quarantine() or an enqueue gate (join watermark,
                # burst breaker) already ledgered it.
                del pending[key]
            else:
                # No silent drops: retry a few times, then record the
                # loss in the ledger where the sweep can see it.
                entry["tries"] = entry.get("tries", 0) + 1
                if entry["tries"] >= 3:
                    ledger_record(
                        trigger["id"], "skipped",
                        f"enqueue failed {entry['tries']}x",
                        channel_id=entry["channel_id"],
                        author=(trigger.get('author', {}) or {})
                        .get('username'))
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


def send_reply(channel_id, text, reply_to=None):
    data = {"content": text}
    if reply_to:
        data["message_reference"] = {"message_id": reply_to}
    return api_post(f"/channels/{channel_id}/messages", data)


def send_typing(channel_id):
    """Show the typing indicator for ~10s. Signals responsiveness while
    the reply worker drafts the real reply."""
    try:
        api_post(f"/channels/{channel_id}/typing", {})
    except Exception:
        pass


def add_reaction(channel_id, message_id, emoji):
    """Add a Unicode emoji reaction to a message (PUT .../@me). Used for
    reactions drafted by the worker via the outbox 'reactions' list."""
    encoded = urllib.parse.quote(emoji)
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
    part. Raises on the first failure: the caller drops (never requeues) a
    partially-sent fragmented reply, so sent parts are never duplicated.
    """
    last_idx = len(parts) - 1
    for i, part in enumerate(parts):
        if i > 0:
            send_typing(channel_id)
            time.sleep(random.uniform(2.0, 4.0))
        if i == last_idx and attachments:
            send_reply_with_files(channel_id, part, attachments,
                                  reply_to if i == 0 else None)
        elif i == 0:
            send_reply(channel_id, part, reply_to)
        else:
            send_reply(channel_id, part)


ATTACH_CTYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".mp4": "video/mp4",
}


def send_reply_with_files(channel_id, text, files, reply_to=None):
    """Send a message with file attachments via multipart/form-data.
    files: list of (local_path, filename)."""
    boundary = "----MuseDiscord%d" % int(time.time() * 1000)
    payload = {"content": text}
    if reply_to:
        payload["message_reference"] = {"message_id": reply_to}
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
    """Delete generated files older than GENERATED_TTL_SECS (already sent)."""
    if os.path.isdir(GENERATED_DIR):
        now = time.time()
        for fname in os.listdir(GENERATED_DIR):
            fpath = os.path.join(GENERATED_DIR, fname)
            try:
                if now - os.path.getmtime(fpath) > GENERATED_TTL_SECS:
                    os.remove(fpath)
            except OSError:
                pass


def sweep_gen_state(state):
    """Drop generation approvals older than GENERATED_TTL_SECS (they are
    single-use and short-lived by design) and bound the sent-file record."""
    approved = state.get("gen_approved", {})
    now = time.time()
    for mid in [m for m, ts in approved.items()
                if now - ts > GENERATED_TTL_SECS]:
        approved.pop(mid, None)
    if len(state.get("gen_sent", [])) > 50:
        state["gen_sent"] = state["gen_sent"][-50:]


def _validate_gen_paths(paths):
    """Keep only paths that resolve inside GENERATED_DIR and exist."""
    gen_root = os.path.realpath(GENERATED_DIR)
    candidates = []
    for p in paths or []:
        if not isinstance(p, str):
            continue
        # Workers sometimes hand over paths relative to $HOME.
        ap = p if os.path.isabs(p) else os.path.join(
            os.path.expanduser("~"), p)
        rp = os.path.realpath(ap)
        if not rp.startswith(gen_root + os.sep):
            print(f"Dropping attachment outside generated dir: {p}", flush=True)
            continue
        if not os.path.isfile(rp):
            print(f"Dropping missing attachment: {p}", flush=True)
            continue
        candidates.append(rp)
    return candidates


def _discover_fresh_generated(state, max_age_secs=GENERATED_TTL_SECS):
    """Newest unsent generated files younger than max_age_secs. Used by the
    owner fallback when the worker couldn't name the file it generated."""
    gen_root = os.path.realpath(GENERATED_DIR)
    if not os.path.isdir(gen_root):
        return []
    sent = set(state.get("gen_sent", []))
    now = time.time()
    fresh = []
    for fname in os.listdir(gen_root):
        if fname.endswith(".json") or ".fit." in fname:
            continue  # sidecars and compressed copies, not originals
        fpath = os.path.join(gen_root, fname)
        if fpath in sent or not os.path.isfile(fpath):
            continue
        try:
            mt = os.path.getmtime(fpath)
        except OSError:
            continue
        if now - mt <= max_age_secs:
            fresh.append((mt, fpath))
    fresh.sort(reverse=True)
    return [fp for _, fp in fresh[:1]]


def _discover_generated(state, approved_ts):
    """Find the newest generated media file created after the approval that
    hasn't been sent yet. The worker often isn't told the local filename
    (the media tool returns a remote URL/handle), so the monitor matches
    the file itself. Returns a local path or None."""
    gen_root = os.path.realpath(GENERATED_DIR)
    if not os.path.isdir(gen_root):
        return None
    sent = set(state.get("gen_sent", []))
    best = None
    for fname in os.listdir(gen_root):
        if fname.endswith(".json") or ".fit." in fname:
            continue  # sidecars and compressed copies, not originals
        fpath = os.path.join(gen_root, fname)
        if fpath in sent or not os.path.isfile(fpath):
            continue
        try:
            mt = os.path.getmtime(fpath)
        except OSError:
            continue
        if mt >= approved_ts - 10 and (best is None or mt > best[1]):
            best = (fpath, mt)
    return best[0] if best else None


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
    """
    ua = headers()["User-Agent"]
    # Path 1: curl (proven reliable on this network)
    try:
        r = subprocess.run(
            ["curl", "-sS", "-L", "--max-time", str(timeout),
             "--max-filesize", str(max_bytes), "-A", ua,
             "-w", "\n%{content_type}", url],
            capture_output=True, timeout=timeout + 10)
        if r.returncode == 0 and r.stdout:
            body, _, ctype = r.stdout.rpartition(b"\n")
            ctype = ctype.decode(errors="replace").split(";")[0].strip().lower()
            if body and len(body) <= max_bytes:
                return bytes(body), ctype
        print("curl %s download failed: %s" %
              (kind, r.stderr.decode(errors="replace")[:150]), flush=True)
    except FileNotFoundError:
        pass  # no curl installed; fall through to urllib
    except Exception as e:
        print("curl %s download failed: %s" % (kind, e), flush=True)
    # Path 2: urllib with length verification + Range resume
    data = bytearray()
    expected = None
    ctype = ""
    for _ in range(4):
        try:
            h = {"User-Agent": ua}
            if data:
                h["Range"] = "bytes=%d-" % len(data)
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
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
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", video_path],
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
                ["ffmpeg", "-v", "error", "-ss", str(t), "-i", video_path,
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
    host = urllib.parse.urlparse(url).hostname or ""
    return host.endswith(EMBED_MEDIA_HOST_SUFFIXES)


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
        hist.append({
            "id": m["id"],
            "author": author.get("username", "?"),
            "text": text or "[shared media]",
            "ts": m.get("timestamp"),
        })
        seen_ids.add(m["id"])
    state["history"][channel_id] = hist[-HISTORY_PER_CHANNEL:]


def check_channel(channel_id, channel_name, state, guild_id=None):
    """Check for new messages in a channel. Returns (triggers, joins):
    triggers are (msg, kind) pairs that need the worker (kind as in
    trigger_kind), joins are Discord system join messages (type 7) from
    the system channel."""
    last_seen = state["last_seen"].get(channel_id)

    params = {"limit": 20}
    if last_seen:
        params["after"] = last_seen

    query = urllib.parse.urlencode(params)
    try:
        messages = api_get(f"/channels/{channel_id}/messages?{query}")
    except Exception as e:
        print(f"Error reading #{channel_name}: {e}", flush=True)
        return [], []

    if not messages:
        return [], []

    record_history(state, channel_id, messages)

    newest = max(messages, key=lambda m: int(m["id"]))
    state["last_seen"][channel_id] = newest["id"]

    triggers = []
    joins = []
    for msg in sorted(messages, key=lambda m: int(m["id"])):
        kind = classify_message(state, msg, channel_name, guild_id)
        if kind == "join":
            joins.append(msg)
        elif kind:
            triggers.append((msg, kind))

    return triggers, joins


def classify_message(state, msg, channel_name, guild_id=None):
    """Classify a single message exactly the way check_channel's loop does.

    Shared by the REST poll path and the gateway fast path so both ingest
    routes behave identically. Returns "mention", "name_drop", "join", or
    None (folded into a burst, skipped, or not actionable).
    """
    if predates_join(state, msg, guild_id):
        # Older than the bot's arrival in this guild: never a trigger,
        # never a fragment. It can still reach the worker as history.
        return None
    if is_join_message(msg):
        # Welcomes are ambient: off while the server is in observe mode.
        return "join" if guild_active(guild_id) else None
    author = msg.get("author", {}) or {}
    if author.get("id") == BOT_ID or author.get("bot"):
        # The bot's own messages, and any other bot's: never triggers.
        # (Bot-authored messages also can't loop the bot into replying to
        # itself or into bot-to-bot chains.)
        return None
    kind = trigger_kind(msg)
    if kind == "name_drop" and not guild_active(guild_id):
        kind = None  # name-drops are ambient: off in observe mode
    if kind:
        # The trigger check runs BEFORE the generic fragment fold: a
        # mention arriving while the author's burst is still open folds
        # into that burst (which flushes with mention semantics), but it
        # must be classified as a mention first, never swallowed
        # unclassified by the fragment path.
        if hold_fragment(state, msg.get("channel_id"), msg, kind):
            return None
        return kind
    if hold_fragment(state, msg.get("channel_id"), msg):
        return None  # folded into a burst already being coalesced
    return None


def is_join_message(msg):
    """True for Discord's system join messages (type 7: 'X joined the
    server'). The author field is the new member. Bots are skipped."""
    if msg.get("type") != 7:
        return False
    author = msg.get("author", {}) or {}
    if author.get("bot"):
        return False
    if author.get("id") == BOT_ID:
        return False
    return True


# Discord does not let bots list DM channels: GET /users/@me/channels always
# returns an empty list for bot tokens. Instead, resolve each known user's DM
# channel directly with POST /users/@me/channels (create-or-get: returns the
# existing channel if there is one, creates an empty one otherwise, and never
# sends a message).
#
# Which users get resolved for REST polling follows behavior.dm_policy:
# "owner_only" (the default) resolves only the owners' DM channels, so
# anyone else who DMs the bot is silently ignored: the bot never sees their
# messages, so there is nothing to decline or deflect. "open" cannot be
# enumerated (there is no DM list endpoint for bots), so DMs then arrive
# via the gateway only. "off" disables DM polling entirely.
def dm_poll_user_ids():
    if DM_POLICY == "owner_only":
        return sorted(OWNER_USER_IDS)
    return []


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
    return out


def check_dm_channel(dm_id, dm_name, state):
    """Return unseen non-bot messages in a DM channel. In a DM every message
    is addressed to the bot, so no mention filtering is needed."""
    try:
        messages = api_get(f"/channels/{dm_id}/messages?limit=20")
    except Exception as e:
        print(f"DM poll error #{dm_name}: {e}", flush=True)
        return []
    if not messages:
        return []

    record_history(state, dm_id, messages)

    last_seen = state["last_seen"].get(dm_id)
    new = [m for m in messages if not last_seen or int(m["id"]) > int(last_seen)]
    if new:
        newest = max(new, key=lambda m: int(m["id"]))
        state["last_seen"][dm_id] = newest["id"]

    return [m for m in sorted(new, key=lambda m: int(m["id"]))
            if m.get("author", {}).get("id") != BOT_USER_ID
            and not hold_fragment(state, dm_id, m)]


def enqueue_message(msg, cname, state, queue, dm=False, fragments=None,
                    guild_id=None, kind="mention"):
    """Run one message (plus any burst fragments coalesced with it) through
    the quarantine/rate-limit/media pipeline and append a SINGLE item to
    the queue. kind is "mention" or "name_drop" (ignored for DMs).
    Returns True if queued."""
    fragments = [f for f in (fragments or []) if f["id"] != msg["id"]]
    if any(q["message_id"] == msg["id"] for q in queue):
        return False
    author_id = msg.get("author", {}).get("id")
    author_name = msg.get("author", {}).get("username")

    # 0. Hard gates, re-checked here because every path (poll, gateway,
    #    restart flush, sweep) funnels through this function.
    if predates_join(state, msg, guild_id):
        ledger_record(msg["id"], "skipped", "predates bot join",
                      channel_id=msg.get("channel_id"), author=author_name)
        return False
    if BREAKER_ENABLED and author_id not in OWNER_USER_IDS \
            and channel_quiet(state, msg["channel_id"]):
        ledger_record(msg["id"], "skipped", "channel quiet (burst breaker)",
                      channel_id=msg.get("channel_id"), author=author_name)
        print(f"Burst breaker: not queueing {author_name} in {cname} "
              f"(channel quiet).", flush=True)
        return False
    # The worker sees the whole burst as one message: fragments joined in
    # order under the trigger's text.
    parts = [msg.get("content", "")]
    parts += [f.get("content", "") for f in fragments]
    content = "\n".join(p for p in parts if p)

    # 1. Injection pre-filter -> quarantine, no auto-reply (combined text).
    if INJECTION_QUARANTINE:
        matched = is_suspicious(content)
        if matched:
            quarantine(msg, cname, matched)
            return False

    # 2. Rate limits (also bound token spend). One charge per burst, not
    #    per fragment.
    if not rate_allows(state, author_id, msg["channel_id"]):
        return False
    rate_record(state, author_id)

    # 3. Download viewable media so the worker can see it, and extract
    #    document text so the worker can read attachments (trigger + every
    #    fragment).
    media, documents, unviewable = [], [], []
    for m in [msg] + fragments:
        md, dc, uv = collect_media(m)
        media += md
        documents += dc
        unviewable += uv

    # 4. Attach recent conversation history (excluding the trigger and its
    #    fragments) so the worker replies with context.
    burst_ids = {msg["id"]} | {f["id"] for f in fragments}
    chan_hist = state.get("history", {}).get(msg["channel_id"], [])
    history = [h for h in chan_hist
               if h["id"] not in burst_ids][-HISTORY_ATTACHED:]

    # 5. Plugin gates. Plugins may annotate the item via the gate_message
    #    hook; the media_generation plugin uses it to grant may_generate
    #    to allowed users. The flag is PERMISSION, not a decision: the
    #    worker still judges from the full conversation context whether
    #    generation is actually wanted, and generates at most once per
    #    queue item. Queue time only records the permission, plus the
    #    single-use approval below (keyed by message id, because the
    #    worker's outbox item does not carry the flag); it must not claim
    #    anywhere that generation is starting.
    extra = {}
    for res in call_hook("gate_message",
                         {"state": state, "owners": OWNER_USER_IDS},
                         msg, guild_id):
        if isinstance(res, dict):
            extra.update(res)
    may_generate = bool(extra.pop("may_generate", False))
    if may_generate:
        state.setdefault("gen_approved", {})[msg["id"]] = time.time()

    # 6. Owner commands: stamped for the owners' messages only, from the
    #    verified user ID in code. The worker decides with natural-language
    #    understanding whether the message asks the bot to DO something.
    owner_command = author_id in OWNER_USER_IDS
    # Remember what the trigger said under every id the worker may reply
    # to, so a burst-breaker notice can quote it after the channel's
    # rolling history has moved on. Bounded.
    texts = state.setdefault("trigger_texts", {})
    for mid in [msg["id"]] + [f["id"] for f in fragments]:
        texts[mid] = content[:120]
    for mid in list(texts)[:-100]:
        del texts[mid]
    if owner_command:
        # Replies to the owners never count toward (or get stopped by)
        # the burst breaker; outbox items only carry reply_to, so keep
        # the ids the worker may reply under.
        state["owner_triggers"] = (state.get("owner_triggers", [])
                                   + [msg["id"]]
                                   + [f["id"] for f in fragments])[-200:]

    # 7. Reply context: when the trigger used Discord's reply feature, say
    #    whose message it was replying to so the worker knows whether the
    #    message was actually addressed to the bot or to someone else.
    reply_ctx = get_reply_context(msg)

    item = {
        "message_id": msg["id"],
        "channel_id": msg["channel_id"],
        "channel_name": cname,
        "guild_id": guild_id,
        "kind": "dm" if dm else kind,
        # name_drop items are "maybe addressed to you": the worker
        # decides from context, and its default verdict is silence.
        "priority": "low" if not dm and kind == "name_drop" else "normal",
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
    }
    item.update(extra)
    queue.append(item)
    burst_note = f" (+{len(fragments)} fragment(s))" if fragments else ""
    print(f"Queued {item['kind']} from {msg['author'].get('username')} in "
          f"{cname}{burst_note}: {content[:80]}", flush=True)
    if item["priority"] != "low":
        # A typing indicator promises a reply; a name-drop most likely
        # gets none.
        send_typing(msg["channel_id"])
    ledger_record(msg["id"], "queued",
                  channel_id=msg.get("channel_id"),
                  author=msg.get("author", {}).get("username"))
    return True


def process_outbox(state):
    """Send replies drafted by the worker, with output guardrails + dedup."""
    if not os.path.exists(OUTBOX_FILE):
        return
    with open(OUTBOX_FILE) as f:
        outbox = json.load(f)
    if not outbox:
        return

    remaining = []
    for item in outbox:
        reply_to = item.get("reply_to")
        # Dedup: never touch the same message twice (overlapping workers)
        if reply_to and reply_to in state["replied_to"]:
            print(f"Skipping duplicate reply to message {reply_to}", flush=True)
            ledger_record(reply_to, "replied", "duplicate suppressed",
                          channel_id=item.get("channel_id"),
                          author=item.get("author"))
            continue
        parts = normalize_parts(item)
        # Worker-drafted emoji reactions: applied to the reply_to
        # message. A react-only item may carry no text.
        reactions = [e for e in (item.get("reactions") or [])
                     if isinstance(e, str) and e.strip()][:5]
        if not parts and not reactions:
            continue
        if parts and not breaker_admit(state, item):
            print(f"Burst breaker: dropping reply to {item.get('author')} "
                  f"in {item.get('channel_name')} (channel quiet).",
                  flush=True)
            if reply_to:
                ledger_record(reply_to, "skipped",
                              "channel quiet (burst breaker)",
                              channel_id=item.get("channel_id"),
                              author=item.get("author"))
            continue
        # Attachments: only from the generated dir. The normal gate is
        # the monitor's may_generate approval for this message (tracked
        # in gen_approved; the worker's outbox item doesn't carry the
        # flag). Approvals are single-use.
        attachments = []
        approved = state.get("gen_approved", {})
        if reply_to in approved:
            approved_ts = approved.pop(reply_to)
            candidates = _validate_gen_paths(item.get("attachments"))
            if not candidates:
                # The media tool usually returns a remote URL/handle, not the
                # local filename, so the worker can't name the file. Find it.
                found = _discover_generated(state, approved_ts)
                if found:
                    print(f"Attaching discovered generated file: {found}", flush=True)
                    candidates.append(found)
                else:
                    print(f"Note: approved generation for {reply_to} but no "
                          f"generated file found", flush=True)
            for rp in candidates:
                send_path, fname = fit_for_discord(rp)
                if send_path:
                    attachments.append((send_path, fname))
                    state.setdefault("gen_sent", []).append(rp)
                else:
                    print(f"Attachment too large even compressed: {rp}", flush=True)
        elif item.get("attachments"):
            if GEN_PLUGIN_ENABLED:
                # The approval key is the trigger message id, but a
                # greenlight can arrive via another message, so the key
                # can miss even though generation ran through the
                # may_generate gate. If the worker listed attachments and
                # a fresh unsent generated file exists, attach it rather
                # than dropping it: generation itself is still gated, and
                # only fresh files from the generated dir qualify.
                candidates = _discover_fresh_generated(state)
                if candidates:
                    print(f"Approval-miss fallback: attaching "
                          f"{len(candidates)} fresh generated file(s) for "
                          f"item {item.get('message_id')}", flush=True)
                else:
                    print(f"Note: item {item.get('message_id')} listed "
                          f"attachments without generation approval; "
                          f"dropped", flush=True)
            else:
                candidates = []
                print(f"Note: item {item.get('message_id')} listed "
                      f"attachments but the media_generation plugin is "
                      f"disabled; dropped", flush=True)
            for rp in candidates:
                send_path, fname = fit_for_discord(rp)
                if send_path:
                    attachments.append((send_path, fname))
                    state.setdefault("gen_sent", []).append(rp)
                else:
                    print(f"Attachment too large even compressed: {rp}",
                          flush=True)
        if attachments:
            # A generation really ran and produced a sendable file. Let
            # plugins account for it via the generation_sent hook (the
            # media_generation plugin counts its daily cap here, at send
            # time, never at queue time).
            call_hook("generation_sent", {"state": state}, item)
        try:
            if parts:
                send_fragmented(item["channel_id"], parts, reply_to, attachments)
                print(f"Sent reply to {item.get('author')} in {item.get('channel_name')} "
                      f"({len(parts)} part(s)): {parts[0][:60]}", flush=True)
                breaker_record(state, item, parts)
            for emoji in reactions:
                if reply_to and add_reaction(item["channel_id"], reply_to, emoji):
                    print(f"Added reaction {emoji} for {item.get('author')} "
                          f"in #{item.get('channel_name')}", flush=True)
            if reply_to:
                state["replied_to"].append(reply_to)
                ledger_record(reply_to, "replied",
                              channel_id=item.get("channel_id"),
                              author=item.get("author"))
        except Exception as e:
            code = getattr(e, "code", None)
            if code == 403:
                # Permanent permission failure (the bot cannot post in
                # that channel): drop it, don't retry forever.
                print(f"Dropping outbox item for #{item.get('channel_name')}: "
                      f"403 Missing Access ({e})", flush=True)
                if reply_to:
                    ledger_record(reply_to, "skipped", "403 missing access",
                                  channel_id=item.get("channel_id"),
                                  author=item.get("author"))
                continue
            if len(parts) > 1:
                # Never requeue a partially-sent fragmented reply: retrying
                # would duplicate the parts that already landed.
                print(f"Dropping fragmented reply after send error: {e}",
                      flush=True)
                continue
            print(f"Failed to send reply: {e}", flush=True)
            remaining.append(item)
    atomic_write(OUTBOX_FILE, remaining)


PIDFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "monitor.pid")


def _acquire_singleton():
    """Ensure only one monitor runs. Uses a pidfile with stale detection:
    if the recorded PID is gone (or isn't this script), the lock is stale
    and gets replaced. Exits the process if a live peer holds the lock."""
    mypid = os.getpid()
    try:
        with open(PIDFILE) as f:
            oldpid = int(f.read().strip())
    except (OSError, ValueError):
        oldpid = None
    if oldpid and oldpid != mypid:
        try:
            with open(f"/proc/{oldpid}/cmdline", "rb") as f:
                cmdline = f.read().decode(errors="replace")
            alive = "monitor.py" in cmdline
        except OSError:
            alive = False
        if alive:
            print(f"Another monitor (pid {oldpid}) is already running; exiting.",
                  flush=True)
            sys.exit(0)
        print(f"Replacing stale monitor pidfile (pid {oldpid} gone).", flush=True)
    with open(PIDFILE, "w") as f:
        f.write(str(mypid))


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
# last_seen check in drain_gateway() prevents double-processing.
#
# Requires the Message Content privileged intent (Discord dev portal -> Bot
# -> Privileged Gateway Intents). Without the toggle, Discord closes the
# connection with code 4014 and we fall back to polling with a loud log.
# ---------------------------------------------------------------------------
GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
# GUILDS | GUILD_MESSAGES | DIRECT_MESSAGES | MESSAGE_CONTENT
GATEWAY_INTENTS = (1 << 0) | (1 << 9) | (1 << 12) | (1 << 15)


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
    while not status.get("stop"):
        try:
            async with websockets.connect(GATEWAY_URL, max_size=2 ** 22) as ws:
                hello = json.loads(await ws.recv())
                hb_interval = hello["d"]["heartbeat_interval"] / 1000.0
                if session_id and status.get("seq") is not None:
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
                        if op == 7:      # Discord asks us to reconnect
                            break
                        if op == 9:      # invalid session: re-identify fresh
                            session_id = None
                            status["seq"] = None
                            break
                        if op != 0:
                            continue     # hello / heartbeat ack / etc.
                        t = pkt.get("t")
                        if t == "READY":
                            session_id = pkt["d"]["session_id"]
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
                                "joined_at": d.get("joined_at")}})
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
            if e.code == 4014:
                print("GATEWAY 4014: the Message Content privileged "
                      "intent is not enabled for this bot application "
                      "(Discord developer portal -> Bot -> Privileged "
                      "Gateway Intents). Realtime ingest stays off; "
                      "REST polling continues.",
                      flush=True)
                status["intent_denied"] = True
                return
            print(f"Gateway closed (code {e.code}); reconnecting...",
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
        print(f"Gateway thread died: {e}", flush=True)
        status["dead"] = True


def drain_gateway(state, inbox, channels):
    """Pull gateway-delivered messages (all guilds + DMs) and classify them
    exactly like polled messages. Returns (triggers, joins, dms): guild
    triggers as (msg, cname, guild_id, kind) tuples, joins as (msg, cname,
    guild_id), DMs as (msg, cname). Guild join/leave events are applied
    to state on the way. Skips anything the REST poll already processed
    (last_seen)."""
    mentions, joins, dms = [], [], []
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
        cid = m.get("channel_id")
        if not cid:
            continue
        last = state["last_seen"].get(cid)
        if last and int(m["id"]) <= int(last):
            continue  # the poll loop already handled it
        gid = m.get("guild_id")
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
                channels[cid] = info
            cname = channel_display(info)
            record_history(state, cid, [m])
            state["last_seen"][cid] = m["id"]
            kind = classify_message(state, m, cname, gid)
            if kind == "join":
                joins.append((m, cname, gid))
            elif kind:
                mentions.append((m, cname, gid, kind))
        else:
            # DM event (no guild_id). The configured DM policy decides
            # whose DMs are even read: "owner_only" (default) drops
            # everyone else's DMs unread and unrecorded; "off" disables
            # DM handling; "open" accepts any human's DM.
            author = m.get("author", {}) or {}
            if author.get("bot"):
                if author.get("id") == BOT_ID:
                    record_history(state, cid, [m])
                    state["last_seen"][cid] = m["id"]
                continue  # own messages are context only; other bots, ignore
            if DM_POLICY == "off":
                continue
            if DM_POLICY == "owner_only" \
                    and author.get("id") not in OWNER_USER_IDS:
                continue
            record_history(state, cid, [m])
            state["last_seen"][cid] = m["id"]
            if hold_fragment(state, cid, m):
                continue  # folded into a burst already being coalesced
            dms.append((m, "DM with " + (author.get("username") or cid)))
    return mentions, joins, dms



# ---------------------------------------------------------------------------
# Reconciliation sweep. Every engine.sweep.every_cycles poll cycles,
# independently re-read recent channel history and diff every mention /
# reply-to-bot against the durable records (replied_to, queue, outbox,
# quarantine, ledger). Anything older than the grace period with no
# terminal state is requeued on the spot. This is the backstop that turns
# every silent failure mode (storm loss, mid-cycle kill, worker drop)
# into a delayed reply instead of a permanent miss. Aborts on the first
# 429: during a rate-limit storm the sweep must not add request pressure.
# (The SWEEP_* constants are resolved from config in the header block.)
# ---------------------------------------------------------------------------
def sweep_mentions(state, queue, channels):
    """Requeue mentions that no durable record accounts for. Returns True
    when the queue changed."""
    if rest_backing_off():
        return False
    entries = _ledger_load()["entries"]
    q_ids = {q.get("message_id") for q in queue}
    outbox_targets = set()
    if os.path.exists(OUTBOX_FILE):
        try:
            with open(OUTBOX_FILE) as f:
                outbox_targets = {i.get("reply_to") for i in json.load(f)}
        except Exception:
            pass
    quar_ids = _quarantine_ids()
    replied = set(state.get("replied_to", []))
    pending_ids = set()
    for entry in state.get("pending", {}).values():
        pending_ids.add(entry.get("first_id"))
        for frag in entry.get("fragments", []):
            pending_ids.add(frag.get("id"))
    now = time.time()
    changed = False
    for cid, info in list(channels.items()):
        cname = channel_display(info)
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
            if author.get("bot") or author.get("id") == BOT_ID:
                continue
            # Direct address only: name-drops are best-effort and most
            # get no reply, so "no reply" is not evidence of a loss.
            if trigger_kind(m) != "mention":
                continue
            if predates_join(state, m, info.get("guild_id")):
                continue
            age = now - _snowflake_ts(mid)
            if age < SWEEP_GRACE_SECS or age > SWEEP_MAX_AGE_SECS:
                continue
            if mid in replied:
                if entries.get(mid, {}).get("status") != "replied":
                    ledger_record(mid, "replied", "sweep sync", cid,
                                  author.get("username"))
                continue
            if mid in quar_ids or mid in q_ids or mid in outbox_targets \
                    or mid in pending_ids:
                continue  # accounted for and in flight
            ent = entries.get(mid)
            if ent and ent.get("status") in LEDGER_TERMINAL:
                continue
            if info.get("can_send") is False:
                ledger_record(mid, "skipped",
                              "no send permission in channel", cid,
                              author.get("username"))
                continue
            recs = (ent or {}).get("recoveries", 0)
            if recs >= SWEEP_MAX_RECOVERIES:
                if ent and ent.get("status") != "skipped":
                    ledger_record(mid, "skipped", "recovery cap reached",
                                  cid, author.get("username"))
                    print(f"Sweep: giving up on unrecoverable mention "
                          f"{mid} from {author.get('username')} in "
                          f"{cname} (recorded in mention ledger).",
                          flush=True)
                continue
            ok = enqueue_message(m, cname, state, queue, fragments=[],
                                 guild_id=info.get("guild_id"))
            if ok:
                changed = True
                q_ids.add(mid)
                ledger_record(mid, "queued",
                              f"sweep recovery #{recs + 1}", cid,
                              author.get("username"),
                              recoveries=recs + 1)
                print(f"Sweep recovered lost mention {mid} from "
                      f"{author.get('username')} in {cname}.", flush=True)
            elif not _ledger_terminal(mid):
                # (A gate inside enqueue, e.g. quarantine or the burst
                # breaker, may already have recorded a terminal state.)
                ledger_record(mid, "enqueue_failed",
                              "sweep enqueue failed", cid,
                              author.get("username"),
                              recoveries=recs + 1)
    return changed


# ---------------------------------------------------------------------------
# Plugins. A plugin is a folder under plugins/ with a plugin.py exposing
# register(api) -> {hook_name: callable}, plus a plugin.yaml describing
# its params. A plugin is loaded only when enabled in config
# (plugins.<name>.enabled: true). The core calls these hooks:
#
#   gate_message(ctx, msg, guild_id) -> dict | None
#       During enqueue: returned dicts are merged into the queue item.
#       The media_generation plugin uses this to set may_generate.
#   member_join(ctx, msg, guild_id) -> queue item dict | None
#       A member joined (Discord system message, type 7). The welcome
#       plugin resolves its target channel from its params and returns a
#       "welcome" queue item for the worker to draft.
#   generation_sent(ctx, outbox_item)
#       A generated attachment was actually sent for an outbox item.
#
# ctx carries {"state", "channels", "guilds"} (subset per hook). Plugins
# receive their params (the config's plugins.<name> block) via the api
# object at register time; api.param(key, guild_id, guild_name) resolves
# one param for one guild (see param_for).
# ---------------------------------------------------------------------------
PLUGIN_HOOKS = {}
GEN_PLUGIN_ENABLED = False  # set when the media_generation plugin loads


def param_for(params, key, guild_id=None, guild_name=None):
    """Resolve one plugin param for one guild, following the package's
    global + per_guild override pattern. Two shapes are supported:

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

    def __init__(self, name, params):
        self.name = name
        self.params = params or {}

    def log(self, *args):
        print(*args, flush=True)

    def param(self, key, guild_id=None, guild_name=None):
        return param_for(self.params, key, guild_id, guild_name)


def load_plugins():
    """Import every enabled plugin under plugins/ and collect its hooks."""
    global GEN_PLUGIN_ENABLED
    plugins_cfg = CFG.get("plugins") or {}
    if not os.path.isdir(PLUGINS_DIR):
        return
    for name in sorted(os.listdir(PLUGINS_DIR)):
        pfile = os.path.join(PLUGINS_DIR, name, "plugin.py")
        if not os.path.isfile(pfile):
            continue
        pcfg = plugins_cfg.get(name) or {}
        if not pcfg.get("enabled"):
            continue
        try:
            spec = importlib.util.spec_from_file_location(
                f"muse_discord_plugin_{name}", pfile)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            hooks = mod.register(PluginAPI(name, pcfg)) or {}
        except Exception as e:
            print(f"Plugin {name} failed to load: {e}", flush=True)
            continue
        for hook_name, fn in hooks.items():
            if callable(fn):
                PLUGIN_HOOKS.setdefault(hook_name, []).append(fn)
        if name == "media_generation":
            GEN_PLUGIN_ENABLED = True
        print(f"Plugin loaded: {name} "
              f"(hooks: {', '.join(sorted(hooks))})", flush=True)


def call_hook(name, ctx, *args):
    """Run every plugin's callable for a hook; returns their results.
    A failing plugin never breaks the pipeline."""
    out = []
    for fn in PLUGIN_HOOKS.get(name, []):
        try:
            out.append(fn(ctx, *args))
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
                  author=(msg.get("author", {}) or {}).get("username"))



def main():
    global BOT_USER_ID, BOT_ID, BOT_NAME, CHANNELS, GUILDS, _FORCE_DISCOVERY
    _acquire_singleton()
    # Identity FIRST, discovery second: the watchdog's health check waits
    # for the "Bot user id" line, and full discovery (a probe per channel
    # and thread) took ~37s by 2026-10-13, past the watchdog's old 30s
    # window, so it kept killing healthy monitors mid-startup.
    try:
        _me = api_get("/users/@me")
        BOT_USER_ID = _me.get("id")
        BOT_ID = BOT_USER_ID
        BOT_NAME = _me.get("username")
        print(f"Bot user id: {BOT_USER_ID}", flush=True)
    except Exception as e:
        # The watchdog's health check greps for the "Bot user id" marker;
        # during a rate-limit storm the identity fetch 429s and the old
        # wording never matched, so the watchdog killed a healthy,
        # gateway-live monitor (2026-10-15). Print the marker honestly
        # and retry the fetch in-loop once REST recovers.
        print(f"Bot user id: unavailable at boot ({e}); "
              f"retrying in-loop.", flush=True)
    CHANNELS, GUILDS = discover_channels()
    print(f"muse-discord monitor starting. Polling {len(CHANNELS)} "
          f"channels + DMs every {POLL_INTERVAL}s.", flush=True)
    if not CHANNELS:
        print("WARNING: no readable channels found (check the bot's "
              "guild membership and channel permissions); discovery "
              "keeps retrying on the refresh cadence.", flush=True)
    state = load_state()
    load_plugins()
    # Join watermarks for every guild, plus the owner summary for any
    # guild joined while the monitor was down.
    sync_guilds(state, GUILDS, CHANNELS)
    if "name" in RESPOND_TO:
        if CALL_NAMES:
            print(f"Name trigger on for {', '.join(CALL_NAMES)} (active "
                  f"servers only; low priority, worker decides).",
                  flush=True)
        else:
            print("WARNING: respond_to includes \"name\" but "
                  "behavior.call_names is empty; the name trigger is "
                  "inert.", flush=True)

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
    _gateway_status = {"wake": threading.Event()}
    gw_thread = threading.Thread(
        target=_gateway_thread_main,
        args=(token, _gateway_inbox, _gateway_status),
        daemon=True, name="discord-gateway")
    gw_thread.start()
    print("Gateway thread started (realtime ingest).", flush=True)

    # A restart must not drop bursts staged before it: their messages are
    # already marked seen, so flush them immediately instead of waiting out
    # a coalesce window that mostly elapsed while the monitor was down.
    if state.get("pending"):
        queue = []
        if os.path.exists(QUEUE_FILE):
            with open(QUEUE_FILE) as f:
                queue = json.load(f)
        if flush_pending(state, queue, force=True):
            atomic_write(QUEUE_FILE, queue)
    save_state(state)

    cycle = 0
    last_full_poll = 0.0
    last_typing = {}
    was_backing_off = False
    while True:
        try:
            now = time.time()
            backing = rest_backing_off()
            full_poll = (now - last_full_poll >= POLL_INTERVAL) \
                and not backing
            # First poll after a rate-limit backoff: light recovery (no
            # discovery refresh this cycle) so recovery doesn't open with
            # the request burst that helped trip the limit.
            recovery_cycle = was_backing_off and not backing
            was_backing_off = backing
            mentions_found = []
            joins_found = []
            dm_items = []

            if full_poll:
                last_full_poll = now
                cycle += 1

                for cid, info in CHANNELS.items():
                    cname = channel_display(info)
                    gid = info["guild_id"]
                    triggers, joins = check_channel(cid, cname, state, gid)
                    mentions_found.extend(
                        [(m, cname, gid, kind) for m, kind in triggers])
                    joins_found.extend([(m, cname, gid) for m in joins])

                # --- DMs: every message not from the bot is addressed
                # to the bot ---
                for dm_id, dm_name in list_dm_channels():
                    dm_items.extend(
                        (m, "DM with " + dm_name)
                        for m in check_dm_channel(dm_id, dm_name, state))

            # Gateway fast path: messages pushed in realtime land here as
            # the same tuples the poll produces. Anything the poll already
            # processed is skipped via last_seen.
            # Guild join/leave events ride the same inbox.
            gw_mentions, gw_joins, gw_dms = drain_gateway(
                state, _gateway_inbox, CHANNELS)
            if gw_mentions or gw_joins or gw_dms:
                print(f"Gateway delivered {len(gw_mentions)} mention(s), "
                      f"{len(gw_joins)} join(s), {len(gw_dms)} DM(s).",
                      flush=True)
            mentions_found.extend(gw_mentions)
            joins_found.extend(gw_joins)
            dm_items.extend(gw_dms)

            # Stage triggers into the coalescing buffer (bursts from the
            # same author fold into one pending entry); welcomes queue
            # immediately. Matured bursts flush into single queue items.
            # The queue file is re-read every cycle because flushes can
            # happen on quiet cycles and the worker removes items it drains.
            queue = []
            if os.path.exists(QUEUE_FILE):
                with open(QUEUE_FILE) as f:
                    queue = json.load(f)
            dirty = False
            for msg, cname, gid, kind in mentions_found:
                info = CHANNELS.get(msg["channel_id"])
                if info is not None and info.get("can_send") is False:
                    # Talk scope: never queue a reply that cannot be
                    # sent. Logged once per channel; the mention is
                    # ledgered as skipped so the sweep leaves it alone.
                    _note_no_send(msg, cname)
                    continue
                stage_trigger(state, msg, cname, gid, kind)
            for msg, cname, gid in joins_found:
                for item in call_hook(
                        "member_join",
                        {"state": state, "channels": CHANNELS,
                         "guilds": GUILDS},
                        msg, gid):
                    if item:
                        queue.append(item)
                        dirty = True
            for msg, cname in dm_items:
                stage_trigger(state, msg, cname, None, "dm", dm=True)

            dirty |= flush_pending(state, queue)
            if dirty:
                atomic_write(QUEUE_FILE, queue)
            save_state(state)

            # Keep the typing indicator alive while replies are pending.
            # Fire for staged bursts too, so the user sees the typing
            # indicator while the burst is being "read", not after the
            # hold. Throttled per channel: the fast loop ticks every ~2s
            # but one typing ping lasts ~10s.
            typing_targets = set()
            if os.path.exists(QUEUE_FILE):
                with open(QUEUE_FILE) as f:
                    pending = json.load(f)
                for item in pending:
                    if item.get("priority") != "low":
                        typing_targets.add(item["channel_id"])
            for entry in state.get("pending", {}).values():
                if entry.get("kind") in ("mention", "dm"):
                    typing_targets.add(entry["channel_id"])
            for cid in typing_targets:
                if channel_quiet(state, cid):
                    continue
                if now - last_typing.get(cid, 0) >= 8:
                    send_typing(cid)
                    last_typing[cid] = now

            # Send any replies drafted by the worker
            process_outbox(state)
            save_state(state)

            if full_poll:
                _rl_note_clean_cycle()
                if BOT_USER_ID is None:
                    try:
                        _me = api_get("/users/@me")
                        BOT_USER_ID = _me.get("id")
                        BOT_ID = BOT_USER_ID
                        BOT_NAME = _me.get("username")
                        print(f"Bot user id: {BOT_USER_ID} "
                              f"(recovered in-loop)", flush=True)
                    except Exception:
                        pass
                if cycle % SWEEP_EVERY_CYCLES == 0:
                    if sweep_mentions(state, queue, CHANNELS):
                        atomic_write(QUEUE_FILE, queue)
                        save_state(state)

                # Periodically re-discover readable channels so permission
                # changes (grants or revocations) take effect without a
                # restart. Counted in full-poll cycles (POLL_INTERVAL each).
                if (cycle % THREAD_REFRESH_CYCLES == 0
                        and cycle % CHANNEL_REFRESH_CYCLES != 0
                        and not recovery_cycle):
                    # Light thread-only refresh: pick up brand-new posts and
                    # threads minutes after they appear instead of waiting
                    # for the full channel refresh. Only threads not already
                    # monitored cost an API probe.
                    try:
                        for gid, gname in get_guilds():
                            try:
                                chans = api_get(f"/guilds/{gid}/channels")
                            except Exception:
                                continue
                            names_by_id = {c["id"]: c.get("name", "?")
                                           for c in chans}
                            new_threads = discover_threads_for_guild(
                                gid, gname, names_by_id, CHANNELS)
                            for tid, info in new_threads.items():
                                seed_channel_seen(tid, channel_display(info),
                                                  state)
                                CHANNELS[tid] = info
                                print(f"Now monitoring "
                                      f"{channel_display(info)}.",
                                      flush=True)
                            if new_threads:
                                save_state(state)
                    except Exception as e:
                        print(f"Thread refresh error: {e}", flush=True)
                if (cycle % CHANNEL_REFRESH_CYCLES == 0 or _FORCE_DISCOVERY) \
                        and not recovery_cycle:
                    _FORCE_DISCOVERY = False
                    fresh, fresh_guilds = discover_channels()
                    GUILDS = fresh_guilds
                    for cid, info in fresh.items():
                        if cid not in CHANNELS:
                            seed_channel_seen(cid, channel_display(info),
                                              state)
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
                    sync_guilds(state, fresh_guilds, fresh)
                    save_state(state)

                flush_join_notices(state, GUILDS, CHANNELS)
                sweep_breaker(state)
                sweep_media()
                sweep_generated()
                sweep_gen_state(state)

        except Exception as e:
            print(f"Poll error: {e}", flush=True)

        # Event-driven wait: a gateway MESSAGE_CREATE wakes the loop at
        # once; otherwise it idles in ~2s ticks so coalesce flushes, the
        # outbox, and typing stay near-realtime, while the full REST poll
        # still runs every POLL_INTERVAL as gap repair.
        if rest_backing_off():
            wait_for = 2.0  # keep the fast path ticking during REST backoff
        else:
            wait_for = min(2.0, max(
                0.0, POLL_INTERVAL - (time.time() - last_full_poll)))
        _gateway_status["wake"].wait(timeout=wait_for)
        _gateway_status["wake"].clear()


if __name__ == "__main__":
    main()
