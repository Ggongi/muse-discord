"""bin/discord-api.py in an owner's DM: every subcommand that takes a
channel refuses an owner's DM channel unless the call carries --for
<the DM's message_id> --nonce <its owner_dm_nonce> (a live one, checked
in constant time against owner_dm_nonces.json, and that message must be
in that channel). So a worker woken by someone else's message cannot
post, edit, delete, react, pin or read there. Server channels and other
people's DMs work as before. The API is stubbed.

Usage: [MUT_DIR=<tree>] python3 tests/ownerdm/test_discord_api.py"""
import importlib.util
import io
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "_lib"))
from common import MUT_DIR, scratch  # noqa: E402

OWNER = "100000000000000001"
STRANGER = "100000000000000002"
BOTID = "100000000000000099"
OWNER_DM = "200000000000000001"
OTHER_DM = "200000000000000004"
GEN_CH = "200000000000000002"
ITEM = "300000000000000001"
NONCE = "c" * 32

WORK = scratch("muse-odm-api-")
CFG = os.path.join(WORK, "config.yaml")
with open(CFG, "w") as f:
    f.write(f'version: 1\nowners: ["{OWNER}"]\n'
            f'token_from: env:MUSE_TEST_TOKEN\n')
os.environ["MUSE_DISCORD_CONFIG"] = CFG
os.environ["MUSE_TEST_TOKEN"] = "dummy-token"
spec = importlib.util.spec_from_file_location(
    "discord_api_odm", os.path.join(MUT_DIR, "bin", "discord-api.py"))
D = importlib.util.module_from_spec(spec)
spec.loader.exec_module(D)

CHANNELS = {
    OWNER_DM: {"id": OWNER_DM, "type": 1, "recipients": [{"id": OWNER}]},
    OTHER_DM: {"id": OTHER_DM, "type": 1,
               "recipients": [{"id": STRANGER}]},
    GEN_CH: {"id": GEN_CH, "type": 0, "guild_id": "5"},
}
MESSAGES = {(OWNER_DM, ITEM)}   # (channel, message) that exist
CALLS = []


class NotFound(SystemExit):
    pass


def fake_api(method, path, data=None):
    CALLS.append((method, path, data))
    if path == "/users/@me":
        return {"id": BOTID}
    m = re.fullmatch(r"/channels/(\d+)", path)
    if method == "GET" and m:
        return CHANNELS[m[1]]
    m = re.fullmatch(r"/channels/(\d+)/messages/(\d+)", path)
    if method == "GET" and m:
        if (m[1], m[2]) in MESSAGES or m[2] == "300000000000000009":
            return {"id": m[2], "author": {"id": BOTID}}
        raise NotFound(f"Discord API 404 on GET {path}")
    if method == "GET" and "/messages?" in path:
        return [{"id": ITEM, "author": {"username": "owner"},
                 "content": "secret plans"}]
    return {"id": "500000000000000001", "name": "t"}


D.api = fake_api
D.OWNER_DM_NONCES_FILE = os.path.join(WORK, "owner_dm_nonces.json")


def writes():
    """The calls that change something, or read messages."""
    return [c for c in CALLS if c[0] != "GET" or "/messages?" in c[1]]


