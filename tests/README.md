# Offline test suites

Offline tests for the package itself: `monitor.py`, `presence.py`,
`watchdog.sh`, the hooks, `bin/`, `muse_settings.py` and
`settings_ui.py`. No Discord, no network: every API call is stubbed,
the gateway is a fake `websockets` package, and every file the code
under test writes goes to a temp dir. They run in CI on every push and
pull request (the `Offline suites` job in `.github/workflows/ci.yml`).

The end-to-end tests against a real Discord server live in `e2e/`, with
their own offline unit tests in `e2e/tests/`.

## Running

```bash
bash tests/run_all.sh                    # every suite, against this checkout
bash tests/run_all.sh settings locking   # some suites
bash tests/run_all.sh /path/to/tree      # another tree (a dir with monitor.py)
bash tests/run_all.sh --skip-slow        # leave out the suites marked slow
bash tests/run_all.sh -j 8               # 8 suites at a time (default 4)
```

It prints one line per suite (tests, seconds, result), then the full
log of each suite that failed, and exits non-zero on any failure.

One suite, or one test, directly:

```bash
python3 tests/settings/test_settings.py
python3 tests/settings/test_settings.py -k NeverShare
MUT_DIR=/path/to/tree python3 tests/locking/test_locking.py
```

`MUT_DIR` is the package tree under test; it defaults to this checkout.
Set `MUSE_TESTS_KEEP_TMP=1` to keep the temp dirs for a look afterwards.

Requirements: Python 3.10+ with PyYAML, plus `bash`, `git`, `jq` and
`unshare` (util-linux). The `websockets` package is not needed (the
suites bring a fake). The tests that run `watchdog.sh` (`reliability`,
`updatedm`, `fastrestart`) do it in a user + PID namespace, so its
`pgrep` / `pkill` only see their fakes; unprivileged user namespaces
must be allowed (on
Ubuntu 24.04: `sudo sysctl -w
kernel.apparmor_restrict_unprivileged_userns=0`). The security suite's
two video tests need `ffmpeg` and `ffprobe` and are skipped without
them.

## What we test

A test earns its place by catching a regression that would do real
harm. Keep (or add) a test for:

- wrong bot behaviour someone would notice: replying when it should
  not, not replying, the wrong place or scope, caps, ignore rules;
- security: prompt-injection paths, owner-DM nonce binding, permission
  checks (one test per distinct attack; most came from adversarial
  reviews);
- data loss or corruption: the config overlay, locking, crash recovery,
  atomic writes;
- crashes, hangs and wedged loops;
- Discord rejecting a payload: component and character limits,
  custom_id length (the `settingsui` crawler checks every page).

