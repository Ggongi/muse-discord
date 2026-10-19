"""settings_ui (the /settings pages, Components V2), offline: one test
or a few per screen and rule of the redesign spec, against real
config.yaml + config.discord.yaml files in a temp dir (writes go through
muse_settings.apply / undo, as the monitor's Ctx does). The fixtures are
in harness.py; crawl.py / test_crawl.py check every page's limits.

Usage: [MUT_DIR=<tree>] python3 tests/settingsui/test_settings_ui.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402
from harness import (ADMIN, ANA, ART, BOB, BOT, CAT, G1, G2, G3,  # noqa: E402
                     GENERAL, HIDDEN, KAI, LOBBY, MANAGE, MEMBER, MS,
                     MUSECHAT, OWNER, OWNER2, ROLE, SAM, SPAM, THREAD, UI,
                     WELCOME, FakeCtx, by_label, command, component,
                     context_command, find, option, submit, text)

S1 = MS.Place.server(G1)
S2 = MS.Place.server(G2)
MC = MS.Place.channel(G1, MUSECHAT)
GEN = MS.Place.channel(G1, GENERAL, CAT)
DMS = MS.Place.dms()
DEF = MS.Place.defaults()


class Base(unittest.TestCase):
    text = ""

    def setUp(self):
        H.install_determinism()
        self.ctx = FakeCtx()

    def h(self, ix):
        self.ctx.refresh()
        r = UI.handle(ix, self.ctx)
        self.assertFalse(any("Traceback" in m for m in self.ctx.logs),
                         self.ctx.logs)
        return r

    def owner(self, cid, values=None, guild=G1, channel=MUSECHAT, **kw):
        return self.h(component(OWNER, cid, values, guild,
                                "0" if guild else None, channel, **kw))

    def admin(self, cid, values=None, channel=GENERAL, **kw):
        return self.h(component(ADMIN, cid, values, G1, MANAGE, channel,
                                **kw))

    def dm(self, cid, values=None, **kw):
        return self.h(component(OWNER, cid, values, None, None, **kw))

    def val(self, key, place):
        return self.ctx.value(key, place)

    def assertNotice(self, r, words):
        self.assertEqual(r["type"], 4)
        self.assertEqual(r["data"]["flags"], 64 | 32768)
        self.assertIn(words, text(r))

    def assertPage(self, r, words=None):
        self.assertIn(r["type"], (4, 7), r)
        self.assertTrue(r["data"]["flags"] & 32768)
        self.assertNotIn("content", r["data"])
        if words:
            self.assertIn(words, text(r))

    def submit(self, cid, fields, guild=G1, user=OWNER, channel=MUSECHAT):
        self.ctx.refresh()
        return UI.handle(submit(user, cid, fields, guild,
                                "0" if guild else None, channel), self.ctx)


# ---------------------------------------------------------------------------
# Entry points (spec §1)
# ---------------------------------------------------------------------------
class Entry(Base):
    def test_owner_in_a_channel_gets_that_channel(self):
        r = self.h(command(OWNER, G1, "0", MUSECHAT))
        self.assertEqual(r["type"], 4)
        self.assertEqual(r["data"]["flags"], 64 | 32768)
        self.assertIn("## #muse-chat", text(r))
        self.assertEqual(r["data"]["components"][0]["accent_color"],
                         UI.BLURPLE)

    def test_thread_opens_its_parent(self):
        r = self.h(command(OWNER, G1, "0", THREAD, 11, MUSECHAT))
        self.assertIn("## #muse-chat", text(r))
        self.assertIn("You ran this in a thread; threads follow #muse-chat",
                      text(r))

    def test_owner_dm_opens_dms(self):
        r = self.h(command(OWNER))
        self.assertIn("## DMs", text(r))

    def test_admin_gets_the_quiet_panel(self):
        r = self.h(command(ADMIN, G1, MANAGE, GENERAL))
        self.assertIn("## Quiet the bot in Muse Lounge", text(r))
        self.assertEqual(r["data"]["components"][0]["accent_color"],
                         UI.AMBER)
        r = self.h(command(ADMIN, G1, str(1 << 3), GENERAL))  # Administrator
        self.assertIn("Quiet the bot", text(r))

    def test_member_refused(self):
        r = self.h(command(MEMBER, G1, "0"))
        self.assertNotice(r, "Only the bot's owners can change its settings"
                          ". Server admins (Manage Server) can make it "
                          "quieter here.")
        self.assertEqual(UI.count_components(r["data"]["components"]), 2)
        r = self.h(command(MEMBER))
        self.assertNotice(r, "Only the bot's owners can use /settings in "
                          "DMs. Server admins: run it in your server.")
        r = self.h(command(ADMIN))      # an admin in a DM
        self.assertNotice(r, "can use /settings in DMs")

    def test_configure_still_opens_the_server(self):
        r = self.dm(f"mdset:open:server:{G2}")
        self.assertEqual(r["type"], 4)
        self.assertIn("## Study Group", text(r))
        sel = find(r, f"mdset:2:gc:s{G2}")
        self.assertEqual(sel["type"], 3)        # string fallback in a DM
        r = self.h(component(MEMBER, f"mdset:open:server:{G2}"))
        self.assertNotice(r, "Only the bot's owners")

    def test_old_ids_are_out_of_date(self):
        for cid in ("mdset:p:e", "mdset:s:d:df", "mdset:h:0", "mdset:go",
                    "mdset:1:pg:x:b", "mdset:open:x", "mdset:2"):
            for user in (OWNER, MEMBER):
                r = self.h(component(user, cid))
                self.assertNotice(r, UI.OUT_OF_DATE)
        self.assertEqual(self.ctx.applied, [])

    def test_crash_is_an_apology(self):
        class Boom(FakeCtx):
            def guilds(self):
                raise RuntimeError("boom")

            @property
            def owners(self):
                raise RuntimeError("boom")

            @owners.setter
            def owners(self, v):
                pass
        ctx = Boom()
        r = UI.handle(command(OWNER, G1, "0"), ctx)
        self.assertNotice(r, "Sorry, something went wrong")
        self.assertTrue(any("RuntimeError" in m for m in ctx.logs))


# ---------------------------------------------------------------------------
# S1. Channel page
# ---------------------------------------------------------------------------
class Channel(Base):
    def test_same_as_names_the_category(self):
        r = self.owner(f"mdset:2:pg:c{G1}.{GENERAL}:b", channel=GENERAL)
        sel = find(r, f"mdset:2:set:c{G1}.{GENERAL}:r")
        first = sel["options"][0]
        self.assertEqual(first["label"], "Same as Staff (category): Off")
        self.assertTrue(first["default"])
        self.assertEqual([o["value"] for o in sel["options"]],
                         ["=", "conversational", "when-addressed", "off"])

    def test_cant_see_warning(self):
        r = self.owner(f"mdset:2:pg:c{G1}.{HIDDEN}:b")
        self.assertIn("The bot can't see #hidden, so it never answers "
                      "there.", text(r))

    def test_set_then_undo_from_the_banner(self):
        r = self.owner(f"mdset:2:set:c{G1}.{MUSECHAT}:r", ["when-addressed"])
        self.assertIn("**Done:** In #muse-chat, When it answers: Chatty → "
                      "Only when @mentioned", text(r))
        self.assertIn("● changed here by @you, Oct 19", text(r))
        self.assertEqual(self.val("who.replies", MC), "when-addressed")
        undo = by_label(r, "Undo")
        self.assertTrue(undo["custom_id"].startswith(
            f"mdset:2:un:c{G1}.{MUSECHAT}:g"))
        r = self.owner(undo["custom_id"],
                       message={"components": r["data"]["components"]})
        # back to Chatty in a 1,240-member server: confirm first
        self.assertIn("Check before undoing", text(r))
        r = self.owner(by_label(r, "Undo")["custom_id"])
        self.assertIn("**Undone:**", text(r))
        self.assertIsNone(by_label(r, "Redo"))
        self.assertEqual(self.val("who.replies", MC), "conversational")
        self.assertIn("## #muse-chat", text(r))

    def test_no_op_guard(self):
        r = self.owner(f"mdset:2:set:c{G1}.{MUSECHAT}:r", ["conversational"])
        self.assertIn("**No change:** In #muse-chat, When it answers is "
                      "already Chatty, same as Muse Lounge.", text(r))
        r = self.owner(f"mdset:2:set:c{G1}.{MUSECHAT}:r", ["="])
        self.assertIn("No change", text(r))
        self.assertEqual(self.ctx.applied, [])

    def test_instructions_modal_at_a_channel(self):
        r = self.owner(f"mdset:2:mod:c{G1}.{MUSECHAT}:i:b")
        self.assertEqual(r["type"], 9)
        self.assertEqual(r["data"]["title"], "Instructions · #muse-chat")
        ids = [c["component"]["custom_id"] for c in r["data"]["components"]
               if c["type"] == 18]
        self.assertEqual(ids, ["n"])            # no length at a channel
        r = self.submit(f"mdset:2:sub:c{G1}.{MUSECHAT}:i:b",
                        {"n": "Stay on topic."})
        self.assertIn("**Done:**", text(r))
        self.assertIn("Added for #muse-chat: “Stay on topic.”", text(r))
        self.assertEqual(self.val("style.notes", MC),
                         "Friendly community. Playful is fine.\n"
                         "Stay on topic.")

    def test_toggle_is_absolute(self):
        cid = f"mdset:2:val:c{G1}.{MUSECHAT}:~media.enabled:on:b"
        self.owner(cid)
        r = self.owner(cid)                 # a stale page: still "on"
        self.assertIn("No change", text(r))
        self.assertIs(self.val("plugins.media.enabled", MC), True)

    def test_pause_names_the_place(self):
        r = self.owner(f"mdset:2:pg:c{G1}.{MUSECHAT}:b")
        self.assertEqual(by_label(r, "Pause #muse-chat")["style"], 4)
        r = self.owner(f"mdset:2:pz:c{G1}.{MUSECHAT}")
        self.assertIn("**Paused:** Off in #muse-chat since Oct 19 (you).",
                      text(r))
        self.assertEqual(r["data"]["components"][1]["accent_color"],
                         UI.GREY)
        r = self.owner(by_label(r, "Resume #muse-chat")["custom_id"])
        # Chatty again in a 1,240-member server: a risky outcome confirms
        self.assertIn("Check before applying", text(r))
        r = self.owner(by_label(r, "Apply")["custom_id"])
        self.assertEqual(self.val("who.replies", MC), "conversational")

    def test_channel_select_in_guild_and_fallback_elsewhere(self):
        r = self.owner(f"mdset:2:pg:c{G1}.{MUSECHAT}:b")
        sel = find(r, f"mdset:2:gc:c{G1}.{MUSECHAT}")
        self.assertEqual(sel["type"], 8)
        self.assertEqual(sel["channel_types"], [0, 5, 15, 16, 2, 13])
        r = self.owner(sel["custom_id"], [GENERAL],
                       resolved=H.resolved_for(8, [GENERAL]))
        self.assertIn("## #general", text(r))
        # From another server: paged string select, own settings first
        r = self.owner(f"mdset:2:pg:s{G1}:b", guild=G3, channel=ART)
        sel = find(r, f"mdset:2:gc:s{G1}")
        self.assertEqual(sel["type"], 3)
        self.assertEqual(len(sel["options"]), 25)   # 23 + More + Find
        self.assertEqual(sel["options"][-2]["value"], "p1")
        self.assertEqual(sel["options"][-1]["value"], "find")
        self.assertEqual(self.owner(sel["custom_id"], ["find"], guild=G3,
                                    channel=ART)["type"], 9)
        r = self.owner(sel["custom_id"], ["p3"], guild=G3, channel=ART)
        sel = find(r, f"mdset:2:gc:s{G1}")
        self.assertEqual(sel["options"][0]["value"], "p2")
        r = self.owner(f"mdset:2:mod:s{G1}:fc:b", guild=G3, channel=ART)
        self.assertEqual(r["type"], 9)
        r = self.submit(f"mdset:2:sub:s{G1}:fc:b", {"q": "lob"}, guild=G3,
                        channel=ART)
        sel = find(r, f"mdset:2:gc:s{G1}")
        self.assertEqual([o["label"] for o in sel["options"]], ["#lobby"])

    def test_deleted_channel_falls_back_to_the_server(self):
        r = self.owner(f"mdset:2:pg:c{G1}.71999999999999999999:b")
        self.assertIn("That channel no longer exists.", text(r))
        self.assertIn("## Muse Lounge", text(r))


# ---------------------------------------------------------------------------
# S2-S4. Server Basics, More, Extras; S10 presets
# ---------------------------------------------------------------------------
class Server(Base):
    def test_basics(self):
        r = self.owner(f"mdset:2:pg:s{G1}:b")
        t = text(r)
        self.assertIn("-# 1,240 members · Custom settings", t)
        self.assertIn("● changed here by @you, Oct 19 (config.yaml says "
                      "Only when @mentioned)", t)
        sel = find(r, f"mdset:2:set:s{G1}:r")
        self.assertEqual(option(sel, value="when-addressed")["description"],
                         "What config.yaml says")
        self.assertTrue(option(sel, value="conversational")["default"])
        self.assertIsNotNone(find(r, f"mdset:2:pre:s{G1}"))
        self.assertIn("Last change in Muse Lounge or its channels:", t)
        self.assertLessEqual(UI.count_components(r["data"]["components"]),
                             35)

    def test_community_preset_one_group_one_undo(self):
        r = self.owner(f"mdset:2:pre:s{G1}", ["community"])
        t = text(r)
        self.assertIn("## Apply the Community preset to Muse Lounge?", t)
        self.assertIn("- When it answers: Chatty → Only when @mentioned", t)
        self.assertIn("- Replies per person per day: No cap → 20", t)
        self.assertIn("Skipped: Generations per day (image and video is off "
                      "here).", t)
        self.assertEqual(self.ctx.applied, [])          # a preview only
        r = self.owner(by_label(r, "Apply Community")["custom_id"])
        self.assertIn("**Done:**", text(r))
        self.assertIn("Matches the **Community** preset", text(r))
        self.assertEqual(self.val("limits.per_day", S1), 300)
        r = self.owner(by_label(r, "Undo")["custom_id"])
        self.assertIn("Check before undoing", text(r))   # Chatty, 1,240
        r = self.owner(by_label(r, "Undo")["custom_id"])
        self.assertIsNone(self.val("limits.per_day", S1))
        self.assertEqual(self.val("who.replies", S1), "conversational")

    def test_friends_in_a_big_server_leaves_memory_out(self):
        self.owner(f"mdset:2:set:s{G1}:r", ["when-addressed"])
        r = self.owner(f"mdset:2:pre:s{G1}", ["friends"])
        self.assertIn("Not included: What it may say about you stays Public "
                      "facts only, because Muse Lounge has more than 50 "
                      "members.", text(r))

    def test_caps_modal_blank_prefill_and_errors(self):
        r = self.owner(f"mdset:2:mod:s{G1}:c:b")
        self.assertEqual(r["type"], 9)
        self.assertEqual(r["data"]["title"], "Reply caps · Muse Lounge")
        for c in r["data"]["components"]:
            if c["type"] == 18:
                self.assertNotIn("value", c["component"])   # never "none"
        r = self.submit(f"mdset:2:sub:s{G1}:c:b", {"pp": "lots", "pd": "5"})
        self.assertIn("**Not saved:** “lots” isn't a whole number. Replies "
                      "per person per day takes 0 to 10,000, or blank for no "
                      "cap.", text(r))
        self.assertIsNone(self.val("limits.per_day", S1))   # atomic
        retry = by_label(r, "Try again")
        r = self.owner(retry["custom_id"])
        self.assertEqual(r["type"], 9)
        vals = {c["component"]["custom_id"]: c["component"].get("value")
                for c in r["data"]["components"] if c["type"] == 18}
        self.assertEqual(vals, {"pp": "lots", "pd": "5"})
        r = self.submit(f"mdset:2:sub:s{G1}:c:b", {"pp": "1,000",
                                                   "pd": "0"})
        self.assertIn("0 means only the owners get replies in Muse Lounge.",
                      text(r))
        self.assertEqual(self.val("limits.per_person_per_day", S1), 1000)
        r = self.submit(f"mdset:2:sub:s{G1}:c:b", {"pp": "", "pd": "none"})
        # no cap any more in a 1,240-member server: confirm first
        self.assertIn("Replies per day (total): no limit any more", text(r))
        r = self.owner(by_label(r, "Apply")["custom_id"])
        self.assertIsNone(self.val("limits.per_day", S1))
        r = self.submit(f"mdset:2:sub:s{G1}:c:b", {"pp": "", "pd": "100001"})
        self.assertIn("100,001 is out of range", text(r))

    def test_bot_exchange_rejects_zero(self):
        r = self.submit(f"mdset:2:sub:s{G1}:bx:m", {"v": "0"})
        self.assertIn("Replies in a row to other bots takes 1 to 50", text(r))
        r = self.submit(f"mdset:2:sub:s{G1}:bx:m", {"v": "4"})
        self.assertIn("› More settings", text(r))   # back where it was
        self.assertEqual(self.val("who.bot_exchange_limit", S1), 4)

    def test_welcome_on_names_the_channel_or_warns(self):
        r = self.owner(f"mdset:2:val:s{G1}:~welcome.enabled:on:b")
        self.assertIn("Welcomes go to: The server's system channel, "
                      "#welcome.", text(r))
        r = self.owner(f"mdset:2:val:s{G2}:~welcome.enabled:on:b")
        self.assertIn("⚠ Study Group has no system channel, so welcomes are "
                      "skipped. Pick a channel.", text(r))

    def test_extras_welcome_channel(self):
        r = self.owner(f"mdset:2:pg:s{G1}:e")
        sel = find(r, f"mdset:2:set:s{G1}:~welcome.channel")
        self.assertEqual(sel["type"], 8)
        self.assertEqual(sel["channel_types"], [0, 5])
        r = self.owner(sel["custom_id"], [LOBBY],
                       resolved=H.resolved_for(8, [LOBBY]))
        self.assertIn("The bot can't post there; welcomes are skipped.",
                      text(r))
        self.assertEqual(self.val("plugins.welcome.channel", S1), LOBBY)
        r = self.owner(f"mdset:2:val:s{G1}:~welcome.channel:system:e")
        self.assertIn("The server's system channel, #welcome", text(r))

    def test_media_who_people(self):
        r = self.owner(f"mdset:2:set:s{G1}:~media.who", ["list"])
        self.assertIn("Pick people below; nothing changes until you do.",
                      text(r))
        self.assertEqual(self.ctx.applied, [])
        sel = find(r, f"mdset:2:ppl:s{G1}:~media.who")
        r = self.owner(sel["custom_id"], [SAM],
                       resolved=H.resolved_for(7, [SAM]))
        self.assertEqual(self.val("plugins.media.who", S1), [OWNER, SAM])

    def test_more_people_selects(self):
        r = self.owner(f"mdset:2:pg:s{G1}:m")
        ig = find(r, f"mdset:2:ppl:s{G1}:ig")
        self.assertEqual(ig["type"], 7)
        self.assertEqual([d["id"] for d in ig["default_values"]], [BOB, SPAM])
        r = self.owner(f"mdset:2:ppl:s{G1}:al", [SAM, ROLE],
                       resolved=H.resolved_for(7, [SAM, ROLE]))
        # only answering two people in a 1,240-member server: confirm
        self.assertIn("Check before applying", text(r))
        r = self.owner(by_label(r, "Apply")["custom_id"])
        self.assertEqual(self.val("who.allow", S1), [SAM, f"role:{ROLE}"])
        self.assertIsNotNone(by_label(r, "Answer everyone"))
        r = self.owner(f"mdset:2:ppl:s{G1}:al", [])
        self.assertEqual(self.val("who.allow", S1), "everyone")
        # owners can't be added to Never answer
        r = self.owner(f"mdset:2:ppl:s{G1}:ig", [OWNER, KAI],
                       resolved=H.resolved_for(7, [OWNER, KAI]))
        self.assertIn("Owners can't be added to Never answer.", text(r))
        self.assertEqual(self.val("who.ignore", S1), [KAI])

    def test_hidden_answer_value(self):
        r = self.owner(f"mdset:2:pg:s{G2}:m")
        sel = find(r, f"mdset:2:set:s{G2}:su")
        last = sel["options"][-1]
        self.assertEqual(last["label"], "Answer everything (old setting "
                                         "from config.yaml)")
        self.assertTrue(last["default"])
        self.assertEqual(sum(1 for o in sel["options"] if o.get("default")),
                         1)

    def test_server_pause_lists_channels_that_still_answer(self):
        self.owner(f"mdset:2:set:c{G1}.{MUSECHAT}:r", ["when-addressed"])
        r = self.owner(f"mdset:2:pz:s{G1}")
        self.assertIn("-# #muse-chat still answers; it has its own setting.",
                      text(r))


# ---------------------------------------------------------------------------
# S5-S7. Defaults, DMs, Bot-wide
# ---------------------------------------------------------------------------
class DefaultsAndDMs(Base):
    def test_defaults_page(self):
        r = self.owner("mdset:2:pg:x:b")
        t = text(r)
        self.assertIn("The bot is in 63 servers; 2 have their own settings.",
                      t)
        sel = find(r, "mdset:2:set:x:r")
        self.assertNotEqual(sel["options"][0]["value"], "=")
        self.assertTrue(option(sel, value="conversational")[
            "description"].startswith("Built-in default"))
        self.assertIsNotNone(by_label(r, "Back to #muse-chat"))
        r = self.dm("mdset:2:pg:x:b")
        self.assertEqual(find(r, "mdset:2:go:x")["type"], 3)

    def test_pause_everywhere_confirms_and_undoes(self):
        r = self.owner("mdset:2:pz:x")
        self.assertIn("## Check before applying", text(r))
        self.assertIn("Pause everywhere: the bot stops answering everyone",
                      text(r))
        self.assertEqual(r["data"]["components"][0]["accent_color"], UI.RED)
        self.assertEqual(self.ctx.applied, [])
        r2 = self.owner(by_label(r, "Cancel")["custom_id"])
        self.assertIn("## Defaults", text(r2))
        r = self.owner(by_label(r, "Apply")["custom_id"])
        self.assertIn("**Paused everywhere:**", text(r))
        self.assertTrue(MS.is_paused(self.ctx.reload(), DEF))
        r = self.owner(by_label(r, "Undo")["custom_id"])
        # Chatty again in Pixel Plaza (48,213 members): the Undo confirms
        self.assertIn("Check before undoing", text(r))
        r = self.owner(by_label(r, "Undo")["custom_id"])
        self.assertFalse(MS.is_paused(self.ctx.reload(), DEF))

    def test_expired_confirmation(self):
        r = self.owner("mdset:2:pz:x")
        ok = by_label(r, "Apply")["custom_id"]
        self.ctx.stash.clear()          # a restart
        r = self.owner(ok)
        self.assertEqual(r["type"], 7)
        self.assertIn("This confirmation expired, so nothing was changed.",
                      text(r))
        self.assertIsNotNone(by_label(r, "Open Defaults settings"))
        self.assertEqual(self.ctx.applied, [])

    def test_bot_wide(self):
        r = self.owner("mdset:2:pg:x:w")
        self.assertIn("Shown as Watching; the kind is set in config.yaml.",
                      text(r))
        r = self.owner("mdset:2:mod:x:st:w")
        self.assertEqual(r["data"]["title"], "Status line")
        r = self.submit("mdset:2:sub:x:st:w", {"v": "Helping out"})
        self.assertIn("“Helping out”", text(r))
        r = self.owner("mdset:2:val:x:un:off:w")
        self.assertIn("Off: no DM when a new version is out", text(r))

    def test_dms_user_select_in_a_server_no_false_warning(self):
        r = self.owner("mdset:2:pg:d:b")
        sel = find(r, "mdset:2:ppl:d:df")
        self.assertEqual(sel["type"], 5)
        r = self.owner(sel["custom_id"], [SAM],
                       resolved=H.resolved_for(5, [SAM]))
        self.assertIn("**Done:** In DMs, Who can DM it: Only you → You and "
                      "@Sam", text(r))
        self.assertEqual(self.val("who.dms_from", DMS), [SAM])
        self.assertEqual(MS.dm_access(self.ctx.reload()), "list")

    def test_dms_in_a_dm_add_by_id_and_remove(self):
        r = self.dm("mdset:2:pg:d:b")
        self.assertIsNone(find(r, "mdset:2:ppl:d:df"))
        self.assertIn("To search by name, run /settings in a server",
                      text(r))
        r = self.dm(by_label(r, "Add by ID")["custom_id"])
        self.assertEqual(r["type"], 9)
        r = self.submit("mdset:2:sub:d:id.df:b",
                        {"ids": f"<@{SAM}>, {KAI} {OWNER}"}, guild=None)
        self.assertIn("Owners can always DM it.", text(r))
        self.assertEqual(self.val("who.dms_from", DMS), [SAM, KAI])
        rm = find(r, "mdset:2:rm:d:df")
        self.assertEqual([o["label"] for o in rm["options"]], ["Sam", "Kai"])
        r = self.dm(rm["custom_id"], [SAM, KAI])
        self.assertEqual(self.val("who.dms_from", DMS), "owners")
        self.assertIn("Only you", text(r) + str(find(r, "mdset:2:set:d:df")))

    def test_pick_people_with_nobody_yet_writes_nothing(self):
        r = self.owner("mdset:2:set:d:df", ["list"])
        self.assertIn("Pick people below; nothing changes until you do.",
                      text(r))
        self.assertEqual(self.ctx.applied, [])
        sel = find(r, "mdset:2:set:d:df")
        self.assertTrue(option(sel, value="list")["default"])

    def test_nobody_and_back(self):
        r = self.owner("mdset:2:set:d:df", ["nobody"])
        self.assertEqual(MS.dm_access(self.ctx.reload()), "nobody")
        self.assertEqual(r["data"]["components"][1]["accent_color"], UI.GREY)
        self.assertNotIn("Pause", str([c.get("label") for c in
                                       H.controls(r)]))
        r = self.owner("mdset:2:ppl:d:df", [SAM],
                       resolved=H.resolved_for(5, [SAM]))
        self.assertEqual(MS.dm_access(self.ctx.reload()), "list")
        self.assertEqual(len({h.get("group") for h in self.ctx.overlay()[
            "history"][-2:]}), 1)       # resume + Who can DM it: one group
        r = self.owner("mdset:2:set:d:df", ["owners"])
        self.assertEqual(MS.dm_access(self.ctx.reload()), "owners")
        r = self.owner("mdset:2:set:d:df", ["anyone"])
        self.assertIn("Anyone on Discord, strangers included", text(r))

    def test_two_owners(self):
        self.ctx = FakeCtx(owners=(OWNER, OWNER2), seed=False)
        r = self.owner("mdset:2:pg:d:b")
        sel = find(r, "mdset:2:set:d:df")
        self.assertEqual(sel["options"][0]["label"],
                         "Only you and the other owners")


# ---------------------------------------------------------------------------
# S8. Why no reply?
# ---------------------------------------------------------------------------
class Why(Base):
    def test_place_page_and_fix(self):
        r = self.owner(f"mdset:2:pg:c{G1}.{MUSECHAT}:y")
        t = text(r)
        self.assertIn("## Why no reply? · #muse-chat", t)
        self.assertEqual(t.count("[message]("), 5)
        self.assertIn(f"<@{SPAM}> is on **Never answer** in Muse Lounge "
                      f"(added by <@{ADMIN}>, admin, Oct 19).", t)
        self.assertIn(f"<@{ANA}> hit Muse Lounge's cap of 5 replies per "
                      f"person per day.", t)
        fix = by_label(r, "Remove spam1")
        r = self.owner(fix["custom_id"],
                       message={"components": r["data"]["components"]})
        self.assertNotIn(SPAM, self.val("who.ignore", S1))
        self.assertIn(f"-# Fixed. <@{SPAM}> must send again; refused "
                      f"messages stay unanswered.", text(r))
        self.assertIn("## Why no reply? · #muse-chat", text(r))
        cap = by_label(r, "Change cap")
        self.assertEqual(self.owner(cap["custom_id"])["type"], 9)

    def test_category_cause_has_no_fix(self):
        r = self.owner(f"mdset:2:pg:c{G1}.{GENERAL}:y", channel=GENERAL)
        self.assertIn("When it answers is Off in Staff (category).",
                      text(r))
        self.assertEqual([c for c in H.controls(r)
                          if ":fx:" in c["custom_id"]], [])

    def test_empty(self):
        r = self.owner(f"mdset:2:pg:c{G1}.{WELCOME}:y")
        self.assertIn("Nothing was refused here recently.", text(r))

    def test_dms_person_check(self):
        r = self.owner("mdset:2:pg:d:y")
        sel = find(r, "mdset:2:wy:d")
        self.assertEqual(sel["type"], 5)
        r = self.owner(sel["custom_id"], [SAM],
                       resolved=H.resolved_for(5, [SAM]))
        self.assertIn("Sam can't DM it: Who can DM it is Only you.", text(r))
        r = self.owner(by_label(r, "Let Sam DM it")["custom_id"])
        self.assertEqual(self.val("who.dms_from", DMS), [SAM])
        self.assertIn("Sam can DM it.", text(r))
        r = self.dm("mdset:2:pg:d:y")
        self.assertIsNotNone(by_label(r, "Check by ID"))

    def test_context_command(self):
        spam_msg = H.OUTCOMES[0]["message_id"]
        r = self.h(context_command(OWNER, spam_msg, author=SPAM))
        self.assertEqual(r["type"], 4)
        t = text(r)
        self.assertIn("## Why no reply?", t)
        self.assertIn("in #muse-chat, 4 min ago", t)
        self.assertIn("Removing spam1 doesn't answer this message", t)
        self.assertEqual(UI.count_components(r["data"]["components"]), 9)
        self.assertIsNotNone(by_label(r, "Open #muse-chat settings"))
        replied = H.OUTCOMES[13]["message_id"]
        r = self.h(context_command(OWNER, replied, author=KAI))
        self.assertIn("It replied.", text(r))
        queued = H.OUTCOMES[14]["message_id"]
        r = self.h(context_command(OWNER, queued, author=ANA))
        self.assertIn("Still working on it (queued 1 min ago).", text(r))
        r = self.h(context_command(OWNER, "1234567890123456789", author=BOT))
        self.assertIn("That's the bot's own message.", text(r))
        r = self.h(context_command(OWNER, "1234567890123456789", author=SAM,
                                   mentions=()))
        self.assertIn("Not recorded: it didn't @mention the bot or reply to "
                      "it, and in #muse-chat it answers @mentions", text(r))
        r = self.h(context_command(OWNER, spam_msg, author=SPAM,
                                   name="Why no reply"))
        self.assertIn("## Why no reply?", text(r))

    def test_context_command_who_may(self):
        msg = H.OUTCOMES[1]["message_id"]
        r = self.h(context_command(ADMIN, msg, perms=MANAGE, author=ANA))
        self.assertIn("hit Muse Lounge's cap", text(r))
        self.assertIsNone(by_label(r, "Change cap"))    # not an admin's
        self.assertTrue(find(r, "mdset:2:pg:")["custom_id"].endswith(":q"))
        r = self.h(context_command(MEMBER, msg, perms="0"))
        self.assertNotice(r, "Only the bot's owners and this server's admins "
                          "can see why the bot didn't reply.")


# ---------------------------------------------------------------------------
# S11. Undo; S12. admins; S14. errors
# ---------------------------------------------------------------------------
class UndoAndErrors(Base):
    def test_stale_group(self):
        r = self.owner(f"mdset:2:set:c{G1}.{MUSECHAT}:r", ["off"])
        stale = by_label(r, "Undo")["custom_id"]
        self.owner(f"mdset:2:set:c{G1}.{MUSECHAT}:r", ["when-addressed"])
        r = self.owner(stale)
        self.assertIn("Something changed since. Undo now would: In "
                      "#muse-chat, When it answers → back to Off", text(r))
        self.assertEqual(self.val("who.replies", MC), "when-addressed")
        self.assertIsNotNone(by_label(r, "Undo that"))

    def test_last_change_slot_undoes_a_channel_change(self):
        self.owner(f"mdset:2:set:c{G1}.{MUSECHAT}:r", ["off"])
        r = self.owner(f"mdset:2:pg:s{G1}:b")
        self.assertIn("-# Last change in Muse Lounge or its channels: In "
                      "#muse-chat, When it answers: Chatty → Off (you, ",
                      text(r))
        r = self.owner(by_label(r, "Undo")["custom_id"],
                       message={"components": r["data"]["components"]})
        r = self.owner(by_label(r, "Undo")["custom_id"])    # confirm
        self.assertEqual(self.val("who.replies", MC), "conversational")
        self.assertIn("## Muse Lounge", text(r))

    def test_broken_overlay_removes_write_controls(self):
        self.ctx = FakeCtx(broken_overlay=True)
        r = self.owner(f"mdset:2:pg:s{G1}:b")
        self.assertIn(UI.OVERLAY_BROKEN, text(r))
        verbs = {c["custom_id"].split(":")[2] for c in H.controls(r)}
        self.assertEqual(verbs & set(UI.WRITE_VERBS), set())
        self.assertIn("-# Now: Same as Defaults: Public facts only", text(r))
        r = self.owner(f"mdset:2:set:s{G1}:m", ["nothing"])
        self.assertIn("**Not saved:**", text(r))


class Admin(Base):
    def test_only_quieter_buttons(self):
        r = self.h(command(ADMIN, G1, MANAGE, MUSECHAT))
        labels = [c.get("label") for c in H.controls(r)]
        self.assertIn("Only when @mentioned in #muse-chat", labels)
        self.assertIn("Off in #muse-chat", labels)
        self.assertNotIn("Chatty in #muse-chat", labels)
        self.assertNotIn("Turn off welcome", labels)     # welcome is off

    def test_mute_and_undo(self):
        r = self.admin(f"mdset:2:val:c{G1}.{MUSECHAT}:r:off:q",
                       channel=MUSECHAT)
        self.assertIn("**Done:** In #muse-chat, When it answers: Chatty → "
                      "Off", text(r))
        self.assertEqual(self.ctx.notified[-1][:3],
                         (G1, ADMIN, f'servers."{G1}".channels."{MUSECHAT}"'
                          f'.replies'))
        self.assertIn("## Quiet the bot", text(r))
        r = self.admin(by_label(r, "Undo")["custom_id"], channel=MUSECHAT)
        self.assertIn("**Undone:**", text(r))
        self.assertEqual(self.val("who.replies", MC), "conversational")

    def test_never_answer_add_and_remove(self):
        r = self.admin(f"mdset:2:ppl:s{G1}:ig", [KAI],
                       resolved=H.resolved_for(7, [KAI]))
        self.assertEqual(self.val("who.ignore", S1), [BOB, SPAM, KAI])
        t = text(r)
        self.assertIn(f"<@{BOB}> (set by the owners)", t)
        self.assertIn(f"<@{KAI}> (added by @you)", t)
        rm = find(r, f"mdset:2:rm:s{G1}:ig")
        self.assertEqual({o["value"] for o in rm["options"]}, {SPAM, KAI})
        r = self.admin(rm["custom_id"], [KAI])
        self.assertEqual(self.val("who.ignore", S1), [BOB, SPAM])

    def test_change_list_reverts_to_the_owners(self):
        r = self.admin(f"mdset:2:val:s{G1}:r:when-addressed:q")
        self.assertIn("Undo goes back to the owners' setting: Chatty",
                      text(r))
        undo = [c for c in H.controls(r)
                if c["custom_id"] == f"mdset:2:val:s{G1}:r:=:q"]
        self.assertEqual(len(undo), 1)
        r = self.admin(undo[0]["custom_id"])
        self.assertEqual(self.val("who.replies", S1), "conversational")

    def test_why_for_admins(self):
        r = self.admin(f"mdset:2:pg:c{G1}.{MUSECHAT}:y")
        self.assertIn("## Why no reply? · #muse-chat", text(r))
        # spam1 was added by an admin: an admin may remove them
        self.assertIsNotNone(by_label(r, "Remove spam1"))
        self.assertIsNone(by_label(r, "Change cap"))
        self.assertTrue(by_label(r, "Back to #muse-chat")[
            "custom_id"].endswith(":q"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
