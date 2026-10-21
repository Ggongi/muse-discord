# UPDATING.md: updating muse-discord

Like SETUP.md, this guide is written to be executed BY a Muse working
with the owner. The goal: the chosen release running, the owner's
customizations intact, and nothing of theirs discarded without their
say-so.

Commands run from `<package dir>`. Never print the bot token.

An update never repeats SETUP.md: `config.yaml`, `persona.md` and the
token stay exactly as they are, and the owner is not asked to do
anything in Discord. Run `bin/connect-discord.py` again only when the
token was rejected (TROUBLESHOOTING.md).

## What belongs to the owner

Updates never need to overwrite these, so never discard them:

- `config.yaml` (untracked), its backups, `config.discord.yaml`
  (settings changed through the bot, if any), and the token file.
- `persona.md`: tracked as a placeholder, written during setup.
  Upstream never changes it, so it never conflicts.
- Custom plugins: their own folders under `plugins/` (untracked).
- `worker-prompt.md` (rendered from the template, untracked) and the
  hooks registered in the agent runtime.
- Runtime state: `state.json`, `mention_ledger.json`, the queue and
  outbox, logs.

Any other tracked file the owner (or a Muse on their behalf) edited is
a **local modification**, handled in step 3b. When the owner wants a
new customization, steer it to `config.yaml`, `persona.md`, or a
plugin where possible: those never conflict with an update.

## Step 1: Pick the release, and look before touching anything

Updates come as releases: semver tags `vX.Y.Z` on origin (the
CHANGELOG has one `## vX.Y.Z (date): title` entry per release). Set
`TAG` to the release to install:

- **Started from the bot's update DM**: the release the owner chose,
  the `version` in the wake payload (also in `update_request.json`).
- **By hand** ("update to the latest release"): the newest release
  tag on origin, skipping pre-releases such as `v1.2.0-rc.1`:

  ```bash
  git ls-remote --tags --refs origin 'v*' | sed 's#.*refs/tags/##' \
    | grep -E '^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$' \
    | sort -V | tail -1
  ```

  If it prints nothing, there is no release yet: tell the owner, and
  stop (or see "Tracking main instead" below).

Then:

```bash
TAG=vX.Y.Z                           # the release picked above
git fetch --no-tags origin tag "$TAG"
OLD=$(git rev-parse HEAD)            # keep this for steps 4 and 6
git describe --tags --match 'v[0-9]*' --abbrev=0 2>/dev/null \
  || echo unreleased                 # the version running now
git log --oneline HEAD.."$TAG"       # what is new
git diff HEAD "$TAG" -- CHANGELOG.md
git status --short --untracked-files=no
git log --oneline "$TAG"..HEAD       # local commits, if any
```

If `git merge-base --is-ancestor "$TAG" HEAD` succeeds, the install
already has that release: tell the owner it is up to date and stop.
Otherwise read the CHANGELOG diff and note every **Action needed**
item; they are handled in step 4.

## Step 2: Decide which path

- **Clean**: `git status` lists nothing (or only `persona.md`) and
  there are no local commits. Go to 3a.
- **Customized**: anything else. Go to 3b.

## Step 3a: Clean install

```bash
git merge --ff-only "$TAG"
```

## Step 3b: Customized install

1. Show the owner what they changed and what the update touches:

   ```bash
   git diff --stat                      # their uncommitted changes
   git diff --stat HEAD "$TAG"          # the update
   ```

   Files in both lists may conflict. Explain it in plain words and
   get an explicit OK before going on.

2. Make a safety net. Nothing below is undone without it:

   ```bash
   git branch "backup/pre-update-$(date +%Y%m%d-%H%M)"
   git diff > "$HOME/muse-discord-local-changes-$(date +%Y%m%d-%H%M).patch"
   ```

3. Apply the update on top of their changes:

   ```bash
   git stash push -m "local changes before update"
   git merge --ff-only "$TAG"
   # or, when there are local commits (a merge commit):
   # git merge --no-edit "$TAG"
   git stash pop
   ```

   Skip the two stash lines when there are no uncommitted changes.
   A merge commit needs a git identity; if git asks for one, set it
   for this repo only (`git config user.name`, `git config
   user.email`) with the owner's OK.

4. If anything conflicts, stop and go through it file by file with
   the owner: keep what their change was for, and take the release's
   fixes. When a conflict is not obviously resolvable, ask. To back
   out entirely: `git merge --abort` if a merge is in progress, then
   `git reset --keep "$OLD"` and `git stash pop`.

Never run `git reset --hard`, `git checkout -- <file>`, `git clean`,
or `git stash drop` on the owner's work unless they ask for exactly
that.

### Tracking main instead

An install that wants every commit on main, not just releases, uses
`@{upstream}` where the steps say `"$TAG"`: in step 1, `git fetch`
instead of fetching a tag; in step 3, `git pull --ff-only` (clean) or
`git pull --no-rebase` (local commits). The update DM still offers
only releases, and skips any release HEAD already contains.

## Step 4: Bring the rest of the install along

- **Dependencies**: run `bash bin/install.sh` if the release has it
  (it is quick when nothing changed); otherwise, if `git diff "$OLD"
  HEAD -- requirements.txt` shows changes, `pip install -r
  requirements.txt`.
