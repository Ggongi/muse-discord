# Changelog

Newest first. **Action needed** lists what an existing install must
do after updating; UPDATING.md walks a Muse through it.

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
