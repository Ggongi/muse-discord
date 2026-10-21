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
     monitor never requests it.
4. (Optional) Under **Bot**, set the bot's username/avatar as desired.
   Under **General Information**, note the **Application ID**; it is
   the `client_id` for the invite URL in step 2, and leave
   **Interactions Endpoint URL** empty: buttons and `/settings` reach
   the bot over its gateway connection, and a URL there sends them
   elsewhere.

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

The `bot` scope is enough for `/settings` too: Discord includes the
`applications.commands` scope with `bot` automatically, so there is
no second scope to add, and a bot invited with an older `scope=bot`
URL needs no new invite. The monitor registers the command itself.

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
var, set `token_from` in config.yaml (step 4) to that path or to
`env:NAME` instead. An env var must be set wherever the watchdog
runs: the processes it starts inherit its environment.

## Step 4: Fill config.yaml

```bash
cp config.example.yaml config.yaml
```

Setup fills in only three settings. Everything else already has a
careful default, so do not walk the owner through the rest of the
file unless they ask.

- **owners**: the owner's Discord user ID, as a quoted string in a
  list (`owners: ["123456789012345678"]`); add co-owners the same
  way. **[owner]** To find it: in Discord, open Settings, Advanced,
  enable **Developer Mode**; then right-click your own avatar/name
  anywhere and choose **Copy User ID**.
- **persona**: leave it at `persona.md` (step 6 writes that file).
- **token_from**: leave it at the token file from step 3, or set the
  path or `env:NAME` used there.

What the defaults mean, in one breath, so the owner knows what they
are getting: in servers the bot answers when @mentioned or replied
to, and keeps talking with someone it just answered when they write
again within about 2 minutes; in DMs it answers only its owners;
replies may draw on public facts about the owner (name, interests,
public projects, opinions) but nothing private; there are no daily
caps; image and video generation is off. Step 5 and "Changing
settings later" below cover changing any of it.

Check the file (no Discord connection is made, and the token is not
needed for this):

```bash
python3 bin/settings.py validate
```

It names each problem: the file, the setting, and why. Fix every
error before step 8: the bot refuses to start with an invalid
config.yaml.

## Step 5: Decide on image and video generation

Ask the owner one question: should the Muse make images and video when
asked? It is off by default. Turn it on only if the worker runtime
actually has media tools; if it doesn't, leave it off (the worker must
never claim generation it cannot start):

```yaml
everywhere:
  media:
    enabled: on
```

When on, the owners (or anyone listed in `media.who`) can ask, up to
`media.per_day` a day (`none` = no cap).

Run `python3 bin/settings.py validate` again after editing.

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
     `jq` and `python3` on PATH. The hook also restarts a dead
     monitor within seconds, without waking a worker (step 8).
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
timer). The watchdog is idempotent and safe to run any time (runs
never overlap: each holds a lock on `watchdog.lock`); it alerts only
on NEW failures (its output contract is documented at the top of the
script). Once a day it also checks for a new muse-discord version and
prints one `UPDATE:` line when there is one; tell the owner, and
update by following UPDATING.md when they say so.

The wake hook (step 7) restarts a dead monitor much sooner. Every
5 seconds it checks that the monitor process exists; when it does not
(say, the VM was replaced and every process died), it starts
`watchdog.sh --from-hook` detached, so the bot is back within seconds
instead of at the next scheduled run. That costs no tokens and wakes
no worker. While restarts keep failing it backs off (1, 2, 5, then
every 10 minutes), and after a token rejection it leaves the restart
to the scheduled run. Keep the schedule anyway: it is the backstop
that restarts presence and a stuck monitor (alive, stale heartbeat),
raises alerts, and checks for updates. A hook-triggered run leaves
alerts to it, so a failure that run hit is still reported by the next
scheduled run. With `token_from: env:NAME` (no token file), the
variable must also be set where the hook runs, or only the scheduled
run can restart the monitor.

To stop everything: unregister the wake hook and the watchdog schedule
first (either one restarts a dead monitor), then kill the PID in
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
   message, no @mention. Expect a reply (a reply to the bot counts
   as addressing it). Then, within 2 minutes, the owner writes once
   more in that channel with no @mention and no reply: expect an
   answer too (the conversational follow-up). The same message from
   anyone else gets none.
