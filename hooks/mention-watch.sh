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
# worker's items, so nothing is drafted or generated twice.
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
# Monitor liveness: before the queue logic, each run checks that the
# monitor process exists (its monitor.pid entry, else the same pgrep
# watchdog.sh uses). When it is gone (say, the VM was replaced and every
# process died), the script starts `watchdog.sh --from-hook` fully
# detached and logs one line, so the bot is back within seconds instead
# of at the next scheduled watchdog run. That never wakes anyone and
# never touches the token: watchdog.sh does the restart and decides
# everything else. Attempts are rate limited in hook state (at once,
# then after 1, 2 and 5 minutes, then every 10 while the monitor stays
# down; the backoff resets once the monitor stays up for the current
# window), and none are made after a token rejection (TOKEN REJECTED at
# the end of monitor.log, or the watchdog's alert saying so): the
# scheduled watchdog run owns that case, so a reset token is not
# retried every few seconds. A stuck monitor (alive, stale heartbeat),
# presence, alerts and update checks stay with the scheduled run.
#
# Owner DMs are handed out alone (never in the same wake as anyone
# else's message), so a server message can never share a worker's
# context with an owner's DM. Only such a wake gets each DM's
# owner_dm_nonce, which every outbox item for an owner DM must carry
# (PROTOCOL.md, Owner DMs); an outbox entry for an owner DM without its
# nonce (one the monitor will refuse) does not count as answering it.
#
# This script targets Muse's hook runtime (it sources
# $HATCH_HOOK_RUNTIME for the silent/wake/log/hook_state helpers and is
# registered as a hook whose prompt points at the rendered
# worker-prompt.md; see SETUP.md, step 5). Other agent runtimes can
# skip the script entirely and simply poll reply_queue.json themselves;
# the queue protocol in PROTOCOL.md is the real contract.
set -euo pipefail
# shellcheck source=/dev/null
source "$HATCH_HOOK_RUNTIME"

# Package dir: the folder containing this hooks/ dir's parent, unless
# overridden. The queue lives at the package root.
PKG_DIR="${MUSE_DISCORD_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# The package's own Python (bin/install.sh may have made a .venv, and
# marks it .muse-ok once the packages import there).
if [ -x "$PKG_DIR/.venv/bin/python3" ] && [ -e "$PKG_DIR/.venv/.muse-ok" ]; then
    PATH="$PKG_DIR/.venv/bin:$PATH"
fi
# Proof for bin/doctor.py that this hook runs (never fails the hook).
touch "$PKG_DIR/.seen-mention-watch" 2>/dev/null || true
QUEUE="$PKG_DIR/reply_queue.json"
OUTBOX="$PKG_DIR/reply_outbox.json"
SENDING="$PKG_DIR/reply_outbox.processing.json"
LEASE_SECS=600

# Hook state (one JSON object): .woken holds the item leases, .restart
# the monitor-restart backoff ({next, fails}).
state=$(hook_state_get 2>/dev/null \
    | jq -c 'if type == "object" then . else {} end' 2>/dev/null) \
    || state='{}'
[ -n "$state" ] || state='{}'
printf -v now '%(%s)T' -1

# ---- Monitor liveness (zero tokens; see the header) ----
# Alive when monitor.pid names a live monitor.py (read without touching
# its flock: probing the lock could make a monitor that is starting
# right then think another one holds it), else when watchdog.sh's own
# pgrep finds one.
monitor_alive() {
    local pid="" arg1=""
    IFS= read -r pid < "$PKG_DIR/monitor.pid" 2>/dev/null || true
    if [[ $pid =~ ^[0-9]+$ ]] \
        && { IFS= read -r -d '' _ && IFS= read -r -d '' arg1; } \
            2>/dev/null < "/proc/$pid/cmdline" \
        && [[ $arg1 == *monitor.py ]]; then
        return 0
    fi
    pgrep -f '^python3 [m]onitor.py' >/dev/null 2>&1
}

