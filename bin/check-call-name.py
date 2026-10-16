#!/usr/bin/env python3
"""check-call-name.py -- setup-time collision check for the bot's call name.

The call name is what people type to address the bot in chat (the opt-in
"name" trigger matches it as a bare word). It is chosen separately from
the Discord application name. A call name that is an everyday word, a
well-known product or assistant name, or the name of the place the bot
lives ("Muse" in a Muse community server) turns ordinary conversation
into summonses: "my Muse can post a selfie" reads as "Muse, ...".

Usage:
  python3 bin/check-call-name.py <call name> [--context TEXT ...]

--context takes the names of the servers the bot will live in (and any
other words those communities use for themselves); repeat it per value.

Exit codes:
  0  no collision found
  2  COLLISION: setup must warn the owner loudly and continue only on
     their explicit confirmation
  3  no system word list was found, so the dictionary check could not
     run: the installer must judge by hand whether the name is an
     everyday word, and treat it as a collision if it is
  64 usage error

Runs offline, needs no token, and never reads config.yaml.
"""
import os
import re
import sys

# Platform, product, and assistant names people say in passing, plus
# words that are already Discord syntax.
RESERVED = {
    "muse", "discord", "wumpus", "clyde", "nitro",
    "claude", "anthropic", "chatgpt", "gpt", "openai", "gemini", "bard",
    "copilot", "siri", "alexa", "cortana", "bixby", "grok", "llama",
    "mistral", "perplexity", "midjourney", "google", "apple", "android",
    "bot", "ai", "assistant", "agent", "admin", "mod", "everyone", "here",
}

WORD_LISTS = (
    "/usr/share/dict/words",
    "/usr/share/dict/american-english",
    "/usr/share/dict/british-english",
    "/usr/share/dict/web2",
)


def _norm(text):
    return re.sub(r"\s+", " ", text.strip().lower())


def _dictionary():
    for path in WORD_LISTS:
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    return path, {w.strip().lower() for w in f if w.strip()}
            except OSError:
                continue
    return None, None


def check(name, contexts):
    """Return (problems, dictionary_path). problems is a list of reasons
    the name collides; empty means clear."""
    n = _norm(name)
    problems = []
    if len(n.replace(" ", "")) < 3:
        problems.append("it is under 3 characters, so it turns up inside "
                        "ordinary text")
    if n in RESERVED:
        problems.append(f"\"{name}\" is a platform, product, or assistant "
                        f"name people mention in passing")
    pattern = re.compile(r"\b" + re.escape(n) + r"\b")
    for ctx in contexts:
        if pattern.search(_norm(ctx)):
            problems.append(f"it appears in \"{ctx}\", so members of that "
                            f"community say it about the place itself")
    dict_path, words = _dictionary()
    if words is not None and " " not in n and n in words:
        problems.append(f"\"{name}\" is a dictionary word ({dict_path})")
    return problems, dict_path


def main(argv):
    contexts = []
    rest = []
    i = 0
    while i < len(argv):
        if argv[i] == "--context":
            if i + 1 >= len(argv):
                break
            contexts.append(argv[i + 1])
            i += 2
            continue
        rest.append(argv[i])
        i += 1
    if len(rest) != 1 or not rest[0].strip():
        print(__doc__.split("\n\n")[2], file=sys.stderr)
        return 64
    name = rest[0].strip()
    problems, dict_path = check(name, contexts)
    if problems:
        print(f"COLLISION: \"{name}\" is a risky call name:")
        for p in problems:
            print(f"  - {p}")
        print("With name triggers on, people using this word in ordinary "
              "conversation will read as summoning the bot. Pick a "
              "distinctive name, or continue only on the owner's explicit "
              "confirmation.")
        return 2
    if dict_path is None:
        print(f"UNCHECKED: no system word list found, so whether \"{name}\" "
              f"is an everyday word was not checked. Judge it by hand; if "
              f"it is one, treat it as a collision.")
        return 3
    print(f"OK: \"{name}\" has no known collisions.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
