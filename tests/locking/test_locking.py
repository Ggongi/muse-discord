"""Locking and speed, offline: worker appends during a slow send are
never lost, concurrent appends are each sent once, the queue is never
resurrected, the reply lock, typing indicators, burst coalescing and
early flush, the latency line, and bin/outbox-append.py's validation.
MUT_DIR = the package tree under test (default: this checkout).
Run: python3 tests/locking/test_locking.py [-k name]"""
import json
import os
import subprocess
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import (ARGV, monitor, fresh_pkg, new_state,  # noqa: E402
                     snowflake, msg, read, write, capture, sent_texts,
                     typing_calls)

m = monitor


def append_via_helper(pkg, items, check=True):
    r = subprocess.run([sys.executable,
                        os.path.join(pkg, "bin", "outbox-append.py")],
                       input=json.dumps(items), capture_output=True,
                       text=True)
    if check:
        assert r.returncode == 0, r.stderr
    return r


def qitem(mid, cid="101", **kw):
    d = {"message_id": mid, "channel_id": cid,
         "queued_at": time.time()}
    d.update(kw)
    return d


# ---------------------------------------------------------------------------
def test_worker_append_during_slow_send_not_lost():
    pkg = fresh_pkg()
    st = new_state()
    m.random.uniform = lambda a, b: 0.4  # typing beat between parts
    orig = m.api_post

    def slow_post(path, data):
        if path.endswith("/messages"):
            time.sleep(0.3)
        return orig(path, data)
    m.api_post = slow_post
    try:
        a, b = snowflake(), snowflake()
        write(m.QUEUE_FILE, [qitem(a), qitem(b)])
        write(m.OUTBOX_FILE, [{"item": a, "channel_id": "101",
                               "reply_to": a,
                               "parts": ["one", "two", "three"]}])
        t = threading.Thread(target=m.process_outbox, args=(st,))
        t.start()
        time.sleep(0.4)  # mid-send
        assert os.path.exists(m.OUTBOX_PROCESSING_FILE)
        append_via_helper(pkg, [{"item": b, "channel_id": "101",
                                 "reply_to": b, "text": "for b"}])
        t.join()
        assert sent_texts() == ["one", "two", "three"], sent_texts()
        out = read(m.OUTBOX_FILE)
        assert [i["item"] for i in out] == [b], out  # NOT lost
        assert not os.path.exists(m.OUTBOX_PROCESSING_FILE)
        assert [q["message_id"] for q in read(m.QUEUE_FILE)] == [b]
        m.process_outbox(st)
        assert sent_texts()[-1] == "for b"
        assert read(m.QUEUE_FILE) == [] and read(m.OUTBOX_FILE) == []
    finally:
        m.api_post = orig


def test_concurrent_appends_stress_each_sent_once():
    """20 helper subprocesses append while the monitor sends with slow
    posts: every item is sent exactly once."""
    pkg = fresh_pkg()
    st = new_state()
    orig = m.api_post

    def slow_post(path, data):
        if path.endswith("/messages"):
            time.sleep(0.05)
        return orig(path, data)
    m.api_post = slow_post
    try:
        ids = [snowflake() for _ in range(20)]
        write(m.QUEUE_FILE, [qitem(i) for i in ids])
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                m.process_outbox(st)
                time.sleep(0.01)
        t = threading.Thread(target=loop)
        t.start()
        procs = [subprocess.Popen(
            [sys.executable, os.path.join(pkg, "bin", "outbox-append.py")],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True)
            for _ in ids]
        for p, i in zip(procs, ids):
            p.stdin.write(json.dumps([{"item": i, "channel_id": "101",
                                       "reply_to": i, "text": "r" + i}]))
            p.stdin.close()
        for p in procs:
            assert p.wait() == 0
        deadline = time.time() + 20
        while time.time() < deadline and len(sent_texts()) < len(ids):
            time.sleep(0.1)
        time.sleep(0.3)
        stop.set()
        t.join()
        texts = sent_texts()
        assert sorted(texts) == sorted("r" + i for i in ids), texts
        assert read(m.OUTBOX_FILE) == [] and read(m.QUEUE_FILE) == []
    finally:
        m.api_post = orig


