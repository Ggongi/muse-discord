#!/usr/bin/env bash
# muse-discord mention watcher (worker wake hook).
# Polls the local reply queue written by the Discord monitor (zero tokens:
# this script never touches the network and has no secrets). Wakes the
# reply worker ONLY when there is queued work that no worker has
# recently been woken for.
#
# Overlap protection uses hook state (not a lock file the worker must
# remember to delete): each queue item gets a lease when a wake hands it
# to a worker, and a wake carries ONLY the items no live lease covers.
# A message arriving while a worker is busy (say, generating a video)
# wakes a second worker for just that message, never for the first
# worker's items, so nothing is drafted, generated, or welcomed twice.
# An item still open when its lease runs out (LEASE_SECS, long enough
# for a slow generation) is handed out again, in case its worker died.
# Items the worker already answered (they have an outbox entry, waiting
# for the monitor to send them) are never handed out. The worker appends
# each reply as soon as it is drafted, so part of a wake can be answered
# while the rest is still in progress; those keep their lease.
#
# The payload carries the items plus the persona text, so the worker's
# happy path is one append per item: no reading the queue or persona
# files.
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
OUTBOX="$PKG_DIR/reply_outbox.json"
SENDING="$PKG_DIR/reply_outbox.processing.json"
LEASE_SECS=600

# No queue file yet -> nothing to do
if [ ! -f "$QUEUE" ]; then
    silent "no queue file" '{}'
fi

# Message ids the worker already answered, read BEFORE the queue: the
# monitor removes an answered item from the queue before it drops the
# item's outbox entry, so this order never sees one without the other.
answered='[]'
for f in "$OUTBOX" "$SENDING"; do
    [ -f "$f" ] || continue
    ids=$(jq -c '[.[]? | objects | (.item, .reply_to) | strings]' "$f" \
        2>/dev/null) || ids='[]'
    answered=$(jq -cn --argjson a "$answered" --argjson b "$ids" '$a + $b')
done

# Open items: queued and not answered (a burst may be answered under any
# of its fragments).
open=$(jq -c --argjson answered "$answered" '[.[]? | objects
    | select(.message_id != null)
    | select(([.message_id] + [(.burst_fragments // [])[]? | .message_id])
        as $ids | ($ids - $answered | length) == ($ids | length))]' \
    "$QUEUE" 2>/dev/null) || open='[]'
count=$(printf '%s' "$open" | jq 'length')
if [ "$count" -eq 0 ]; then
    silent "queue empty" '{}'
fi

now=$(date +%s)
woken=$(hook_state_get | jq -c '.woken // {}' 2>/dev/null) || woken='{}'

# Due: open items with no live lease.
due=$(printf '%s' "$open" | jq -c --argjson w "$woken" --argjson now "$now" \
    --argjson lease "$LEASE_SECS" \
    '[.[] | select((($w[.message_id] // 0) + $lease) <= $now)]')
ndue=$(printf '%s' "$due" | jq 'length')
if [ "$ndue" -eq 0 ]; then
    silent "a worker already holds every open item" \
        '{"open":"'"$count"'"}'
fi
expired=$(printf '%s' "$due" | jq --argjson w "$woken" \
    '[.[] | select($w[.message_id] != null)] | length')
if [ "$expired" -gt 0 ]; then
    log "re-waking: item lease expired" '{"items":"'"$expired"'"}'
fi

# Leases: keep the ones of items still queued (answered ones too, in case
# their outbox entry is briefly unreadable), stamp the items this wake
# hands out.
# hook_state_set is a no-op on dry runs, so testing never consumes a detection.
queued=$(jq -c '[.[]? | objects | .message_id | strings]' "$QUEUE" \
    2>/dev/null) || queued='[]'
leases=$(jq -cn --argjson ids "$queued" --argjson due "$due" \
    --argjson w "$woken" --argjson now "$now" '
    {woken: (($w | with_entries(select(.key as $k | $ids | index($k))))
        + ($due | map({key: .message_id, value: $now}) | from_entries))}')
hook_state_set "$leases"

# The persona text (persona.file in config.yaml, relative to the package
# dir; default persona.md), so the worker need not read it. null when
# unreadable: the worker then reads the file itself.
CONFIG="${MUSE_DISCORD_CONFIG:-$PKG_DIR/config.yaml}"
persona_file=$(python3 - "$CONFIG" "$PKG_DIR" 2>/dev/null <<'PY'
import os
import sys
try:
    import yaml
    with open(sys.argv[1]) as f:
        name = ((yaml.safe_load(f) or {}).get("persona") or {}).get("file")
except Exception:
    name = None
print(os.path.join(sys.argv[2], os.path.expanduser(name or "persona.md")))
PY
) || persona_file="$PKG_DIR/persona.md"
if [ -r "$persona_file" ]; then
    persona_args=(--rawfile persona "$persona_file")
else
    persona_args=(--argjson persona null)
fi

payload=$(printf '%s' "$due" | jq -c "${persona_args[@]}" '{count: length, persona: $persona, items: [.[] | {message_id, channel_id, channel_name, kind, author, author_id, content, media, documents, unviewable, history, may_generate, owner_command, welcome, welcome_prompt, reply_to, burst, burst_fragments, from_bot, bot_turns, participants}]}')
wake "muse-discord mentions pending" "$payload"