# True after a token rejection: the scheduled watchdog run handles that
# (and alerts), so the hook never retries a refused token on its own.
token_rejected() {
    if tail -c 4096 "$PKG_DIR/monitor.log" 2>/dev/null \
        | grep -q "TOKEN REJECTED"; then
        return 0
    fi
    grep -qs "rejected the token" "$PKG_DIR/watchdog_state.json"
}

# Run watchdog.sh --from-hook fully detached (double fork, new session,
# every inherited descriptor closed, stdio on /dev/null): this script
# returns at once, never waits on it, and a hook-runtime timeout that
# kills this script's process tree cannot take the restart down.
start_watchdog() {
    python3 - "$PKG_DIR" >/dev/null 2>&1 <<'PYEOF'
import os
import sys
d = sys.argv[1]
if os.fork() > 0:
    os._exit(0)
os.setsid()
if os.fork() > 0:
    os._exit(0)
os.chdir(d)
null = os.open(os.devnull, os.O_RDWR)
for fd in (0, 1, 2):
    os.dup2(null, fd)
try:
    fds = [int(x) for x in os.listdir("/proc/self/fd")]
except OSError:
    fds = range(3, 1024)
for fd in fds:
    if fd > 2:
        try:
            os.close(fd)
        except OSError:
            pass
os.execvp("bash", ["bash", os.path.join(d, "watchdog.sh"), "--from-hook"])
PYEOF
}

if [ -f "$PKG_DIR/watchdog.sh" ]; then
    rs_next=0
    rs_fails=0
    if [[ $state == *'"restart"'* ]]; then
        read -r rs_next rs_fails < <(printf '%s' "$state" \
            | jq -r '.restart | "\(.next // 0) \(.fails // 0)"' \
            2>/dev/null) || true
        [[ $rs_next =~ ^[0-9]+$ ]] || rs_next=0
        [[ $rs_fails =~ ^[0-9]+$ ]] || rs_fails=0
    fi
    restart=""
    if monitor_alive; then
        # Up for a whole backoff window: reset the backoff.
        if [ "$rs_next" -gt 0 ] && [ "$now" -ge "$rs_next" ]; then
            restart=null
            log "monitor is running again" '{"attempts":"'"$rs_fails"'"}'
        fi
    elif [ "$now" -ge "$rs_next" ]; then
        if token_rejected; then
            restart="{\"next\":$((now + 600)),\"fails\":$rs_fails}"
            log "monitor down after a token rejection; leaving it to the scheduled watchdog run" '{}'
        else
            rs_fails=$((rs_fails + 1))
            case $rs_fails in
                1) wait_secs=60 ;;
                2) wait_secs=120 ;;
                3) wait_secs=300 ;;
                *) wait_secs=600 ;;
            esac
            restart="{\"next\":$((now + wait_secs)),\"fails\":$rs_fails}"
            if start_watchdog; then
                log "monitor not running; started watchdog.sh" \
                    '{"attempt":"'"$rs_fails"'","retry_after_secs":"'"$wait_secs"'"}'
            else
                log "monitor not running; could not start watchdog.sh" \
                    '{"attempt":"'"$rs_fails"'"}'
            fi
        fi
    fi
    if [ -n "$restart" ]; then
        state=$(printf '%s' "$state" | jq -c --argjson r "$restart" \
            'if $r == null then del(.restart) else .restart = $r end')
        hook_state_set "$state"
    fi
fi

# No queue file yet -> nothing to do
if [ ! -f "$QUEUE" ]; then
    silent "no queue file" '{}'
fi

