#!/usr/bin/env python3
"""muse-discord settings tool.

    bin/settings.py validate
        Check config.yaml (and config.discord.yaml, the Discord-side
        changes). Exit status 1, with every problem listed, when the bot
        would refuse to start.
    bin/settings.py show [--dms | --server ID [--channel ID]
                          [--category ID]] [--explain]
        What the bot does in one place: every setting's resolved value
        and where it comes from (built-in, config.yaml, a Discord change,
        or inherited from a wider scope). No flags = the everywhere
        scope. --explain lists every layer for each setting.
    bin/settings.py fold [--dry-run]
        Copy the Discord-side changes into config.yaml and clear them.
        Rewrites config.yaml (its comments are lost) after saving a
        timestamped backup next to it. Does not prompt.
    bin/settings.py migrate [--dry-run] [--state PATH]
        Convert an old-layout config.yaml to the new layout. Resolves the
        old and the new file for every known scope and refuses if any
        behavior would change (except the announced new default,
        who.replies: conversational). Saves a timestamped backup first;
        comments in the old file are not carried over. Does not prompt.

--config PATH picks the file (default: $MUSE_DISCORD_CONFIG, else
config.yaml in the package directory).
"""
import argparse
import json
import os
import sys

PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PKG)
import muse_settings as MS  # noqa: E402

PLUGINS = os.path.join(PKG, "plugins")


def _cfg_path(args):
    return args.config or os.environ.get("MUSE_DISCORD_CONFIG") \
        or os.path.join(PKG, "config.yaml")


def _known_guilds(cfg):
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(cfg)),
                               "state.json")) as f:
            return (json.load(f) or {}).get("known_guilds") or {}
    except (OSError, ValueError, AttributeError):
        return {}


def _fmt(spec, value):
    v = MS.display(spec, value)
    if isinstance(v, str):
        return json.dumps(v) if v == "" or "\n" in v else v
    if isinstance(v, list):
        return "[" + ", ".join(json.dumps(x) if isinstance(x, str) else
                               str(x) for x in v) + "]"
    return str(v)


def cmd_validate(args):
    cfg = _cfg_path(args)
    st = MS.load(cfg, PLUGINS)
    print(f"{cfg}: valid ({st.layout} layout).")
    if st.notice:
        print(st.notice)
        for line in st.mapping:
            print(f"  {line}")
    n = len(st.overlay)
    if n or st.retire:
        print(f"{MS.OVERLAY_NAME}: {n} Discord change(s) in effect.")
    for keystr, _entry, reason in st.retire:
        print(f"  {keystr}: no longer applies ({reason}); the monitor will "
              f"move it to history and tell the owners.")
    if getattr(st, "overlay_error", None):
        print(st.overlay_error)
    return 0


def cmd_show(args):
    cfg = _cfg_path(args)
    st = MS.load(cfg, PLUGINS)
    known = _known_guilds(cfg)
    if args.dms:
        scope = MS.Scope.dm()
    elif args.server:
        name = known.get(args.server) or st.labels.get(args.server)
        scope = MS.Scope.server(args.server, name, args.channel,
                                args.category)
    else:
        if args.channel:
            print("--channel needs --server", file=sys.stderr)
            return 2
        scope = MS.Scope.everywhere()
    print(f"Settings for: {scope.label()}"
          + ("  (config.yaml uses the old layout)" if st.layout == "old"
             else ""))
    if scope.kind == "everywhere":
        print(f"  owners                   {', '.join(st.owners) or '(none)'}")
        print(f"  persona                  {st.persona}")
        print("  token_from               "
              + ", ".join(f"env:{w}" if k == "env" else w
                          for k, w in st.token_sources()))
        for key in ("presence.status", "presence.kind", "updates.notify"):
            print(f"  {key:<24} {_fmt(MS.TOP[key], st.top(key))}")
        if not st.presence_enabled:
            print("  presence.enabled         off")
    kinds = {MS._scope_kind(p) for p, _n in st.chain(scope)
             if p[0] not in ("builtin", "builtin_all_servers")}
    if scope.kind == "server":
        kinds |= {"server", "channel"}
    for key, value, source, rows in st.explain(scope):
        spec = st.spec(key)
        if spec is None or not kinds & set(spec.scopes):
            continue
        if spec.hidden and source.startswith("built-in"):
            continue
        print(f"  {key:<24} {_fmt(spec, value):<18} [{source}]")
        if args.explain:
            for name, path, is_set, v, src in rows:
                shown = _fmt(spec, v) if is_set else "(not set)"
                where = MS._source_text(src) if is_set else ""
                print(f"      {name:<28} {shown}"
                      + (f"  ({where})" if where else ""))
    return 0


def cmd_fold(args):
    cfg = _cfg_path(args)
    st = MS.load(cfg, PLUGINS)
    if not st.overlay:
        print("Nothing to fold: no Discord changes in effect.")
        return 0
    keys = sorted(MS.format_key(p, k) for p, k in st.overlay)
    if args.dry_run:
        print("Would copy into config.yaml (and clear from "
              f"{MS.OVERLAY_NAME}):")
        for k in keys:
            print(f"  {k}")
        return 0
    bak, folded = MS.fold(cfg, PLUGINS)
    print(f"Backed up config.yaml to {bak}.")
    print(f"Copied {len(folded)} Discord change(s) into config.yaml and "
          f"cleared them from {MS.OVERLAY_NAME}:")
    for k in folded:
        print(f"  {k}")
    print("config.yaml was rewritten: its comments are gone (the backup "
          "has them).")
    return 0


def cmd_migrate(args):
    cfg = _cfg_path(args)
    bak, text, lines, notes = MS.migrate(cfg, PLUGINS, args.state,
                                         write=not args.dry_run)
    print("What moved where:")
    for line in lines or ["(nothing set; the defaults carry over)"]:
        print(f"  {line}")
    print("Behavior check: every known scope resolves the same, except:")
    for n in notes:
        print(f"  {n}")
    if args.dry_run:
        print("\nThe new config.yaml would be:\n")
        print(text)
        return 0
    print(f"\nBacked up the old config.yaml to {bak}.")
    print("Wrote config.yaml in the new layout. Comments in the old file "
          "are not carried over (the backup has them).")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="settings.py", description="muse-discord settings tool")
    ap.add_argument("--config", help="config.yaml to use")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate", help="check config.yaml")
    sh = sub.add_parser("show", help="resolved settings for one scope")
    g = sh.add_mutually_exclusive_group()
    g.add_argument("--dms", action="store_true")
    g.add_argument("--server")
    sh.add_argument("--channel", help="a channel id (with --server)")
    sh.add_argument("--category", help="that channel's category id")
    sh.add_argument("--explain", action="store_true")
    fo = sub.add_parser("fold", help="copy Discord changes into "
                        "config.yaml")
    fo.add_argument("--dry-run", action="store_true")
    mi = sub.add_parser("migrate", help="convert an old-layout config.yaml")
    mi.add_argument("--dry-run", action="store_true")
    mi.add_argument("--state", help="state.json (server names; default: "
                    "next to config.yaml)")
    args = ap.parse_args(argv)
    try:
        return {"validate": cmd_validate, "show": cmd_show,
                "fold": cmd_fold, "migrate": cmd_migrate}[args.cmd](args)
    except MS.SettingsError as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