def test_queue_write_no_resurrect_no_drop():
    """The main loop's flush (slow: media downloads) races a concurrent
    removal and append; the delta write keeps both."""
    fresh_pkg()
    st = new_state()
    x, y, w = snowflake(), snowflake(), snowflake()
    write(m.QUEUE_FILE, [qitem(x), qitem(y)])
    trig = msg()
    m.stage_trigger(st, trig, "S #c", "G", "mention")
    st["pending"][next(iter(st["pending"]))]["first_seen"] -= 10

    def slow_media(_msg):
        time.sleep(1.0)
        return [], [], []
    m.collect_media = slow_media
    # Main-loop pattern (main(): snapshot, flush, delta write).
    queue = m.load_queue()
    n_before = len(queue)
    t = threading.Thread(target=lambda: (
        time.sleep(0.3),
        m.queue_edit(add=[qitem(w)], drop=lambda q: q["message_id"] == x)))
    t.start()
    with capture():
        assert m.flush_pending(st, queue)
    t.join()
    m.queue_edit(add=queue[n_before:])
    m.collect_media = lambda _m: ([], [], [])
    ids = [q["message_id"] for q in read(m.QUEUE_FILE)]
    assert ids == [y, w, trig["id"]], ids


def test_queue_stress_threads():
    """One thread appends 200 items one at a time; another removes every
    item it sees: none resurrected, none lost, no duplicates."""
    fresh_pkg()
    write(m.QUEUE_FILE, [])
    ids = [str(1000 + i) for i in range(200)]
    removed = set()
    done = threading.Event()

    def adder():
        for i in ids:
            m.queue_edit(add=[{"message_id": i}])
        done.set()

    def remover():
        while True:
            fin = done.is_set()
            seen = {q["message_id"] for q in m.load_queue()}
            take = {i for i in seen if int(i) % 2 == 0}
            if take:
                m.queue_edit(drop=lambda q: q["message_id"] in take)
                removed.update(take)
            if fin and not take:
                break
    ta, tr = threading.Thread(target=adder), threading.Thread(target=remover)
    ta.start()
    tr.start()
    ta.join()
    tr.join()
    final = [q["message_id"] for q in read(m.QUEUE_FILE)]
    assert final == [i for i in ids if int(i) % 2], final[:10]
    assert removed == {i for i in ids if int(i) % 2 == 0}


def test_atomic_write_unique_temps():
    fresh_pkg()
    path = m.QUEUE_FILE
    stop = threading.Event()
    bad = []

    def writer(tag):
        for n in range(300):
            m.atomic_write(path, [{"w": tag, "n": n, "pad": "x" * 2000}])

    def reader():
        while not stop.is_set():
            try:
                with open(path) as f:
                    json.load(f)
            except FileNotFoundError:
                pass
            except ValueError as e:
                bad.append(e)
    r = threading.Thread(target=reader)
    r.start()
    ws = [threading.Thread(target=writer, args=(t,)) for t in "ab"]
    for w in ws:
        w.start()
    for w in ws:
        w.join()
    stop.set()
    r.join()
    assert not bad, bad[:3]
    leftovers = [f for f in os.listdir(os.path.dirname(path))
                 if f.endswith(".tmp")]
    assert not leftovers, leftovers


def test_monitor_removes_answered_items():
    fresh_pkg()
    st = new_state()
    mention, welcome, trig, frag1, frag2, silent_id, gone = (
        snowflake() for _ in range(7))
    old_welcome = snowflake()
    st["reply_aliases"] = {frag1: trig, frag2: trig}
    write(m.QUEUE_FILE, [
        qitem(mention),
        qitem(welcome, kind="welcome", welcome=True),
        qitem(trig, burst=True, burst_fragments=[
            {"message_id": frag1}, {"message_id": frag2}]),
        qitem(silent_id),
        qitem(old_welcome, kind="welcome", welcome=True),
    ])
    write(m.OUTBOX_FILE, [
        {"item": mention, "channel_id": "101", "reply_to": mention,
         "text": "hello"},
        {"item": welcome, "channel_id": "101", "text": "welcome!"},
        # old worker: no item, reply under the LAST fragment
        {"channel_id": "101", "reply_to": frag2, "text": "burst reply"},
        {"item": silent_id, "channel_id": "101", "reply_to": silent_id,
         "silent": True},
        # old worker already removed its queue item: nothing breaks
        {"channel_id": "101", "reply_to": gone, "text": "late"},
        # old worker welcome without item: left for the worker to remove
        {"channel_id": "101", "text": "old-style welcome"},
    ])
    with capture() as out:
        m.process_outbox(st)
    assert read(m.QUEUE_FILE) == [qitem(old_welcome, kind="welcome",
                                        welcome=True)] or \
        [q["message_id"] for q in read(m.QUEUE_FILE)] == [old_welcome], \
        read(m.QUEUE_FILE)
    assert len(sent_texts()) == 5, sent_texts()
    for mid in (mention, welcome, trig, frag2, silent_id):
        assert mid in st["replied_to"], mid
    assert "Latency " + mention in out.getvalue(), out.getvalue()
    # a second worker drafts the same welcome: suppressed
    write(m.OUTBOX_FILE, [{"item": welcome, "channel_id": "101",
                           "text": "welcome again"}])
    with capture() as out:
        m.process_outbox(st)
    assert len(sent_texts()) == 5
    assert "Skipping duplicate" in out.getvalue()