3. **DMs**: owner DMs the bot: expect a reply. A non-owner DMs it
   (second account, if available): expect total silence (the default
   `dms.who.dms_from: owners`).
4. **Talk scope**: in a channel where the bot can read but NOT send,
   mention it. Expect: no reply, one log line `Skipping replies in
...: the bot cannot send messages there.`, and a `skipped` entry
   in `mention_ledger.json` for that message.
5. **Catch-up**: stop the monitor (`kill $(cat monitor.pid)`) and post
   a mention right away. Expect the wake hook to restart it within
   ~10s (`watchdog.log`: `run triggered by the wake hook`) and the
   reply right after: `last_seen` never advanced while it was down.
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
8. **Generation** (if enabled): owner asks the bot for an image.
   Expect a reply with the image attached, and the file visible in
   `generated/<message id>/`. A user not in `media.who` asking gets a
   brief decline.
9. **New server** (if a second server is available): invite the bot
   there. Expect a DM to the owner within a minute naming the server,
   its ID, and the visible channels. The new server follows
   `all_servers`: an @mention there gets a reply.
10. **Another bot** (if one is in the server): have it @mention this
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
DMs follow `updates.notify` in config.yaml.

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

Last, save a memory note in the owner's main chat, so a later request
to change the bot's settings finds its guide: "To change
muse-discord's settings, read `<package dir>/SETTINGS.md` first and
follow it."

Tell the owner, in one line: muse-discord sends its maintainer
anonymous daily usage counts (no names, ids or messages; README,
"Privacy and usage stats"), and `python3 bin/telemetry.py off` (or
`telemetry: usage_stats: off` in config.yaml) turns them off.

## Changing settings later

