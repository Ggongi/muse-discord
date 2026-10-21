"""bin/discord-api.py is for owner commands only: every subcommand but
resolve-channel and list-threads needs --for <an open owner-command
item> --nonce <its owner_command_nonce> (checked in constant time
against owner_items.json), anywhere. In an owner's DM it must also be
that DM's item and nonce (owner_dm_nonces.json; that message must be in
that channel). So a worker woken by someone else's message cannot post,
edit, delete, react, pin or read anywhere, even knowing the owner's
message id. The API is stubbed.

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
SRV_ITEM = "300000000000000007"   # an owner's command in a server
SRV_NONCE = "e" * 32
STRANGER_ITEM = "300000000000000008"

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
D.OWNER_ITEMS_FILE = os.path.join(WORK, "owner_items.json")
SERVER = ("--for", SRV_ITEM, "--nonce", SRV_NONCE)


def write_owner_items(data):
    with open(D.OWNER_ITEMS_FILE, "w") as f:
        json.dump(data, f)


def writes():
    """The calls that change something, or read messages."""
    return [c for c in CALLS if c[0] != "GET" or "/messages?" in c[1]]


class _Tool(unittest.TestCase):
    def setUp(self):
        with open(D.OWNER_DM_NONCES_FILE, "w") as f:
            json.dump({ITEM: NONCE}, f)
        write_owner_items({ITEM: NONCE, SRV_ITEM: SRV_NONCE})

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


class OwnerDmChannel(_Tool):
    def test_refused_without_the_nonce(self):
        for args in self.every_subcommand(OWNER_DM) + self.every_subcommand(
                OWNER_DM, ("--for", ITEM)):
            rc = self.run_tool(*args)
            self.assertNotEqual(rc, 0, args)
            self.assertEqual(writes(), [], args)

    def test_refused_with_a_wrong_or_stale_nonce(self):
        for prefix in (("--for", ITEM, "--nonce", "0" * 32),
                       ("--for", ITEM, "--nonce", NONCE + "0"),
                       ("--for", ITEM, "--nonce", "é" * 32),
                       ("--for", "300000000000000002", "--nonce", NONCE),
                       ("--nonce", NONCE, "--for", "1/../../x")):
            rc = self.run_tool(*prefix, "post", OWNER_DM, "hi")
            self.assertNotEqual(rc, 0, prefix)
            self.assertEqual(writes(), [], prefix)
        # Spent (the monitor dropped it from the file once answered).
        with open(D.OWNER_DM_NONCES_FILE, "w") as f:
            json.dump({}, f)
        rc = self.run_tool("--for", ITEM, "--nonce", NONCE, "post",
                           OWNER_DM, "hi")
        self.assertNotEqual(rc, 0)
        self.assertEqual(writes(), [])
        os.remove(D.OWNER_DM_NONCES_FILE)
        rc = self.run_tool("--for", ITEM, "--nonce", NONCE, "post",
                           OWNER_DM, "hi")
        self.assertEqual(writes(), [])

    def test_the_item_must_be_in_that_channel(self):
        with open(D.OWNER_DM_NONCES_FILE, "w") as f:
            json.dump({ITEM: NONCE, "300000000000000003": "d" * 32}, f)
        write_owner_items({ITEM: NONCE, "300000000000000003": "d" * 32})
        rc = self.run_tool("--for", "300000000000000003", "--nonce",
                           "d" * 32, "post", OWNER_DM, "hi")
        self.assertNotEqual(rc, 0)
        self.assertEqual(writes(), [])

    def test_allowed_with_the_live_nonce_and_elsewhere(self):
        for args in self.every_subcommand(OWNER_DM, (
                "--for", ITEM, "--nonce", NONCE)):
            rc = self.run_tool(*args)
            self.assertEqual(rc, 0, args)
            self.assertTrue(writes(), args)
        # Server channels and other people's DMs: an owner's command
        # from a server (or the owner DM's own item) may act there.
        for cid in (GEN_CH, OTHER_DM):
            for prefix in (SERVER, ("--for", ITEM, "--nonce", NONCE)):
                for args in self.every_subcommand(cid, prefix):
                    rc = self.run_tool(*args)
                    self.assertEqual(rc, 0, args)
                    self.assertTrue(writes(), args)
        # A server command's nonce opens no owner DM.
        for args in self.every_subcommand(OWNER_DM, SERVER):
            self.assertNotEqual(self.run_tool(*args), 0, args)
            self.assertEqual(writes(), [], args)

    def test_unknown_dm_fails_closed(self):
        CHANNELS["200000000000000005"] = {"id": "200000000000000005",
                                          "type": 1}
        rc = self.run_tool(*SERVER, "post", "200000000000000005", "hi")
        self.assertNotEqual(rc, 0)
        self.assertEqual(writes(), [])
        # Unreadable owners: every DM is treated as an owner's.
        real = D._owners
        D._owners = lambda: None
        try:
            rc = self.run_tool(*SERVER, "post", OTHER_DM, "hi")
            self.assertNotEqual(rc, 0)
            self.assertEqual(writes(), [])
            self.assertEqual(self.run_tool(*SERVER, "post", GEN_CH, "hi"),
                             0)
        finally:
            D._owners = real
        self.assertEqual(D._owners(), {OWNER})

    def test_non_canonical_ids_refused(self):
        for cid in ("0" + GEN_CH, GEN_CH.replace("2", "\u0662")):
            rc = self.run_tool(*SERVER, "post", cid, "hi")
            self.assertNotEqual(rc, 0)
            self.assertEqual(CALLS, [])


class OwnerCommandsOnly(_Tool):
    """Anywhere, not just an owner's DM: no valid owner --for, no act."""

    def test_refused_without_a_valid_owner_item(self):
        bad = [(), ("--for", SRV_ITEM), ("--nonce", SRV_NONCE),
               ("--for", SRV_ITEM, "--nonce", "0" * 32),
               ("--for", SRV_ITEM, "--nonce", NONCE),     # another's
               # A stranger's open item, or one the monitor never
               # recorded as an owner's command.
               ("--for", STRANGER_ITEM, "--nonce", SRV_NONCE),
               ("--for", "1/../x", "--nonce", SRV_NONCE)]
        for cid in (GEN_CH, OTHER_DM):
            for prefix in bad:
                for args in self.every_subcommand(cid, prefix):
                    self.assertNotEqual(self.run_tool(*args), 0, args)
                    self.assertEqual(CALLS, [], args)
        # Answered (the monitor dropped it), or no file at all.
        write_owner_items({ITEM: NONCE})
        self.assertNotEqual(self.run_tool(*SERVER, "post", GEN_CH, "x"), 0)
        os.remove(D.OWNER_ITEMS_FILE)
        self.assertNotEqual(self.run_tool(*SERVER, "read", GEN_CH), 0)
        self.assertEqual(CALLS, [])

    def test_lookups_need_no_owner_item(self):
        CHANNELS_LIST = [{"id": GEN_CH, "name": "general", "type": 0}]
        real = D.api

        def api(method, path, data=None):
            CALLS.append((method, path, data))
            if path.endswith("/channels"):
                return CHANNELS_LIST
            return {"threads": [{"id": "1", "parent_id": GEN_CH}]}
        D.api = api
        try:
            self.assertEqual(self.run_tool("resolve-channel",
                                           "500000000000000001",
                                           "#general"), 0)
            self.assertEqual(self.run_tool("list-threads",
                                           "500000000000000001", GEN_CH), 0)
        finally:
            D.api = real


if __name__ == "__main__":
    unittest.main()
