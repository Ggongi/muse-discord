"""Welcome plugin for muse-discord.

Queues a welcome item when a member joins a server. The core detects
joins (Discord system messages, type 7) and calls the member_join hook;
this plugin resolves WHERE the welcome goes (per_guild channel param,
then the default; "system" = the server's system channel, a name is
resolved inside that server, an ID is used as-is, "off" disables
welcomes for that server) and renders the prompt param with {user} and
{server} substituted. The worker drafts the actual greeting from the
rendered prompt; see worker-prompt.template.md.

State kept in the monitor's state.json under plugins.welcome: the
per-guild daily counts and the recently welcomed message ids (so a
restart never double-welcomes).
"""
from datetime import datetime, timezone

DEFAULT_PROMPT = (
    "Welcome {user} to {server}! Write a short, warm welcome. Point "
    "them to this server's rules and getting-started channels if it "
    "has them. Do not invite them to talk to the bot.")


def register(api):
    def _plugin_state(state):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        w = state.setdefault("plugins", {}).setdefault(
            "welcome", {"day": today, "counts": {}, "welcomed": []})
        if w.get("day") != today:
            w["day"] = today
            w["counts"] = {}
        return w

    def _resolve_channel(target, guild_id, guild_name, ctx):
        """Resolve the channel param to a channel id, or None to skip."""
        if target is None:
            target = "system"
        target = str(target).strip()
        if target.lower() == "off":
            return None
        if target.lower() == "system":
            gid_info = (ctx.get("guilds") or {}).get(guild_id) or {}
            return gid_info.get("system_channel_id")
        if target.isdigit():
            return target
        # A channel name, resolved inside THIS server only.
        name = target.lstrip("#").lower()
        for cid, info in (ctx.get("channels") or {}).items():
            if info.get("guild_id") == guild_id \
                    and (info.get("name") or "").lower() == name:
                return cid
        return None

    def member_join(ctx, msg, guild_id):
        state = ctx["state"]
        channels = ctx.get("channels") or {}
        guilds = ctx.get("guilds") or {}
        guild_name = (guilds.get(guild_id) or {}).get("name") or guild_id

        # Freshness: skip ancient joins so a long outage does not
        # welcome someone who joined days ago.
        max_age = api.param("max_age_hours", guild_id, guild_name)
        try:
            max_age_secs = float(max_age if max_age is not None else 24) \
                * 3600
        except (TypeError, ValueError):
            max_age_secs = 24 * 3600
        try:
            ts = datetime.fromisoformat(
                (msg.get("timestamp") or "").replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - ts).total_seconds()
        except Exception:
            age = 0
        if age > max_age_secs:
            api.log(f"Welcome plugin: skipping stale join {msg['id']} "
                    f"(age {age:.0f}s)")
            return None

        w = _plugin_state(state)
        if msg["id"] in w.get("welcomed", []):
            return None
        cap = api.param("daily_cap", guild_id, guild_name)
        if cap is not None:
            try:
                cap = int(cap)
            except (TypeError, ValueError):
                cap = None
            if cap is not None and w["counts"].get(guild_id, 0) >= cap:
                api.log(f"Welcome plugin: daily cap reached for "
                        f"{guild_name}, skipping join {msg['id']}")
                return None

        target = api.param("channel", guild_id, guild_name)
        channel_id = _resolve_channel(target, guild_id, guild_name, ctx)
        if not channel_id:
            api.log(f"Welcome plugin: no welcome channel resolved for "
                    f"{guild_name} (param: {target!r}); skipping")
            return None
        info = channels.get(channel_id)
        if info is not None and info.get("can_send") is False:
            api.log(f"Welcome plugin: bot cannot send in channel "
                    f"{channel_id} ({guild_name}); skipping welcome")
            return None

        prompt = api.param("prompt", guild_id, guild_name) \
            or DEFAULT_PROMPT
        author = msg.get("author", {}) or {}
        rendered = str(prompt).replace("{user}", f"<@{author.get('id')}>") \
                              .replace("{server}", str(guild_name))

        w["counts"][guild_id] = w["counts"].get(guild_id, 0) + 1
        w["welcomed"] = (w.get("welcomed", []) + [msg["id"]])[-200:]

        if info is not None:
            cname = f"{info.get('guild_name', guild_name)} " \
                    f"#{info.get('name', '?')}"
        else:
            cname = f"{guild_name} #welcome"
        api.log(f"Welcome plugin: queued welcome for "
                f"{author.get('username')} in {cname}")
        return {
            "message_id": msg["id"],
            "channel_id": channel_id,
            "channel_name": cname,
            "guild_id": guild_id,
            "kind": "welcome",
            "welcome": True,
            "author": author.get("username"),
            "author_id": author.get("id"),
            "content": "",
            "timestamp": msg.get("timestamp"),
            "media": [],
            "documents": [],
            "unviewable": [],
            "history": [],
            "may_generate": False,
            "owner_command": False,
            "burst": False,
            "burst_fragments": [],
            "reply_to": None,
            "welcome_prompt": rendered,
        }

    return {"member_join": member_join}
