"""The settings engine (muse_settings.py, settings v2 "PR A"), offline:
places, words, risk by outcome, grouped writes and undo, the no-op
guard, the server-admin revert rule, who.dms_from lists (and the
monitor's DM gate), presets, pause, the bin/settings.py commands, and
old overlay files.

Usage: MUT_DIR=<tree> python3 tests/settingsengine/test_settings_engine.py
(MUT_DIR defaults to this repo)."""
import contextlib
import io
import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake import (ADMIN, ADMIN2, ANN, BASE, CAT, CH, CH2,  # noqa: E402
                  CH_G2, G1, G2, G3, INFO, MS, MUT, NAMES, OWNER,
                  PLUGINS, SAM, SPAM, FakeCtx, config, doc, load,
                  load_monitor)

P = MS.Place
C = MS.Change
DEF, DMS = P.defaults(), P.dms()
S1, S2, S3 = P.server(G1), P.server(G2), P.server(G3)
GEN = P.channel(G1, CH, CAT)
RND = P.channel(G1, CH2)
KS = f'servers."{G1}".who.replies'
KS_GEN = f'servers."{G1}".channels."{CH}".replies'


def apply(cfg, changes, by=OWNER, **kw):
    kw.setdefault("role", "owner")
    return MS.apply(cfg, changes, by, plugins_dir=PLUGINS, **kw)


def value(cfg, place, key):
    return MS.place_value(load(cfg), place, key)


# ---------------------------------------------------------------------------
# 1. Places
# ---------------------------------------------------------------------------
class Places(unittest.TestCase):
    def test_write_paths_and_defaults_split(self):
        st = load(config(self))
        pp = MS.place_path
        self.assertEqual(pp(st, DEF, "who.replies"), ("all_servers",))
        self.assertEqual(pp(st, DEF, "plugins.welcome.enabled"),
                         ("all_servers",))
        self.assertEqual(pp(st, DEF, "privacy.memory"), ("everywhere",))
        self.assertEqual(pp(st, DEF, "who.dms_from"), ("dms",))
        self.assertEqual(pp(st, DEF, "presence.status"), ("top",))
        self.assertEqual(pp(st, DMS, "style.notes"), ("dms",))
        self.assertEqual(pp(st, S1, "limits.per_day"), ("servers", G1))
        self.assertEqual(pp(st, GEN, "who.replies"),
                         ("servers", G1, "channels", CH))
        for place, key in ((GEN, "privacy.memory"), (DMS, "presence.status"),
                           (S1, "who.dms_from"), (DMS,
                                                  "who.bot_exchange_limit"),
                           (DEF, "privacy.never_share")):
            with self.assertRaises(MS.SettingsError):
                pp(st, place, key)

    def test_place_syntax(self):
        for text, place in (("defaults", DEF), ("dms", DMS),
                            (f"server:{G1}", S1),
                            (f"channel:{G1}:{CH}", P.channel(G1, CH))):
            self.assertEqual(MS.parse_place(text), place)
            self.assertEqual(MS.format_place(place), text)
        # info fills in the category, so the channel resolves like a
        # message there
        self.assertEqual(MS.parse_place(f"channel:{G1}:{CH}",
                                        INFO).category_id, CAT)
        bad = ["", "everywhere", "server:", "server:abc", f"channel:{G1}",
               f"channel:{G1}:{CH}:9", f"category:{G1}:{CAT}", "dm",
               f"server: {G1}", None]
        for text in bad:
            with self.assertRaises(MS.SettingsError, msg=text):
                MS.parse_place(text)
        with self.assertRaises(MS.SettingsError) as e:
            MS.parse_place(f"channel:{G1}:{CAT}", INFO)
        self.assertIn("category", str(e.exception))

    def test_place_keys(self):
        st = load(config(self))
        pk = MS.place_keys
        self.assertIn("who.dms_from", pk(st, DEF, "basics"))
        self.assertIn("who.dms_from", pk(st, DMS, "basics"))
        self.assertNotIn("who.dms_from", pk(st, S1, "basics"))
        self.assertEqual(pk(st, GEN, "basics"),
                         ["who.replies", "style.notes",
                          "plugins.media.enabled"])
        self.assertEqual(pk(st, GEN, "more"), [])
        self.assertIn("plugins.welcome.enabled", pk(st, GEN, "hidden"))
        self.assertIn("plugins.welcome.enabled", pk(st, DMS, "hidden"))
        self.assertNotIn("plugins.welcome.enabled", pk(st, DMS, "basics"))
        self.assertIn("plugins.welcome.enabled", pk(st, S1, "basics"))
        for k in ("presence.status", "updates.notify"):
            self.assertIn(k, pk(st, DEF, "more"))
            self.assertNotIn(k, pk(st, S1, "more"))
        hidden = pk(st, DEF, "hidden")
        for k in ("abilities.owner_commands", "presence.kind",
                  "privacy.never_share", "plugins.welcome.daily_cap",
                  "plugins.welcome.max_age_hours", "plugins.welcome.prompt"):
            self.assertIn(k, hidden)
            self.assertNotIn(k, pk(st, DEF, "basics") + pk(st, DEF, "more"))
        self.assertNotIn("who.bot_exchange_limit", pk(st, DMS, "more"))
        self.assertLessEqual(len([k for k in pk(st, DEF, "basics")
                                  if not k.startswith("plugins.")]), 6)

    def test_hidden_keys_stay_valid_and_overlay_keeps_them(self):
        """The compat trap: hiding is UI-only; presence.kind and welcome
        at everywhere keep working from the overlay."""
        ov = ("version: 1\nvalues:\n  presence.kind: {value: playing, by: "
              f"'{OWNER}', at: x}}\n  everywhere.plugins.welcome.enabled: "
              f"{{value: 'on', by: '{OWNER}', at: x}}\n")
        st = load(config(self, overlay=ov))
        self.assertEqual(st.retire, [])
        self.assertEqual(st.top("presence.kind"), "playing")
        self.assertIn("presence.kind", MS.TOP_DISCORD_EDITABLE)
        self.assertIn("everywhere", MS.CORE["who.allow"].scopes)


