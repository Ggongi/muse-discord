"""Regression tests for the adversarial review of the /settings redesign
(review B, 2026-10-19): each finding's proof of concept, now refused or
handled. The monitor-side ones are in tests/settingsplumb (UIData,
AdminDigest); the hostile-fixture crawl is test_crawl_hostile.py.

Usage: [MUT_DIR=<tree>] python3 tests/settingsui/test_review.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness as H  # noqa: E402
from harness import (ADMIN, G1, G2, G3, MANAGE, MS, OWNER, SAM,  # noqa: E402
                     UI, FakeCtx, by_label, component, context_command,
                     text)

UNMAPPED = "70000000000000000999"


def apply(ctx, changes):
    MS.apply(ctx.cfg, changes, OWNER, plugins_dir=H.PLUGINS, role="owner")
    ctx.refresh()


class Base(unittest.TestCase):
    def setUp(self):
        H.install_determinism()
        self.ctx = FakeCtx()

    def h(self, ix):
        self.ctx.refresh()
        r = UI.handle(ix, self.ctx)
        self.assertNotIn(UI.APOLOGY, text(r), self.ctx.logs[-1:])
        return r

    def buttons(self, r):
        return [c for c in H.walk(r["data"]["components"])
                if c.get("type") == 2]


class WhyScope(Base):
    """#1: an entry for an unmapped channel (guild_id None) or another
    server must not be explained with DM or that server's settings."""

    def thread_ix(self, user, mid, author, perms="0"):
        ix = context_command(user, mid, guild=G1, perms=perms,
                             channel=UNMAPPED, author=author)
        ix["channel"] = {"id": UNMAPPED, "type": 11,
                         "parent_id": H.MUSECHAT}
        return ix

    def test_unmapped_thread_uses_the_interactions_server(self):
        apply(self.ctx, [MS.Change(MS.Place.dms(), "who.ignore", [SAM])])
        o = H._outcome(40, 5, "not-allowed", channel=UNMAPPED, guild=None,
                       author=SAM)
        self.ctx.outcomes.append(o)
        r = self.h(self.thread_ix(ADMIN, o["message_id"], SAM, MANAGE))
        self.assertNotIn("in DMs", text(r))
        self.assertIn("wasn't allowed then", text(r))
        # Owners: a replies-off cause is looked up in #muse-chat, so the
        # fix never targets DMs.
        apply(self.ctx, [MS.Change(MS.Place.dms(), "who.replies", "off")])
        o2 = H._outcome(41, 5, "replies-off", channel=UNMAPPED, guild=None,
                        author=H.KAI)
        self.ctx.outcomes.append(o2)
        r = self.h(self.thread_ix(OWNER, o2["message_id"], H.KAI))
        self.assertNotIn("in DMs", text(r))
        self.assertIn("When it answers was Off in #muse-chat", text(r))
        self.assertEqual([b for b in self.buttons(r)
                          if ":fx:d:" in b["custom_id"]], [])

    def test_another_servers_entry_is_not_recorded(self):
        ctx = FakeCtx(text=H.BASE.replace(
            "servers:\n", f'servers:\n  "{G3}":\n    name: Pixel Plaza\n'
            f'    who:\n      allow: ["{SAM}"]\n', 1))
        tid = H.OUTCOMES[16]["message_id"]      # Kai's, in Pixel Plaza
        r = UI.handle(context_command(ADMIN, tid, guild=G1, perms=MANAGE,
                                      author=H.KAI), ctx)
        self.assertIn("Not recorded", text(r))
        self.assertNotIn("Pixel Plaza", text(r))
        # a server message's id seen from a DM: not recorded either
        r = UI.handle(context_command(OWNER, tid, guild=None,
                                      author=H.KAI), ctx)
        self.assertIn("Not recorded", text(r))

    def test_why_page_skips_other_servers(self):
        o = dict(H.OUTCOMES[0], guild_id=G3, message_id="1999")
        self.ctx.outcomes = [o]
        self.ctx.recent_outcomes = lambda place, limit=20: [dict(o)]
        r = self.h(component(OWNER, f"mdset:2:pg:s{G1}:y", None, G1, "0"))
        self.assertIn("Nothing was refused here recently.", text(r))