def test_malformed_items_rejected_without_blocking():
    fresh_pkg()
    st = new_state()
    good, empty = snowflake(), snowflake()
    write(m.QUEUE_FILE, [qitem(good), qitem(empty)])
    write(m.OUTBOX_FILE, [
        {"channel_id": "101", "reply_to": good, "reactions": 7},
        "just a string",
        {"channel_id": "101", "reply_to": good, "text": 5},
        {"item": empty, "channel_id": "101", "reply_to": empty,
         "content": "I put my reply in the wrong field"},
        {"channel_id": "101", "text": "no reply_to but reactions",
         "reactions": [1]},
        {"channel_id": ["101"], "text": "x"},
        {"channel_id": "101", "reactions": ["\U0001F44D"]},  # react, no target
        {"item": good, "channel_id": "101", "reply_to": good,
         "text": "the good one"},
    ])
    with capture() as out:
        m.process_outbox(st)
    assert sent_texts() == ["the good one"], sent_texts()
    rej = read(m.OUTBOX_REJECTED_FILE)
    assert len(rej) == 7, [r["reason"] for r in rej]
    reasons = [r["reason"] for r in rej]
    assert any("nothing to send" in r for r in reasons), reasons
    assert read(m.OUTBOX_FILE) == []
    # the empty item was NOT settled: its mention stays open
    assert [q["message_id"] for q in read(m.QUEUE_FILE)] == [empty]
    assert empty not in st["replied_to"]
    assert m.ledger_record and \
        m._ledger_load()["entries"].get(empty, {}).get("status") != "skipped"
    assert out.getvalue().count("Rejected outbox item") == 7


def test_corrupt_outbox_file():
    fresh_pkg()
    st = new_state()
    with open(m.OUTBOX_FILE, "w") as f:
        f.write('[{"channel_id": "101", "te')  # mid-write
    with capture():
        m.process_outbox(st)
    with open(m.OUTBOX_FILE) as f:
        assert f.read().startswith('[{"channel_id"'), \
            "young unparseable file must be left alone"
    old = time.time() - 60
    os.utime(m.OUTBOX_FILE, (old, old))
    with capture() as out:
        m.process_outbox(st)
    assert read(m.OUTBOX_FILE) == []
    assert "not valid JSON" in read(m.OUTBOX_REJECTED_FILE)[0]["reason"]
    assert "Rejected reply_outbox.json" in out.getvalue()


def test_retry_merges_with_appends():
    pkg = fresh_pkg()
    st = new_state()
    a, b = snowflake(), snowflake()
    write(m.QUEUE_FILE, [qitem(a), qitem(b)])
    write(m.OUTBOX_FILE, [{"item": a, "channel_id": "101", "reply_to": a,
                           "text": "flaky"}])
    orig = m.api_post

    def failing(path, data):
        if path.endswith("/messages"):
            append_via_helper(pkg, [{"item": b, "channel_id": "101",
                                     "reply_to": b, "text": "b"}])
            raise OSError("timeout")
        return orig(path, data)
    m.api_post = failing
    try:
        with capture():
            m.process_outbox(st)
    finally:
        m.api_post = orig
    out = read(m.OUTBOX_FILE)
    assert [i["item"] for i in out] == [a, b], out
    assert out[0]["_retry"]["n"] == 1 and "picked" in out[0]["_retry"]
    assert len(read(m.QUEUE_FILE)) == 2  # nothing settled yet
    # crash leftover: processing file is merged back first
    write(m.OUTBOX_PROCESSING_FILE, [{"item": "77", "channel_id": "101",
                                      "text": "leftover"}])
    out[0]["_retry"]["next"] = 0
    write(m.OUTBOX_FILE, out)
    with capture():
        m.process_outbox(st)
    assert sent_texts() == ["leftover", "flaky", "b"], sent_texts()
    assert read(m.QUEUE_FILE) == []


