# SETUP.md: installing muse-discord

This guide is written to be executed BY a Muse (the owner's agent)
working with the owner. Steps that only the owner can do (clicking in
the Discord developer portal, pasting the token) are marked
**[owner]**. Everything else, the Muse does and verifies.

Conventions: `<package dir>` is where this repo lives. Commands run
from `<package dir>` unless noted. Never print the bot token, never
write it anywhere except the token file, and never commit config.yaml.

## Step 0: Prerequisites

- Python 3.10+ (`python3 --version`).
- Optional but recommended on PATH: `ffmpeg` + `ffprobe` (video frame
  extraction, oversize attachment compression), `pdftotext` from
  poppler (PDF attachment text), `curl` (more reliable downloads).
- Install Python deps: `pip install -r requirements.txt`
  (websockets, PyYAML).

## Step 1: Create the Discord application [owner, guided]

1. Go to https://discord.com/developers/applications and click
   **New Application**. Give it the bot's application name: what
   Discord shows in the member list. What people call the bot in
   chat is a separate choice, made in step 6; the two may match, but
   settle the call name first if the owner wants them to.
2. Open the application, go to **Bot**, and click **Reset Token** (or
   **Copy** if shown). This token is the bot's password. Keep the page
   open; the token is placed in step 3. Do NOT paste it into this
   repo, a chat log, or any file other than the token file.
3. Still under **Bot**, scroll to **Privileged Gateway Intents** and
   enable:
   - **Message Content Intent** (REQUIRED: without it Discord closes
     the gateway with code 4014 and the bot cannot read message text)
   - **Server Members Intent** is NOT needed; leave it off. The
     monitor never requests it: joins (for the welcome plugin) arrive
     as Discord's join system messages in the server's system channel.
4. (Optional) Under **Bot**, set the bot's username/avatar as desired.
   Under **General Information**, note the **Application ID**; it is
   the `client_id` for the invite URL in step 2.

## Step 2: Invite the bot to a server [owner]

Build the invite URL (replace `<APP_ID>`):

```
https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot&permissions=2252074691710016
```

The permission integer grants: View Channel, Send Messages, Send
Messages in Threads, Read Message History, Attach Files, Embed Links,
Add Reactions, Pin Messages (for the owner's `pin`/`unpin` commands;
since February 2026 Discord requires it to pin, and Manage Messages
no longer suffices). No administrator, no moderation permissions.
A bot invited with an older URL lacks Pin Messages: in Server
Settings, Roles, enable **Pin Messages** on the bot's role.

Open the URL, pick a server, authorize. The bot appears in the member
list (offline until step 8). Per-channel overrides still apply: the
connector reads whatever the bot can read and replies wherever it can
send, so adjust channel permissions in Discord to shape its scope.
To keep it out of a channel entirely, deny it View Channel there.

## Step 3: Place the token

```bash
mkdir -p ~/.config/muse-discord
# [owner pastes the token into this file, or the Muse writes it from
#  the owner's paste through a channel that does not echo it into logs]
printf '%s' '<TOKEN>' > ~/.config/muse-discord/bot_token
chmod 600 ~/.config/muse-discord/bot_token
```

Verify WITHOUT printing it: `test -s ~/.config/muse-discord/bot_token`
should succeed. If the install must use a different path or an env
var, set `bot.token_file` / `bot.token_env` in config.yaml (step 4)
instead. An env var must be set wherever the watchdog runs: the
processes it starts inherit its environment.

## Step 4: Fill config.yaml

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml`:

- **owners.user_ids**: the owner's Discord user ID (a number, as a
  quoted string). **[owner]** To find it: in Discord, open Settings,
  Advanced, enable **Developer Mode**; then right-click your own
  avatar/name anywhere and choose **Copy User ID**.
- **bot.guild_ids**: leave `[]` unless the bot should only run in
  specific servers.
- **behavior.dm_policy**: `owner_only` is the recommended default.
- **behavior.respond_to**: leave it at `[mention, reply_to_bot]`.
- **behavior.active_guild_ids**: servers where ambient behavior
  (welcomes) runs. Every other server stays in observe mode, where
  only @mentions, replies to the bot, and DMs get answers. Leave it
  empty unless step 5 turns on an ambient behavior.
  **[owner]** To find a server ID: with Developer Mode on, right-click
  the server icon and choose **Copy Server ID**.
- **safety**: reply caps are off by default. If the owner wants to
  bound token spend, set `daily_reply_cap` (all items queued for the
  worker per day, answered or not) and/or `per_user_daily_cap`; their
  own messages are always exempt.
- Review **engine** tuning; defaults are sane.
- **behavior.bots**: `direct` (default) lets other bots, such as
  other people's Muses, talk to this one by @mention or reply. Set
  `off` to ignore bots entirely.
- **presence**: keeps the bot shown as online. By default there is no
  status line under its name; set `status_text` (and `status_kind`)
  for one, or `enabled: false` to skip the presence process.

Validate the config loads (no Discord connection is made):

```bash
python3 -c "
import monitor
print('owners:', monitor.OWNER_USER_IDS)
print('poll:', monitor.POLL_INTERVAL, 'coalesce:', monitor.COALESCE_SECS)
print('plugins:', monitor.CFG.get('plugins'))
"
```

(Run with a token in argv if the token file isn't placed yet:
`python3 -c "..." ` imports monitor, which requires a resolvable
token; pass a dummy only for this check if you must, and never log it.)

## Step 5: Decide the plugins

Both plugins are off by default. Enable in `config.yaml` under
`plugins:` only what the owner wants:

- **welcome**: greets new members, in active servers only (add each
  server's ID to `behavior.active_guild_ids`). Set
  `plugins.welcome.enabled: true` and review the `channel` param (default: each server's system
  channel; per-server overrides via `per_guild`, keyed by guild ID or
  server name, values are channel IDs, channel names, or `off`) and
  the `prompt` param (the instructions the worker drafts from;
  `{user}` and `{server}` are substituted per join).
- **media_generation**: lets allowed users have the worker generate
  images/video. Requires the worker runtime to actually have media
  tools; if it doesn't, leave this off (the worker must never claim
  generation it cannot start). `allowed_user_ids: []` means owners
  only.

## Step 6: The persona interview (do not skip)

The shipped `persona.md` is a neutral placeholder. Before first run,
interview the owner and rewrite `persona.md` from their answers.

### The call name (first)

The call name is what people type when they talk to the bot ("Pip,
what's the weather?"). It is separate from the Discord application
name in step 1, and it is the name the persona answers to. Ask the
owner for it.

### The persona questions

Ask in the owner's own words (a few questions at a time, not a form):

1. What is the bot, in one line? (Its name is the call name above.)
2. What tone should it have? (e.g. warm and playful, dry and precise,
   professional, chaotic) Any reference points: characters, people,
   other bots?
3. How should it talk differently to the owner vs. to other members?
4. Signature quirks: catchphrases, emoji habits, formatting, things it
   would never say. Any words or topics that are off-limits?
5. Boundaries: what should it never do or discuss in public channels?
   How should it handle questions about the owner?
6. How long should replies typically be? How much humor is too much?

Write the answers into `persona.md` as direct instructions to the
bot (second person, like the placeholder). Keep it under a page. The
worker re-reads it on every wake, so it can be edited live later.

## Step 7: Install the worker

The worker is an agent session woken when the queue is non-empty; its
behavior comes from `worker-prompt.template.md`.

1. Fill the template's seams:
   - **PERSONA seam**: point it at `persona.md` (step 1 of the prompt
     already takes it from the wake payload) or inline the persona.
   - **GENERATION POLICY seam**: state what media tools THIS runtime
     actually has (images? video? neither?) in the "know your limits"
     bullet.
   - **CLASSIFICATION / JUDGMENT seam**: adjust only if the owner wants
     different silence/reply defaults.
2. Register the wake hook in the owner's agent environment:
   - **Muse hook runtime**: register `hooks/mention-watch.sh` as a
     polling hook (every 5 seconds) whose wake prompt is the filled
     template, delivered to a dedicated worker session/chat (not the
     owner's main chat, so routine replies don't spam it). The script
     resolves the package dir from its own location; set
     `MUSE_DISCORD_DIR` if the package lives elsewhere. It needs
     `jq` and `python3` on PATH.
   - **Any other runtime**: schedule a poller that checks
     `reply_queue.json` every few seconds and, when it holds items no
     running worker was handed (re-handing an item out after ~10
     minutes in case its worker died), starts a worker session with
     the filled template as its prompt and those items. PROTOCOL.md
     specifies the file contract the worker implements.
3. Make sure the worker's working directory or paths point at
   `<package dir>` for `reply_queue.json`, `reply_outbox.json`,
   `persona.md`, `generated/`, `bin/outbox-append.py`, and
   `bin/discord-api.py`.

## Step 8: Start it, and keep it alive

Start the supervisor once; it starts the monitor and presence
detached and reports health:

```bash
bash watchdog.sh
# expected: "OK monitor restarted" / "OK presence restarted"
# (or "OK ... alive" if already running)
```

Then schedule `watchdog.sh` to run every ~10 minutes in whatever
scheduler the environment has (cron, a scheduled agent job, systemd
timer). The watchdog is idempotent and safe to run any time; it
alerts only on NEW failures (its output contract is documented at the
top of the script). Once a day it also checks for a new muse-discord
version and prints one `UPDATE:` line when there is one; tell the
owner, and update by following UPDATING.md when they say so. To stop everything: kill the PIDs in
`monitor.pid` and the presence process (`pkill -f presence.py`).

Equivalent manual start (no watchdog):
`python3 monitor.py` and `python3 presence.py`, each detached, from
`<package dir>`.

## Step 9: Smoke tests

Run these with the owner watching; each maps to a behavior they should
see working before calling the install done.

1. **Mention reply**: owner posts `@<bot> hello` in a channel the bot
   can send in. Expect: typing indicator within ~5s, a reply in the
   persona's voice within ~15s (coalesce window + worker time).
2. **Reply-to-bot**: owner uses Discord's reply feature on a bot
   message, no @mention. Expect a reply (the `reply_to_bot` trigger).
3. **DM policy**: owner DMs the bot: expect a reply. A non-owner DMs
   it (second account, if available): expect total silence, and the
   monitor log shows nothing about it (owner_only means unread).
4. **Talk scope**: in a channel where the bot can read but NOT send,
   mention it. Expect: no reply, one log line `Skipping replies in
...: the bot cannot send messages there.`, and a `skipped` entry
   in `mention_ledger.json` for that message.
5. **Catch-up**: stop the monitor (`kill $(cat monitor.pid)`), post a
   mention, wait a minute, run `bash watchdog.sh`. Expect the reply
   after restart: `last_seen` never advanced while it was down.
6. **Sweep recovery** (the deep guarantee): the sweep runs every
   ~3 min and logs `Sweep recovered lost mention ...` only when it
   finds a mention no record accounts for. No log lines about it in
   normal operation is correct; the ledger is the audit trail.
7. **429 behavior** (cannot be forced; know the shape): during a
   Discord rate limit, monitor.log shows ONE line
   `RATE LIMITED: backing off REST for Ns.` per storm, the gateway
   keeps delivering, and polls resume when the window ends. Do NOT
   restart the monitor repeatedly during a storm; restarts and extra
   API calls extend the limit.
8. **Welcome** (if enabled): a new member joins (alt account). Expect
   a welcome in the resolved channel, drafted from the prompt param.
9. **Generation** (if enabled): owner asks the bot for an image.
   Expect a reply with the image attached, and the file visible in
   `generated/<message id>/`. A non-allowed user asking gets a brief
   decline.
10. **New server** (if a second server is available): invite the
    bot there. Expect a DM to the owner within a minute naming the
    server, its ID, the visible channels, and observe mode. An
    @mention there gets a reply, and a member joining there gets no
    welcome until the server is activated.
11. **Another bot** (if one is in the server): have it @mention this
    bot or reply to one of its messages. Expect a reply that reaches
    the other bot as a real mention or reply, and expect the exchange
    to wind down on its own (silence or a reaction) instead of
    bouncing confirmations back and forth.

## Step 10: Register the update hook

When a new muse-discord release (a version tag such as `v1.2.0`)
ships, the bot DMs each owner what is new, with **Update now** and
**Later** buttons. **Update now** writes `update_request.json` in
`<package dir>`; this hook turns that into a wake of the owner's Muse
in its main chat, which then updates the install to that release by
following UPDATING.md. Without the hook, the button does nothing. The
DMs follow `updates.check` in config.yaml.

- **Muse hook runtime**: register `hooks/update-watch.sh` as a
  polling hook (every ~60 seconds is plenty) delivered to the
  owner's **main chat** (not the worker's chat: updating needs the
  full agent and the owner's answers), with this wake prompt:

  ```
  An owner pressed "Update now" on muse-discord's update DM. The wake
  payload names the package dir and the requested release (a version
  tag like v1.2.0). Update this install to exactly that release by
  following UPDATING.md in that package dir, from step 1, with the
  owner in this chat; it asks before touching anything they
  customized. When you are done, or if you stop early (merge
  conflicts, local changes the owner has not approved, already on
  that version), delete update_request.json in the package dir and
  report here in a few lines: the version now running, or why you
  stopped and what is waiting on the owner.
  ```

  The script never runs git and holds no secrets; it reads only
  `update_request.json` and wakes once per request. It needs `jq` on
  PATH, and `MUSE_DISCORD_DIR` if the package lives elsewhere.
- **Any other runtime**: poll for `update_request.json` and, once per
  request, start a session in the owner's main chat with the prompt
  above.

To check it: run `bash watchdog.sh` (or wait for the daily check);
when a newer release exists, each owner gets one DM about it. Pressing
**Later** leaves the install as it is. An install with no release yet
(tracking main) is offered the newest release, unless it already
contains it.

## Troubleshooting

- **No welcomes in a server**: the server is in observe mode. Add
  its ID to `behavior.active_guild_ids`.

- **`GATEWAY DISABLED (code 4014)` in monitor.log** (the watchdog
  alerts "monitor degraded"): Message Content intent is off.
  Step 1.3. The bot still polls, but content-based triggers are
  blind; fix the intent and restart.
- **`TOKEN REJECTED` in monitor.log / watchdog says token
  rejected**: the token was reset in the portal. Repeat step 3 with
  the fresh token; the watchdog starts the bot on its next run.
- **Watchdog kills the monitor at startup**: the monitor prints the
  `Bot user id` marker (or `Bot identity pending` while Discord rate
  limits it) BEFORE discovery on purpose; if monitor.log has neither
  line, the token is unreadable or Python deps are missing (check
  the log's first lines). Each restart keeps the previous run's log
  as `monitor.log.1`.
- **Watchdog says the monitor was stuck**: its main loop stopped
  touching `monitor.heartbeat` for 10+ minutes, so the watchdog
  restarted it. The stuck run's log is `monitor.log.1`.
- **"no readable channels found"**: the bot lacks View Channel in
  every channel, or hasn't been invited. Check step 2 and channel
  overrides.
- **Queue fills but no replies**: the worker isn't waking. Check the
  hook/poller registration (step 7) and that paths point at THIS
  package dir's `reply_queue.json`. The wake script hands an item out
  again 10 minutes after its last wake in case a worker died
  mid-drain.
- **Replies sent twice**: two workers or two monitors are running.
  The monitor's pidfile singleton prevents the latter; check for a
  second worker poller.
