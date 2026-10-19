# Changelog

Newest first. Each release is headed `## vX.Y.Z (YYYY-MM-DD): Title`
and is published as a GitHub Release (see RELEASING.md). The dated
entries below the releases are pre-release history. **Action
needed** lists what an existing install must do after updating;
UPDATING.md walks a Muse through it.

## 2026-10-19: Change settings by asking

New:

- DM the bot what you want, like "only answer in #general when I @
  you" or "be brief in the Work server", and it changes the setting.
  Changes that are safe or make the bot quieter apply at once, with
  an Undo button. Changes that open the bot to more people or loosen
  your privacy show what they would do and wait for your Apply. Only
  your own DMs count: never server messages, other people, or
  forwarded messages. If someone else's message tries it, nothing
  changes and you get a DM about it (at most one an hour).
- Your Muse can change settings from its main chat too (SETTINGS.md).
  Each change it makes shows up in your DMs with an Undo button,
  without a ping.
- `bin/settings.py places` lists your servers and channels with their
  ids, and `bin/settings.py` names channels in what it prints.

**Action needed:**

- Update the installed worker prompt (new SETTINGS REQUESTS section).
- Have your Muse save a memory note: "To change muse-discord's
  settings, read <package dir>/SETTINGS.md first."
- If your runtime registered a copy of hooks/mention-watch.sh, update
  the copy.

## 2026-10-19: Settings engine: places, plain words, undo groups, presets

New:

- "Who can DM it" can name people: `dms.who.dms_from` takes a list
  of user ids, meaning you plus those people (for example a friend),
  besides `owners`, `shared-servers` and `anyone`. Their DMs are
  caught up after an outage like yours.
- `bin/settings.py` can change settings, not only show them: `set`,
  `reset`, `undo`, `preset` (Friends, Community or Work, for a
  server), `pause`, `resume` and `explain`, each for one place
  (`defaults`, `dms`, `server:<id>` or `channel:<id>:<id>`). It says
  what changed in plain words. Risky changes are only made with
  `--yes`.
- Changes made together (a preset, Pause everywhere) are undone
  together with one Undo.

Changed:

- Risk warnings look at what a change leads to, not only at the one
  setting: for example opening DMs warns only when more people can
  actually DM the bot, and an allow list warns how many places go
  quiet.
- Setting a value that is already in force writes nothing, and
  setting what config.yaml says at that spot removes the Discord
  change instead, so the file stays in charge.
- Server admins can undo their own changes back to what the owners
  set (still never louder than that). They can no longer remove a
  setting you made, and what you change later (pausing a server, for
  example) now also reaches the channels an admin quieted.
- Settings changed from Discord and from `bin/settings.py` at the
  same moment no longer overwrite each other, and a
  `config.discord.yaml` that cannot be read is left alone (changes are
  refused with a message) instead of being replaced.

**Action needed:**

- None.

## 2026-10-19: Daily limits, ignored mentions, threads and notices

Fixed:

- A message over a daily limit (`limits.per_day` /
  `limits.per_person_per_day`) is now dropped right away. It used to
  be retried for 15 minutes while the person's next messages there
  were folded into it and went unanswered, and a restart could answer
  it late. Their later messages are judged on their own.
- A mention the settings tell the bot to ignore (replies off, someone
  on the ignore list, other bots ignored, DMs off) stays ignored. It
  used to get answered up to 6 hours later if the setting was relaxed
  in the meantime. With DMs off, your own DMs are no longer answered
  late either.
- A mention in a brand-new thread or forum post could wait about 4
  minutes for a reply when the bot noticed the thread at the same
  moment. It is now answered right away.
- Discord's own notices ("X pinned a message", "X started a thread")
  no longer count as someone talking to the bot. Right after a reply,
  a pin could get answered as a follow-up.

**Action needed:**

- None.

## 2026-10-19: Status line changes show up right away

Fixed:

- Changing the bot's status line (`presence.status` / `presence.kind`)
  with `/settings` or in `config.yaml` did nothing until the presence
  client restarted, which the watchdog never does while it is healthy.
  It now updates within a few seconds, without reconnecting.

**Action needed:**

- None.

## 2026-10-19: Change settings from Discord with /settings

New:

- Type `/settings` in a DM with your bot or in a server. A private
  page (only you see it) lets you pick a place (everywhere, DMs, all
  servers, a server, a channel or category) and change how the bot
  behaves there with menus and buttons. Changes apply at once, show
  where each value comes from, and can be reset or undone. Risky
  changes (your memory in a big server, strangers' DMs, unlimited
  image generation) ask you to confirm first. "Why?" shows how each
  value was decided.
- Server admins can use `/settings` in their own server to make the
  bot quieter there (replies off or only when addressed, plugins off,
  ignore someone), never louder; you get a DM when they do.
- When the bot joins a new server, its DM to you has a Configure
  button.
- Discord changes go to `config.discord.yaml`; your `config.yaml` is
  never rewritten.

**Action needed:**

- None. The existing bot invite already covers slash commands; the
  command registers itself on the next start. `/settings` can take a
  minute to appear in Discord the first time.

## 2026-10-18: New settings, per server and per channel

New:

- A simpler `config.yaml`: settings grouped by what you think about
  (who it talks to, how it talks, what it may do, privacy, limits),
  set for everywhere, DMs, all servers, one server, or one channel or
  category. The most specific place wins; threads follow their
  channel. `bin/settings.py show --explain` shows what the bot does in
  any place and where each value comes from.
- Conversational replies (the default): after the bot answers you,
  your next messages in that channel within about 2 minutes count as
  talking to it, even without an @mention or Discord's reply. Set
  `who.replies: when-addressed` for the old, strict behavior.
- Your Muse's memory stays private: `privacy.memory` (default
  `public-only`) tells it what it may draw on outside your own DMs,
  and `privacy.never_share` holds any reply containing a listed
  phrase for you to Send anyway or Discard.
- Strangers' DMs are not answered by default (`who.dms_from: owners`).
- Every cap accepts `none`, the default.
- Changes made from Discord (coming with `/settings`) will live in
  `config.discord.yaml`, never rewriting your file.

**Action needed:**

- Nothing breaks: an old `config.yaml` keeps working, with one notice
  line in monitor.log. It also gets conversational replies, the one
  intended change in behavior.
- Optional: convert the file with `bin/settings.py migrate` (keeps a
  backup; refuses if anything else would change). UPDATING.md has
  your Muse offer it and ask you first.
- Reinstall the worker prompt from `worker-prompt.template.md`.
- A config with a mistake now stops the bot with a message naming the
  setting, instead of quietly guessing. Run
  `bin/settings.py validate` after editing.

## 2026-10-18: Updates arrive as a DM with an Update button

New:

- Updates now come as releases: version tags such as `v1.0.0`, each
  with its own CHANGELOG entry, instead of every commit on main. When
  a new release is out, the bot DMs its owners what's new in it, with
  **Update now** and **Later** buttons. Update now wakes your Muse in
  its main chat to update to exactly that release (following
  UPDATING.md) and report back; Later skips that version (a newer one
  asks again). Before, the daily check only printed a line to the
  watchdog's scheduler, which owners never saw, and counted commits.
  `updates.check: false` turns the DMs off.
- The first release, v1.0.0, is tagged right after this change. An
  install on main is then offered it, unless it already has it.

**Action needed:**

- This update itself is applied by hand, the last one: tell your Muse
  "Update muse-discord by following UPDATING.md". Its step 4 now
  registers the new update hook (`hooks/update-watch.sh`, SETUP.md
  step 10), and the restart in step 5 turns the DMs on.

## 2026-10-18: Back online in seconds after a crash

Fixed:

- When the machine running the bot was replaced or the monitor died,
  the bot stayed offline until the next watchdog run, up to 10
  minutes. The wake hook, which already runs every few seconds, now
  starts the watchdog as soon as it sees no monitor, so the bot is
  back within seconds. The watchdog cron stays as the backstop. A
  rejected token never causes a restart loop.
- Two watchdog runs at once could start two monitors; the watchdog
  now holds a lock.

Faster:

- The reply worker ends its run as soon as its replies are written:
  no summary or follow-up checks. A message that arrives right after
  a reply no longer waits for that wrap-up. It still flags abusive
  messages for you in its one final line.

**Action needed:**

- Reinstall the worker prompt from `worker-prompt.template.md`.
- If your hook runs a copy of `hooks/mention-watch.sh` rather than the
  package's own file, update the copy. If the bot uses `bot.token_env`,
  set that variable where the hook runs too, or only the cron can
  restart it.

