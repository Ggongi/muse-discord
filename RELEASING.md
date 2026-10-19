# Releasing muse-discord

For maintainers. A release is a semver tag on `main` plus a GitHub Release whose
notes come from CHANGELOG.md. Installs offer owners an update per release tag,
so every release reaches every owner.

## When to release

Release when `main` has enough user-facing change to be worth an owner's
attention: a feature, or a batch of fixes. A bad bug fix can go out on its own
as a patch release. Docs-only and CI-only changes don't need a release.

## Choosing the version

Tags are `vMAJOR.MINOR.PATCH`:

- **MAJOR:** updating needs something risky from the owner, or an existing
  `config.yaml`, plugin or worker prompt stops working.
- **MINOR:** new features; old configs keep working.
- **PATCH:** fixes only.

Pre-releases such as `v1.2.0-rc.1` are published as GitHub pre-releases and
ignored by installs. They use their own CHANGELOG entry if there is one, else
the upcoming `v1.2.0` entry.

## Writing the CHANGELOG entry

Add the entry at the top of CHANGELOG.md:

```markdown
## v1.2.0 (2026-11-02): Short title an owner understands

Added:

- ...

**Action needed:**

- What an existing install must do after updating.
```

The update DM shows every owner each release's title and whether it has an
**Action needed** list, so write both for owners, not for developers. Leave out
the Action needed list when there is nothing to do. UPDATING.md walks a Muse
through the list.

## Cutting the release

1. Open a PR that adds the entry (with the date you plan to release) and merge
   it.
2. On an up-to-date `main`, run `bin/release.sh 1.2.0`. It checks the tree, the
   CHANGELOG entry and that the tag is new, shows what it will do, asks for
   confirmation (`--yes` skips it), then pushes an annotated tag `v1.2.0`.

The push runs `.github/workflows/release.yml`, which publishes the GitHub
Release titled `v1.2.0: Short title` with the entry as notes. It fails, naming
the heading it expected, if the entry is missing. Re-running it (say, after a
GitHub outage) updates the existing release instead of failing. The notes come
from the tagged commit, so fix a typo by editing the release on GitHub, and in
CHANGELOG.md on `main` for the record.

## What installs see

Each install checks for new release tags at most once a day and DMs its owner
about a new one, so expect owners to hear within a day. Nothing is applied until
the owner asks their Muse to update.

## Fixing a bad release

Ship a new patch release with the fix. Never move or delete a published tag:
installs may already have it checked out, and moving it would hand different
owners different code under one version.
