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
python3 tests/settings/test_settings.py -k never_share
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

## The suites

| Suite | Covers |
| --- | --- |
| `config` | Config resolution: defaults for missing keys and explicit nulls, zeros honored or clamped, out-of-range and garbage values and their warnings; `plugin.yaml` defaults, overrides and `per_guild` merges; the welcome plugin's daily cap and stale joins. |
| `security` | Generated files bound to their queue item (no attachment leaks across channels), `bin/discord-api.py` file and id gating, safe media fetches (host allowlist, redirects, private addresses), injection quarantine for documents and reply context, `allowed_mentions`, ffmpeg input whitelists, generated-file housekeeping. |
| `locking` | Worker appends during a slow send, concurrent appends sent once, no queue resurrection, the reply lock, typing, burst coalescing and early flush, the latency line, `bin/outbox-append.py` validation; the wake hook's item leases (`test_hook.py`). |
| `reliability` (slow) | Token rejection, the burst ledger, gateway gap repair, discovery, outbox sends and nonces, the presence gateway loop (`test_units.py`); `main()` boot and REST 401/429 runs (`test_main.py`); `watchdog.sh` liveness, restarts and alerts (`test_watchdog.py`). |
| `nametrigger` | The removed bare-name trigger: one removal warning for an old config, a bare name never triggers, leftover `name_drop` bursts are dropped. |
| `fastloop` (slow) | REST work (polls, sweeps, discovery, slow sends, hung reads) never blocks the main loop; DM and server burst timing; an outbox written mid-wait is sent promptly. |
| `updatedm` (slow) | Update notices: watchdog version fields from release tags (a local bare upstream), the owner DM with buttons, `INTERACTION_CREATE` handling, `hooks/update-watch.sh`, the `UPDATING.md` commands. |
| `fastrestart` (slow) | The wake hook restarting a dead monitor (fast, silent, once, with backoff; never after a token rejection) and the `watchdog.sh` run lock, against fake processes. |
| `settings` | `muse_settings`: schema validation, scopes, the Discord overlay and its conflict rule, old-layout migration, live reload, the per-message card, the `never_share` backstop, who-rules, per-scope caps, follow-ups, plugins, the shell and presence integration. |
| `settingsui` | `settings_ui.py`, the `/settings` pages: fake interactions from owners, server admins and strangers, in DMs and servers, writes through `muse_settings`, every response checked against Discord's limits. |
| `settingsplumb` | The `/settings` plumbing in `monitor.py`: command registration, interaction routing, the 3 s deadline under a slow main loop, `SettingsCtx` writes, reset, undo and reload, the admin-change digest, the join notice's Configure button, a missing `settings_ui.py`. |
| `presencelive` | `presence.py` following settings changes live on the open gateway session: one Presence Update per change, clearing, rate limiting, invalid edits, disabling, resumes, heartbeats. |
| `monitorfixes` | Daily-cap refusals dropped at once; declined direct messages ledgered as skipped; new threads seeded at their own id; Discord system notices (pins, thread starts) never trigger. |

The slow suites are marked by a `SLOW` file in their directory (its
text says why). They take 50 s to 2 min each, the rest a few seconds;
`run_all.sh` starts the slow ones first, and the whole run takes about
2 minutes at 4 suites at a time.

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
5. If the suite takes more than about 30 s, add a `SLOW` file saying
   why, and keep the whole run under 5 minutes in CI.
6. Check it: `bash tests/run_all.sh <name>`, `uvx ruff check --select
   E9,F tests`, and `shellcheck` on any shell scripts.