## 2026-10-18: One slow reply no longer holds back the rest

Faster:

- When one wake carries several messages (say a DM plus two welcomes),
  the worker now answers DMs and mentions first and sends each reply
  as soon as it is written, instead of writing them all at the end.
  Replies in the same channel keep their order.

Fixed:

- A reply to a message that was deleted before the reply went out
  failed on every retry until it was dropped. It now posts as a
  normal message.
- The latency log could show a negative `ingest` when the machine's
  clock trailed Discord's by a fraction of a second.

**Action needed:**

- Reinstall the worker prompt from `worker-prompt.template.md` (the
  faster ordering only takes effect then). If your agent runtime
  limits tool calls per run, allow `bin/outbox-append.py` to run once
  per reply.

## 2026-10-18: Realtime messages no longer wait for the REST poll

Fixed:

- A DM or mention the gateway delivered at once could wait 25-95s
  before the monitor even looked at it, because the main loop read
  every channel over REST (and ran the sweep and channel refreshes)
  in one go before handling it; behind a slow proxy that took most of
  each 30s cycle. The REST work is now done in short slices between
  the realtime work, so new messages are picked up within a second.
  Every channel is still read about every `rest_poll_seconds`, the
  sweep and refreshes keep their cadence, and the catch-up after a
  restart still reads everything.
- A Discord read that hangs gives up after 8s instead of 20s.

Faster:

- A DM burst goes out 0.5s after its author stops (servers stay at
  1.5s, bots at 5s; typing still holds a burst open).
- A reply the worker writes is picked up within about 0.25s instead
  of up to 2s.
- The latency line splits the old "coalesce" figure into ingest (until
  the monitor first saw the message) and hold (the burst wait):
  `Latency <id>: ingest 0.4s, hold 1.6s, worker 30.1s, send 2.0s,
  total 34.1s`.

**Action needed:**

- None; the monitor restarts with the update.

## 2026-10-17: Less code: three features removed

Removed:

- The bare-name trigger (`name` in `behavior.respond_to`,
  `behavior.call_names`, and `bin/check-call-name.py`). The bot answers
  @mentions, replies to it, and DMs; its name alone in a sentence is
  not a trigger. Observe mode stays, for welcomes.
- The optional `behavior.max_bot_turns` cap. The worker ends bot
  conversations by judgment, as before; `bot_turns` on queue items
  still tells it how long a run has gone (up to 12).
- The `--for` check in `bin/discord-api.py`. Owner commands still
  reach the tool only through owner items; uploads still come only
  from `generated/`, and ids are still validated. A leading `--for`
  is accepted and ignored.

**Action needed:**

- None required. Old `call_names`, a `name` trigger, or a
  `max_bot_turns` value in config.yaml print one warning at startup and
  are ignored; delete them to silence it.
- Optional: reinstall the worker prompt (name-drop and `--for` text
  removed).
- Optional: delete a leftover `owner_commands.json`.

## 2026-10-17: Faster replies, and fixes from a full code review

Faster and smoother:

- Replies come sooner. The worker gets its items and the persona in
  the wake message and writes all its replies in one step
  (`bin/outbox-append.py`); the monitor removes answered queue items
  itself. A burst goes out about 1.5s after its author stops typing
  (5s for a bot) instead of always waiting the full window.
- The typing indicator stays on until the reply lands, instead of
  flickering, for up to 3 minutes.
- Each reply logs a latency line showing where the time went.

Fixed:

- Replies the worker wrote while the monitor was sending could be
  lost, and queue edits from either side could undo the other's (a
  welcome could post twice). Both files are now locked.
- A second worker could start on items the first was still handling.
- A generated image could be attached to a different reply, even in a
  public channel. Files now live in `generated/<message_id>/` and only
  go out with that message's reply.
- The owner-command tool could upload any local file and was not
  limited to owner commands in code. It now only uploads from
  `generated/`, needs `--for <message_id>` naming a live owner
  command, validates ids, and never pings roles.
- Image and GIF links matched lookalike domains and followed redirects
  anywhere; downloads are now https-only, on the allowlist, and never
  to private addresses.
- Prompt injection hidden in attached documents or the replied-to
  message is now quarantined too.
- A revoked token left the bot running deaf: it now exits, and the
  watchdog alerts. The watchdog also restarts a monitor that hangs,
  keeps the previous log as `monitor.log.1`, and supports `token_env`.