- **Config**: `git diff "$OLD" HEAD -- config.example.yaml` shows new
  settings. Every new setting has a safe default in the code, so an
  older `config.yaml` keeps working. Mention each new setting to the
  owner and add only what they want to `config.yaml`. Then run
  `python3 bin/settings.py validate` (if the release has it) and fix
  what it reports before the restart.
- **Settings layout** (once, when updating from a release without
  `bin/settings.py`): see "Converting to the new settings layout"
  below.
- **Action needed** items from the CHANGELOG: walk through each one
  with the owner and do the ones they agree to.
- **Worker prompt**: render it again, with the same `--media` as
  before (what this runtime can generate): `python3
  bin/render-worker-prompt.py --media <none|images|video|images,video>`.
  Once, on an install whose wake hook still carries the whole prompt
  inline (from before the renderer), replace that wake prompt with the
  short one in SETUP.md step 5's table, which points at
  `worker-prompt.md`; from then on, re-rendering is enough. If the
  owner had asked for changes inside a seam, apply them to
  `worker-prompt.md` again after rendering.
- **Wake hook**: if `hooks/mention-watch.sh` changed and the runtime
  registered a copy rather than the file itself, update the copy.
- **Watchdog hook**: if the runtime has no hook for
  `hooks/watchdog-hook.sh` yet, register it as SETUP.md step 5's table
  says (every 10 minutes, main chat), and remove the old scheduler
  entry for `watchdog.sh` if it had one.
- **Settings guide**: if the release has `SETTINGS.md` and your memory
  has no note about it yet, save one: "To change muse-discord's
  settings, read `<package dir>/SETTINGS.md` first and follow it."
  Then follow that file whenever the owner asks you to change how the
  bot behaves.
- **Update hook**: if the runtime has no hook for
  `hooks/update-watch.sh` yet, register it as SETUP.md step 5's table
  says (main chat, every 60 s, its wake prompt). From then on
  the owner can start an update from the bot's update DM. If the
  script changed and the runtime registered a copy, update the copy.

### Converting to the new settings layout

This release reorganized `config.yaml` (see `config.example.yaml`).
An old-layout file keeps working unchanged: the bot reads it, maps
every old setting to its new name, and logs one line saying so. So
converting is optional, and never done without the owner.

1. Tell the owner, in plain words: the bot works as before; the file
   can be converted to the new, shorter layout, which is easier to
   read and needed for per-server and per-channel settings; their
   current file is kept as a backup. One thing does change either
   way: in servers the bot now also answers the person it just
   replied to when they keep talking within about 2 minutes. If they
   prefer the old behavior, after converting, set
   `all_servers.who.replies: when-addressed`.
2. Only with their OK, convert:

   ```bash
   python3 bin/settings.py migrate
   ```

   It writes the new `config.yaml`, keeps the old one as a
   timestamped backup next to it, and prints any difference in
   effective behavior. The only expected difference is the
   conversational replies above; it refuses to write if anything
   else would change.

   The backup is `config.yaml.bak-<UTC time>`. migrate does not
   prompt, so ask the owner before running it; `--dry-run` shows
   what it would write without writing.

3. Show the owner the new `config.yaml` and what migrate printed,
   then run `python3 bin/settings.py validate`.
4. If the owner wants to undo it, copy the backup back over
   `config.yaml`; the old layout still works.

Render the worker prompt for this release (step 4, "Worker prompt"):
the new SETTINGS section is how the Muse learns each place's reply
length, house rules, and how much of the owner's memory it may use.

## Step 5: Restart

```bash
kill "$(cat monitor.pid)"
pkill -f "^python3 presence.py"
bash watchdog.sh
# expected: "OK monitor restarted" (and "OK presence restarted" if
# presence is enabled), or "OK monitor alive" when the wake hook
# restarted the monitor first
```

## Step 6: Check, then report

1. Run `python3 bin/doctor.py --no-dm` and fix what it reports (or,
   from a release without it, have the owner @mention the bot and
   expect a reply).
2. Tell the owner, briefly: the version now running (`$TAG`;
   `git describe --tags` shows it, plus any local commits on top),
   what changed in a few sentences, which Action needed items were
   done, and anything still waiting on them.

If `update_request.json` exists (the owner started this update from
the bot's update DM), delete it now. Delete it too if you stop at an
earlier step.

If the new version misbehaves and the owner wants to roll back:
`git reset --keep "$OLD"`, then restart as in step 5.

## For maintainers

- Never edit `persona.md` upstream: it is the owner's file. The default
  persona setup writes lives in `bin/connect-discord.py`
  (DEFAULT_PERSONA); it replaces only the shipped placeholder, which
  its SHIPPED_PERSONAS hash recognizes.
- Every user-visible change gets a CHANGELOG entry, with an **Action
  needed** list whenever an existing install must do something.
- Installs are offered releases: semver tags `vX.Y.Z` (pre-release
  tags are ignored), each with a `## vX.Y.Z (YYYY-MM-DD): Short
  title` CHANGELOG entry, newest first. The update DM lists those
  titles and flags Action needed from those entries, so a release's
  changes go under its version heading. The first release, `v1.0.0`,
  is tagged once the update DM ships; installs then on main are
  offered it unless their HEAD already contains it.
- Every new config setting gets a built-in default in the settings
  code (`muse_settings.py`) so existing `config.yaml` files keep
  working, and every renamed one keeps mapping from its old name.
