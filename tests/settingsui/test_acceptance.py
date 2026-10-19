"""/settings acceptance: the task walkthroughs of the redesign spec (§7),
re-run click by click. Each step finds its control on the page the
person is looking at, by its visible label (so the click targets are
asserted too), and clicks are counted the spec's way: /settings 1, a
button 1, a select 2, a user select 3, a modal 2 (the button that
opens it counts 1), the context command 2 (right-click, Apps).

Usage: [MUT_DIR=<tree>] python3 tests/settingsui/test_acceptance.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402
from harness import (ADMIN, ART, G1, G2, G3, KAI, MANAGE, MS,  # noqa: E402
                     MUSECHAT, OWNER, SAM, UI, FakeCtx, text)

S1 = MS.Place.server(G1)
MC = MS.Place.channel(G1, MUSECHAT)
DMS = MS.Place.dms()
DEF = MS.Place.defaults()


class Player:
    """One person clicking through /settings."""

    def __init__(self, tc, ctx, user, guild=None, perms=None,
                 channel=MUSECHAT):
        self.tc, self.ctx = tc, ctx
        self.user, self.guild, self.perms, self.channel = \
            user, guild, perms, channel
        self.clicks = 0
        self.page = None

    def _answer(self, r):
        self.tc.assertNotIn(UI.OUT_OF_DATE, text(r))
        self.tc.assertNotIn(UI.APOLOGY, text(r), self.ctx.logs[-1:])
        self.page = r
        return r

    def settings(self):
        self.clicks += 1
        self.ctx.refresh()
        return self._answer(UI.handle(H.command(
            self.user, self.guild, self.perms, self.channel), self.ctx))

    def context(self, message_id, author):
        self.clicks += 2
        self.ctx.refresh()
        return self._answer(UI.handle(H.context_command(
            self.user, message_id, self.guild, self.perms, self.channel,
            author), self.ctx))

    def _click(self, cid, values=None, resolved=None):
        self.ctx.refresh()
        return self._answer(UI.handle(H.component(
            self.user, cid, values, self.guild, self.perms, self.channel,
            resolved, {"id": "9", "components": (self.page or {}).get(
                "data", {}).get("components")}), self.ctx))

    def press(self, label):
        b = H.by_label(self.page, label)
        self.tc.assertIsNotNone(b, f"no [{label}] on: {text(self.page)}")
        self.tc.assertEqual(b["type"], 2)
        self.clicks += 1
        return self._click(b["custom_id"])

    def press_in(self, section_words, label):
        """The button beside the section that says section_words."""
        for c in H.walk(self.page["data"].get("components")):
            if c.get("type") == 9 and section_words in c["components"][0][
                    "content"] and c["accessory"].get("label") == label:
                self.clicks += 1
                return self._click(c["accessory"]["custom_id"])
        self.tc.fail(f"no [{label}] beside {section_words!r}")

    def choose(self, prefix, option_label):
        s = H.find(self.page, prefix)
        self.tc.assertIsNotNone(s, f"no {prefix} select")
        o = H.option(s, label=option_label)
        self.tc.assertIsNotNone(o, (option_label, [x["label"] for x in
                                                   s["options"]]))
        self.clicks += 2
        return self._click(s["custom_id"], [o["value"]])

    def pick(self, prefix, ids):
        s = H.find(self.page, prefix)
        self.tc.assertIsNotNone(s, f"no {prefix} select")
        self.tc.assertIn(s["type"], (5, 7, 8))
        self.clicks += 2 if s["type"] == 8 else 3
        return self._click(s["custom_id"], ids,
                           H.resolved_for(s["type"], ids))

    def fill(self, fields):
        self.tc.assertEqual(self.page["type"], 9, text(self.page))
        names = {c["component"]["custom_id"]
                 for c in self.page["data"]["components"] if c["type"] == 18}
        self.tc.assertTrue(set(fields) <= names, (fields, names))
        self.clicks += 2
        self.ctx.refresh()
        return self._answer(UI.handle(H.submit(
            self.user, self.page["data"]["custom_id"], fields, self.guild,
            self.perms, self.channel), self.ctx))


class Tasks(unittest.TestCase):
    def setUp(self):
        H.install_determinism()
        self.ctx = FakeCtx()

    def owner(self, guild=G1, channel=MUSECHAT):
        return Player(self, self.ctx, OWNER, guild, "0" if guild else None,
                      channel)

    def val(self, key, place):
        return self.ctx.value(key, place)

    def test_1a_mentions_only_in_this_channel(self):
        p = self.owner()
        p.settings()
        p.choose(f"mdset:2:set:c{G1}.{MUSECHAT}:r", "Only when @mentioned")
        self.assertEqual(p.clicks, 3)
        self.assertEqual(self.val("who.replies", MC), "when-addressed")

    def test_1b_same_from_a_dm(self):
        p = self.owner(None)
        p.settings()
        p.choose("mdset:2:go:d", "Muse Lounge")
        p.choose(f"mdset:2:gc:s{G1}", "#muse-chat")
        p.choose(f"mdset:2:set:c{G1}.{MUSECHAT}:r", "Only when @mentioned")
        self.assertEqual(p.clicks, 7)
        self.assertEqual(self.val("who.replies", MC), "when-addressed")

    def test_2a_let_sam_dm_it_from_a_dm(self):
        p = self.owner(None)
        p.settings()
        p.press("Add by ID")
        p.fill({"ids": SAM})
        self.assertEqual(p.clicks, 4)
        self.assertIn("**Done:**", text(p.page))
        self.assertNotIn("Check before", text(p.page))  # no false warning
        self.assertEqual(self.val("who.dms_from", DMS), [SAM])
        self.assertEqual(MS.dm_access(self.ctx.reload()), "list")

    def test_2a_with_a_searching_dm_user_select(self):
        UI.DM_USER_SELECT_SEARCHES = True
        self.addCleanup(setattr, UI, "DM_USER_SELECT_SEARCHES", False)
        p = self.owner(None)
        p.settings()
        p.pick("mdset:2:ppl:d:df", [SAM])
        self.assertEqual(p.clicks, 4)
        self.assertEqual(self.val("who.dms_from", DMS), [SAM])

    def test_2b_defaults_only_answer_sam_must_confirm(self):
        p = self.owner(None)
        p.settings()
        p.choose("mdset:2:go:d", "Defaults")
        p.press("More settings")
        p.pick("mdset:2:ppl:x:al", [SAM])
        t = text(p.page)
        self.assertIn("## Check before applying", t)
        self.assertRegex(t, r"will get answers in \d+ places")
        p.press("Cancel")
        self.assertEqual(self.val("who.allow", DEF), "everyone")
        self.assertIn("› More settings", text(p.page))

    def test_3_pixel_plaza_memory(self):
        p = self.owner(G3, ART)
        p.settings()
        p.press("Pixel Plaza")
        p.choose(f"mdset:2:set:s{G3}:m", "Nothing personal")
        self.assertEqual(p.clicks, 4)
        self.assertEqual(self.val("privacy.memory", MS.Place.server(G3)),
                         "nothing")

    def test_4_welcome_on(self):
        p = self.owner()
        p.settings()
        p.press("Muse Lounge")
        p.press("Welcome: Off · Turn on")
        self.assertEqual(p.clicks, 3)
        self.assertIn("Welcomes go to: The server's system channel, "
                      "#welcome.", text(p.page))
        self.assertIs(self.val("plugins.welcome.enabled", S1), True)
        # and a server with no system channel says so
        q = self.owner(G2, H.HOMEWORK)
        q.settings()
        q.press("Study Group")
        q.press("Welcome: Off · Turn on")
        self.assertIn("no system channel, so welcomes are skipped",
                      text(q.page))

    def test_5_cap_replies_per_day_from_a_dm(self):
        p = self.owner(None)
        p.settings()
        p.choose("mdset:2:go:d", "Defaults")
        p.press_in("Replies per person per day", "Change")
        for c in p.page["data"]["components"]:
            if c["type"] == 18:
                self.assertNotIn("value", c["component"])   # blank prefill
        p.fill({"pp": "", "pd": "200"})
        self.assertEqual(p.clicks, 6)
        self.assertEqual(self.val("limits.per_day", DEF), 200)

    def _sam_ignored(self):
        MS.apply(self.ctx.cfg, [MS.Change(S1, "who.ignore",
                                          [H.BOB, H.SPAM, SAM])],
                 OWNER, plugins_dir=H.PLUGINS, role="owner")
        o = dict(H.OUTCOMES[0], author_id=SAM, author="sam", at=H.NOW - 30,
                 message_id=H.sf(H.NOW - 30, 99))
        self.ctx.outcomes.append(o)
        return o

    def test_6_why_no_answer_to_sam(self):
        self._sam_ignored()
        p = self.owner()
        p.settings()
        p.press("Why no reply?")
        p.press("Remove Sam")
        self.assertEqual(p.clicks, 3)
        self.assertNotIn(SAM, self.val("who.ignore", S1))
        self.assertIn("Fixed.", text(p.page))

    def test_n2_context_command_with_fix(self):
        o = self._sam_ignored()
        p = self.owner()
        p.context(o["message_id"], SAM)
        p.press("Remove Sam")
        self.assertEqual(p.clicks, 3)
        self.assertNotIn(SAM, self.val("who.ignore", S1))

    def test_7a_undo_a_channel_change_from_the_server_page(self):
        self.owner().settings()
        p = self.owner()
        p.settings()
        p.choose(f"mdset:2:set:c{G1}.{MUSECHAT}:r", "Off")
        q = self.owner(None)
        q.settings()
        q.choose("mdset:2:go:d", "Muse Lounge")
        self.assertIn("#muse-chat, When it answers: Chatty → Off",
                      text(q.page))
        q.press("Undo")
        # +1: assess flags Chatty in a server over 500 members
        self.assertIn("Check before undoing", text(q.page))
        q.press("Undo")
        self.assertEqual(q.clicks, 4 + 1)
        self.assertEqual(self.val("who.replies", MC), "conversational")

    def test_7b_pause_everywhere_misclick(self):
        p = self.owner()
        p.settings()
        p.press("Defaults")
        p.press("Pause everywhere")
        self.assertIn("Check before applying", text(p.page))
        self.assertFalse(MS.is_paused(self.ctx.reload(), DEF))  # 0 harm
        p.press("Apply")
        self.assertTrue(MS.is_paused(self.ctx.reload(), DEF))
        n = p.clicks
        p.press("Undo")
        # Chatty again in Pixel Plaza (48,213 members): assess confirms
        if "Check before undoing" in text(p.page):
            p.press("Undo")
        self.assertFalse(MS.is_paused(self.ctx.reload(), DEF))
        self.assertLessEqual(p.clicks - n, 2)

    def admin(self, channel=MUSECHAT):
        return Player(self, self.ctx, ADMIN, G1, MANAGE, channel)

    def test_8a_admin_mutes_a_channel(self):
        p = self.admin()
        p.settings()
        p.press("Off in #muse-chat")
        self.assertEqual(p.clicks, 2)
        self.assertEqual(self.val("who.replies", MC), "off")

    def test_8b_admin_pauses_the_server_by_mistake(self):
        p = self.admin()
        p.settings()
        p.press("Off in all of Muse Lounge")
        self.assertEqual(self.val("who.replies", S1), "off")
        p.press("Undo")
        self.assertEqual(p.clicks, 3)
        self.assertEqual(self.val("who.replies", S1), "conversational")

    def test_8c_admin_wrong_pick_in_never_answer(self):
        p = self.admin()
        p.settings()
        p.pick(f"mdset:2:ppl:s{G1}:ig", [KAI])
        self.assertIn(KAI, self.val("who.ignore", S1))
        p.press("Undo")
        self.assertEqual(p.clicks, 5)
        self.assertNotIn(KAI, self.val("who.ignore", S1))
        # or the Remove select
        q = self.admin()
        q.settings()
        q.pick(f"mdset:2:ppl:s{G1}:ig", [KAI])
        q.choose(f"mdset:2:rm:s{G1}:ig", "Kai")
        self.assertNotIn(KAI, self.val("who.ignore", S1))

    def test_n1_community_preset_and_one_undo(self):
        p = self.owner()
        p.settings()
        p.press("Muse Lounge")
        p.choose(f"mdset:2:pre:s{G1}", "Community")
        p.press("Apply Community")
        self.assertEqual(p.clicks, 5)
        self.assertEqual(MS.matching_preset(self.ctx.reload(), S1),
                         "Community")
        n = p.clicks
        p.press("Undo")
        if "Check before undoing" in text(p.page):
            p.press("Undo")     # Chatty again in a 1,240-member server
        self.assertLessEqual(p.clicks - n, 2)
        self.assertIsNone(MS.matching_preset(self.ctx.reload(), S1))
        self.assertIsNone(self.val("limits.per_day", S1))


if __name__ == "__main__":
    unittest.main(verbosity=1)
