#!/usr/bin/env bash
# ============================================================
#  KillTheHost - Restart Script (Linux / macOS)
#  Stops the launcher (if running) and starts it again.
#
#  Usage:  ./restart.sh
#  AGPL-3.0  |  KillTheHost Launcher v1.5
# ============================================================

ROOT="$(cd "$(dirname "$0")" && pwd)"

echo "[KillTheHost] Restarting launcher..."
bash "$ROOT/stop.sh"
sleep 2
exec sh "$ROOT/launch.sh"
