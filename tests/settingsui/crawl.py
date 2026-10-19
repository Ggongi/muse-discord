"""The /settings snapshot crawler: from every entry point (spec §1), click
every control a page renders, check every response against Discord's
limits and the copy rules (spec §7), and render each page as text.

    python3 tests/settingsui/crawl.py            # check against goldens
    python3 tests/settingsui/crawl.py --update   # rewrite the goldens

The goldens are tests/settingsui/snapshots/<entry>.txt; test_crawl.py
runs the same crawl and diffs them. Navigation is followed breadth
first; every write control is clicked on a copy of the settings (and
the controls on its answer, one level deeper: confirm, Undo, Try
again), then the settings are put back. The 60 filler servers and 74
filler channels count as one each.
MUT_DIR = the package tree under test."""
import argparse
import difflib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import harness as H  # noqa: E402
from harness import UI  # noqa: E402

SNAPSHOTS = os.path.join(HERE, "snapshots")
SELECT_TYPES = (3, 5, 6, 7, 8)
NAV_VERBS = ("pg", "go", "gc")
RAW_KEYS = re.compile(r"\b(who|style|privacy|limits|plugins)\.")
RAW_VALUES = re.compile(r"\b(conversational|when-addressed|none|"
                        r"public-only)\b")
BARE_ID = re.compile(r"\d{15,25}")
# Text Displays: the only links are ours (messages), names never ping or
# mention anyone (spec §7, review B #4).
FOREIGN_LINK = re.compile(r"(?<!\\)\]\((?!https://discord\.com/channels/)")
MASS_PING = re.compile(r"@(everyone|here)\b")
RAW_MENTION = re.compile(r"<(@|@&|#)(?!\d{15,25}>)")
STRIP = re.compile(r"<(@|@&|#)\d+>|\]\(https://discord\.com/[^)]*\)")
NAMES = {H.OWNER: "OWNER", H.OWNER2: "OWNER2", H.ADMIN: "ADMIN",
         H.MEMBER: "MEMBER", H.SAM: "SAM", H.KAI: "KAI", H.ANA: "ANA",
         H.SPAM: "SPAM", H.BOB: "BOB", H.OTHERBOT: "OTHERBOT", H.BOT: "BOT",
         H.ROLE: "ROLE", H.G1: "G1", H.G2: "G2", H.G3: "G3", H.CAT: "CAT",
         H.GENERAL: "GENERAL", H.MUSECHAT: "MUSECHAT",
         H.WELCOME: "WELCOME", H.LOBBY: "LOBBY", H.HIDDEN: "HIDDEN",
         H.THREAD: "THREAD", H.HOMEWORK: "HOMEWORK", H.ART: "ART"}
FILLER = re.compile(r"(6[12]000000000000000\d{3}|7[12]000000000000000\d{3})")
COLOURS = {UI.BLURPLE: "blurple", UI.GREY: "grey", UI.AMBER: "amber",
           UI.RED: "red"}
STYLES = {1: "primary", 2: "", 3: "success", 4: "danger"}


# ---------------------------------------------------------------------------
# Checks (spec §7)
# ---------------------------------------------------------------------------
def visible(resp):
    """Every piece of text a person sees: text displays, labels, option
    labels and descriptions, placeholders, modal titles."""
    out = []
    data = resp.get("data") or {}
    if resp.get("type") == 9:
        out.append(data.get("title") or "")
    for c in H.walk(data.get("components")):
        for k in ("content", "label", "placeholder", "description"):
            if isinstance(c.get(k), str):
                out.append(c[k])
        for o in c.get("options") or []:
            out.append(o.get("label") or "")
            out.append(o.get("description") or "")
    return out


def budget(resp):
    return sum(len(t) for t in visible(resp))


