#!/bin/bash
# muse-discord watchdog: keeps the monitor and presence processes alive.
#
# Fully automatic: both processes read the bot token themselves from the
# configured token file (bot.token_file in config.yaml, mode 600). This
# script never sees the token value; it only checks that the file exists.
#
# Output contract (for whatever scheduler runs this, e.g. cron):
#   - "OK ..." lines for healthy or recovered state.
#   - "ALERT: ..." printed ONLY on a NEW failure needing the operator's
#     attention. Repeat runs while the same failure persists print
#     "OK (already alerted)".
#   - State lives in watchdog_state.json; log in watchdog.log.
#
# Health contract with the processes (do not change casually):
#   - monitor.py prints a line containing "Bot user id" at startup,
#     BEFORE its (slow) channel discovery, success or failure.
#   - presence.py prints a line containing "READY" once connected.
#   - Both are started fully detached (double-fork) so a scheduler
#     timeout killing this script's process tree cannot take them down.

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
PRESENCE_ENABLED="$(cfg_get presence.enabled true)"

# Start a fully detached daemon via double-fork: the final process is
# reparented to PID 1 immediately, so it is NOT part of this script's
# process tree. (Plain `setsid ... &` leaves the child under this shell;
# when a scheduler's exec session times out, the runtime may kill the
# whole tree and the bot dies with it.)
# $1 = script (monitor.py/presence.py), $2 = log file (truncated first,
# then appended).
daemonize() {
  : > "$2"
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
# $1 = alive() pattern, $2 = log marker, $3 = log file, $4 = max seconds.
wait_healthy() {
  local n=$(( ($4 + 4) / 5 ))
  for _ in $(seq 1 "$n"); do
    sleep 5
    if alive "$1" && grep -q "$2" "$3" 2>/dev/null; then
      return 0
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
if alive "[m]onitor.py"; then
  echo "OK monitor alive"
else
  log "monitor not running; attempting restart"
  if [ ! -s "$TOKEN_FILE" ]; then
    ALERT_SIG="monitor DOWN and no token file at $TOKEN_FILE; cannot restart"
    log "ALERT: $ALERT_SIG"
  else
    daemonize monitor.py "$DIR/monitor.log"
    # The health window must comfortably exceed monitor startup:
    # the identity marker prints immediately, but give slow systems room.
    if wait_healthy "[m]onitor.py" "Bot user id" "$DIR/monitor.log" 120; then
      log "monitor restarted OK"
      echo "OK monitor restarted"
    else
      pkill -f "^python3 [m]onitor.py" 2>/dev/null
      if grep -q "401" "$DIR/monitor.log" 2>/dev/null; then
        TOKEN_BAD=1
        ALERT_SIG="monitor DOWN: Discord rejected the token (401); it was probably reset. Paste a fresh bot token into the token file and the watchdog will recover on its next run."
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
      if [ ! -s "$TOKEN_FILE" ]; then
        ALERT_SIG="presence DOWN and no token file at $TOKEN_FILE; cannot restart"
        log "ALERT: $ALERT_SIG"
      else
        daemonize presence.py "$DIR/presence.log"
        if wait_healthy "[p]resence.py" "READY" "$DIR/presence.log" 60; then
          log "presence restarted OK"
          echo "OK presence restarted"
        else
          pkill -f "^python3 [p]resence.py" 2>/dev/null
          ALERT_SIG="presence DOWN: restart attempted but no READY in presence.log; check $DIR/presence.log"
          log "ALERT: $ALERT_SIG"
        fi
      fi
    fi
  fi
fi

# ---------------- transition-based alerting ----------------
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
