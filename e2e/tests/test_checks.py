"""Offline tests for e2e/checks.py on synthetic transcripts.

Run from the repo root: python3 -m unittest discover -s e2e/tests -t .
"""
import os
import sys
import unittest

E2E = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, E2E)

import checks  # noqa: E402

IDS = {"SUT": "900", "A": "901", "B": "902", "SUT_NAME": "muse-test"}
AUTHOR_IDS = {"sut": "900", "a": "901", "b": "902"}
T0 = 1_800_000_000  # epoch seconds of the scenario start


def snowflake(secs):
    return str((int(secs * 1000) - checks.DISCORD_EPOCH_MS) << 22)


def iso(secs):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(secs, timezone.utc).isoformat()


def msg(author, at, content="", reply_to=None, **extra):
    """A transcript message posted `at` seconds after T0 (its id is the
    matching snowflake, so ids order like times)."""
    m = {"id": snowflake(T0 + at), "author": author,
         "author_id": AUTHOR_IDS[author], "ts": iso(T0 + at),
         "content": content, "reply_to": reply_to, "attachments": [],
         "mentions": [], "mention_everyone": False, "mention_roles": []}
    m.update(extra)
    return m


def transcript(messages, labels=None, **extra):
    tr = {"scenario": "t", "channel_id": "1", "started_at": iso(T0),
          "ids": dict(IDS), "labels": labels or {}, "converse_turns": None,
          "messages": messages,
          "sut_files": {"quarantine": None, "ledger": None},
          "monitor_log_tail": ""}
    tr.update(extra)
    return tr


def run(check, tr):
    """Evaluate one check; returns the single result."""
    (res,) = checks.evaluate({"expect": {"checks": [check]}}, tr)
    return res


def basic():
    """A mentions at t=0 (q1), B chats at t=1 (q2), SUT replies to q1
    at t=4."""
    q1 = msg("a", 0, "<@900> hi")
    q2 = msg("b", 1, "unrelated")
    r1 = msg("sut", 4, "ack 1", reply_to=q1["id"], mentions=["901"])
    return transcript([q1, q2, r1], {"q1": q1["id"], "q2": q2["id"]}), \
        q1, q2, r1