# ---------------------------------------------------------------------------
# 2. Words
# ---------------------------------------------------------------------------
class Words(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(MS.label("who.replies"), "When it answers")
        self.assertEqual(MS.label("who.dms_from"), "Who can DM it")
        self.assertEqual(MS.label("plugins.welcome.enabled"),
                         "Welcome new members")
        self.assertEqual(MS.label("plugins.other_thing.enabled"),
                         "Other thing (on/off)")
        vl = MS.value_label
        self.assertEqual(vl("who.replies", "conversational"), "Chatty")
        self.assertEqual(vl("who.replies", "when-addressed"),
                         "Only when @mentioned")
        self.assertEqual(vl("who.dms_from", "owners"), "Only you")
        self.assertEqual(vl("who.dms_from", [SAM], NAMES), "You and @Sam")
        self.assertEqual(vl("privacy.memory", "public-only"),
                         "Public facts only")
        self.assertEqual(vl("limits.per_day", None), "No limit")
        self.assertEqual(vl("limits.per_day", 1500), "1,500")
        self.assertEqual(vl("plugins.media.enabled", True), "On")
        self.assertEqual(vl("who.ignore", []), "Nobody")
        self.assertEqual(vl("who.allow", [SAM, ANN, "role:5"], NAMES),
                         "@Sam, @Ann and role 5")
        self.assertEqual(vl("style.notes", ""), "(empty)")
        self.assertEqual(vl("plugins.welcome.channel", CH, {CH: "general"}),
                         "#general")

    def test_describe_change(self):
        names = {CH: "general", G1: "Lounge"}
        self.assertEqual(
            MS.describe_change(GEN, "who.replies", "conversational",
                               "when-addressed", names),
            "In #general, When it answers: Chatty → Only when @mentioned")
        self.assertEqual(
            MS.describe_change(S1, "privacy.memory", "public-only",
                               "nothing", lambda i: names.get(i)),
            "In Lounge, What it may say about you: Public facts only → "
            "Nothing personal")
        self.assertEqual(
            MS.describe_change(DEF, "who.replies", "conversational", "off"),
            "By default in servers, When it answers: Chatty → Off")
        self.assertEqual(
            MS.describe_change(DEF, "style.length", "normal", "brief"),
            "By default, Reply length: Normal → Brief")
        self.assertTrue(MS.describe_change(
            DEF, "presence.status", "", "x").startswith("Bot-wide, "))
        # raw ids only as a fallback
        self.assertNotIn(CH, MS.describe_change(GEN, "who.replies", "off",
                                                "off", names))
        self.assertIn(CH, MS.describe_change(GEN, "who.replies", "off",
                                             "conversational"))
        self.assertIn("already", MS.describe_change(GEN, "who.replies",
                                                    "off", "off"))

    def test_source_tags_and_explain(self):
        cfg = config(self, BASE + "    who:\n      replies: off\n")
        apply(cfg, [C(GEN, "who.replies", "when-addressed")])
        st = load(cfg)
        names = MS.names_from(INFO, st, NAMES)
        self.assertEqual(MS.source_tag(st, S1, "who.replies", names),
                         "● set here in config.yaml")
        self.assertTrue(MS.source_tag(st, GEN, "who.replies", names)
                        .startswith("● changed here by @Zach on "))
        self.assertEqual(MS.source_tag(st, RND, "who.replies", names),
                         "Same as Lounge")
        self.assertEqual(MS.source_tag(st, S1, "style.length", names),
                         "Same as Defaults")
        self.assertEqual(MS.source_tag(st, DEF, "style.length", names),
                         "Built-in default")
        lines = MS.explain_lines(st, GEN, names=names)
        self.assertTrue(lines[0].startswith("When it answers: Only when "
                                            "@mentioned (● changed here"))

    def test_text_parsing(self):
        st = load(config(self))
        self.assertEqual(MS.key_from_text(st, "When it answers"),
                         "who.replies")
        self.assertEqual(MS.key_from_text(st, "replies"), "who.replies")
        self.assertEqual(MS.parse_value(st, "who.replies", "Chatty"),
                         "conversational")
        self.assertEqual(MS.parse_value(st, "who.dms_from", f"{SAM},{ANN}"),
                         [SAM, ANN])
        self.assertEqual(MS.parse_value(st, "limits.per_day", "no limit"),
                         "none")
        with self.assertRaises(MS.SettingsError):
            MS.key_from_text(st, "loudness")


# ---------------------------------------------------------------------------
# 3. Risk by outcome
# ---------------------------------------------------------------------------
class Risk(unittest.TestCase):
    def assess(self, cfg, changes, info=INFO):
        return MS.assess(load(cfg), changes, info, NAMES)

    def test_dms_open(self):
        cfg = config(self)
        a = self.assess(cfg, [C(DMS, "who.dms_from", "anyone")])
        self.assertTrue(a.risky)
        self.assertIn("strangers", a.warnings[0])
        self.assertIn("Other bots can DM", a.warnings[1])
        a = self.assess(cfg, [C(DMS, "who.dms_from", "shared-servers")])
        self.assertIn("shares a server", a.warnings[0])
        # Named people: names them, not "strangers"
        a = self.assess(cfg, [C(DMS, "who.dms_from", [SAM])])
        self.assertTrue(a.risky)
        self.assertEqual(a.warnings, ["@Sam can DM the bot and get "
                                      "answers."])
        # An open dms_from with an allow list: only those people
        a = self.assess(cfg, [C(DMS, "who.dms_from", "anyone"),
                              C(DMS, "who.allow", [SAM])])
        self.assertEqual(a.warnings, ["@Sam can DM the bot and get "
                                      "answers."])
        # DMs off: opening dms_from opens nothing
        apply(cfg, [C(DMS, "who.replies", "off")])
        self.assertFalse(self.assess(cfg, [C(DMS, "who.dms_from",
                                             "anyone")]).risky)

    def test_dms_tightening_and_same_audience_are_safe(self):
        cfg = config(self, BASE + "dms:\n  who:\n    dms_from: anyone\n")
        for ch in ([C(DMS, "who.dms_from", "shared-servers")],
                   [C(DMS, "who.dms_from", [SAM])],
                   [C(DMS, "style.length", "brief")],
                   [C(DMS, "who.dms_from", "owners")]):
            self.assertFalse(self.assess(cfg, ch).risky, ch)
        cfg = config(self, BASE + f'dms:\n  who:\n    dms_from: ["{SAM}"]\n')
        a = self.assess(cfg, [C(DMS, "who.dms_from", [SAM, ANN])])
        self.assertEqual(a.warnings, ["@Ann can DM the bot and get "
                                      "answers."])

    def test_allow_note_when_dms_owner_only(self):
        """The UX trap: an allow list in DMs does nothing while only the
        owners can DM; say so, without calling it risky."""
        a = self.assess(config(self), [C(DMS, "who.allow", [SAM])])
        self.assertFalse(a.risky)
        self.assertEqual(len(a.notes), 1)
        self.assertIn("You and people you pick", a.notes[0])

    def test_memory(self):
        cfg = config(self)
        a = self.assess(cfg, [C(S1, "privacy.memory", "everything")])
        self.assertTrue(a.risky)
        self.assertIn("More than 50 members", a.warnings[0])
        self.assertIn("Lounge (1,240 members)", a.warnings[0])
        a = self.assess(cfg, [C(S3, "privacy.memory", "everything")])
        self.assertTrue(a.risky)
        self.assertNotIn("More than 50 members", a.warnings[0])
        # at Defaults: every server it applies to, and later ones
        a = self.assess(cfg, [C(DEF, "privacy.memory", "everything")])
        self.assertIn("Pixel (48,213 members)", a.warnings[0])
        # DMs: only when open to strangers
        self.assertFalse(self.assess(cfg, [C(DMS, "privacy.memory",
                                             "everything")]).risky)
        apply(cfg, [C(DMS, "who.dms_from", "anyone")])
        a = self.assess(cfg, [C(DMS, "privacy.memory", "everything")])
        self.assertTrue(a.risky)
        self.assertIn("strangers", a.warnings[0])
        # quieter memory is never risky
        self.assertFalse(self.assess(cfg, [C(S2, "privacy.memory",
                                             "nothing")]).risky)

    def test_media(self):
        cfg = config(self)
        self.assertFalse(self.assess(cfg, [C(S1, "plugins.media.enabled",
                                             True)]).risky)
        a = self.assess(cfg, [C(S1, "plugins.media.enabled", True),
                              C(S1, "plugins.media.who", "everyone")])
        self.assertTrue(any("Anyone in Lounge can ask for generated"
                            in w for w in a.warnings), a.warnings)
        self.assertTrue(any("Images and video per day" in w
                            for w in a.warnings))
        a = self.assess(cfg, [C(S1, "plugins.media.enabled", True),
                              C(S1, "plugins.media.who", [SAM]),
                              C(S1, "plugins.media.per_day", 5)])
        self.assertFalse(a.risky, a.warnings)
        a = self.assess(cfg, [C(S1, "plugins.media.enabled", True),
                              C(S1, "plugins.media.who", [SAM]),
                              C(S1, "plugins.media.per_day", 21)])
        self.assertTrue(a.risky)

    def test_allow_list_counts_places(self):
        cfg = config(self)
        a = self.assess(cfg, [C(DEF, "who.allow", [SAM])])
        self.assertTrue(a.risky)
        self.assertIn("in 4 places (Lounge, Pixel, Study and servers it "
                      "joins later)", a.warnings[0])
        a = self.assess(cfg, [C(S2, "who.allow", [SAM])])
        self.assertIn("in 1 place (Pixel)", a.warnings[0])
        # with DMs open, DMs go quiet too
        apply(cfg, [C(DMS, "who.dms_from", "anyone")])
        a = self.assess(cfg, [C(DEF, "who.allow", [SAM])])
        self.assertIn("5 places", " ".join(a.warnings))
        # widening an existing list is not new silence
        apply(cfg, [C(S2, "who.allow", [SAM])])
        self.assertFalse(self.assess(cfg, [C(S2, "who.allow",
                                             [SAM, ANN])]).risky)

    def test_pause_everywhere(self):
        cfg = config(self, BASE + "    who:\n      replies: "
                     "when-addressed\n")
        a = self.assess(cfg, MS.pause_changes(DEF))
        self.assertTrue(a.risky)
        self.assertIn("Pause everywhere", a.warnings[0])
        self.assertIn("Lounge sets its own and keeps answering",
                      a.warnings[0])
        for pl in (DMS, S1, GEN):
            self.assertFalse(self.assess(cfg, MS.pause_changes(pl)).risky)

    def test_caps_removed_where_large(self):
        cfg = config(self, BASE + "    limits:\n      per_day: 100\n"
                     f'  "{G3}":\n    limits:\n      per_day: 10\n')
        a = self.assess(cfg, [C(S1, "limits.per_day", None)])
        self.assertTrue(a.risky)
        self.assertIn("no limit any more in Lounge", a.warnings[0])
        self.assertFalse(self.assess(cfg, [C(S3, "limits.per_day",
                                             None)]).risky)
        self.assertFalse(self.assess(cfg, [C(S1, "limits.per_day",
                                             200)]).risky)
        # an everywhere cap removed with 49k members in all
        cfg = config(self, BASE.replace("servers:", "everywhere:\n  limits:"
                                        "\n    per_person_per_day: 5\n"
                                        "servers:"))
        a = self.assess(cfg, [C(DEF, "limits.per_person_per_day", None)])
        self.assertTrue(a.risky)
        self.assertNotIn("the bot as a whole", a.warnings[0])  # servers named
        a = MS.assess(load(cfg), [C(DEF, "limits.per_person_per_day",
                                    None)], {"guilds": []})
        self.assertTrue(a.risky)   # nothing known: fail closed
        self.assertIn("size unknown", a.warnings[0])

    def test_conversational_big(self):
        cfg = config(self, BASE.replace("servers:", "all_servers:\n  who:\n"
                                        "    replies: when-addressed\n"
                                        "servers:"))
        a = self.assess(cfg, [C(S2, "who.replies", "conversational")])
        self.assertTrue(a.risky)
        self.assertIn("Chatty in Pixel (48,213 members)", a.warnings[0])
        a = self.assess(cfg, [C(GEN, "who.replies", "conversational")])
        self.assertIn("#general in Lounge", a.warnings[0])
        self.assertFalse(self.assess(cfg, [C(S3, "who.replies",
                                             "conversational")]).risky)
        # without member counts the size is unknown: fail closed
        a = MS.assess(load(cfg), [C(S2, "who.replies", "conversational")])
        self.assertTrue(a.risky)
        self.assertIn("size unknown", a.warnings[0])

    def test_bot_exchange(self):
        cfg = config(self, BASE + "    who:\n      bot_exchange_limit: 4\n")
        self.assertTrue(self.assess(cfg, [C(S1, "who.bot_exchange_limit",
                                            None)]).risky)
        self.assertTrue(self.assess(cfg, [C(S1, "who.bot_exchange_limit",
                                            11)]).risky)
        self.assertFalse(self.assess(cfg, [C(S1, "who.bot_exchange_limit",
                                             10)]).risky)

    def test_undo_of_own_change_judged_by_outcome(self):
        cfg = config(self, BASE.replace("servers:", "all_servers:\n  who:\n"
                                        "    replies: when-addressed\n"
                                        "servers:"))
        apply(cfg, [C(S3, "style.length", "brief")])
        st = load(cfg)
        self.assertFalse(MS.assess(st, MS.undo_plan(st, S3), INFO).risky)
        # an undo that reopens Chatty in a big server is risky
        apply(cfg, [C(S2, "who.replies", "conversational")], OWNER)
        apply(cfg, [C(S2, "who.replies", "off")], OWNER)
        st = load(cfg)
        self.assertTrue(MS.assess(st, MS.undo_plan(st, S2), INFO).risky)


# ---------------------------------------------------------------------------
# 4. Grouped writes and undo
# ---------------------------------------------------------------------------
class Groups(unittest.TestCase):
    def test_apply_is_one_write_one_group(self):
        cfg = config(self)
        r = apply(cfg, [C(S1, "who.replies", "when-addressed"),
                        C(S1, "limits.per_day", 50),
                        C(GEN, "who.replies", "off")])
        self.assertTrue(r.changed)
        self.assertEqual(len(r.group), 11)
        self.assertEqual(r.describe({CH: "general", G1: "Lounge"})[2],
                         "In #general, When it answers: Chatty → Off")
        hist = doc(cfg)["history"]
        self.assertEqual(len(hist), 3)
        self.assertEqual({h["group"] for h in hist}, {r.group})
        self.assertEqual({h["by"] for h in hist}, {OWNER})
        # one bad change: nothing at all is written
        before = doc(cfg)
        for bad in ([C(S1, "who.replies", "off"),
                     C(S1, "limits.per_day", -5)],
                    [C(S1, "who.replies", "off"),
                     C(S1, "who.replies", "when-addressed")],
                    [C(S1, "who.replies", "off"),
                     C(GEN, "privacy.memory", "nothing")],
                    [C(S1, "privacy.suspicious", "answer")]):
            with self.assertRaises(MS.SettingsError):
                apply(cfg, bad)
            self.assertEqual(doc(cfg), before)

    def test_group_ids(self):
        cfg = config(self)
        r = apply(cfg, [C(S1, "style.length", "brief")], "muse.ai",
                  group="nl-1234567890123456789")
        self.assertEqual(r.group, "nl-1234567890123456789")
        self.assertEqual(doc(cfg)["history"][-1]["group"], r.group)
        self.assertEqual(doc(cfg)["history"][-1]["by"], "muse.ai")
        for bad in ("x" * 25, "a b", "", "g/1"):
            with self.assertRaises(MS.SettingsError):
                apply(cfg, [C(S1, "style.length", "detailed")], group=bad)

    def test_undo_whole_group_and_children(self):
        cfg = config(self)
        apply(cfg, [C(S1, "style.length", "brief")])
        r = apply(cfg, [C(S1, "who.replies", "when-addressed"),
                        C(GEN, "who.replies", "off")])
        st = load(cfg)
        names = MS.names_from(INFO, st)
        text, ident = MS.undo_preview(st, S1, True, names)
        self.assertEqual(ident, r.group)
        self.assertIn("In #general, When it answers → back to Chatty",
                      text)
        self.assertIn("In Lounge, When it answers → back to Chatty", text)
        self.assertIn("includes a change in a channel", text)
        done = MS.undo_changes(cfg, S1, OWNER, PLUGINS, children=True,
                               expect=ident)
        self.assertEqual(sorted(done), sorted([KS, KS_GEN]))
        st = load(cfg)
        self.assertEqual(MS.place_value(st, GEN, "who.replies"),
                         "conversational")
        self.assertEqual(MS.place_value(st, S1, "style.length"), "brief")
        # the undo itself is never offered (no redo); the older change is
        text, _ = MS.undo_preview(st, S1, True, names)
        self.assertEqual(text, "Undo: In Lounge, Reply length → back to "
                               "Normal")

    def test_children_only_when_asked(self):
        cfg = config(self)
        apply(cfg, [C(GEN, "who.replies", "off")])
        st = load(cfg)
        self.assertEqual(MS.undo_preview(st, S1), (None, None))
        self.assertIsNone(MS.undo(cfg, f'servers."{G1}"', OWNER, PLUGINS))
        self.assertEqual(MS.undo(cfg, f'servers."{G1}"', OWNER, PLUGINS,
                                 children=True), KS_GEN)

    def test_group_reverted_wherever_it_landed(self):
        """Pause everywhere writes DMs and All servers; Undo at Defaults
        undoes both."""
        cfg = config(self)
        apply(cfg, MS.pause_changes(DEF))
        st = load(cfg)
        self.assertTrue(MS.is_paused(st, DEF))
        self.assertEqual(len(MS.undo_plan(st, DEF)), 2)
        MS.undo(cfg, "defaults", OWNER, PLUGINS)
        self.assertFalse(MS.is_paused(load(cfg), DEF))
        self.assertEqual(value(cfg, DMS, "who.replies"), "conversational")

    def test_stale_undo_refused(self):
        cfg = config(self)
        apply(cfg, [C(S1, "who.replies", "off")])
        _t, ident = MS.undo_preview(load(cfg), S1)
        apply(cfg, [C(S1, "style.length", "brief")])
        with self.assertRaises(MS.SettingsError):
            MS.undo(cfg, S1, OWNER, PLUGINS, expect=ident)
        self.assertEqual(value(cfg, S1, "who.replies"), "off")

    def test_undo_group_and_group_current(self):
        cfg = config(self)
        r = apply(cfg, [C(S1, "who.replies", "off"),
                        C(S1, "style.length", "brief")], group="nl-77")
        self.assertTrue(MS.group_current(load(cfg), "nl-77"))
        self.assertFalse(MS.group_current(load(cfg), "nl-78"))
        apply(cfg, [C(S1, "style.length", "detailed")])
        self.assertFalse(MS.group_current(load(cfg), r.group))
        with self.assertRaises(MS.SettingsError):
            MS.undo_group(cfg, "nl-77", OWNER, PLUGINS, role="owner")
        r2 = apply(cfg, [C(S2, "who.replies", "off")], group="nl-79")
        self.assertEqual(MS.undo_group(cfg, "nl-79", OWNER, PLUGINS,
                                       role="owner"),
                         [f'servers."{G2}".who.replies'])
        self.assertFalse(MS.group_current(load(cfg), r2.group))
        with self.assertRaises(MS.SettingsError):   # undone already
            MS.undo_group(cfg, "nl-79", OWNER, PLUGINS, role="owner")
        with self.assertRaises(MS.SettingsError):
            MS.undo_group(cfg, "nl-nope", OWNER, PLUGINS, role="owner")
        # an undo's own group is not undoable
        apply(cfg, [C(S3, "who.replies", "off"),
                    C(S3, "style.length", "brief")])
        MS.undo(cfg, S3, OWNER, PLUGINS)
        ugroup = doc(cfg)["history"][-1]["group"]
        self.assertTrue(doc(cfg)["history"][-1]["undo"])
        with self.assertRaises(MS.SettingsError):
            MS.undo_group(cfg, ugroup, OWNER, PLUGINS, role="owner")

    def test_old_ungrouped_history_loads_and_undoes(self):
        """A config.discord.yaml from before groups: entries and history
        lines without group/role still load and undo one at a time."""
        ov = f"""# old file
version: 1
values:
  servers."{G1}".who.replies:
    value: when-addressed
    base: null
    by: '{OWNER}'
    at: '2026-10-18T10:00:00Z'
  dms.style.length:
    value: brief
    base: null
    by: '{OWNER}'
    at: '2026-10-18T10:01:00Z'
history:
- key: servers."{G1}".who.replies
  from: inherit
  to: 'off'
  by: '{OWNER}'
  at: '2026-10-18T09:00:00Z'
- key: servers."{G1}".who.replies
  from: 'off'
  to: when-addressed
  by: '{OWNER}'
  at: '2026-10-18T10:00:00Z'
- key: dms.style.length
  from: inherit
  to: brief
  by: '{OWNER}'
  at: '2026-10-18T10:01:00Z'
"""
        cfg = config(self, overlay=ov)
        st = load(cfg)
        self.assertEqual(st.retire, [])
        self.assertEqual(MS.place_value(st, S1, "who.replies"),
                         "when-addressed")
        text, ident = MS.undo_preview(st, S1)
        self.assertEqual(text, "Undo: In Lounge, When it answers → back to "
                               "Off")
        self.assertTrue(ident.startswith("h"))
        self.assertEqual(MS.undo(cfg, S1, OWNER, PLUGINS, expect=ident), KS)
        self.assertEqual(value(cfg, S1, "who.replies"), "off")
        self.assertEqual(value(cfg, DMS, "style.length"), "brief")
        self.assertEqual(MS.undo(cfg, S1, OWNER, PLUGINS), KS)
        self.assertEqual(value(cfg, S1, "who.replies"), "conversational")
        self.assertIsNone(MS.undo(cfg, S1, OWNER, PLUGINS))
        self.assertEqual(MS.undo(cfg, "dms", OWNER, PLUGINS),
                         "dms.style.length")


# ---------------------------------------------------------------------------
# 5. The no-op guard
# ---------------------------------------------------------------------------
class NoOp(unittest.TestCase):
    def test_same_value_writes_nothing(self):
        cfg = config(self)
        apply(cfg, [C(S1, "style.length", "brief")])
        n = len(doc(cfg)["history"])
        r = apply(cfg, [C(S1, "style.length", "brief")])
        self.assertFalse(r.changed)
        self.assertEqual(r.status, MS.NO_CHANGE)
        self.assertEqual(len(doc(cfg)["history"]), n)
        # same effective value with nothing set at that layer
        r = apply(cfg, [C(RND, "who.replies", "conversational")])
        self.assertIs(r.status, MS.NO_CHANGE)
        self.assertNotEqual(MS.NO_CHANGE, "no change")
        # a reset where nothing is set
        self.assertFalse(apply(cfg, [C(S2, "who.replies",
                                       "inherit")]).changed)
        self.assertIs(MS.reset(cfg, f'servers."{G2}".who.replies', OWNER,
                               PLUGINS), MS.NO_CHANGE)
        self.assertIs(MS.set_value(cfg, f'servers."{G1}".style.length',
                                   "brief", OWNER, PLUGINS), MS.NO_CHANGE)
        self.assertEqual(len(doc(cfg)["history"]), n)
        # mixed: only the real change is written
        r = apply(cfg, [C(S1, "style.length", "brief"),
                        C(S1, "who.replies", "off")])
        self.assertEqual([p.key for p in r.written], ["who.replies"])
        self.assertEqual([p.key for p in r.unchanged], ["style.length"])

    def test_reset_still_writes_inherit(self):
        cfg = config(self, BASE + "    who:\n      replies: off\n")
        r = apply(cfg, [C(S1, "who.replies", "inherit")])
        self.assertTrue(r.changed)
        self.assertEqual(doc(cfg)["values"][KS]["value"], "inherit")
        self.assertEqual(value(cfg, S1, "who.replies"), "conversational")
        MS.set_value(cfg, f'servers."{G2}".who.replies', "off", OWNER,
                     PLUGINS)
        self.assertEqual(MS.reset(cfg, f'servers."{G2}".who.replies',
                                  OWNER, PLUGINS), "inherit")
        self.assertEqual(doc(cfg)["values"][f'servers."{G2}".who.replies']
                         ["value"], "inherit")

    def test_file_value_drops_the_entry(self):
        cfg = config(self, BASE + "    who:\n      replies: off\n")
        apply(cfg, [C(S1, "who.replies", "when-addressed")])
        self.assertIn(KS, doc(cfg)["values"])
        apply(cfg, [C(S1, "who.replies", "off")])
        self.assertNotIn(KS, doc(cfg)["values"])
        self.assertTrue(doc(cfg)["history"][-1]["dropped"])
        self.assertEqual(value(cfg, S1, "who.replies"), "off")
        # still undoable
        MS.undo(cfg, S1, OWNER, PLUGINS)
        self.assertEqual(value(cfg, S1, "who.replies"), "when-addressed")
        # the same through set_value
        MS.set_value(cfg, KS, "off", OWNER, PLUGINS)
        self.assertNotIn(KS, doc(cfg)["values"])

    def test_channel_with_unknown_category_is_written(self):
        cfg = config(self, BASE + f'    channels:\n      "{CAT}":\n'
                     "        replies: off\n")
        # #general is in category Staff (off); without knowing that, a
        # "conversational" write must not be skipped
        r = apply(cfg, [C(P.channel(G1, CH), "who.replies",
                          "conversational")])
        self.assertTrue(r.changed)
        self.assertEqual(value(cfg, GEN, "who.replies"), "conversational")


# ---------------------------------------------------------------------------
# 6. Server admins: quieter, or revert their own changes
# ---------------------------------------------------------------------------
class Admin(unittest.TestCase):
    def setUp(self):
        self.cfg = config(self, BASE + "    who:\n      replies: "
                          "when-addressed\n      ignore: "
                          f'["{SPAM}"]\n')

    def admin(self, changes, gid=G1, info=INFO, by=ADMIN):
        return apply(self.cfg, changes, by, role="admin", admin_gid=gid,
                     info=info)

    def refused(self, changes, **kw):
        before = doc(self.cfg)
        with self.assertRaises(MS.SettingsError):
            self.admin(changes, **kw)
        self.assertEqual(doc(self.cfg), before)

    def test_quieter_ok_and_marked(self):
        self.admin([C(GEN, "who.replies", "off")])
        self.assertEqual(value(self.cfg, GEN, "who.replies"), "off")
        e = doc(self.cfg)["values"][KS_GEN]
        self.assertEqual(e["role"], "admin")
        self.assertEqual(e["below"], {})
        self.assertEqual(doc(self.cfg)["history"][-1]["role"], "admin")

    def test_revert_own_change(self):
        self.admin([C(S1, "who.replies", "off")])
        self.admin([C(S1, "who.replies", "inherit")])   # reset = revert
        self.assertEqual(value(self.cfg, S1, "who.replies"),
                         "when-addressed")   # the file's value again
        self.admin([C(GEN, "who.replies", "off")])
        self.admin([C(GEN, "who.replies", "inherit")])
        self.assertEqual(value(self.cfg, GEN, "who.replies"),
                         "when-addressed")

    def test_revert_restores_owner_overlay_value(self):
        apply(self.cfg, [C(GEN, "who.replies", "conversational")], OWNER)
        self.admin([C(GEN, "who.replies", "when-addressed")])
        self.admin([C(GEN, "who.replies", "off")])        # a chain
        self.assertEqual(doc(self.cfg)["values"][KS_GEN]["below"],
                         {"value": "conversational"})
        # reverting goes back to the owner's value, not past it
        self.admin([C(GEN, "who.replies", "inherit")])
        self.assertEqual(value(self.cfg, GEN, "who.replies"),
                         "conversational")
        # picking the owners' value explicitly is a revert too
        self.admin([C(GEN, "who.replies", "off")])
        self.admin([C(GEN, "who.replies", "conversational")])
        self.assertEqual(value(self.cfg, GEN, "who.replies"),
                         "conversational")

    def test_never_past_the_owners_value(self):
        self.admin([C(S1, "who.replies", "off")])
        # conversational is louder than both now (off) and the owners'
        # when-addressed
        self.refused([C(S1, "who.replies", "conversational")])
        self.refused([C(GEN, "who.replies", "conversational")])

    def test_cannot_revert_an_owners_entry(self):
        apply(self.cfg, [C(GEN, "who.replies", "off")], OWNER)
        self.refused([C(GEN, "who.replies", "inherit")])
        self.refused([C(GEN, "who.replies", "when-addressed")])
        # nor the file's
        self.refused([C(S1, "who.ignore", [])])

    def test_legacy_and_named_entries_are_owner_level(self):
        """Recognised only by role: admin. An old entry by a non-owner id
        without a role, and a non-numeric by, are owner-level."""
        ov = ("version: 1\nvalues:\n"
              f"  {KS_GEN}: {{value: 'off', base: null, by: '{ADMIN2}', "
              f"at: x}}\n"
              f'  servers."{G1}".channels."{CH2}".replies: {{value: \'off\','
              f" base: null, by: muse.ai, at: x}}\n")
        self.cfg = config(self, BASE, overlay=ov)
        self.refused([C(GEN, "who.replies", "inherit")])
        self.refused([C(RND, "who.replies", "inherit")])
        self.assertEqual(MS.entry_role({"by": ADMIN2}), "owner")
        self.assertEqual(MS.entry_role({"by": "cli", "role": "admin"}),
                         "admin")

    def test_ignore_list(self):
        self.admin([C(S1, "who.ignore", [SPAM, SAM])])
        # removing someone the admins added is a revert
        self.admin([C(S1, "who.ignore", [SPAM])])
        self.assertEqual(value(self.cfg, S1, "who.ignore"), [SPAM])
        self.admin([C(S1, "who.ignore", [SPAM, SAM, ANN])])
        self.admin([C(S1, "who.ignore", [SPAM, ANN])])
        # the owners' entries cannot be removed: an admin's list is
        # always joined with the owners'
        self.admin([C(S1, "who.ignore", [ANN])])
        self.assertEqual(value(self.cfg, S1, "who.ignore"), [ANN, SPAM])
        # a reset reverts to the owners' list
        self.admin([C(S1, "who.ignore", "inherit")])
        self.assertEqual(value(self.cfg, S1, "who.ignore"), [SPAM])

    def test_forged_scope_key_and_value(self):
        self.refused([C(S2, "who.replies", "off")])          # other guild
        self.refused([C(P.channel(G1, CH_G2), "who.replies", "off")])
        self.refused([C(P.channel(G2, CH_G2), "who.replies", "off")])
        self.refused([C(P.channel(G1, "70000000000000000555"),
                        "who.replies", "off")])                   # unknown
        self.refused([C(GEN, "who.replies", "off")], info=None)  # no list
        for pl in (DEF, DMS):
            self.refused([C(pl, "who.replies", "off")])
        self.refused([C(S1, "who.allow", [SAM])])
        self.refused([C(S1, "privacy.memory", "nothing")])
        self.refused([C(GEN, "who.ignore", [SAM])])
        self.refused([C(S1, "plugins.media.enabled", True)])
        self.refused([C(S1, "who.replies", "conversational")])
        # a forged layer path on an innocent-looking change
        self.refused([C(S1, "who.replies", "off", path=("all_servers",))])
        self.refused([C(S1, "who.replies", "off",
                        path=("servers", G2))])
        # a preset is an owner's tool
        st = load(self.cfg)
        self.refused(MS.preset_changes(st, "work", S1, INFO))
        # a mixed batch is refused whole
        self.refused([C(S1, "plugins.welcome.enabled", False),
                      C(S1, "who.replies", "conversational")])

    def test_admin_undo_sees_only_admin_changes(self):
        apply(self.cfg, [C(S1, "who.replies", "off")], OWNER)
        st = load(self.cfg)
        self.assertEqual(MS.undo_plan(st, S1, admin_gid=G1), [])
        self.assertIsNone(MS.undo(self.cfg, S1, ADMIN, PLUGINS,
                                  admin_gid=G1, info=INFO))
        # back to the file's value: the owner's entry goes away
        apply(self.cfg, [C(S1, "who.replies", "when-addressed")], OWNER)
        self.assertNotIn(KS, doc(self.cfg)["values"])
        self.admin([C(GEN, "who.replies", "off")])
        apply(self.cfg, [C(S1, "style.length", "brief")], OWNER)
        st = load(self.cfg)
        text, _ = MS.undo_preview(st, S1, True, MS.names_from(INFO, st),
                                  admin_gid=G1)
        self.assertIn("#general", text)
        self.assertEqual(MS.undo(self.cfg, S1, ADMIN, PLUGINS, children=True,
                                 admin_gid=G1, info=INFO), KS_GEN)
        self.assertEqual(value(self.cfg, GEN, "who.replies"),
                         "when-addressed")
        self.assertEqual(doc(self.cfg)["history"][-1]["role"], "admin")
        # another server's admin cannot undo it
        self.admin([C(GEN, "who.replies", "off")])
        self.assertIsNone(MS.undo(self.cfg, P.server(G2), ADMIN, PLUGINS,
                                  admin_gid=G2, info=INFO))

    def test_legacy_ui_path_marks_admin_writes(self):
        MS.set_value(self.cfg, KS_GEN, "off", ADMIN, PLUGINS)
        self.assertEqual(doc(self.cfg)["values"][KS_GEN]["role"], "admin")
        MS.set_value(self.cfg, f'servers."{G1}".style.length', "brief",
                     OWNER, PLUGINS)
        self.assertNotIn("role", doc(self.cfg)["values"]
                         [f'servers."{G1}".style.length'])


# ---------------------------------------------------------------------------
# 7. who.dms_from lists
# ---------------------------------------------------------------------------
class DmsFrom(unittest.TestCase):
    def test_values(self):
        spec = MS.CORE["who.dms_from"]
        self.assertEqual(MS.normalize(spec, [SAM, int(ANN), SAM]),
                         (True, [SAM, ANN]))
        self.assertEqual(MS.normalize(spec, SAM), (True, [SAM]))
        self.assertEqual(MS.normalize(spec, []), (True, "owners"))
        self.assertFalse(MS.normalize(spec, ["role:5"])[0])
        self.assertFalse(MS.normalize(spec, ["sam"])[0])
        self.assertIn("list of user ids", spec.valid_text())
        L = MS.loosens
        self.assertTrue(L("who.dms_from", "owners", [SAM]))
        self.assertFalse(L("who.dms_from", [SAM], "owners"))
        self.assertTrue(L("who.dms_from", [SAM], [SAM, ANN]))
        self.assertFalse(L("who.dms_from", [SAM, ANN], [SAM]))
        self.assertTrue(L("who.dms_from", [SAM], "anyone"))
        self.assertFalse(L("who.dms_from", "anyone", [SAM]))
        self.assertIsNone(L("who.dms_from", "shared-servers", [SAM]))
        self.assertTrue(L("who.dms_from", "owners", "anyone"))

    def test_config_list_loads(self):
        st = load(config(self, BASE + f'dms:\n  who:\n    dms_from: ["{SAM}"'
                         "]\n    allow: everyone\n"))
        self.assertEqual(st.get("who.dms_from", MS.Scope.dm()), [SAM])
        self.assertEqual(MS.dm_access(st), "list")
        with self.assertRaises(MS.SettingsError):
            load(config(self, BASE + 'dms:\n  who:\n    dms_from: ["sam"]\n'))

    def test_choices(self):
        cfg = config(self)
        st = load(cfg)
        self.assertEqual(MS.dm_access(st), "owners")
        self.assertEqual(MS.dms_from_changes(st, "nobody"),
                         [C(DMS, "who.replies", "off")])
        apply(cfg, MS.dms_from_changes(st, "nobody"))
        st = load(cfg)
        self.assertEqual(MS.dm_access(st), "nobody")
        ch = MS.dms_from_changes(st, "list", [SAM])
        self.assertEqual(ch, [C(DMS, "who.dms_from", [SAM]),
                              C(DMS, "who.replies", "inherit")])
        apply(cfg, ch)
        st = load(cfg)
        self.assertEqual(MS.dm_access(st), "list")
        self.assertEqual(MS.value_label("who.dms_from",
                                        st.get("who.dms_from",
                                               MS.Scope.dm()), NAMES),
                         "You and @Sam")
        with self.assertRaises(MS.SettingsError):
            MS.dms_from_changes(st, "list", [])

    def test_monitor_dm_gate(self):
        m = load_monitor(self, BASE + f'dms:\n  who:\n    dms_from: ["{SAM}"'
                         "]\n")
        sam = {"id": SAM, "username": "sam"}
        ann = {"id": ANN, "username": "ann"}
        owner = {"id": OWNER, "username": "zach"}
        self.assertTrue(m.dm_sender_allowed(sam))
        self.assertTrue(m.dm_sender_allowed(owner))
        self.assertFalse(m.dm_sender_allowed(ann))
        self.assertEqual(m.dm_refusal(ann), "not in who.dms_from")
        # dms.who.allow / ignore still apply on top of the list
        m = load_monitor(self, BASE + f'dms:\n  who:\n    dms_from: ["{SAM}"'
                         f', "{ANN}"]\n    ignore: ["{ANN}"]\n')
        self.assertTrue(m.dm_sender_allowed(sam))
        self.assertEqual(m.dm_refusal(ann), "on who.ignore")
        m = load_monitor(self, BASE + "dms:\n  who:\n    dms_from: anyone\n"
                         f'    allow: ["{SAM}"]\n')
        self.assertTrue(m.dm_sender_allowed(sam))
        self.assertEqual(m.dm_refusal(ann), "not in who.allow")
        # Nobody: DMs off, owners included (and the owner's refusal is
        # ledgered with its reason, so the sweep never answers it later)
        m = load_monitor(self, BASE + f'dms:\n  who:\n    dms_from: ["{SAM}"'
                         "]\n    replies: 'off'\n")
        self.assertFalse(m.dm_sender_allowed(sam))
        self.assertFalse(m.dm_sender_allowed(owner))
        msg = {"id": "123456789012345678", "channel_id": "4" + "0" * 17,
               "author": owner, "type": 0}
        with contextlib.redirect_stdout(io.StringIO()):
            m.note_dm_refused(msg)
        ent = m._ledger_load()["entries"]["123456789012345678"]
        self.assertEqual(ent["reason"], "settings: who.replies is off for "
                                        "DMs")
        # DMs off: nobody's DM channel is polled
        self.assertEqual(m.dm_poll_user_ids(), [])

    def test_shared_servers_unchanged(self):
        m = load_monitor(self, BASE + "dms:\n  who:\n    dms_from: "
                         "shared-servers\n")
        m.api.members.add((G1, SAM))
        self.assertTrue(m.dm_sender_allowed({"id": SAM}))
        self.assertEqual(m.dm_refusal({"id": ANN}),
                         "shares no server with the bot")


# ---------------------------------------------------------------------------
# 8. Presets
# ---------------------------------------------------------------------------
class Presets(unittest.TestCase):
    def test_changes(self):
        cfg = config(self)
        st = load(cfg)
        ch = {c.key: c.value for c in MS.preset_changes(st, "Community", S1,
                                                        INFO)}
        self.assertEqual(ch, {"who.replies": "when-addressed",
                              "limits.per_person_per_day": 20,
                              "limits.per_day": 300,
                              "who.bot_exchange_limit": 4})  # media off
        ch = {c.key: c.value for c in MS.preset_changes(st, "work", S1)}
        self.assertEqual(ch, {"who.replies": "when-addressed",
                              "privacy.memory": "nothing",
                              "who.bots": "ignore",
                              "style.notes": MS.WORK_NOTES})
        apply(cfg, [C(S1, "plugins.media.enabled", True),
                    C(S1, "style.notes", "Be kind.")])
        st = load(cfg)
        ch = {c.key: c.value for c in MS.preset_changes(st, "community", S1)}
        self.assertEqual(ch["plugins.media.per_day"], 10)
        ch = {c.key: c.value for c in MS.preset_changes(st, "work", S1)}
        self.assertNotIn("style.notes", ch)   # only if empty

    def test_friends_size_and_place(self):
        st = load(config(self))
        with self.assertRaises(MS.SettingsError) as e:
            MS.preset_changes(st, "friends", S1, INFO)
        self.assertIn("1,240", str(e.exception))
        ch = MS.preset_changes(st, "friends", S3, INFO)
        self.assertIn(C(S3, "privacy.memory", "everything"), ch)
        self.assertTrue(MS.assess(st, ch, INFO).risky)  # confirm first
        for pl in (DEF, DMS, GEN):
            with self.assertRaises(MS.SettingsError):
                MS.preset_changes(st, "work", pl)
        with self.assertRaises(MS.SettingsError):
            MS.preset_changes(st, "party", S1)

    def test_one_group_and_matching(self):
        cfg = config(self)
        st = load(cfg)
        self.assertIsNone(MS.matching_preset(st, S1))
        r = apply(cfg, MS.preset_changes(st, "community", S1, INFO))
        self.assertEqual(MS.matching_preset(load(cfg), S1), "Community")
        self.assertEqual(MS.preset_changes(load(cfg), "community", S1), [])
        self.assertEqual(len({h["group"] for h in doc(cfg)["history"]}), 1)
        MS.undo(cfg, S1, OWNER, PLUGINS)
        self.assertIsNone(MS.matching_preset(load(cfg), S1))
        self.assertFalse(MS.group_current(load(cfg), r.group))
        apply(cfg, MS.preset_changes(load(cfg), "work", S1))
        self.assertEqual(MS.matching_preset(load(cfg), S1), "Work")
        apply(cfg, MS.preset_changes(load(cfg), "friends", S3, INFO))
        self.assertEqual(MS.matching_preset(load(cfg), S3), "Friends")


# ---------------------------------------------------------------------------
# 9. Pause
# ---------------------------------------------------------------------------
class Pause(unittest.TestCase):
    def test_pause_and_resume_name_their_place(self):
        names = {G1: "Lounge", CH: "general"}
        self.assertEqual(MS.pause_label(GEN, names), "Pause in #general")
        self.assertEqual(MS.pause_label(S1, names), "Pause in Lounge")
        self.assertEqual(MS.pause_label(DEF), "Pause everywhere")
        self.assertEqual(MS.pause_changes(GEN),
                         [C(GEN, "who.replies", "off")])
        cfg = config(self, BASE + "    who:\n      replies: when-addressed\n")
        st = load(cfg)
        self.assertEqual(MS.resume_changes(st, S1), [])
        apply(cfg, MS.pause_changes(S1))
        st = load(cfg)
        self.assertTrue(MS.is_paused(st, S1))
        self.assertFalse(MS.is_paused(st, GEN))     # paused at the server
        self.assertEqual(MS.resume_changes(st, GEN), [])
        self.assertEqual(MS.resume_changes(st, S1),
                         [C(S1, "who.replies", "when-addressed")])
        apply(cfg, MS.resume_changes(st, S1))
        self.assertEqual(value(cfg, S1, "who.replies"), "when-addressed")
        apply(cfg, MS.pause_changes(GEN))
        self.assertEqual(value(cfg, RND, "who.replies"), "when-addressed")
        self.assertEqual(value(cfg, GEN, "who.replies"), "off")
        st = load(cfg)
        self.assertEqual(MS.resume_changes(st, GEN),
                         [C(GEN, "who.replies", "inherit")])

    def test_everywhere(self):
        cfg = config(self)
        apply(cfg, MS.pause_changes(DEF))
        st = load(cfg)
        self.assertEqual(len(MS.resume_changes(st, DEF)), 2)
        apply(cfg, MS.resume_changes(st, DEF))
        self.assertFalse(MS.is_paused(load(cfg), DEF))


# ---------------------------------------------------------------------------
# 10. bin/settings.py
# ---------------------------------------------------------------------------
class CLI(unittest.TestCase):
    def setUp(self):
        self.cfg = config(self, state={"known_guilds": {G1: "Lounge",
                                                        G2: "Pixel"},
                                       "guild_members": {G1: 1240,
                                                         G2: 48213}})

    def run_cli(self, *args):
        r = subprocess.run([sys.executable,
                            os.path.join(MUT, "bin", "settings.py"),
                            "--config", self.cfg] + list(args),
                           capture_output=True, text=True, timeout=60)
        return r.returncode, r.stdout + r.stderr

    def test_set_explain_undo(self):
        rc, out = self.run_cli("set", "When it answers",
                               "Only when @mentioned", "--place",
                               f"server:{G1}")
        self.assertEqual(rc, 0, out)
        self.assertIn("In Lounge, When it answers: Chatty → Only when "
                      "@mentioned", out)
        h = doc(self.cfg)["history"][-1]
        self.assertEqual(h["by"], "muse.ai")
        self.assertRegex(h["group"], r"^cli-[0-9a-f]{8}$")
        self.assertNotIn("role", h)
        rc, out = self.run_cli("set", "who.replies", "when-addressed",
                               "--place", f"server:{G1}")
        self.assertEqual(rc, 0)
        self.assertIn("No change", out)
        rc, out = self.run_cli("explain", "--place", f"server:{G1}")
        self.assertIn("When it answers: Only when @mentioned (● changed "
                      "here by muse.ai", out)
        rc, out = self.run_cli("explain", "--place", f"server:{G1}",
                               "privacy.never_share")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli("undo", "--place", f"server:{G1}")
        self.assertEqual(rc, 3, out)   # undoing to Chatty in a big server
        self.assertIn("Warning: Chatty in Lounge", out)
        rc, out = self.run_cli("undo", "--place", f"server:{G1}", "--yes",
                               "--by", "zach")
        self.assertEqual(rc, 0, out)
        self.assertIn("Undo: In Lounge, When it answers → back to Chatty",
                      out)
        self.assertEqual(doc(self.cfg)["history"][-1]["by"], "zach")
        rc, out = self.run_cli("undo", "--place", f"server:{G1}")
        self.assertIn("Nothing to undo in Lounge", out)

    def test_risky_needs_yes(self):
        rc, out = self.run_cli("set", "who.dms_from", "anyone", "--place",
                               "dms")
        self.assertEqual(rc, 3)
        self.assertIn("Warning: Anyone on Discord", out)
        self.assertNotIn("dms.who.dms_from", doc(self.cfg)["values"])
        rc, out = self.run_cli("set", "who.dms_from", "anyone", "--place",
                               "dms", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(value(self.cfg, DMS, "who.dms_from"), "anyone")
        rc, out = self.run_cli("set", "who.dms_from", "Nobody", "--place",
                               "dms")
        self.assertEqual(rc, 0, out)
        self.assertEqual(MS.dm_access(load(self.cfg)), "nobody")
        rc, out = self.run_cli("set", "who.dms_from", SAM, "--place", "dms",
                               "--yes")
        self.assertEqual(rc, 0, out)
        self.assertEqual(MS.dm_access(load(self.cfg)), "list")

    def test_preset_pause_resume_reset(self):
        rc, out = self.run_cli("preset", "community", "--place",
                               f"server:{G1}")
        self.assertEqual(rc, 0, out)
        self.assertIn("Replies per day (total): No limit → 300", out)
        rc, out = self.run_cli("explain", "--place", f"server:{G1}")
        self.assertIn("Matches: Community", out)
        rc, out = self.run_cli("preset", "friends", "--place",
                               f"server:{G1}")
        self.assertEqual(rc, 1)
        self.assertIn("up to 50 members", out)
        rc, out = self.run_cli("pause", "--place", "defaults")
        self.assertEqual(rc, 3)
        rc, out = self.run_cli("pause", "--place",
                               f"channel:{G1}:{CH}")
        self.assertEqual(rc, 0, out)
        self.assertIn("When it answers: Only when @mentioned → Off", out)
        rc, out = self.run_cli("resume", "--place", f"channel:{G1}:{CH}")
        self.assertEqual(rc, 0, out)
        rc, out = self.run_cli("resume", "--place", f"channel:{G1}:{CH}")
        self.assertIn("is not paused", out)
        rc, out = self.run_cli("reset", "limits.per_day", "--place",
                               f"server:{G1}", "--yes")
        self.assertEqual(rc, 0, out)
        self.assertIsNone(value(self.cfg, S1, "limits.per_day"))
        # the whole preset was one group: undo reverts the rest
        self.assertEqual(len({h.get("group") for h in
                              doc(self.cfg)["history"]}), 4)

    def test_errors(self):
        for args in (["set", "who.replies", "loud", "--place", "dms"],
                     ["set", "who.replies", "off", "--place", "nowhere"],
                     ["set", "who.replies", "off", "--place",
                      f"category:{G1}:{CAT}"],
                     ["set", "privacy.memory", "nothing", "--place",
                      f"channel:{G1}:{CH}"],
                     ["set", "volume", "11", "--place", "dms"]):
            rc, out = self.run_cli(*args)
            self.assertEqual(rc, 1, (args, out))
        self.assertEqual(doc(self.cfg)["values"], {})
        rc, out = self.run_cli("set", "who.replies", "off")
        self.assertEqual(rc, 2)   # --place is required
        rc, out = self.run_cli("validate")
        self.assertEqual(rc, 0, out)


# ---------------------------------------------------------------------------
# 11. The current /settings still works on the new engine
# ---------------------------------------------------------------------------
class SettingsUIStillWorks(unittest.TestCase):
    def test_dms_page_with_a_list(self):
        import settings_ui as UI
        cfg = config(self, BASE + f'dms:\n  who:\n    dms_from: ["{SAM}"]\n')
        ctx = FakeCtx(cfg)
        ix = {"type": 3, "id": "1", "token": "t", "user": {"id": OWNER},
              "data": {"custom_id": "mdset:d:d:who", "component_type": 2}}
        r = UI.handle(ix, ctx)
        self.assertEqual(r["type"], 7, r)
        self.assertIn("DMs from", r["data"]["content"])
        self.assertEqual(ctx.logs, [])
        ix["data"] = {"custom_id": "mdset:s:d:df", "component_type": 3,
                      "values": ["owners"]}
        r = UI.handle(ix, ctx)
        self.assertIn("Saved", r["data"]["content"])
        self.assertEqual(value(cfg, DMS, "who.dms_from"), "owners")
        # an admin's /settings write is recorded as an admin's
        ix = {"type": 3, "id": "1", "token": "t", "guild_id": G1,
              "member": {"user": {"id": ADMIN}, "permissions": str(1 << 5)},
              "channel_id": CH, "data": {"custom_id":
                                         f"mdset:s:c{G1}.{CH}:r",
                                         "component_type": 3,
                                         "values": ["off"]}}
        r = UI.handle(ix, ctx)
        self.assertIn("Saved", r["data"]["content"])
        self.assertEqual(doc(cfg)["values"][KS_GEN]["role"], "admin")
        self.assertEqual(MS.undo_plan(load(cfg), S1, True, admin_gid=G1),
                         [C(P.channel(G1, CH), "who.replies", "inherit",
                            path=("servers", G1, "channels", CH))])



# ---------------------------------------------------------------------------
# 12. Review findings (2026-10-19): one regression test each
# ---------------------------------------------------------------------------
def admin_apply(cfg, changes, gid=G1, info=INFO):
    return MS.apply(cfg, changes, ADMIN, plugins_dir=PLUGINS, role="admin",
                    admin_gid=gid, info=info)


class ReviewHigh(unittest.TestCase):
    def test_h1_owner_resetting_an_admin_value_makes_it_the_owners(self):
        """poc1: an owner re-setting what an admin set is not a no-op; the
        entry becomes the owners', so the admin cannot revert it."""
        cfg = config(self, BASE + "all_servers:\n  who:\n    replies: "
                     "conversational\n")
        admin_apply(cfg, [C(S1, "who.replies", "off")])
        r = apply(cfg, [C(S1, "who.replies", "off")])          # owner
        self.assertTrue(r.changed)
        e = doc(cfg)["values"][KS]
        self.assertNotIn("role", e)
        self.assertNotIn("below", e)
        with self.assertRaises(MS.SettingsError):
            admin_apply(cfg, [C(S1, "who.replies", "inherit")])
        self.assertEqual(value(cfg, S1, "who.replies"), "off")
        # Never answer
        cfg = config(self)
        admin_apply(cfg, [C(S1, "who.ignore", [SPAM])])
        self.assertTrue(apply(cfg, [C(S1, "who.ignore", [SPAM])]).changed)
        for v in ([], "inherit"):
            with self.assertRaises(MS.SettingsError):
                admin_apply(cfg, [C(S1, "who.ignore", v)])
        self.assertEqual(value(cfg, S1, "who.ignore"), [SPAM])
        # an Extra
        cfg = config(self, BASE + "all_servers:\n  plugins:\n    media:\n"
                     "      enabled: on\n")
        admin_apply(cfg, [C(S1, "plugins.media.enabled", False)])
        self.assertTrue(apply(cfg, [C(S1, "plugins.media.enabled",
                                      False)]).changed)
        with self.assertRaises(MS.SettingsError):
            admin_apply(cfg, [C(S1, "plugins.media.enabled", "inherit")])
        self.assertIs(value(cfg, S1, "plugins.media.enabled"), False)
        # the legacy set_value path (today's /settings)
        cfg = config(self, BASE + "all_servers:\n  who:\n    replies: "
                     "conversational\n")
        MS.set_value(cfg, KS, "off", ADMIN, PLUGINS)
        self.assertEqual(MS.set_value(cfg, KS, "off", OWNER, PLUGINS), "off")
        with self.assertRaises(MS.SettingsError):
            admin_apply(cfg, [C(S1, "who.replies", "inherit")])
        with self.assertRaises(MS.SettingsError):
            MS.set_value(cfg, KS, "inherit", ADMIN, PLUGINS)

    def test_h2_admin_cannot_drop_an_owners_pin(self):
        """poc4: resetting an owner's entry was allowed whenever it changed
        nothing at that moment; a later parent change then leaked."""
        cfg = config(self, BASE + "all_servers:\n  who:\n    replies: "
                     "conversational\n")
        apply(cfg, [C(RND, "who.replies", "when-addressed")])
        apply(cfg, [C(S1, "who.replies", "when-addressed")])
        with self.assertRaises(MS.SettingsError):
            admin_apply(cfg, [C(RND, "who.replies", "inherit")])
        apply(cfg, [C(S1, "who.replies", "inherit")])
        self.assertEqual(value(cfg, RND, "who.replies"), "when-addressed")
        # quieter over the pin is fine, and stays bounded by it
        admin_apply(cfg, [C(RND, "who.replies", "off")])
        admin_apply(cfg, [C(RND, "who.replies", "inherit")])
        self.assertEqual(value(cfg, RND, "who.replies"), "when-addressed")
        # a pin in config.yaml is a pin too
        cfg = config(self, BASE + f'    channels:\n      "{CH2}":\n'
                     "        replies: when-addressed\n")
        with self.assertRaises(MS.SettingsError):
            admin_apply(cfg, [C(RND, "who.replies", "inherit")])

    def test_h2_seed257(self):
        """poc3: category off by an admin, an owner's channel pin, then the
        admin resets the pin, sets it off, and undoes."""
        cfg = config(self, BASE + "all_servers:\n  who:\n    replies: "
                     "when-addressed\n")
        CATP = P.channel(G1, CAT)
        admin_apply(cfg, [C(CATP, "who.replies", "off")])
        apply(cfg, [C(RND, "who.replies", "when-addressed")])
        with self.assertRaises(MS.SettingsError):
            admin_apply(cfg, [C(RND, "who.replies", "inherit")])
        admin_apply(cfg, [C(RND, "who.replies", "off")])
        apply(cfg, [C(DEF, "who.replies", "off")])
        MS.undo(cfg, S1, ADMIN, PLUGINS, children=True, admin_gid=G1,
                info=INFO)
        self.assertEqual(value(cfg, RND, "who.replies"), "when-addressed")

    def test_h3_ui_admin_undo_never_reverts_an_owners_group(self):
        """poc2: an admin's Undo on the server page reverted an owner's
        whole group (DMs back to anyone)."""
        import settings_ui as UI
        cfg = config(self, BASE + "dms:\n  who:\n    dms_from: anyone\n"
                     "all_servers:\n  who:\n    replies: when-addressed\n")
        apply(cfg, [C(DMS, "who.dms_from", "owners"),
                    C(S1, "who.replies", "conversational")])
        ix = {"type": 3, "id": "1", "token": "t", "guild_id": G1,
              "member": {"user": {"id": ADMIN},
                         "permissions": str(1 << 5)},
              "channel_id": CH, "data": {"custom_id": f"mdset:u:s{G1}",
                                         "component_type": 2}}
        r = UI.handle(ix, FakeCtx(cfg))
        self.assertIn("Nothing to undo", r["data"]["content"])
        self.assertEqual(value(cfg, DMS, "who.dms_from"), "owners")
        self.assertEqual(value(cfg, S1, "who.replies"), "conversational")
        # the admin's own change is undone, and only that
        admin_apply(cfg, [C(GEN, "who.replies", "off")])
        ix["data"] = {"custom_id": f"mdset:u:c{G1}.{CH}",
                      "component_type": 2}
        r = UI.handle(ix, FakeCtx(cfg))
        self.assertIn("Undone", r["data"]["content"])
        self.assertEqual(value(cfg, GEN, "who.replies"), "conversational")
        self.assertEqual(value(cfg, DMS, "who.dms_from"), "owners")

    def test_h3_ui_owner_undo_judges_the_whole_group(self):
        import settings_ui as UI
        cfg = config(self, BASE + "dms:\n  who:\n    dms_from: anyone\n")
        apply(cfg, [C(DMS, "who.dms_from", "owners"),
                    C(S1, "style.length", "brief")])
        ix = {"type": 3, "id": "1", "token": "t", "guild_id": G1,
              "member": {"user": {"id": OWNER}, "permissions": "0"},
              "channel_id": CH, "data": {"custom_id": f"mdset:u:s{G1}",
                                         "component_type": 2}}
        r = UI.handle(ix, FakeCtx(cfg))
        self.assertIn("Are you sure", r["data"]["content"])
        self.assertIn("Anyone on Discord", r["data"]["content"])
        self.assertEqual(value(cfg, DMS, "who.dms_from"), "owners")

    def test_h3_roles_are_explicit(self):
        cfg = config(self, BASE + "dms:\n  who:\n    dms_from: anyone\n")
        with self.assertRaises(MS.SettingsError):
            MS.apply(cfg, [C(S1, "style.length", "brief")], OWNER,
                     plugins_dir=PLUGINS)
        res = apply(cfg, [C(DMS, "who.dms_from", "owners")])
        for role in (None, "admin"):
            with self.assertRaises(MS.SettingsError):
                MS.undo_group(cfg, res.group, ADMIN, PLUGINS, role=role)
        self.assertEqual(value(cfg, DMS, "who.dms_from"), "owners")
        with self.assertRaises(MS.SettingsError):
            MS.undo_changes(cfg, DMS, ADMIN, PLUGINS, role="admin")


class ReviewMedium(unittest.TestCase):
    def test_m4_two_processes_lose_nothing(self):
        """poc6: the CLI and the monitor write at once."""
        import multiprocessing as mp
        cfg = config(self)
        ctx = mp.get_context("fork")
        procs = [ctx.Process(target=_writer, args=(cfg, g, 25))
                 for g in (G1, G2)]
        for pr in procs:
            pr.start()
        for pr in procs:
            pr.join(60)
        self.assertEqual([pr.exitcode for pr in procs], [0, 0])
        lines = [h for h in doc(cfg)["history"]
                 if h.get("key", "").endswith("limits.per_day")]
        self.assertEqual(len(lines), 50)
        self.assertEqual(value(cfg, S1, "limits.per_day"), 1024)
        self.assertEqual(value(cfg, S2, "limits.per_day"), 1024)

    def test_m5_unreadable_overlay_is_never_wiped(self):
        for bad in ("values: [\n", "- a list\n", "values: 5\n"):
            cfg = config(self, overlay=bad)
            ov = MS.overlay_path_for(cfg)
            st = load(cfg)       # still loads (the bot keeps running)
            self.assertEqual(st.overlay, {})
            for fn in (lambda: apply(cfg, [C(S1, "who.replies", "off")]),
                       lambda: MS.set_value(cfg, KS, "off", OWNER, PLUGINS),
                       lambda: MS.undo(cfg, S1, OWNER, PLUGINS),
                       lambda: MS.undo_group(cfg, "g1", OWNER, PLUGINS,
                                             role="owner")):
                with self.assertRaises(MS.SettingsError) as e:
                    fn()
                self.assertIn(MS.OVERLAY_NAME, str(e.exception))
            self.assertEqual(open(ov).read(), bad)
        cfg = config(self, overlay="values: [\n")
        r = subprocess.run([sys.executable, os.path.join(MUT, "bin",
                                                         "settings.py"),
                            "--config", cfg, "set", "who.replies", "off",
                            "--place", f"server:{G1}"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn("cannot be read", r.stderr)

    def test_m6_owner_tightening_reaches_admin_entries(self):
        """poc5: an owner's pause reaches a channel an admin set."""
        cfg = config(self, BASE + "all_servers:\n  who:\n    replies: "
                     "conversational\n")
        admin_apply(cfg, [C(GEN, "who.replies", "when-addressed")])
        apply(cfg, MS.pause_changes(S1))
        self.assertEqual(value(cfg, GEN, "who.replies"), "off")
        MS.undo(cfg, S1, OWNER, PLUGINS)
        self.assertEqual(value(cfg, GEN, "who.replies"), "when-addressed")
        # an admin's server entry does not outlive Pause everywhere
        admin_apply(cfg, [C(S1, "who.replies", "when-addressed")])
        st = load(cfg)
        a = MS.assess(st, MS.pause_changes(DEF), INFO)
        self.assertNotIn("keeps answering", a.warnings[0])
        apply(cfg, MS.pause_changes(DEF))
        self.assertEqual(value(cfg, S1, "who.replies"), "off")
        # lists join; Extras need both on
        cfg = config(self, BASE + f'    who:\n      ignore: ["{SPAM}"]\n'
                     "all_servers:\n  plugins:\n    media:\n      enabled: "
                     "on\n")
        admin_apply(cfg, [C(S1, "who.ignore", [SPAM, SAM])])
        apply(cfg, [C(S1, "who.ignore", [SPAM, ANN])])
        self.assertEqual(value(cfg, S1, "who.ignore"), [SPAM, ANN])
        admin_apply(cfg, [C(GEN, "plugins.media.enabled", False)])
        apply(cfg, [C(DEF, "plugins.media.enabled", False)])
        apply(cfg, [C(DEF, "plugins.media.enabled", True)])
        self.assertIs(value(cfg, GEN, "plugins.media.enabled"), False)

    def test_m7_listed_people_are_polled_and_swept(self):
        text = BASE.replace("servers:", "everywhere:\n  privacy:\n    "
                            "never_share: [\"secret\"]\nservers:")
        m = load_monitor(self, text + f'dms:\n  who:\n    dms_from: '
                         f'["{SAM}"]\n')
        self.assertIn(SAM, m.dm_poll_user_ids())
        self.assertTrue(m.dm_swept_author(SAM))
        self.assertFalse(m.dm_swept_author(ANN))
        chans = dict(m.list_dm_channels())
        sam_dm = "4" + SAM[1:]
        own_dm = "4" + OWNER[1:]
        self.assertIn(sam_dm, chans)
        self.assertEqual(m.DM_CHANNEL_USER[sam_dm], SAM)
        # posts were only channel lookups (nothing sent)
        self.assertTrue(all(p == "/users/@me/channels"
                            for p, _d in m.api.posts))
        # never_share: still private for the owner's DM only
        st = {"hold_released": {}, "item_meta": {}}
        self.assertFalse(m.never_share_hit(st, {"channel_id": own_dm}, None,
                                           ["the secret"]))
        self.assertTrue(m.never_share_hit(st, {"channel_id": sam_dm}, None,
                                          ["the secret"]))
        # the sweep recovers a lost DM from Sam, not one from Ann
        import time as _t
        old = int(((_t.time() - 600) * 1000 - 1420070400000)) << 22
        sam_msg = {"id": str(old), "channel_id": sam_dm, "content": "hi",
                   "author": {"id": SAM, "username": "sam"}, "type": 0,
                   "timestamp": "2026-10-19T00:00:00+00:00", "mentions": []}
        ann_msg = dict(sam_msg, id=str(old + 1),
                       author={"id": ANN, "username": "ann"})
        m.api.history[sam_dm] = [sam_msg, ann_msg]
        with contextlib.redirect_stdout(io.StringIO()):
            state = m.load_state()
            q = []
            m.sweep_mentions(state, q, {}, dms=True)
        self.assertEqual([i.get("message_id") for i in q], [sam_msg["id"]])
        # a refused DM from a listed person is ledgered (never answered
        # late by the sweep)
        with open(m.cfg_path, "a") as f:
            f.write("    replies: 'off'\n")
        os.utime(m.cfg_path, (_t.time() + 5, _t.time() + 5))
        with contextlib.redirect_stdout(io.StringIO()):
            m.note_dm_refused(dict(sam_msg, id="123456789012345679"))
        self.assertEqual(m._ledger_load()["entries"]["123456789012345679"]
                         ["status"], "skipped")


def _writer(cfg, gid, n):
    for i in range(n):
        MS.apply(cfg, [C(P.server(gid), "limits.per_day", 1000 + i)], OWNER,
                 plugins_dir=PLUGINS, role="owner")


class ReviewLow(unittest.TestCase):
    def test_assess_fails_closed_without_counts(self):
        cfg = config(self, BASE.replace("servers:", "all_servers:\n  who:\n"
                                        "    replies: when-addressed\n"
                                        "servers:"))
        st = load(cfg)
        a = MS.assess(st, [C(S1, "who.replies", "conversational")])
        self.assertTrue(a.risky)
        self.assertIn("size unknown", a.warnings[0])
        st = load(config(self, BASE + "    limits:\n      per_day: 9\n"))
        a = MS.assess(st, [C(S1, "limits.per_day", None)])
        self.assertTrue(a.risky)
        a = MS.assess(st, [C(S1, "privacy.memory", "everything")])
        self.assertIn("size unknown", a.warnings[0])

    def test_assess_dm_details(self):
        st = load(config(self, BASE + "dms:\n  who:\n    dms_from: anyone\n"
                         "    bots: ignore\n"))
        a = MS.assess(st, [C(DMS, "who.bots", "when-addressed")], INFO)
        self.assertTrue(a.risky)
        self.assertIn("Other bots can DM", a.warnings[0])
        st = load(config(self, BASE + "dms:\n  who:\n    dms_from: "
                         "shared-servers\n"))
        a = MS.assess(st, [C(DMS, "who.dms_from", [SAM])], INFO, NAMES)
        self.assertEqual(a.warnings, ["@Sam can DM the bot and get "
                                      "answers."])

    def test_no_doubled_server_name(self):
        cfg = config(self, BASE.replace("servers:", "all_servers:\n  who:\n"
                                        "    replies: when-addressed\n"
                                        "servers:"))
        a = MS.assess(load(cfg), [C(P.channel(G1, "70000000000000000777"),
                                    "who.replies", "conversational")],
                      {"guilds": INFO["guilds"]})
        self.assertIn("channel 70000000000000000777 in Lounge (1,240 "
                      "members)", a.warnings[0])
        self.assertNotIn("in Lounge in Lounge", a.warnings[0])

    def test_group_expansion_skips_keys_changed_since(self):
        cfg = config(self)
        apply(cfg, [C(S1, "who.replies", "off"),
                    C(S1, "style.length", "brief")])
        apply(cfg, [C(S1, "style.length", "detailed")])
        apply(cfg, [C(S1, "style.length", "brief")])   # same as the group
        MS.undo(cfg, S1, OWNER, PLUGINS)   # the newest: back to detailed
        self.assertEqual(value(cfg, S1, "style.length"), "detailed")
        plan = MS.undo_plan(load(cfg), S1)
        self.assertEqual([c.key for c in plan], ["style.length"])

    def test_history_trim_keeps_groups_whole_and_resume_value(self):
        cfg = config(self, BASE + "    who:\n      replies: "
                     "when-addressed\n")
        apply(cfg, MS.pause_changes(S1))
        for i in range(60):
            apply(cfg, [C(S2, "limits.per_day", 10 + i),
                        C(S3, "limits.per_day", 10 + i)])
        hist = doc(cfg)["history"]
        self.assertLessEqual(len(hist), MS.HISTORY_CAP)
        groups = [h.get("group") for h in hist]
        self.assertNotEqual(groups[0], None)
        self.assertEqual(groups.count(groups[0]), 2)   # not split
        self.assertFalse(any(h.get("key") == KS for h in hist))
        st = load(cfg)
        self.assertEqual(MS.resume_changes(st, S1),
                         [C(S1, "who.replies", "when-addressed")])

    def test_cli_details(self):
        cfg = config(self, BASE + f'    who:\n      ignore: ["{SPAM}"]\n')

        def cli(*args):
            r = subprocess.run([sys.executable, os.path.join(
                MUT, "bin", "settings.py"), "--config", cfg] + list(args),
                capture_output=True, text=True, timeout=60)
            return r.returncode, r.stdout + r.stderr
        for word in ("Nobody", "none", ""):
            rc, out = cli("set", "Never answer", word, "--place",
                          f"server:{G1}")
            self.assertEqual(rc, 0, out)
            self.assertEqual(value(cfg, S1, "who.ignore"), [])
            cli("undo", "--place", f"server:{G1}")
        for bad in ("abc", CH):
            rc, out = cli("set", "who.replies", "off", "--place",
                          f"channel:{G1}:{CH}", "--category", bad)
            self.assertEqual(rc, 1, out)
        # --by is a name: a user id (an owner's or anyone's) is refused,
        # so a CLI change can never pass for a Discord-side one.
        for by in (ADMIN, OWNER, " "):
            rc, out = cli("set", "style.length", "brief", "--place",
                          f"server:{G1}", "--by", by)
            self.assertEqual(rc, 2, out)
        rc, out = cli("set", "style.length", "brief", "--place",
                      f"server:{G1}", "--by", "helper")
        rc, out = cli("undo", "--place", f"server:{G1}", "--by", "helper")
        self.assertEqual(rc, 0, out)
        for h in doc(cfg)["history"]:
            self.assertNotIn("role", h)
        self.assertEqual(doc(cfg)["values"], {})

    def test_undo_group_names_its_undo(self):
        cfg = config(self)
        apply(cfg, [C(S1, "who.replies", "off")], group="nl-555")
        MS.undo_group(cfg, "nl-555", OWNER, PLUGINS, role="owner",
                      undo_id="nl-undo-555")
        self.assertEqual(doc(cfg)["history"][-1]["group"], "nl-undo-555")
        self.assertTrue(doc(cfg)["history"][-1]["undo"])

    def test_group_undo_plan(self):
        cfg = config(self)
        apply(cfg, [C(GEN, "who.replies", "off"),
                    C(S1, "style.length", "brief")], group="nl-600")
        plan = MS.group_undo_plan(load(cfg), "nl-600")
        self.assertEqual([(c.place, c.key, c.value) for c in plan],
                         [(S1, "style.length", "inherit"),
                          (P.channel(G1, CH), "who.replies", "inherit")])
        self.assertEqual(plan[1].path, ("servers", G1, "channels", CH))
        # Judged by outcome: back to Chatty in Lounge (1,240 members).
        risk = MS.assess(load(cfg), plan, INFO)
        self.assertTrue(any("Chatty in #general" in w
                            for w in risk.warnings), risk.warnings)
        self.assertEqual(MS.group_undo_plan(load(cfg), "nl-nope"), [])
        MS.undo_group(cfg, "nl-600", OWNER, PLUGINS, role="owner")
        self.assertEqual(value(cfg, GEN, "who.replies"), "conversational")

    def test_undo_changes_names_its_undo(self):
        cfg = config(self)
        apply(cfg, [C(S1, "who.replies", "off")])
        MS.undo_changes(cfg, S1, OWNER, PLUGINS, role="owner",
                        undo_id="nl-700")
        last = doc(cfg)["history"][-1]
        self.assertEqual((last["group"], last["undo"]), ("nl-700", True))
        with self.assertRaises(MS.SettingsError):
            MS.undo_changes(cfg, S1, OWNER, PLUGINS, role="owner",
                            undo_id="not a group id!")

    def test_no_change_is_a_sentinel(self):
        self.assertNotEqual(MS.NO_CHANGE, "no change")
        self.assertFalse(isinstance(MS.NO_CHANGE, str))
        cfg = config(self)
        apply(cfg, [C(S1, "style.notes", "no change")])
        self.assertEqual(value(cfg, S1, "style.notes"), "no change")
        self.assertIsNot(MS.set_value(cfg, f'servers."{G1}".style.notes',
                                      "x", OWNER, PLUGINS), MS.NO_CHANGE)


# ---------------------------------------------------------------------------
# 13. Fuzz: server admins can never leave the bot louder than an
#     owners-only replay of the same owner actions.
# ---------------------------------------------------------------------------
_RANK = {"off": 0, "when-addressed": 1, "conversational": 2}
_UNL = "70000000000000000777"
_FUZZ_SCOPES = {
    "server": MS.Scope.server(G1),
    "#general": MS.Scope.server(G1, channel_id=CH, category_id=CAT),
    "#random": MS.Scope.server(G1, channel_id=CH2),
    "unlisted": MS.Scope.server(G1, channel_id=_UNL),
    "unlisted-in-cat": MS.Scope.server(G1, channel_id=_UNL,
                                       category_id=CAT),
}


class FuzzAdmin(unittest.TestCase):
    SEEDS = int(os.environ.get("FUZZ_SEEDS", "500"))
    STEPS = 10

    def eff(self, st):
        return {n: st.get("who.replies", sc)
                for n, sc in _FUZZ_SCOPES.items()}

    def test_admins_never_louder_than_owners_alone(self):
        import random
        CATP = P.channel(G1, CAT)
        oplaces = [DEF, S1, GEN, RND, CATP]
        aplaces = [S1, GEN, RND, CATP]
        vals = ["off", "when-addressed", "conversational", "inherit"]
        text = BASE + "all_servers:\n  who:\n    replies: when-addressed\n"
        for seed in range(self.SEEDS):
            rnd = random.Random(seed)
            real, truth = config(self, text), config(self, text)
            log = []
            for _ in range(self.STEPS):
                kind = rnd.choice(["owner", "admin", "admin", "aundo"])
                try:
                    if kind == "owner":
                        ch = [C(rnd.choice(oplaces), "who.replies",
                                rnd.choice(vals))]
                        # an owner's action the two worlds would record
                        # differently (the no-op guard looks at what is
                        # in force) is left out of both
                        nr = [p.noop for p in MS.plan_changes(load(real),
                                                              ch)]
                        nt = [p.noop for p in MS.plan_changes(load(truth),
                                                              ch)]
                        if nr != nt:
                            continue
                        apply(real, ch)
                        apply(truth, ch)
                    elif kind == "admin":
                        ch = [C(rnd.choice(aplaces), "who.replies",
                                rnd.choice(vals))]
                        admin_apply(real, ch)
                    else:
                        ch = "undo"
                        MS.undo(real, S1, ADMIN, PLUGINS, children=True,
                                admin_gid=G1, info=INFO)
                    log.append(f"{kind} {ch}")
                except MS.SettingsError:
                    log.append(f"{kind} refused")
                er, et = self.eff(load(real)), self.eff(load(truth))
                bad = {n: (er[n], et[n]) for n in _FUZZ_SCOPES
                       if _RANK[er[n]] > _RANK[et[n]]}
                self.assertEqual(bad, {}, f"seed {seed}: " + "; ".join(log))


if __name__ == "__main__":
    unittest.main(verbosity=1)
