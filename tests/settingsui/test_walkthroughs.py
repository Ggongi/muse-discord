"""/settings behaviour, click by click, offline: an owner's change
applies and can be undone (a stale Undo never applies, a group's Undo is
judged by its whole outcome), Pause everywhere
asks first and undoes, a server admin can only make things quieter and
undo their own change, a stranger is refused, bad input and an unreadable
overlay write nothing, and a crash is an apology. Controls are found by
their custom_id (verb and place), never by their label or the page's
words, so the redesign can change any copy.

Usage: [MUT_DIR=<tree>] python3 tests/settingsui/test_walkthroughs.py"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crawl  # noqa: E402
import harness as H  # noqa: E402
from harness import (ADMIN, G1, GENERAL, MANAGE, MEMBER, MS,  # noqa: E402
                     MUSECHAT, OWNER, UI, FakeCtx, command, component,
                     context_command, submit)

S1 = MS.Place.server(G1)
MC = MS.Place.channel(G1, MUSECHAT)
DEF = MS.Place.defaults()
DMS = MS.Place.dms()


def verb(c):
    return c["custom_id"].split(":")[2]


def control(resp, which):
    """The first control whose verb is which (or whose custom_id starts
    with which, when it has a colon)."""
    for c in H.controls(resp):
        if (":" in which and c["custom_id"].startswith(which)) \
                or (":" not in which and verb(c) == which):
            return c
    return None


class Base(unittest.TestCase):
    def setUp(self):
        H.install_determinism()
        self.ctx = FakeCtx()

    def h(self, ix):
        self.ctx.refresh()
        r = UI.handle(ix, self.ctx)
        self.assertFalse(crawl.crashed(self.ctx), self.ctx.logs[-1:])
        self.assertEqual(crawl.check(r, "response"), [])
        return r

    def owner(self, cid, values=None, guild=G1, **kw):
        return self.h(component(OWNER, cid, values, guild,
                                "0" if guild else None, **kw))

    def admin(self, cid, values=None, **kw):
        return self.h(component(ADMIN, cid, values, G1, MANAGE, MUSECHAT,
                                **kw))

    def confirm_if_asked(self, r, who=None):
        """A risky outcome asks first: press its confirmation."""
        ok = control(r, "ok")
        if ok is not None:
            r = (who or self.owner)(ok["custom_id"])
        return r

    def val(self, key, place):
        return self.ctx.value(key, place)


class Owner(Base):
    def test_change_applies_and_undo_reverts(self):
        r = self.h(command(OWNER, G1, "0", MUSECHAT))
        sel = control(r, f"mdset:2:set:c{G1}.{MUSECHAT}:r")
        self.assertIsNotNone(sel)
        r = self.owner(sel["custom_id"], ["when-addressed"])
        self.assertEqual(self.val("who.replies", MC), "when-addressed")
        undo = control(r, "un")
        r = self.confirm_if_asked(self.owner(undo["custom_id"]))
        self.assertEqual(self.val("who.replies", MC), "conversational")
        # A stale Undo (the setting changed since) never applies.
        r = self.owner(sel["custom_id"], ["off"])
        stale = control(r, "un")["custom_id"]
        self.owner(sel["custom_id"], ["when-addressed"])
        self.owner(stale)
        self.assertEqual(self.val("who.replies", MC), "when-addressed")

    def test_undo_of_a_group_is_judged_whole(self):
        """Undo on a server page undoes the newest group there, all of it:
        when that would open DMs to anyone again it asks first."""
        def apply(*changes):
            MS.apply(self.ctx.cfg, list(changes), OWNER,
                     plugins_dir=H.PLUGINS, role="owner")
        apply(MS.Change(DMS, "who.dms_from", "anyone"))
        apply(MS.Change(DMS, "who.dms_from", "owners"),
              MS.Change(S1, "style.length", "brief"))
        r = self.owner(f"mdset:2:un:s{G1}")
        self.assertEqual(self.val("who.dms_from", DMS), "owners")  # asked
        self.owner(control(r, "ok")["custom_id"])
        self.assertEqual(self.val("who.dms_from", DMS), "anyone")
        self.assertEqual(self.val("style.length", S1), "normal")

    def test_pause_everywhere_asks_first_and_undoes(self):
        r = self.owner("mdset:2:pz:x")
        self.assertFalse(MS.is_paused(self.ctx.reload(), DEF))  # asked
        self.assertEqual(self.ctx.applied, [])
        ok = control(r, "ok")["custom_id"]
        # An expired confirmation (the stash is gone) changes nothing.
        stash = dict(self.ctx.stash)
        self.ctx.stash.clear()
        self.owner(ok)
        self.assertEqual(self.ctx.applied, [])
        self.ctx.stash.update(stash)
        r = self.owner(ok)
        self.assertTrue(MS.is_paused(self.ctx.reload(), DEF))
        self.confirm_if_asked(self.owner(control(r, "un")["custom_id"]))
        self.assertFalse(MS.is_paused(self.ctx.reload(), DEF))

    def test_bad_input_and_unreadable_overlay_write_nothing(self):
        r = self.owner(f"mdset:2:mod:s{G1}:c:b")
        self.assertEqual(r["type"], 9)
        for fields in ({"pp": "lots", "pd": "5"}, {"pp": "", "pd": "-5"},
                       {"pp": "", "pd": "100001"}):
            self.h(submit(OWNER, f"mdset:2:sub:s{G1}:c:b", fields, G1, "0"))
            self.assertIsNone(self.val("limits.per_day", S1), fields)
            self.assertIsNone(self.val("limits.per_person_per_day", S1))
        self.h(submit(OWNER, f"mdset:2:sub:s{G1}:c:b",
                      {"pp": "1,000", "pd": ""}, G1, "0"))
        self.assertEqual(self.val("limits.per_person_per_day", S1), 1000)
        # An unreadable overlay: no write controls, and a stale page's
        # write changes nothing.
        self.ctx = FakeCtx(broken_overlay=True)
        with open(self.ctx.overlay_path) as f:
            before = f.read()
        r = self.owner(f"mdset:2:pg:s{G1}:b")
        self.assertEqual({verb(c) for c in H.controls(r)}
                         & set(UI.WRITE_VERBS), set())
        self.owner(f"mdset:2:set:s{G1}:m", ["nothing"])   # a stale page
        with open(self.ctx.overlay_path) as f:
            self.assertEqual(f.read(), before)

    def test_crash_is_an_ephemeral_apology(self):
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
        self.assertEqual(r["type"], 4)
        self.assertTrue(r["data"]["flags"] & 64)
        self.assertNotIn("boom", str(r))            # no internals shown
        self.assertTrue(crawl.crashed(ctx))         # but logged


class Admin(Base):
    def test_quieter_only_and_undo(self):
        r = self.h(command(ADMIN, G1, MANAGE, MUSECHAT))
        # Every write the panel offers makes things quieter: none sets
        # Chatty or turns an extra on.
        for c in H.controls(r):
            self.assertNotIn(":conversational:", c["custom_id"])
            self.assertNotIn(":on:", c["custom_id"])
        before = self.ctx.overlay()
        self.admin(f"mdset:2:val:c{G1}.{MUSECHAT}:r:conversational:q")
        self.assertEqual(self.ctx.overlay(), before)        # refused
        r = self.admin(f"mdset:2:val:c{G1}.{MUSECHAT}:r:off:q")
        self.assertEqual(self.val("who.replies", MC), "off")
        key = f'servers."{G1}".channels."{MUSECHAT}".replies'
        self.assertEqual(self.ctx.overlay()["values"][key]["role"], "admin")
        self.assertEqual(self.ctx.notified[-1][:3], (G1, ADMIN, key))
        self.admin(control(r, "un")["custom_id"])
        self.assertEqual(self.val("who.replies", MC), "conversational")


class Stranger(Base):
    def test_member_is_refused_everywhere(self):
        for ix in (command(MEMBER, G1, "0"), command(MEMBER),
                   command(ADMIN),                   # an admin in a DM
                   context_command(MEMBER, H.OUTCOMES[1]["message_id"],
                                   perms="0"),
                   component(MEMBER, f"mdset:open:server:{G1}")):
            r = self.h(ix)
            self.assertEqual(r["type"], 4)
            self.assertEqual(H.controls(r), [])
        self.assertEqual(self.ctx.applied, [])
        r = self.h(command(ADMIN, G1, MANAGE, GENERAL))   # an admin: panel
        self.assertTrue(H.controls(r))


if __name__ == "__main__":
    unittest.main()