class Evaluate(unittest.TestCase):
    def test_one_result_per_check_in_order(self):
        tr, *_ = basic()
        sc = {"expect": {"checks": [{"sut_reply_count": {"eq": 1}},
                                    {"replies_to": "q1"},
                                    {"max_chars": 5}]}}
        res = checks.evaluate(sc, tr)
        self.assertEqual([r["name"] for r in res],
                         ["sut_reply_count", "replies_to", "max_chars"])
        self.assertTrue(all(r["ok"] for r in res))
        for r in res:
            self.assertEqual(set(r), {"name", "ok", "detail"})

    def test_unknown_check(self):
        tr, *_ = basic()
        r = run({"bogus": 1}, tr)
        self.assertEqual(r, {"name": "bogus", "ok": False,
                             "detail": "unknown check"})

    def test_malformed_entry(self):
        tr, *_ = basic()
        (r,) = checks.evaluate({"expect": {"checks": ["replies_to"]}}, tr)
        self.assertFalse(r["ok"])

    def test_no_checks(self):
        self.assertEqual(checks.evaluate({}, {}), [])

    def test_missing_label_fails_cleanly(self):
        tr, *_ = basic()
        r = run({"replies_to": "q7"}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("label 'q7'", r["detail"])

    def test_bad_argument_never_raises(self):
        tr, *_ = basic()
        r = run({"latency": "q1"}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("check crashed", r["detail"])

    def test_empty_transcript_never_raises(self):
        for name in checks.CHECKS:
            arg = {"sut_reply_count": {"eq": 0}, "total_chars": {"max": 1},
                   "converse_turns": {"max": 1},
                   "latency": {"from": "q1", "max": 1},
                   "mentions": {"label": "q1", "user": "a"},
                   "ledger_status": {"label": "q1", "status": "replied"},
                   "replies_to_any": ["q1"], "max_chars": 10,
                   "content_matches": "x"}.get(name, "q1")
            r = run({name: arg}, {})
            self.assertIn("detail", r)


class SutReplyCount(unittest.TestCase):
    def test_pass_and_fail(self):
        tr, *_ = basic()
        self.assertTrue(run({"sut_reply_count": {"eq": 1}}, tr)["ok"])
        self.assertTrue(run({"sut_reply_count": {"min": 1, "max": 2}},
                            tr)["ok"])
        r = run({"sut_reply_count": {"eq": 0}}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("1 SUT message(s), expected 0", r["detail"])
        self.assertIn('"ack 1"', r["detail"])
        self.assertFalse(run({"sut_reply_count": {"min": 2}}, tr)["ok"])
        tr["messages"].append(msg("sut", 5, "again"))
        r = run({"sut_reply_count": {"max": 1}}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("2 SUT message(s), expected at most 1", r["detail"])


class RepliesTo(unittest.TestCase):
    def test_pass(self):
        tr, *_ = basic()
        r = run({"replies_to": "q1"}, tr)
        self.assertTrue(r["ok"], r["detail"])

    def test_fail_wrong_target(self):
        tr, *_ = basic()
        r = run({"replies_to": "q2"}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("1 of 1 SUT message(s) do not reply to q2", r["detail"])

    def test_fail_unthreaded_extra(self):
        tr, *_ = basic()
        tr["messages"].append(msg("sut", 6, "part two"))
        r = run({"replies_to": "q1"}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("not a reply", r["detail"])

    def test_fail_no_sut_messages(self):
        tr, q1, q2, r1 = basic()
        tr["messages"].remove(r1)
        r = run({"replies_to": "q1"}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("no SUT messages", r["detail"])


class RepliesToAny(unittest.TestCase):
    def test_pass_and_fail(self):
        tr, q1, q2, r1 = basic()
        tr["messages"].append(msg("sut", 5, "ack 1", reply_to=q2["id"]))
        self.assertTrue(run({"replies_to_any": ["q1", "q2"]}, tr)["ok"])
        r = run({"replies_to_any": ["q1"]}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("1 of 2", r["detail"])

    def test_fail_empty(self):
        tr, q1, q2, r1 = basic()
        tr["messages"].remove(r1)
        self.assertFalse(run({"replies_to_any": ["q1"]}, tr)["ok"])


class NoSutReplyTo(unittest.TestCase):
    def test_pass_and_fail(self):
        tr, *_ = basic()
        self.assertTrue(run({"no_sut_reply_to": "q2"}, tr)["ok"])
        r = run({"no_sut_reply_to": "q1"}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("1 SUT message(s) reply to q1", r["detail"])


class NoSutBefore(unittest.TestCase):
    def test_pass(self):
        tr, *_ = basic()
        self.assertTrue(run({"no_sut_before": "q1"}, tr)["ok"])

    def test_fail_standalone_before(self):
        tr, *_ = basic()
        early = msg("sut", 0.5, "jumping in!")
        tr["messages"].insert(1, early)
        r = run({"no_sut_before": "q2"}, tr)
        self.assertFalse(r["ok"])
        self.assertIn(early["id"], r["detail"])

    def test_non_numeric_ids_fall_back_to_ts(self):
        tr = transcript([msg("a", 0, id="x1"), msg("sut", 2, id="x2")],
                        {"q1": "x1"})
        self.assertTrue(run({"no_sut_before": "q1"}, tr)["ok"])
        tr["messages"][1]["ts"] = iso(T0 - 1)
        self.assertFalse(run({"no_sut_before": "q1"}, tr)["ok"])


class Latency(unittest.TestCase):
    def test_pass(self):
        tr, *_ = basic()
        r = run({"latency": {"from": "q1", "max": 10}}, tr)
        self.assertTrue(r["ok"], r["detail"])
        self.assertIn("4.0s", r["detail"])

    def test_fail_too_slow(self):
        tr, *_ = basic()
        r = run({"latency": {"from": "q1", "max": 3}}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("after 4.0s, limit 3s", r["detail"])

    def test_fail_no_reply_to_label(self):
        tr, *_ = basic()
        r = run({"latency": {"from": "q2", "max": 60}}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("no SUT message replies to q2", r["detail"])

    def test_uses_first_reply(self):
        tr, q1, q2, r1 = basic()
        tr["messages"].append(msg("sut", 50, "late", reply_to=q1["id"]))
        self.assertTrue(run({"latency": {"from": "q1", "max": 5}}, tr)["ok"])

    def test_snowflake_fallback_without_ts(self):
        tr, q1, q2, r1 = basic()
        for m in tr["messages"]:
            m.pop("ts")
        self.assertTrue(run({"latency": {"from": "q1", "max": 5}}, tr)["ok"])
        self.assertFalse(run({"latency": {"from": "q1", "max": 3}},
                             tr)["ok"])

    def test_label_missing_from_messages_uses_snowflake(self):
        tr, q1, q2, r1 = basic()
        tr["messages"].remove(q1)
        self.assertTrue(run({"latency": {"from": "q1", "max": 5}}, tr)["ok"])

    def test_z_suffix_timestamps(self):
        tr, q1, q2, r1 = basic()
        for m in tr["messages"]:
            m["ts"] = m["ts"].replace("+00:00", "Z")
        self.assertTrue(run({"latency": {"from": "q1", "max": 5}}, tr)["ok"])


class Chars(unittest.TestCase):
    def test_max_chars(self):
        tr, *_ = basic()
        tr["messages"].append(msg("sut", 5, "x" * 2000))
        self.assertTrue(run({"max_chars": 2000}, tr)["ok"])
        tr["messages"].append(msg("sut", 6, "x" * 2001))
        r = run({"max_chars": 2000}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("1 SUT message(s) longer than 2000", r["detail"])
        self.assertIn("2001", r["detail"])

    def test_total_chars(self):
        tr, *_ = basic()
        tr["messages"].append(msg("sut", 5, "x" * 1795))
        self.assertTrue(run({"total_chars": {"min": 1700, "max": 1800}},
                            tr)["ok"])
        r = run({"total_chars": {"max": 1000}}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("total 1800 chars", r["detail"])
        self.assertFalse(run({"total_chars": {"min": 1801}}, tr)["ok"])


class ContentMatches(unittest.TestCase):
    def test_pass_and_fail(self):
        tr, *_ = basic()
        self.assertTrue(run({"content_matches": "^ack 1$"}, tr)["ok"])
        r = run({"content_matches": "^ack 3$"}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("do not match /^ack 3$/", r["detail"])

    def test_fail_when_no_sut_messages(self):
        tr, q1, q2, r1 = basic()
        tr["messages"].remove(r1)
        self.assertFalse(run({"content_matches": ".*"}, tr)["ok"])


class NoMassMentions(unittest.TestCase):
    def test_pass_with_neutralized_text(self):
        tr, *_ = basic()
        tr["messages"].append(msg("sut", 5, "hey @​everyone <@&1>"))
        self.assertTrue(run({"no_mass_mentions": True}, tr)["ok"])

    def test_fail_mention_everyone(self):
        tr, *_ = basic()
        tr["messages"].append(msg("sut", 5, "hi", mention_everyone=True))
        r = run({"no_mass_mentions": True}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("mention_everyone is true", r["detail"])

    def test_fail_role(self):
        tr, *_ = basic()
        tr["messages"].append(msg("sut", 5, "<@&1>", mention_roles=["1"]))
        r = run({"no_mass_mentions": True}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("mention_roles ['1']", r["detail"])

    def test_fail_raw_here(self):
        tr, *_ = basic()
        tr["messages"].append(msg("sut", 5, "listen up @here"))
        r = run({"no_mass_mentions": True}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("raw @here", r["detail"])

    def test_driver_messages_ignored(self):
        tr, *_ = basic()
        tr["messages"].append(msg("a", 5, "@everyone", mention_everyone=True))
        self.assertTrue(run({"no_mass_mentions": True}, tr)["ok"])


class NoDuplicateReplies(unittest.TestCase):
    def test_pass_same_text_different_targets(self):
        tr, q1, q2, r1 = basic()
        tr["messages"].append(msg("sut", 5, "ack 1", reply_to=q2["id"]))
        self.assertTrue(run({"no_duplicate_replies": True}, tr)["ok"])

    def test_fail_duplicate(self):
        tr, q1, q2, r1 = basic()
        dup = msg("sut", 9, "ack 1", reply_to=q1["id"])
        tr["messages"].append(dup)
        r = run({"no_duplicate_replies": True}, tr)
        self.assertFalse(r["ok"])
        self.assertIn(r1["id"], r["detail"])
        self.assertIn(dup["id"], r["detail"])


class Mentions(unittest.TestCase):
    def setUp(self):
        q1 = msg("b", 0, "<@900> [[mention_a]]")
        self.q1 = q1
        self.tr = transcript([q1], {"q1": q1["id"]})

    def test_pass(self):
        self.tr["messages"].append(msg("sut", 3, "hey <@901>",
                                       reply_to=self.q1["id"],
                                       mentions=["902", "901"]))
        r = run({"mentions": {"label": "q1", "user": "a"}}, self.tr)
        self.assertTrue(r["ok"], r["detail"])

    def test_nickname_form(self):
        self.tr["messages"].append(msg("sut", 3, "hey <@!901>",
                                       reply_to=self.q1["id"],
                                       mentions=["901"]))
        self.assertTrue(run({"mentions": {"label": "q1", "user": "a"}},
                            self.tr)["ok"])

    def test_fail_reply_ping_only(self):
        # B is in mentions only because the reply pings B: not a mention.
        self.tr["messages"].append(msg("sut", 3, "ack 1",
                                       reply_to=self.q1["id"],
                                       mentions=["902"]))
        r = run({"mentions": {"label": "q1", "user": "b"}}, self.tr)
        self.assertFalse(r["ok"])
        self.assertIn("<@902>", r["detail"])

    def test_fail_text_but_no_ping(self):
        self.tr["messages"].append(msg("sut", 3, "hey <@901>",
                                       reply_to=self.q1["id"],
                                       mentions=["902"]))
        self.assertFalse(run({"mentions": {"label": "q1", "user": "a"}},
                             self.tr)["ok"])

    def test_fail_no_reply(self):
        self.tr["messages"].append(msg("sut", 3, "hey <@901>",
                                       mentions=["901"]))
        r = run({"mentions": {"label": "q1", "user": "a"}}, self.tr)
        self.assertFalse(r["ok"])
        self.assertIn("no SUT message replies to q1", r["detail"])


class Quarantined(unittest.TestCase):
    def test_pass_trigger_and_fragment(self):
        tr, q1, q2, r1 = basic()
        tr["sut_files"]["quarantine"] = [
            {"message_id": "5", "matched_pattern": "x"},
            {"message_id": q1["id"], "matched_pattern": "ignore...",
             "fragments": [{"message_id": q2["id"], "content": "y"}]}]
        r = run({"quarantined": "q1"}, tr)
        self.assertTrue(r["ok"])
        self.assertIn("ignore...", r["detail"])
        self.assertTrue(run({"quarantined": "q2"}, tr)["ok"])

    def test_fail_not_listed(self):
        tr, *_ = basic()
        tr["sut_files"]["quarantine"] = [{"message_id": "5"}]
        r = run({"quarantined": "q1"}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("not in quarantine.json (1 entries", r["detail"])

    def test_fail_no_file(self):
        tr, *_ = basic()
        r = run({"quarantined": "q1"}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("no quarantine.json", r["detail"])

    def test_fail_not_a_list(self):
        tr, *_ = basic()
        tr["sut_files"]["quarantine"] = {"oops": 1}
        self.assertFalse(run({"quarantined": "q1"}, tr)["ok"])


class LedgerStatus(unittest.TestCase):
    def test_pass_and_fail(self):
        tr, q1, q2, r1 = basic()
        tr["sut_files"]["ledger"] = {"entries": {
            q1["id"]: {"status": "skipped",
                       "reason": "no reply (worker's call)"}}}
        r = run({"ledger_status": {"label": "q1", "status": "skipped"}}, tr)
        self.assertTrue(r["ok"])
        self.assertIn("worker's call", r["detail"])
        r = run({"ledger_status": {"label": "q1", "status": "replied"}}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("'skipped'", r["detail"])
        self.assertIn("expected 'replied'", r["detail"])

    def test_fail_missing_entry(self):
        tr, *_ = basic()
        tr["sut_files"]["ledger"] = {"entries": {}}
        r = run({"ledger_status": {"label": "q1", "status": "replied"}}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("no ledger entry", r["detail"])

    def test_fail_no_ledger(self):
        tr, *_ = basic()
        r = run({"ledger_status": {"label": "q1", "status": "replied"}}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("mention_ledger.json", r["detail"])


class ConverseTurns(unittest.TestCase):
    def test_pass_and_fail(self):
        tr, *_ = basic()
        tr["converse_turns"] = 3
        self.assertTrue(run({"converse_turns": {"max": 5}}, tr)["ok"])
        tr["converse_turns"] = 8
        r = run({"converse_turns": {"max": 5}}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("8 SUT turn(s)", r["detail"])

    def test_fail_missing(self):
        tr, *_ = basic()
        r = run({"converse_turns": {"max": 5}}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("no converse_turns", r["detail"])


class LogChecks(unittest.TestCase):
    LOG = ("Note: config.yaml uses the old layout; it still works as is.\n"
           "Queued mention (follow-up) from ua in #c: hi\n")

    def test_contains_uses_the_scenario_log(self):
        tr, *_ = basic()
        tr["monitor_log"] = self.LOG
        tr["monitor_log_tail"] = "something else entirely"
        r = run({"log_contains": r"^Queued \w+ \(follow-up\) from "}, tr)
        self.assertTrue(r["ok"], r)
        self.assertIn("scenario's monitor.log", r["detail"])
        self.assertIn("Queued mention (follow-up)", r["detail"])
        r = run({"log_contains": "uses the old layout"}, tr)
        self.assertTrue(r["ok"], r)
        r = run({"log_contains": "something else"}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("not found", r["detail"])

    def test_contains_falls_back_to_the_tail(self):
        tr, *_ = basic()
        tr["monitor_log_tail"] = self.LOG
        r = run({"log_contains": "old layout"}, tr)
        self.assertTrue(r["ok"], r)
        self.assertIn("tail", r["detail"])
        tr["monitor_log_tail"] = ""
        self.assertFalse(run({"log_contains": "x"}, tr)["ok"])

    def test_lacks(self):
        tr, *_ = basic()
        tr["monitor_log"] = self.LOG
        r = run({"log_lacks": r"\(follow-up\)"}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("1 line(s)", r["detail"])
        r = run({"log_lacks": "CONFIG ERROR"}, tr)
        self.assertTrue(r["ok"], r)


class Held(unittest.TestCase):
    def tr(self, holds):
        tr, q1, *_ = basic()
        tr["sut_files"]["holds"] = holds
        return tr, q1

    def test_pass_by_item_or_reply_to(self):
        tr, q1 = self.tr({"abc": {"item": {"item": "x", "reply_to": None}},
                          "def": {"item": {"item": "7", "reply_to": "8"}}})
        tr["labels"]["q1"] = "7"
        r = run({"held": "q1"}, tr)
        self.assertTrue(r["ok"], r)
        self.assertIn("hold def", r["detail"])
        tr["labels"]["q1"] = "8"
        self.assertTrue(run({"held": "q1"}, tr)["ok"])

    def test_fail(self):
        tr, q1 = self.tr({"abc": {"item": {"item": "x", "text": "secret"}}})
        r = run({"held": "q1"}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("is not held", r["detail"])
        self.assertNotIn("secret", r["detail"])  # never the held text
        for holds in (None, {}, []):
            tr, _ = self.tr(holds)
            r = run({"held": "q1"}, tr)
            self.assertFalse(r["ok"])
            self.assertIn("no never_share holds", r["detail"])


class QueueItem(unittest.TestCase):
    def setUp(self):
        self.t, self.q1, self.q2, _ = basic()

    def with_items(self, items):
        self.t["sut_files"]["worker_items"] = items
        return self.t

    def test_pass_trigger_and_fragment(self):
        tr = self.with_items([
            {"message_id": self.q1["id"], "fragment_ids": [],
             "follow_up": False, "kind": "mention",
             "settings": {"speaker": "owner", "where": "server"}},
            {"message_id": "5", "fragment_ids": [self.q2["id"]],
             "follow_up": True}])
        r = run({"queue_item": {"label": "q1", "follow_up": False,
                                "kind": "mention",
                                "settings": {"speaker": "owner"}}}, tr)
        self.assertTrue(r["ok"], r)
        self.assertIn("settings.speaker 'owner'", r["detail"])
        r = run({"queue_item": {"label": "q2", "follow_up": True}}, tr)
        self.assertTrue(r["ok"], r)

    def test_missing_field_is_false(self):
        tr = self.with_items([{"message_id": self.q1["id"]}])
        self.assertTrue(run({"queue_item": {"label": "q1",
                                            "follow_up": False}}, tr)["ok"])
        r = run({"queue_item": {"label": "q1", "follow_up": True}}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("follow_up False (expected True)", r["detail"])

    def test_newest_record_wins(self):
        tr = self.with_items([
            {"message_id": self.q1["id"], "follow_up": False},
            {"message_id": self.q1["id"], "follow_up": True}])
        self.assertTrue(run({"queue_item": {"label": "q1",
                                            "follow_up": True}}, tr)["ok"])

    def test_fail(self):
        tr = self.with_items([{"message_id": self.q1["id"],
                               "follow_up": True,
                               "settings": {"speaker": "member"}}])
        r = run({"queue_item": {"label": "q1",
                                "settings": {"speaker": "owner"}}}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("settings.speaker 'member' (expected 'owner')",
                      r["detail"])
        r = run({"queue_item": {"label": "q2", "follow_up": True}}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("never queued", r["detail"])
        tr = self.with_items(None)
        r = run({"queue_item": {"label": "q1", "follow_up": True}}, tr)
        self.assertFalse(r["ok"])
        self.assertIn("no worker items", r["detail"])

if __name__ == "__main__":
    unittest.main()
