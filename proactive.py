#!/usr/bin/env python3
"""muse-discord proactive (2026-10-21): the bot starts a conversation on
its own now and then, like a person would, instead of only replying.

Off by default (proactive.enabled; the Friends preset turns it on for
its server). monitor.py loads this module (like telemetry.py) and calls
in at a few points; everything else is here:

- tick(state): the main loop. A heartbeat at jittered times
  (proactive.frequency: rarely about 1-2 a day, sometimes 4-6, often
  about hourly; moved out of quiet hours). Each heartbeat works out who
  and where the bot may reach out to right now (eligible(): owners by
  DM, people who DMed the bot first, quiet server channels where it is
  on) and, when anyone is eligible and the day's total allows, queues
  ONE item of kind "proactive" (a slot) for the worker: one wake, which
  usually ends in silence. The slot carries a nonce and the list of
  targets; it lives SLOT_SECS.
- outbox_item(state, item): an outbox item naming a slot. It may post
  only to one of that slot's targets, only with the slot's nonce, only
  while the slot is fresh and unspent, and only within the caps, in
  quiet-hours-free time, with the target still eligible. It never
  falls through to the ordinary send path.
- opt_action(state, item): "stop messaging me" (and "you can message me
  again"): an outbox item answering a person's own message carries
  "proactive": "opt-out" | "opt-in"; the person is that message's
  author from the monitor's own record, never a field the worker
  writes.
- note_item(mid, meta): every queued message (who wrote, where, when):
  whom the bot may DM first (only people who DMed it), and whether
  someone answered the last unprompted message.
- hook_nonces(): the live slot's nonce, for owner_dm_nonces.json: the
  wake hook hands a proactive item out alone (never with anyone's
  message) and only that wake gets the nonce, as for an owner's DM.

The binding, in one shape that reminders and watches share in spirit (a
monitor-initiated queue item of its own kind, plus the places its reply
may go): slot = {"id", "kind": "proactive", "nonce", "at", "expires",
"targets": {channel_id: {"type", "user_id"}}, "max_posts", "posts",
"spent"}. The worker cannot add a target, so nothing it read (other
people's messages, its memory) can steer an unprompted message to
anyone else, and the caps bound the rest.

proactive.json (mode 600, monitor-internal): the next heartbeat, the
live slot, spent slot ids, the unprompted messages sent (for the caps),
who has messaged the bot, and opt-outs. Standard library only.
"""
import hmac
import json
import random
import secrets
import time
from datetime import datetime, timedelta, timezone

VERSION = 1
KIND = "proactive"
FIELD = "proactive"                 # the slot's details on a queue item;
                                    # an outbox item's opt-out / opt-in
NONCE_FIELD = "owner_dm_nonce"      # the wake hook's one capability field
OPT_VALUES = ("opt-out", "opt-in")
SLOT_SECS = 15 * 60                 # a slot is spent or gone after this
MAX_PER_SLOT = 1                    # unprompted messages per heartbeat
ACTIVE_SECS = 30 * 60               # never into a conversation this fresh
TICK_SECS = 30
# Heartbeat gaps (seconds) by proactive.frequency, drawn uniformly.
INTERVALS = {"rarely": (12 * 3600, 24 * 3600),
             "sometimes": (4 * 3600, 6 * 3600),
             "often": (45 * 60, 75 * 60)}
PER_DAY = {"rarely": 2, "sometimes": 5, "often": 10}   # in all, a day
QUIET_SPREAD_SECS = 2 * 3600        # after quiet hours, a heartbeat lands
                                    # within this of their end
TARGETS_MAX = 25
DAY, WEEK = 86400, 7 * 86400
SENT_KEEP_SECS = 8 * DAY
SPENT_KEEP = 500
PEOPLE_MAX = 3000
FLUSH_SECS = 60
SEND_RETRY_SECS = 90                # as the monitor's sends (nonce window)
TEXT_KINDS = ("mention", "dm")      # queued messages that count as talking
FORBIDDEN = ("reply_to", "reactions", "attachments", "settings",
             "schedule", FIELD)     # never on a slot's outbox item