The owner may ask to change how the bot behaves at any time ("be
brief in the work server", "stay quiet in #announcements", "let my
friend's DMs through"). There are three ways:

- **They DM the bot** what they want ("only answer in #general when I
  @ you"). It changes the setting itself: changes that are safe or
  make it quieter apply at once, with an **Undo** button; changes
  that open it to more people or loosen their privacy show what they
  would do and wait for their **Apply**. Only the owners' own DMs
  count.
- **They type `/settings`** in Discord (below).
- **They ask you**, in this chat: follow SETTINGS.md, which changes
  settings with `bin/settings.py` (the bot re-reads them at once, no
  restart). Edit `config.yaml` by hand only for what that does not
  offer (owners, persona, never_share, plugins).

The owner can also change the most-used settings from Discord,
without the Muse: type `/settings` in any channel the bot is in, or in
a DM with the bot. Only the person who typed it sees the page it
opens, and it is one page:

- At the top, one line on how the bot behaves there ("I reply only
  when @mentioned · shares only public facts about people"), and a
  **Where** select: the channel you typed it in (in a thread, its
  parent channel), all of that server, or everywhere (every server
  and DM that has no setting of its own). In a DM with the bot it
  opens DMs.
- Below it, a few settings for that place, each a select: **When I
  reply** (Chat freely, Only when @mentioned), **Reply length**,
  **Images & video** (Off, Only you, Everyone), **Who can DM me**
  (in DMs and everywhere) and **Replies a person a day** (for a
  server and everywhere). A channel has only When I reply and Images
  & video, and either can be "Like the server" to follow the server
  again ("Like the <name> category" when the channel's category sets
  its own value).
- **Style** for the whole server: Friends, Community or Work, applied
  in one click (it lists what changed, with **Undo**).
- **Pause here** (on the Everywhere page, **Pause everywhere**) and
  **Resume**, **Why no reply?**, and **Undo** for the last change
  made there (on a channel's page, the newest change in its server).
- Everything else is changed by DMing the bot (above) or asking your
  Muse: instructions, what it may say about you, other bots, the
  total daily cap, prompt injection, who it answers or ignores, who
  may DM it or make images by name, images and video per day, the
  status line, update notices, and plugins. The page's last line says
  so.
- Each change shows a **Done** line with **Undo**. Changes with risky
  results (DMs opened to more people, images and video for everyone
  without a cap, a cap removed in a big server, Pause everywhere)
  show what they mean and wait for **Apply**.
- **Why no reply?** lists the last messages that addressed the bot
  there and got no answer, why, and a fix button where one helps.
  Right-click a message, Apps, **Why no reply?** does the same for one
  message. That menu entry shows only for people with Manage Server
  (server admins, and the owner in their own servers); elsewhere, use
  the **Why no reply?** button on the `/settings` pages.
- When the bot joins a new server, the owner's DM about it has a
  **Configure** button that opens that server's page.
- Server admins (Manage Server) who type `/settings` in their own
  server get **Quiet the bot**: answer only @mentions or nothing, in
  that channel or the whole server; ignore people (and stop ignoring
  those admins added); turn images and video or a plugin off; and
  undo what admins changed (back to the owner's setting, never
  louder). The owner gets one DM per minute summing up
  what admins changed. Anyone else is told only the owners can change
  settings.
- Not editable from Discord: `owners`, `token_from`, `persona`, the
  `never_share` phrases, categories, and installing plugins. Those
  stay in config.yaml.
- Discord changes go to `config.discord.yaml`, never into
  config.yaml. If the owner later edits the same setting in
  config.yaml, the file wins and the owner gets a DM saying so.

- Settings live in a few places, and the most specific one wins:
  `everywhere`, then `dms` for direct messages, or `all_servers`,
  then one server under `servers`, then a category, then one
  channel. A thread follows its parent channel. Lists replace the
  list above them; only `notes` add up (the server's, then the
  channel's).
- A channel or category takes only `replies`, `notes`,
  `media.enabled` and `plugins.<name>.enabled`. Everything else is
  set per server at most.
- `replies` is `conversational` (the default: answers when
  addressed, and keeps talking with the person it just answered
  when they write again within about 2 minutes), `when-addressed`
  (only @mentions and replies to the bot), or `off`.
- Servers and channels are keyed by their id, in quotes. A `name:`
  next to one is only a label for people.
- After every edit, run `python3 bin/settings.py validate`. A config
  that fails it keeps the bot from starting.

When the owner asks "what is my bot doing in X?", show them instead
of reading the file:

```bash
python3 bin/settings.py show --explain                  # everywhere
python3 bin/settings.py show --dms --explain
python3 bin/settings.py show --server <id> --explain
python3 bin/settings.py show --server <id> --channel <id> --explain
```

`--explain` prints each setting's effective value and where it came
from (built-in, a line in config.yaml, or a broader scope).
`python3 bin/settings.py explain --place server:<id>` says the same
in plain words, and `set`, `reset`, `undo`, `preset`, `pause` and
`resume` change settings the way /settings does (see
`python3 bin/settings.py --help`; risky changes need `--yes`, which
you add only after the owner says yes).
Settings changed through the bot rather than the file are kept in
`config.discord.yaml` next to it; `show --explain` names them too,
and `python3 bin/settings.py fold` copies them into config.yaml (see
PROTOCOL.md, "Settings").

The few engine timings (polling, sweep, backoff) are not owner
settings; PROTOCOL.md lists them under "Engine settings" for the
rare install that needs them.

## Troubleshooting

- **`/settings` does not show up in Discord**: the monitor registers
  it once per start; monitor.log says `/settings command created`
  (or `updated`) when it did, and a `registration failed` line (it
  retries every minute) when it could not. A new command can take a
  moment to appear; restarting the Discord app helps. In a server,
  check Server Settings, Integrations, the bot: a server admin may
  have hidden the command there. If the bot answers "Settings UI
  unavailable", `settings_ui.py` is missing from the package dir.
  **Why no reply?** (right-click a message, Apps) is registered the
  same way; if Discord refuses the "?" in its name, the log says so
  and it is registered as "Why no reply".
- **`GATEWAY DISABLED (code 4014)` in monitor.log** (the watchdog
  alerts "monitor degraded"): Message Content intent is off.
  Step 1.3. The bot still polls, but content-based triggers are
  blind; fix the intent and restart.
- **`TOKEN REJECTED` in monitor.log / watchdog says token
  rejected**: the token was reset in the portal. Repeat step 3 with
  the fresh token; the watchdog starts the bot on its next scheduled
  run (the wake hook never retries a rejected token itself).
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
