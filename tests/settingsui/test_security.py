"""/settings security, offline: forged custom ids from server admins,
members and other owners, stash tokens used by the wrong person, old
and malformed ids, and a fuzz over every id the owner's pages render.
Nothing here may write, crash, or loosen anything. Refusals are checked
by what did not happen (no write, an ephemeral answer), never by their
wording.

Usage: [MUT_DIR=<tree>] python3 tests/settingsui/test_security.py"""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import crawl  # noqa: E402
import harness as H  # noqa: E402
from harness import (ADMIN, BOB, G1, G2, G3, GENERAL, KAI, MANAGE,  # noqa
                     MEMBER, MS, MUSECHAT, OWNER, OWNER2, SAM, SPAM, UI,
                     FakeCtx, command, component, submit)

S1 = MS.Place.server(G1)
MC = MS.Place.channel(G1, MUSECHAT)


class Base(unittest.TestCase):
    def setUp(self):
        H.install_determinism()
        self.ctx = FakeCtx()
        self.before = self.ctx.overlay()

    def h(self, ix):
        self.ctx.refresh()
        r = UI.handle(ix, self.ctx)
        self.assertFalse(crawl.crashed(self.ctx), self.ctx.logs[-1:])
        return r

    def admin(self, cid, values=None, guild=G1, **kw):
        return self.h(component(ADMIN, cid, values, guild, MANAGE, GENERAL,
                                **kw))

    def unchanged(self):
        self.assertEqual(self.ctx.overlay(), self.before)

    def assertAdminRefused(self, r):
        self.assertEqual(r["type"], 4, r)       # an ephemeral notice
        self.assertEqual(self.ctx.overlay(), self.before)


