#!/usr/bin/env bash
# ============================================================
#  KillTheHost - Stop Script (Linux / macOS)
#  Stops the launcher running on port 5000.
#
#  Usage:  ./stop.sh
#  AGPL-3.0  |  KillTheHost Launcher v1.5
# ============================================================

PORT=5000
PID_FILE="$HOME/.killthehost/launcher.pid"

is_alive() { kill -0 "$1" 2>/dev/null; }

PIDS=""

# 1) PID file written by launcher.py
if [ -f "$PID_FILE" ]; then
    FILE_PID="$(tr -dc '0-9' < "$PID_FILE")"
    if [ -n "$FILE_PID" ] && is_alive "$FILE_PID"; then
        PIDS="$FILE_PID"
    fi
fi

# 2) Fallback: whoever is listening on the launcher port
if [ -z "$PIDS" ]; then
    if command -v lsof > /dev/null 2>&1; then
        PIDS="$(lsof -ti tcp:"$PORT" -sTCP:LISTEN 2>/dev/null | tr '\n' ' ')"
    elif command -v fuser > /dev/null 2>&1; then
        PIDS="$(fuser "$PORT"/tcp 2>/dev/null | tr -s ' ')"
    elif command -v ss > /dev/null 2>&1; then
        PIDS="$(ss -ltnpH "sport = :$PORT" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2 | sort -u | tr '\n' ' ')"
    fi
fi

PIDS="$(echo "$PIDS" | xargs)"

if [ -z "$PIDS" ]; then
    rm -f "$PID_FILE"
    echo "[KillTheHost] Launcher is not running."
    exit 0
fi

echo "[KillTheHost] Stopping launcher (PID: $PIDS)..."

# Graceful stop: SIGTERM lets the launcher shut its services down cleanly
for pid in $PIDS; do kill -TERM "$pid" 2>/dev/null; done

# Wait up to 3 seconds
for _ in 1 2 3 4 5 6; do
    STILL=""
    for pid in $PIDS; do is_alive "$pid" && STILL="$STILL $pid"; done
    [ -z "$STILL" ] && break
    sleep 0.5
done

# Force kill anything left
if [ -n "$STILL" ]; then
    echo "[KillTheHost] Launcher did not exit in time — forcing (SIGKILL)."
    for pid in $STILL; do kill -KILL "$pid" 2>/dev/null; done
    sleep 0.5
fi

rm -f "$PID_FILE"
echo "[KillTheHost] Launcher stopped."
