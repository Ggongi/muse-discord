#!/usr/bin/env bash
# muse-discord watchdog hook: the scheduled watchdog run, as a Muse hook.
# Register it to run every 10 minutes, delivered to the owner's MAIN
# chat (SETUP.md, step 5). Each run executes watchdog.sh (which keeps
# the monitor and presence alive and checks for updates) and:
#   - stays silent when everything is OK;
#   - wakes the main chat with the ALERT lines when the watchdog raises
#     one (it alerts once per new failure, never every run).
# UPDATE lines are not passed on: the bot already DMs the owners about
# a new release, with an Update now button (updates.notify).
#
# Zero tokens on a healthy run; never touches the token itself. Like the
# other hooks, it sources $HATCH_HOOK_RUNTIME for silent/wake/log.
# A runtime without hooks can run watchdog.sh from any scheduler (cron,
# a scheduled job) every ~10 minutes instead; its ALERT lines then need
# a reader.
set -euo pipefail
# shellcheck source=/dev/null
source "$HATCH_HOOK_RUNTIME"

PKG_DIR="${MUSE_DISCORD_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# Proof for bin/doctor.py that this hook runs (never fails the hook).
touch "$PKG_DIR/.seen-watchdog-hook" 2>/dev/null || true

out=$(bash "$PKG_DIR/watchdog.sh" 2>&1) || true
alerts=$(printf '%s\n' "$out" | grep '^ALERT:' || true)
if [ -z "$alerts" ]; then
    silent "watchdog OK" '{}'
fi
payload=$(jq -cn --arg a "$alerts" --arg dir "$PKG_DIR" \
    '{alerts: ($a | split("\n")), package_dir: $dir,
      guide: ($dir + "/TROUBLESHOOTING.md")}')
wake "muse-discord needs attention" "$payload"
