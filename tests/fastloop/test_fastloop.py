"""Fast loop: REST work (polls, sweeps, discovery, slow sends) must not
block the main loop. Each main() scenario runs in its own process
(run_fast.py). MUT_DIR = the package tree under test (default: this
checkout). Run: python3 tests/fastloop/test_fastloop.py [-k name]"""
import collections
import contextlib
import io
import json
import os
import subprocess
import sys
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "_lib"))


def run(scenario, seconds):
    p = subprocess.run([sys.executable, os.path.join(HERE, "run_fast.py"),
                        scenario, str(seconds)],
                       capture_output=True, text=True, timeout=seconds + 60,
                       env=os.environ.copy())
    last = (p.stdout.strip().splitlines() or ["{}"])[-1]
    try:
        return json.loads(last)
    except ValueError:
        raise AssertionError(p.stdout[-3000:] + p.stderr[-3000:])


def staged_at(r):
    out = {}
    for mid, t in r["staged"]:
        out.setdefault(mid, t)
    return out


def max_gap(ts):
    return max((b - a for a, b in zip(ts, ts[1:])), default=None)


def polls_by_channel(r):
    polls = collections.defaultdict(list)
    for path, t in r["gets"]:
        if "/messages?limit=100" in path:
            polls[path.split("/")[2]].append(t)
    return polls


class SlowRest(unittest.TestCase):
    """40 channels, every read 0.5s (a slow proxy): one rotation takes
    ~20s of requests."""

    @classmethod
    def setUpClass(cls):
        cls.r = run("slow40", 50)

    def test_gateway_messages_staged_promptly(self):
        st = staged_at(self.r)
        self.assertEqual(len(self.r["put"]), 4, self.r["out"][-2000:])
        for mid, t in self.r["put"]:
            self.assertIn(mid, st, f"never staged: {mid}")
            self.assertLess(st[mid] - t, 2.0,
                            f"put at {t}, staged at {st[mid]}")

    def test_loop_never_blocked_long(self):
        gap = max_gap(self.r["drains"])
        print(f"\n  slow40: max gap between drains {gap:.3f}s",
              file=sys.stderr)
        self.assertLess(gap, 1.2)

    def test_every_channel_polled_each_rotation(self):
        polls = polls_by_channel(self.r)
        self.assertEqual(len(polls), 40)
        for cid, ts in polls.items():
            self.assertGreaterEqual(len(ts), 2, (cid, ts))
            # about every POLL_INTERVAL (25s here)
            self.assertLess(ts[1] - ts[0], 25 + 3, (cid, ts))