def iso(ts):
    if not ts:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def snowflake_ts(mid):
    try:
        return ((int(mid) >> 22) + 1420070400000) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def new_slot_id(now):
    """A queue-item id for a slot: a snowflake for now (it sorts and reads
    like a message id and passes the outbox's id checks) whose low bits
    are random, so it never names a real message."""
    ms = int(now * 1000) - 1420070400000
    return str((ms << 22) | secrets.randbits(22) | 1)


# -- time ---------------------------------------------------------------------
def zone(name):
    if not name or str(name).upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(str(name))
    except Exception:
        return timezone.utc


def parse_hours(text):
    """((start h, m), (end h, m)) of quiet hours, or None when off."""
    t = str(text or "").strip().lower()
    if t in ("", "off", "none"):
        return None
    try:
        a, b = t.split("-")
        sh, sm = (int(x) for x in a.strip().split(":"))
        eh, em = (int(x) for x in b.strip().split(":"))
    except ValueError:
        return None
    if (sh, sm) == (eh, em):
        return None
    return (sh, sm), (eh, em)


def in_quiet(ts, hours, tz):
    """True when ts falls in quiet hours (local time in tz)."""
    if hours is None:
        return False
    local = datetime.fromtimestamp(ts, tz)
    m = local.hour * 60 + local.minute
    s = hours[0][0] * 60 + hours[0][1]
    e = hours[1][0] * 60 + hours[1][1]
    return s <= m < e if s < e else (m >= s or m < e)


def quiet_end(ts, hours, tz):
    """When the quiet hours ts is in end (epoch seconds)."""
    local = datetime.fromtimestamp(ts, tz)
    end = local.replace(hour=hours[1][0], minute=hours[1][1], second=0,
                        microsecond=0)
    if end <= local:
        end += timedelta(days=1)
    return end.timestamp()


class Verdict:
    """outbox_item's answer: reject (why it is set aside, or None),
    retry (keep it in the outbox for a later pass), spent (the slot id
    whose queue item can go: the slot is used up)."""

    def __init__(self, reject=None, retry=False, spent=None):
        self.reject, self.retry, self.spent = reject, retry, spent


class _G:
    """The monitor's globals, read live (tests stub them on the module)."""

    def __init__(self, g):
        self.__dict__["_g"] = g

    def __getattr__(self, name):
        return self._g[name]


