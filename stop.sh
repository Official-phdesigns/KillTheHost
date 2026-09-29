#!/usr/bin/env bash
# ============================================================
#  KillTheHost - Stop Script (Linux / macOS)
#  Stops the launcher (port 5000) AND every suite service
#  listed in Launcher/launcher.py SERVICES (PHP-MNGR, DB-3NGIN3,
#  MAIL-SRVR, STAX-MNGR, NODE-MNGR, SEC-MNGR, ...).
#
#  Service ports are read from launcher.py at run time, so new
#  services added to the launcher are stopped automatically.
#
#  Usage:  ./stop.sh
#  AGPL-3.0  |  KillTheHost Launcher v1.5
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER_PY="$SCRIPT_DIR/Launcher/launcher.py"
PID_FILE="$HOME/.killthehost/launcher.pid"

LAUNCHER_PORT=5000
FALLBACK_PORTS="4280 7734 6060 6161 7272 8080"

# ── Read ports from launcher.py ─────────────────────────────
if [ -f "$LAUNCHER_PY" ]; then
    P="$(sed -n 's/^LAUNCHER_PORT[[:space:]]*=[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$LAUNCHER_PY" | head -n1)"
    [ -n "$P" ] && LAUNCHER_PORT="$P"
    SERVICE_PORTS="$(sed -n 's/.*"port"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$LAUNCHER_PY" | sort -un | xargs)"
fi
[ -z "$SERVICE_PORTS" ] && SERVICE_PORTS="$FALLBACK_PORTS"

# Alive = exists and is not a zombie (exited, just not reaped yet)
is_alive() {
    kill -0 "$1" 2>/dev/null || return 1
    case "$(ps -o stat= -p "$1" 2>/dev/null)" in Z*) return 1 ;; esac
    return 0
}

# PIDs listening on a TCP port (lsof → ss → fuser)
pids_on_port() {
    local port="$1" out=""
    if command -v lsof > /dev/null 2>&1; then
        out="$(lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null)"
        [ -z "$out" ] && command -v sudo > /dev/null 2>&1 && \
            out="$(sudo -n lsof -ti tcp:"$port" -sTCP:LISTEN 2>/dev/null)"
    fi
    if [ -z "$out" ] && command -v ss > /dev/null 2>&1; then
        out="$(ss -ltnpH "sport = :$port" 2>/dev/null | grep -o 'pid=[0-9]*' | cut -d= -f2)"
    fi
    if [ -z "$out" ] && command -v fuser > /dev/null 2>&1; then
        out="$(fuser "$port"/tcp 2>/dev/null)"
    fi
    echo "$out" | tr -s ' \n' ' ' | xargs -n1 2>/dev/null | sort -un | xargs
}

proc_name() { ps -p "$1" -o comm= 2>/dev/null | head -n1; }

# SIGTERM, wait up to 3s, then SIGKILL (sudo -n used if we lack permission)
stop_pids() {
    local pids="$1" pid still=""
    for pid in $pids; do
        kill -TERM "$pid" 2>/dev/null || sudo -n kill -TERM "$pid" 2>/dev/null
    done
    for _ in 1 2 3 4 5 6; do
        still=""
        for pid in $pids; do is_alive "$pid" && still="$still $pid"; done
        [ -z "$still" ] && return 0
        sleep 0.5
    done
    for pid in $still; do
        kill -KILL "$pid" 2>/dev/null || sudo -n kill -KILL "$pid" 2>/dev/null
    done
    sleep 0.5
    for pid in $still; do is_alive "$pid" && return 1; done
    return 0
}

# ── 1) Launcher ─────────────────────────────────────────────
LPIDS=""
if [ -f "$PID_FILE" ]; then
    FILE_PID="$(tr -dc '0-9' < "$PID_FILE")"
    [ -n "$FILE_PID" ] && is_alive "$FILE_PID" && LPIDS="$FILE_PID"
fi
[ -z "$LPIDS" ] && LPIDS="$(pids_on_port "$LAUNCHER_PORT")"

if [ -n "$LPIDS" ]; then
    echo "[KillTheHost] Stopping launcher on port $LAUNCHER_PORT (PID: $LPIDS)..."
    # SIGTERM lets the launcher shut its managed services down cleanly first
    stop_pids "$LPIDS" || echo "[KillTheHost] WARNING: launcher PID(s) still alive: $LPIDS"
else
    echo "[KillTheHost] Launcher is not running."
fi
rm -f "$PID_FILE"

# Give services a moment to release their ports after the launcher's shutdown
sleep 1

# ── 2) Suite services on their ports ────────────────────────
STOPPED=0
FAILED=""
for port in $SERVICE_PORTS; do
    [ "$port" = "$LAUNCHER_PORT" ] && continue
    SPIDS="$(pids_on_port "$port")"
    [ -z "$SPIDS" ] && continue
    for pid in $SPIDS; do
        echo "[KillTheHost] Stopping service on port $port (PID: $pid, $(proc_name "$pid"))..."
    done
    if stop_pids "$SPIDS"; then
        STOPPED=$((STOPPED + 1))
    else
        FAILED="$FAILED $port"
    fi
done

if [ -n "$FAILED" ]; then
    echo "[KillTheHost] WARNING: could not free port(s):$FAILED (try: sudo ./stop.sh)"
    exit 1
fi

if [ "$STOPPED" -gt 0 ]; then
    echo "[KillTheHost] Stopped $STOPPED leftover service(s)."
fi
echo "[KillTheHost] All suite ports free: $LAUNCHER_PORT $SERVICE_PORTS"
exit 0