def check(resp, where):
    """Problems with one response (empty: fine)."""
    bad = []

    def no(cond, msg):
        if cond:
            bad.append(f"{where}: {msg}")
    if not isinstance(resp, dict):
        return [f"{where}: not a dict"]
    t = resp.get("type")
    data = resp.get("data") or {}
    no(t not in (4, 7, 9), f"type {t}")
    if t == 4:
        no(data.get("flags") != 64 | 32768, f"flags {data.get('flags')}")
    if t == 7:
        no(not (data.get("flags", 0) & 32768), "type 7 without V2 flag")
    if t in (4, 7):
        no("content" in data or "embeds" in data, "content or embeds")
        no(data.get("allowed_mentions") != {"parse": []}, "mentions")
    if t == 9:
        no(not data.get("custom_id", "").startswith("mdset:2:"),
           f"modal id {data.get('custom_id')}")
        no(len(data.get("custom_id", "")) > 100, "modal id too long")
        no(not 1 <= len(data.get("title", "")) <= 45,
           f"modal title {data.get('title')!r}")
        comps = data.get("components") or []
        no(not 1 <= len(comps) <= 5, f"{len(comps)} modal components")
        for c in comps:
            no(c.get("type") not in (10, 18), f"modal top type {c}")
            if c.get("type") == 18:
                no(not 1 <= len(c.get("label", "")) <= 45,
                   f"modal label {c.get('label')!r}")
                no(len(c.get("description", "")) > 100,
                   "modal label description")
                inner = c.get("component") or {}
                no(inner.get("type") not in (3, 4), f"label holds {inner}")
                if inner.get("type") == 4:
                    no("label" in inner, "text input with its own label")
                    no(len(inner.get("value", "")) > inner.get(
                        "max_length", 4000), "prefill too long")
                    no(str(inner.get("value", "")).lower() == "none",
                       "prefilled none")
    total = UI.count_components(data.get("components"))
    no(total > UI.MAX_COMPONENTS, f"{total} components")
    no(budget(resp) > UI.TEXT_BUDGET, f"{budget(resp)} characters")
    ids = []
    for c in H.walk(data.get("components")):
        ct = c.get("type")
        if ct == 1:
            kids = c.get("components") or []
            sels = [k for k in kids if k.get("type") in SELECT_TYPES]
            no(not kids, "empty row")
            no(sels and len(kids) != 1, "a select shares its row")
            no(len(kids) > 5, f"{len(kids)} in a row")
            no(any(k.get("type") not in (2,) + SELECT_TYPES for k in kids),
               "row holds a non-control")
        if ct == 9:
            no(not isinstance(c.get("accessory"), dict), "section with no "
               "accessory")
            no(not 1 <= len(c.get("components") or []) <= 3,
               "section texts")
        if ct == 10:
            no(not c.get("content"), "empty text")
        if ct == 2:
            no(not 1 <= len(c.get("label", "")) <= 80,
               f"button label {c.get('label')!r}")
            no(c.get("style") not in (1, 2, 3, 4), "button style")
        if ct in SELECT_TYPES:
            no(len(c.get("placeholder", "")) > 150, "placeholder")
            mn, mx = c.get("min_values", 1), c.get("max_values", 1)
            no(not 0 <= mn <= mx <= 25, f"min/max {mn}/{mx}")
            no(len(c.get("default_values") or []) > mx, "defaults")
        if ct == 3:
            opts = c.get("options") or []
            no(not 1 <= len(opts) <= 25, f"{len(opts)} options")
            vals = [o.get("value") for o in opts]
            no(len(vals) != len(set(vals)), "duplicate option values")
            no(sum(1 for o in opts if o.get("default")) > c.get(
                "max_values", 1), "too many defaults")
            for o in opts:
                no(not 1 <= len(o.get("label", "")) <= 100, "option label")
                no(not 1 <= len(o.get("value", "")) <= 100, "option value")
                no(len(o.get("description", "")) > 100, "option desc")
        if "custom_id" in c and t != 9:
            cid = c["custom_id"]
            ids.append(cid)
            no(not cid.startswith("mdset:2:"), f"custom id {cid}")
            no(len(cid) > 100, f"custom id {len(cid)} chars: {cid}")
    no(len(ids) != len(set(ids)), "duplicate custom ids")
    for c in H.walk(data.get("components")):
        if c.get("type") != 10:
            continue
        t = c.get("content") or ""
        no(bool(FOREIGN_LINK.search(t)), f"a link a name made in {t!r}")
        no(bool(MASS_PING.search(t)), f"@everyone / @here in {t!r}")
        no(bool(RAW_MENTION.search(t)), f"a mention a name made in {t!r}")
    for line in visible(resp):
        no(bool(RAW_KEYS.search(line)), f"raw key in {line!r}")
        no(bool(RAW_VALUES.search(line)), f"raw value in {line!r}")
        no(bool(BARE_ID.search(STRIP.sub("", line))),
           f"bare id in {line!r}")
    return bad


