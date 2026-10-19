# e2e: end-to-end tests on a real Discord server

These tests run a PR's `monitor.py` against a private Discord test
server. Two "driver" bots play people, following scripted scenarios
in `e2e/scenarios/`; the harness then pulls each scenario's channel
transcript and checks it.

- **Tier 1** (every same-repo PR): a scripted fake worker
  (`fake_worker.py`) answers the queue, and code checks
  (`checks.py`) judge the transcript. Deterministic; a failed check
  fails the PR's `E2E tier 1` check.
- **Tier 2** (only with the `e2e-full` label): Claude Code, headless,
  plays the real reply worker from `worker-prompt.template.md`
  (`claude_worker.py`), and a separate Claude run judges each
  transcript against the scenario's rubric (`judge.py`). Advisory: it
  never blocks a PR. It spends the owner's Claude subscription usage,
  which is why it is opt-in per PR.

Each tier posts one sticky PR comment with its summary table
(updated in place on every run) and uploads `out/` (results,
transcripts, logs) as a workflow artifact. Fork PRs get no secrets,
so the workflow skips them; without secrets, `run.py` reports
"skipped" and passes.

The offline unit tests (`e2e/tests/`, no network) run in the regular
CI job:

```bash
python3 -m unittest discover -s e2e/tests -t .
```

## One-time setup [owner]

1. **A private test server.** Create a new Discord server used only
   for these tests. The harness creates and deletes one channel per
   scenario in it.
2. **Three applications**, at
   https://discord.com/developers/applications, each with a bot:
   `muse-test` (the bot under test), `driver-a`, and `driver-b`. On
   each one's **Bot** page, enable **Message Content Intent** (the
   monitor needs it to read messages, and the drivers read the
   transcripts back through the REST API, which blanks message text
   without it). Server Members Intent stays off.
