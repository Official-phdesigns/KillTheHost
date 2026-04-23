#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║       STAX-MNGR  v1.0  —  KillTheHost               ║
║                                                      ║
║   Manage all Docker containers on this system.       ║
║   Deploy pre-configured self-hosted app stacks.      ║
║                                                      ║
║   Browser UI → http://localhost:6161                 ║
║   Pure Python 3.8+. Zero pip installs.               ║
╚══════════════════════════════════════════════════════╝
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ── Constants ─────────────────────────────────────────────────────────────────
PORT       = 6161
DATA_DIR   = Path.home() / ".staxmngr"
STACKS_FILE = DATA_DIR / "stacks.json"
STAX_NET   = "stax-net"
VERSION    = "1.0"

DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Install log buffer (key = slug) ──────────────────────────────────────────
_install_logs: dict = {}    # slug -> list of str
_install_state: dict = {}   # slug -> "installing" | "done" | "error"
_update_status: dict = {}   # slug -> "up_to_date" | "updated" | "has_update"
_update_available: dict = {} # slug -> True | False
_starting_stacks: dict = {} # slug -> expiry timestamp (show "Starting…" post-install)
_log_lock = threading.Lock()


def _check_stack_updates(slug: str) -> bool:
    """Check if newer images exist for a stack. Returns True if any updated."""
    if slug not in STACKS:
        return False
    has_update = False
    for svc in STACKS[slug]["services"]:
        image = svc["image"]
        out, err, rc = docker(f"pull {image}", timeout=180)
        if rc != 0:
            continue
        combined = (out + err).lower()
        # "Status: Image is up to date" = already current
        # "Status: Downloaded newer image" = actually updated
        # "Pull complete" appears for BOTH cases (layer digest lines) — do NOT use it alone
        if "status: image is up to date" in combined or "image is up to date for" in combined:
            pass  # this image is current
        elif "status: downloaded newer image" in combined or "downloaded newer image for" in combined:
            has_update = True
        # If neither status line present, assume up to date (avoids false positives)

    if has_update:
        _update_available[slug] = True
        _update_status[slug] = "has_update"
    else:
        _update_available[slug] = False
        _update_status[slug] = "up_to_date"
    return has_update


def _background_update_checker():
    """Check installed stacks for updates at startup then every 6 hours."""
    time.sleep(30)  # Short wait for Docker to be ready
    while True:
        installed = load_stacks()
        for slug in list(installed.keys()):
            # Skip stacks currently being installed
            if _install_state.get(slug) == "installing":
                continue
            try:
                _check_stack_updates(slug)
            except Exception:
                pass
            time.sleep(3)
        time.sleep(6 * 3600)


