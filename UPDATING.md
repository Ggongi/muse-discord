# UPDATING.md: updating muse-discord

Like SETUP.md, this guide is written to be executed BY a Muse working
with the owner. The goal: the newest version running, the owner's
customizations intact, and nothing of theirs discarded without their
say-so.

Commands run from `<package dir>`. Never print the bot token.

## What belongs to the owner

Updates never need to overwrite these, so never discard them:

- `config.yaml` (untracked) and the token file.
- `persona.md`: tracked as a placeholder, rewritten during setup.
  Upstream never changes it, so it never conflicts.
- Custom plugins: their own folders under `plugins/` (untracked).
- The installed worker prompt, which lives in the agent runtime, not
  in this repo.
- Runtime state: `state.json`, `mention_ledger.json`, the queue and
  outbox, logs.

Any other tracked file the owner (or a Muse on their behalf) edited is
a **local modification**, handled in step 3b. When the owner wants a
new customization, steer it to `config.yaml`, `persona.md`, or a
plugin where possible: those never conflict with an update.

## Step 1: Look before touching anything

```bash
git fetch
OLD=$(git rev-parse HEAD)            # keep this for steps 4 and 6
git log --oneline HEAD..@{upstream}  # what is new
git diff HEAD @{upstream} -- CHANGELOG.md
git status --short --untracked-files=no
git log --oneline @{upstream}..HEAD  # local commits, if any
```

If nothing is new upstream, tell the owner it is up to date and stop.
Otherwise read the CHANGELOG diff and note every **Action needed**
item; they are handled in step 4.

## Step 2: Decide which path

- **Clean**: `git status` lists nothing (or only `persona.md`) and
  there are no local commits. Go to 3a.
- **Customized**: anything else. Go to 3b.

## Step 3a: Clean install

```bash
git pull --ff-only
```

## Step 3b: Customized install

1. Show the owner what they changed and what the update touches:

   ```bash
   git diff --stat                      # their uncommitted changes
   git diff --stat HEAD @{upstream}     # the update
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
   git pull --rebase          # replays any local commits on top
   git stash pop
   ```

4. If anything conflicts, go through it file by file with the owner:
   keep what their change was for, and take upstream's fixes. When a
   conflict is not obviously resolvable, stop and ask. To back out
   entirely: `git rebase --abort` if a rebase is in progress, then
   `git reset --keep "$OLD"` and `git stash pop`.

Never run `git reset --hard`, `git checkout -- <file>`, `git clean`,
or `git stash drop` on the owner's work unless they ask for exactly
that.

## Step 4: Bring the rest of the install along

- **Dependencies**: if `git diff "$OLD" HEAD -- requirements.txt`
  shows changes, run `pip install -r requirements.txt`.
- **Config**: `git diff "$OLD" HEAD -- config.example.yaml` shows new
  settings. Every new setting has a safe default in the code, so an
  older `config.yaml` keeps working. Mention each new setting to the
  owner and add only what they want to `config.yaml`.
- **Action needed** items from the CHANGELOG: walk through each one
  with the owner and do the ones they agree to.
- **Worker prompt**: `git diff "$OLD" HEAD --
  worker-prompt.template.md` shows prompt changes. Apply them to the
  installed prompt in the agent runtime, keeping what the owner put
  inside the seams (persona, generation policy, judgment tweaks).
- **Wake hook**: if `hooks/mention-watch.sh` changed and the runtime
  registered a copy rather than the file itself, update the copy.

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

1. Run smoke test 1 from SETUP.md (owner @mentions the bot; expect a
   reply).
2. Tell the owner, briefly: the version now running
   (`git log -1 --format='%h %cs'`), what changed in a few sentences,
   which Action needed items were done, and anything still waiting
   on them.

If the new version misbehaves and the owner wants to roll back:
`git reset --keep "$OLD"`, then restart as in step 5.

## For maintainers

- Never edit `persona.md` upstream: it is the owner's file.
- Every user-visible change gets a CHANGELOG entry, with an **Action
  needed** list whenever an existing install must do something.
- Every new config setting gets a default in `DEFAULT_CONFIG`
  (monitor.py) so existing `config.yaml` files keep working.
