"""Shared paths and scratch dirs for the offline suites.

Every suite puts this directory on sys.path first thing:

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "_lib"))

MUT_DIR is the package tree under test (default: this checkout)."""
import atexit
import os
import shutil
import sys
import tempfile

# Never leave __pycache__ behind in the tree under test (or here).
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
# Scratch git repos must not pick up the user's config (commit signing,
# hooks, templates); the same goes for every git the code under test runs.
os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
# Usage stats (telemetry.py) are never sent from a test, whatever the
# endpoint; the telemetry suite turns them on per Recorder, at a fake.
os.environ["MUSE_DISCORD_TELEMETRY"] = "off"
os.environ.pop("MUSE_DISCORD_TELEMETRY_URL", None)

LIB = os.path.dirname(os.path.abspath(__file__))
TESTS = os.path.dirname(LIB)
REPO = os.path.dirname(TESTS)
MUT_DIR = os.path.abspath(os.environ.get("MUT_DIR") or REPO)
os.environ["MUT_DIR"] = MUT_DIR   # subprocesses see the same tree

FAKEWS = os.path.join(LIB, "fakews")          # offline `websockets`
FAKE_PROC = os.path.join(LIB, "fake_proc.py")  # monitor/presence stand-in
HOOK_RUNTIME = os.path.join(LIB, "hook_runtime.sh")  # hatch hook stubs

_KEEP = os.environ.get("MUSE_TESTS_KEEP_TMP") == "1"


def scratch(prefix="muse-test-"):
    """A fresh temp dir, removed when this process exits (unless
    MUSE_TESTS_KEEP_TMP=1, for debugging)."""
    d = tempfile.mkdtemp(prefix=prefix)
    if not _KEEP:
        atexit.register(shutil.rmtree, d, True)
    return d


def copy_watchdog(dst_dir):
    """Copy the tree's watchdog.sh into dst_dir with its 5 s health poll
    (wait_healthy) shortened to 0.2 s: the fakes are healthy at once, so
    only the waiting changes, not what is checked. Returns the path."""
    with open(os.path.join(MUT_DIR, "watchdog.sh")) as f:
        text = f.read()
    dst = os.path.join(dst_dir, "watchdog.sh")
    with open(dst, "w") as f:
        f.write(text.replace("    sleep 5\n", "    sleep 0.2\n"))
    shutil.copymode(os.path.join(MUT_DIR, "watchdog.sh"), dst)
    return dst


def redirect_files(mod, tmp):
    """Point every *_FILE path the module keeps under its own dir
    (BASE_DIR) at tmp instead, so a test never writes into the tree under
    test. Call it right after importing; set specific paths after."""
    base = getattr(mod, "BASE_DIR", None)
    if not base:
        return
    for name in dir(mod):
        val = getattr(mod, name)
        if (name.endswith("_FILE") and isinstance(val, str)
                and val.startswith(base + os.sep)):
            setattr(mod, name, os.path.join(tmp, os.path.relpath(val, base)))
