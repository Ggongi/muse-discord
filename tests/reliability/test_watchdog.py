"""watchdog.sh scenarios against fake monitor/presence processes. Each runs
in its own user+PID namespace (unshare), so the watchdog's pgrep/pkill
can only ever see the fakes, and every fake dies with the namespace."""
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
from common import FAKE_PROC, FAKEWS, MUT_DIR, scratch  # noqa: E402


def scenario(steps, env=None, token_file=True, token_env=None,
             presence=False, old_log=None):
    d = scratch("muse-wd-")
    shutil.copy(os.path.join(MUT_DIR, "watchdog.sh"), d)
    for n in ("monitor.py", "presence.py"):
        shutil.copy(FAKE_PROC, os.path.join(d, n))
    tok = os.path.join(d, "bot_token")
    if token_file:
        with open(tok, "w") as f:
            f.write("dummy\n")
    with open(os.path.join(d, "config.yaml"), "w") as f:
        f.write(f"bot:\n  token_file: {tok}\n"
                f"  token_env: {token_env or 'null'}\n"
                f"presence:\n  enabled: {'true' if presence else 'false'}\n"
                f"updates:\n  check: false\n")
    if old_log:
        with open(os.path.join(d, "monitor.log"), "w") as f:
            f.write(old_log)
    e = os.environ.copy()
    # A fake websockets package, so the watchdog never pip-installs.
    e["PYTHONPATH"] = FAKEWS
    e.update(env or {})
    p = subprocess.run(
        ["unshare", "--user", "--map-root-user", "--pid", "--fork",
         "--mount-proc", "bash", "-c", f"cd '{d}' && {steps}"],
        capture_output=True, text=True, timeout=400, env=e)
    return p.stdout + p.stderr, d


WD = "bash watchdog.sh"


class Watchdog(unittest.TestCase):
    def test_token_env_only_install_starts(self):
        out, _ = scenario(WD, env={"MY_BOT_TOKEN": "x"}, token_file=False,
                          token_env="MY_BOT_TOKEN")
        self.assertIn("OK monitor restarted", out)
        self.assertNotIn("ALERT", out)

    def test_missing_token_names_both_sources(self):
        out, _ = scenario(WD, token_file=False, token_env="MY_BOT_TOKEN")
        self.assertIn("ALERT: monitor DOWN and env var $MY_BOT_TOKEN unset",
                      out)

    def test_log_rotated_not_truncated(self):
        out, d = scenario(WD, old_log="Traceback: the crash we need\n")
        with open(os.path.join(d, "monitor.log.1")) as f:
            self.assertIn("the crash we need", f.read())

    def test_identity_pending_counts_as_started(self):
        out, _ = scenario(WD, env={"FAKE_MODE": "pending"})
        self.assertIn("OK monitor restarted (identity pending", out)
        self.assertNotIn("ALERT", out)

    def test_token_rejected_alerts_fast(self):
        out, _ = scenario(f"time -p {WD}", env={"FAKE_MODE": "token"})
        self.assertIn("ALERT: monitor DOWN: Discord rejected the token", out)
        real = float(out.split("real ")[1].split()[0])
        self.assertLess(real, 30)

    def test_stale_heartbeat_restarts(self):
        steps = (f"{WD}; touch -d '20 minutes ago' monitor.heartbeat; "
                 f"{WD}; echo; {WD}")
        out, _ = scenario(steps, env={"FAKE_MODE": "hung"})
        self.assertIn("OK monitor restarted (it was stuck: heartbeat", out)
        self.assertEqual(out.count("OK monitor alive"), 1, out)

    def test_gateway_disabled_alerts_once(self):
        out, _ = scenario(f"{WD}; {WD}; {WD}", env={"FAKE_MODE": "gwoff"})
        self.assertEqual(out.count("ALERT: monitor degraded: GATEWAY "
                                   "DISABLED (code 4014)"), 1, out)
        self.assertIn("OK already alerted", out)

    def test_presence_token_rejected(self):
        out, _ = scenario(WD, env={"FAKE_PMODE": "token"}, presence=True)
        self.assertIn("ALERT: presence DOWN: Discord rejected the token",
                      out)

    def test_healthy_run_is_quiet(self):
        out, _ = scenario(f"{WD}; {WD}", presence=True)
        self.assertIn("OK monitor alive", out)
        self.assertIn("OK presence alive", out)
        self.assertNotIn("ALERT", out)


if __name__ == "__main__":
    unittest.main()
