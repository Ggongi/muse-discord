#!/bin/bash
# muse-discord watchdog: keeps the monitor and presence processes alive.
#
# Fully automatic: both processes read the bot token themselves from the
# configured token file (bot.token_file in config.yaml, mode 600), or
# from the environment variable named by bot.token_env, which must then
# be set in this script's own environment (the processes inherit it).
# This script never uses the token value; it only checks that one of the
# two is present.
#
# Output contract (for whatever scheduler runs this, e.g. cron):
#   - "OK ..." lines for healthy or recovered state.
#   - "ALERT: ..." printed ONLY on a NEW failure needing the operator's
#     attention. Repeat runs while the same failure persists print
#     "OK (already alerted)".
#   - "UPDATE: ..." printed ONCE per new upstream version (checked at
#     most daily; updates.check in config.yaml turns it off). Never an
#     alert, and the watchdog never applies updates: see UPDATING.md.
#   - State lives in watchdog_state.json and update_check.json; log in
#     watchdog.log.
#
# Health contract with the processes (do not change casually):
#   - monitor.py prints "Bot user id: <id>" once its identity is
#     confirmed, or "Bot identity pending" when /users/@me failed for a
#     reason that can heal (a 429 storm, the network) and it keeps
#     retrying in-loop. Either counts as a healthy start; both print
#     BEFORE its (slow) channel discovery.
#   - Either process prints "TOKEN REJECTED" and exits when Discord
#     refuses the token (REST 401 or gateway close 4004): the token alert.
#   - monitor.py prints "GATEWAY DISABLED" when realtime ingest stops for
#     good (e.g. the Message Content intent is off) while REST polling
#     carries on: alerted as degraded.
#   - monitor.py touches monitor.heartbeat from its main loop; a live
#     monitor whose heartbeat is older than HEARTBEAT_STALE_SECS is stuck
#     and gets restarted.
#   - presence.py prints a line containing "READY" once connected.
#   - Both are started fully detached (double-fork) so a scheduler
#     timeout killing this script's process tree cannot take them down.
#     The previous run's log is kept as <name>.log.1 on each restart.

set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$DIR/watchdog.log"
STATE="$DIR/watchdog_state.json"
CONFIG="$DIR/config.yaml"

log() { echo "$(date '+%F %T') $*" >> "$LOG"; }
alive() { pgrep -f "^python3 $1" >/dev/null 2>&1; }
get_state() { python3 -c "import json;print(json.load(open('$STATE')).get('last_alert',''))" 2>/dev/null || echo ""; }
set_state() { python3 -c "import json;json.dump({'last_alert':\"$1\"},open('$STATE','w'))" 2>/dev/null; }

# Read one config value with the same YAML the monitor uses. Prints the
# fallback (arg 2) when the config or key is missing.
cfg_get() {
  python3 - "$CONFIG" "$1" "$2" <<'PYEOF' 2>/dev/null || echo "$2"
import sys
try:
    import yaml
    with open(sys.argv[1]) as f:
        cfg = yaml.safe_load(f) or {}
    cur = cfg
    for part in sys.argv[2].split("."):
        cur = (cur or {}).get(part)
        if cur is None:
            break
    if cur is None:
        print(sys.argv[3] if len(sys.argv) > 3 else "")
    elif isinstance(cur, bool):
        print("true" if cur else "false")
    else:
        print(cur)
except Exception:
    print(sys.argv[3] if len(sys.argv) > 3 else "")
PYEOF
}

TOKEN_FILE="$(cfg_get bot.token_file "$HOME/.config/muse-discord/bot_token")"
TOKEN_FILE="${TOKEN_FILE/#\~/$HOME}"
TOKEN_ENV="$(cfg_get bot.token_env "")"
PRESENCE_ENABLED="$(cfg_get presence.enabled true)"
HEARTBEAT="$DIR/monitor.heartbeat"
HEARTBEAT_STALE_SECS=600