class AdminForgery(Base):
    def test_loosening_is_refused(self):
        # The owners make Muse Lounge quieter; an admin quiets #muse-chat.
        self.h(component(OWNER, f"mdset:2:set:s{G1}:r", ["when-addressed"],
                         G1, "0"))
        self.admin(f"mdset:2:val:c{G1}.{MUSECHAT}:r:off:q")
        self.before = self.ctx.overlay()
        for cid, values in (
                (f"mdset:2:val:s{G1}:r:conversational:q", None),
                (f"mdset:2:val:c{G1}.{MUSECHAT}:r:conversational:q", None),
                (f"mdset:2:set:s{G1}:r", ["conversational"]),
                (f"mdset:2:val:s{G1}:~media.enabled:on:q", None),
                (f"mdset:2:set:s{G1}:m", ["everything"]),
                (f"mdset:2:val:s{G1}:pd:=:q", None),
                (f"mdset:2:val:s{G1}:al:everyone:q", None),
                (f"mdset:2:val:s{G1}:b:when-addressed:q", None),
                (f"mdset:2:rs:s{G1}", None)):
            self.assertAdminRefused(self.admin(cid, values))
        self.unchanged()

    def test_other_servers_and_places(self):
        for cid in (f"mdset:2:val:s{G3}:r:off:q",
                    f"mdset:2:val:c{G3}.{H.ART}:r:off:q",
                    f"mdset:2:pg:s{G3}:q", "mdset:2:pg:x:b", "mdset:2:pg:d:b",
                    "mdset:2:val:x:r:off:q", "mdset:2:val:d:r:off:q",
                    f"mdset:2:pg:s{G1}:m", f"mdset:2:pg:s{G1}:e",
                    f"mdset:2:val:c{G1}.71999999999999999999:r:off:q",
                    f"mdset:2:un:s{G3}", "mdset:2:un:x"):
            self.assertAdminRefused(self.admin(cid))
        # their own server's panel from another server: refused too
        self.assertAdminRefused(self.admin(f"mdset:2:pg:s{G1}:q", guild=G2))
        self.unchanged()

    def test_undoing_an_owner_entry_is_refused(self):
        # The owners set Chatty in Muse Lounge (seeded); an admin's reset
        # there would drop the owners' setting.
        self.assertAdminRefused(self.admin(f"mdset:2:val:s{G1}:r:=:q"))
        # Bob is on Never answer by the owners: removing him is refused
        self.assertAdminRefused(self.admin(f"mdset:2:rm:s{G1}:ig", [BOB]))
        self.assertAdminRefused(self.admin(f"mdset:2:fx:s{G1}:ig:-{BOB}"))
        # The admin's Undo never reaches an owner's change
        self.h(component(OWNER, f"mdset:2:set:s{G1}:m", ["nothing"], G1,
                         "0"))
        self.admin(f"mdset:2:un:s{G1}")         # their own: spam1
        self.assertEqual(self.ctx.value("who.ignore", S1), [BOB])
        self.assertEqual(self.ctx.value("privacy.memory", S1), "nothing")
        self.admin(f"mdset:2:un:s{G1}")         # nothing of theirs left
        self.assertEqual(self.ctx.value("privacy.memory", S1), "nothing")

    def test_owner_only_verbs(self):
        for cid, values in ((f"mdset:2:pre:s{G1}", ["community"]),
                            (f"mdset:2:mod:s{G1}:c:b", None),
                            (f"mdset:2:mod:c{G1}.{MUSECHAT}:i:b", None),
                            (f"mdset:2:go:s{G1}", ["x"]),
                            (f"mdset:2:gc:s{G1}", [MUSECHAT]),
                            (f"mdset:2:fx:s{G1}:al:+{SAM}", None),
                            (f"mdset:2:fx:s{G1}:r:=", None),
                            (f"mdset:2:wy:d:{SAM}", None)):
            self.assertAdminRefused(self.admin(cid, values))
        r = self.h(submit(ADMIN, f"mdset:2:sub:s{G1}:c:b",
                          {"pp": "", "pd": ""}, G1, MANAGE, GENERAL))
        self.assertAdminRefused(r)
        self.unchanged()

    def test_ok_token_minted_for_an_owner(self):
        r = self.h(component(OWNER, "mdset:2:pz:x", None, G1, "0"))
        ok = next(c["custom_id"] for c in H.controls(r)
                  if c["custom_id"].split(":")[2] == "ok")
        tok = ok.split(":")[-1]
        for who in ((ADMIN, G1, MANAGE), (MEMBER, G1, "0"),
                    (OWNER2, None, None)):
            r = self.h(component(who[0], f"mdset:2:ok:s{G1}:{tok}", None,
                                 who[1], who[2], GENERAL))
            self.assertEqual(r["type"], 4)
            self.unchanged()
        # A second owner is an owner, but it is not their token.
        self.ctx.owners.add(OWNER2)
        self.h(component(OWNER2, ok))
        self.unchanged()
        # still the owner's to use
        r = self.h(component(OWNER, ok, None, G1, "0"))
        self.assertTrue(MS.is_paused(self.ctx.reload(), MS.Place.defaults()))

    def test_admin_writes_are_reported(self):
        self.admin(f"mdset:2:val:s{G1}:r:off:q")
        self.admin(f"mdset:2:ppl:s{G1}:ig", [KAI],
                   resolved=H.resolved_for(7, [KAI]))
        self.admin(f"mdset:2:un:s{G1}")
        self.assertEqual([n[:2] for n in self.ctx.notified],
                         [(G1, ADMIN)] * 3)
        # admin entries are recorded as an admin's
        e = self.ctx.overlay()["values"][f'servers."{G1}".who.replies']
        self.assertEqual(e["role"], "admin")

    def test_add_only_and_no_owners(self):
        self.admin(f"mdset:2:ppl:s{G1}:ig", [OWNER, KAI],
                   resolved=H.resolved_for(7, [OWNER, KAI]))
        self.assertEqual(self.ctx.value("who.ignore", S1), [BOB, SPAM, KAI])
        # an empty pick from a forged select changes nothing
        self.admin(f"mdset:2:ppl:s{G1}:ig", [])
        self.assertEqual(self.ctx.value("who.ignore", S1), [BOB, SPAM, KAI])


