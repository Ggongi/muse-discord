"""Config resolution, offline: resolved constants for missing keys,
explicit nulls and zeros, out-of-range and garbage values (with their
warnings), and plugin.yaml defaults with user overrides and per_guild
merges. Each case runs in a fresh subprocess (monitor resolves its
config at import). MUT_DIR = the package tree under test (default: this
checkout)."""
import json
import os
import subprocess
import sys
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
from common import MUT_DIR, scratch  # noqa: E402

PROBE = r"""
import sys, json
sys.argv = ["x", "dummy-token"]
sys.path.insert(0, %(repo)r)
import monitor
out = {k: getattr(monitor, k) for k in (
    "COALESCE_SECS", "BOT_COALESCE_SECS", "MAX_REPLY_CHARS",
    "POLL_INTERVAL", "SWEEP_EVERY_CYCLES", "SWEEP_GRACE_SECS",
    "SWEEP_MAX_AGE_SECS", "SWEEP_MAX_RECOVERIES", "BACKOFF_INITIAL_SECS",
    "BACKOFF_MAX_SECS", "CHANNEL_REFRESH_CYCLES", "DAILY_REPLY_CAP",
    "PER_USER_DAILY_CAP")}
out["HAS_MAX_BOT_TURNS"] = hasattr(monitor, "MAX_BOT_TURNS")
if %(plugins)r:
    monitor.PLUGINS_DIR = %(repo)r + "/plugins"
    captured = {}
    orig = monitor.PluginAPI.__init__
    def init(self, name, params):
        orig(self, name, params)
        captured[name] = self
    monitor.PluginAPI.__init__ = init
    monitor.load_plugins()
    res = {}
    for name, api in captured.items():
        res[name] = {"params": api.params}
        for key in ("channel", "prompt", "daily_cap", "max_age_hours",
                    "allowed_user_ids"):
            res[name][key] = api.param(key, "111", "Guild One")
            res[name][key + "@222"] = api.param(key, "222", "Guild Two")
    out["plugins"] = res
print("JSON" + json.dumps(out))
"""


def run(name, yaml_text, plugins=False):
    d = scratch("muse-cfg-")
    path = os.path.join(d, f"cfg_{name}.yaml")
    with open(path, "w") as f:
        f.write(textwrap.dedent(yaml_text))
    env = dict(os.environ, MUSE_DISCORD_CONFIG=path)
    p = subprocess.run([sys.executable, "-c",
                        PROBE % {"repo": MUT_DIR, "plugins": plugins}],
                       capture_output=True, text=True, env=env, cwd=d,
                       timeout=60)
    if p.returncode != 0:
        raise AssertionError(f"case {name} crashed:\n{p.stdout}{p.stderr}")
    lines = p.stdout.splitlines()
    data = json.loads([ln for ln in lines if ln.startswith("JSON")][0][4:])
    warnings = [ln for ln in lines if ln.startswith("WARNING")]
    return data, warnings, lines