class OwnerDmChannel(unittest.TestCase):
    def setUp(self):
        with open(D.OWNER_DM_NONCES_FILE, "w") as f:
            json.dump({ITEM: NONCE}, f)

    def run_tool(self, *args):
        CALLS.clear()
        err, out = io.StringIO(), io.StringIO()
        old = sys.stderr, sys.stdout
        sys.stderr, sys.stdout = err, out
        try:
            rc = D.main(["discord-api.py", *args])
        except SystemExit as e:
            rc = e.code if isinstance(e.code, int) else str(e.code)
        finally:
            sys.stderr, sys.stdout = old
        self.assertNotIn(NONCE, out.getvalue() + err.getvalue()
                         + str(rc))
        return rc

    def every_subcommand(self, cid, prefix=()):
        msg = "300000000000000009"
        return [
            [*prefix, "post", cid, "hello"],
            [*prefix, "read", cid, "5"],
            [*prefix, "edit", cid, msg, "changed"],
            [*prefix, "delete-own", cid, msg],
            [*prefix, "react", cid, msg, "\U0001F44D"],
            [*prefix, "pin", cid, msg],
            [*prefix, "unpin", cid, msg],
            [*prefix, "thread", cid, "title", "text"],
        ]

    def test_refused_without_the_nonce(self):
        for args in self.every_subcommand(OWNER_DM) + self.every_subcommand(
                OWNER_DM, ("--for", ITEM)):
            rc = self.run_tool(*args)
            self.assertIn("is an owner's DM", str(rc), args)
            self.assertEqual(writes(), [], args)

    def test_refused_with_a_wrong_or_stale_nonce(self):
        for prefix in (("--for", ITEM, "--nonce", "0" * 32),
                       ("--for", ITEM, "--nonce", NONCE + "0"),
                       ("--for", ITEM, "--nonce", "é" * 32),
                       ("--for", "300000000000000002", "--nonce", NONCE),
                       ("--nonce", NONCE, "--for", "1/../../x")):
            rc = self.run_tool(*prefix, "post", OWNER_DM, "hi")
            self.assertIn("Refusing", str(rc), prefix)
            self.assertEqual(writes(), [], prefix)
        # Spent (the monitor dropped it from the file once answered).
        with open(D.OWNER_DM_NONCES_FILE, "w") as f:
            json.dump({}, f)
        rc = self.run_tool("--for", ITEM, "--nonce", NONCE, "post",
                           OWNER_DM, "hi")
        self.assertIn("not the live owner_dm_nonce", str(rc))
        self.assertEqual(writes(), [])
        os.remove(D.OWNER_DM_NONCES_FILE)
        rc = self.run_tool("--for", ITEM, "--nonce", NONCE, "post",
                           OWNER_DM, "hi")
        self.assertEqual(writes(), [])

    def test_the_item_must_be_in_that_channel(self):
        with open(D.OWNER_DM_NONCES_FILE, "w") as f:
            json.dump({ITEM: NONCE, "300000000000000003": "d" * 32}, f)
        rc = self.run_tool("--for", "300000000000000003", "--nonce",
                           "d" * 32, "post", OWNER_DM, "hi")
        self.assertIn("is not a message in", str(rc))
        self.assertEqual(writes(), [])

    def test_allowed_with_the_live_nonce(self):
        for args in self.every_subcommand(OWNER_DM, (
                "--for", ITEM, "--nonce", NONCE)):
            rc = self.run_tool(*args)
            self.assertEqual(rc, 0, args)
            self.assertTrue(writes(), args)

    def test_server_channels_and_other_dms_as_before(self):
        for cid in (GEN_CH, OTHER_DM):
            for args in self.every_subcommand(cid) + self.every_subcommand(
                    cid, ("--for", "123")):
                rc = self.run_tool(*args)
                self.assertEqual(rc, 0, args)
                self.assertTrue(writes(), args)

    def test_dm_without_recipients_is_guarded(self):
        CHANNELS["200000000000000005"] = {"id": "200000000000000005",
                                          "type": 1}
        rc = self.run_tool("post", "200000000000000005", "hi")
        self.assertIn("is an owner's DM", str(rc))
        self.assertEqual(writes(), [])

    def test_non_canonical_ids_refused(self):
        for cid in ("0" + GEN_CH, GEN_CH.replace("2", "\u0662")):
            rc = self.run_tool("post", cid, "hi")
            self.assertIn("not a Discord id", str(rc))
            self.assertEqual(CALLS, [])

    def test_owners_unreadable_fails_closed(self):
        real = D._owners
        D._owners = lambda: None
        try:
            rc = self.run_tool("post", OTHER_DM, "hi")
            self.assertIn("is an owner's DM", str(rc))
            self.assertEqual(writes(), [])
            self.assertEqual(self.run_tool("post", GEN_CH, "hi"), 0)
        finally:
            D._owners = real
        self.assertEqual(D._owners(), {OWNER})


if __name__ == "__main__":
    unittest.main()