def owner_ids(ctx, pages):
    """Every custom_id the owner's pages render (a short crawl)."""
    H.install_determinism()
    c = crawl.Crawl(ctx, crawl.Actor(OWNER, G1, "0"), pages).run(
        "x", UI.handle(command(OWNER, G1, "0"), ctx))
    return sorted(c.ids)


class Members(Base):
    def test_every_rendered_control_refused(self):
        ids = owner_ids(self.ctx, 80)
        self.ctx = FakeCtx()
        self.before = self.ctx.overlay()
        self.assertGreater(len(ids), 40)
        ids.append(f"mdset:open:server:{G1}")
        for cid in ids:
            for guild, perms in ((G1, "0"), (None, None)):
                r = self.h(component(MEMBER, cid, ["x"], guild, perms))
                self.assertEqual(r["type"], 4, cid)
        r = self.h(component(ADMIN, f"mdset:open:server:{G1}", None, G1,
                             MANAGE))
        self.assertEqual(r["type"], 4)
        self.unchanged()
        self.assertEqual(self.ctx.applied, [])


class Ids(Base):
    def test_old_and_malformed_ids_are_out_of_date(self):
        bad = ["mdset:p:e", "mdset:s:d:df", "mdset:u:s" + G1,
               "mdset:y:d:df:anyone", "mdset:1:pg:x:b", "mdset:2:pg",
               "mdset:2:pg:q:b", "mdset:2:pg:x:z", "mdset:2:pg:x:b:zz",
               "mdset:2:zz:x", f"mdset:2:pg:c{G1}.{H.CAT}:b",
               f"mdset:2:set:s{G1}:zz", f"mdset:2:set:s{G1}:pk",
               f"mdset:2:val:s{G1}:r:loud", f"mdset:2:val:s{G1}:r:off:zz",
               f"mdset:2:fx:s{G1}:ig:-abc", f"mdset:2:fx:s{G1}:r:?",
               "mdset:2:mod:x:zz", "mdset:2:wy:x", "mdset:2:ok:x",
               "mdset:2:pg:x:b:" + "9" * 90, "mdset:open:server:x"]
        for cid in bad:
            r = self.h(component(OWNER, cid, ["off"], G1, "0"))
            self.assertEqual(r["type"], 4, cid)
        self.unchanged()
        self.assertEqual(self.ctx.applied, [])

    def test_stash_wrong_user_keeps_the_token(self):
        tok = self.ctx.stash_put(OWNER, {"kind": "apply", "changes": [],
                                         "origin": ["x", "b"]})
        self.ctx.owners.add(OWNER2)
        self.h(component(OWNER2, f"mdset:2:ok:x:{tok}"))
        self.assertIn(tok, self.ctx.stash)
        self.assertEqual(self.ctx.applied, [])


class Fuzz(Base):
    def test_mutated_ids_never_crash(self):
        rnd = random.Random(7)
        corpus = [c for c in owner_ids(self.ctx, 60)
                  if c.startswith("mdset:2:")]
        pieces = ["x", "d", f"s{G1}", f"c{G1}.{GENERAL}", "=", "on", "off",
                  "-" + SAM, "+" + SAM, "v", "q", "b", "", "~media.who",
                  "r", "df", "ig", "99999999999999999999999999"]
        for _ in range(400):
            parts = rnd.choice(corpus).split(":")
            i = rnd.randrange(2, len(parts))
            parts[i] = rnd.choice(pieces)
            if rnd.random() < 0.3:
                parts.append(rnd.choice(pieces))
            cid = ":".join(parts)
            for user, guild, perms in ((OWNER, G1, "0"), (ADMIN, G1, MANAGE),
                                       (MEMBER, None, None)):
                r = self.h(component(user, cid, [rnd.choice(pieces)],
                                     guild, perms))
                self.assertIn(r["type"], (4, 7, 9), cid)
                if user == MEMBER:
                    self.assertEqual(r["type"], 4)
        self.assertEqual([a for a in self.ctx.applied if a[1] == MEMBER], [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