class SweepSliced(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = run("sweep40", 32)

    def test_sweep_recovers_and_loop_stays_responsive(self):
        r = self.r
        self.assertIn(f"Sweep recovered lost mention {r['lost']}", r["out"])
        gap = max_gap(r["drains"])
        print(f"\n  sweep40: max gap between drains {gap:.3f}s",
              file=sys.stderr)
        self.assertLess(gap, 1.0)
        st = staged_at(r)
        for mid, t in r["put"]:
            self.assertLess(st[mid] - t, 1.0)
        # the sweep pass read every channel
        swept = {p.split("/")[2] for p, _ in r["gets"]
                 if p.endswith("?limit=20")}
        self.assertGreaterEqual(len(swept - {"DM1"}), 40)
        for cid, ts in polls_by_channel(r).items():
            self.assertGreaterEqual(len(ts), 2, (cid, ts))


class HungRequest(unittest.TestCase):
    def test_hung_read_stalls_at_most_the_read_timeout(self):
        r = run("hung", 13)
        self.assertEqual(r.get("read_timeout"), 8)
        self.assertIn("Error reading #Srv #hung: timed out", r["out"])
        (mid, t), = r["put"]
        st = staged_at(r)
        self.assertIn(mid, st, r["out"])
        self.assertLess(st[mid] - t, 8 + 1.5)


class Bursts(unittest.TestCase):
    def test_dm_quiet_half_second_server_unchanged_typing_holds(self):
        r = run("bursts", 11)
        st = staged_at(r)
        enq = {mid: t for mid, t, ok in r["enqueued"] if ok}
        # put order: DM at 1s, server mention at 1s, held DM at 4s
        dm1, srv, dm2 = [mid for mid, _ in r["put"]]
        # The times are rounded to the millisecond, so a wait of exactly
        # the window can come out a hair under it (2.502 - 1.002 is
        # 1.4999999999999998): allow EPS below each lower bound.
        EPS = 0.001
        self.assertTrue(0.5 - EPS <= enq[dm1] - st[dm1] < 0.8,
                        (st[dm1], enq[dm1]))
        self.assertTrue(1.5 - EPS <= enq[srv] - st[srv] < 1.8,
                        (st[srv], enq[srv]))
        # the owner kept typing: the DM burst waits the 5s window
        self.assertTrue(5.0 - EPS <= enq[dm2] - st[dm2] < 5.6,
                        (st[dm2], enq[dm2]))
        self.assertIn("queued=True (author stopped typing)", r["out"])


class Outbox(unittest.TestCase):
    def test_outbox_written_mid_wait_is_sent_promptly(self):
        r = run("outbox", 7)
        sends = [t for p, t in r["posts"] if p.endswith("/messages")]
        self.assertEqual(len(sends), 2, r["out"])
        for w, s in zip(r["outbox_written"], sends):
            self.assertLess(s - w, 0.5, (w, s))


class LatencyLine(unittest.TestCase):
    def test_ingest_hold_split(self):
        import monitor_harness as H
        mod = H.load_monitor()
        st = {"last_seen": {}, "replied_to": [], "history": {},
              "rate": {"day": "", "day_count": 0, "users": {}},
              "guild_joined": {H.G1: 0}}
        m = H.msg("500", "U1", "@bot hi", mention=True, age=3.0)
        with contextlib.redirect_stdout(io.StringIO()):
            mod.stage_trigger(st, m, "Srv #g", H.G1, "mention")
        ing = st["ingested_at"][m["id"]]
        self.assertAlmostEqual(ing, time.time(), delta=0.5)
        # pretend: delivered 0.4s after creation, held 1.6s, worker 30s
        t0 = mod._snowflake_ts(m["id"])
        st["ingested_at"][m["id"]] = t0 + 0.4
        st["queued_at"] = {m["id"]: t0 + 2.0}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            mod._log_latency(st, m["id"], t0 + 32.1, t0 + 34.1)
        self.assertEqual(out.getvalue().strip(),
                         f"Latency {m['id']}: ingest 0.4s, hold 1.6s, "
                         f"worker 30.1s, send 2.0s, total 34.1s")
        self.assertNotIn(m["id"], st["ingested_at"])
        self.assertNotIn(m["id"], st["queued_at"])
        # bounded like queued_at
        for i in range(300):
            mod.note_ingested(st, str(i))
        self.assertLessEqual(len(st["ingested_at"]), 200)


class SlicedJobs(unittest.TestCase):
    """Discovery and the thread refresh run one request per step and
    are applied on completion."""

    def setUp(self):
        import monitor_harness as H
        self.H = H
        mod = self.mod = H.load_monitor()
        mod.api.routes.update({
            "/users/@me/guilds": [{"id": H.G1, "name": "Srv"}],
            f"/guilds/{H.G1}?with_counts=true": {"roles": []},
            f"/guilds/{H.G1}/members/{H.BOT}": {"roles": [],
                                                 "joined_at": None},
            f"/guilds/{H.G1}/channels": [
                {"id": "C1", "type": 0, "name": "general"},
                {"id": "C2", "type": 0, "name": "new"}],
            f"/guilds/{H.G1}/threads/active": {"threads": [
                {"id": "T1", "parent_id": "C1", "name": "post"}]},
        })
        mod.CHANNELS = {"C1": H.chan(), "OLD": H.chan("old")}
        self.st = {"last_seen": {"C1": "5", "OLD": "5"}, "replied_to": [],
                   "history": {}, "guild_joined": {H.G1: 0},
                   "known_guilds": {H.G1: "Srv"},
                   "rate": {"day": "", "day_count": 0, "users": {}}}

    def _run(self, job, between=None):
        steps = 0
        with contextlib.redirect_stdout(io.StringIO()):
            while not job.done():
                # a zero budget: one request per slice
                job.run_slice(self.st, time.time(), True)
                steps += 1
                if between and steps == 2:
                    between()
        return steps

    def test_discovery_job_sliced_and_keeps_gateway_channels(self):
        mod, H = self.mod, self.H
        job = mod.GenJob("Channel refresh", mod.discovery_iter(self.st))

        def gateway_registers():
            mod.CHANNELS["GW"] = H.chan("gw")
        steps = self._run(job, gateway_registers)
        self.assertGreaterEqual(steps, 6)
        self.assertEqual(set(mod.CHANNELS), {"C1", "C2", "T1", "GW"})
        self.assertIn("C2", self.st["last_seen"])  # seeded
        self.assertEqual(self.st["last_seen"]["C1"], "5")  # untouched

    def test_thread_refresh_job(self):
        mod = self.mod
        job = mod.GenJob("Thread refresh", mod.thread_refresh_iter(self.st))
        steps = self._run(job)
        self.assertGreaterEqual(steps, 3)
        self.assertIn("T1", mod.CHANNELS)
        self.assertIn("T1", self.st["last_seen"])

    def test_backoff_pauses_job(self):
        mod = self.mod
        job = mod.GenJob("Thread refresh", mod.thread_refresh_iter(self.st))
        mod._rl_until = time.time() + 60
        self.assertEqual(job.run_slice(self.st, time.time() + 1, True), 0)
        self.assertFalse(job.done())
        mod._rl_until = 0
        self._run(job)
        self.assertIn("T1", mod.CHANNELS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