Do not add tests of styling, wording, copy, formatting, emoji, labels
or the order of text (assert what happened, not how it was said: a
refusal is "nothing was written", not its sentence); snapshot or golden
tests of UI copy; near-duplicates of a path already tested (extend a
table-driven test instead); what another suite already covers; trivial
helpers, getters and argument parsing; or log lines, unless the line is
the behaviour (an owner alert, the watchdog's markers). Prefer shortened
intervals or a fast clock over real sleeps.

## The suites

| Suite | Covers |
| --- | --- |
| `config` | Config resolution: defaults, nulls, zeros honored or clamped, out-of-range values; the removed bare-name trigger never firing (`test_name_trigger.py`). |
| `security` | Generated files bound to their queue item (no attachment leaks across channels), `bin/discord-api.py` file and id gating, safe media fetches (host allowlist, redirects, private addresses), injection quarantine for documents and reply context, `allowed_mentions`, ffmpeg input whitelists, generated-file housekeeping. |
| `locking` | Worker appends during a slow send, concurrent appends sent once, no queue resurrection, atomic writes, malformed and corrupt outboxes, crash leftovers, typing, burst windows; the wake hook's item leases (`test_hook.py`); `bin/outbox-append.py` (`test_outbox_append.py`). |
| `reliability` | Token rejection, the burst ledger, gateway gap repair, discovery, reply references and send nonces, the gateway loops (`test_units.py`); `main()` boot and REST 401/429 runs (`test_main.py`); `watchdog.sh` liveness, restarts and alerts (`test_watchdog.py`). |
| `fastloop` (slow) | REST work (polls, sweeps, discovery, hung reads) never blocks the main loop; an outbox written mid-wait is sent promptly. |
| `updatedm` (slow) | Update notices: watchdog version fields from release tags (a local bare upstream), the owner DM with buttons, the clicks, `hooks/update-watch.sh`, the `UPDATING.md` commands. |
| `fastrestart` (slow) | The wake hook restarting a dead monitor (fast, silent, once, with backoff; never after a token rejection) and the `watchdog.sh` run lock, against fake processes. |
| `settings` | `muse_settings`: invalid files refused, scopes, the Discord overlay and its conflict rule, old-layout loading and migration, live reload, what the per-message card allows, the `never_share` backstop, who-rules, `testing.treat_as_human`, per-scope caps, follow-ups, image and video generation per scope (old `plugins.media` keys and overlays included), a minimal plugin loading and its hook running, the shell shim. |
| `settingsengine` | The settings engine: where writes land, what is risky (asks first), grouped writes and Undo, the no-op guard, the server-admin rule (quieter only, never past the owners; with a fuzz), `who.dms_from` and the monitor's DM gate, presets, pause, `bin/settings.py`, the review findings. |
| `settingsplumb` | The `/settings` plumbing in `monitor.py`: registering the commands within Discord's limits, interaction routing, the 3 s deadline under a slow main loop, `SettingsCtx` writes under the lock, thread safety, the stash, outcomes kept to their place, the admin-change digest, the join notice's Configure button, a missing `settings_ui.py`. |
| `presencelive` | `presence.py` following settings changes live on the open gateway session: one Presence Update per change, rate limiting, invalid edits, disabling, heartbeats. |
| `settingsnl` | Settings requests (`settings_nl.py`): who may change settings by DM (owners' own DMs only; forged, replayed, forwarded and stale actions refused), meaning failures (all or nothing), apply / confirm / cancel / expiry / stale cards / Undo, the lockout guard, retries and restarts, notices for `bin/settings.py` changes, the owner alert, the card's places; `bin/settings.py places`. |
| `ownerdm` | Owner DMs bound to their wake: a worker woken by someone else's message cannot answer, silence or post into an owner's DM without its `owner_dm_nonce` (one test per attack); the legit path, retries and crash resends; nonce lifetime; the wake hook; `bin/discord-api.py` in an owner's DM channel; the review regressions. |
| `monitorfixes` | Daily-cap refusals dropped at once; declined messages never answered later by the sweep; new threads read from their start; Discord system notices never trigger. |
| `settingsui` (slow) | `settings_ui.py`, the `/settings` pages: a crawl from every entry point checking every response against Discord's limits and for names that ping, mention or link (`crawl.py`, `test_crawl.py`, `test_crawl_hostile.py`); forged custom ids, stash tokens and a fuzz (`test_security.py`); the review findings (`test_review.py`); walkthroughs found by custom_id, never by label (`test_walkthroughs.py`). No test reads the pages' words. |
| `telemetry` | Usage stats (`telemetry.py`): after a realistic flow, no id, name, message content, path or error message anywhere in a report (and the Worker's `validate.js` accepts it, when `node` is installed); off by setting or by env sends nothing, and off days are never sent later; a hanging, failing or unreachable endpoint never blocks the loop and is tried again the next day only; one send per day; nothing sent before the owners' one-time notice is 24 h old, or while it cannot be delivered. The Worker's own tests: `cd telemetry/worker && node --test`. |
| `settingspreview` | `dev/settings-preview/serve.py`, the local `/settings` design preview: serves its page, answers `/settings` and a click, local requests only, picks up edits to `settings_ui.py` without a restart. |

To see the `/settings` pages as Discord draws them and click through
them without a bot, run `python3 dev/settings-preview/serve.py` (see
`dev/settings-preview/README.md`); it uses the `settingsui` fixtures.

The slow suites are marked by a `SLOW` file in their directory (its
text says why). They take 10 to 15 s each, the rest a few seconds;
`run_all.sh` starts the slow ones first, and the whole run takes about
35 s at 4 suites at a time.

## Layout

```text
tests/
  run_all.sh            the runner
  _lib/                 shared by the suites (not a suite)
    common.py           MUT_DIR, temp dirs, file redirection, git isolation
    monitor_harness.py  load monitor.py / presence.py with the API stubbed
    settings_harness.py the same, for the settings suites
    fakews/             a scripted fake `websockets` package
    fake_proc.py        stands in for monitor.py / presence.py
    hook_runtime.sh     stand-ins for the hatch hook runtime functions
  <suite>/test_*.py     one or more test files per suite
```

## Adding a suite

1. Make `tests/<name>/` with one or more `test_*.py` files. `run_all.sh`
   finds every `tests/*/test_*.py` (directories starting with `_` are
   skipped), so nothing needs registering.
2. Each file must run on its own as `MUT_DIR=<tree> python3
   tests/<name>/test_<x>.py`, use `unittest` (the runner counts the
   `Ran N tests` line), and exit non-zero on failure.
3. Put `_lib` on the path and take the tree from it, never from a
   hardcoded path:

   ```python
   sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
       os.path.abspath(__file__))), "_lib"))
   from common import MUT_DIR, scratch  # noqa: E402
   ```

4. Never write into the tree under test or into `tests/`: make temp
   dirs with `scratch()` (removed at exit), and call
   `redirect_files(mod, tmp)` after importing `monitor.py` yourself
   (`monitor_harness.load_monitor` and `settings_harness.load_monitor`
   already do). No network: stub `api_get` / `api_post`, and use
   `_lib/fakews` for the gateway.
5. Follow "What we test" above. If the suite takes more than about
   10 s, add a `SLOW` file saying why; keep the whole run under a
   minute.
6. Check it: `bash tests/run_all.sh <name>`, `uvx ruff check --select
   E9,F tests`, and `shellcheck` on any shell scripts.