class Config(unittest.TestCase):
    def check(self, label, got, want):
        with self.subTest(label):
            self.assertEqual(got, want, label)

    def test_empty_config(self):
        """Missing keys -> defaults, no warnings."""
        d, w, _ = run("empty", "{}\n")
        self.check("COALESCE_SECS", d["COALESCE_SECS"], 5.0)
        self.check("MAX_REPLY_CHARS", d["MAX_REPLY_CHARS"], 1800)
        self.check("POLL_INTERVAL", d["POLL_INTERVAL"], 30)
        self.check("SWEEP_EVERY_CYCLES", d["SWEEP_EVERY_CYCLES"], 6)
        self.check("SWEEP_GRACE_SECS", d["SWEEP_GRACE_SECS"], 240.0)
        self.check("SWEEP_MAX_AGE_SECS", d["SWEEP_MAX_AGE_SECS"], 6 * 3600.0)
        self.check("SWEEP_MAX_RECOVERIES", d["SWEEP_MAX_RECOVERIES"], 2)
        self.check("BACKOFF_INITIAL_SECS", d["BACKOFF_INITIAL_SECS"], 60.0)
        self.check("BACKOFF_MAX_SECS", d["BACKOFF_MAX_SECS"], 900.0)
        self.check("CHANNEL_REFRESH_CYCLES", d["CHANNEL_REFRESH_CYCLES"], 60)
        self.check("DAILY_REPLY_CAP", d["DAILY_REPLY_CAP"], None)
        self.check("warnings", w, [])

    def test_explicit_nulls(self):
        """Explicit nulls -> defaults too."""
        d, w, _ = run("nulls", """
        behavior: {coalesce_seconds: null, max_reply_chars: null,
                   max_bot_turns: null}
        engine:
          rest_poll_seconds: null
          sweep: {every_cycles: null, grace_seconds: null}
        """)
        self.check("COALESCE_SECS", d["COALESCE_SECS"], 5.0)
        self.check("MAX_REPLY_CHARS", d["MAX_REPLY_CHARS"], 1800)
        self.check("POLL_INTERVAL", d["POLL_INTERVAL"], 30)
        self.check("SWEEP_EVERY_CYCLES", d["SWEEP_EVERY_CYCLES"], 6)
        self.check("warnings", w, [])

    def test_explicit_zeros(self):
        """Explicit zeros: honored where meaningful, clamped where not."""
        d, w, _ = run("zeros", """
        behavior: {coalesce_seconds: 0, max_reply_chars: 0, max_bot_turns: 0}
        safety: {daily_reply_cap: 0, per_user_daily_cap: 0}
        engine:
          rest_poll_seconds: 0
          discovery_refresh_minutes: 0
          sweep: {every_cycles: 0, grace_seconds: 0, max_age_hours: 0,
                  max_recoveries: 0}
          backoff: {initial_seconds: 0, max_seconds: 0}
        """)
        self.check("COALESCE_SECS", d["COALESCE_SECS"], 0.0)
        self.check("BOT_COALESCE_SECS", d["BOT_COALESCE_SECS"], 15.0)
        self.check("MAX_REPLY_CHARS", d["MAX_REPLY_CHARS"], 1)
        self.check("POLL_INTERVAL", d["POLL_INTERVAL"], 10)
        self.check("CHANNEL_REFRESH_CYCLES", d["CHANNEL_REFRESH_CYCLES"], 6)
        self.check("SWEEP_EVERY_CYCLES", d["SWEEP_EVERY_CYCLES"], 1)
        self.check("SWEEP_GRACE_SECS", d["SWEEP_GRACE_SECS"], 0.0)
        self.check("SWEEP_MAX_AGE_SECS", d["SWEEP_MAX_AGE_SECS"], 0.0)
        self.check("SWEEP_MAX_RECOVERIES", d["SWEEP_MAX_RECOVERIES"], 0)
        self.check("BACKOFF_INITIAL_SECS", d["BACKOFF_INITIAL_SECS"], 1.0)
        self.check("BACKOFF_MAX_SECS", d["BACKOFF_MAX_SECS"], 1.0)
        self.check("DAILY_REPLY_CAP", d["DAILY_REPLY_CAP"], 0)
        self.check("PER_USER_DAILY_CAP", d["PER_USER_DAILY_CAP"], 0)
        self.check("HAS_MAX_BOT_TURNS", d["HAS_MAX_BOT_TURNS"], False)
        self.check("max_bot_turns removed warning",
                   sum("max_bot_turns was removed" in ln for ln in w), 1)
        self.check("warning count", len(w), 7)

    def test_out_of_range(self):
        """Out of range above / garbage."""
        d, w, _ = run("range", """
        behavior: {max_reply_chars: 5000, coalesce_seconds: -3}
        engine: {rest_poll_seconds: 45, discovery_refresh_minutes: 3,
                 sweep: {every_cycles: "abc"}}
        """)
        self.check("MAX_REPLY_CHARS", d["MAX_REPLY_CHARS"], 2000)
        self.check("COALESCE_SECS", d["COALESCE_SECS"], 0.0)
        self.check("POLL_INTERVAL", d["POLL_INTERVAL"], 45)
        self.check("CHANNEL_REFRESH_CYCLES", d["CHANNEL_REFRESH_CYCLES"], 4)
        self.check("SWEEP_EVERY_CYCLES", d["SWEEP_EVERY_CYCLES"], 6)
        self.check("warning count", len(w), 3)

    def test_plugins_enabled_no_params(self):
        """Plugins enabled without params -> plugin.yaml defaults."""
        d, w, lines = run("plugins_bare", """
        plugins:
          welcome: {enabled: true}
          media_generation: {enabled: true}
        """, plugins=True)
        wp = d["plugins"]["welcome"]
        self.check("welcome daily_cap", wp["daily_cap"], 10)
        self.check("welcome max_age_hours", wp["max_age_hours"], 24)
        self.check("welcome channel", wp["channel"], "system")
        self.check("welcome prompt starts", (wp["prompt"] or "")[:18],
                   "Welcome {user} to ")
        self.check("welcome no description leak",
                   "description" in json.dumps(wp["params"]), False)
        mp = d["plugins"]["media_generation"]
        self.check("media allowed_user_ids", mp["allowed_user_ids"], [])
        self.check("media daily_cap", mp["daily_cap"], None)

    def test_plugins_with_overrides(self):
        """User overrides win; explicit null turns the cap off; per_guild
        merges with the declared default; block-level per_guild works."""
        d, w, _ = run("plugins_over", """
        plugins:
          welcome:
            enabled: true
            daily_cap: null
            channel:
              per_guild: {"222": "intro"}
            per_guild:
              "222": {max_age_hours: 2}
          media_generation:
            enabled: true
            daily_cap: 3
            allowed_user_ids: ["42"]
        """, plugins=True)
        wp = d["plugins"]["welcome"]
        self.check("welcome daily_cap (null wins)", wp["daily_cap"], None)
        self.check("welcome channel @111 (default kept)", wp["channel"],
                   "system")
        self.check("welcome channel @222 (per_guild)", wp["channel@222"],
                   "intro")
        self.check("welcome max_age_hours @111", wp["max_age_hours"], 24)
        self.check("welcome max_age_hours @222 (block per_guild)",
                   wp["max_age_hours@222"], 2)
        mp = d["plugins"]["media_generation"]
        self.check("media daily_cap", mp["daily_cap"], 3)
        self.check("media allowed_user_ids", mp["allowed_user_ids"], ["42"])

    def test_plain_override_of_shaped_param(self):
        """Plain-value override of a {default, per_guild} param."""
        d, w, _ = run("plugins_plain", """
        plugins:
          welcome: {enabled: true, channel: "general", daily_cap: 0}
        """, plugins=True)
        wp = d["plugins"]["welcome"]
        self.check("welcome channel plain", wp["channel"], "general")
        self.check("welcome daily_cap 0", wp["daily_cap"], 0)

    def test_plugin_disabled(self):
        """Disabled plugin: not loaded."""
        d, w, _ = run("plugins_off", "{}\n", plugins=True)
        self.check("no plugins loaded", d["plugins"], {})


if __name__ == "__main__":
    unittest.main()