class People(Base):
    def test_foreign_role_is_never_written(self):
        """#3: a role of the server the interaction is in, picked for
        another server."""
        r = self.h(component(OWNER, f"mdset:2:pg:s{G2}:m", None, G1, "0"))
        sel = H.find(r, f"mdset:2:ppl:s{G2}:al")
        self.assertEqual(sel["type"], 5)        # a user select off-guild
        r = self.h(component(OWNER, f"mdset:2:ppl:s{G2}:al", [H.ROLE], G1,
                             "0", resolved=H.resolved_for(7, [H.ROLE])))
        self.assertIn("Roles only count in their own server", text(r))
        self.assertEqual(self.ctx.value("who.allow", MS.Place.server(G2)),
                         "everyone")
        # a person picked with it is written, the role left out
        r = self.h(component(OWNER, f"mdset:2:ppl:s{G2}:ig", [H.ROLE, SAM],
                             G1, "0", resolved=H.resolved_for(
                                 7, [H.ROLE, SAM])))
        self.assertEqual(self.ctx.value("who.ignore", MS.Place.server(G2)),
                         [SAM])

    def test_roles_kept_when_editing_off_guild(self):
        apply(self.ctx, [MS.Change(MS.Place.server(G2), "who.ignore",
                                   [f"role:{H.ROLE}"])])
        self.h(component(OWNER, f"mdset:2:ppl:s{G2}:ig", [SAM], G1, "0",
                         resolved=H.resolved_for(5, [SAM])))
        self.assertEqual(self.ctx.value("who.ignore", MS.Place.server(G2)),
                         [SAM, f"role:{H.ROLE}"])

    def test_forged_ids_are_dropped(self):
        """#6: ids no resolved data vouches for (another server's, a
        channel's) are never written, and never named."""
        before = self.ctx.overlay()
        r = self.h(component(ADMIN, f"mdset:2:ppl:s{G1}:ig", [G2, H.ART],
                             G1, MANAGE))
        self.assertEqual(self.ctx.overlay(), before)
        self.assertNotIn("Study Group", text(r))
        r = self.h(component(OWNER, f"mdset:2:ppl:s{G1}:al", [G2], G1,
                             "0"))
        self.assertEqual(self.ctx.overlay(), before)
        # a person's name only comes from people lookups
        R = UI.Req(component(OWNER, "mdset:2:pg:x:b"), self.ctx)
        self.assertIsNone(R.person(G2))
        self.assertEqual(R.person(SAM), "Sam")

    def test_admin_entry_holds_only_admin_additions(self):
        """#5: an admin's add copies nothing the owners set elsewhere."""
        apply(self.ctx, [MS.Change(MS.Place.defaults(), "who.ignore",
                                   [H.KAI])])
        self.h(component(ADMIN, f"mdset:2:ppl:s{G2}:ig", [SAM], G2, MANAGE,
                         H.HOMEWORK, resolved=H.resolved_for(5, [SAM])))
        e = self.ctx.overlay()["values"][f'servers."{G2}".who.ignore']
        self.assertEqual(e["value"], [SAM])
        self.assertEqual(self.ctx.value("who.ignore", MS.Place.server(G2)),
                         [SAM, H.KAI])
        apply(self.ctx, [MS.Change(MS.Place.defaults(), "who.ignore", [])])
        self.assertEqual(self.ctx.value("who.ignore", MS.Place.server(G2)),
                         [SAM])
        apply(self.ctx, [MS.Change(MS.Place.defaults(), "who.ignore",
                                   [H.KAI])])
        r = self.h(H.command(ADMIN, G2, MANAGE, H.HOMEWORK))
        self.assertIn(f"<@{H.KAI}> (set by the owners)", text(r))
        self.assertIn(f"<@{SAM}> (added by", text(r))
        rm = H.find(r, f"mdset:2:rm:s{G2}:ig")
        self.assertEqual([o["value"] for o in rm["options"]], [SAM])
        # removing it leaves the owners' list alone
        self.h(component(ADMIN, rm["custom_id"], [SAM], G2, MANAGE,
                         H.HOMEWORK))
        self.assertEqual(self.ctx.value("who.ignore", MS.Place.server(G2)),
                         [H.KAI])
        # the owners' entry at the server layer stays in an admin's add
        r = self.h(component(ADMIN, f"mdset:2:ppl:s{G1}:ig", [SAM], G1,
                             MANAGE, resolved=H.resolved_for(5, [SAM])))
        self.assertEqual(self.ctx.value("who.ignore", MS.Place.server(G1)),
                         [H.BOB, H.SPAM, SAM])
        self.assertIsNotNone(H.find(r, f"mdset:2:rm:s{G1}:ig"))