# The spec's component counts (§2), banner or last-change slot included.
# (S4's own breakdown adds up to 30, not the 29 it states.)
SPEC_COUNTS = {"S1 channel": 25, "S2 server": 35, "S3 server more": 30,
               "S3 defaults more": 31, "S3 dms more": 22, "S4 extras": 30,
               "S5 defaults": 34, "S6 dms": 30, "S6 dms fallback": 33,
               "S7 bot-wide": 14, "S8a why": 21, "S8b card": 9,
               "S9 confirm": 6, "S10 preset": 9, "S12 quiet": 37,
               "S13 refusal": 2, "S14 notice": 2, "S14 expired": 4,
               "S15 servers": 7}


def screen(resp):
    """Which screen of the spec a response is (None: a modal)."""
    if resp.get("type") == 9:
        return None
    comps = (resp.get("data") or {}).get("components") or []
    cont = next((c for c in comps if c.get("type") == 17), None)
    if cont is None:
        return None
    first = next((c.get("content", "") for c in H.walk(cont["components"])
                  if c.get("type") == 10), "")
    ids = " ".join(c.get("custom_id", "") for c in H.walk(comps))
    if first.startswith("## Quiet the bot"):
        return "S12 quiet"
    if first.startswith("## Why no reply? ·"):
        return "S8a why"
    if first.startswith("## Why no reply?"):
        return "S8b card"
    if first.startswith("## Check before"):
        return "S9 confirm"
    if first.startswith(("## Apply the", "## ")) and " preset" in \
            first.split("\n")[0]:
        return "S10 preset"
    if "› More settings" in first:
        return ("S3 defaults more" if first.startswith("## Defaults")
                else "S3 dms more" if first.startswith("## DMs")
                else "S3 server more")
    if "› Extras" in first:
        return "S4 extras"
    if first.startswith("## Other servers"):
        return "S15 servers"
    if first.startswith("## Bot-wide"):
        return "S7 bot-wide"
    if first.startswith("## DMs"):
        return "S6 dms fallback" if ":rm:d:df" in ids or \
            ":mod:d:id.df" in ids else "S6 dms"
    if first.startswith("## Defaults"):
        return "S5 defaults"
    if first.startswith("## #"):
        return "S1 channel"
    if first.startswith("## "):
        return "S2 server"
    if first == UI.EXPIRED:
        return "S14 expired"
    if first.startswith("Only the bot's owners"):
        return "S13 refusal"
    return "S14 notice"


def out_of_date(resp):
    return UI.OUT_OF_DATE in "\n".join(H.texts(resp))


# ---------------------------------------------------------------------------
# Text renders
# ---------------------------------------------------------------------------
def sym(s):
    s = str(s)
    for k in sorted(NAMES, key=len, reverse=True):
        s = s.replace(k, f"<{NAMES[k]}>")
    s = FILLER.sub("<FILLER>", s)
    return re.sub(r"\d{17,20}", "<ID>", s)


