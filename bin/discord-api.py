#!/usr/bin/env python3
"""discord-api.py -- allowlisted Discord API tool for muse-discord.

This is the bot's owner-command path: ONLY the reply worker may call it,
and ONLY for a queue item the monitor stamped with owner_command=true
(which the monitor sets exclusively for the owners' messages, from their
verified user IDs). It is how "react to your post in #showcase" actually
does something instead of getting a chatter reply.

That rule is enforced here, not only in the worker's prompt: every call
must say which owner command it serves, as `--for <message_id>` before
the subcommand, and the tool refuses unless the monitor recorded that id
as a live owner command (owner_commands.json, written by the monitor
when it queues an owner_command item, valid for an hour). A worker
talked into using the tool by a stranger's message has no id to cite.
Reads are gated too: the bot can read channels the conversation's
audience cannot, and what the tool prints can end up in a public reply.

Every id argument must be a plain Discord id (ids are interpolated into
API paths, so "1/../../guilds/2/bans/3" must never get through), files
can only be uploaded from the package's generated/ dir, and messages
never ping roles, @everyone or @here.

Subcommands are a deliberate allowlist. Moderation (ban/kick/timeout),
server settings, webhooks, roles, and anything else are NOT implemented:
if the owner asks for something outside this list, the worker says so
honestly instead of improvising.

The bot token is resolved exactly like monitor.py's: the package
config.yaml (../config.yaml relative to this file, or
MUSE_DISCORD_CONFIG) bot.token_env env var first, then bot.token_file
(mode 600). The token is never printed.
"""
import json
import mimetypes
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
import uuid

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GENERATED_DIR = os.path.join(PACKAGE_DIR, "generated")
OWNER_COMMANDS_FILE = os.path.join(PACKAGE_DIR, "owner_commands.json")
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


def owner_command_error(message_id):
    """None when message_id is a live owner command the monitor recorded,
    else the reason to refuse."""
    if not re.fullmatch(r"[0-9]{17,20}", message_id or ""):
        return f"--for needs a Discord message id, not {message_id!r}."
    try:
        with open(OWNER_COMMANDS_FILE) as f:
            live = json.load(f)
        expires = live.get(message_id) if isinstance(live, dict) else None
    except (OSError, ValueError):
        expires = None
    if not isinstance(expires, (int, float)) or expires <= time.time():
        return (f"Refusing: {message_id} is not a live owner command. Use "
                f"the message_id of a queue item stamped "
                f"owner_command: true, within an hour of it.")
    return None


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


def _load_token():
    bot = _bot_cfg()
    env_name = bot.get("token_env")
    if env_name and os.environ.get(env_name):
        return os.environ[env_name].strip()
    token_file = os.path.expanduser(
        bot.get("token_file") or "~/.config/muse-discord/bot_token")
    with open(token_file) as f:
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
    api("PUT", f"/channels/{cid}/messages/{mid}/reactions/{enc}/@me")
    print(f"reacted {emoji} on {mid}")


def cmd_pin(args):
    """pin <channel_id> <message_id>"""
    cid, mid = snowflake(args[0]), snowflake(args[1])
    api("PUT", f"/channels/{cid}/messages/pins/{mid}")
    print(f"pinned {mid}")


def cmd_unpin(args):
    """unpin <channel_id> <message_id>"""
    cid, mid = snowflake(args[0]), snowflake(args[1])
    api("DELETE", f"/channels/{cid}/messages/pins/{mid}")
    print(f"unpinned {mid}")


def cmd_edit(args):
    """edit <channel_id> <message_id> <text> -- edit ONLY the bot's message."""
    cid, mid, text = snowflake(args[0]), snowflake(args[1]), args[2]
    m = api("GET", f"/channels/{cid}/messages/{mid}")
    if (m.get("author") or {}).get("id") != bot_id():
        raise SystemExit("Refusing: that message was not sent by the bot.")
    api("PATCH", f"/channels/{cid}/messages/{mid}",
        {"content": text, "allowed_mentions": ALLOWED_MENTIONS})
    print(f"edited {mid}")


def cmd_delete_own(args):
    """delete-own <channel_id> <message_id> -- delete ONLY the bot's message."""
    cid, mid = snowflake(args[0]), snowflake(args[1])
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
    # --for <message_id> must come first, so it can never be mistaken
    # for (or eat) a text argument.
    for_id = None
    args = argv[1:]
    if len(args) >= 2 and args[0] == "--for":
        for_id, args = args[1], args[2:]
    if not args or args[0] not in COMMANDS:
        print("usage: discord-api.py --for <owner command message_id> <"
              + "|".join(sorted(COMMANDS)) + "> ...", file=sys.stderr)
        return 2
    fn, min_args = COMMANDS[args[0]]
    if len(args) - 1 < min_args:
        print(fn.__doc__.strip(), file=sys.stderr)
        return 2
    if for_id is None:
        print("Refusing: every call needs --for <message_id> (the owner "
              "command it serves) before the subcommand.", file=sys.stderr)
        return 2
    err = owner_command_error(for_id)
    if err:
        print(err, file=sys.stderr)
        return 3
    fn(args[1:])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