- A mention in the middle of a burst could be answered twice.
- `bot.guild_ids` was ignored for realtime messages.
- First run (or a deleted `state.json`) turned the owner's DM backlog
  into one message.
- Messages missed while the gateway reconnected could be skipped for
  good; owner DMs are now swept too.
- A brief error during channel discovery dropped a channel for 30
  minutes.
- Explicit `0` config values were silently replaced by defaults, and
  `plugin.yaml` defaults (such as the welcome plugin's daily cap) were
  never applied.
- Pin and unpin use Discord's current endpoints.

**Action needed**

- Update the installed worker prompt from `worker-prompt.template.md`.
  Until you do, generated images will not attach and owner commands
  will be refused (the tool now needs `--for`), and the worker keeps
  its slower, unlocked path.
- If the wake hook runtime registered a copy of
  `hooks/mention-watch.sh`, update the copy.
- To let the bot pin messages, enable **Pin Messages** for its role
  in each server (new invites from SETUP.md include it).
- If you use the welcome plugin without a `daily_cap` in your config,
  the documented default of 10 a day now applies.

## 2026-10-17: No more duplicate replies

- Fixed: the bot could post the same reply several times in a row.
  A send that timed out after Discord had already accepted it was
  retried, posting another copy each time. Every message now carries
  a nonce that Discord uses to drop repeats, and retries are spaced
  out and stop after 90 seconds.
- Fixed: two monitors could start at the same moment (for example,
  overlapping watchdog runs) and each send every reply. The pidfile
  is now locked for the monitor's whole lifetime.
- A multi-part reply that fails partway is now retried instead of
  dropped; the parts that already landed are not repeated.

**Action needed**

- Update and restart (UPDATING.md). Every install before this one can
  post duplicates.
- Check for a second running monitor once: `pgrep -af monitor.py`
  should list one process. If it lists more, stop them all and run
  `bash watchdog.sh`.

## 2026-10-16: Bots can talk to each other

- Other bots, such as other people's Muses, can @mention this bot or
  reply to it and get an answer (`behavior.bots: direct`). They never
  trigger name-drops or welcomes.
- The worker addresses people and agents with real mentions, ends
  conversations the way a person would (silence, a reaction, or a
  closing line that invites no reply), and never answers a pure
  acknowledgment.
- Optional `behavior.max_bot_turns` caps bot-to-bot runs with no
  human in between. No cap by default.
- No status line under the bot's name by default.
- Removed the burst breaker added in the previous release: it cut
  lively conversations off mid-flow, which is not how a person chats.
- No daily reply cap by default (`safety.daily_reply_cap: null`).
  Both reply caps are still available.
- Fixed: replies to multi-message bursts, and deliberate no-replies,
  could be requeued by the sweep and answered twice.

**Action needed**

- Update the installed worker prompt from `worker-prompt.template.md`
  (new OTHER AGENTS, ADDRESSING PEOPLE, and ENDING CONVERSATIONS
  sections, and `silent` outbox items).
- Optional: set `presence.status_text: ""` in `config.yaml` to drop
  the "Watching for mentions" line.
- Optional: set `behavior.bots: off` to keep ignoring other bots.
- Optional: set `safety.daily_reply_cap: null` in `config.yaml` to drop
  the old 30-a-day cap, and delete the `safety.burst_breaker` block
  (it is ignored now).

## 2026-10-16: Trigger safety

- Only @mentions, replies to the bot, and DMs trigger it by default.
  Answering to its bare name is an opt-in `name` trigger using
  `behavior.call_names`; setup checks the name for collisions with
  `bin/check-call-name.py`.
- New servers start in observe mode and the owner gets a DM summary.
  Welcomes and name triggers run only in servers listed in
  `behavior.active_guild_ids`.
- The bot never answers messages from before it joined a server.
- Burst breaker: more than 5 replies in 10 minutes in one channel
  quiets it for 30 minutes and DMs the owner. (Removed in the next
  release.)

**Action needed**

- If the welcome plugin is on: add each server's ID to
  `behavior.active_guild_ids`, or welcomes stop.
- If the bot should keep answering to its bare name: add `name` to
  `behavior.respond_to` and set `behavior.call_names` (run
  `bin/check-call-name.py` on the name first).
- Update the installed worker prompt (name-drop handling).

## 2026-10-15: Initial release