# The worker's answers so far, read BEFORE the queue: the monitor removes
# an answered item from the queue before it drops the item's outbox
# entry, so this order never sees one without the other. Each is the
# ids it names and the owner-DM nonce it carries.
answered='[]'
for f in "$OUTBOX" "$SENDING"; do
    [ -f "$f" ] || continue
    ids=$(jq -c '[.[]? | objects | {ids: [(.item, .reply_to) | strings],
        nonce: ([.owner_dm_nonce, (.settings | objects | .nonce)]
            | map(strings) | first)}]' "$f" 2>/dev/null) || ids='[]'
    answered=$(jq -cn --argjson a "$answered" --argjson b "$ids" '$a + $b')
done

# The queue, then the owner-DM nonces (the monitor's private file; see
# the header). In that order: the monitor writes an owner DM's nonce
# before it queues the DM, so every owner DM read here has its nonce
# unless the monitor has yet to mint it (then it waits, below). Read to
# tell a real answer to an owner DM from a forged one; they reach a
# payload only in an owner-DM-only wake.
queue=$(jq -c 'if type == "array" then . else [] end' "$QUEUE" \
    2>/dev/null) || queue='[]'
[ -n "$queue" ] || queue='[]'
all_nonces='{}'
if [ -f "$PKG_DIR/owner_dm_nonces.json" ]; then
    all_nonces=$(jq -c 'if type == "object" then . else {} end' \
        "$PKG_DIR/owner_dm_nonces.json" 2>/dev/null) || all_nonces='{}'
    [ -n "$all_nonces" ] || all_nonces='{}'
fi