def test_typing_thread_lifecycle():
    fresh_pkg()
    st = new_state()
    m.TYPING_EVERY_SECS = 2
    m.TYPING_MAX_SECS = 6
    t = threading.Thread(target=m._typing_thread_main, daemon=True)
    t.start()
    try:
        # 1. pending burst types; a burst in enqueue back-off does not
        m.stage_trigger(st, msg(cid="101", author="U1"), "S #a", "G",
                        "mention")
        m.stage_trigger(st, msg(cid="103", author="U3"), "S #c", "G",
                        "mention")
        st["pending"]["103:U3"]["next_try"] = time.time() + 300
        write(m.QUEUE_FILE, [])
        with capture():
            m.typing_refresh(st)
        time.sleep(1.5)
        chans = {c[2].split("/")[2] for c in typing_calls()}
        assert chans == {"101"}, chans
        # 2. the main loop goes quiet (slow work) after the burst is
        # queued: typing keeps going on its own clock
        del st["pending"]
        a = snowflake()
        write(m.QUEUE_FILE, [qitem(a, cid="101")])
        m.typing_refresh(st)
        n0 = len(typing_calls())
        time.sleep(4.5)
        c1 = [c for c in typing_calls()[n0:] if "/101/" in c[2]]
        assert len(c1) >= 2, typing_calls()
        # 3. the reply goes out: typing stops for good
        write(m.OUTBOX_FILE, [{"item": a, "channel_id": "101",
                               "reply_to": a, "text": "done"}])
        with capture():
            m.process_outbox(st)
        m.typing_refresh(st)
        sent_at = time.time()
        time.sleep(3)
        after = [c for c in typing_calls() if c[0] > sent_at]
        assert not after, after
        # 4. an item nobody answers stops after TYPING_MAX_SECS
        b = snowflake()
        it = qitem(b, cid="105")
        # Queued 2 s ago: 4 s of typing left (TYPING_MAX_SECS = 6), so
        # a slow runner still sees at least one refresh (every 2 s).
        it["queued_at"] = time.time() - 2
        write(m.QUEUE_FILE, [it])
        m.typing_refresh(st)
        time.sleep(2.5)
        assert [c for c in typing_calls() if "/105/" in c[2]]
        cut = it["queued_at"] + m.TYPING_MAX_SECS + 0.5
        time.sleep(max(0.0, cut - time.time()))
        time.sleep(2.5)
        assert not [c for c in typing_calls()
                    if "/105/" in c[2] and c[0] > cut]
        # 5. REST back-off: no typing at all
        it["queued_at"] = time.time()
        write(m.QUEUE_FILE, [it])
        m.typing_refresh(st)
        m._rl_until = time.time() + 100
        cut = time.time()
        time.sleep(2.5)
        m.send_typing("109")
        assert not [c for c in typing_calls() if c[0] > cut]
    finally:
        m._rl_until = 0.0
        m.TYPING_EVERY_SECS = 8
        m.TYPING_MAX_SECS = 180
        m._typing_sources.clear()


def _drain(st, inbox_items):
    import queue as pyqueue
    q = pyqueue.Queue()
    for it in inbox_items:
        q.put(it)
    chans = {"101": {"name": "c", "guild_id": "G", "guild_name": "S"}}
    with capture():
        mentions, joins, dms = m.drain_gateway(st, q, chans)
        for mm, cname, gid, kind in mentions:
            m.stage_trigger(st, mm, cname, gid, kind)
    return mentions


def test_early_flush_on_typing_stop():
    fresh_pkg()
    st = new_state()
    write(m.QUEUE_FILE, [])
    t = msg(author="U1")
    _drain(st, [{"_typing": {"channel_id": "101", "user_id": "U1"}}, t])
    q = []
    with capture():
        assert not m.flush_pending(st, q)  # too soon (< 1.5s)
    m._activity["101:U1"]["msg_at"] -= 1.6
    with capture() as out:
        assert m.flush_pending(st, q)
    assert "(author stopped typing)" in out.getvalue()
    assert q[0]["message_id"] == t["id"]


