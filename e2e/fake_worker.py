#!/usr/bin/env python3
"""fake_worker.py -- the tier-1 e2e worker: scripted, no LLM.

    python3 e2e/fake_worker.py --pkg <package dir> [--interval 1]

Polls <pkg>/reply_queue.json and answers every queue item exactly once
with one outbox item, written through <pkg>/bin/outbox-append.py like a
real worker (PROTOCOL.md). The reply threads under the item's LAST
burst fragment (the trigger when there are none) and says `ack <n>`,
n = the number of messages in the item (trigger + fragments), unless
the item's text (any fragment) carries a directive:

  [[silent]]     a `silent` outbox item: deliberately no reply
  [[long]]       a 3500-character reply (the monitor must cap/split it)
  [[everyone]]   the reply contains @everyone and <@&<guild id>>
                 (allowed_mentions must stop both pings)
  [[mention_a]]  the reply mentions driver A as <@id> (driver A is the
                 config's first owner; --a-id overrides)
  [[slow]]       wait 15 s before writing the outbox item

Directives combine ([[slow]] [[long]] is a slow long reply); [[silent]]
wins over the rest. Runs until SIGTERM. Never reads the token.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time

LONG_CHARS = 3500
SLOW_SECS = 15.0


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def driver_a_id(pkg):
    """Driver A is the test config's (only) owner."""
    try:
        import yaml
        with open(os.path.join(pkg, "config.yaml")) as f:
            cfg = yaml.safe_load(f) or {}
        owners = (cfg.get("owners") or {}).get("user_ids") or []
        return str(owners[0]) if owners else None
    except Exception:
        return None


def item_texts(item):
    """The trigger's text plus every fragment's (content already joins
    them for bursts; fragments are checked too in case it is cut)."""
    texts = [item.get("content") or ""]
    texts += [f.get("content") or "" for f in item.get("burst_fragments")
              or []]
    return "\n".join(texts)


def directives(item):
    text = item_texts(item)
    return {d for d in ("silent", "long", "everyone", "mention_a", "slow")
            if f"[[{d}]]" in text}


def build_reply(item, a_id=None):
    """(outbox item, delay seconds) answering one queue item."""
    frags = item.get("burst_fragments") or []
    n = 1 + len(frags)
    last = frags[-1]["message_id"] if frags else item.get("message_id")
    flags = directives(item)
    out = {"item": item.get("message_id"),
           "channel_id": item.get("channel_id"),
           "channel_name": item.get("channel_name"),
           "author": item.get("author")}
    if item.get("kind") != "welcome":
        out["reply_to"] = last  # system join messages cannot be replied to
    delay = SLOW_SECS if "slow" in flags else 0.0
    if "silent" in flags:
        out["silent"] = True
        return out, delay
    text = f"ack {n}"
    if "mention_a" in flags and a_id:
        text += f" <@{a_id}>"
    if "everyone" in flags:
        text += f" @everyone <@&{item.get('guild_id')}>"
    if "long" in flags:
        filler = " lorem ipsum dolor sit amet"
        while len(text) < LONG_CHARS:
            text += filler
        text = text[:LONG_CHARS]
    out["text"] = text
    return out, delay


def append_outbox(pkg, items):
    """Append through the package's own tool (locks, validates)."""
    tool = os.path.join(pkg, "bin", "outbox-append.py")
    p = subprocess.run([sys.executable, tool], input=json.dumps(items),
                       capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        print(f"outbox-append failed ({p.returncode}): {p.stderr.strip()}",
              flush=True)
    return p.returncode == 0


class Worker:
    def __init__(self, pkg, a_id=None, clock=time.time):
        self.pkg = os.path.abspath(pkg)
        self.a_id = a_id
        self.clock = clock
        self.answered = set()   # queue message_ids handled (or scheduled)
        self.scheduled = []     # [(due, outbox item)] for [[slow]]
        # Items already answered in the outbox (a restarted worker) are
        # never answered again.
        for name in ("reply_outbox.json", "reply_outbox.processing.json"):
            for o in load_json(os.path.join(self.pkg, name), []) or []:
                if isinstance(o, dict) and o.get("item"):
                    self.answered.add(str(o["item"]))

    def tick(self):
        """One poll: schedule new items, write what is due. Returns the
        number of outbox items written."""
        if self.a_id is None:
            self.a_id = driver_a_id(self.pkg)
        queue = load_json(os.path.join(self.pkg, "reply_queue.json"), [])
        now = self.clock()
        for item in queue if isinstance(queue, list) else []:
            mid = str(item.get("message_id") or "")
            if not mid or mid in self.answered:
                continue
            self.answered.add(mid)
            out, delay = build_reply(item, self.a_id)
            print(f"item {mid}: {sorted(directives(item)) or 'ack'}"
                  f"{f' (in {delay:.0f}s)' if delay else ''}", flush=True)
            self.scheduled.append((now + delay, out))
        due = [o for t, o in self.scheduled if t <= now]
        if not due:
            return 0
        if append_outbox(self.pkg, due):
            self.scheduled = [(t, o) for t, o in self.scheduled if t > now]
            return len(due)
        return 0  # retried next tick


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--pkg", required=True, help="the SUT package dir")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--a-id", default=None,
                    help="driver A's user id (default: first owner)")
    args = ap.parse_args(argv)
    stop = {"now": False}

    def on_term(signum, frame):
        stop["now"] = True
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    w = Worker(args.pkg, a_id=args.a_id)
    print(f"fake worker polling {w.pkg}", flush=True)
    while not stop["now"]:
        try:
            w.tick()
        except Exception as e:  # keep answering; the run checks results
            print(f"fake worker error: {e}", flush=True)
        time.sleep(args.interval)
    return 0


if __name__ == "__main__":
    sys.exit(main())
