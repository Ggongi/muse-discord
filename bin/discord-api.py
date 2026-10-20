#!/usr/bin/env python3
"""discord-api.py -- allowlisted Discord API tool for muse-discord.

This is the bot's owner-command path: ONLY the reply worker may call it,
and ONLY for a queue item the monitor stamped with owner_command=true
(which the monitor sets exclusively for the owners' messages, from their
verified user IDs). It is how "react to your post in #showcase" actually
does something instead of getting a chatter reply.

Every id argument must be a plain Discord id (ids are interpolated into
API paths, so "1/../../guilds/2/bans/3" must never get through), files
can only be uploaded from the package's generated/ dir, and messages
never ping roles, @everyone or @here.

An owner's DM channel is off limits unless the run holds that DM: any
subcommand on a channel the API reports as a DM with an owner (or any
DM, when the owners cannot be read from the config) needs a leading
`--for <the DM's message_id> --nonce <its owner_dm_nonce>`, checked in
constant time against owner_dm_nonces.json (the monitor's private file,
read here and never printed) and against the API (that message must be
in that channel). The wake hook hands a nonce only to a wake that holds
nothing but owner DMs, so a worker woken by someone else's message (and
maybe their prompt injection) cannot post, edit, delete, react, pin or
read in an owner's DM through this tool. Server channels need no
--for, which is accepted and ignored there.

Subcommands are a deliberate allowlist. Moderation (ban/kick/timeout),
server settings, webhooks, roles, and anything else are NOT implemented:
if the owner asks for something outside this list, the worker says so
honestly instead of improvising.

The bot token is resolved exactly like monitor.py's: token_from in the
package config.yaml (../config.yaml relative to this file, or
MUSE_DISCORD_CONFIG), read through muse_settings.py so both config
layouts work (old layout: the bot.token_env env var first, then
bot.token_file, mode 600). The token is never printed.
"""
import hmac
import json
import mimetypes
import os
import re
import sys
import urllib.parse
import urllib.request
import urllib.error
import uuid

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GENERATED_DIR = os.path.join(PACKAGE_DIR, "generated")
OWNER_DM_NONCES_FILE = os.path.join(PACKAGE_DIR, "owner_dm_nonces.json")
DM_TYPES = (1, 3)   # DM, group DM
# --for / --nonce from the command line (main), for guard_channel.
BINDING = {"for": None, "nonce": None}
API = "https://discord.com/api/v10"
TIMEOUT = 30
# Messages may ping users (<@id>), never roles or @everyone/@here.
ALLOWED_MENTIONS = {"parse": ["users"]}


def snowflake(value):
    """Return value if it is a plain Discord id, else refuse."""
    if not re.fullmatch(r"[0-9]{17,20}", value or ""):
        raise SystemExit(f"Refusing: {value!r} is not a Discord id.")
    return value


def generated_file(path):
    """Resolve an upload path, refusing anything that is not a regular
    file inside the package's generated/ dir after resolving symlinks
    (the same rule as monitor.py's _validate_gen_paths). Keeps the tool
    from ever posting the token file, SSH keys, or anything else."""
    root = os.path.realpath(GENERATED_DIR)
    rp = os.path.realpath(os.path.expanduser(path))
    if not rp.startswith(root + os.sep) or not os.path.isfile(rp):
        raise SystemExit(f"Refusing to upload {path!r}: only files inside "
                         f"{GENERATED_DIR}/ can be attached.")
    return rp


def _bot_cfg():
    try:
        import yaml
        path = os.environ.get("MUSE_DISCORD_CONFIG") or os.path.join(
            PACKAGE_DIR, "config.yaml")
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("bot") or {}
    except Exception:
        return {}


def _owners():
    """The owners' user ids from the config, or None when it cannot be
    read (then every DM counts as an owner's)."""
    path = os.environ.get("MUSE_DISCORD_CONFIG") or os.path.join(
        PACKAGE_DIR, "config.yaml")
    try:
        sys.path.insert(0, PACKAGE_DIR)
        import muse_settings
        owners = muse_settings.load(
            path, os.path.join(PACKAGE_DIR, "plugins")).owners
        return {str(o) for o in owners}
    except Exception:
        return None


