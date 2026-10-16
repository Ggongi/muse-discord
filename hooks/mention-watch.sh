#!/usr/bin/env bash
# muse-discord mention watcher (worker wake hook).
# Polls the local reply queue written by the Discord monitor (zero tokens:
# this script never touches the network and has no secrets). Wakes the
# reply worker ONLY when there is queued work that no worker has
# recently been woken for.
#
# Overlap protection uses hook state (not a lock file the worker must
# remember to delete): we record the queue's content hash at each wake
# and stay silent while a recent wake covers the identical queue. A
# 5-minute backstop re-wakes in case a worker died without draining
# the queue.
#
# This script targets Muse's hook runtime (it sources
# $HATCH_HOOK_RUNTIME for the silent/wake/log/hook_state helpers and is
# registered as a hook with a prompt like worker-prompt.template.md;
# see SETUP.md). Other agent runtimes can skip the script entirely and
# simply poll reply_queue.json themselves; the queue protocol in
# PROTOCOL.md is the real contract.
set -euo pipefail
# shellcheck source=/dev/null
source "$HATCH_HOOK_RUNTIME"

# Package dir: the folder containing this hooks/ dir's parent, unless
# overridden. The queue lives at the package root.
PKG_DIR="${MUSE_DISCORD_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
QUEUE="$PKG_DIR/reply_queue.json"
REWAKE_AFTER_SECS=300

# No queue file yet -> nothing to do
if [ ! -f "$QUEUE" ]; then
    silent "no queue file" '{}'
fi

count=$(jq 'length' "$QUEUE" 2>/dev/null || echo 0)
if [ "$count" -eq 0 ]; then
    silent "queue empty" '{}'
fi

# Stable hash of the queued message IDs
qhash=$(jq -c '[.[].message_id] | sort | join(",")' "$QUEUE" | md5sum | cut -d' ' -f1)
now=$(date +%s)

state=$(hook_state_get)
last_hash=$(printf '%s' "$state" | jq -r '.last_wake_hash // empty')
last_wake=$(printf '%s' "$state" | jq -r '.last_wake_at // 0')

if [ -n "$last_hash" ] && [ "$qhash" = "$last_hash" ]; then
    age=$(( now - last_wake ))
    if [ "$age" -lt "$REWAKE_AFTER_SECS" ]; then
        silent "worker already woken for this queue" '{"age_secs":"'"$age"'"}'
    fi
    log "re-waking: queue unchanged since last wake" '{"age_secs":"'"$age"'"}'
fi

# hook_state_set is a no-op on dry runs, so testing never consumes a detection.
hook_state_set '{"last_wake_hash":"'"$qhash"'","last_wake_at":'"$now"'}'

payload=$(jq -c '{count: length, items: [.[] | {message_id, channel_id, channel_name, kind, priority, author, author_id, content, media, documents, unviewable, history, may_generate, owner_command, welcome, welcome_prompt, reply_to, burst, burst_fragments}]}' "$QUEUE")
wake "muse-discord mentions pending" "$payload"
