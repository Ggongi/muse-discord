# TROUBLESHOOTING.md: when muse-discord misbehaves

For the owner's Muse. Start with `python3 bin/doctor.py --no-dm`: it
names each problem with its fix. The notes below cover what it reports,
the watchdog's alerts, and checks to run by hand.

## What a problem means

- **The bot is not running**: run `bash watchdog.sh`. If it still
  fails, read the first lines of `monitor.log` (the previous run's log
  is `monitor.log.1`): a Python error usually means missing packages,
  so run `bash bin/install.sh`.
- **`TOKEN REJECTED` / "Discord rejected the token"**: the token was
  reset in the Developer Portal. Ask the owner for a fresh one (Bot
  page, **Reset Token**, **Copy**) and pipe it to
  `bin/connect-discord.py` as in SETUP.md step 3. The watchdog starts
  the bot on its next run (the wake hook never retries a rejected
  token by itself).
- **`GATEWAY DISABLED (code 4014)` / "Message Content is off"**: the
  bot polls but can't see replies, follow-ups or reply-to-bot. Run
  `bin/connect-discord.py` again with the saved token (it turns the
  intent on), or send the owner its `OWNER STEP`. Then restart:
  `kill "$(cat monitor.pid)"; bash watchdog.sh`.
- **The bot looks stuck**: its main loop stopped touching
  `monitor.heartbeat` for 10+ minutes; the watchdog restarts it, and
  the stuck run's log is `monitor.log.1`.
- **It can't see any channels**: it was not invited, or its role lacks
  View Channels. Send the owner the invite link again
  (`bin/connect-discord.py --wait 1` prints what it sees). To keep it
  out of a channel, deny it View Channel there.
- **It can't post where it was mentioned**: give its role Send Messages
  in that channel, or mention it elsewhere.
- **A hook "has never run" or "last ran N minutes ago"**: register it
  again as SETUP.md step 5's table says; each hook touches a
  `.seen-<name>` file in the package dir on every run.
- **Queue fills but no replies**: the wake hook isn't waking a worker,
  or its prompt doesn't point at this package's `worker-prompt.md`.
  The hook hands an item out again 10 minutes after its last wake, in
  case a worker died.
- **Replies sent twice**: two workers are draining the queue (two
  registered wake hooks, or a hook plus a poller). The monitor's pid
  file already prevents two monitors.
- **Couldn't DM the owner**: they block DMs from server members. In
  Discord: right-click the server icon, **Privacy Settings**, switch on
  **Direct Messages**.
- **`/settings` doesn't show up**: the monitor registers it once per
  start (`/settings command created` in monitor.log, or a
  `registration failed` line; it retries every minute). Restarting the
  Discord app helps; a server admin may have hidden it under Server
  Settings, Integrations. "Settings UI unavailable" means
  `settings_ui.py` is missing.
- **The invite link fails**: on the Bot page, **Requires OAuth2 Code
  Grant** must be off. `bin/connect-discord.py` reports it.
- **Buttons and `/settings` do nothing**: the app's **Interactions
  Endpoint URL** (General Information page) must be empty: they reach
  the bot over its gateway connection.
- **Pin requests fail**: a bot invited before February 2026 lacks the
  Pin Messages permission. In Server Settings, Roles, the bot's role,
  switch on **Pin Messages** (or invite it again with the current link).
- **It ignores a server it was added to**: none of the owners is in
  that server, so it answers nobody there (a stranger may have added a
  public bot). The owner's join DM says so; to let it talk there, they
  press **Configure** and choose When it answers.

## Keeping the token somewhere else

`token_from` in config.yaml can name another file, or `env:NAME` for
an environment variable. A variable must be set wherever the watchdog
and the wake hook run (the processes they start inherit it); with only
the variable, just the scheduled watchdog run can restart the monitor.

## Stopping the bot

Unregister the wake hook and the watchdog hook first (either one
restarts a dead monitor), then `kill "$(cat monitor.pid)"` and
`pkill -f "^python3 presence.py"`.

## Checks by hand

Worth running after a big change, with the owner's help where noted.
`mention_ledger.json` records how every message that addressed the bot
ended.

1. **Reply-to-bot and follow-ups** (owner): reply to a bot message
   without @mentioning it: it answers. Within 2 minutes, write once more
   with no mention: it answers that too. Someone else's message doesn't.
2. **A channel it can read but not post in**: a mention there gets no
   reply, one `Skipping replies in ...` log line and a `skipped` ledger
   entry.
3. **Restart catch-up**: `kill "$(cat monitor.pid)"` and mention the
   bot at once. The wake hook restarts it within ~10 s
   (`run triggered by the wake hook` in watchdog.log) and the reply
   follows.
4. **Sweep**: every ~3 minutes it rereads recent history and logs
   `Sweep recovered lost mention ...` only when it finds one no record
   accounts for; silence is normal.
5. **Rate limits**: during a Discord 429 storm monitor.log shows one
   `RATE LIMITED: backing off REST for Ns.` line per storm while the
   gateway keeps delivering. Don't restart it repeatedly: that extends
   the limit.
6. **Images and video** (owner, when the runtime can make them): ask it
   for an image; the reply carries it, and the file is in
   `generated/<message id>/`. Someone not in `media.who` gets a brief
   no.
7. **A DM from someone else** (second account): no answer (DMs are
   owners-only by default).
8. **Another server the owner is in**: invite the bot; the owner gets a
   DM naming the server and its channels, and a mention there is
   answered.
9. **Another bot**: have it @mention this bot; the answer reaches it as
   a real mention, and the exchange winds down instead of looping.
