#!/usr/bin/env python3
"""muse-discord presence client.

Keeps a Discord gateway (websocket) session open so the bot shows as
ONLINE instead of offline. REST API calls alone don't create presence;
only an active gateway session does.

- Identifies with intents=0 (no message events; detection happens in
  monitor.py, not here).
- Shows ONE static status from config (presence.status_text /
  presence.status_kind). There is deliberately no rotation, scheduling,
  or profile/banner management: the status is set once at identify and
  left alone.
- Sends heartbeats on Discord's interval; auto-reconnects with backoff
  and resumes the session when Discord allows it.
- If presence.enabled is false in config, exits immediately (the
  watchdog checks the same flag and won't try to restart it).

Token resolution matches monitor.py: argv[1], else the configured
bot.token_env env var, else the configured bot.token_file. The token is
never printed or written anywhere.

Usage: python3 presence.py [bot-token]
"""
import asyncio
import json
import os
import sys

import websockets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"
ACTIVITY_TYPES = {"playing": 0, "streaming": 1, "listening": 2,
                  "watching": 3, "custom": 4, "competing": 5}


def load_config():
    try:
        import yaml
    except ImportError:
        print("PyYAML is required: pip install -r requirements.txt",
              file=sys.stderr)
        sys.exit(1)
    path = os.environ.get("MUSE_DISCORD_CONFIG") or os.path.join(
        BASE_DIR, "config.yaml")
    if len(sys.argv) > 2 and sys.argv[1] == "--config":
        path = sys.argv[2]
    cfg = {}
    if os.path.exists(path):
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
    return cfg


CFG = load_config()
BOT_CFG = CFG.get("bot") or {}
PRESENCE_CFG = CFG.get("presence") or {}


def _load_token():
    if len(sys.argv) > 1 and sys.argv[1] != "--config":
        return sys.argv[1]
    env_name = BOT_CFG.get("token_env")
    if env_name and os.environ.get(env_name):
        return os.environ[env_name].strip()
    token_file = BOT_CFG.get("token_file") \
        or "~/.config/muse-discord/bot_token"
    try:
        with open(os.path.expanduser(token_file)) as f:
            return f.read().strip()
    except OSError:
        return ""


TOKEN = _load_token()


def build_activity():
    """The single static activity from config, or None for no activity."""
    text = str(PRESENCE_CFG.get("status_text") or "").strip()
    if not text:
        return None
    kind = str(PRESENCE_CFG.get("status_kind") or "watching").lower()
    atype = ACTIVITY_TYPES.get(kind, 3)
    if atype == 4:
        return {"name": "Custom Status", "type": 4, "state": text}
    return {"name": text, "type": atype}


def presence_payload():
    acts = []
    a = build_activity()
    if a:
        acts.append(a)
    return {"status": "online", "activities": acts, "afk": False,
            "since": None}


async def run():
    backoff = 5
    session_id = None
    seq = None
    while True:
        hb_task = None
        try:
            async with websockets.connect(
                    GATEWAY, max_size=10 * 1024 * 1024) as ws:
                hello = json.loads(await ws.recv())
                if hello.get("op") != 10:
                    print(f"Unexpected first payload: {hello}", flush=True)
                    continue
                hb_interval = hello["d"]["heartbeat_interval"] / 1000.0

                if session_id and seq is not None:
                    await ws.send(json.dumps({"op": 6, "d": {
                        "token": TOKEN, "session_id": session_id,
                        "seq": seq}}))
                else:
                    await ws.send(json.dumps({"op": 2, "d": {
                        "token": TOKEN,
                        "intents": 0,
                        "properties": {"os": "linux",
                                       "browser": "muse-discord",
                                       "device": "muse-discord"},
                        "presence": presence_payload(),
                    }}))
                backoff = 5  # reset on successful connect

                async def heartbeat_loop():
                    while True:
                        await asyncio.sleep(hb_interval)
                        await ws.send(json.dumps({"op": 1, "d": seq}))

                hb_task = asyncio.create_task(heartbeat_loop())
                async for raw in ws:
                    msg = json.loads(raw)
                    op = msg.get("op")
                    if op == 0:  # dispatch
                        seq = msg.get("s", seq)
                        if msg.get("t") == "READY":
                            session_id = msg["d"].get("session_id")
                            user = msg["d"]["user"]
                            # The watchdog's health check greps
                            # presence.log for "READY".
                            print(f"READY - {user.get('username')} is "
                                  f"online (session {session_id})",
                                  flush=True)
                        elif msg.get("t") == "RESUMED":
                            print("Session resumed", flush=True)
                    elif op == 1:  # heartbeat requested now
                        await ws.send(json.dumps({"op": 1, "d": seq}))
                    elif op == 7:  # server asks us to reconnect
                        print("Discord requested reconnect", flush=True)
                        break
                    elif op == 9:  # invalid session: re-identify fresh
                        print("Invalid session, will re-identify",
                              flush=True)
                        session_id = None
                        seq = None
                        await asyncio.sleep(5)
                        break
                    # op 11 = heartbeat ACK, nothing to do
        except Exception as e:
            print(f"Gateway error: {e}", flush=True)
        finally:
            if hb_task:
                hb_task.cancel()
        print(f"Reconnecting in {backoff}s", flush=True)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 300)


if __name__ == "__main__":
    if PRESENCE_CFG.get("enabled") is False:
        print("presence.enabled is false in config; not starting.",
              flush=True)
        sys.exit(0)
    if not TOKEN:
        sys.exit("usage: presence.py <bot-token> (or configure "
                 "bot.token_file / bot.token_env)")
    asyncio.run(run())
