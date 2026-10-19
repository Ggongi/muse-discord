"""Reliability, main()-loop scenarios (boot 401/429, REST 401 mid-run,
the heartbeat), each in its own process (run_main.py)."""
import json
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))


def run(scenario, seconds=5):
    p = subprocess.run([sys.executable, os.path.join(HERE, "run_main.py"),
                        scenario, str(seconds)],
                       capture_output=True, text=True, timeout=60,
                       env=os.environ.copy())
    last = (p.stdout.strip().splitlines() or ["{}"])[-1]
    try:
        return json.loads(last)
    except ValueError:
        raise AssertionError(p.stdout + p.stderr)


class F1Main(unittest.TestCase):
    def test_boot_401_exits_with_marker(self):
        r = run("boot401", 3)
        self.assertEqual(r["exit"], 3, r["out"])
        self.assertIn("TOKEN REJECTED", r["out"])
        self.assertNotIn("Bot user id", r["out"])

    def test_boot_429_prints_pending_not_identity_marker(self):
        r = run("boot429", 3)
        self.assertIn("Bot identity pending", r["out"])
        self.assertNotIn("Bot user id", r["out"])

    def test_gateway_4004_exits_main_loop(self):
        r = run("gw4004", 4)
        self.assertEqual(r["exit"], 3, r["out"])
        self.assertIn("TOKEN REJECTED", r["out"])

    def test_rest_401_mid_run_exits(self):
        r = run("rest401", 5)
        self.assertEqual(r["exit"], 3, r["out"])

    def test_heartbeat_is_touched(self):
        r = run("phases", 2)
        self.assertTrue(r["heartbeat"])

    def test_identity_adopted_from_gateway_ready(self):
        r = run("ready_identity", 4)
        self.assertIn("Bot user id: 999 (from gateway READY)", r["out"])


class F3Phases(unittest.TestCase):
    def test_failing_outbox_does_not_skip_cleanup(self):
        r = run("phases", 3)
        self.assertIn("corrupt outbox", r["out"])
        self.assertGreater(r["calls"]["sweep_media"], 0, r["out"])


class F5Main(unittest.TestCase):
    def test_no_dm_poll_without_identity(self):
        r = run("identity_pending", 4)
        self.assertEqual(r["calls"]["list_dm_channels"], 0, r["out"])


class F8BatchFold(unittest.TestCase):
    def test_same_batch_followup_folds_into_burst(self):
        r = run("batch_fold", 5)
        items = r.get("queue") or []
        self.assertEqual(len(items), 1, r["out"])
        self.assertTrue(items[0]["burst"])
        self.assertIn("deploy command", items[0]["content"])


if __name__ == "__main__":
    unittest.main()