def render(resp):
    data = resp.get("data") or {}
    head = (f"type {resp.get('type')} · "
            f"{UI.count_components(data.get('components'))} components · "
            f"{budget(resp)} chars")
    lines = [head]
    if resp.get("type") == 9:
        lines.append(f"MODAL {sym(data.get('custom_id'))} "
                     f"“{data.get('title')}”")

    def text_lines(t, pad):
        for i, ln in enumerate(str(t).split("\n")):
            lines.append(pad + ("| " if i == 0 else "| ") + sym(ln))

    def ctl(c, pad):
        ct = c.get("type")
        if ct == 2:
            st = STYLES.get(c.get("style"), "")
            lines.append(f"{pad}[{c.get('label')}]"
                         + (f"({st})" if st else "")
                         + f" {sym(c.get('custom_id'))}")
        elif ct in SELECT_TYPES:
            kind = {3: "SELECT", 5: "USERS", 6: "ROLES", 7: "MENTIONABLE",
                    8: "CHANNELS"}[ct]
            extra = ""
            if c.get("default_values"):
                extra = " defaults=" + ",".join(
                    sym(d["id"]) for d in c["default_values"])
            lines.append(f"{pad}{kind} {sym(c.get('custom_id'))} "
                         f"“{c.get('placeholder', '')}” "
                         f"{c.get('min_values', 1)}-{c.get('max_values', 1)}"
                         + extra)
            for o in c.get("options") or []:
                lines.append(f"{pad}  {'*' if o.get('default') else '-'} "
                             f"{o['label']}"
                             + (f" — {o['description']}"
                                if o.get("description") else "")
                             + f"  ({sym(o['value'])})")
        elif ct == 4:
            lines.append(f"{pad}INPUT {c.get('custom_id')} style "
                         f"{c.get('style')} max {c.get('max_length')}"
                         + (f" value=“{sym(c['value'])}”" if c.get("value")
                            else "")
                         + (f" placeholder=“{c['placeholder']}”"
                            if c.get("placeholder") else ""))

    def comp(c, pad):
        ct = c.get("type")
        if ct == 17:
            col = COLOURS.get(c.get("accent_color"), "none")
            lines.append(f"{pad}C {col}")
            for x in c.get("components") or []:
                comp(x, pad + "  ")
        elif ct == 10:
            text_lines(c.get("content"), pad + "TD ")
        elif ct == 14:
            lines.append(f"{pad}SEP")
        elif ct == 9:
            for x in c.get("components") or []:
                text_lines(x.get("content"), pad + "SEC ")
            ctl(c.get("accessory") or {}, pad + "    ")
        elif ct == 1:
            lines.append(f"{pad}ROW")
            for x in c.get("components") or []:
                ctl(x, pad + "  ")
        elif ct == 18:
            lines.append(f"{pad}LABEL “{c.get('label')}”"
                         + (f" — {c['description']}"
                            if c.get("description") else ""))
            ctl(c.get("component") or {}, pad + "  ")
        else:
            lines.append(f"{pad}? {json.dumps(c)[:80]}")
    for c in data.get("components") or []:
        comp(c, "")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Clicking
# ---------------------------------------------------------------------------
class Actor:
    """Who is clicking, and where."""

    def __init__(self, user, guild=None, perms=None, channel=H.MUSECHAT):
        self.user, self.guild, self.perms, self.channel = \
            user, guild, perms, channel

    def click(self, ctx, custom_id, values=None, resolved=None,
              message=None):
        ctx.refresh()
        return UI.handle(H.component(
            self.user, custom_id, values, self.guild, self.perms,
            self.channel, resolved, message), ctx)

    def submit(self, ctx, custom_id, fields):
        ctx.refresh()
        return UI.handle(H.submit(self.user, custom_id, fields, self.guild,
                                  self.perms, self.channel), ctx)


def clicks_for(c):
    """[(values, resolved)] to try on one control."""
    ct = c.get("type")
    cid = c.get("custom_id", "")
    if ct == 2:
        return [(None, None)]
    if ct == 3:
        return [([o["value"]], None) for o in c.get("options") or []]
    if ct == 5:
        return [([H.SAM], H.resolved_for(5, [H.SAM]))]
    if ct == 7:
        if ":ig" in cid:
            return [([H.SAM, H.ROLE], H.resolved_for(7, [H.SAM, H.ROLE])),
                    ([], {})] if c.get("min_values", 1) == 0 else \
                [([H.SAM], H.resolved_for(7, [H.SAM]))]
        return [([H.SAM], H.resolved_for(7, [H.SAM]))]
    if ct == 8:
        target = H.LOBBY if "welcome" in cid else H.GENERAL
        return [([target], H.resolved_for(8, [target]))]
    return []