# Open items: queued and not answered (a burst may be answered under any
# of its fragments). An owner DM counts as answered only by an entry
# that carries its nonce: anything else in the outbox naming it is a
# forgery the monitor refuses, and must not keep the DM from its own
# wake.
open=$(printf '%s' "$queue" | jq -c --argjson answered "$answered" \
    --argjson nonces "$all_nonces" \
    '[.[]? | objects | select(.message_id != null)
    | ($nonces[.message_id | tostring]) as $n
    | (.kind == "dm" and (.settings.speaker? // "") == "owner") as $owner
    | select(([.message_id] + [(.burst_fragments // [])[]? | .message_id])
        as $ids | [$answered[] | select(.ids as $a
            | any($ids[]; . as $i | any($a[]; . == $i)))
          | select(if $n != null then .nonce == $n else ($owner | not)
              end)] | length == 0)]') || open='[]'
count=$(printf '%s' "$open" | jq 'length')
if [ "$count" -eq 0 ]; then
    silent "queue empty" '{}'
fi

woken=$(printf '%s' "$state" | jq -c '.woken // {}' 2>/dev/null) \
    || woken='{}'

# Due: open items with no live lease.
due=$(printf '%s' "$open" | jq -c --argjson w "$woken" --argjson now "$now" \
    --argjson lease "$LEASE_SECS" \
    '[.[] | select((($w[.message_id] // 0) + $lease) <= $now)]')
# An owner DM without its nonce yet (the monitor mints one within
# seconds) waits, unleased: a run handed it could not answer it.
nonceless=$(printf '%s' "$due" | jq --argjson nonces "$all_nonces" \
    '[.[] | select(.kind == "dm" and (.settings.speaker? // "") == "owner"
        and $nonces[.message_id | tostring] == null)] | length')
if [ "$nonceless" -gt 0 ]; then
    log "owner DMs wait for their nonce" '{"items":"'"$nonceless"'"}'
    due=$(printf '%s' "$due" | jq -c --argjson nonces "$all_nonces" \
        '[.[] | select((.kind == "dm"
            and (.settings.speaker? // "") == "owner"
            and $nonces[.message_id | tostring] == null) | not)]')
fi
ndue=$(printf '%s' "$due" | jq 'length')
if [ "$ndue" -eq 0 ]; then
    silent "a worker already holds every open item" \
        '{"open":"'"$count"'"}'
fi
# Owner DMs wake alone: a run that handles an owner's DM (which may ask
# to change settings) never also reads a server message or anyone
# else's, so text written by someone else can never share a context
# with an owner's settings request. The rest are handed out on the
# next run, a few seconds later (they keep no lease meanwhile).
owner_dms=$(printf '%s' "$due" | jq -c '[.[] | select(.kind == "dm"
    and (.settings.speaker // "") == "owner")]')
nowner=$(printf '%s' "$owner_dms" | jq 'length')
if [ "$nowner" -gt 0 ] && [ "$nowner" -lt "$ndue" ]; then
    log "owner DMs wake alone; the rest wait for the next run" \
        '{"owner_dms":"'"$nowner"'","held":"'"$((ndue - nowner))"'"}'
    due=$owner_dms
    ndue=$nowner
fi
# A wake of owner DMs only carries each item's owner_dm_nonce (the
# capability every outbox item for an owner DM must echo; PROTOCOL.md).
# The monitor keeps the nonces out of the queue file, in a private file
# only this hook reads, so a worker woken for anything else never sees
# one.
nonces='{}'
if [ "$nowner" -gt 0 ] && [ "$nowner" -eq "$ndue" ]; then
    nonces=$all_nonces
fi
expired=$(printf '%s' "$due" | jq --argjson w "$woken" \
    '[.[] | select($w[.message_id] != null)] | length')
if [ "$expired" -gt 0 ]; then
    log "re-waking: item lease expired" '{"items":"'"$expired"'"}'
fi

# Leases: keep the ones of items still queued (answered ones too, in case
# their outbox entry is briefly unreadable), stamp the items this wake
# hands out. The rest of the hook state (.restart) is kept as is.
# hook_state_set is a no-op on dry runs, so testing never consumes a detection.
queued=$(jq -c '[.[]? | objects | .message_id | strings]' "$QUEUE" \
    2>/dev/null) || queued='[]'
leases=$(jq -cn --argjson ids "$queued" --argjson due "$due" \
    --argjson w "$woken" --argjson now "$now" --argjson st "$state" '
    $st + {woken: (($w | with_entries(select(.key as $k | $ids | index($k))))
        + ($due | map({key: .message_id, value: $now}) | from_entries))}')
hook_state_set "$leases"

# The persona text (persona in config.yaml, old layout persona.file,
# relative to the package dir; default persona.md), so the worker need
# not read it. null when unreadable: the worker then reads the file
# itself.
CONFIG="${MUSE_DISCORD_CONFIG:-$PKG_DIR/config.yaml}"
persona_file=$(python3 - "$CONFIG" "$PKG_DIR" 2>/dev/null <<'PY'
import os
import sys
try:
    import yaml
    with open(sys.argv[1]) as f:
        name = (yaml.safe_load(f) or {}).get("persona")
    if isinstance(name, dict):
        name = name.get("file")
    if not isinstance(name, str):
        name = None
except Exception:
    name = None
print(os.path.join(sys.argv[2], os.path.expanduser(name or "persona.md")))
PY
) || persona_file="$PKG_DIR/persona.md"

# Usage counts (telemetry.py): one line per wake, once a monitor that
# keeps them (telemetry.json) has run; it counts and removes the file.
[ -e "$PKG_DIR/telemetry.json" ] \
    && { echo >> "$PKG_DIR/telemetry.wakes"; } 2>/dev/null || true

if [ -r "$persona_file" ]; then
    persona_args=(--rawfile persona "$persona_file")
else
    persona_args=(--argjson persona null)
fi

payload=$(printf '%s' "$due" | jq -c "${persona_args[@]}" --argjson nonces "$nonces" '{count: length, persona: $persona, items: [.[] | {message_id, channel_id, channel_name, kind, author, author_id, content, media, documents, unviewable, history, may_generate, owner_command, reply_to, burst, burst_fragments, from_bot, bot_turns, participants, follow_up, settings} + (if $nonces[.message_id] then {owner_dm_nonce: $nonces[.message_id]} else {} end)]}')
wake "muse-discord mentions pending" "$payload"