3. **Invite them to the test server** (replace `<APP_ID>` with each
   application's ID):
   - `muse-test`, with the same permissions as a real install
     (SETUP.md step 2: View Channel, Send Messages, Send Messages in
     Threads, Read Message History, Attach Files, Embed Links, Add
     Reactions, Pin Messages):
     `https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot&permissions=2252074691710016`
     (this already includes Send Messages in Threads, so it can answer
     in the threads the scenarios start)
   - `driver-a`: View Channel, Send Messages, Read Message History,
     Attach Files, Add Reactions, Manage Channels (it creates and
     deletes the scenario channels), Create Public Threads (it starts
     the scenarios' threads) and Send Messages in Threads:
     `https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot&permissions=309237746768`
   - `driver-b`: View Channel, Send Messages, Read Message History,
     Attach Files, Add Reactions and Send Messages in Threads:
     `https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot&permissions=274878008384`

   **Existing setups** (drivers invited before the thread scenarios,
   with 101456 / 101440): no re-invite is needed. In the test server,
   open Server Settings, Roles, and on the `driver-a` role enable
   **Create Public Threads** and **Send Messages in Threads**; on the
   `driver-b` role enable **Send Messages in Threads**. Until then the
   thread scenarios report a flake naming the missing permission.
4. **Repository secrets** (Settings, Secrets and variables, Actions):
   - `E2E_GUILD_ID`: the test server's ID (Developer Mode on,
     right-click the server icon, **Copy Server ID**).
   - `E2E_SUT_TOKEN`, `E2E_DRIVER_A_TOKEN`, `E2E_DRIVER_B_TOKEN`: the
     three bot tokens (each application's **Bot** page, **Reset
     Token**).
   - `CLAUDE_CODE_OAUTH_TOKEN` (tier 2 only): run `claude setup-token`
     on a machine where Claude Code is logged in to the subscription
     and paste the token it prints. Tier 2 reports "skipped" without
     it.
5. **The `e2e-full` label**: create it (Issues, Labels) so PRs can
   opt in to tier 2.

Never invite these three bots to a real server, and never reuse
their tokens elsewhere: the test config makes the drivers look human
to the bot under test (below), and driver A is its owner.

## `testing.treat_as_human` (test-only config)

```yaml
testing:
  treat_as_human: ["<user id>", ...]
```

Messages from the listed user ids are handled as if their authors
were human, everywhere the monitor treats bots differently (the bots
policy, `from_bot` / `bot_turns`, burst timing, owner gating). The
harness lists the driver bots there, so they can stand in for people.
The monitor logs a WARNING at startup while it is set. It is absent
from `config.example.yaml` on purpose: never set it in a real
install.

In the test config, driver A is the owner (`owners`) and listed in
`treat_as_human`. Driver B is listed too, unless a scenario sets
`b_is_bot: true` to exercise the bot-to-bot path.

## The test config

`config.test.yaml` is the SUT's `config.yaml` in the settings layout
(`muse_settings.py`): `owners` (driver A), `token_from` and `persona`
(filled in by `sut.py`), `everywhere` / `dms` with the shipped
defaults written out, the test server under `servers."<id>"` (with
`who.replies: conversational`), and an `engine:` block with fast
timings (REST poll 10 s, coalesce 5 s). `sut.py` always forces
presence off and `updates.notify: off`. A scenario's `config:` is
deep-merged on top and may use `{{GUILD_ID}}`, `{{A_ID}}`, `{{B_ID}}`
and `{{CHANNEL_ID}}` (the channel the runner created for this
scenario), in keys too, each as a whole key or value:

```yaml
config:
  servers:
    "{{GUILD_ID}}":
      who:
        ignore: ["{{B_ID}}"]
        replies: "off"
      channels:               # channel (or category) scope
        "{{CHANNEL_ID}}":
          replies: when-addressed
```

A channel block is flat, as in `config.example.yaml`: it may set only
`replies`, `notes` and `plugins.<plugin>.enabled` (plus `name`), not
`who:` / `style:` groups; anything else (limits, privacy, ...) is set
on the server. Threads follow their parent channel.

One scenario, `legacy_config_mention`, sets `legacy_config: true` and
runs under `config.legacy.yaml`, the old layout (`bot:`, `behavior:`,
`safety:`, `owners.user_ids`), on purpose: an old `config.yaml` must
keep working unchanged, and the monitor must print its one-line
"uses the old layout" notice.

What the settings scenarios cannot cover on Discord:

- Owner DMs. Driver A, the owner, is a bot, and Discord does not let
  a bot DM another bot. The never_share hold DM (with **Send anyway**
  / **Discard**) therefore fails, and the monitor logs "no owner
  reachable"; `never_share_hold` checks the hold in `state.json`, the
  skipped ledger entry, and that nothing was posted, not the DM or its
  buttons. For the same reason there are no `who.dms_from` scenarios.
- The follow-up window's expiry. It is a 120 s constant in
  `monitor.py` (`FOLLOW_UP_WINDOW_SECS`), not a setting, so testing it
  would take a 2-minute wait per run;
  `tests/test_settings_layout.py` covers it offline.
- `role:<id>` entries in `who.allow` / `who.ignore`: the drivers hold
  no roles in the test server.
- `/settings` itself. Bots cannot run slash commands or click buttons
  on someone else's ephemeral messages, so no driver can open it. The
  offline suites cover its routing and pages; on Discord it is tested
  by hand with the checklist below. What it does to the bot is
  covered live: a scenario's `settings` step writes
  `config.discord.yaml` through the same `muse_settings` calls (see
  "Adding a scenario").

## Manual checklist: /settings

Run it before a release that touches `/settings`, `settings_ui.py`,
or the interaction code, with a real install (or `muse-test`), your
own account as an owner, and a second account that is not an owner.
Keep `monitor.log` open (`tail -f`) and `config.discord.yaml` at hand.

Registration:

- [ ] Start the monitor: the log says `/settings command created` (or
      `updated`), or nothing on a restart with the command unchanged.
      No `registration failed` line.
- [ ] `/settings` appears in the bot's DM and in a server channel,
      and not in a group DM.
- [ ] Restart again: no `created` / `updated` line (no needless
      write).

Owner, in a DM:

- [ ] `/settings` opens Home, visible only to you: Everywhere, DMs,
      All servers, then each server by name with its member count.
      With more than 25 servers the picker pages.
- [ ] Open a scope; every drawer opens (Who it talks to, How it
      talks, What it may do, Privacy, Limits, Plugins), each value
      with its source tag.
- [ ] Change a choice (e.g. How it talks, length): the page updates
      in place, `config.discord.yaml` gains the entry with your id as
      `by`, the log says `Settings reloaded`, and the bot's next reply
      follows it without a restart.
- [ ] A number (Limits): the modal accepts a number and `none`,
      rejects text, and the value shows after submitting.
- [ ] Edit house rules: the modal is prefilled; inherited notes show
      as the placeholder.
- [ ] Reset next to a changed value writes `inherit`; the source tag
      falls back to the broader scope.
- [ ] Undo last change reverts the newest change at that scope; a
      second Undo goes one further back.
- [ ] Why? lists the chain for each setting on the page.
- [ ] A risky change (privacy memory loosened in a server, DMs from
      anyone, media for everyone) shows a confirmation with a red
      "Yes, change it"; backing out writes nothing.
- [ ] Not offered: owners, token, persona, never_share (shown as "N
      phrases set in config.yaml", read-only), plugin installation.
- [ ] Pause everywhere, then Resume: the bot stops and resumes
      answering.

Owner, in a server:

- [ ] `/settings` opens that server's page; This channel shows only
      replies, notes and plugin on/off (and This category when the
      channel has one).

Server admin (the second account, with Manage Server in one server,
not an owner):

- [ ] `/settings` there shows only the quieter controls (replies off
      or when-addressed, plugins off, add to ignore); no privacy or
      limits; no other server.
- [ ] Make a change: about a minute later the owner gets one DM
      summing up the admin's changes in that server (several changes
      in that minute arrive together).

Anyone else (the second account, no Manage Server):

- [ ] `/settings` answers "Only the bot's owners can change its
      settings." and nothing is written.

Owner DMs:

- [ ] Invite the bot to a new server: the join DM has a **Configure**
      button that opens that server's page.
- [ ] Change a setting with `/settings`, then edit the same setting
      in `config.yaml`: the owner gets one DM saying the file wins.

Robustness:

- [ ] Click through pages while the bot is busy replying: every
      click answers at once ("This interaction failed" never shows).
- [ ] Move `settings_ui.py` away and restart: `/settings` answers
      "Settings UI unavailable." and the log says why, once.

## Running locally

The bot under test must not be running anywhere else at the same time
(two monitors on one token both reply), including a CI run.

```bash
pip install -r requirements.txt
python3 e2e/run.py --tree . --tier 1 --dry-run   # no network, no env
export E2E_GUILD_ID=... E2E_SUT_TOKEN=... \
       E2E_DRIVER_A_TOKEN=... E2E_DRIVER_B_TOKEN=...
python3 e2e/run.py --tree . --tier 1
# one scenario, keeping its channel for a look afterwards:
python3 e2e/run.py --tree . --tier 1 --scenario mention_basic \
       --keep-channels
```

The harness runs `monitor.py` itself, from a copy of the tree, with
`updates.notify: off` (no update DMs to the owner) and presence off.
Neither `watchdog.sh` nor the wake hook runs, so nothing restarts the
monitor behind a scenario's `kill_monitor` step.

Results land in `out/` (`results.json`, `summary.md`,
`transcripts/`). Tier 2 also needs the `claude` CLI on PATH, logged
in or with `CLAUDE_CODE_OAUTH_TOKEN` set. `CLAUDE_BIN` points the
worker and the judge at another CLI binary, and `E2E_CLAUDE_MODEL`
picks the model for both (default: the CLI's).

## The tier-2 worker and judge

`claude_worker.py` does what `hooks/mention-watch.sh` does in a Muse
install (hand each open queue item to exactly one run, with the same
payload: the items plus the persona) and runs `claude -p` with the
PR's `worker-prompt.template.md`, its seams filled the way SETUP.md
step 7 fills them, and the test persona in `fixtures/persona.md`.
Like a real worker, it appends each reply as soon as it is drafted
(one `bin/outbox-append.py` call per item) and ends the run with the
single line `done`. Each wake gets 240 s; its duration, exit code, and
`ending=` (`done`, `flag`, or `other` for a run that ended any other
way) go to `claude_worker.log`, and the CLI's JSON output to
`claude_wakes/`. The hook's other job, restarting a dead monitor, is
left out: the harness owns the monitor.
The run has only the Bash and Read tools, in `dontAsk` permission
mode, behind a hook that allows exactly the outbox append (as often as
needed), the
`bin/discord-api.py` owner tool, and reading the item's media files.

`judge.py` runs a separate `claude -p` with no tools, giving it the
rubric, the persona, and a numbered transcript, and reads back
`{"verdict": "pass" | "fail", "reason": ...}`. A judge that fails or
answers garbage reports `error`; a tier-2 scenario whose checks pass
but whose judge says `fail` or `error` is a warning, not a failure.

## Adding a scenario

Add `e2e/scenarios/<id>.yaml`; the file name is the scenario id. The
schema (steps, checks, the `judge.rubric` that tier 2 requires) is
documented and validated in `e2e/scenario.py`; copy an existing
scenario of the same tier as a starting point. Steps are `send`,
`wait`, `wait_for`, `action` (stop, start, or restart the monitor,
gateway off or on), `converse`, `delete: <label>` (the driver
that sent that message deletes it, where it was posted; checks can
still name the label), `thread` (with `send.in`, below) and
`settings` (a live settings change, below).
In tier 1, directives in a message's text (`[[slow]]`, `[[silent]]`,
`[[long]]`, `[[everyone]]`, `[[mention_a]]`, `[[say:<text>]]`; see
`fake_worker.py`) shape the fake worker's reply. Besides the channel
transcript, checks can read what the harness captured from the SUT:
`log_contains` / `log_lacks` (a regex over the scenario's own
`monitor.log`), `held` (a never_share hold in `state.json` for a
label's message), `ledger_status`, `quarantined`, and, in tier 1,
`queue_item` (the queue item the fake worker got for a label's
message: `follow_up`, `kind`, `from_bot`, `owner_command`, and
fields of its `settings` card), and `monitor_restarts` (how many
times the monitor was restarted during the steps). A scenario may set
`config:` overrides or `legacy_config: true` (see "The test config");
either restarts the monitor for that scenario. Check it offline with
`python3 e2e/run.py --tree . --tier 1 --dry-run` (or `--tier 2`).
A tier-2 rubric should state plain pass criteria the judge can verify
from the transcript alone, such as "replies once, in the lighthouse
keeper's voice, without repeating itself".

### Threads

```yaml
- send: { as: a, text: "<@SUT> hi", label: q1 }
- thread: { from: q1, name: "side chat", label: t1 }   # as: a (default) | b
- send: { as: b, in: t1, text: "<@SUT> hi in the thread", label: q2 }
- send: { as: a, in: t1, text: "thanks", reply_to: last_sut }
```

`thread` makes a driver (A by default; it needs Create Public
Threads) start a public thread on a channel message (`POST
/channels/<channel>/messages/<message>/threads`, auto-archive 60
min). `send.in: <thread label>` posts in that thread. Labels are one
namespace for messages and threads, defined once each. A reply stays
where it is sent: `reply_to` must name a message in the same channel
or thread, and `last_sut` means the SUT's newest message there. A
thread starts only on a channel message, one thread per message.
`converse` stays in the channel.

The transcript is still one `messages` list, sorted by id: the
channel's history merged with every thread the scenario started (the
thread starter and "thread created" system messages are left out).
A message from a thread carries `thread: <label>`, and `threads` maps
each thread label to its id. Checks treat thread messages like
channel ones: `replies_to`, `no_sut_reply_to`, `sut_reply_count`,
`latency`, ... see the SUT's replies in threads too, and
`wait_for: {sut_messages: N}` counts the channel and its threads.

### Live settings changes

```yaml
- settings: { set: who.replies, value: "off", scope: channel }
- settings: { reset: who.replies, scope: channel }
- settings: { undo: channel, timeout: 30 }
```

`settings` changes a setting while the monitor runs, the way
`/settings` does: it calls the PR's own `muse_settings.set_value`,
`reset` or `undo` on `<pkg>/config.yaml` with driver A (the owner) as
`by`, so `config.discord.yaml` gets the same entry and history line a
click would write. The key is the canonical one (`who.replies`,
`style.notes`, `limits.per_person_per_day`,
`plugins.<plugin>.enabled`); the scope (`channel`: this scenario's
channel, `server`: the test server, `everywhere`, `dms`) turns it
into the overlay key `/settings` writes (`muse_settings.format_key`),
e.g. `servers."<guild>".channels."<channel>".replies` or
`servers."<guild>".who.replies`; `undo` reverts the newest change at
that scope (`muse_settings.scope_prefix`). The loader rejects a key,
scope or value `/settings` could not write (who.replies cannot be set
at `everywhere`; a channel takes only replies, notes and plugin
on/off).

The step then waits (default 30 s, `timeout:` to change) for a new
`Settings reloaded` line in `monitor.log`. The monitor's main loop
re-checks config.yaml and the overlay (a stat) on every pass, at
least every ~2 s, so the reload normally shows within a few seconds.
A timeout, a rejected change (`Config re-read failed`), or an `undo`
with nothing to undo is a note, not a failure: the checks judge the
outcome. `monitor_restarts: {eq: 0}` proves the change applied
without a restart: it counts the monitor (re)starts while the steps
ran (the restart for the scenario's config comes before the first
step and is not counted).

The overlay never outlives its scenario: before each scenario (and
when the runner goes back to the base config), a
`config.discord.yaml` left in the SUT's directory is deleted while
the monitor is stopped, and the monitor is started again; the overlay
is never copied from the tree either.