def modal_fields(resp):
    """Plausible answers for a modal: what it is prefilled with (a cap
    box gets a number, a search box a word)."""
    out = {}
    for c in (resp.get("data") or {}).get("components") or []:
        if c.get("type") != 18:
            continue
        inner = c["component"]
        if inner.get("type") == 4:
            v = inner.get("value") or ""
            if not v and inner.get("max_length") == 7:
                v = "12"
            if inner.get("custom_id") == "q":
                v = "room"
            if inner.get("custom_id") == "ids":
                v = f"<@{H.KAI}>"
            out[inner["custom_id"]] = v
        elif inner.get("type") == 3:
            d = [o["value"] for o in inner.get("options") or []
                 if o.get("default")]
            out[inner["custom_id"]] = d or [inner["options"][0]["value"]]
    return out


def canonical(custom_id, values):
    return FILLER.sub("F", custom_id) + "|" + ",".join(
        FILLER.sub("F", v) for v in values or [])


class Crawl:
    def __init__(self, ctx, actor, max_pages=5000, seen=None):
        self.ctx, self.actor = ctx, actor
        self.problems = []
        self.pages = []          # (path, render)
        self.seen = set() if seen is None else seen
        self.full = True         # render the next note in full
        self.screens = []        # (screen, components, path)
        self.max_pages = max_pages
        self.count = 0

    def note(self, path, resp):
        self.count += 1
        self.screens.append((screen(resp), UI.count_components(
            (resp.get("data") or {}).get("components")), path))
        for p in check(resp, path):
            self.problems.append(p)
        if out_of_date(resp):
            self.problems.append(f"{path}: answered out of date")
        if "went wrong" in "\n".join(H.texts(resp)):
            self.problems.append(f"{path}: crashed: {self.ctx.logs[-1:]}")
        text = render(resp)
        if not self.full and resp.get("type") != 9:
            # A write's answer: its banner and heading are enough for the
            # golden (every check above ran on the whole page).
            text = "\n".join(text.splitlines()[:4]) + "\n  …"
        self.pages.append((path, text))

    def do(self, path, c, values, resolved, message):
        cid = c["custom_id"]
        r = self.actor.click(self.ctx, cid, values, resolved, message)
        step = f"{path} > {sym(cid)}" + (f"={sym(','.join(values))}"
                                         if values else "")
        self.note(step, r)
        if r.get("type") == 9:
            fields = modal_fields(r)
            r2 = self.actor.submit(self.ctx, r["data"]["custom_id"], fields)
            self.note(step + " > submit", r2)
            return step + " > submit", r2
        return step, r

    def run(self, path, first):
        self.note(path, first)
        queue = [(path, first)]
        while queue and len(self.pages) < self.max_pages:
            path, resp = queue.pop(0)
            if resp.get("type") not in (4, 7):
                continue
            msg = {"id": "9", "flags": 64 | 32768,
                   "components": resp["data"].get("components")}
            for c in H.controls(resp):
                verb = c["custom_id"].split(":")[2]
                for values, resolved in clicks_for(c):
                    key = canonical(c["custom_id"], values)
                    if key in self.seen:
                        continue
                    self.seen.add(key)
                    if verb in NAV_VERBS:
                        step, r = self.do(path, c, values, resolved, msg)
                        queue.append((step, r))
                        continue
                    snap = self.ctx.snapshot()
                    self.full = False
                    try:
                        step, r = self.do(path, c, values, resolved, msg)
                        self.second(step, r)
                    finally:
                        self.full = True
                        self.ctx.restore(snap)
        return self

    def second(self, path, resp):
        """One level deeper after a write: the controls it added (Apply,
        Undo, Try again), each on its own copy."""
        if resp.get("type") not in (4, 7):
            return
        msg = {"id": "9", "flags": 64 | 32768,
               "components": resp["data"].get("components")}
        for c in H.controls(resp):
            verb = c["custom_id"].split(":")[2]
            if verb not in ("ok", "un", "re"):
                continue
            snap = self.ctx.snapshot()
            try:
                self.do(path, c, None, None, msg)
            finally:
                self.ctx.restore(snap)


