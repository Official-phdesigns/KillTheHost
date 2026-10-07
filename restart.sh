#!/usr/bin/env bash
# ============================================================
#  KillTheHost - Restart Script (Linux / macOS)
#  Stops the launcher AND anything holding a suite port
#  (duplicates / leftovers from earlier runs), verifies every
#  port is free, then starts the launcher again.
#
#  Usage:  ./restart.sh
#  AGPL-3.0  |  KillTheHost Launcher v1.6
# ============================================================

ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "[KillTheHost] Restarting launcher..."

# stop.sh exits non-zero if any suite port could not be freed; retry once
if ! bash "$ROOT/stop.sh"; then
    echo "[KillTheHost] Some ports still busy — retrying stop..."
    sleep 2
    if ! bash "$ROOT/stop.sh"; then
        echo "[KillTheHost] ERROR: suite ports are still held by another process."
        echo "[KillTheHost] Run 'sudo ./stop.sh' (process may belong to another user), then ./launch.sh"
        exit 1
    fi
fi

sleep 1
exec sh "$ROOT/launch.sh"
