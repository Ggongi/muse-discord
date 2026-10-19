"""Offline tests for claude_worker.py and judge.py.

No network and no real Claude: CLAUDE_BIN points at small fake `claude`
scripts written to a temp dir. Run from the repo root:

    python3 -m unittest discover -s e2e/tests -t .
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

E2E = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(E2E)
sys.path.insert(0, E2E)

import claude_worker  # noqa: E402
import judge  # noqa: E402

# Fake CLIs. Each records its argv and stdin next to itself.
FAKE_HEAD = """\
#!%s
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
prompt = sys.stdin.read()
with open(os.path.join(here, "calls.jsonl"), "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(),
                        "prompt": prompt}) + "\\n")
""" % sys.executable

FAKES = {
    # (a) a worker: answer every payload item through the append tool,
    # under the last burst fragment, one append per item (step 3), then
    # end with the one line `done` (step 5), as the prompt says.
    "append": FAKE_HEAD + textwrap.dedent("""\
        import subprocess
        payload = json.loads(prompt.split("```json\\n", 1)[1]
                             .split("\\n```", 1)[0])
        code = 0
        for it in payload["items"]:
            frags = it.get("burst_fragments") or []
            rt = frags[-1]["message_id"] if frags else it["message_id"]
            out = {"item": it["message_id"], "channel_id": it["channel_id"],
                   "reply_to": rt, "text": "ahoy " + it["author"]}
            r = subprocess.run([sys.executable, "bin/outbox-append.py"],
                               input=json.dumps(out), text=True,
                               capture_output=True)
            code = code or r.returncode
        print(json.dumps({"type": "result", "is_error": False,
                          "num_turns": 1 + len(payload["items"]),
                          "result": "done"}))
        sys.exit(code)
        """),
    # (b) hangs (with a child process, to check the group kill).
    "hang": FAKE_HEAD + textwrap.dedent("""\
        import subprocess, time
        subprocess.Popen(["sleep", "300"])
        time.sleep(300)
        """),
    # (c) garbage on stdout.
    "garbage": FAKE_HEAD + 'print("lol not json {verdict: maybe")\n',
    # (d) a valid verdict, wrapped in prose and a fence inside `result`.
    "verdict": FAKE_HEAD + textwrap.dedent("""\
        res = ('Here it is:\\n```json\\n{"verdict": "fail", "reason": '
               '"[2] ignored the question"}\\n```')
        print(json.dumps({"type": "result", "is_error": False,
                          "result": res}))
        """),
    "verdict_plain": FAKE_HEAD
    + 'print(\'{"verdict": "PASS", "reason": "nautical and brief"}\')\n',
    "is_error": FAKE_HEAD
    + 'print(json.dumps({"is_error": True, "result": "auth failed"}))\n',
    "exit2": FAKE_HEAD + 'sys.stderr.write("bad flag\\n"); sys.exit(2)\n',
}

QUEUE = [
    {"message_id": "1001", "channel_id": "500", "channel_name": "T #e2e",
     "guild_id": "400", "kind": "mention", "author": "driver-a",
     "author_id": "11", "content": "<@99> hello", "media": [],
     "history": [], "owner_command": True, "queued_at": 1},
    {"message_id": "1002", "channel_id": "500", "channel_name": "T #e2e",
     "guild_id": "400", "kind": "mention", "author": "driver-b",
     "author_id": "12", "content": "one\ntwo", "burst": True,
     "burst_fragments": [
         {"message_id": "1002", "content": "one", "timestamp": "t"},
         {"message_id": "1003", "content": "two", "timestamp": "t"}],
     "media": [{"path": "/x/media/1002_0.img.png",
                "content_type": "image/png", "source": "attachment"}]},
]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="e2e-claude-test-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.pkg = os.path.join(self.tmp, "pkg")
        os.makedirs(os.path.join(self.pkg, "bin"))
        os.makedirs(os.path.join(self.pkg, "media"))
        shutil.copy(os.path.join(REPO, "bin", "outbox-append.py"),
                    os.path.join(self.pkg, "bin"))
        shutil.copy(os.path.join(REPO, "worker-prompt.template.md"),
                    self.pkg)
        self.write("reply_queue.json", QUEUE)
        self.fakes = os.path.join(self.tmp, "fakes")
        os.makedirs(self.fakes)
        old = os.environ.get("CLAUDE_BIN")
        self.addCleanup(self.restore_env, old)

    def restore_env(self, old):
        if old is None:
            os.environ.pop("CLAUDE_BIN", None)
        else:
            os.environ["CLAUDE_BIN"] = old

    def write(self, name, data):
        with open(os.path.join(self.pkg, name), "w") as f:
            json.dump(data, f)

    def read(self, name):
        with open(os.path.join(self.pkg, name)) as f:
            return json.load(f)

    def use_fake(self, name):
        path = os.path.join(self.fakes, "claude-" + name)
        with open(path, "w") as f:
            f.write(FAKES[name])
        os.chmod(path, 0o755)
        os.environ["CLAUDE_BIN"] = path
        return path

    def calls(self):
        path = os.path.join(self.fakes, "calls.jsonl")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return [json.loads(line) for line in f]


class TestWorkerPieces(Base):
    def test_render_prompt_fills_seams_and_paths(self):
        with open(os.path.join(REPO, "worker-prompt.template.md")) as f:
            text, warnings = claude_worker.render_prompt(f.read(), self.pkg)
        self.assertEqual(warnings, [])
        self.assertNotIn("<package dir>", text)
        self.assertIn("python3 %s/bin/outbox-append.py" % self.pkg, text)
        self.assertIn("persona text from the wake payload", text)
        self.assertIn("NO media", text)
        self.assertNotIn("installing Muse fills this in", text)
        self.assertNotIn("This template is the prompt", text)

    def test_render_prompt_warns_on_missing_seams(self):
        text, warnings = claude_worker.render_prompt("hi <package dir>",
                                                     "/p")
        self.assertEqual(text, "hi /p")
        self.assertEqual(len(warnings), 2)

    def test_open_items_skip_answered_burst(self):
        self.write("reply_outbox.processing.json",
                   [{"channel_id": "500", "reply_to": "1003", "text": "x"}])
        items = claude_worker.open_items(self.pkg)
        self.assertEqual([i["message_id"] for i in items], ["1001"])

    def test_payload_matches_hook_projection(self):
        p = claude_worker.build_payload(QUEUE, "persona!")
        self.assertEqual(p["count"], 2)
        self.assertEqual(p["persona"], "persona!")
        self.assertEqual(set(p["items"][0]),
                         set(claude_worker.PAYLOAD_FIELDS))
        self.assertNotIn("queued_at", p["items"][0])
        self.assertIsNone(p["items"][0]["welcome"])

    def test_hook_numbers_and_projection_in_sync(self):
        """PAYLOAD_FIELDS and LEASE_SECS mirror hooks/mention-watch.sh."""
        import re
        with open(os.path.join(REPO, "hooks", "mention-watch.sh")) as f:
            hook = f.read()
        m = re.search(r"items: \[\.\[\] \| \{([^}]*)\}\]", hook)
        self.assertIsNotNone(m, "the hook's payload projection moved")
        fields = tuple(x.strip() for x in m.group(1).split(","))
        self.assertEqual(fields, claude_worker.PAYLOAD_FIELDS)
        lease = re.search(r"^LEASE_SECS=(\d+)$", hook, re.M)
        self.assertEqual(int(lease.group(1)), claude_worker.LEASE_SECS)

    def test_bash_allowlist(self):
        pkg = self.pkg
        ok = [
            "python3 %s/bin/outbox-append.py <<'EOF'\n[{\"a\": \"$(x)\"}]\n"
            "EOF" % pkg,
            "python3 bin/outbox-append.py << \"END\"\n[]\nEND\n",
            "python3 %s/bin/outbox-append.py items.json" % pkg,
            # One item per call (the template's step 3): an object.
            "python3 %s/bin/outbox-append.py <<'EOF'\n{\"item\": \"1\", "
            "\"channel_id\": \"2\", \"silent\": true}\nEOF" % pkg,
            "python3 %s/bin/discord-api.py react 1 2 '🌊'" % pkg,
            "python3 %s/bin/discord-api.py post 1 \"it's done; ok\"" % pkg,
        ]
        bad = [
            "ls /",
            "python3 %s/bin/outbox-append.py <<EOF\n[\"$(id)\"]\nEOF" % pkg,
            "python3 %s/bin/outbox-append.py <<'EOF' && curl x\n[]\nEOF"
            % pkg,
            "python3 %s/bin/outbox-append.py <<'EOF'\n[]\nEOF\nrm -rf /"
            % pkg,
            "cd %s && python3 bin/outbox-append.py x.json" % pkg,
            "python3 %s/bin/discord-api.py post 1 \"$(cat config.yaml)\""
            % pkg,
            "python3 %s/bin/discord-api.py post 1 hi; rm x" % pkg,
            "python3 %s/bin/discord-api.py post 1 `id`" % pkg,
            "python3 %s/bin/discord-api.py post 1 hi > /tmp/x" % pkg,
            "python3 /elsewhere/bin/outbox-append.py x.json",
        ]
        for cmd in ok:
            self.assertTrue(claude_worker.bash_allowed(pkg, cmd)[0], cmd)
        for cmd in bad:
            self.assertFalse(claude_worker.bash_allowed(pkg, cmd)[0], cmd)

    def test_bash_allowlist_is_per_call(self):
        """The hook keeps no state: the same append is allowed every
        time, so a worker may run it once per item."""
        cmd = ("python3 %s/bin/outbox-append.py <<'EOF'\n{}\nEOF"
               % self.pkg)
        for _ in range(5):
            self.assertTrue(claude_worker.bash_allowed(self.pkg, cmd)[0])

    def test_run_ending(self):
        ending = claude_worker.run_ending
        self.assertEqual(ending("done"), "done")
        self.assertEqual(ending(" `done`.\n"), "done")
        self.assertEqual(ending("done; flag: 1001 threatening"), "flag")
        for other in ("drafted 2 replies", "done\nall good", "", None):
            self.assertEqual(ending(other), "other", other)

    def test_template_has_the_steps_mirrored_here(self):
        """The worker template still asks for what this file and the
        fakes assume: one append per item, then `done`."""
        with open(os.path.join(REPO, "worker-prompt.template.md")) as f:
            text = f.read()
        self.assertIn("One call per item", text)
        self.assertIn("`done`", text)
        self.assertIn("done; flag:", text)

    def test_read_allowlist(self):
        pkg = self.pkg
        check = claude_worker.check_tool
        self.assertTrue(check(pkg, "Read", {
            "file_path": pkg + "/media/1002_0.img.png"})[0])
        self.assertTrue(check(pkg, "Read", {"file_path": "persona.md"})[0])
        for path in (pkg + "/config.yaml", pkg + "/media/../config.yaml",
                     "/etc/passwd", pkg + "/media"):
            self.assertFalse(check(pkg, "Read", {"file_path": path})[0],
                             path)
        self.assertFalse(check(pkg, "Write", {"file_path": "x"})[0])

    def test_hook_mode_prints_decision(self):
        script = os.path.join(E2E, "claude_worker.py")
        call = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
        r = subprocess.run([sys.executable, script, "--hook", "--pkg",
                            self.pkg], input=json.dumps(call),
                           capture_output=True, text=True, timeout=30)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PreToolUse")
        self.assertEqual(out["permissionDecision"], "deny")

    def test_worker_args_restrict_tools(self):
        args = claude_worker.worker_args(self.pkg)
        self.assertIn("-p", args)
        self.assertEqual(args[args.index("--tools") + 1], "Bash,Read")
        self.assertEqual(args[args.index("--permission-mode") + 1],
                         "dontAsk")
        settings = json.loads(args[args.index("--settings") + 1])
        hook = settings["hooks"]["PreToolUse"][0]
        self.assertEqual(hook["matcher"], "*")
        self.assertIn("--hook", hook["hooks"][0]["command"])


class TestWorkerRuns(Base):
    def test_answers_each_item_once(self):
        self.use_fake("append")
        log = os.path.join(self.tmp, "worker.log")
        w = claude_worker.Worker(self.pkg, log, timeout=30)
        self.assertIsNotNone(w.poll())
        w.wait(30)
        outbox = self.read("reply_outbox.json")
        self.assertEqual(sorted((o["item"], o["reply_to"]) for o in outbox),
                         [("1001", "1001"), ("1002", "1003")])
        # The queue still holds both (no monitor here): answered, so no
        # second wake, even after the leases would have run out.
        w.lease = 0
        self.assertIsNone(w.poll())
        # ...and not after the monitor took the outbox for sending.
        os.rename(os.path.join(self.pkg, "reply_outbox.json"),
                  os.path.join(self.pkg, "spare.json"))
        self.assertIsNone(w.poll())
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        call = calls[0]
        self.assertEqual(os.path.realpath(call["cwd"]),
                         os.path.realpath(self.pkg))
        self.assertIn("Beacon", call["prompt"])  # the test persona
        self.assertIn('"count": 2', call["prompt"])
        with open(log) as f:
            text = f.read()
        self.assertIn("wake 1: done exit=0", text)
        self.assertIn("turns=3", text)  # one append call per item
        self.assertIn("ending=done result='done'", text)
        self.assertRegex(text, r"duration=\d+\.\ds")
        self.assertTrue(os.path.exists(
            os.path.join(self.tmp, "claude_wakes", "wake-001.json")))

    def test_new_item_while_busy_gets_its_own_wake(self):
        self.use_fake("hang")
        w = claude_worker.Worker(self.pkg, timeout=30)
        self.addCleanup(w.shutdown)
        w.poll()
        self.assertIsNone(w.poll())  # both leased by the busy wake
        queue = QUEUE + [dict(QUEUE[0], message_id="1004")]
        self.write("reply_queue.json", queue)
        # The next poll hands out only the new item.
        t = w.poll()
        self.assertIsNotNone(t)
        time.sleep(1)
        payloads = [c["prompt"] for c in self.calls()]
        self.assertEqual(len(payloads), 2)
        second = [p for p in payloads if '"message_id": "1004"' in p]
        self.assertEqual(len(second), 1)
        self.assertNotIn('"message_id": "1001"', second[0])

    def test_timeout_kills_the_run(self):
        self.use_fake("hang")
        log = os.path.join(self.tmp, "worker.log")
        w = claude_worker.Worker(self.pkg, log, timeout=1)
        start = time.monotonic()
        w.poll()
        w.wait(30)
        self.assertLess(time.monotonic() - start, 20)
        with open(log) as f:
            self.assertIn("exit=timeout", f.read())
        self.assertIsNone(w.poll())  # still leased: not re-handed
        self.assertFalse(os.path.exists(
            os.path.join(self.pkg, "reply_outbox.json")))

    def test_missing_cli_is_logged(self):
        os.environ["CLAUDE_BIN"] = os.path.join(self.tmp, "nope")
        log = os.path.join(self.tmp, "worker.log")
        w = claude_worker.Worker(self.pkg, log, timeout=5)
        w.poll()
        w.wait(10)
        with open(log) as f:
            self.assertIn("exit=error", f.read())

    def test_cli_runs_until_sigterm(self):
        self.use_fake("append")
        log = os.path.join(self.tmp, "worker.log")
        proc = subprocess.Popen(
            [sys.executable, os.path.join(E2E, "claude_worker.py"),
             "--pkg", self.pkg, "--log", log, "--poll", "0.2"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.time() + 30
            outbox = os.path.join(self.pkg, "reply_outbox.json")
            while time.time() < deadline and not os.path.exists(outbox):
                time.sleep(0.2)
            time.sleep(1)  # several more polls: no second answer
            proc.send_signal(signal.SIGTERM)
            self.assertEqual(proc.wait(timeout=20), 0)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(len(self.read("reply_outbox.json")), 2)
        self.assertEqual(len(self.calls()), 1)
        with open(log) as f:
            self.assertIn("stopping", f.read())


TRANSCRIPT = {
    "scenario": "persona_voice", "channel_id": "500",
    "started_at": "2026-10-17T12:00:00.000000+00:00",
    "ids": {"SUT": "99", "A": "11", "B": "12", "SUT_NAME": "muse-test"},
    "labels": {"q1": "m1"},
    "messages": [
        {"id": "m1", "author": "a", "author_id": "11",
         "ts": "2026-10-17T12:00:01.5+00:00", "content": "<@99> hi there",
         "reply_to": None, "attachments": ["cat.png"]},
        {"id": "m2", "author": "sut", "author_id": "99",
         "ts": "2026-10-17T12:00:09Z", "content": "Ahoy <@11>!",
         "reply_to": "m1", "attachments": []},
        {"id": "m3", "author": "b", "author_id": "12",
         "ts": "2026-10-17T12:00:20+00:00", "content": "late",
         "reply_to": "zzz"},
    ],
}
SCENARIO = {"id": "persona_voice", "tier": 2, "b_is_bot": True,
            "judge": {"rubric": "SUT replies once, in a nautical voice."}}


class TestJudge(Base):
    def test_render_transcript(self):
        text, who = judge.render_transcript(SCENARIO, TRANSCRIPT)
        lines = text.splitlines()
        self.assertEqual(lines[0], "[1] +1s A (q1): @SUT hi there")
        self.assertEqual(lines[1], "    attachments: cat.png")
        self.assertEqual(lines[2], "[2] +9s SUT -> [1]: Ahoy @A!")
        self.assertIn("B -> (earlier message): late", lines[3])
        self.assertIn("B = another bot", who)
        self.assertIn("A = the server owner (a human)", who)

    def test_prompt_has_rubric_persona_transcript(self):
        p = judge.build_prompt(SCENARIO, TRANSCRIPT)
        self.assertIn("nautical voice", p)
        self.assertIn("Beacon", p)
        self.assertIn("[2] +9s SUT -> [1]", p)

    def test_valid_verdict_in_result(self):
        self.use_fake("verdict")
        v = judge.judge(SCENARIO, TRANSCRIPT, timeout=30)
        self.assertEqual(v, {"verdict": "fail",
                             "reason": "[2] ignored the question"})
        argv = self.calls()[0]["argv"]
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertIn("--output-format", argv)

    def test_plain_verdict(self):
        self.use_fake("verdict_plain")
        self.assertEqual(judge.judge(SCENARIO, TRANSCRIPT)["verdict"],
                         "pass")

    def test_garbage_is_error(self):
        self.use_fake("garbage")
        v = judge.judge(SCENARIO, TRANSCRIPT, timeout=30)
        self.assertEqual(v["verdict"], "error")
        self.assertIn("unparseable", v["reason"])

    def test_cli_error_result(self):
        self.use_fake("is_error")
        v = judge.judge(SCENARIO, TRANSCRIPT, timeout=30)
        self.assertEqual(v["verdict"], "error")
        self.assertIn("auth failed", v["reason"])

    def test_nonzero_exit(self):
        self.use_fake("exit2")
        v = judge.judge(SCENARIO, TRANSCRIPT, timeout=30)
        self.assertEqual(v["verdict"], "error")
        self.assertIn("bad flag", v["reason"])

    def test_timeout(self):
        self.use_fake("hang")
        start = time.monotonic()
        v = judge.judge(SCENARIO, TRANSCRIPT, timeout=1)
        self.assertLess(time.monotonic() - start, 20)
        self.assertEqual(v["verdict"], "error")
        self.assertIn("timed out", v["reason"])

    def test_missing_cli(self):
        os.environ["CLAUDE_BIN"] = os.path.join(self.tmp, "nope")
        v = judge.judge(SCENARIO, TRANSCRIPT)
        self.assertEqual(v["verdict"], "error")

    def test_never_raises_on_junk_input(self):
        self.use_fake("verdict_plain")
        v = judge.judge(None, {"messages": [{"id": None}], "labels": 3})
        self.assertIn(v["verdict"], ("pass", "error"))


if __name__ == "__main__":
    unittest.main()
