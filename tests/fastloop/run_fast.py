"""Run monitor.main() offline for one fast-loop scenario (in its own
process, under an alarm) and print a JSON result on the last line.
Usage: run_fast.py <scenario> <seconds>   (MUT_DIR = the code under test)"""
import contextlib
import http.server
import io
import json
import os
import signal
import socketserver
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "_lib"))
for k in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY",
          "all_proxy", "ALL_PROXY"):
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = os.environ["no_proxy"] = "*"

from common import signed  # noqa: E402
import monitor_harness as H  # noqa: E402
from monitor_harness import G1, OWNER, BOT, msg, chan  # noqa: E402

scenario = sys.argv[1]
seconds = float(sys.argv[2])


class Stop(BaseException):
    pass


mod = H.load_monitor()
T0 = time.time()
rec = {"drains": [], "staged": [], "enqueued": [], "put": [], "gets": [],
       "posts": [], "outbox_written": None}


def rel(t):
    return round(t - T0, 3)


# --- instrumentation -------------------------------------------------------
_orig_drain = mod.drain_gateway


def drain(*a, **kw):
    rec["drains"].append(rel(time.time()))
    return _orig_drain(*a, **kw)


mod.drain_gateway = drain
_orig_load_state = mod.load_state
LIVE = {}   # the main loop's state, for signing outbox items


def load_state_live():
    LIVE["state"] = _orig_load_state()
    return LIVE["state"]


mod.load_state = load_state_live
_orig_stage = mod.stage_trigger


def stage(state, m, *a, **kw):
    rec["staged"].append([m["id"], rel(time.time())])
    return _orig_stage(state, m, *a, **kw)


mod.stage_trigger = stage
_orig_enq = mod.enqueue_message


def enq(m, *a, **kw):
    ok = _orig_enq(m, *a, **kw)
    rec["enqueued"].append([m["id"], rel(time.time()), ok])
    return ok


mod.enqueue_message = enq
SLOW = {"secs": 0.0, "prefix": "/channels/"}
_orig_get = mod.api._get


def slow_get(path):
    if SLOW["secs"] and path.startswith(SLOW["prefix"]):
        time.sleep(SLOW["secs"])
    rec["gets"].append([path, rel(time.time())])
    return _orig_get(path)


mod.api._get = slow_get
_orig_post = mod.api.post


def post(path, data):
    rec["posts"].append([path, rel(time.time())])
    return _orig_post(path, data)


mod.api.post = post
mod.api_post = post
mod.api.routes["/users/@me"] = {"id": BOT, "username": "musebot"}

gw_script = []  # [(at_secs, item or callable)]


PROBE = {"on": False}


def probe(inbox, status):
    """Wake the loop every 0.2s (as a gateway event would): the gaps
    between drains are then how long the loop was blocked."""
    while True:
        time.sleep(0.2)
        inbox.put(typing("PROBE", "nobody"))
        status["wake"].set()


def fake_gw(token, inbox, status):
    if PROBE["on"]:
        threading.Thread(target=probe, args=(inbox, status),
                         daemon=True).start()
    for at, item in gw_script:
        time.sleep(max(0.0, at - (time.time() - T0)))
        if callable(item):
            item(inbox, status)
            continue
        inbox.put(item)
        if "id" in item:
            rec["put"].append([item["id"], rel(time.time())])
            status["wake"].set()


mod._gateway_thread_main = fake_gw
GUILDS = {G1: {"name": "Srv", "system_channel_id": None, "joined_at": None}}


def setup(chans, last_seen=None):
    mod.discover_channels = lambda previous=None: (dict(chans), GUILDS)
    st = {"last_seen": last_seen or {c: "1" for c in chans},
          "guild_joined": {G1: 0}, "known_guilds": {G1: "Srv"}}
    st["last_seen"].setdefault("DM1", "1")
    with open(mod.STATE_FILE, "w") as f:
        json.dump(st, f)


def dm(content="hey"):
    m = msg("DM1", OWNER, content, gid=None, age=0)
    return m


def mention(cid, author="U7"):
    return msg(cid, author, "@bot hi", mention=True, age=0)


def typing(cid, uid):
    return {"_typing": {"channel_id": cid, "user_id": uid}}


