"""Media generation plugin for muse-discord.

Supplies the may_generate gate for queue items. The gate is PERMISSION,
never a keyword test and never a decision: when this plugin grants
may_generate on an item, the worker still judges from the full
conversation context whether a visual is actually wanted (explicit asks,
project briefs, greenlights all count; ordinary chat does not), and
generates at most once per item. The monitor records a single-use
approval for granted items so its attachment pipeline can attach the
generated file when the worker's reply is sent.

Allowed users: the plugins.media.who setting where the message is
(owners, everyone, or a list of user ids and roles). The core runs this
plugin only where plugins.media.enabled is on.

The optional per_day cap counts files actually SENT (via the
generation_sent hook), not permissions granted: a grant that produces
no file costs nothing. Like every cap, it counts within the scope where
it is set (everywhere = one total; a server = that server).

Without the core's ctx["settings"] (an old caller), the old params
(allowed_user_ids, daily_cap) are read through api.param.
"""
from datetime import datetime, timezone


def register(api):
    def _plugin_state(state):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        g = state.setdefault("plugins", {}).setdefault(
            "media_generation", {"day": today, "sent_today": 0})
        if g.get("day") != today:
            g["day"] = today
            g["sent_today"] = 0
            g["counts"] = {}
        return g

    def _count(g, counter):
        # "everywhere" is the old sent_today (one total).
        if counter == "everywhere":
            return g.get("sent_today", 0)
        return (g.get("counts") or {}).get(counter, 0)

    def _allowed_ids(ctx, guild_id):
        configured = api.param("allowed_user_ids", guild_id)
        ids = {str(u) for u in (configured or [])}
        if not ids:
            ids = {str(u) for u in (ctx.get("owners") or [])}
        return ids

    def _cap(ctx):
        cap = api.param("daily_cap")
        if cap is None:
            return None
        try:
            return int(cap)
        except (TypeError, ValueError):
            return None

    def gate_message(ctx, msg, guild_id):
        author_id = (msg.get("author", {}) or {}).get("id")
        vals = ctx.get("settings")
        if isinstance(vals, dict) and "author_matches" in ctx:
            if not ctx["author_matches"](vals.get("who") or "owners"):
                return None
            g = _plugin_state(ctx["state"])
            for counter, cap in ctx["cap_layers"]("per_day"):
                if _count(g, counter) >= cap:
                    api.log(f"Media generation plugin: daily cap ({cap}) "
                            f"reached; not granting may_generate")
                    return None
            return {"may_generate": True}
        if author_id not in _allowed_ids(ctx, guild_id):
            return None
        cap = _cap(ctx)
        if cap is not None:
            g = _plugin_state(ctx["state"])
            if g.get("sent_today", 0) >= cap:
                api.log(f"Media generation plugin: daily cap ({cap}) "
                        f"reached; not granting may_generate")
                return None
        return {"may_generate": True}

    def generation_sent(ctx, item):
        g = _plugin_state(ctx["state"])
        g["sent_today"] = g.get("sent_today", 0) + 1
        for counter in ctx.get("counter_ids") or []:
            if counter != "everywhere":
                counts = g.setdefault("counts", {})
                counts[counter] = counts.get(counter, 0) + 1

    return {"gate_message": gate_message,
            "generation_sent": generation_sent}