def test_typing_after_message_holds_then_max_wait():
    fresh_pkg()
    st = new_state()
    t = msg(author="U1")
    _drain(st, [t, {"_typing": {"channel_id": "101", "user_id": "U1"}}])
    m._activity["101:U1"]["msg_at"] -= 3
    q = []
    with capture():
        assert not m.flush_pending(st, q)  # still typing
    # a follow-up fragment arrives, then they stop typing
    f = msg(author="U1", content="and also", mention=False)
    _drain(st, [f])
    entry = st["pending"]["101:U1"]
    assert [x["id"] for x in entry["fragments"]] == [f["id"]]
    with capture():
        assert not m.flush_pending(st, q)
    m._activity["101:U1"]["msg_at"] -= 1.6
    with capture():
        assert m.flush_pending(st, q)
    assert q[0]["burst_fragments"][0]["message_id"] == f["id"]
    # typing never stops: the window is the maximum wait
    t2 = msg(author="U2")
    _drain(st, [t2, {"_typing": {"channel_id": "101", "user_id": "U2"}}])
    m._activity["101:U2"]["msg_at"] -= 4.9
    st["pending"]["101:U2"]["first_seen"] -= 4.9
    with capture():
        assert not m.flush_pending(st, q)
    st["pending"]["101:U2"]["first_seen"] -= 0.2
    with capture() as out:
        assert m.flush_pending(st, q)
    assert "stopped typing" not in out.getvalue()


def test_no_gateway_waits_full_window():
    """A burst ingested by the REST poll only (gateway down) never
    flushes early."""
    fresh_pkg()
    st = new_state()
    t = msg(author="U1")
    with capture():
        kind = m.classify_message(st, t, "S #c", "G")
        m.stage_trigger(st, t, "S #c", "G", kind)
    entry = st["pending"]["101:U1"]
    entry["first_seen"] -= 4.0
    q = []
    with capture():
        assert not m.flush_pending(st, q)
    entry["first_seen"] -= 1.1
    with capture():
        assert m.flush_pending(st, q)


def test_bot_author_longer_quiet():
    fresh_pkg()
    st = new_state()
    t = msg(author="B1", bot=True)
    _drain(st, [t])
    assert st["pending"]["101:B1"]["window"] == m.BOT_COALESCE_SECS
    m._activity["101:B1"]["msg_at"] -= 2
    q = []
    with capture():
        assert not m.flush_pending(st, q)
    m._activity["101:B1"]["msg_at"] -= 3.1
    with capture():
        assert m.flush_pending(st, q)


def test_latency_line_format():
    fresh_pkg()
    st = new_state()
    trig_t = time.time() - 30
    a = snowflake(trig_t)
    queued = trig_t + 4.8
    st["queued_at"] = {a: queued}
    write(m.QUEUE_FILE, [qitem(a, queued_at=queued)])
    write(m.OUTBOX_FILE, [{"item": a, "channel_id": "101", "reply_to": a,
                           "text": "hi"}])
    with capture() as out:
        m.process_outbox(st)
    line = [ln for ln in out.getvalue().splitlines()
            if ln.startswith("Latency")][0]
    # The line format changed to ingest/hold/worker (no ingested_at
    # here, so ingest runs to the queueing and hold is 0).
    assert line.startswith(f"Latency {a}: ingest 4.8s, hold 0.0s, "
                           f"worker 2"), line
    assert "send 0.0s" in line and "total 30.0s" in line, line
    assert a not in st["queued_at"]


def test_helper_rejects_bad_batch():
    pkg = fresh_pkg()
    r = append_via_helper(pkg, [
        {"item": "1", "channel_id": "101".replace("C", "") or "1",
         "reply_to": "1", "text": "fine"},
        {"item": "2", "channel_id": "5", "reply_to": "2",
         "content": "oops"}], check=False)
    assert r.returncode == 1 and '"text", not "content"' in r.stderr, \
        r.stderr
    assert read(os.path.join(pkg, "reply_outbox.json")) is None
    r = append_via_helper(pkg, {"item": "1", "channel_id": "5",
                                "reply_to": "1", "silent": True})
    assert "appended 1 item" in r.stdout
    assert len(read(os.path.join(pkg, "reply_outbox.json"))) == 1


# ---------------------------------------------------------------------------
class Locking(unittest.TestCase):
    """The module-level test_* functions above, in file order (they
    share the one imported monitor and reset it through fresh_pkg)."""


def _method(fn):
    def run(self):
        fn()
    run.__doc__ = fn.__doc__
    return run


for _i, (_name, _fn) in enumerate(
        [(n, f) for n, f in list(globals().items())
         if n.startswith("test_") and callable(f)]):
    setattr(Locking, f"test_{_i:02d}_{_name[5:]}", _method(_fn))


if __name__ == "__main__":
    unittest.main(argv=ARGV)
