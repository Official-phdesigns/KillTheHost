#!/usr/bin/env sh
# ============================================================
#  KillTheHost — Linux / macOS Launcher
#  Place this file in:  KillTheHost/
#
#  First time:
#    chmod +x launch.sh
#    ./launch.sh
#
#  AGPL-3.0  |  KillTheHost Launcher v1.6 
# ============================================================

SCRIPT="$(cd "$(dirname "$0")" && pwd)/Launcher/launcher.py"

# ── Find Python ─────────────────────────────────────────────
PYTHON=""

for candidate in python3 python; do
    if command -v "$candidate" > /dev/null 2>&1; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo ""
    echo "  [ERROR] Python was not found on your system."
    echo ""
    echo "  Install it:"
    echo "    Ubuntu / Debian : sudo apt install python3"
    echo "    Fedora          : sudo dnf install python3"
    echo "    macOS (brew)    : brew install python"
    echo "    macOS (direct)  : https://python.org/downloads/"
    echo ""
    exit 1
fi

# ── Verify minimum version (3.8+) ───────────────────────────
PY_MAJOR=$("$PYTHON" -c "import sys; print(sys.version_info.major)")
PY_MINOR=$("$PYTHON" -c "import sys; print(sys.version_info.minor)")

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 8 ]; }; then
    echo ""
    echo "  [ERROR] Python 3.8+ is required."
    echo "  Found:  $("$PYTHON" --version 2>&1)"
    echo "  Upgrade: https://python.org/downloads/"
    echo ""
    exit 1
fi

# ── Check launcher script exists ────────────────────────────
if [ ! -f "$SCRIPT" ]; then
    ROOT="$(cd "$(dirname "$0")" && pwd)"
    echo ""
    echo "  [ERROR] Launcher script not found:"
    echo "  $SCRIPT"
    echo ""
    echo "  ── Diagnosing your folder structure ──"
    echo ""
    echo "  Contents of: $ROOT"
    ls -1 "$ROOT" 2>/dev/null | sed 's/^/    /'
    echo ""
    if [ -d "$ROOT/Launcher" ]; then
        echo "  Contents of: $ROOT/Launcher"
        ls -1 "$ROOT/Launcher" 2>/dev/null | sed 's/^/    /'
        echo ""
        if [ -d "$ROOT/Launcher/assets" ]; then
            echo "  Contents of: $ROOT/Launcher/assets"
            ls -1 "$ROOT/Launcher/assets" 2>/dev/null | sed 's/^/    /'
            echo ""
        else
            echo "  [!] $ROOT/Launcher/assets  -- folder does not exist"
            echo ""
        fi
    else
        echo "  [!] $ROOT/Launcher  -- folder does not exist"
        echo ""
    fi
    echo "  Paste the output above so the path can be corrected."
    echo ""
    exit 1
fi

# ── Linux: check Docker group membership ────────────────────
OS="$(uname -s)"
if [ "$OS" = "Linux" ]; then
    if ! id -nG 2>/dev/null | grep -qw "docker"; then
        echo ""
        echo "  [NOTICE] Your user is not in the docker group."
        echo "  PHP-MNGR may fail to reach the Docker socket."
        echo "  Fix: sudo usermod -aG docker \$USER  (then log out & back in)"
        echo ""
    fi
fi

# Gtk modules / dbus-launch are installed by launcher.py on start-up
# (apt, dnf, yum, zypper, pacman). Set KTH_SKIP_GTK_INSTALL=1 to skip.

# ── Linux: clean browser environment ─────────────────────────────────────────
# Remove GTK_MODULES entirely — no modules to load means no "Failed to load" warnings.
# Launcher.py opens browsers with setsid so Flatpak/Snap browsers have no tty to write to.
if [ "$OS" = "Linux" ]; then
    unset GTK_MODULES
    unset GTK2_MODULES
    unset GDK_MODULES
    if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ] && [ -S "/run/user/$(id -u)/bus" ]; then
        export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$(id -u)/bus"
    fi
    if [ -z "${XDG_RUNTIME_DIR:-}" ] && [ -d "/run/user/$(id -u)" ]; then
        export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    fi
fi


# ── Launch ──────────────────────────────────────────────────
echo ""
echo "  KillTheHost Launcher"
echo "  Python : $("$PYTHON" --version 2>&1)"
echo "  Script : $SCRIPT"
echo ""

exec "$PYTHON" "$SCRIPT"