# True when the processes will find a token: the bot.token_env variable
# set in this environment (they inherit it), or a non-empty token file.
token_available() {
  if [[ "$TOKEN_ENV" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
      && [ -n "${!TOKEN_ENV:-}" ]; then
    return 0
  fi
  [ -s "$TOKEN_FILE" ]
}
if [ -n "$TOKEN_ENV" ]; then
  TOKEN_WHERE="env var \$$TOKEN_ENV unset and no token file at $TOKEN_FILE"
else
  TOKEN_WHERE="no token file at $TOKEN_FILE"
fi

# Seconds since the monitor last touched its heartbeat, or -1 when there
# is no heartbeat file (a monitor from before heartbeats existed).
heartbeat_age() {
  python3 - "$HEARTBEAT" <<'PYEOF' 2>/dev/null || echo -1
import os, sys, time
print(int(time.time() - os.path.getmtime(sys.argv[1])))
PYEOF
}

# Stop a process: SIGTERM, up to 10s to exit, then SIGKILL.
# $1 = alive() pattern.
stop_proc() {
  pkill -f "^python3 $1" 2>/dev/null
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    alive "$1" || return 0
    sleep 1
  done
  pkill -9 -f "^python3 $1" 2>/dev/null
  sleep 1
}

# Start a fully detached daemon via double-fork: the final process is
# reparented to PID 1 immediately, so it is NOT part of this script's
# process tree. (Plain `setsid ... &` leaves the child under this shell;
# when a scheduler's exec session times out, the runtime may kill the
# whole tree and the bot dies with it.)
# $1 = script (monitor.py/presence.py), $2 = log file. The previous log
# moves to $2.1 rather than being truncated: it holds the crash or token
# error that made this restart necessary.
daemonize() {
  if [ -f "$2" ]; then
    mv -f "$2" "$2.1"
  fi
  python3 - "$DIR" "$1" "$2" <<'EOF'
import os, sys
d, script, logf = sys.argv[1], sys.argv[2], sys.argv[3]
if os.fork() > 0:
    os._exit(0)
os.setsid()
if os.fork() > 0:
    os._exit(0)
os.chdir(d)
si = open(os.devnull, 'r')
so = open(logf, 'a')
se = open(logf, 'a')
os.dup2(si.fileno(), 0)
os.dup2(so.fileno(), 1)
os.dup2(se.fileno(), 2)
# argv[0]='python3' keeps the pgrep "^python3 ..." health checks matching.
os.execv(sys.executable, ['python3', script])
EOF
}

# Poll for a healthy start, breaking early instead of sleeping a fixed time.
# $1 = alive() pattern, $2 = log marker (extended regex), $3 = log file,
# $4 = max seconds. A rejected token ends the wait at once.
wait_healthy() {
  local n=$(( ($4 + 4) / 5 ))
  for _ in $(seq 1 "$n"); do
    sleep 5
    if alive "$1" && grep -Eq "$2" "$3" 2>/dev/null; then
      return 0
    fi
    if grep -q "TOKEN REJECTED" "$3" 2>/dev/null; then
      return 1
    fi
  done
  return 1
}

ALERT_SIG=""
TOKEN_BAD=0

# Environment refreshes can wipe pip packages; reinstall BEFORE any
# restart so monitor's gateway thread (which imports websockets at
# thread start and never retries) comes up healthy too. If this check
# lived after the monitor restart, every refresh would leave the monitor
# running with a permanently dead gateway thread.
WEBSOCKETS_OK=1
if ! python3 -c "import websockets" 2>/dev/null; then
  log "websockets missing; reinstalling"
  if ! pip install --break-system-packages -q websockets >>"$LOG" 2>&1; then
    log "websockets reinstall failed"
    WEBSOCKETS_OK=0
  fi
fi
if ! python3 -c "import yaml" 2>/dev/null; then
  log "PyYAML missing; reinstalling"
  pip install --break-system-packages -q pyyaml >>"$LOG" 2>&1 || \
    log "PyYAML reinstall failed (monitor cannot load config without it)"
fi

# ---------------- monitor ----------------
MONITOR_HUNG=""
DEGRADED_SIG=""
if alive "[m]onitor.py"; then
  HB_AGE="$(heartbeat_age)"
  if [ "$HB_AGE" -ge "$HEARTBEAT_STALE_SECS" ]; then
    # The process lives but its main loop has not moved in 10+ minutes:
    # it answers nobody, and only a restart helps.
    log "monitor alive but its heartbeat is ${HB_AGE}s old (stuck); restarting"
    stop_proc "[m]onitor.py"
    MONITOR_HUNG="$HB_AGE"
  else
    echo "OK monitor alive"
    GW_OFF="$(grep -o -m1 'GATEWAY DISABLED ([^)]*)' "$DIR/monitor.log" 2>/dev/null)"
    if [ -n "$GW_OFF" ]; then
      DEGRADED_SIG="monitor degraded: $GW_OFF in monitor.log; realtime ingest is off, so replies wait for the REST poll. See SETUP.md Troubleshooting, then restart the monitor."
    fi
  fi
fi
if ! alive "[m]onitor.py"; then
  [ -z "$MONITOR_HUNG" ] && log "monitor not running; attempting restart"
  if ! token_available; then
    ALERT_SIG="monitor DOWN and $TOKEN_WHERE; cannot restart"
    log "ALERT: $ALERT_SIG"
  else
    daemonize monitor.py "$DIR/monitor.log"
    # The health window must comfortably exceed monitor startup:
    # the identity marker prints immediately, but give slow systems room.
    if wait_healthy "[m]onitor.py" "Bot user id|Bot identity pending" \
        "$DIR/monitor.log" 120; then
      NOTE=""
      if [ -n "$MONITOR_HUNG" ]; then
        NOTE=" (it was stuck: heartbeat ${MONITOR_HUNG}s old; its log is monitor.log.1)"
      fi
      if ! grep -q "Bot user id" "$DIR/monitor.log" 2>/dev/null; then
        NOTE="$NOTE (identity pending: Discord is rate limiting or unreachable; it keeps retrying)"
      fi
      log "monitor restarted OK$NOTE"
      echo "OK monitor restarted$NOTE"
    else
      stop_proc "[m]onitor.py"
      if grep -q "TOKEN REJECTED" "$DIR/monitor.log" 2>/dev/null; then
        TOKEN_BAD=1
        ALERT_SIG="monitor DOWN: Discord rejected the token; it was probably reset. Paste a fresh bot token into the token file and the watchdog will recover on its next run."
      else
        ALERT_SIG="monitor DOWN: restart attempted but the process is not healthy; check $DIR/monitor.log"
      fi
      log "ALERT: $ALERT_SIG"
    fi
  fi
fi

# ---------------- presence ----------------
if [ "$PRESENCE_ENABLED" != "true" ]; then
  echo "OK presence disabled in config"
elif alive "[p]resence.py"; then
  echo "OK presence alive"
else
  if [ "$TOKEN_BAD" = "1" ]; then
    log "skipping presence restart (token already known bad)"
  else
    log "presence not running; attempting restart"
    # websockets already reinstalled above (before the monitor section);
    # fail here only if that reinstall failed.
    if [ "$WEBSOCKETS_OK" != "1" ]; then
      ALERT_SIG="presence DOWN: could not reinstall the websockets package"
      log "ALERT: $ALERT_SIG"
    fi
    if [ -z "$ALERT_SIG" ]; then
      if ! token_available; then
        ALERT_SIG="presence DOWN and $TOKEN_WHERE; cannot restart"
        log "ALERT: $ALERT_SIG"
      else
        daemonize presence.py "$DIR/presence.log"
        if wait_healthy "[p]resence.py" "READY" "$DIR/presence.log" 60; then
          log "presence restarted OK"
          echo "OK presence restarted"
        else
          stop_proc "[p]resence.py"
          if grep -q "TOKEN REJECTED" "$DIR/presence.log" 2>/dev/null; then
            ALERT_SIG="presence DOWN: Discord rejected the token; it was probably reset. Paste a fresh bot token into the token file and the watchdog will recover on its next run."
          else
            ALERT_SIG="presence DOWN: restart attempted but no READY in presence.log; check $DIR/presence.log"
          fi
          log "ALERT: $ALERT_SIG"
        fi
      fi
    fi
  fi
fi

# ---------------- transition-based alerting ----------------
# A degraded-but-running monitor alerts only when nothing is down.
if [ -z "$ALERT_SIG" ] && [ -n "$DEGRADED_SIG" ]; then
  ALERT_SIG="$DEGRADED_SIG"
  log "ALERT: $ALERT_SIG"
fi
if [ -n "$ALERT_SIG" ]; then
  if [ "$(get_state)" != "$ALERT_SIG" ]; then
    set_state "$ALERT_SIG"
    echo "ALERT: $ALERT_SIG"
  else
    echo "OK already alerted (still failing): $ALERT_SIG"
  fi
else
  if [ -n "$(get_state)" ]; then
    set_state ""
    log "recovered; alert state cleared"
    echo "OK recovered; previous alert cleared"
  fi
fi

# ---------------- updates ----------------
# At most once a day, fetch the upstream branch and print ONE "UPDATE:"
# line per new upstream version. Never pulls: applying an update is the
# owner's call, and their Muse does it by following UPDATING.md. Skipped
# quietly for installs that are not a git clone with an upstream.
UPDATE_STATE="$DIR/update_check.json"
upd_get() {
  python3 - "$UPDATE_STATE" "$1" <<'PYEOF' 2>/dev/null || echo ""
import json, sys
print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))
PYEOF
}
upd_set() {
  python3 - "$UPDATE_STATE" "$1" "$2" <<'PYEOF' 2>/dev/null
import json, sys
json.dump({"checked": int(sys.argv[2]), "announced": sys.argv[3]},
          open(sys.argv[1], "w"))
PYEOF
}
if [ "$(cfg_get updates.check true)" = "true" ] \
    && git -C "$DIR" rev-parse --abbrev-ref '@{upstream}' >/dev/null 2>&1; then
  now=$(date +%s)
  last_check="$(upd_get checked)"
  announced="$(upd_get announced)"
  if [ $(( now - ${last_check:-0} )) -ge 86400 ]; then
    to=""
    command -v timeout >/dev/null 2>&1 && to="timeout 60"
    if GIT_TERMINAL_PROMPT=0 $to git -C "$DIR" fetch --quiet >>"$LOG" 2>&1; then
      behind=$(git -C "$DIR" rev-list --count 'HEAD..@{upstream}' 2>/dev/null || echo 0)
      upstream_head=$(git -C "$DIR" rev-parse '@{upstream}' 2>/dev/null || echo "")
      if [ "${behind:-0}" -gt 0 ] && [ "$upstream_head" != "$announced" ]; then
        log "update available: $behind new commit(s) upstream"
        echo "UPDATE: muse-discord has $behind new commit(s) upstream. Ask your Muse to update it by following UPDATING.md."
        announced="$upstream_head"
      fi
    else
      log "update check: git fetch failed; retrying tomorrow"
    fi
    upd_set "$now" "$announced"
  fi
fi
