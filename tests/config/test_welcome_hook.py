"""Welcome plugin hook, offline: fire member_join 12 times with no params
in config; the plugin.yaml daily_cap (10) must stop the 11th and 12th,
and a stale join (older than max_age_hours) is never welcomed.
MUT_DIR = the package tree under test (default: this checkout)."""
import contextlib
import importlib.util
import io
import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
from common import MUT_DIR, redirect_files, scratch  # noqa: E402


def load_monitor():
    tmp = scratch("muse-welcome-")
    cfg = os.path.join(tmp, "config.yaml")
    with open(cfg, "w") as f:
        f.write("plugins:\n  welcome: {enabled: true}\n")
    os.environ["MUSE_DISCORD_CONFIG"] = cfg
    sys.argv = ["x", "dummy-token"]
    sys.path.insert(0, MUT_DIR)
    spec = importlib.util.spec_from_file_location(
        "welcome_mut_monitor", os.path.join(MUT_DIR, "monitor.py"))
    mod = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(mod)
    redirect_files(mod, tmp)
    mod.PLUGINS_DIR = os.path.join(MUT_DIR, "plugins")
    with contextlib.redirect_stdout(io.StringIO()):
        mod.load_plugins()
    return mod


class WelcomeHook(unittest.TestCase):
    def test_plugin_yaml_daily_cap_and_stale_join(self):
        monitor = load_monitor()
        state = {}
        ctx = {"state": state,
               "channels": {"900": {"guild_id": "111", "name": "general",
                                    "guild_name": "G", "can_send": True}},
               "guilds": {"111": {"name": "G", "system_channel_id": "900"}}}
        now = datetime.now(timezone.utc).isoformat()
        queued = 0
        with contextlib.redirect_stdout(io.StringIO()):
            for i in range(12):
                msg = {"id": str(1000 + i), "timestamp": now,
                       "author": {"id": str(5000 + i), "username": f"u{i}"}}
                res = monitor.call_hook("member_join", ctx, msg, "111")
                queued += sum(1 for r in res if r)
        self.assertEqual(queued, 10)
        old = {"id": "2000", "timestamp": "2020-01-01T00:00:00+00:00",
               "author": {"id": "1", "username": "old"}}
        state["plugins"]["welcome"]["counts"] = {}
        with contextlib.redirect_stdout(io.StringIO()):
            res = monitor.call_hook("member_join", ctx, old, "111")
        self.assertFalse(any(res), "a stale join was welcomed")


if __name__ == "__main__":
    unittest.main()