def lazy(fn):
    """A gateway item built at delivery time (fresh snowflake)."""
    def put(inbox, status):
        item = fn()
        inbox.put(item)
        if "id" in item:
            rec["put"].append([item["id"], rel(time.time())])
            status["wake"].set()
    return put


if scenario == "slow":
    # 15 channels, every channel read sleeps 0.25s: a rotation takes ~4s.
    chans = {f"C{i:02d}": chan(f"c{i}") for i in range(15)}
    setup(chans)
    SLOW["secs"] = 0.25
    mod.POLL_INTERVAL = 5
    PROBE["on"] = True
    gw_script += [(2, lazy(dm)), (4, lazy(lambda: mention("C05"))),
                  (7, lazy(dm)), (10, lazy(lambda: mention("C14")))]
elif scenario == "sweep":
    # The sweep pass runs every cycle alongside the poll, both sliced;
    # a mention the poll never saw (below its watermark) is recovered.
    chans = {f"C{i:02d}": chan(f"c{i}") for i in range(20)}
    lost = msg("C19", "U9", "@bot are you there", mention=True, age=300)
    mod.api.msgs["C19"] = [lost]
    setup(chans, {**{c: "1" for c in chans}, "C19": H.sf(0)})
    rec["lost"] = lost["id"]
    SLOW["secs"] = 0.1
    mod.POLL_INTERVAL = 4
    PROBE["on"] = True
    mod.SWEEP_EVERY_CYCLES = 1
    gw_script += [(4, lazy(lambda: mention("C07"))),
                  (6, lazy(lambda: mention("C08"))),
                  (8, lazy(lambda: mention("C09")))]
elif scenario == "outbox":
    setup({"500": chan()})
    rec["outbox_written"] = []

    def write_outbox(inbox, status):
        # A message the monitor queued there (an outbox item answers one).
        mid = H.sf(0)
        mod.queue_edit(add=[{"message_id": mid, "channel_id": "500"}])
        tmp = mod.OUTBOX_FILE + ".w"
        with open(tmp, "w") as f:
            json.dump([signed(mod, LIVE["state"], {
                "item": mid, "channel_id": "500", "reply_to": mid,
                "text": "hello"})], f)
        os.replace(tmp, mod.OUTBOX_FILE)
        rec["outbox_written"].append(rel(time.time()))
    gw_script += [(3.1, write_outbox), (5.37, write_outbox)]
elif scenario == "hung":
    # The real api_get against a local server where one channel's read
    # never answers: the loop may stall for READ_TIMEOUT_SECS at most.
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            rec["gets"].append([self.path, rel(time.time())])
            if self.path.startswith("/channels/CH/"):
                time.sleep(60)
                return
            if self.path == "/users/@me":
                return self._send({"id": BOT, "username": "musebot"})
            return self._send([])

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(n)
            if self.path == "/users/@me/channels":
                return self._send({"id": "DM1",
                                   "recipients": [{"username": "owner"}]})
            return self._send({"id": H.sf(0)})

    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
    srv = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    mod.API = f"http://127.0.0.1:{srv.server_address[1]}"
    # The module's own api_get/api_post (load_monitor stubs them), rebound
    # to its globals, so they use its API base, _api_call, and timeout.
    import importlib.util
    import types
    spec = importlib.util.spec_from_file_location(
        "real_monitor", os.path.join(H.MUT_DIR, "monitor.py"))
    real = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(real)
    mod.api_get = types.FunctionType(real.api_get.__code__, mod.__dict__)
    mod.api_post = types.FunctionType(real.api_post.__code__, mod.__dict__)
    mod.READ_TIMEOUT_SECS = 2   # 8 in production; shorter for speed
    setup({"C0": chan(), "C1": chan(), "CH": chan("hung")})
    gw_script += [(1, lazy(lambda: mention("C0")))]
else:
    sys.exit(f"unknown scenario {scenario}")


def on_alarm(*_):
    raise Stop()


signal.signal(signal.SIGALRM, on_alarm)
signal.setitimer(signal.ITIMER_REAL, seconds)
out = io.StringIO()
result = {}
with contextlib.redirect_stdout(out):
    try:
        mod.main()
        result["exit"] = "returned"
    except SystemExit as e:
        result["exit"] = e.code
    except Stop:
        result["exit"] = "stopped"
signal.setitimer(signal.ITIMER_REAL, 0)
result["out"] = out.getvalue()
result.update(rec)
print(json.dumps(result))