def _ilog(slug: str, msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with _log_lock:
        _install_logs.setdefault(slug, []).append(line)
        if len(_install_logs[slug]) > 500:
            _install_logs[slug] = _install_logs[slug][-400:]
    print(f"[STAX/{slug}] {msg}", flush=True)


# ── Stack definitions ─────────────────────────────────────────────────────────
STACKS = {
    "vaultwarden": {
        "startup_secs": 30,
        "name": "VaultWarden",
        "icon": "🔐",
        "description": "Password manager compatible with all Bitwarden clients. "
                       "Secure, lightweight, and self-hosted.",
        "category": "Privacy",
        "tags": ["passwords", "security", "bitwarden"],
        "url_port": 8200,
        "docs": "https://github.com/dani-garcia/vaultwarden",
        "services": [
            {
                "name": "stax-vaultwarden",
                "image": "vaultwarden/server:latest",
                "ports": [("8200", "80")],
                "volumes": [("{data}/data", "/data")],
                "env": {"WEBSOCKET_ENABLED": "true", "SIGNUPS_ALLOWED": "true"},
                "network": STAX_NET,
            }
        ],
    },
    "uptimekuma": {
        "startup_secs": 30,
        "name": "Uptime Kuma",
        "icon": "📡",
        "description": "Self-hosted uptime monitoring tool. "
                       "Monitor websites, APIs, TCP, DNS, and more with beautiful status pages.",
        "category": "Monitoring",
        "tags": ["uptime", "monitoring", "alerting"],
        "url_port": 3002,
        "docs": "https://github.com/louislam/uptime-kuma",
        "services": [
            {
                "name": "stax-uptimekuma",
                "image": "louislam/uptime-kuma:latest",
                "ports": [("3002", "3001")],
                "volumes": [("{data}/data", "/app/data")],
                "env": {},
                "network": STAX_NET,
            }
        ],
    },
    "navidrome": {
        "startup_secs": 30,
        "name": "Navidrome",
        "icon": "🎵",
        "description": "Modern music server and streamer. "
                       "Compatible with Subsonic/Airsonic apps. Stream your music anywhere.",
        "category": "Media",
        "tags": ["music", "streaming", "subsonic"],
        "url_port": 4533,
        "docs": "https://www.navidrome.org",
        "services": [
            {
                "name": "stax-navidrome",
                "image": "deluan/navidrome:latest",
                "ports": [("4533", "4533")],
                "volumes": [("{data}/data", "/data"),
                             ("{data}/music", "/music")],
                "env": {"ND_SCANSCHEDULE": "1h", "ND_LOGLEVEL": "info",
                        "ND_SESSIONTIMEOUT": "24h"},
                "network": STAX_NET,
            }
        ],
    },
    "wikijs": {
        "startup_secs": 90,
        "name": "Wiki.js",
        "icon": "📖",
        "description": "The most powerful and extensible open source wiki software. "
                       "Beautiful editor, powerful search, and 100+ storage/auth integrations.",
        "category": "Productivity",
        "tags": ["wiki", "documentation", "knowledge-base"],
        "url_port": 3003,
        "docs": "https://js.wiki",
        "services": [
            {
                "name": "stax-wikijs",
                "image": "ghcr.io/requarks/wiki:2",
                "ports": [("3003", "3000")],
                "volumes": [("{data}/data", "/wiki/data/content")],
                "env": {"DB_TYPE": "sqlite", "DB_FILEPATH": "/wiki/data/wiki.db"},
                "network": STAX_NET,
            }
        ],
    },
    "jellyfin": {
        "startup_secs": 60,
        "name": "Jellyfin",
        "icon": "🎬",
        "description": "The Free Software Media System. "
                       "Stream movies, TV shows, music and photos to any device.",
        "category": "Media",
        "tags": ["media", "streaming", "movies", "tv"],
        "url_port": 8096,
        "docs": "https://jellyfin.org",
        "services": [
            {
                "name": "stax-jellyfin",
                "image": "jellyfin/jellyfin:latest",
                "ports": [("8096", "8096")],
                "volumes": [("{data}/config", "/config"),
                             ("{data}/cache", "/cache"),
                             ("{data}/media", "/media")],
                "env": {},
                "network": STAX_NET,
            }
        ],
    },
    "ollama": {
        "startup_secs": 60,
        "name": "Ollama",
        "icon": "🤖",
        "description": "Run large language models locally. "
                       "Pull and run Llama, Mistral, Gemma, CodeLlama and more in one command.",
        "category": "AI",
        "tags": ["llm", "ai", "local-ai"],
        "url_port": 11434,
        "docs": "https://ollama.ai",
        "services": [
            {
                "name": "stax-ollama",
                "image": "ollama/ollama:latest",
                "ports": [("11434", "11434")],
                "volumes": [("{data}/ollama", "/root/.ollama")],
                "env": {},
                "network": STAX_NET,
            }
        ],
    },
    "openwebui": {
        "startup_secs": 60,
        "name": "Open WebUI",
        "icon": "💬",
        "description": "Feature-rich, user-friendly web interface for Ollama and OpenAI-compatible APIs. "
                       "Chat history, multimodal support, and RAG built in.",
        "category": "AI",
        "tags": ["llm", "ai", "chatbot", "ollama"],
        "url_port": 3004,
        "docs": "https://github.com/open-webui/open-webui",
        "services": [
            {
                "name": "stax-openwebui",
                "image": "ghcr.io/open-webui/open-webui:main",
                "ports": [("3004", "8080")],
                "volumes": [("{data}/data", "/app/backend/data")],
                "env": {"OLLAMA_BASE_URL": "http://host.docker.internal:11434",
                        "WEBUI_SECRET_KEY": "change-me-in-production"},
                "extra_args": ["--add-host=host.docker.internal:host-gateway"],
                "network": STAX_NET,
            }
        ],
    },
    "wordpress": {
        "startup_secs": 120,
        "name": "WordPress",
        "icon": "📝",
        "description": "The world's most popular CMS. "
                       "Runs with its own MySQL database in an isolated Docker network.",
        "category": "Web",
        "tags": ["cms", "blog", "website"],
        "url_port": 8080,
        "docs": "https://wordpress.org",
        "services": [
            {
                "name": "stax-wordpress-db",
                "image": "mysql:8.0",
                "ports": [],
                "volumes": [("{data}/mysql", "/var/lib/mysql")],
                "env": {"MYSQL_ROOT_PASSWORD": "staxrootpass",
                        "MYSQL_DATABASE": "wordpress",
                        "MYSQL_USER": "wordpress",
                        "MYSQL_PASSWORD": "staxwppass"},
                "network": "stax-wordpress-net",
            },
            {
                "name": "stax-wordpress",
                "image": "wordpress:latest",
                "ports": [("8080", "80")],
                "volumes": [("{data}/html", "/var/www/html")],
                "env": {"WORDPRESS_DB_HOST": "stax-wordpress-db",
                        "WORDPRESS_DB_USER": "wordpress",
                        "WORDPRESS_DB_PASSWORD": "staxwppass",
                        "WORDPRESS_DB_NAME": "wordpress"},
                "network": "stax-wordpress-net",
                "depends_on_delay": 10,
            },
        ],
        "networks": ["stax-wordpress-net"],
    },
    "nextcloud": {
        "startup_secs": 180,
        "name": "Nextcloud",
        "icon": "☁️",
        "description": "Self-hosted file sync and collaboration platform. "
                       "Files, calendars, contacts, video calls, and 200+ apps.",
        "category": "Productivity",
        "tags": ["files", "cloud", "collaboration", "storage"],
        "url_port": 8888,
        "docs": "https://nextcloud.com",
        "services": [
            {
                "name": "stax-nextcloud",
                "image": "nextcloud:latest",
                "ports": [("8888", "80")],
                "volumes": [("{data}/html", "/var/www/html"),
                             ("{data}/data", "/var/www/html/data")],
                "env": {"NEXTCLOUD_ADMIN_USER": "admin",
                        "NEXTCLOUD_ADMIN_PASSWORD": "changeme123"},
                "network": STAX_NET,
            }
        ],
    },
    "gitea": {
        "startup_secs": 60,
        "name": "Gitea",
        "icon": "🐙",
        "description": "Lightweight self-hosted Git service. Full GitHub-like experience — "
                       "repositories, issues, pull requests, CI/CD actions, and a web editor.",
        "category": "DevOps",
        "tags": ["git", "version-control", "devops", "ci-cd"],
        "url_port": 3010,
        "docs": "https://gitea.io",
        "hidden_ports": [2222],
        "services": [
            {
                "name": "stax-gitea-db",
                "image": "postgres:15-alpine",
                "ports": [],
                "volumes": [("{data}/postgres", "/var/lib/postgresql/data")],
                "env": {"POSTGRES_DB":       "gitea",
                        "POSTGRES_USER":     "gitea",
                        "POSTGRES_PASSWORD": "giteapass"},
                "network": "stax-gitea-net",
            },
            {
                "name": "stax-gitea",
                "image": "gitea/gitea:latest",
                "ports": [("3010", "3000"), ("2222", "22")],
                "volumes": [("{data}/gitea", "/data"),
                             ("/etc/timezone", "/etc/timezone:ro"),
                             ("/etc/localtime", "/etc/localtime:ro")],
                "env": {"USER_UID":                  "1000",
                        "USER_GID":                  "1000",
                        "GITEA__database__DB_TYPE":  "postgres",
                        "GITEA__database__HOST":     "stax-gitea-db:5432",
                        "GITEA__database__NAME":     "gitea",
                        "GITEA__database__USER":     "gitea",
                        "GITEA__database__PASSWD":   "giteapass",
                        "GITEA__server__HTTP_PORT":  "3000",
                        "GITEA__server__ROOT_URL":   "http://localhost:3010/",
                        "GITEA__server__SSH_PORT":   "2222",
                        "GITEA__server__DOMAIN":     "localhost"},
                "network": "stax-gitea-net",
                "depends_on_delay": 10,
            },
        ],
        "networks": ["stax-gitea-net"],
    },
}

CATEGORIES = ["All", "Privacy", "Media", "Productivity", "AI", "Web",
               "Communication", "Monitoring", "DevOps"]

# ── Docker helpers ────────────────────────────────────────────────────────────

def run(cmd: str, timeout=60) -> tuple:
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", 1
    except Exception as e:
        return "", str(e), 1


def docker(cmd: str, timeout=300) -> tuple:
    return run(f"docker {cmd}", timeout=timeout)


def docker_list_containers() -> list:
    """Return all containers (running + stopped) with details."""
    fmt = '{"id":"{{.ID}}","name":"{{.Names}}","image":"{{.Image}}","status":"{{.Status}}",' \
          '"state":"{{.State}}","ports":"{{.Ports}}","created":"{{.RunningFor}}"}'
    out, _, rc = docker(f'ps -a --format \'{fmt}\'')
    containers = []
    if rc == 0 and out:
        for line in out.splitlines():
            try:
                containers.append(json.loads(line))
            except Exception:
                pass
    return containers


def docker_stats() -> dict:
    """Return CPU/mem stats for running containers."""
    fmt = '{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'
    out, _, rc = docker(f'stats --no-stream --format "{fmt}"', timeout=15)
    stats = {}
    if rc == 0 and out:
        for line in out.splitlines():
            parts = line.split('\t')
            if len(parts) >= 3:
                name = parts[0].lstrip('/')
                stats[name] = {"cpu": parts[1], "mem": parts[2]}
    return stats


def docker_logs(name: str, tail=100) -> str:
    out, err, _ = docker(f"logs --tail={tail} {name} 2>&1")
    return out or err or "(no logs)"


def ensure_network(network: str) -> bool:
    _, _, rc = docker(f"network inspect {network}")
    if rc != 0:
        _, err, rc2 = docker(f"network create {network}")
        return rc2 == 0
    return True


def container_running(name: str) -> bool:
    out, _, rc = docker(f"inspect -f '{{{{.State.Running}}}}' {name} 2>/dev/null")
    return rc == 0 and out.strip() == "true"


def container_exists(name: str) -> bool:
    _, _, rc = docker(f"inspect {name}")
    return rc == 0


# ── Stack state persistence ───────────────────────────────────────────────────

def load_stacks() -> dict:
    if STACKS_FILE.exists():
        try:
            return json.loads(STACKS_FILE.read_text())
        except Exception:
            pass
    return {}


def save_stacks(data: dict):
    STACKS_FILE.write_text(json.dumps(data, indent=2))


def stack_status(slug: str) -> str:
    """running | starting | stopped | partial | not_installed"""
    state     = load_stacks()
    installed = slug in state

    # If we just finished installing, show "starting" while containers boot
    if slug in _starting_stacks:
        if time.time() < _starting_stacks[slug]:
            return "starting"
        else:
            del _starting_stacks[slug]

    svc_names = [s["name"] for s in STACKS[slug]["services"]]
    running   = sum(1 for n in svc_names if container_running(n))
    exists    = sum(1 for n in svc_names if container_exists(n))

    if not installed and exists == 0:
        return "not_installed"
    if running == len(svc_names):
        return "running"
    # Single container stacks that aren't running yet = stopped, not partial
    if running > 0 and len(svc_names) > 1:
        return "partial"
    if exists > 0 or installed:
        return "stopped"
    return "not_installed"


# ── Stack installer ───────────────────────────────────────────────────────────

def install_stack(slug: str, overrides: dict = None):
    """Run in a background thread. Streams progress via _install_logs."""
    if slug not in STACKS:
        _ilog(slug, f"ERROR: unknown stack '{slug}'")
        _install_state[slug] = "error"
        return

    _install_state[slug] = "installing"
    stack = STACKS[slug]
    overrides = overrides or {}
    # Always expand ~ to the real home directory — docker requires absolute paths
    raw_dir = overrides.get("data_dir", str(DATA_DIR / slug))
    data_base = Path(raw_dir).expanduser().resolve()
    data_base.mkdir(parents=True, exist_ok=True)

    _ilog(slug, f"Installing {stack['name']}…")

    # Create custom networks
    for net in stack.get("networks", []):
        _ilog(slug, f"Creating network {net}…")
        ensure_network(net)

    # Ensure default stax-net
    ensure_network(STAX_NET)

    # Install each service
    for svc in stack["services"]:
        name    = svc["name"]
        image   = svc["image"]
        network = svc.get("network", STAX_NET)
        delay   = svc.get("depends_on_delay", 0)

        # Remove existing container if present
        if container_exists(name):
            _ilog(slug, f"Removing existing {name}…")
            docker(f"rm -f {name}")

        # Pull image — capture output to detect if update was available
        _ilog(slug, f"Pulling {image}…")
        pull_out, pull_err, rc = docker(f"pull {image}", timeout=600)
        combined = (pull_out + pull_err).lower()
        if rc != 0:
            _ilog(slug, f"WARNING: pull failed: {pull_err[:200]}")
        elif "status: image is up to date" in combined or "image is up to date for" in combined:
            _ilog(slug, f"✓ {image} already up to date")
            if _update_status.get(slug) != "updated":
                _update_status[slug] = "up_to_date"
        elif "status: downloaded newer image" in combined or "downloaded newer image for" in combined:
            _ilog(slug, f"↑ {image} updated to latest")
            _update_status[slug] = "updated"
        else:
            # No clear status line — treat as up to date to avoid false positives
            _ilog(slug, f"✓ {image} pulled")
            if _update_status.get(slug) != "updated":
                _update_status[slug] = "up_to_date"

        # Build volume args
        vol_args = []
        for host_path, container_path in svc.get("volumes", []):
            host_path = host_path.replace("{data}", str(data_base))
            # Expand ~ but do NOT resolve symlinks — /etc/localtime is a symlink to a file
            host_path = str(Path(host_path).expanduser())
            p = Path(host_path)
            # Only mkdir for data paths we own — skip system files/sockets/symlinks
            is_system = host_path.startswith(("/etc/", "/var/run/", "/proc/",
                                               "/sys/", "/dev/", "/usr/", "/run/"))
            if not is_system and not p.exists():
                try:
                    p.mkdir(parents=True, exist_ok=True)
                except Exception as mk_err:
                    _ilog(slug, f"Note: could not create {host_path}: {mk_err}")
            vol_args.append(f"-v '{host_path}:{container_path}'")

        # Build port args
        port_args = []
        for host_port, container_port in svc.get("ports", []):
            port_args.append(f"-p {host_port}:{container_port}")

        # Build env args
        env_args = []
        for k, v in svc.get("env", {}).items():
            env_args.append(f"-e '{k}={v}'")

        # Extra args (e.g. --add-host)
        extra = " ".join(svc.get("extra_args", []))

        # Build full docker run command
        cmd_parts = [
            "run -d",
            f"--name {name}",
            f"--network {network}",
            "--restart unless-stopped",
            " ".join(vol_args),
            " ".join(port_args),
            " ".join(env_args),
            extra,
            image,
        ]
        cmd = " ".join(p for p in cmd_parts if p)

        if delay > 0:
            _ilog(slug, f"Waiting {delay}s for dependencies…")
            time.sleep(delay)

        _ilog(slug, f"Starting {name}…")
        out, err, rc = docker(cmd)
        if rc == 0:
            _ilog(slug, f"✓ {name} started (ID: {out[:12]})")
        else:
            _ilog(slug, f"✗ {name} failed: {err[:300]}")
            _install_state[slug] = "error"
            return

    # Save state
    state = load_stacks()
    state[slug] = {"installed_at": datetime.now().isoformat(), "data_dir": str(data_base)}
    save_stacks(state)

    # Auto-open firewall ports so service is immediately reachable
    _ilog(slug, "Opening firewall ports…")
    auto_open_stack_ports(slug)
    enable_ip_forward()

    # Mark as "starting" using per-stack startup time (heavy stacks need longer)
    startup_secs = stack.get("startup_secs", 60)
    _starting_stacks[slug] = time.time() + startup_secs
    _ilog(slug, f"Waiting up to {startup_secs}s for app to be ready…")

    # Commit update status — if every image was already current, mark as up-to-date
    if _update_status.get(slug) == "up_to_date":
        _update_available[slug] = False
        _ilog(slug, "✓ All images are up to date")
    elif _update_status.get(slug) == "updated":
        _update_available[slug] = False   # just pulled fresh — no pending update
        _ilog(slug, "✓ Images updated to latest")

    _ilog(slug, f"✓ {stack['name']} installed successfully!")
    if stack.get("url_port"):
        _ilog(slug, f"  Open at: http://localhost:{stack['url_port']}")
    _install_state[slug] = "done"


def remove_stack(slug: str):
    """Stop and remove all containers for a stack."""
    if slug not in STACKS:
        return {"ok": False, "error": "Unknown stack"}
    stack = STACKS[slug]
    for svc in reversed(stack["services"]):
        name = svc["name"]
        if container_exists(name):
            docker(f"rm -f {name}")
    # Remove custom networks
    for net in stack.get("networks", []):
        docker(f"network rm {net} 2>/dev/null")
    state = load_stacks()
    state.pop(slug, None)
    save_stacks(state)
    return {"ok": True}


def update_stack(slug: str):
    """Pull latest images and recreate containers."""
    if slug not in STACKS:
        return {"ok": False, "error": "Unknown stack"}
    threading.Thread(target=install_stack, args=(slug,), daemon=True).start()
    return {"ok": True}


def start_stack(slug: str):
    for svc in STACKS[slug]["services"]:
        docker(f"start {svc['name']} 2>/dev/null")
    return {"ok": True}


def stop_stack(slug: str):
    for svc in reversed(STACKS[slug]["services"]):
        docker(f"stop {svc['name']} 2>/dev/null")
    return {"ok": True}


def check_stack_ports(slug: str) -> dict:
    """Check if stack ports are free and not blocked by firewall."""
    if slug not in STACKS:
        return {"ok": False, "error": "Unknown stack"}
    import socket as _sock

    ufw_out, _, _  = run("ufw status 2>/dev/null")
    ufw_active     = "Status: active" in ufw_out

    results   = []
    conflicts = []
    blocked   = []

    for svc in STACKS[slug]["services"]:
        for host_port, _ in svc.get("ports", []):
            raw  = str(host_port).split("/")[0]
            if not raw.isdigit():
                continue
            port = int(raw)

            # Is something already listening on this port?
            in_use = False
            try:
                with _sock.create_connection(("127.0.0.1", port), timeout=0.5):
                    in_use = True
            except Exception:
                pass
            if in_use:
                conflicts.append(port)

            # Is UFW blocking it?
            if ufw_active and str(port) not in ufw_out:
                blocked.append(port)

            results.append({
                "port": port, "in_use": in_use,
                "ufw_blocked": ufw_active and str(port) not in ufw_out,
            })

    return {
        "ok":        True,
        "ports":     results,
        "conflicts": conflicts,
        "blocked":   blocked,
        "ufw_active": ufw_active,
        "clean":     not conflicts and not blocked,
    }


def open_firewall_port(port: int) -> dict:
    """Open a port in UFW — both INPUT and FORWARD (required for Docker)."""
    for prefix in ("", "sudo "):
        _, _, rc = run(f"{prefix}ufw allow {port}/tcp 2>/dev/null")
        run(f"{prefix}ufw route allow proto tcp from any to any port {port} 2>/dev/null")
        if rc == 0:
            run(f"{prefix}ufw reload 2>/dev/null")
            return {"ok": True}
    return {"ok": False,
            "cmd": f"sudo ufw allow {port}/tcp && sudo ufw route allow proto tcp from any to any port {port}"}


def auto_open_stack_ports(slug: str):
    """Automatically open UFW INPUT + ROUTE rules for a newly installed stack."""
    try:
        ufw_out, _, _ = run("ufw status 2>/dev/null")
        if "Status: active" not in ufw_out:
            return  # UFW not active — Docker iptables handles it
        for svc in STACKS.get(slug, {}).get("services", []):
            for host_port, _ in svc.get("ports", []):
                raw = str(host_port).split("/")[0]
                if raw.isdigit():
                    for prefix in ("", "sudo "):
                        _, _, rc = run(f"{prefix}ufw allow {raw}/tcp 2>/dev/null")
                        run(f"{prefix}ufw route allow proto tcp from any to any port {raw} 2>/dev/null")
                        if rc == 0:
                            break
        run("ufw reload 2>/dev/null || sudo ufw reload 2>/dev/null")
    except Exception:
        pass


def enable_ip_forward() -> bool:
    """Enable kernel IP forwarding so Docker containers are reachable."""
    try:
        run("sysctl -w net.ipv4.ip_forward=1 2>/dev/null || sudo sysctl -w net.ipv4.ip_forward=1 2>/dev/null")
        run("grep -q net.ipv4.ip_forward=1 /etc/sysctl.conf 2>/dev/null || "
            "echo 'net.ipv4.ip_forward=1' | sudo tee -a /etc/sysctl.conf >/dev/null 2>&1")
        return True
    except Exception:
        return False


def fix_network_access() -> dict:
    """Open UFW ports for all installed stacks + enable IP forwarding."""
    try:
        enable_ip_forward()
        ufw_out, _, _ = run("ufw status 2>/dev/null")
        ufw_active = "Status: active" in ufw_out
        opened, failed = [], []
        for slug in load_stacks():
            if slug not in STACKS:
                continue
            for svc in STACKS[slug]["services"]:
                for hp, _ in svc.get("ports", []):
                    raw = str(hp).split("/")[0]
                    if raw.isdigit():
                        port = int(raw)
                        if ufw_active:
                            r = open_firewall_port(port)
                            (opened if r["ok"] else failed).append(port)
                        else:
                            opened.append(port)
        if ufw_active:
            run("ufw reload 2>/dev/null || sudo ufw reload 2>/dev/null")
        return {"ok": True, "opened": opened, "failed": failed,
                "public_ip": get_public_ip(), "ufw_active": ufw_active}
    except Exception as e:
        return {"ok": False, "error": str(e), "opened": [], "failed": []}



def get_public_ip() -> str:
    """Return server public IP — local socket first, no hanging external calls."""
    try:
        import socket as _s
        with _s.socket(_s.AF_INET, _s.SOCK_DGRAM) as s:
            s.settimeout(1)
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        pass
    import urllib.request
    for url in ("https://api.ipify.org", "https://icanhazip.com"):
        try:
            return urllib.request.urlopen(url, timeout=2).read().decode().strip()
        except Exception:
            pass
    return ""


def get_network_status() -> dict:
    """Instant status — NO HTTP checks (fast path for UI load)."""
    try:
        ufw_out, _, _ = run("ufw status 2>/dev/null")
        ufw_active    = "Status: active" in ufw_out
    except Exception:
        ufw_out, ufw_active = "", False
    try:
        fwd_out, _, _ = run("sysctl net.ipv4.ip_forward 2>/dev/null")
        ip_fwd = "= 1" in fwd_out
    except Exception:
        ip_fwd = False
    try:
        public_ip = get_public_ip()
    except Exception:
        public_ip = ""
    port_list = []
    try:
        for slug, s in [(sl, STACKS[sl]) for sl in load_stacks() if sl in STACKS]:
            hidden = set(str(p) for p in s.get("hidden_ports", []))
            for svc in s["services"]:
                for hp, _ in svc.get("ports", []):
                    raw = str(hp).split("/")[0]
                    if raw.isdigit() and raw not in hidden:
                        port_list.append({
                            "port":      int(raw),
                            "stack":     s["name"],
                            "slug":      slug,
                            "ufw_open":  not ufw_active or raw in ufw_out,
                            "http_ok":   None,
                            "http_code": None,
                        })
    except Exception:
        pass
    return {"ok": True, "ufw_active": ufw_active, "ip_forward": ip_fwd,
            "public_ip": public_ip, "stack_ports": port_list}


def check_ports_async() -> dict:
    """HTTP check all installed stack ports (slow path, called separately)."""
    try:
        ufw_out, _, _ = run("ufw status 2>/dev/null")
        ufw_active = "Status: active" in ufw_out
        port_specs = []
        for slug, s in [(sl, STACKS[sl]) for sl in load_stacks() if sl in STACKS]:
            hidden = set(str(p) for p in s.get("hidden_ports", []))
            for svc in s["services"]:
                for hp, _ in svc.get("ports", []):
                    raw = str(hp).split("/")[0]
                    if raw.isdigit() and raw not in hidden:
                        port_specs.append((slug, s["name"], int(raw), ufw_active, ufw_out))
    except Exception:
        return {"ok": True, "stack_ports": []}

    import concurrent.futures
    def _chk(args):
        slug, name, port, ufw_active, ufw_out = args
        try: http_ok, code = _http_check(port)
        except Exception: http_ok, code = False, 0
        return {"port": port, "stack": name, "slug": slug,
                "ufw_open": not ufw_active or str(port) in ufw_out,
                "http_ok": http_ok, "http_code": code}

    results = []
    if port_specs:
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                futures = {ex.submit(_chk, s): s for s in port_specs}
                for fut in concurrent.futures.as_completed(futures, timeout=20):
                    try: results.append(fut.result())
                    except Exception:
                        sp = futures[fut]
                        results.append({"port": sp[2], "stack": sp[1], "slug": sp[0],
                                        "ufw_open": True, "http_ok": False, "http_code": 0})
        except Exception:
            results = [_chk(s) for s in port_specs]
    return {"ok": True, "stack_ports": results}



def _http_check(port: int) -> tuple:
    """Try an HTTP GET to localhost:PORT. Returns (ok, status_code)."""
    import urllib.request, urllib.error
    try:
        req  = urllib.request.Request(
            f"http://127.0.0.1:{port}/",
            headers={"User-Agent": "STAX-MNGR-healthcheck/1.0"}
        )
        resp = urllib.request.urlopen(req, timeout=3)
        return True, resp.status
    except urllib.error.HTTPError as e:
        return True, e.code   # server responded — it's alive
    except Exception:
        return False, 0


def diagnose_stack(slug: str) -> dict:
    """Deep diagnostic: check containers, ports, HTTP, logs."""
    if slug not in STACKS:
        return {"ok": False, "error": "Unknown stack"}
    stack = STACKS[slug]
    services_info = []
    for svc in stack["services"]:
        name    = svc["name"]
        exists  = container_exists(name)
        running = container_running(name) if exists else False
        logs    = ""
        if exists:
            logs_out, logs_err, _ = docker(f"logs --tail=30 {name} 2>&1")
            logs = (logs_out + logs_err).strip()[-2000:]
        port_checks = []
        for host_port, _ in svc.get("ports", []):
            raw = str(host_port).split("/")[0]
            if raw.isdigit():
                port = int(raw)
                http_ok, http_code = _http_check(port)
                port_checks.append({
                    "port": port, "http_ok": http_ok, "http_code": http_code
                })
        services_info.append({
            "name":    name,
            "running": running,
            "exists":  exists,
            "logs":    logs,
            "ports":   port_checks,
        })
    return {"ok": True, "services": services_info, "public_ip": get_public_ip()}


# ── HTTP Server ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"

        if path == "/":
            self._raw(200, "text/html; charset=utf-8", HTML.encode())
        elif path == "/api/containers":
            containers = docker_list_containers()
            stats      = docker_stats()
            for c in containers:
                c["name"] = c["name"].lstrip("/")
                n = c["name"]
                if n in stats:
                    c["cpu"] = stats[n]["cpu"]
                    c["mem"] = stats[n]["mem"]
            self._json({"ok": True, "containers": containers})
        elif path == "/api/stacks":
            result = {}
            for slug, s in STACKS.items():
                result[slug] = {
                    "name": s["name"], "icon": s["icon"],
                    "description": s["description"], "category": s["category"],
                    "tags": s["tags"], "url_port": s["url_port"],
                    "docs": s["docs"], "status": stack_status(slug),
                    "update_status":   _update_status.get(slug, None),
                    "update_available": _update_available.get(slug, None),
                    "startup_secs":    s.get("startup_secs", 60),
                }
            self._json({"ok": True, "stacks": result})
        elif path.startswith("/api/stacks/") and path.endswith("/ports"):
            slug = path.split("/")[3]
            self._json(check_stack_ports(slug))
        elif path.startswith("/api/stacks/") and path.endswith("/health"):
            slug = path.split("/")[3]
            if slug not in STACKS:
                self._json({"ok": False, "ready": False}); return
            port = STACKS[slug].get("url_port")
            if not port:
                self._json({"ok": True, "ready": True, "http_code": 0}); return
            http_ok, code = _http_check(port)
            self._json({"ok": True, "ready": http_ok, "http_code": code,
                        "url": f"http://localhost:{port}"})
        elif path.startswith("/api/stacks/") and path.endswith("/diagnose"):
            slug = path.split("/")[3]
            self._json(diagnose_stack(slug))
        elif path.startswith("/api/stacks/") and path.endswith("/log"):
            slug = path.split("/")[3]
            self._json({"ok": True, "log": _install_logs.get(slug, []),
                        "state": _install_state.get(slug, "idle")})
        elif path.startswith("/api/containers/") and path.endswith("/logs"):
            name = path.split("/")[3]
            self._json({"ok": True, "logs": docker_logs(name)})
        elif path == "/api/network/status":
            try:
                self._json(get_network_status())
            except Exception as e:
                self._json({"ok": True, "stack_ports": [], "ufw_active": False,
                            "ip_forward": False, "public_ip": "", "error": str(e)})
        elif path == "/api/network/check-ports":
            try:
                self._json(check_ports_async())
            except Exception as e:
                self._json({"ok": True, "stack_ports": [], "error": str(e)})
        else:
            self._raw(404, "text/plain", b"Not found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length).decode()) if length else {}
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")

        # Container actions
        if path.startswith("/api/containers/"):
            parts = path.split("/")
            name, action = parts[3], parts[4]
            if action == "start":
                _, err, rc = docker(f"start {name}")
                self._json({"ok": rc == 0, "error": err})
            elif action == "stop":
                _, err, rc = docker(f"stop {name}")
                self._json({"ok": rc == 0, "error": err})
            elif action == "restart":
                _, err, rc = docker(f"restart {name}")
                self._json({"ok": rc == 0, "error": err})
            elif action == "remove":
                _, err, rc = docker(f"rm -f {name}")
                self._json({"ok": rc == 0, "error": err})
            else:
                self._json({"ok": False, "error": "Unknown action"})
            return

        # Stack actions
        if path.startswith("/api/stacks/"):
            parts  = path.split("/")
            slug   = parts[3]
            action = parts[4] if len(parts) > 4 else ""
            if action == "install":
                _install_logs[slug] = []
                _install_state[slug] = "queued"
                threading.Thread(
                    target=install_stack, args=(slug, body), daemon=True
                ).start()
                self._json({"ok": True})
            elif action == "remove":
                self._json(remove_stack(slug))
            elif action == "update":
                _install_logs[slug] = []
                threading.Thread(
                    target=install_stack, args=(slug,), daemon=True
                ).start()
                self._json({"ok": True})
            elif action == "start":
                self._json(start_stack(slug))
            elif action == "stop":
                self._json(stop_stack(slug))
            else:
                self._json({"ok": False, "error": "Unknown action"})
            return

        if path == "/api/ports/open":
            port = body.get("port")
            if not port:
                self._json({"ok": False, "error": "Port required"}); return
            self._json(open_firewall_port(int(port))); return

        if path == "/api/network/fix":
            self._json(fix_network_access()); return

        self._raw(404, "text/plain", b"Not found")

    def _json(self, data):
        body = json.dumps(data).encode()
        self._raw(200, "application/json", body)

    def _raw(self, code, ct, body):
        try:
            self.send_response(code)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client disconnected before response finished


# ── Embedded UI ───────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>KillTheHost - STAX-MNGR</title>
    <link rel="shortcut icon" href="https://www.phdesigns.net/img/favicon.ico" type="image/x-icon">
    <link rel="icon" href="https://www.phdesigns.net/img/favicon.ico" type="image/x-icon">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg:       #1a1a1a;
  --sidebar:  #242424;
  --card:     #2a2a2a;
  --card2:    #222222;
  --border:   #383838;
  --text:     #e8e8e8;
  --dim:      #888;
  --muted:    #555;
  --green:    #10a37f;
  --green-bg: #0c1f18;
  --green-dim:#1e4a30;
  --blue:     #3b82f6;
  --amber:    #f59e0b;
  --red:      #ef4444;
  --purple:   #a78bfa;
  --cyan:     #06b6d4;
  --log-bg:   #141414;
  --inp:      #1e1e1e;
}
::-webkit-scrollbar { width: 4px; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 2px; }

body {
  font-family: ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  background: var(--bg); color: var(--text);
  display: flex; flex-direction: column; height: 100vh; overflow: hidden;
  font-size: 13px; line-height: 1.5;
}

/* ── Top bar ─────────────────────────────────────────── */
.topbar {
  padding: 10px 20px; border-bottom: 1px solid var(--border);
  background: var(--sidebar); display: flex; align-items: center;
  justify-content: space-between; gap: 12px; flex-wrap: wrap; flex-shrink: 0;
}
.brand { display: flex; align-items: flex-start; gap: 10px; }
.kth-mark {
  width: 34px; height: 34px; border-radius: 9px;
  background: linear-gradient(145deg,#ff56b9 0%,#ef63d6 62%,#c86bff 100%);
  display: flex; align-items: center; justify-content: center;
  font-family: "Menlo", "Consolas", monospace; font-size: 13px; font-weight: 700;
  color: #fff; letter-spacing: -.6px; flex-shrink: 0;
  box-shadow: inset 0 0 0 1px rgba(255,255,255,.14);
  margin-top: 1px;
}
.brand-main { display: flex; flex-direction: column; gap: 1px; }
.kth-logo { display: flex; align-items: center; gap: 0; font-size: 18px; font-weight: 800; line-height: 1; }
.kth-word { color: #f4f5fb; letter-spacing: -.35px; }
.kth-word.the {
  background: linear-gradient(135deg,#ff5ab8 0%,#f468cd 55%,#c96dff 100%);
  -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent;
}
.brand-app { font-size: 13px; font-weight: 700; color: var(--text); letter-spacing: .32px; }
.brand-suite { font-size: 10px; color: var(--muted); letter-spacing: .22px; text-transform: uppercase; }
.hdr-right { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.status-pill {
  display: flex; align-items: center; gap: 6px; padding: 4px 10px;
  border-radius: 20px; font-size: 11px; border: 1px solid var(--border);
  background: var(--card);
}
#live-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--green);
            box-shadow: 0 0 5px var(--green); animation: pulse 2.5s ease-in-out infinite; }
@keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.3; } }

/* ── Shell ───────────────────────────────────────────── */
.shell { display: flex; flex: 1; overflow: hidden; }

/* ── Sidebar nav ─────────────────────────────────────── */
.sidebar {
  width: 188px; flex-shrink: 0; background: var(--sidebar);
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column; padding: 12px 8px; gap: 2px;
}
.nav-item {
  display: flex; align-items: center; gap: 10px;
  padding: 8px 12px; border-radius: 6px;
  cursor: pointer; transition: background .15s, color .15s;
  color: var(--dim); font-size: 13px; font-weight: 500;
  border: none; background: transparent; width: 100%; text-align: left;
}
.nav-item:hover { background: var(--card); color: var(--text); }
.nav-item.active { background: var(--green-bg); color: var(--green); }
.nav-item .nav-ico { font-size: 14px; flex-shrink: 0; }
.nav-badge { margin-left: auto; background: var(--border); color: var(--dim);
             font-size: 10px; padding: 1px 6px; border-radius: 10px; }
.nav-badge.live { background: var(--green-bg); color: var(--green); }
.sidebar-footer { margin-top: auto; padding-top: 10px; border-top: 1px solid var(--border); }
.sidebar-footer p { font-size: 10px; color: var(--muted); text-align: center; margin-top: 6px; }

/* ── Main content ────────────────────────────────────── */
.main { flex: 1; overflow-y: auto; display: flex; flex-direction: column; }
.panel { display: none; flex-direction: column; flex: 1; padding: 20px; gap: 16px; }
.panel.active { display: flex; }

/* ── Toolbar ─────────────────────────────────────────── */
.toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.panel-title { font-size: 18px; font-weight: 700; color: var(--text); }
.panel-sub { font-size: 12px; color: var(--muted); margin-top: 2px; }
.search-wrap { position: relative; flex: 1; min-width: 180px; max-width: 320px; }
.search-wrap input {
  width: 100%; background: var(--inp); border: 1px solid var(--border);
  border-radius: 6px; padding: 7px 10px 7px 32px;
  color: var(--text); font-size: 12px; outline: none;
}
.search-wrap input:focus { border-color: var(--green); }
.search-ico { position: absolute; left: 10px; top: 50%; transform: translateY(-50%);
               color: var(--muted); font-size: 14px; }
.filter-tabs { display: flex; gap: 4px; flex-wrap: wrap; }
.ftab {
  padding: 5px 12px; border-radius: 6px; font-size: 11px; font-weight: 600;
  cursor: pointer; border: 1px solid var(--border); background: transparent;
  color: var(--muted); transition: all .15s;
}
.ftab:hover { background: var(--card); color: var(--text); }
.ftab.active { background: var(--green-bg); color: var(--green); border-color: var(--green); }
.ml-auto { margin-left: auto; }

/* ── Buttons ─────────────────────────────────────────── */
.btn {
  padding: 6px 14px; border-radius: 7px; font-size: 12px; font-weight: 600;
  cursor: pointer; border: none; transition: filter .15s, transform .1s;
  display: inline-flex; align-items: center; gap: 5px;
}
.btn:disabled { opacity: .35; cursor: not-allowed; }
.btn:not(:disabled):hover { filter: brightness(1.12); }
.btn:not(:disabled):active { transform: scale(.97); }
.btn-green  { background: var(--green); color: #000; }
.btn-ghost  { background: var(--card); color: var(--text); border: 1px solid var(--border); }
.btn-ghost:not(:disabled):hover { border-color: var(--muted); }
.btn-red    { background: var(--red); color: #fff; }
.btn-amber  { background: var(--amber); color: #000; }
.btn-sm     { padding: 4px 10px; font-size: 11px; }
.btn-icon   { padding: 5px 8px; }

/* ── Container table ─────────────────────────────────── */
.ctable-wrap { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; }
thead th {
  text-align: left; font-size: 11px; font-weight: 600; text-transform: uppercase;
  letter-spacing: .5px; color: var(--muted); padding: 8px 12px;
  border-bottom: 1px solid var(--border); background: var(--inp);
}
tbody tr { border-bottom: 1px solid var(--border); transition: background .1s; }
tbody tr:hover { background: var(--inp); }
tbody td { padding: 10px 12px; vertical-align: middle; }
.cname { font-weight: 600; color: var(--text); font-size: 13px; }
.cimage { font-size: 11px; color: var(--muted); margin-top: 2px; }
.cports { font-size: 11px; color: var(--dim); font-family: monospace; }
.cbadge {
  display: inline-block; padding: 2px 8px; border-radius: 20px; font-size: 11px; font-weight: 600;
}
.cbadge-run  { background: var(--green-bg); color: var(--green); }
.cbadge-stop { background: rgba(100,116,139,.15); color: var(--muted); }
.cbadge-stax { background: rgba(59,130,246,.12); color: var(--blue); }
.cpu-mem { font-size: 11px; color: var(--dim); font-family: monospace; }
.action-btns { display: flex; gap: 4px; }

/* ── Stack grid ──────────────────────────────────────── */
.stack-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 14px;
}
.stack-card {
  background: var(--card); border: 1px solid var(--border); border-radius: 8px;
  padding: 16px; display: flex; flex-direction: column; gap: 10px;
  transition: border-color .15s;
}
.stack-card:hover { border-color: var(--dim); }
.stack-card.installed { border-color: var(--green-dim); }
.stack-card.running   { border-color: var(--green); }
.sc-head { display: flex; align-items: flex-start; gap: 12px; }
.sc-icon { font-size: 28px; flex-shrink: 0; line-height: 1; margin-top: 2px; }
.sc-info { flex: 1; min-width: 0; }
.sc-name { font-size: 14px; font-weight: 700; color: var(--text); }
.sc-cat  { font-size: 10px; color: var(--muted); text-transform: uppercase;
           letter-spacing: .5px; margin-top: 1px; }
.sc-desc { font-size: 12px; color: var(--dim); line-height: 1.5; }
.sc-meta { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.tag { font-size: 10px; background: var(--card2); color: var(--muted);
       padding: 2px 7px; border-radius: 10px; border: 1px solid var(--border); }
.sc-port { margin-left: auto; font-size: 11px; font-family: monospace;
           color: var(--green); font-weight: 600; }
.sc-status { font-size: 11px; font-weight: 600; }
.sc-status.running   { color: var(--green); }
.sc-status.starting,
.sc-status.partial   { color: var(--amber); animation: upd-pulse 1.5s ease-in-out infinite; }
.sc-status.stopped   { color: var(--muted); }
.sc-status.not_installed { color: var(--muted); }
.sc-actions { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; }

/* ── Install log (inline on card) ────────────────────── */
.install-log {
  background: var(--log-bg); border: 1px solid var(--border); border-radius: 6px;
  padding: 10px 12px; font-family: "Menlo","Consolas",monospace; font-size: 11px;
  line-height: 1.65; max-height: 160px; overflow-y: auto; color: var(--dim);
  white-space: pre-wrap; word-break: break-all; display: none;
}
.update-badge {
  display: inline-flex; align-items: center; gap: 4px;
  padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: 700;
  background: rgba(245,158,11,.15); color: var(--amber);
  border: 1px solid rgba(245,158,11,.3); animation: upd-pulse 2s ease-in-out infinite;
}
@keyframes upd-pulse { 0%,100% { opacity:1; } 50% { opacity:.5; } }

/* ── Log viewer ──────────────────────────────────────── */
.log-box {
  background: var(--log-bg); border: 1px solid var(--border); border-radius: 8px;
  padding: 12px 14px; font-family: "Menlo","Consolas",monospace; font-size: 11.5px;
  line-height: 1.7; max-height: 260px; overflow-y: auto; color: var(--dim);
  white-space: pre-wrap; word-break: break-all;
}
.log-ok   { color: var(--green); }
.log-err  { color: var(--red); }
.log-warn { color: var(--amber); }

/* ── Stats bar ───────────────────────────────────────── */
.stats-row { display: grid; grid-template-columns: repeat(auto-fit,minmax(140px,1fr)); gap: 12px; }
.stat-card {
  background: var(--card); border: 1px solid var(--border); border-radius: 10px;
  padding: 14px 16px;
}
.stat-val { font-size: 28px; font-weight: 800; color: var(--green); line-height: 1; }
.stat-lbl { font-size: 11px; color: var(--muted); margin-top: 4px; text-transform: uppercase;
             letter-spacing: .5px; }

/* ── Empty state ─────────────────────────────────────── */
.empty { text-align: center; padding: 60px 20px; color: var(--muted); }
.empty .ico { font-size: 40px; margin-bottom: 12px; }
.empty p { font-size: 13px; }

/* ── Toast ───────────────────────────────────────────── */
#toast { position: fixed; bottom: 20px; right: 20px; display: flex;
          flex-direction: column; gap: 6px; z-index: 9999; }
.tmsg {
  background: var(--card); border: 1px solid var(--border); border-radius: 6px;
  padding: 9px 14px; font-size: 12px; color: var(--text); opacity: 0;
  transform: translateY(6px); transition: all .2s; max-width: 280px;
}
.tmsg.ok  { border-color: var(--green); color: var(--green); }
.tmsg.err { border-color: var(--red);   color: var(--red);   }
.tmsg.show { opacity: 1; transform: translateY(0); }

/* ── Scrollable log tab ──────────────────────────────── */
.log-controls { display: flex; gap: 8px; align-items: center; }
.log-sel {
  background: var(--inp); border: 1px solid var(--border); border-radius: 6px;
  padding: 5px 10px; color: var(--text); font-size: 12px; outline: none; flex: 1; max-width: 240px;
}
#full-log { flex: 1; min-height: 300px; }
</style>
</head>
<body>

<div class="topbar">
  <div class="brand">
    <div class="kth-mark">&gt;_</div>
    <div class="brand-main">
      <div class="kth-logo">
        <span class="kth-word">Kill</span><span class="kth-word the">The</span><span class="kth-word">Host</span>
      </div>
      <div class="brand-app">STAX-MNGR</div>
      <div class="brand-suite">KillTheHost Suite · Stack Manager</div>
    </div>
  </div>
  <div class="hdr-right">
    <div class="status-pill">
      <div id="live-dot"></div>
      <span id="live-text">Live</span>
    </div>
  </div>
</div>

<div class="shell">
  <!-- Sidebar -->
  <nav class="sidebar">
    <button class="nav-item active" onclick="tab('containers',this)">
      <span class="nav-ico">🐳</span> Containers
      <span class="nav-badge live" id="nb-running">0</span>
    </button>
    <button class="nav-item" onclick="tab('stacks',this)">
      <span class="nav-ico">📦</span> Stacks
      <span class="nav-badge" id="nb-installed">0</span>
    </button>
    <button class="nav-item" onclick="tab('logs',this)">
      <span class="nav-ico">📋</span> Logs
    </button>
    <button class="nav-item" onclick="tab('network',this)">
      <span class="nav-ico">🌐</span> Network
      <span class="nav-badge" id="nb-blocked" style="display:none;background:rgba(239,68,68,.2);color:var(--red)">!</span>
    </button>

    <div class="sidebar-footer">
      <button class="btn btn-ghost" style="width:100%;font-size:11px" onclick="refreshAll()">↻ Refresh</button>
      <p>STAX-MNGR v1.0<br>KillTheHost</p>
    </div>
  </nav>

  <!-- Main panels -->
  <div class="main">

    <!-- Containers panel -->
    <div class="panel active" id="panel-containers">
      <div>
        <div class="panel-title">Docker Containers</div>
        <div class="panel-sub">All containers on this system — running and stopped</div>
      </div>
      <div class="toolbar">
        <div class="search-wrap">
          <span class="search-ico">⌕</span>
          <input id="c-search" placeholder="Search containers…" oninput="filterContainers()">
        </div>
        <div class="filter-tabs">
          <button class="ftab active" onclick="setCFilter('all',this)">All</button>
          <button class="ftab" onclick="setCFilter('running',this)">Running</button>
          <button class="ftab" onclick="setCFilter('stopped',this)">Stopped</button>
          <button class="ftab" onclick="setCFilter('stax',this)">STAX</button>
        </div>
        <button class="btn btn-ghost btn-sm" id="restart-all-btn" onclick="restartAllContainers()" title="Restart all running containers">↺ Restart All STAX</button>
        <button class="btn btn-ghost btn-sm ml-2" id="crefresh-btn" onclick="refreshContainers()">↻</button>
      </div>

      <div class="stats-row" id="c-stats-row">
        <div class="stat-card"><div class="stat-val" id="stat-total">—</div><div class="stat-lbl">Total Containers</div></div>
        <div class="stat-card"><div class="stat-val" id="stat-run">—</div><div class="stat-lbl">Running</div></div>
        <div class="stat-card"><div class="stat-val" id="stat-stop">—</div><div class="stat-lbl">Stopped</div></div>
        <div class="stat-card"><div class="stat-val" id="stat-stax">—</div><div class="stat-lbl">STAX Managed</div></div>
      </div>

      <div class="ctable-wrap">
        <table>
          <thead>
            <tr>
              <th>Container</th>
              <th>Status</th>
              <th>Ports</th>
              <th>CPU / Mem</th>
              <th>Uptime</th>
              <th style="text-align:right">Actions</th>
            </tr>
          </thead>
          <tbody id="c-tbody">
            <tr><td colspan="6" style="text-align:center;padding:40px;color:var(--muted)">Loading…</td></tr>
          </tbody>
        </table>
      </div>

      <!-- Container log viewer -->
      <div id="clog-panel" style="display:none;flex-direction:column;gap:8px">
        <div style="display:flex;align-items:center;gap:8px">
          <b id="clog-title" style="font-size:12px;color:var(--dim)">Logs</b>
          <button class="btn btn-ghost btn-sm ml-auto" onclick="document.getElementById('clog-panel').style.display='none'">✕ Close</button>
          <button class="btn btn-ghost btn-sm" onclick="refreshCLog()">↻</button>
        </div>
        <div class="log-box" id="clog-box"></div>
      </div>
    </div>

    <!-- Stacks panel -->
    <div class="panel" id="panel-stacks">
      <div>
        <div class="panel-title">Stack Installer</div>
        <div class="panel-sub">Deploy pre-configured self-hosted applications with one click</div>
      </div>
      <div class="toolbar">
        <div class="search-wrap">
          <span class="search-ico">⌕</span>
          <input id="s-search" placeholder="Search stacks…" oninput="filterStacks()">
        </div>
        <div class="filter-tabs" id="cat-tabs">
          <button class="ftab active" onclick="setSFilter('All',this)">All</button>
          <button class="ftab" onclick="setSFilter('Privacy',this)">🛡 Privacy</button>
          <button class="ftab" onclick="setSFilter('Media',this)">🎬 Media</button>
          <button class="ftab" onclick="setSFilter('Productivity',this)">⚡ Productivity</button>
          <button class="ftab" onclick="setSFilter('AI',this)">🤖 AI</button>
          <button class="ftab" onclick="setSFilter('Web',this)">🌐 Web</button>
          <button class="ftab" onclick="setSFilter('Monitoring',this)">📡 Monitor</button>
          <button class="ftab" onclick="setSFilter('DevOps',this)">🔧 DevOps</button>
        </div>
      </div>
      <div class="stack-grid" id="stack-grid">
        <div class="empty"><div class="ico">📦</div><p>Loading stacks…</p></div>
      </div>
    </div>

    <!-- Logs panel -->
    <div class="panel" id="panel-logs">
      <div>
        <div class="panel-title">Container Logs</div>
        <div class="panel-sub">Live log output from any Docker container</div>
      </div>
      <div class="log-controls">
        <select class="log-sel" id="log-container-sel" onchange="loadFullLog()">
          <option value="">— Select a container —</option>
        </select>
        <button class="btn btn-ghost btn-sm" onclick="loadFullLog()">↻ Refresh</button>
        <button class="btn btn-ghost btn-sm" onclick="document.getElementById('full-log').innerHTML=''">🗑 Clear</button>
      </div>
      <div class="log-box" id="full-log" style="flex:1;min-height:400px;max-height:none">Select a container above to view logs.</div>
    </div>

    <!-- Network panel -->
    <div class="panel" id="panel-network">
      <div>
        <div class="panel-title">Network Access</div>
        <div class="panel-sub">Firewall, port forwarding, and access URLs for installed stacks</div>
      </div>

      <!-- Quick fix bar -->
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;
                  background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px 16px">
        <div>
          <div style="font-size:13px;font-weight:700">🔧 Fix All Network Access</div>
          <div style="font-size:11px;color:var(--muted);margin-top:2px">
            Opens UFW firewall ports for all installed stacks and enables IP forwarding
          </div>
        </div>
        <button class="btn btn-green ml-auto" id="fix-net-btn" onclick="fixNetwork()">🔧 Fix Network</button>
      </div>

      <!-- Status cards -->
      <div class="stats-row" id="net-stats">
        <div class="stat-card"><div class="stat-val" id="net-ip">—</div><div class="stat-lbl">Public IP</div></div>
        <div class="stat-card"><div class="stat-val" id="net-ufw">—</div><div class="stat-lbl">Firewall (UFW)</div></div>
        <div class="stat-card"><div class="stat-val" id="net-fwd">—</div><div class="stat-lbl">IP Forwarding</div></div>
        <div class="stat-card"><div class="stat-val" id="net-blocked">—</div><div class="stat-lbl">Blocked Ports</div></div>
      </div>

      <!-- Port table -->
      <div class="ctable-wrap">
        <table>
          <thead>
            <tr>
              <th>Stack</th>
              <th>Port</th>
              <th>Server reachable?</th>
              <th>Local URL</th>
              <th>Public URL</th>
              <th>Firewall</th>
              <th style="text-align:right">Actions</th>
            </tr>
          </thead>
          <tbody id="net-tbody">
            <tr><td colspan="7" style="text-align:center;padding:30px;color:var(--muted)">Loading…</td></tr>
          </tbody>
        </table>
      </div>

      <!-- Cloud firewall note -->
      <div style="background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.25);border-radius:8px;padding:12px 16px;font-size:12px;color:var(--dim)">
        <b style="color:var(--amber)">☁ Cloud firewall note:</b>
        If you're on Hetzner, DigitalOcean, Vultr, or AWS — they have a <b>cloud-level firewall</b>
        separate from UFW. Even if UFW allows a port, your cloud provider's security group may block it.
        Open ports in your provider's control panel too:
        <span style="color:var(--muted)">Hetzner → Firewall rules · DigitalOcean → Networking → Firewalls · Vultr → Firewall Groups</span>
      </div>
    </div>

  </div>
</div>

<div id="toast"></div>

<script>
// ── State ─────────────────────────────────────────────────────────────────
let allContainers = [];
let allStacks     = {};
let cFilter       = "all";
let sFilter       = "All";
let curCLogName   = null;

// ── Init ──────────────────────────────────────────────────────────────────
window.addEventListener("DOMContentLoaded", () => {
  loadContainers();
  loadStacks();
  setInterval(loadContainers, 8000);
  setInterval(loadStacks, 15000);  // refresh stacks + badge every 15s
});

// ── Navigation ────────────────────────────────────────────────────────────
function tab(id, btn) {
  document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
  document.querySelectorAll(".nav-item").forEach(b => b.classList.remove("active"));
  document.getElementById("panel-" + id).classList.add("active");
  btn.classList.add("active");
  if (id === "logs") populateLogSel();
  if (id === "network") loadNetwork();
}

async function refreshAll() {
  const btn = document.querySelector('.sidebar-footer .btn');
  if (btn) { btn.textContent = '⟳ Refreshing…'; btn.disabled = true; }
  await Promise.all([loadContainers(), loadStacks()]);
  if (btn) { btn.textContent = '↻ Refresh'; btn.disabled = false; }
}

async function refreshContainers() {
  const btn = document.getElementById('crefresh-btn');
  if (btn) { btn.textContent = '⟳'; btn.disabled = true; }
  await loadContainers();
  if (btn) { btn.textContent = '↻'; btn.disabled = false; }
}

async function restartAllContainers() {
  const targets = allContainers.filter(c => c.name.startsWith('stax-') && c.state === 'running');
  if (targets.length === 0) { toast('No running STAX containers to restart.', 'info'); return; }
  if (!confirm(`Restart all ${targets.length} running STAX container(s)?`)) return;
  const btn = document.getElementById('restart-all-btn');
  if (btn) { btn.textContent = '⟳ Restarting…'; btn.disabled = true; }
  let ok = 0, fail = 0, failNames = [];
  for (const c of targets) {
    try {
      const r = await api(`/api/containers/${c.name}/restart`, 'POST');
      if (r?.ok) { ok++; } else { fail++; failNames.push(c.name); }
    } catch { fail++; failNames.push(c.name); }
  }
  if (btn) { btn.textContent = '↺ Restart All STAX'; btn.disabled = false; }
  if (fail) {
    toast(`Restarted ${ok}, failed ${fail}: ${failNames.join(', ')}`, 'err');
  } else {
    toast(`Restarted all ${ok} STAX container(s).`, 'ok');
  }
  await loadContainers();
}

// ── API helper ────────────────────────────────────────────────────────────
async function api(path, method="GET", body=null) {
  try {
    const opts = { method, headers: {} };
    if (body) { opts.body = JSON.stringify(body); opts.headers["Content-Type"] = "application/json"; }
    const r = await fetch(path, opts);
    return await r.json();
  } catch { return null; }
}

function toast(msg, type="") {
  const box = document.getElementById("toast");
  const el  = document.createElement("div");
  el.className = "tmsg " + type;
  el.textContent = msg;
  box.appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
  setTimeout(() => { el.classList.remove("show"); setTimeout(() => el.remove(), 300); }, 3500);
}

function esc(s) {
  return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

// ── Containers ────────────────────────────────────────────────────────────
async function loadContainers() {
  const r = await api("/api/containers");
  if (!r?.ok) return;
  allContainers = r.containers || [];

  const running = allContainers.filter(c => c.state === "running").length;
  const stopped = allContainers.length - running;
  const stax    = allContainers.filter(c => c.name.startsWith("stax-")).length;

  document.getElementById("stat-total").textContent = allContainers.length;
  document.getElementById("stat-run").textContent   = running;
  document.getElementById("stat-stop").textContent  = stopped;
  document.getElementById("stat-stax").textContent  = stax;
  document.getElementById("nb-running").textContent = running;

  renderContainers();
}

function setCFilter(f, btn) {
  cFilter = f;
  document.querySelectorAll("#panel-containers .ftab").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  renderContainers();
}

function filterContainers() { renderContainers(); }

function renderContainers() {
  const q = document.getElementById("c-search").value.toLowerCase();
  let filtered = allContainers.filter(c => {
    if (cFilter === "running" && c.state !== "running") return false;
    if (cFilter === "stopped" && c.state === "running") return false;
    if (cFilter === "stax"    && !c.name.startsWith("stax-")) return false;
    if (q && !c.name.toLowerCase().includes(q) && !c.image.toLowerCase().includes(q)) return false;
    return true;
  });

  const tbody = document.getElementById("c-tbody");
  if (!filtered.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="empty"><div class="ico">🐳</div><p>No containers match the filter.</p></td></tr>`;
    return;
  }

  tbody.innerHTML = filtered.map(c => {
    const isRun   = c.state === "running";
    const isStax  = c.name.startsWith("stax-");
    const name    = c.name.replace(/^\//, "");
    const badge   = isRun
      ? `<span class="cbadge cbadge-run">● running</span>`
      : `<span class="cbadge cbadge-stop">○ stopped</span>`;
    const staxBadge = isStax ? `<span class="cbadge cbadge-stax" style="margin-left:4px">STAX</span>` : "";
    const cpu  = c.cpu  ? `<span class="cpu-mem">${esc(c.cpu)} / ${esc(c.mem)}</span>` : `<span style="color:var(--muted)">—</span>`;
    const ports = c.ports ? esc(c.ports).replace(/,/g, "<br>") : "—";

    return `<tr>
      <td>
        <div class="cname">${esc(name)}</div>
        <div class="cimage">${esc(c.image)}</div>
      </td>
      <td>${badge}${staxBadge}</td>
      <td><span class="cports">${ports || "—"}</span></td>
      <td>${cpu}</td>
      <td style="font-size:11px;color:var(--muted)">${esc(c.created)}</td>
      <td>
        <div class="action-btns" style="justify-content:flex-end">
          ${isRun
            ? `<button class="btn btn-ghost btn-sm btn-icon" onclick="cAction('${name}','stop')" title="Stop">⏹</button>
               <button class="btn btn-ghost btn-sm btn-icon" onclick="cAction('${name}','restart')" title="Restart">↺</button>`
            : `<button class="btn btn-green btn-sm btn-icon" onclick="cAction('${name}','start')" title="Start">▶</button>`}
          <button class="btn btn-ghost btn-sm btn-icon" onclick="showCLog('${name}')" title="Logs">📋</button>
          ${!isRun ? `<button class="btn btn-ghost btn-sm btn-icon" onclick="cAction('${name}','remove')" title="Remove" style="color:var(--red)">🗑</button>` : ""}
        </div>
      </td>
    </tr>`;
  }).join("");
}

async function cAction(name, action) {
  if (action === "remove" && !confirm(`Remove container "${name}"?`)) return;
  const r = await api(`/api/containers/${name}/${action}`, "POST");
  toast(r?.ok ? `${action} → ${name}` : (r?.error || "Failed"), r?.ok ? "ok" : "err");
  setTimeout(loadContainers, 800);
}

async function showCLog(name) {
  curCLogName = name;
  document.getElementById("clog-title").textContent = `📋 Logs — ${name}`;
  document.getElementById("clog-panel").style.display = "flex";
  await refreshCLog();
}

async function refreshCLog() {
  if (!curCLogName) return;
  const r = await api(`/api/containers/${curCLogName}/logs`);
  const box = document.getElementById("clog-box");
  box.textContent = r?.logs || "(no output)";
  box.scrollTop = box.scrollHeight;
}

// ── Stacks ────────────────────────────────────────────────────────────────
async function loadStacks() {
  const r = await api("/api/stacks");
  if (!r?.ok) return;
  allStacks = r.stacks || {};
  const installed = Object.values(allStacks).filter(s => s.status !== "not_installed").length;
  const badge = document.getElementById("nb-installed");
  badge.textContent = installed;
  badge.className = "nav-badge" + (installed > 0 ? " live" : "");
  renderStacks();
}

function setSFilter(f, btn) {
  sFilter = f;
  document.querySelectorAll("#panel-stacks .ftab").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  renderStacks();
}

function filterStacks() { renderStacks(); }

function renderStacks() {
  const q = document.getElementById("s-search").value.toLowerCase();
  const grid = document.getElementById("stack-grid");

  const filtered = Object.entries(allStacks).filter(([slug, s]) => {
    if (sFilter !== "All" && s.category !== sFilter) return false;
    if (q && !s.name.toLowerCase().includes(q) && !s.description.toLowerCase().includes(q)) return false;
    return true;
  });

  if (!filtered.length) {
    grid.innerHTML = `<div class="empty" style="grid-column:1/-1"><div class="ico">📦</div><p>No stacks found.</p></div>`;
    return;
  }

  grid.innerHTML = filtered.map(([slug, s]) => {
    const isRun  = s.status === "running";
    const isInst = s.status !== "not_installed";
    const statusText = {
      running:       "● Running",
      starting:      "⟳ Starting…",
      partial:       "⟳ Finishing up",
      stopped:       "○ Stopped",
      not_installed: "◌ Not installed",
    }[s.status] || "◌ Not installed";
    const statusCls = s.status || "not_installed";

    const portBadge = s.url_port
      ? `<span class="sc-port">:${s.url_port}</span>`
      : `<span class="sc-port" style="color:var(--muted)">bg</span>`;

    const tags = (s.tags || []).slice(0, 3).map(t =>
      `<span class="tag">${esc(t)}</span>`).join("");

    // Update button state
    const updateBtn = s.update_status === "up_to_date"
      ? `<button class="btn btn-sm" style="background:var(--green-bg);color:var(--green);border:1px solid var(--green-dim);cursor:default">✓ Up-to-date</button>`
      : s.update_available === true
        ? `<button class="btn btn-amber btn-sm" onclick="doUpdate('${slug}')">⬆ Update Available</button>`
        : `<button class="btn btn-ghost btn-sm" onclick="doUpdate('${slug}')">↑ Update</button>`;

    const isStarting = s.status === "starting" || s.status === "partial";

    const actions = isInst ? `
      ${isRun
        ? `<button class="btn btn-ghost btn-sm" onclick="stackAction('${slug}','stop')">⏹ Stop</button>`
        : isStarting
          ? `<button class="btn btn-ghost btn-sm" disabled style="opacity:.5;cursor:not-allowed">⏳ Pending</button>`
          : `<button class="btn btn-green btn-sm" onclick="stackAction('${slug}','start')">▶ Start</button>`}
      ${s.url_port ? `<a href="http://localhost:${s.url_port}" target="_blank" class="btn btn-ghost btn-sm">↗ Open</a>` : ""}
      ${updateBtn}
      <button class="btn btn-ghost btn-sm" onclick="stackAction('${slug}','remove')" style="color:var(--red)">🗑</button>
    ` : `
      <button class="btn btn-green btn-sm" id="install-btn-${slug}" onclick="doInstall('${slug}')">📦 Install</button>
      <a href="${esc(s.docs)}" target="_blank" class="btn btn-ghost btn-sm">Docs ↗</a>
    `;

    // Show update-available badge in header if update detected
    const updateBadge = s.update_available === true && isInst
      ? `<span class="update-badge">⬆ New version</span>` : "";

    return `
    <div class="stack-card ${statusCls === 'running' ? 'running' : isInst ? 'installed' : ''}" id="sc-${slug}">
      <div class="sc-head">
        <span class="sc-icon">${esc(s.icon)}</span>
        <div class="sc-info">
          <div class="sc-name">${esc(s.name)} ${updateBadge}</div>
          <div class="sc-cat">${esc(s.category)}</div>
        </div>
      </div>
      <div class="sc-desc">${esc(s.description)}</div>
      <div class="sc-meta">${tags}${portBadge}</div>
      <div class="sc-actions">
        <span class="sc-status ${statusCls}">${statusText}</span>
        <div style="margin-left:auto;display:flex;gap:6px;flex-wrap:wrap">${actions}</div>
      </div>
      <!-- Inline install/update log -->
      <div class="install-log" id="il-${slug}"></div>
    </div>`;
  }).join("");
}

async function doInstall(slug) {
  const btn = document.getElementById(`install-btn-${slug}`);
  if (btn) { btn.disabled = true; btn.textContent = "Checking ports…"; }

  // Port check first
  const pc = await api(`/api/stacks/${slug}/ports`);
  if (pc?.ok) {
    const blocked  = pc.blocked  || [];
    const conflicts = pc.conflicts || [];

    if (conflicts.length) {
      showPortWarning(slug, conflicts, "conflict");
      if (btn) { btn.disabled = false; btn.textContent = "📦 Install"; }
      return;
    }

    if (blocked.length) {
      // Show firewall warning but still allow install
      showPortWarning(slug, blocked, "firewall");
    }
  }

  if (btn) { btn.textContent = "Installing…"; }
  const logBox = document.getElementById(`il-${slug}`);
  if (logBox) { logBox.style.display = "block"; logBox.textContent = "Starting install…"; }
  await api(`/api/stacks/${slug}/install`, "POST", {});
  pollInstallLog(slug);
}

function showPortWarning(slug, ports, type) {
  const logBox = document.getElementById(`il-${slug}`);
  if (!logBox) return;
  logBox.style.display = "block";

  if (type === "conflict") {
    logBox.innerHTML = `<span class="log-err">⚠ Port conflict: ${ports.join(", ")} already in use by another process.\nStop the conflicting service first, then install.</span>`;
    return;
  }

  // Firewall blocked — show with open buttons
  const btnHtml = ports.map(p =>
    `<button onclick="openPort(${p},'${slug}')" style="margin:2px 4px;padding:3px 8px;background:var(--amber);color:#000;border:none;border-radius:4px;font-size:10px;cursor:pointer;font-weight:700">Open :${p}</button>`
  ).join("");

  logBox.innerHTML =
    `<span class="log-warn">⚠ Firewall (UFW) is blocking port${ports.length > 1 ? "s" : ""}: ${ports.join(", ")}\n` +
    `Users outside this machine won't be able to reach the service.\n` +
    `Open the port${ports.length > 1 ? "s" : ""} now or run: <b>sudo ufw allow PORT/tcp</b></span>\n` +
    btnHtml;
}

async function openPort(port, slug) {
  const r = await api("/api/ports/open", "POST", { port });
  if (r?.ok) {
    toast(`Port ${port} opened ✓`, "ok");
    if (slug === "__net__") { loadNetwork(); return; }
    const pc = await api(`/api/stacks/${slug}/ports`);
    if (pc?.blocked?.length === 0) {
      const logBox = document.getElementById(`il-${slug}`);
      if (logBox) logBox.innerHTML = `<span class="log-ok">✓ All ports open. Ready to install.</span>`;
    }
  } else {
    toast(`Could not open port ${port} — run: sudo ufw allow ${port}/tcp`, "err");
    if (r?.cmd) {
      const logBox = document.getElementById(`il-${slug}`);
      if (logBox) logBox.innerHTML += `\n<span class="log-err">Run manually: ${esc(r.cmd)}</span>`;
    }
  }
}

// After install completes, poll actual HTTP health — not just Docker status
function startRunningPoller(slug) {
  const s     = allStacks[slug] || {};
  const maxMs = ((s.startup_secs || 60) + 120) * 1000;
  const name  = s.name || slug;
  const port  = s.url_port;
  const started = Date.now();

  const logBox = document.getElementById(`il-${slug}`);
  const appendLog = msg => {
    if (logBox) { logBox.innerHTML += `\n<span>${esc(msg)}</span>`; logBox.scrollTop = logBox.scrollHeight; }
  };

  const interval = setInterval(async () => {
    const elapsed = Math.round((Date.now() - started) / 1000);

    if (Date.now() - started > maxMs) {
      clearInterval(interval);
      appendLog(`⚠ App did not respond after ${elapsed}s. Check Network → 🔍 Diagnose.`);
      toast(`${name}: check Network → Diagnose`, "");
      await loadStacks();
      return;
    }

    const r = await api(`/api/stacks/${slug}/health`);
    if (r?.ready) {
      clearInterval(interval);
      toast(`✓ ${name} is ready!`, "ok");
      appendLog(`✓ App is responding (HTTP ${r.http_code}) after ${elapsed}s`);
      if (port) appendLog(`  → http://localhost:${port}`);
      await loadStacks();
    } else {
      if (elapsed % 30 === 0 || elapsed < 10)
        appendLog(`⟳ Waiting for ${name} to start… (${elapsed}s / ~${s.startup_secs || 60}s expected)`);
    }
  }, 6000);
}

async function doUpdate(slug) {
  if (allStacks[slug]) { allStacks[slug].update_status = null; allStacks[slug].update_available = null; }
  renderStacks();
  const logBox = document.getElementById(`il-${slug}`);
  if (logBox) { logBox.style.display = "block"; logBox.textContent = "Checking for updates…"; }
  await api(`/api/stacks/${slug}/update`, "POST");
  pollInstallLog(slug);
}

function pollInstallLog(slug) {
  const interval = setInterval(async () => {
    const r = await api(`/api/stacks/${slug}/log`);
    if (!r) return;
    const logBox = document.getElementById(`il-${slug}`);
    if (logBox) {
      logBox.innerHTML = (r.log || []).map(l => {
        const cls = l.includes("✓") ? "log-ok" : l.includes("✗") || l.includes("ERROR") ? "log-err" : "";
        return `<span class="${cls}">${esc(l)}</span>`;
      }).join("\n");
      logBox.scrollTop = logBox.scrollHeight;
    }
    if (r.state === "done") {
      clearInterval(interval);
      toast(`${allStacks[slug]?.name || slug} ready!`, "ok");
      await loadStacks();
      await loadContainers();
      startRunningPoller(slug);  // keep polling until all containers are running
    } else if (r.state === "error") {
      clearInterval(interval);
      toast(`Failed — see log on card`, "err");
    }
  }, 1200);
}

async function stackAction(slug, action) {
  if (action === "remove" && !confirm(`Remove ${allStacks[slug]?.name || slug}?\n\nContainers will be deleted. Data directories are preserved.`)) return;
  const r = await api(`/api/stacks/${slug}/${action}`, "POST");
  toast(r?.ok ? `${action} → ${allStacks[slug]?.name}` : (r?.error || "Failed"), r?.ok ? "ok" : "err");
  setTimeout(loadStacks, 800);
  setTimeout(loadContainers, 800);
}

// ── Logs panel ────────────────────────────────────────────────────────────
function populateLogSel() {
  const sel = document.getElementById("log-container-sel");
  const cur = sel.value;
  sel.innerHTML = `<option value="">— Select a container —</option>` +
    allContainers.map(c => {
      const n = c.name.replace(/^\//, "");
      return `<option value="${esc(n)}" ${n === cur ? "selected" : ""}>${esc(n)}</option>`;
    }).join("");
}

async function loadFullLog() {
  const sel = document.getElementById("log-container-sel");
  const name = sel.value;
  const box  = document.getElementById("full-log");
  if (!name) { box.textContent = "Select a container above to view logs."; return; }
  box.textContent = "Loading…";
  const r = await api(`/api/containers/${name}/logs`);
  box.textContent = r?.logs || "(no output)";
  box.scrollTop = box.scrollHeight;
}

// ── Network panel ──────────────────────────────────────────────────────────
let _netPublicIp = "";

async function loadNetwork() {
  // Phase 1: instant load of basic info (no HTTP checks)
  document.getElementById("net-ip").textContent      = "Detecting…";
  document.getElementById("net-ufw").textContent     = "…";
  document.getElementById("net-fwd").textContent     = "…";
  document.getElementById("net-blocked").textContent = "…";
  document.getElementById("net-tbody").innerHTML =
    `<tr><td colspan="7" style="text-align:center;padding:24px;color:var(--muted)">Loading…</td></tr>`;

  const r = await api("/api/network/status");
  if (!r) {
    document.getElementById("net-tbody").innerHTML =
      `<tr><td colspan="7" style="text-align:center;padding:24px;color:var(--red)">
        Could not connect to STAX-MNGR server. Is it running?
      </td></tr>`;
    return;
  }

  _netPublicIp = r.public_ip || "";
  document.getElementById("net-ip").textContent  = r.public_ip || "Unknown";
  document.getElementById("net-ufw").textContent = r.ufw_active ? "Active" : "Off";
  document.getElementById("net-ufw").style.color = r.ufw_active ? "var(--amber)" : "var(--green)";
  document.getElementById("net-fwd").textContent = r.ip_forward ? "✓ On" : "✗ Off";
  document.getElementById("net-fwd").style.color = r.ip_forward ? "var(--green)" : "var(--red)";

  const ports = r.stack_ports || [];
  if (!ports.length) {
    document.getElementById("net-blocked").textContent = "0";
    document.getElementById("net-tbody").innerHTML =
      `<tr><td colspan="7" style="text-align:center;padding:30px;color:var(--muted)">No stacks installed yet.</td></tr>`;
    return;
  }

  // Render rows immediately with "Checking…" for HTTP status
  renderNetTable(ports);

  // Phase 2: fire off HTTP checks in background (may take a few seconds)
  document.getElementById("net-blocked").textContent = "…";
  api("/api/network/check-ports").then(r2 => {
    if (!r2?.stack_ports) return;
    const blocked = r2.stack_ports.filter(p => !p.ufw_open).length;
    document.getElementById("net-blocked").textContent = blocked;
    document.getElementById("net-blocked").style.color = blocked ? "var(--red)" : "var(--green)";
    const nb = document.getElementById("nb-blocked");
    nb.style.display = blocked > 0 ? "" : "none";
    renderNetTable(r2.stack_ports);
  });
}

function renderNetTable(ports) {
  const tbody = document.getElementById("net-tbody");
  tbody.innerHTML = ports.map(p => {
    const fwBadge = p.ufw_open
      ? `<span class="cbadge cbadge-run" style="font-size:10px">✓ Open</span>`
      : `<span class="cbadge" style="background:rgba(239,68,68,.15);color:var(--red);font-size:10px">✗ Blocked</span>`;

    const httpBadge = p.http_ok === null
      ? `<span style="color:var(--muted);font-size:11px">⟳ Checking…</span>`
      : p.http_ok
        ? `<span class="cbadge cbadge-run" style="font-size:10px">✓ Up (${p.http_code})</span>`
        : `<span class="cbadge" style="background:rgba(239,68,68,.15);color:var(--red);font-size:10px">✗ Down</span>`;

    const localUrl  = `http://localhost:${p.port}`;
    const publicUrl = _netPublicIp ? `http://${_netPublicIp}:${p.port}` : "";
    const fwBtn = p.ufw_open ? ""
      : `<button class="btn btn-amber btn-sm btn-icon" onclick="openPort(${p.port},'__net__')" title="Open UFW">🔓</button>`;

    return `<tr>
      <td style="font-weight:600">${esc(p.stack)}</td>
      <td><code style="color:var(--green);font-size:12px">:${p.port}</code></td>
      <td>${httpBadge}</td>
      <td><a href="${localUrl}" target="_blank" style="color:var(--blue);font-size:11px">${localUrl} ↗</a></td>
      <td>${publicUrl ? `<a href="${publicUrl}" target="_blank" style="color:var(--dim);font-size:11px">${publicUrl} ↗</a>` : `<span style="color:var(--muted)">—</span>`}</td>
      <td>${fwBadge}</td>
      <td style="text-align:right;display:flex;gap:4px;justify-content:flex-end">
        ${fwBtn}
        <button class="btn btn-ghost btn-sm btn-icon" onclick="diagnoseStack('${esc(p.slug)}')" title="Diagnose">🔍</button>
      </td>
    </tr>`;
  }).join("");
}
async function fixNetwork() {
  const btn = document.getElementById("fix-net-btn");
  btn.disabled = true; btn.textContent = "Fixing…";
  const r = await api("/api/network/fix", "POST");
  btn.disabled = false; btn.textContent = "🔧 Fix Network";
  if (r?.ok) {
    const opened = r.opened?.length || 0;
    const failed = r.failed?.length || 0;
    toast(failed > 0
      ? `Opened ${opened} ports, ${failed} failed — run manually`
      : `✓ Network fixed — ${opened} port${opened===1?"":"s"} configured`, failed ? "" : "ok");
    loadNetwork();
  } else {
    toast("Fix failed — try running manually", "err");
  }
}



async function diagnoseStack(slug) {
  toast("Running diagnostics…");
  const r = await api(`/api/stacks/${slug}/diagnose`);
  if (!r?.ok) { toast("Diagnose failed", "err"); return; }

  const lines = [];
  const publicIp = r.public_ip || "?";

  for (const svc of r.services || []) {
    lines.push(`\n=== ${svc.name} ===`);
    lines.push(`Docker state: ${svc.running ? "✓ running" : svc.exists ? "○ stopped/exited" : "✗ does not exist"}`);

    for (const pc of svc.ports || []) {
      if (pc.http_ok) {
        lines.push(`Port :${pc.port} → ✓ HTTP responding (${pc.http_code})`);
        lines.push(`  Local:  http://localhost:${pc.port}`);
        lines.push(`  Public: http://${publicIp}:${pc.port}`);
        if (pc.http_code >= 200 && pc.http_code < 400) {
          lines.push(`  ✓ App is working. If you can't reach it externally, check your cloud`);
          lines.push(`    provider's firewall (Hetzner/DO/Vultr security groups), NOT UFW.`);
        }
      } else {
        lines.push(`Port :${pc.port} → ✗ Not responding`);
        if (!svc.running) {
          lines.push(`  Container is not running — check logs below`);
        } else {
          lines.push(`  Container is running but app hasn't started yet — wait 30s and retry`);
        }
      }
    }

    if (svc.logs) {
      lines.push(`\n--- Container logs ---`);
      lines.push(svc.logs);
    }
  }

  // Show in a modal overlay
  const modal = document.createElement("div");
  modal.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:9998;display:flex;align-items:center;justify-content:center";
  modal.innerHTML = `
    <div style="background:var(--card);border:1px solid var(--border);border-radius:10px;
                width:min(760px,95vw);max-height:80vh;display:flex;flex-direction:column;overflow:hidden">
      <div style="padding:14px 18px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px">
        <span style="font-weight:700;font-size:14px">🔍 Diagnostic — ${esc(allStacks[slug]?.name || slug)}</span>
        <button onclick="this.closest('[style*=position]').remove()"
          style="margin-left:auto;background:transparent;border:none;color:var(--muted);font-size:18px;cursor:pointer">✕</button>
      </div>
      <div style="padding:14px;overflow-y:auto;flex:1">
        <pre style="font-family:'Menlo','Consolas',monospace;font-size:11px;line-height:1.7;
                    color:var(--dim);white-space:pre-wrap;word-break:break-all">${esc(lines.join("\n"))}</pre>
      </div>
      <div style="padding:12px 18px;border-top:1px solid var(--border);font-size:11px;color:var(--muted)">
        <b style="color:var(--amber)">ERR_CONNECTION_RESET?</b>
        This usually means the app is responding locally but your cloud provider's firewall is blocking
        the public IP. Open the port in your provider's control panel (not just UFW).
      </div>
    </div>`;
  document.body.appendChild(modal);
  modal.addEventListener("click", e => { if (e.target === modal) modal.remove(); });
}
</script>
</body>
</html>"""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"""
╔══════════════════════════════════════════════════════╗
║       STAX-MNGR  v{VERSION:<35}║
║       KillTheHost Docker Stack Manager               ║
╚══════════════════════════════════════════════════════╝
  UI  →  http://localhost:{PORT}
  Data → {DATA_DIR}
""")
    try:
        server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    except OSError as e:
        print(f"  [FATAL] Cannot bind to port {PORT}: {e}")
        sys.exit(1)

    # Start background update checker
    threading.Thread(target=_background_update_checker, daemon=True).start()

    import webbrowser
    threading.Thread(
        target=lambda: (time.sleep(1), webbrowser.open(f"http://localhost:{PORT}")),
        daemon=True
    ).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.\n")


if __name__ == "__main__":
    main()