def _live_nonce(message_id):
    try:
        with open(OWNER_DM_NONCES_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    want = data.get(message_id) if isinstance(data, dict) else None
    return want if isinstance(want, str) and want else None


def guard_channel(cid):
    """Refuse to act in an owner's DM channel unless this run holds that
    DM (see the module docstring). Server channels pass untouched."""
    ch = api("GET", f"/channels/{cid}")
    if not isinstance(ch, dict) or ch.get("type") not in DM_TYPES:
        return
    owners = _owners()
    recips = {str((r or {}).get("id")) for r in ch.get("recipients") or []}
    if ch.get("type") == 1 and owners is not None \
            and not recips & owners:
        return   # a DM with someone else: not an owner's
    mid, got = BINDING["for"], BINDING["nonce"]
    if not mid or not got:
        raise SystemExit(
            f"Refusing: {cid} is an owner's DM. Only a run handed that DM "
            f"may act there: put --for <its message_id> --nonce <its "
            f"owner_dm_nonce> before the subcommand.")
    if not re.fullmatch(r"[0-9]{17,20}", mid):
        raise SystemExit(f"Refusing: --for {mid!r} is not a Discord id.")
    want = _live_nonce(mid)
    if not want or not hmac.compare_digest(
            want.encode(), got.encode("utf-8", "replace")):
        raise SystemExit(f"Refusing: --nonce is not the live owner_dm_nonce "
                         f"of {mid}.")
    try:
        api("GET", f"/channels/{cid}/messages/{mid}")
    except SystemExit:
        raise SystemExit(f"Refusing: {mid} is not a message in {cid}.")


def _token_sources():
    """token_from as [("env", NAME) | ("file", path)], in order."""
    path = os.environ.get("MUSE_DISCORD_CONFIG") or os.path.join(
        PACKAGE_DIR, "config.yaml")
    try:
        sys.path.insert(0, PACKAGE_DIR)
        import muse_settings
        return muse_settings.load(
            path, os.path.join(PACKAGE_DIR, "plugins")).token_sources()
    except Exception:
        bot = _bot_cfg()  # the old layout, read directly
        out = [("env", bot["token_env"])] if bot.get("token_env") else []
        return out + [("file", bot.get("token_file")
                       or "~/.config/muse-discord/bot_token")]


def _load_token():
    files = []
    for kind, where in _token_sources():
        if kind == "env":
            if where and os.environ.get(where):
                return os.environ[where].strip()
            continue
        files.append(where)
        try:
            with open(os.path.expanduser(where)) as f:
                return f.read().strip()
        except OSError:
            continue
    # Nothing readable: fail the way a missing token file always did.
    with open(os.path.expanduser(
            files[-1] if files else "~/.config/muse-discord/bot_token")) as f:
        return f.read().strip()


TOKEN = _load_token()
BOT_ID = None


def bot_id():
    global BOT_ID
    if BOT_ID is None:
        BOT_ID = api("GET", "/users/@me")["id"]
    return BOT_ID


def api(method, path, data=None):
    body = json.dumps(data).encode() if data is not None else b""
    req = urllib.request.Request(API + path, data=body, method=method)
    req.add_header("Authorization", f"Bot {TOKEN}")
    # Cloudflare's bad-browser check (error 1010) rejects bare urllib
    # requests: a User-Agent and Content-Type are required, even for
    # bodyless PUTs like reactions.
    req.add_header("User-Agent", "DiscordBot (muse-discord-cli, 1.0)")
    req.add_header("Content-Type", "application/json")
    req.add_header("Content-Length", str(len(body)))
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            return json.loads(raw) if raw else {"ok": True, "status": r.status}
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        raise SystemExit(f"Discord API {e.code} on {method} {path}: {detail}")


def api_multipart(path, payload, file_path):
    """POST with a single file attachment (Discord multipart upload).

    The file is referenced as attachments[0] inside payload["message"];
    used by forum-thread creation so the starter post carries the image
    itself instead of an external link.
    """
    boundary = "----musediscord" + uuid.uuid4().hex
    filename = os.path.basename(file_path)
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    with open(file_path, "rb") as f:
        file_bytes = f.read()
    payload = dict(payload)
    payload["message"] = dict(payload["message"])
    payload["message"]["attachments"] = [{"id": 0, "filename": filename}]
    chunks = [
        (f"--{boundary}\r\n"
         "Content-Disposition: form-data; name=\"payload_json\"\r\n"
         "Content-Type: application/json\r\n\r\n").encode()
        + json.dumps(payload).encode() + b"\r\n",
        (f"--{boundary}\r\n"
         f"Content-Disposition: form-data; name=\"files[0]\"; "
         f"filename=\"{filename}\"\r\n"
         f"Content-Type: {ctype}\r\n\r\n").encode()
        + file_bytes + b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    body = b"".join(chunks)
    req = urllib.request.Request(API + path, data=body, method="POST")
    req.add_header("Authorization", f"Bot {TOKEN}")
    req.add_header("User-Agent", "DiscordBot (muse-discord-cli, 1.0)")
    req.add_header("Content-Type",
                   f"multipart/form-data; boundary={boundary}")
    req.add_header("Content-Length", str(len(body)))
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            return json.loads(raw) if raw else {"ok": True, "status": r.status}
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        raise SystemExit(f"Discord API {e.code} on POST {path}: {detail}")


def cmd_resolve_channel(args):
    """resolve-channel <guild_id> <name> -- find a guild channel by name ("#" optional)."""
    guild_id, name = snowflake(args[0]), args[1].lstrip("#").lower()
    channels = api("GET", f"/guilds/{guild_id}/channels")
    hits = [c for c in channels if c.get("name", "").lower() == name]
    if not hits:
        names = sorted(c.get("name", "?") for c in channels)
        raise SystemExit(f"No channel named '{args[1]}'. Guild channels: "
                         + ", ".join("#" + n for n in names))
    for c in hits:
        print(f"{c['id']}  #{c['name']}  type={c['type']}")


def cmd_read(args):
    """read <channel_id> [n] -- recent messages, newest last."""
    cid = snowflake(args[0])
    n = int(args[1]) if len(args) > 1 else 10
    guard_channel(cid)
    msgs = api("GET", f"/channels/{cid}/messages?limit={min(n, 50)}")
    for m in reversed(msgs):
        author = (m.get("author") or {}).get("username", "?")
        text = (m.get("content") or "")[:280].replace("\n", " ")
        print(f"{m['id']}  {author}: {text}")


def cmd_list_threads(args):
    """list-threads <guild_id> <forum_channel_id> -- active threads, with owner ids."""
    guild_id, forum_id = snowflake(args[0]), snowflake(args[1])
    data = api("GET", f"/guilds/{guild_id}/threads/active")
    mine = [t for t in data.get("threads", [])
            if t.get("parent_id") == forum_id]
    if not mine:
        print("(no active threads)")
        return
    for t in mine:
        print(f"{t['id']}  owner={t.get('owner_id')}  "
              f"msgs={t.get('message_count', '?')}  {t.get('name')}")


def cmd_post(args):
    """post <channel_id> <text> -- send a message as the bot."""
    cid, text = snowflake(args[0]), args[1]
    guard_channel(cid)
    m = api("POST", f"/channels/{cid}/messages",
            {"content": text, "allowed_mentions": ALLOWED_MENTIONS})
    print(f"posted message {m['id']}")


def cmd_thread(args):
    """thread <forum_channel_id> <title> <text> [file] -- create a forum post.

    With the optional file argument, the image/file is attached to the
    starter post itself (multipart upload, no external link). Only files
    inside the package's generated/ dir can be attached.
    """
    forum_id, title, text = snowflake(args[0]), args[1], args[2]
    upload = generated_file(args[3]) if len(args) > 3 else None
    payload = {"name": title,
               "message": {"content": text,
                           "allowed_mentions": ALLOWED_MENTIONS},
               "auto_archive_duration": 1440}
    guard_channel(forum_id)
    if upload:
        t = api_multipart(f"/channels/{forum_id}/threads", payload, upload)
    else:
        t = api("POST", f"/channels/{forum_id}/threads", payload)
    print(f"created thread {t['id']} '{t.get('name')}'")
    if len(args) > 3:
        # A forum thread's starter message shares the thread's id; fetch
        # it back so the printed line proves the attachment really landed.
        m = api("GET", f"/channels/{t['id']}/messages/{t['id']}")
        names = [a.get("filename") for a in m.get("attachments", [])]
        print(f"starter message attachments: {names}")


def cmd_react(args):
    """react <channel_id> <message_id> <emoji> -- add a reaction as the bot."""
    cid, mid, emoji = snowflake(args[0]), snowflake(args[1]), args[2]
    enc = urllib.parse.quote(emoji, safe="")  # "/" stays in the segment
    guard_channel(cid)
    api("PUT", f"/channels/{cid}/messages/{mid}/reactions/{enc}/@me")
    print(f"reacted {emoji} on {mid}")


def cmd_pin(args):
    """pin <channel_id> <message_id>"""
    cid, mid = snowflake(args[0]), snowflake(args[1])
    guard_channel(cid)
    api("PUT", f"/channels/{cid}/messages/pins/{mid}")
    print(f"pinned {mid}")


def cmd_unpin(args):
    """unpin <channel_id> <message_id>"""
    cid, mid = snowflake(args[0]), snowflake(args[1])
    guard_channel(cid)
    api("DELETE", f"/channels/{cid}/messages/pins/{mid}")
    print(f"unpinned {mid}")


def cmd_edit(args):
    """edit <channel_id> <message_id> <text> -- edit ONLY the bot's message."""
    cid, mid, text = snowflake(args[0]), snowflake(args[1]), args[2]
    guard_channel(cid)
    m = api("GET", f"/channels/{cid}/messages/{mid}")
    if (m.get("author") or {}).get("id") != bot_id():
        raise SystemExit("Refusing: that message was not sent by the bot.")
    api("PATCH", f"/channels/{cid}/messages/{mid}",
        {"content": text, "allowed_mentions": ALLOWED_MENTIONS})
    print(f"edited {mid}")


def cmd_delete_own(args):
    """delete-own <channel_id> <message_id> -- delete ONLY the bot's message."""
    cid, mid = snowflake(args[0]), snowflake(args[1])
    guard_channel(cid)
    m = api("GET", f"/channels/{cid}/messages/{mid}")
    if (m.get("author") or {}).get("id") != bot_id():
        raise SystemExit("Refusing: that message was not sent by the bot.")
    api("DELETE", f"/channels/{cid}/messages/{mid}")
    print(f"deleted {mid}")


COMMANDS = {
    "resolve-channel": (cmd_resolve_channel, 2),
    "read": (cmd_read, 1),
    "list-threads": (cmd_list_threads, 2),
    "post": (cmd_post, 2),
    "edit": (cmd_edit, 3),
    "thread": (cmd_thread, 3),
    "react": (cmd_react, 3),
    "pin": (cmd_pin, 2),
    "unpin": (cmd_unpin, 2),
    "delete-own": (cmd_delete_own, 2),
}


def main(argv):
    args = argv[1:]
    # Leading --for <message_id> and --nonce <owner_dm_nonce>: needed
    # only in an owner's DM (guard_channel); elsewhere --for is accepted
    # and ignored, for worker prompts written when the tool required it.
    BINDING.update({"for": None, "nonce": None})
    while len(args) >= 2 and args[0] in ("--for", "--nonce"):
        BINDING[args[0][2:]] = args[1]
        args = args[2:]
    if not args or args[0] not in COMMANDS:
        print("usage: discord-api.py [--for <message_id> --nonce "
              "<owner_dm_nonce>] <" + "|".join(sorted(COMMANDS)) + "> ...",
              file=sys.stderr)
        return 2
    fn, min_args = COMMANDS[args[0]]
    if len(args) - 1 < min_args:
        print(fn.__doc__.strip(), file=sys.stderr)
        return 2
    fn(args[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