# ---------------------------------------------------------------------------
# Entry points (spec §1)
# ---------------------------------------------------------------------------
def entries():
    """[(name, ctx factory, actor, first interaction)]."""
    O = H.OWNER
    MS_ = H.MEMBER
    out = [
        ("owner_channel", H.FakeCtx, Actor(O, H.G1, "0", H.MUSECHAT),
         H.command(O, H.G1, "0", H.MUSECHAT)),
        ("owner_thread", H.FakeCtx, Actor(O, H.G1, "0", H.THREAD),
         H.command(O, H.G1, "0", H.THREAD, 11, H.MUSECHAT)),
        ("owner_dm", H.FakeCtx, Actor(O), H.command(O)),
        ("owner_configure", H.FakeCtx, Actor(O),
         H.component(O, f"mdset:open:server:{H.G2}")),
        ("admin_panel", H.FakeCtx,
         Actor(H.ADMIN, H.G1, H.MANAGE, H.GENERAL),
         H.command(H.ADMIN, H.G1, H.MANAGE, H.GENERAL)),
        ("member", H.FakeCtx, Actor(MS_, H.G1, "0"),
         H.command(MS_, H.G1, "0")),
        ("member_dm", H.FakeCtx, Actor(MS_), H.command(MS_)),
        ("why_owner", H.FakeCtx, Actor(O, H.G1, "0"),
         H.context_command(O, H.OUTCOMES[0]["message_id"], author=H.SPAM)),
        ("why_admin", H.FakeCtx, Actor(H.ADMIN, H.G1, H.MANAGE),
         H.context_command(H.ADMIN, H.OUTCOMES[1]["message_id"],
                           perms=H.MANAGE, author=H.ANA)),
        ("why_member", H.FakeCtx, Actor(MS_, H.G1, "0"),
         H.context_command(MS_, H.OUTCOMES[0]["message_id"])),
        ("old_id", H.FakeCtx, Actor(O), H.component(O, "mdset:p:e")),
        ("expired", H.FakeCtx, Actor(O, H.G1, "0"),
         H.component(O, f"mdset:2:ok:s{H.G1}:t999999999", None, H.G1,
                     "0")),
        ("overlay_broken", lambda: H.FakeCtx(broken_overlay=True),
         Actor(O, H.G1, "0", H.MUSECHAT),
         H.command(O, H.G1, "0", H.MUSECHAT)),
    ]
    return out


def crawl_all(names=None):
    """{entry name: Crawl}. Entries clicking as the same person in the
    same place share what they have clicked already (the later one
    records only what is new)."""
    out = {}
    seen = {}
    for name, factory, actor, first in entries():
        if names and name not in names:
            continue
        H.install_determinism()
        ctx = factory()
        ctx.refresh()
        resp = UI.handle(first, ctx)
        key = (actor.user, actor.guild, factory)
        out[name] = Crawl(ctx, actor, seen=seen.setdefault(key, set())
                          ).run(name, resp)
        if name == "old_id":
            # Out of date is the right answer here.
            out[name].problems = [p for p in out[name].problems
                                  if "out of date" not in p]
    return out


def golden_text(crawl):
    return "\n\n".join(f"=== {path}\n{text}" for path, text in crawl.pages)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true")
    ap.add_argument("entries", nargs="*")
    a = ap.parse_args(argv)
    res = crawl_all(a.entries or None)
    rc = 0
    for name, c in res.items():
        path = os.path.join(SNAPSHOTS, f"{name}.txt")
        text = golden_text(c) + "\n"
        print(f"{name}: {len(c.pages)} responses, {len(c.problems)} "
              f"problems")
        for p in c.problems[:40]:
            print("  " + p)
        if c.problems:
            rc = 1
        if a.update:
            os.makedirs(SNAPSHOTS, exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
        else:
            try:
                with open(path) as f:
                    want = f.read()
            except OSError:
                print(f"  no golden {path}")
                rc = 1
                continue
            if want != text:
                rc = 1
                diff = difflib.unified_diff(want.splitlines(),
                                            text.splitlines(), "golden",
                                            "now", lineterm="", n=1)
                print("\n".join(list(diff)[:80]))
    return rc


if __name__ == "__main__":
    sys.exit(main())