class Proactive:
    def __init__(self, monitor_globals, clock=time.time, rng=None):
        self.m = _G(monitor_globals)
        self.clock = clock
        self.rng = rng or random.Random()
        self._data = None
        self._path = None
        self._dirty = False
        self._next_tick = 0.0
        self._flushed = 0.0

    # -- the store ------------------------------------------------------------
    def path(self):
        return self.m.PROACTIVE_FILE

    def data(self):
        """proactive.json, read once (re-read when the path changes)."""
        p = self.path()
        if self._data is None or self._path != p:
            self._path = p
            d = None
            try:
                with open(p) as f:
                    d = json.load(f)
            except FileNotFoundError:
                pass
            except (OSError, ValueError) as e:
                print(f"proactive.json unreadable ({e}); starting it "
                      f"again (opt-outs in it are lost).", flush=True)
            d = d if isinstance(d, dict) else {}
            for k, empty in (("spent", {}), ("sent", []), ("people", {}),
                             ("opted_out", {})):
                if not isinstance(d.get(k), type(empty)):
                    d[k] = empty
            if not isinstance(d.get("slot"), dict):
                d["slot"] = None
            d["v"] = VERSION
            self._data = d
        return self._data

    def save(self):
        try:
            self.m._write_private(self.path(), self.data())
            self._dirty = False
            self._flushed = self.clock()
        except OSError as e:
            print(f"Could not write proactive.json: {e}", flush=True)

    # -- settings ---------------------------------------------------------------
    def st(self):
        return self.m.settings()

    def every(self, key):
        return self.st().get(key, self.m.MS.Scope.everywhere())

    def quiet_now(self, now):
        return in_quiet(now, parse_hours(self.every("proactive.quiet_hours")),
                        zone(self.every("proactive.timezone")))

    def enabled_anywhere(self):
        st, MS = self.st(), self.m.MS
        if st.get("proactive.enabled", MS.Scope.dm()) \
                or st.get("proactive.enabled", MS.Scope("all_servers")):
            return True
        for cid, info in self.m.CHANNELS.items():
            if st.get("proactive.enabled",
                      self.m.scope_for(info.get("guild_id"), cid)):
                return True
        return False

    def schedule(self, now, freq=None):
        """The next heartbeat: a uniform gap for the frequency, moved out
        of quiet hours (to within QUIET_SPREAD_SECS after they end)."""
        freq = freq or self.every("proactive.frequency")
        lo, hi = INTERVALS.get(freq, INTERVALS["rarely"])
        t = now + self.rng.uniform(lo, hi)
        hours = parse_hours(self.every("proactive.quiet_hours"))
        tz = zone(self.every("proactive.timezone"))
        if in_quiet(t, hours, tz):
            t = quiet_end(t, hours, tz) + self.rng.uniform(
                0, min(lo, QUIET_SPREAD_SECS))
        return t

    # -- what the monitor tells us ---------------------------------------------
    def note_item(self, mid, meta):
        """A queued message (note_item_meta): who talked to the bot,
        where, when; a DM means they started a DM conversation."""
        try:
            if not isinstance(meta, dict) or meta.get("bot") \
                    or meta.get("kind") not in TEXT_KINDS:
                return
            uid = str(meta.get("author_id") or "")
            if not uid:
                return
            people = self.data()["people"]
            p = people.setdefault(uid, {})
            ts = snowflake_ts(mid) or self.clock()
            if ts >= p.get("last", 0):
                p.update(last=ts, last_mid=str(mid),
                         last_cid=str(meta.get("channel_id") or ""))
            if meta.get("dm") and meta.get("channel_id"):
                p["dm"] = str(meta["channel_id"])
            if meta.get("author"):
                p["name"] = str(meta["author"])[:100]
            if len(people) > PEOPLE_MAX:
                for old in sorted(people, key=lambda u: people[u].get(
                        "last", 0))[:len(people) - PEOPLE_MAX]:
                    del people[old]
            self._dirty = True
        except Exception as e:
            print(f"Proactive note error: {e}", flush=True)

    def hook_nonces(self):
        """{slot id: nonce} for owner_dm_nonces.json: the live, unspent
        slot only."""
        try:
            s = self.data().get("slot")
            if s and not s.get("spent") and s.get("expires", 0) > self.clock():
                return {s["id"]: s["nonce"]}
        except Exception:
            pass
        return {}

    # -- eligibility ------------------------------------------------------------
    def sent_to(self, key, value, since):
        return [e for e in self.data()["sent"]
                if e.get(key) == value and e.get("at", 0) >= since]

    def history(self, state, cid):
        return ((state.get("history") or {}).get(cid) or [])

    def is_bot_msg(self, h):
        return str(h.get("author_id")) in (str(self.m.BOT_USER_ID),
                                           str(self.m.BOT_ID))

    def person_problem(self, state, uid, cid, now):
        """Why no unprompted message may go to person uid (in their DM
        channel cid) now, or None."""
        d = self.data()
        if uid in d["opted_out"]:
            return "opted out"
        st = self.st()
        if self.sent_total(now) >= self.total_cap():
            return "the day's total is reached"
        day = st.get("proactive.per_person_per_day",
                     self.m.MS.Scope.everywhere())
        week = st.get("proactive.per_person_per_week",
                      self.m.MS.Scope.everywhere())
        if len(self.sent_to("user_id", uid, now - DAY)) >= day:
            return "per person per day cap"
        if len(self.sent_to("user_id", uid, now - WEEK)) >= week:
            return "per person per week cap"
        mine = self.sent_to("user_id", uid, 0)
        p = d["people"].get(uid) or {}
        if mine:
            last = max(e["at"] for e in mine)
            hist = self.history(state, cid)
            answered = p.get("last", 0) > last or any(
                not self.is_bot_msg(h) and snowflake_ts(h.get("id")) > last
                for h in hist)
            if not answered:
                return "no answer to the last unprompted message yet"
        hist = self.history(state, cid)
        newest = max([snowflake_ts(h.get("id")) for h in hist]
                     + [p.get("last", 0)])
        if now - newest < ACTIVE_SECS:
            return "a conversation is going on"
        return None

    def channel_problem(self, state, cid, now):
        """Why no unprompted message may go to server channel cid now,
        or None (a channel the bot reads and may post in, not a thread,
        proactive on, replies not off or paused there, an owner in the
        server, quiet for ACTIVE_SECS, the bot not the last to speak,
        within the caps)."""
        m = self.m
        info = m.CHANNELS.get(cid)
        if not info or info.get("thread") or info.get("can_send") is not True:
            return "not a channel the bot can post in"
        gid = info.get("guild_id")
        if m.owners_absent(gid) or m.owner_check_pending(state, gid):
            return "none of the owners is in that server"
        here = self.st().resolve(m.scope_for(gid, cid))
        if not here["proactive.enabled"]:
            return "proactive is off there"
        if here["who.replies"] == "off":
            return "replies are off (or paused) there"
        if self.sent_total(now) >= self.total_cap():
            return "the day's total is reached"
        cap = self.every("proactive.per_channel_per_day")
        if len(self.sent_to("channel_id", cid, now - DAY)) >= cap:
            return "per channel per day cap"
        hist = self.history(state, cid)
        if not hist:
            return "nothing known about the channel's activity"
        last = hist[-1]
        if self.is_bot_msg(last):
            return "the bot spoke last there"
        if now - snowflake_ts(last.get("id")) < ACTIVE_SECS:
            return "a conversation is going on"
        mine = self.sent_to("channel_id", cid, 0)
        if mine and not any(not self.is_bot_msg(h)
                            and snowflake_ts(h.get("id"))
                            > max(e["at"] for e in mine) for h in hist):
            return "no answer to the last unprompted message yet"
        return None

    def total_cap(self):
        return PER_DAY.get(self.every("proactive.frequency"), PER_DAY["rarely"])

    def sent_total(self, now):
        return len([e for e in self.data()["sent"]
                    if e.get("at", 0) >= now - DAY])

    def owner_dm_channel(self, uid):
        """An owner's DM channel: known, else opened (the same call
        notify_owners makes); None when Discord cannot say."""
        p = self.data()["people"].get(uid) or {}
        if p.get("dm"):
            return p["dm"]
        for cid, u in self.m.DM_CHANNEL_USER.items():
            if u == uid:
                return str(cid)
        if self.m.rest_backing_off():
            return None
        try:
            c = self.m.api_post("/users/@me/channels", {"recipient_id": uid})
        except Exception as e:
            print(f"Proactive: no DM channel for owner {uid} ({e}).",
                  flush=True)
            return None
        cid = str((c or {}).get("id") or "")
        if not self.m._is_snowflake(cid):
            return None
        self.data()["people"].setdefault(uid, {})["dm"] = cid
        self._dirty = True
        return cid

    def target_problem(self, state, t, now):
        """Why target t (a slot's binding entry) is not eligible now."""
        if self.quiet_now(now):
            return "quiet hours"
        st, MS = self.st(), self.m.MS
        if t["type"] == "channel":
            return self.channel_problem(state, t["channel_id"], now)
        dm_here = st.resolve(MS.Scope.dm())
        if not dm_here["proactive.enabled"]:
            return "proactive is off for DMs"
        if dm_here["who.replies"] == "off":
            return "DMs are off (or paused)"
        uid = t["user_id"]
        owner = uid in self.m.OWNER_USER_IDS
        if t["type"] == "owner" and not owner:
            return "no longer an owner"
        if t["type"] == "dm":
            if owner:
                return "an owner now (owner check-ins instead)"
            p = self.data()["people"].get(uid) or {}
            if p.get("dm") != t["channel_id"]:
                return "never DMed the bot there"
            if self.m.dm_refusal({"id": uid, "username": p.get("name")
                                  or uid}) is not None:
                return "the DM settings no longer answer them"
        return self.person_problem(state, uid, t["channel_id"], now)

    def eligible(self, state, now):
        """[target] the bot may reach out to now (the slot's list)."""
        m, d = self.m, self.data()
        cands = []
        dm_here = self.st().resolve(m.MS.Scope.dm())
        if dm_here["proactive.enabled"] and dm_here["who.replies"] != "off":
            for uid in sorted(m.OWNER_USER_IDS):
                if uid in d["opted_out"]:
                    continue
                cid = self.owner_dm_channel(uid)
                if cid:
                    cands.append({"type": "owner", "user_id": uid,
                                  "channel_id": cid})
            for uid, p in sorted(d["people"].items()):
                if p.get("dm") and uid not in m.OWNER_USER_IDS \
                        and uid not in d["opted_out"]:
                    cands.append({"type": "dm", "user_id": uid,
                                  "channel_id": p["dm"]})
        for cid in sorted(m.CHANNELS):
            cands.append({"type": "channel", "user_id": None,
                          "channel_id": str(cid)})
        out = []
        for t in cands:
            try:
                why = self.target_problem(state, t, now)
            except Exception as e:
                why = f"error ({e})"
            if why is None:
                out.append(t)
            if len(out) >= TARGETS_MAX:
                break
        return out

    # -- the heartbeat ----------------------------------------------------------
    def describe(self, state, t, now):
        """What the worker gets about one target: who or where, when they
        last talked and how that ended, the last unprompted message, and
        the settings card of the place. No message text."""
        m = self.m
        cid, uid = t["channel_id"], t.get("user_id")
        hist = self.history(state, cid)
        out = {"channel_id": cid, "type": t["type"]}
        mine = self.sent_to("channel_id", cid, 0)
        out["last_unprompted"] = iso(max([e["at"] for e in mine] or [0]))
        if t["type"] == "channel":
            info = m.CHANNELS.get(cid) or {}
            gid = info.get("guild_id")
            scope = m.scope_for(gid, cid)
            card = m.settings_card(scope, self.st().resolve(scope), None,
                                   False, cid)
            card.pop("speaker", None)
            humans = [h for h in hist if not self.is_bot_msg(h)]
            out.update(
                where=m.channel_display(info),
                last_message_at=iso(snowflake_ts(hist[-1]["id"]))
                if hist else None,
                recent_messages=len([h for h in humans if now - snowflake_ts(
                    h.get("id")) < DAY]),
                recent_people=len({h.get("author_id") for h in humans
                                   if now - snowflake_ts(h.get("id")) < DAY}))
        else:
            p = self.data()["people"].setdefault(uid, {})
            name = m._USER_NAMES.get(uid) or p.get("name") \
                or m.DM_CHANNELS.get(cid) or uid
            if name != uid and p.get("name") != name:
                p["name"] = name
                self._dirty = True
            scope = m.MS.Scope.dm()
            card = m.settings_card(scope, self.st().resolve(scope), uid,
                                   False, cid)
            card.pop("places", None)
            last_them = max([p.get("last", 0)] + [
                snowflake_ts(h.get("id")) for h in hist
                if str(h.get("author_id")) == uid])
            last_you = max([0] + [snowflake_ts(h.get("id")) for h in hist
                                  if self.is_bot_msg(h)])
            outcome = None
            if p.get("last_mid"):
                e = m._ledger_load()["entries"].get(p["last_mid"]) or {}
                outcome = e.get("code")
            out.update(user_id=uid, name=name, owner=t["type"] == "owner",
                       where=f"DM with {name}",
                       last_from_them=iso(last_them),
                       last_from_you=iso(last_you),
                       ended_with=None if not (last_them or last_you)
                       else ("you" if last_you > last_them else "them"),
                       their_last_message=outcome)
        out["settings"] = card
        return out

    def make_slot(self, state, targets, now):
        d = self.data()
        sid = new_slot_id(now)
        slot = {"id": sid, "kind": KIND, "nonce": secrets.token_hex(16),
                "at": now, "expires": now + SLOT_SECS,
                "max_posts": MAX_PER_SLOT, "spent": False, "posts": {},
                "targets": {t["channel_id"]: {"type": t["type"],
                                              "user_id": t.get("user_id")}
                            for t in targets}}
        tzname = self.every("proactive.timezone")
        local = datetime.fromtimestamp(now, zone(tzname))
        item = {
            "message_id": sid, "kind": KIND, "channel_id": None,
            "channel_name": "(no one asked: your call)", "guild_id": None,
            "author": None, "author_id": None, "content": "",
            "timestamp": iso(now), "queued_at": now,
            "media": [], "documents": [], "unviewable": [], "history": [],
            "may_generate": False, "owner_command": False,
            "reply_to": None, "burst": False, "burst_fragments": [],
            "from_bot": False, "bot_turns": 0, "participants": [],
            "follow_up": False, "settings": None,
            FIELD: {"expires_at": iso(now + SLOT_SECS),
                    "max_messages": MAX_PER_SLOT,
                    "local_time": local.strftime("%A %H:%M"),
                    "timezone": tzname,
                    "targets": [self.describe(state, t, now)
                                for t in targets]},
        }
        d["slot"] = slot
        self.save()                  # the slot (and its nonce) first,
        self.m.nl_write_nonces(state)  # then the hook's nonce file,
        self.m.queue_edit(add=[item])  # then the queue
        print(f"Heartbeat: {len(targets)} place(s) or people it may reach "
              f"out to; queued slot {sid}.", flush=True)
        return item

    def end_slot(self, state, why):
        d = self.data()
        s = d.get("slot")
        if not s:
            return
        d["spent"][s["id"]] = self.clock()
        for old in sorted(d["spent"], key=d["spent"].get)[:-SPENT_KEEP]:
            del d["spent"][old]
        d["slot"] = None
        sid = s["id"]
        try:
            self.m.queue_edit(drop=lambda q: str(q.get("message_id")) == sid)
        except Exception as e:
            print(f"Proactive: could not drop slot {sid} ({e}).", flush=True)
        self.save()
        self.m.nl_write_nonces(state)
        print(f"Slot {sid} ended ({why}).", flush=True)

    def tick(self, state, now=None):
        """Main loop: slot upkeep, then the heartbeat when it is due."""
        now = self.clock() if now is None else now
        if now < self._next_tick:
            return
        self._next_tick = now + TICK_SECS
        d = self.data()
        s = d.get("slot")
        if s:
            sending = [p for p in s["posts"].values()
                       if p.get("status") == "sending"
                       and now - p.get("first", now) < SEND_RETRY_SECS + 60]
            if s.get("spent") and not sending:
                self.end_slot(state, "used")
            elif not s.get("spent") and now > s.get("expires", 0):
                self.end_slot(state, "expired unanswered")
            else:
                # The hook's copy of the nonce (after a restart, say);
                # written only when it changed.
                self.m.nl_write_nonces(state)
        cut = now - SENT_KEEP_SECS
        if any(e.get("at", 0) < cut for e in d["sent"]):
            d["sent"] = [e for e in d["sent"] if e.get("at", 0) >= cut]
            self._dirty = True
        if not d.get("seeded"):
            # People who DMed the bot before this module kept count.
            for mid, meta in list((state.get("item_meta") or {}).items()):
                self.note_item(mid, meta)
            d["seeded"] = True
            self._dirty = True
        freq = self.every("proactive.frequency")
        if d.get("freq") != freq or not d.get("next"):
            d["freq"], d["next"] = freq, self.schedule(now, freq)
            self._dirty = True
        elif now >= d["next"]:
            d["next"] = self.schedule(now, freq)
            self._dirty = True
            if d.get("slot"):
                print("Heartbeat skipped: the last one is still open.",
                      flush=True)
            elif self.enabled_anywhere() and not self.quiet_now(now) \
                    and self.sent_total(now) < self.total_cap():
                targets = self.eligible(state, now)
                if targets:
                    self.make_slot(state, targets, now)
                    return
                print("Heartbeat: nobody and nowhere to reach out to now; "
                      "no wake.", flush=True)
        if self._dirty and now - self._flushed >= FLUSH_SECS:
            self.save()

    # -- the worker's answer to a slot -------------------------------------------
    def outbox_item(self, state, item):
        """None when the outbox item names no slot (it goes the usual way);
        else a Verdict. Posts only to the slot's targets, with its nonce,
        while it is fresh and unspent, within the caps."""
        d = self.data()
        s = d.get("slot") or {}
        names = [str(item[f]) for f in ("item", "reply_to") if item.get(f)]
        known = set(d["spent"]) | ({s["id"]} if s else set())
        sid = next((n for n in names if n in known), None)
        if sid is None:
            return None
        now = self.clock()
        if str(item.get("item") or "") != sid:
            return Verdict(f"slot {sid}: item must be the slot's message_id")
        bad = [k for k in FORBIDDEN if k in item]
        if bad:
            return Verdict(f"slot {sid}: carries no {', '.join(bad)}")
        if not s or s["id"] != sid:
            return Verdict(f"slot {sid}: already used or expired")
        got = item.get(NONCE_FIELD)
        if not isinstance(got, str) or not hmac.compare_digest(
                s["nonce"].encode(), got.encode("utf-8", "replace")):
            # A forgery from another wake: the slot is untouched.
            return Verdict(f"slot {sid}: bad nonce")
        cid = str(item.get("channel_id") or "")
        post = s["posts"].get(cid)
        if post and post.get("status") == "sending":
            return self._send(state, s, cid, item, now)   # a retry
        if s.get("spent"):
            return Verdict(f"slot {sid}: already used", spent=sid)
        if now > s.get("expires", 0):
            return Verdict(f"slot {sid}: expired", spent=sid)
        if item.get("silent"):
            s["spent"] = True
            self.save()
            print(f"Slot {sid}: the worker chose not to reach out.",
                  flush=True)
            return Verdict(spent=sid)
        t = s["targets"].get(cid)
        if t is None:
            s["spent"] = True
            self.save()
            return Verdict(f"slot {sid}: {cid} is not one of its targets",
                           spent=sid)
        t = dict(t, channel_id=cid)
        why = self.target_problem(state, t, now)
        if why is None and len(s["posts"]) >= s.get("max_posts",
                                                    MAX_PER_SLOT):
            why = "the slot's messages are used"
        parts = self.m.normalize_parts(item)
        if why is None and not parts:
            why = "nothing to send"
        if why is None and t["type"] != "owner" \
                and self.m.never_share_hit(state, {"channel_id": cid}, None,
                                           parts):
            why = "it contains a never_share phrase (dropped)"
        if why is not None:
            s["spent"] = True
            self.save()
            return Verdict(f"slot {sid}: not sent to {cid}: {why}",
                           spent=sid)
        s["posts"][cid] = {"status": "sending", "first": now, "n": 0,
                           "next": 0, "parts": parts}
        if len(s["posts"]) >= s.get("max_posts", MAX_PER_SLOT):
            s["spent"] = True
        entry = {"at": now, "slot": sid, "channel_id": cid,
                 "user_id": t.get("user_id"), "type": t["type"],
                 "pending": True}
        d["sent"].append(entry)
        self.save()
        return self._send(state, s, cid, item, now)

    def _send(self, state, s, cid, item, now):
        """Send (or retry) the accepted post for cid: the parts fixed at
        acceptance (the outbox copy is worker-writable), pinging at most
        that one person."""
        sid, post = s["id"], s["posts"][cid]
        if post.get("next", 0) > now:
            return Verdict(retry=True, spent=sid)
        t = s["targets"][cid]
        mentions = {"parse": []}
        if t.get("user_id"):
            mentions["users"] = [t["user_id"]]
        parts = post["parts"]
        try:
            for i, part in enumerate(parts):
                if i:
                    self.m.send_typing(cid)
                    time.sleep(self.rng.uniform(1.0, 2.0))
                self.m.api_post(f"/channels/{cid}/messages", {
                    "content": part, "allowed_mentions": mentions,
                    "nonce": self.m.send_nonce(cid, sid, parts, i),
                    "enforce_nonce": True})
        except Exception as e:
            code = getattr(e, "code", None)
            post["n"] = post.get("n", 0) + 1
            if code == 403 or now - post["first"] >= SEND_RETRY_SECS:
                post["status"] = "failed"
                self.data()["sent"] = [x for x in self.data()["sent"]
                                       if not (x.get("slot") == sid and
                                               x.get("channel_id") == cid)]
                self.save()
                print(f"Slot {sid}: could not post to {cid} ({e}); "
                      f"dropped.", flush=True)
                return Verdict(spent=sid)
            post["next"] = now + min(30, 2 ** post["n"])
            self.save()
            print(f"Slot {sid}: post to {cid} failed (attempt "
                  f"{post['n']}; retrying, same nonce): {e}", flush=True)
            return Verdict(retry=True, spent=sid)
        post["status"] = "sent"
        post.pop("parts", None)
        for x in self.data()["sent"]:
            if x.get("slot") == sid and x.get("channel_id") == cid:
                x.pop("pending", None)
        self.save()
        print(f"Slot {sid}: reached out in {cid} ({t['type']}, "
              f"{len(parts)} part(s)): {parts[0][:60]}", flush=True)
        return Verdict(spent=sid)

    # -- "stop messaging me" ------------------------------------------------------
    def opt_action(self, state, item):
        """None when the outbox item carries no opt-out / opt-in, or it was
        recorded; else why it is refused. The person is the author of
        the message the item answers (item_meta), never anything the
        worker wrote; only a person's own message (a mention or a DM,
        not a bot's) can carry it."""
        if FIELD not in item:
            return None
        v = item.get(FIELD)
        if v not in OPT_VALUES:
            return f"{FIELD}: expected \"opt-out\" or \"opt-in\""
        qid = str(item.get("item") or "")
        meta = (state.get("item_meta") or {}).get(qid)
        if not isinstance(meta, dict) or meta.get("bot") \
                or meta.get("kind") not in TEXT_KINDS:
            return (f"{FIELD}: goes on the reply to the person's own "
                    f"message (item: its message_id)")
        if str(item.get("channel_id")) != str(meta.get("channel_id")):
            return f"{FIELD}: channel_id must be that message's channel"
        uid = str(meta.get("author_id") or "")
        if not uid:
            return f"{FIELD}: whose message it is is not known"
        d = self.data()
        if v == "opt-out":
            d["opted_out"][uid] = self.clock()
        else:
            d["opted_out"].pop(uid, None)
        self.save()
        print(f"Proactive: {uid} {'opted out' if v == 'opt-out' else 'opted back in'}"
              f" (item {qid}).", flush=True)
        return None