class Markup(Base):
    def test_names_are_escaped_in_text_plain_in_labels(self):
        """#4: names with markdown, links and mentions."""
        tid = H.OUTCOMES[0]["message_id"]
        ix = context_command(OWNER, tid, author=H.SPAM)
        ix["data"]["resolved"]["messages"][tid]["member"] = {
            "nick": "[Fix it here](https://evil.example) @everyone"}
        r = self.h(ix)
        t = text(r)
        self.assertNotIn("[Fix it here](https://evil.example)", t)
        self.assertIn(r"\[Fix it here\]\(https://evil.example\)", t)
        self.assertNotIn("@everyone", t)
        self.assertIn("Remove [Fix it here](https://evil.example) @everyone",
                      [b["label"] for b in self.buttons(r)])
        self.assertEqual(r["data"]["allowed_mentions"], {"parse": []})
        guilds = [dict(g) for g in H.GUILDS]
        guilds[0]["name"] = ("# Muse [verify](https://evil.example) "
                             "<@&55500000000000000001>")
        ctx = FakeCtx(guilds=guilds)
        r = UI.handle(component(OWNER, f"mdset:2:pg:s{G1}:b", None, G1, "0"),
                      ctx)
        t = text(r)
        self.assertIn(r"## \# Muse \[verify\]\(https://evil.example\) <" +
                      UI.ZWSP + "@&", t)
        self.assertNotIn("<@&", t.replace("<" + UI.ZWSP, ""))
        r = UI.handle(component(OWNER, "mdset:2:pg:x:b", None, G1, "0"),
                      ctx)
        go = [b["label"] for b in self.buttons(r)]
        self.assertTrue(all("\\" not in x and UI.ZWSP not in x for x in go))


class Copy(Base):
    def test_admins_get_neutral_copy(self):
        """#8: admins never read about the owners' review DMs or the
        never-share phrases."""
        for code, owner_words in (("quarantined", "look for the bot's DM"),
                                  ("never-share", "never share")):
            o = H._outcome(50, 3, code, author=SAM)
            for user, perms in ((ADMIN, MANAGE), (OWNER, "0")):
                R = UI.Req(component(user, "mdset:2:pg:x:b", None, G1,
                                     perms), self.ctx)
                words = UI.explain_outcome(R, o)[0]
                if user == ADMIN:
                    self.assertNotIn(owner_words, words)
                    self.assertNotIn("never", words.lower().replace(
                        "never answer", ""))
                else:
                    self.assertIn(owner_words, words)

    def test_admin_undo_banner_has_no_channel_note(self):
        r = self.h(component(ADMIN, f"mdset:2:val:c{G1}.{H.MUSECHAT}:r:off:q",
                             None, G1, MANAGE, H.MUSECHAT))
        r = self.h(component(ADMIN, by_label(r, "Undo")["custom_id"], None,
                             G1, MANAGE, H.MUSECHAT))
        self.assertIn("**Undone:**", text(r))
        self.assertNotIn("this includes a change in a channel", text(r))

    def test_negative_cap(self):
        r = UI.handle(H.submit(OWNER, f"mdset:2:sub:s{G1}:c:b",
                               {"pp": "-5", "pd": ""}, G1, "0"), self.ctx)
        self.assertIn("“-5” can't be negative. Replies per person per day "
                      "takes 0 to 10,000", text(r))

    def test_replied_has_no_dead_link(self):
        tid = H.OUTCOMES[13]["message_id"]
        r = self.h(context_command(OWNER, tid, author=H.KAI))
        self.assertIn("It replied.", text(r))
        self.assertNotIn("[reply]", text(r))


class Budget(Base):
    def test_long_options_fit(self):
        """#7: options are shortened and descriptions dropped before a
        page goes over the text budget."""
        comps = [UI.container([UI.td("x" * 2000)]),
                 UI.row(UI.sel("mdset:2:go:x", [
                     UI.opt("n" * 100, f"s{i}", "d" * 100)
                     for i in range(25)], "Go to…"))]
        UI.finish_text(comps)
        self.assertLessEqual(UI.text_size(comps), UI.TEXT_BUDGET)
        opts = comps[1]["components"][0]["options"]
        self.assertLessEqual(max(len(o["label"]) for o in opts), 60)


if __name__ == "__main__":
    unittest.main(verbosity=1)
