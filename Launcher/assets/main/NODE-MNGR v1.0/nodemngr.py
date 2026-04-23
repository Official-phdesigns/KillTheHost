#!/usr/bin/env python3
"""
╔════════════════════════════════════════════════════╗
║       NODE-MNGR  v1.0  —  KillTheHost              ║
║                                                    ║
║   Deploy & manage React + Node.js projects.        ║
║   Git clone, npm/yarn/pnpm, env vars, logs.        ║
║   Per-app Node version via nvm.                    ║
║                                                    ║
║   Browser UI → http://localhost:7272               ║
║   Pure Python 3.8+. Zero pip installs.             ║
╚════════════════════════════════════════════════════╝
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT      = 7272
DATA_DIR  = Path.home() / ".nodemngr"
APPS_FILE = DATA_DIR / "apps.json"
VERSION   = "1.0"

DATA_DIR.mkdir(parents=True, exist_ok=True)

_build_logs:  dict = {}
_build_state: dict = {}
_processes:   dict = {}
_log_lock = threading.Lock()

# ─── NVM helpers ──────────────────────────────────────────────────────────────

NVM_INIT = '. "$HOME/.nvm/nvm.sh" 2>/dev/null || . /usr/local/share/nvm/nvm.sh 2>/dev/null || true'

def nvm_available() -> bool:
    out, _, rc = run(f'bash -lc "{NVM_INIT} && nvm --version"')
    return rc == 0 and bool(out.strip())

def nvm_list_installed() -> list:
    out, _, _ = run(f'bash -lc "{NVM_INIT} && nvm ls --no-colors"')
    versions = []
    for line in out.splitlines():
        m = re.search(r'v(\d+\.\d+\.\d+)', line)
        if m:
            versions.append(m.group(0))
    return list(dict.fromkeys(versions))  # dedupe, preserve order

def nvm_current() -> str:
    out, _, _ = run(f'bash -lc "{NVM_INIT} && nvm current"')
    return out.strip()

def nvm_install_and_use(version: str, cwd: str = None, app_id: str = None) -> tuple:
    """Install (if needed) and use a Node version. Returns (stdout, stderr, rc)."""
    cmd = f'bash -lc "{NVM_INIT} && nvm install {version} && nvm use {version}"'
    if app_id:
        blog(app_id, f"nvm: installing/switching to Node {version}…")
    out, err, rc = run(cmd, cwd=cwd, timeout=300)
    return out, err, rc

def detect_required_node(app_dir: Path) -> str:
    """Read .nvmrc / .node-version / package.json engines.node"""
    for fname in (".nvmrc", ".node-version"):
        f = app_dir / fname
        if f.exists():
            v = f.read_text().strip().lstrip("v")
            if v:
                return v
    pkg = app_dir / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text())
            eng = data.get("engines", {}).get("node", "")
            # strip semver range chars to get a usable version
            m = re.search(r'(\d+(?:\.\d+)*)', eng)
            if m:
                return m.group(1)
        except Exception:
            pass
    return ""

def nvm_wrap(cmd: str, node_version: str = "") -> str:
    """Wrap a shell command so it runs under the correct Node version via nvm."""
    if node_version:
        return f'bash -lc "{NVM_INIT} && nvm use {node_version} --silent && {cmd}"'
    return f'bash -lc "{NVM_INIT} && {cmd}"'

# ─── Core helpers ─────────────────────────────────────────────────────────────

def run(cmd: str, cwd=None, timeout=120) -> tuple:
    try:
        r = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=cwd,
            env={**os.environ, "CI": "false", "FORCE_COLOR": "0"}
        )
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except subprocess.TimeoutExpired:
        return "", "timeout", 1
    except Exception as e:
        return "", str(e), 1


def blog(app_id: str, msg: str):
    ts   = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with _log_lock:
        _build_logs.setdefault(app_id, []).append(line)
        if len(_build_logs[app_id]) > 800:
            _build_logs[app_id] = _build_logs[app_id][-600:]
    print(f"[NODE/{app_id}] {msg}", flush=True)


def load_apps() -> dict:
    if APPS_FILE.exists():
        try:
            return json.loads(APPS_FILE.read_text())
        except Exception:
            pass
    return {}


def save_apps(data: dict):
    APPS_FILE.write_text(json.dumps(data, indent=2))


def get_public_ip() -> str:
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


def next_port(apps: dict) -> int:
    used = {a.get("port", 0) for a in apps.values()}
    p = 3100
    while p in used:
        p += 1
    return p


def detect_framework(path: Path) -> str:
    pkg = path / "package.json"
    if not pkg.exists():
        return "node"
    try:
        data = json.loads(pkg.read_text())
    except Exception:
        return "node"
    deps    = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
    if "next" in deps:                                              return "nextjs"
    if "nuxt" in deps or "@nuxt/core" in deps:                     return "nuxt"
    if "vite" in deps and ("react" in deps or "@vitejs/plugin-react" in deps): return "vite-react"
    if "vite" in deps:                                              return "vite"
    if "react-scripts" in deps:                                     return "cra"
    if "react" in deps:                                             return "react"
    if "vue" in deps:                                               return "vue"
    if "svelte" in deps:                                            return "svelte"
    if "astro" in deps:                                             return "astro"
    if "remix" in deps or "@remix-run/node" in deps:               return "remix"
    return "node"


def detect_pkg_manager(path: Path) -> str:
    if (path / "pnpm-lock.yaml").exists(): return "pnpm"
    if (path / "yarn.lock").exists():      return "yarn"
    return "npm"


def detect_start_cmd(path: Path, framework: str, pkg_mgr: str) -> str:
    pkg = path / "package.json"
    if pkg.exists():
        try:
            data    = json.loads(pkg.read_text())
            scripts = data.get("scripts", {})
            if "start" in scripts: return f"{pkg_mgr} {'run ' if pkg_mgr != 'npm' else ''}start"
            if "dev"   in scripts: return f"{pkg_mgr} run dev"
            if "serve" in scripts: return f"{pkg_mgr} run serve"
        except Exception:
            pass
    return f"{pkg_mgr} start"


def detect_build_cmd(path: Path, framework: str, pkg_mgr: str) -> str:
    pkg = path / "package.json"
    if pkg.exists():
        try:
            data    = json.loads(pkg.read_text())
            scripts = data.get("scripts", {})
            if "build" in scripts: return f"{pkg_mgr} run build"
        except Exception:
            pass
    return ""


def framework_icon(fw: str) -> str:
    return {"nextjs":"▲","nuxt":"💚","vite-react":"⚡","vite":"⚡","cra":"⚛",
            "react":"⚛","vue":"💚","svelte":"🔥","astro":"🚀","remix":"💿","node":"🟢"}.get(fw,"📦")


def framework_label(fw: str) -> str:
    return {"nextjs":"Next.js","nuxt":"Nuxt","vite-react":"Vite + React","vite":"Vite",
            "cra":"Create React App","react":"React","vue":"Vue","svelte":"Svelte",
            "astro":"Astro","remix":"Remix","node":"Node.js"}.get(fw, fw.title())


def _write_env_file(app: dict):
    env_vars = app.get("env", {})
    app_dir  = Path(app["path"])
    env_path = app_dir / ".env"
    lines    = [f'{k}={v}' for k, v in env_vars.items()]
    env_path.write_text("\n".join(lines) + "\n")


# ─── Build / start logic ──────────────────────────────────────────────────────

def _build_and_start(app_id: str):
    apps = load_apps()
    if app_id not in apps:
        return
    app      = apps[app_id]
    app_dir  = Path(app["path"])
    pkg_mgr  = app.get("pkg_manager", "npm")
    fw       = app.get("framework", "node")
    node_ver = app.get("node_version", "").strip()

    _build_state[app_id] = "building"
    blog(app_id, f"Starting {framework_label(fw)} app — {app['name']}")

    # ── Node version setup ──────────────────────────────────────────────────
    if not node_ver:
        detected = detect_required_node(app_dir)
        if detected:
            node_ver = detected
            blog(app_id, f"Detected required Node version: {node_ver} (from project files)")
            apps = load_apps()
            if app_id in apps:
                apps[app_id]["node_version"] = node_ver
                save_apps(apps)

    if node_ver and nvm_available():
        out, err, rc = nvm_install_and_use(node_ver, cwd=str(app_dir), app_id=app_id)
        if rc != 0:
            blog(app_id, f"⚠ nvm switch failed (continuing with system Node): {err[:200]}")
        else:
            blog(app_id, f"✓ Node {node_ver} active")
    elif node_ver:
        blog(app_id, f"⚠ nvm not found — cannot switch to Node {node_ver}, using system Node")

    # ── Env file ────────────────────────────────────────────────────────────
    if app.get("env"):
        blog(app_id, "Writing .env file…")
        _write_env_file(app)

    # ── Install deps ────────────────────────────────────────────────────────
    blog(app_id, f"Installing dependencies with {pkg_mgr}…")
    if node_ver and nvm_available():
        install_cmd = nvm_wrap(
            f"{pkg_mgr} install {'--legacy-peer-deps' if pkg_mgr == 'npm' else ''}",
            node_ver
        )
    else:
        install_cmd = f"{pkg_mgr} install {'--legacy-peer-deps' if pkg_mgr == 'npm' else ''}"

    out, err, rc = run(install_cmd, cwd=str(app_dir), timeout=300)
    if rc != 0:
        blog(app_id, f"✗ Install failed: {err[:400]}")
        _build_state[app_id] = "error"
        apps = load_apps()
        if app_id in apps:
            apps[app_id]["status"] = "error"
            save_apps(apps)
        return
    blog(app_id, "✓ Dependencies installed")

    # ── Build ───────────────────────────────────────────────────────────────
    build_cmd = app.get("build_cmd", "")
    if build_cmd:
        blog(app_id, f"Building: {build_cmd}…")
        wrapped_build = nvm_wrap(build_cmd, node_ver) if node_ver and nvm_available() else build_cmd
        out, err, rc = run(wrapped_build, cwd=str(app_dir), timeout=600)
        if rc != 0:
            blog(app_id, f"✗ Build failed: {err[:400]}")
            _build_state[app_id] = "error"
            apps = load_apps()
            if app_id in apps:
                apps[app_id]["status"] = "error"
                save_apps(apps)
            return
        blog(app_id, "✓ Build complete")

    # ── Start process ───────────────────────────────────────────────────────
    start_cmd = app.get("start_cmd", "npm start")
    port      = app.get("port", 3100)
    env       = {**os.environ, "PORT": str(port), "CI": "false"}
    if app.get("env"):
        env.update(app["env"])

    blog(app_id, f"Starting: {start_cmd} on port {port}…")

    if node_ver and nvm_available():
        launch_cmd = f'bash -lc "{NVM_INIT} && nvm use {node_ver} --silent && {start_cmd}"'
    else:
        launch_cmd = start_cmd

    try:
        proc = subprocess.Popen(
            launch_cmd, shell=True, cwd=str(app_dir),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env, bufsize=1
        )
        _processes[app_id] = proc

        def _stream():
            for line in proc.stdout:
                blog(app_id, line.rstrip())
            proc.wait()
            blog(app_id, f"Process exited (code {proc.returncode})")
            apps2 = load_apps()
            if app_id in apps2:
                apps2[app_id]["status"] = "stopped"
                save_apps(apps2)

        threading.Thread(target=_stream, daemon=True).start()
        time.sleep(2)

        if proc.poll() is None:
            blog(app_id, f"✓ App running at http://localhost:{port}")
            _build_state[app_id] = "done"
            apps = load_apps()
            if app_id in apps:
                apps[app_id]["status"] = "running"
                apps[app_id]["pid"]    = proc.pid
                save_apps(apps)
        else:
            blog(app_id, "✗ Process exited immediately — check logs")
            _build_state[app_id] = "error"
            apps = load_apps()
            if app_id in apps:
                apps[app_id]["status"] = "error"
                save_apps(apps)
    except Exception as e:
        blog(app_id, f"✗ Failed to start: {e}")
        _build_state[app_id] = "error"


def _clone_and_start(app_id: str, repo_url: str):
    apps = load_apps()
    if app_id not in apps:
        return
    app     = apps[app_id]
    app_dir = Path(app["path"])

    _build_state[app_id] = "building"
    blog(app_id, f"Cloning {repo_url}…")

    if app_dir.exists():
        shutil.rmtree(app_dir)
    app_dir.mkdir(parents=True, exist_ok=True)

    out, err, rc = run(f"git clone {repo_url} .", cwd=str(app_dir), timeout=300)
    if rc != 0:
        blog(app_id, f"✗ Clone failed: {err[:400]}")
        _build_state[app_id] = "error"
        apps = load_apps()
        if app_id in apps:
            apps[app_id]["status"] = "error"
            save_apps(apps)
        return
    blog(app_id, "✓ Repository cloned")

    fw      = detect_framework(app_dir)
    pkg_mgr = detect_pkg_manager(app_dir)
    req_node = detect_required_node(app_dir)

    apps = load_apps()
    if app_id in apps:
        apps[app_id]["framework"]   = fw
        apps[app_id]["pkg_manager"] = pkg_mgr
        if req_node and not apps[app_id].get("node_version"):
            apps[app_id]["node_version"] = req_node
            blog(app_id, f"Detected required Node version from repo: {req_node}")
        if not apps[app_id].get("build_cmd"):
            apps[app_id]["build_cmd"] = detect_build_cmd(app_dir, fw, pkg_mgr)
        if not apps[app_id].get("start_cmd"):
            apps[app_id]["start_cmd"] = detect_start_cmd(app_dir, fw, pkg_mgr)
        save_apps(apps)

    blog(app_id, f"Detected: {framework_label(fw)} | {pkg_mgr}")
    _build_and_start(app_id)


def stop_app(app_id: str):
    proc = _processes.get(app_id)
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    _processes.pop(app_id, None)
    apps = load_apps()
    if app_id in apps:
        apps[app_id]["status"] = "stopped"
        apps[app_id].pop("pid", None)
        save_apps(apps)


def restart_app(app_id: str):
    stop_app(app_id)
    time.sleep(1)
    _build_logs[app_id] = []
    threading.Thread(target=_build_and_start, args=(app_id,), daemon=True).start()


def delete_app(app_id: str, delete_files: bool = False):
    stop_app(app_id)
    apps = load_apps()
    app  = apps.pop(app_id, None)
    save_apps(apps)
    if delete_files and app:
        p = Path(app.get("path", ""))
        if p.exists() and str(DATA_DIR) in str(p):
            shutil.rmtree(p, ignore_errors=True)
    _build_logs.pop(app_id, None)
    _build_state.pop(app_id, None)


def app_status(app_id: str) -> str:
    proc = _processes.get(app_id)
    if proc and proc.poll() is None:
        return "running"
    apps = load_apps()
    return apps.get(app_id, {}).get("status", "stopped")


# ─── HTTP Handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"

        if path == "/":
            self._raw(200, "text/html; charset=utf-8", HTML.encode())

        elif path == "/api/apps":
            apps = load_apps()
            result = {}
            for aid, a in apps.items():
                result[aid] = {**a, "status": app_status(aid)}
            self._json({"ok": True, "apps": result})

        elif path == "/api/system":
            node_v, _, _ = run("node --version 2>/dev/null")
            npm_v,  _, _ = run("npm --version 2>/dev/null")
            yarn_v, _, _ = run("yarn --version 2>/dev/null")
            pnpm_v, _, _ = run("pnpm --version 2>/dev/null")
            git_v,  _, _ = run("git --version 2>/dev/null")
            nvm_v,  _, _ = run(f'bash -lc "{NVM_INIT} && nvm --version"')
            nvm_installed = nvm_list_installed() if nvm_v else []
            nvm_cur       = nvm_current() if nvm_v else ""
            self._json({
                "ok":           True,
                "node":         node_v or "not found",
                "npm":          npm_v  or "not found",
                "yarn":         yarn_v or "not found",
                "pnpm":         pnpm_v or "not found",
                "git":          git_v  or "not found",
                "ip":           get_public_ip(),
                "nvm":          nvm_v  or "not found",
                "nvm_versions": nvm_installed,
                "nvm_current":  nvm_cur,
            })

        elif path == "/api/nvm/versions":
            installed = nvm_list_installed()
            self._json({"ok": True, "versions": installed})

        elif path.startswith("/api/apps/") and path.endswith("/log"):
            app_id = path.split("/")[3]
            self._json({
                "ok":    True,
                "log":   _build_logs.get(app_id, []),
                "state": _build_state.get(app_id, "idle"),
            })

        elif path.startswith("/api/apps/") and path.endswith("/env"):
            app_id = path.split("/")[3]
            apps   = load_apps()
            self._json({"ok": True, "env": apps.get(app_id, {}).get("env", {})})

        else:
            self._raw(404, "text/plain", b"Not found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = json.loads(self.rfile.read(length).decode()) if length else {}
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/")

        if path == "/api/apps/create":
            name     = (body.get("name") or "").strip()
            repo     = (body.get("repo") or "").strip()
            local    = (body.get("local_path") or "").strip()
            fw       = (body.get("framework") or "").strip()
            pkg_mgr  = (body.get("pkg_manager") or "npm").strip()
            env_raw  = body.get("env", {})
            build_c  = (body.get("build_cmd") or "").strip()
            start_c  = (body.get("start_cmd") or "").strip()
            node_ver = (body.get("node_version") or "").strip()

            if not name:
                self._json({"ok": False, "error": "Name required"}); return

            apps   = load_apps()
            app_id = f"app_{int(time.time()*1000)}"
            port   = next_port(apps)

            if local:
                app_dir = Path(local).expanduser().resolve()
                if not app_dir.exists():
                    self._json({"ok": False, "error": f"Path not found: {local}"}); return
                fw      = fw or detect_framework(app_dir)
                pkg_mgr = detect_pkg_manager(app_dir)
                build_c = build_c or detect_build_cmd(app_dir, fw, pkg_mgr)
                start_c = start_c or detect_start_cmd(app_dir, fw, pkg_mgr)
                if not node_ver:
                    node_ver = detect_required_node(app_dir)
            else:
                app_dir = DATA_DIR / "apps" / app_id
                app_dir.mkdir(parents=True, exist_ok=True)

            app = {
                "id":           app_id,
                "name":         name,
                "repo":         repo,
                "path":         str(app_dir),
                "framework":    fw or "node",
                "pkg_manager":  pkg_mgr,
                "build_cmd":    build_c,
                "start_cmd":    start_c,
                "port":         port,
                "env":          env_raw,
                "node_version": node_ver,
                "status":       "stopped",
                "created":      datetime.now().isoformat(),
            }
            apps[app_id] = app
            save_apps(apps)
            _build_logs[app_id] = []

            if repo:
                threading.Thread(target=_clone_and_start, args=(app_id, repo), daemon=True).start()
            else:
                threading.Thread(target=_build_and_start, args=(app_id,), daemon=True).start()

            self._json({"ok": True, "app_id": app_id, "port": port})
            return

        if path.startswith("/api/apps/"):
            parts  = path.split("/")
            app_id = parts[3]
            action = parts[4] if len(parts) > 4 else ""

            if action == "stop":
                stop_app(app_id)
                self._json({"ok": True})

            elif action == "restart":
                threading.Thread(target=restart_app, args=(app_id,), daemon=True).start()
                self._json({"ok": True})

            elif action == "delete":
                delete_app(app_id, delete_files=body.get("delete_files", False))
                self._json({"ok": True})

            elif action == "env":
                apps = load_apps()
                if app_id not in apps:
                    self._json({"ok": False, "error": "Not found"}); return
                apps[app_id]["env"] = body.get("env", {})
                save_apps(apps)
                _write_env_file(apps[app_id])
                self._json({"ok": True})

            elif action == "update":
                apps = load_apps()
                if app_id not in apps:
                    self._json({"ok": False, "error": "Not found"}); return
                for k in ("name", "build_cmd", "start_cmd", "port", "framework", "pkg_manager", "node_version"):
                    if k in body:
                        apps[app_id][k] = body[k]
                save_apps(apps)
                self._json({"ok": True})

            elif action == "pull":
                apps = load_apps()
                if app_id not in apps:
                    self._json({"ok": False, "error": "Not found"}); return
                app_dir = Path(apps[app_id]["path"])
                _build_logs[app_id] = []
                blog(app_id, "Pulling latest from git…")
                out, err, rc = run("git pull", cwd=str(app_dir), timeout=120)
                if rc == 0:
                    blog(app_id, f"✓ {out or 'Up to date'}")
                    self._json({"ok": True})
                else:
                    blog(app_id, f"✗ {err}")
                    self._json({"ok": False, "error": err})

            elif action == "set-node":
                # Install + switch Node version for this app
                version = (body.get("version") or "").strip()
                if not version:
                    self._json({"ok": False, "error": "version required"}); return
                apps = load_apps()
                if app_id not in apps:
                    self._json({"ok": False, "error": "App not found"}); return

                def _do_set_node(aid, ver):
                    blog(aid, f"Setting Node version to {ver}…")
                    if not nvm_available():
                        blog(aid, "✗ nvm not found. Install nvm first: https://github.com/nvm-sh/nvm")
                        _build_state[aid] = "error"
                        return
                    out, err, rc = nvm_install_and_use(ver, app_id=aid)
                    if rc == 0:
                        blog(aid, f"✓ Node {ver} installed and active")
                        a2 = load_apps()
                        if aid in a2:
                            a2[aid]["node_version"] = ver
                            save_apps(a2)
                        _build_state[aid] = "idle"
                    else:
                        blog(aid, f"✗ Failed to set Node {ver}: {err[:300]}")
                        _build_state[aid] = "error"

                _build_logs[app_id] = []
                _build_state[app_id] = "building"
                threading.Thread(target=_do_set_node, args=(app_id, version), daemon=True).start()
                self._json({"ok": True})

            else:
                self._json({"ok": False, "error": "Unknown action"})
            return

        # NVM global install
        if path == "/api/nvm/install":
            version = (body.get("version") or "").strip()
            if not version:
                self._json({"ok": False, "error": "version required"}); return
            def _do_install(ver):
                out, err, rc = run(
                    f'bash -lc "{NVM_INIT} && nvm install {ver}"',
                    timeout=300
                )
                print(f"[NVM] install {ver}: rc={rc}", flush=True)
            threading.Thread(target=_do_install, args=(version,), daemon=True).start()
            self._json({"ok": True})
            return

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
            pass


# ─── Embedded UI ──────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>KillTheHost - NODE-MNGR</title>
    <link rel="shortcut icon" href="https://www.phdesigns.net/img/favicon.ico" type="image/x-icon">
    <link rel="icon" href="https://www.phdesigns.net/img/favicon.ico" type="image/x-icon">
<style>
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg:#1a1a1a; --sidebar:#242424; --card:#2a2a2a; --card2:#2222;
  --border:#383838; --text:#e8e8e8; --dim:#888; --muted:#555;
  --green:#10a37f; --green-bg:#0c1f18; --green-dim:#1e4a30;
  --blue:#3b82f6; --amber:#f59e0b; --red:#ef4444;
  --purple:#a78bfa; --cyan:#06b6d4; --log-bg:#141414; --inp:#1e1e1e;
  --yellow:#F7DF1E;
}
::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
body{font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
  background:var(--bg);color:var(--text);display:flex;flex-direction:column;
  height:100vh;overflow:hidden;font-size:13px;line-height:1.5}
.topbar{padding:10px 20px;border-bottom:1px solid var(--border);background:var(--sidebar);
  display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;flex-shrink:0}
.brand{display:flex;align-items:flex-start;gap:10px}
.kth-mark{width:34px;height:34px;border-radius:9px;
  background:linear-gradient(145deg,#ff56b9 0%,#ef63d6 62%,#c86bff 100%);
  display:flex;align-items:center;justify-content:center;
  font-family:"Menlo","Consolas",monospace;font-size:13px;font-weight:700;
  color:#fff;letter-spacing:-.6px;flex-shrink:0;
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.14);margin-top:1px}
.brand-main{display:flex;flex-direction:column;gap:1px}
.kth-logo{display:flex;align-items:center;gap:0;font-size:18px;font-weight:800;line-height:1}
.kth-word{color:#f4f5fb;letter-spacing:-.35px}
.kth-word.the{background:linear-gradient(135deg,#ff5ab8 0%,#f468cd 55%,#c96dff 100%);
  -webkit-background-clip:text;background-clip:text;-webkit-text-fill-color:transparent}
.brand-app{font-size:13px;font-weight:700;color:var(--text);letter-spacing:.32px}
.brand-suite{font-size:10px;color:var(--muted);letter-spacing:.22px;text-transform:uppercase}
.hdr-right{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.status-pill{display:flex;align-items:center;gap:6px;padding:4px 10px;border-radius:20px;
  font-size:11px;border:1px solid var(--border);background:var(--card)}
#live-dot{width:6px;height:6px;border-radius:50%;background:var(--green);
  box-shadow:0 0 5px var(--green);animation:pulse 2.5s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.shell{display:flex;flex:1;overflow:hidden}
.sidebar{width:188px;flex-shrink:0;background:var(--sidebar);border-right:1px solid var(--border);
  display:flex;flex-direction:column;padding:12px 8px;gap:2px}
.nav-item{display:flex;align-items:center;gap:10px;padding:8px 12px;border-radius:6px;
  cursor:pointer;transition:background .15s,color .15s;color:var(--dim);
  font-size:13px;font-weight:500;border:none;background:transparent;width:100%;text-align:left}
.nav-item:hover{background:var(--card);color:var(--text)}
.nav-item.active{background:var(--green-bg);color:var(--green)}
.nav-item .nav-ico{font-size:14px;flex-shrink:0}
.nav-badge{margin-left:auto;background:var(--border);color:var(--dim);
  font-size:10px;padding:1px 6px;border-radius:10px}
.nav-badge.live{background:var(--green-bg);color:var(--green)}
.sidebar-footer{margin-top:auto;padding-top:10px;border-top:1px solid var(--border)}
.sidebar-footer p{font-size:10px;color:var(--muted);text-align:center;margin-top:6px}
.main{flex:1;overflow-y:auto;display:flex;flex-direction:column}
.panel{display:none;flex-direction:column;flex:1;padding:20px;gap:16px}
.panel.active{display:flex}
.toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.panel-title{font-size:18px;font-weight:700}
.panel-sub{font-size:12px;color:var(--muted);margin-top:2px}
.ml-auto{margin-left:auto}
.btn{padding:6px 14px;border-radius:7px;font-size:12px;font-weight:600;cursor:pointer;
  border:none;transition:filter .15s,transform .1s;display:inline-flex;align-items:center;gap:5px}
.btn:disabled{opacity:.35;cursor:not-allowed}
.btn:not(:disabled):hover{filter:brightness(1.12)}
.btn:not(:disabled):active{transform:scale(.97)}
.btn-green{background:var(--green);color:#000}
.btn-ghost{background:var(--card);color:var(--text);border:1px solid var(--border)}
.btn-ghost:not(:disabled):hover{border-color:var(--muted)}
.btn-red{background:var(--red);color:#fff}
.btn-amber{background:var(--amber);color:#000}
.btn-blue{background:var(--blue);color:#fff}
.btn-yellow{background:var(--yellow);color:#000}
.btn-sm{padding:4px 10px;font-size:11px}
.btn-icon{padding:5px 8px}
.stats-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:12px}
.stat-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.stat-val{font-size:26px;font-weight:800;color:var(--green);line-height:1}
.stat-lbl{font-size:11px;color:var(--muted);margin-top:4px;text-transform:uppercase;letter-spacing:.5px}
.app-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px}
.app-card{background:var(--card);border:1px solid var(--border);border-radius:10px;
  padding:16px;display:flex;flex-direction:column;gap:12px;transition:border-color .15s}
.app-card:hover{border-color:var(--dim)}
.app-card.running{border-color:var(--green)}
.app-card.error{border-color:var(--red)}
.ac-head{display:flex;align-items:flex-start;gap:12px}
.ac-icon{font-size:26px;flex-shrink:0;line-height:1;margin-top:2px}
.ac-info{flex:1;min-width:0}
.ac-name{font-size:14px;font-weight:700}
.ac-fw{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-top:1px}
.ac-repo{font-size:11px;color:var(--dim);margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ac-meta{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.tag{font-size:10px;background:var(--card2);color:var(--muted);
  padding:2px 7px;border-radius:10px;border:1px solid var(--border)}
.tag.node-tag{background:#1a1a00;color:var(--yellow);border-color:#3a3a00;font-weight:700}
.ac-port{font-size:11px;font-family:monospace;color:var(--green);font-weight:600}
.ac-status{font-size:11px;font-weight:600}
.ac-status.running{color:var(--green)}
.ac-status.building{color:var(--amber);animation:blink 1.5s ease-in-out infinite}
.ac-status.error{color:var(--red)}
.ac-status.stopped{color:var(--muted)}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.4}}
.ac-actions{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.inline-log{background:var(--log-bg);border:1px solid var(--border);border-radius:6px;
  padding:10px 12px;font-family:"Menlo","Consolas",monospace;font-size:11px;
  line-height:1.65;max-height:160px;overflow-y:auto;color:var(--dim);
  white-space:pre-wrap;word-break:break-all;display:none}
.log-box{background:var(--log-bg);border:1px solid var(--border);border-radius:8px;
  padding:12px 14px;font-family:"Menlo","Consolas",monospace;font-size:11.5px;
  line-height:1.7;overflow-y:auto;color:var(--dim);white-space:pre-wrap;word-break:break-all}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.form-full{grid-column:1/-1}
.form-group{display:flex;flex-direction:column;gap:5px}
.form-label{font-size:11px;font-weight:600;color:var(--dim);text-transform:uppercase;letter-spacing:.4px}
.form-inp,.form-sel,.form-ta{background:var(--inp);border:1px solid var(--border);border-radius:6px;
  padding:8px 10px;color:var(--text);font-size:12px;outline:none;font-family:inherit}
.form-inp:focus,.form-sel:focus,.form-ta:focus{border-color:var(--green)}
.form-ta{resize:vertical;min-height:80px;font-family:"Menlo","Consolas",monospace;font-size:11px}
.form-hint{font-size:10px;color:var(--muted)}
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:9000;
  display:flex;align-items:center;justify-content:center}
.modal{background:var(--card);border:1px solid var(--border);border-radius:12px;
  width:min(600px,95vw);max-height:90vh;display:flex;flex-direction:column;overflow:hidden}
.modal-head{padding:16px 20px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:10px}
.modal-title{font-size:15px;font-weight:700}
.modal-body{padding:20px;overflow-y:auto;flex:1;display:flex;flex-direction:column;gap:14px}
.modal-foot{padding:14px 20px;border-top:1px solid var(--border);display:flex;gap:8px;justify-content:flex-end}
.sys-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
.sys-card{background:var(--inp);border:1px solid var(--border);border-radius:8px;padding:12px 14px}
.sys-key{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;margin-bottom:4px}
.sys-val{font-size:13px;font-weight:600;font-family:monospace;color:var(--text)}
.sys-val.ok{color:var(--green)}
.sys-val.bad{color:var(--red)}
.nvm-versions{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.nvm-ver-chip{background:var(--inp);border:1px solid var(--border);border-radius:6px;
  padding:4px 10px;font-size:11px;font-family:monospace;color:var(--text);cursor:pointer;
  transition:border-color .15s,background .15s}
.nvm-ver-chip:hover{border-color:var(--yellow);background:#1a1a00;color:var(--yellow)}
.nvm-ver-chip.active{border-color:var(--green);background:var(--green-bg);color:var(--green)}
.empty{text-align:center;padding:60px 20px;color:var(--muted)}
.empty .ico{font-size:40px;margin-bottom:12px}
#toast{position:fixed;bottom:20px;right:20px;display:flex;flex-direction:column;gap:6px;z-index:9999}
.tmsg{background:var(--card);border:1px solid var(--border);border-radius:6px;
  padding:9px 14px;font-size:12px;color:var(--text);opacity:0;
  transform:translateY(6px);transition:all .2s;max-width:300px}
.tmsg.ok{border-color:var(--green);color:var(--green)}
.tmsg.err{border-color:var(--red);color:var(--red)}
.tmsg.show{opacity:1;transform:translateY(0)}
.env-row{display:flex;gap:6px;align-items:center}
.env-key,.env-val{flex:1;background:var(--inp);border:1px solid var(--border);border-radius:5px;
  padding:6px 8px;color:var(--text);font-size:11px;font-family:monospace;outline:none}
.env-key:focus,.env-val:focus{border-color:var(--green)}
.node-modal-log{background:var(--log-bg);border:1px solid var(--border);border-radius:6px;
  padding:10px 12px;font-family:"Menlo","Consolas",monospace;font-size:11px;
  line-height:1.65;height:140px;overflow-y:auto;color:var(--dim);
  white-space:pre-wrap;word-break:break-all;margin-top:8px}
.ver-presets{display:flex;flex-wrap:wrap;gap:6px;margin-top:6px}
.ver-preset{background:var(--inp);border:1px solid var(--border);border-radius:5px;
  padding:3px 9px;font-size:11px;font-family:monospace;cursor:pointer;color:var(--dim);
  transition:border-color .15s,color .15s}
.ver-preset:hover{border-color:var(--yellow);color:var(--yellow)}
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
      <div class="brand-app">NODE-MNGR</div>
      <div class="brand-suite">KillTheHost Suite &middot; Node.js Manager</div>
    </div>
  </div>
  <div class="hdr-right">
    <div class="status-pill"><div id="live-dot"></div><span>Live</span></div>
    <button class="btn btn-green btn-sm" onclick="openDeploy()">&#xFF0B; Deploy App</button>
  </div>
</div>
<div class="shell">
  <nav class="sidebar">
    <button class="nav-item active" onclick="tab('apps',this)">
      <span class="nav-ico">&#x1F7E2;</span> Apps
      <span class="nav-badge live" id="nb-running">0</span>
    </button>
    <button class="nav-item" onclick="tab('logs',this)">
      <span class="nav-ico">&#x1F4CB;</span> Logs
    </button>
    <button class="nav-item" onclick="tab('system',this)">
      <span class="nav-ico">&#x2699;&#xFE0F;</span> System
    </button>
    <div class="sidebar-footer">
      <button class="btn btn-ghost" style="width:100%;font-size:11px" onclick="refreshAll()">&#x21BB; Refresh</button>
      <p>NODE-MNGR v1.1<br>KillTheHost</p>
    </div>
  </nav>
  <div class="main">

    <!-- APPS PANEL -->
    <div class="panel active" id="panel-apps">
      <div>
        <div class="panel-title">Node.js Apps</div>
        <div class="panel-sub">Deploy and manage React, Next.js, Vite, and Node.js projects</div>
      </div>
      <div class="stats-row">
        <div class="stat-card"><div class="stat-val" id="stat-total">&#x2014;</div><div class="stat-lbl">Total Apps</div></div>
        <div class="stat-card"><div class="stat-val" id="stat-run">&#x2014;</div><div class="stat-lbl">Running</div></div>
        <div class="stat-card"><div class="stat-val" id="stat-stop">&#x2014;</div><div class="stat-lbl">Stopped</div></div>
        <div class="stat-card"><div class="stat-val" id="stat-err">&#x2014;</div><div class="stat-lbl">Errors</div></div>
      </div>
      <div class="app-grid" id="app-grid">
        <div class="empty" style="grid-column:1/-1"><div class="ico">&#x1F7E2;</div><p>No apps yet &mdash; click <b>Deploy App</b> to get started.</p></div>
      </div>
    </div>

    <!-- LOGS PANEL -->
    <div class="panel" id="panel-logs">
      <div>
        <div class="panel-title">App Logs</div>
        <div class="panel-sub">Live output from your Node.js processes</div>
      </div>
      <div class="toolbar">
        <select class="form-sel" id="log-app-sel" onchange="loadAppLog()" style="max-width:260px">
          <option value="">&#x2014; Select an app &#x2014;</option>
        </select>
        <button class="btn btn-ghost btn-sm" onclick="loadAppLog()">&#x21BB; Refresh</button>
        <button class="btn btn-ghost btn-sm" onclick="document.getElementById('log-full').innerHTML=''">&#x1F5D1; Clear</button>
      </div>
      <div class="log-box" id="log-full" style="flex:1;min-height:400px;max-height:none">Select an app above to view logs.</div>
    </div>

    <!-- SYSTEM PANEL -->
    <div class="panel" id="panel-system">
      <div>
        <div class="panel-title">System Info</div>
        <div class="panel-sub">Node.js, npm, yarn, pnpm, git, and nvm versions on this server</div>
      </div>
      <div class="sys-grid" id="sys-grid">
        <div class="empty"><div class="ico">&#x2699;&#xFE0F;</div><p>Loading&#x2026;</p></div>
      </div>
      <div id="nvm-section" style="display:none">
        <div style="font-size:13px;font-weight:700;margin-bottom:8px">&#x1F4E6; Installed Node Versions (nvm)</div>
        <div class="nvm-versions" id="nvm-ver-list"></div>
        <div style="margin-top:14px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
          <input class="form-inp" id="nvm-install-ver" placeholder="e.g. 20, 18.17.0, lts/*" style="max-width:220px">
          <button class="btn btn-yellow btn-sm" onclick="nvmInstallGlobal()">&#x2B07; Install Node Version</button>
          <span style="font-size:11px;color:var(--muted)">Installs globally via nvm</span>
        </div>
      </div>
    </div>

  </div>
</div>

<!-- Deploy Modal -->
<div class="modal-bg" id="deploy-modal" style="display:none" onclick="if(event.target===this)closeDeploy()">
  <div class="modal">
    <div class="modal-head">
      <span class="modal-title">&#x1F680; Deploy New App</span>
      <button onclick="closeDeploy()" style="margin-left:auto;background:transparent;border:none;color:var(--muted);font-size:18px;cursor:pointer">&#x2715;</button>
    </div>
    <div class="modal-body">
      <div class="form-grid">
        <div class="form-group form-full">
          <label class="form-label">App Name *</label>
          <input class="form-inp" id="d-name" placeholder="my-react-app">
        </div>
        <div class="form-group form-full">
          <label class="form-label">Git Repository URL</label>
          <input class="form-inp" id="d-repo" placeholder="https://github.com/user/repo.git">
          <span class="form-hint">Leave blank to use a local path instead</span>
        </div>
        <div class="form-group form-full">
          <label class="form-label">Local Path (if no repo)</label>
          <input class="form-inp" id="d-local" placeholder="/home/user/my-project">
        </div>
        <div class="form-group">
          <label class="form-label">Framework</label>
          <select class="form-sel" id="d-fw">
            <option value="">Auto-detect</option>
            <option value="nextjs">Next.js</option>
            <option value="vite-react">Vite + React</option>
            <option value="cra">Create React App</option>
            <option value="vite">Vite</option>
            <option value="nuxt">Nuxt</option>
            <option value="vue">Vue</option>
            <option value="svelte">Svelte</option>
            <option value="astro">Astro</option>
            <option value="remix">Remix</option>
            <option value="node">Node.js (Express/Fastify/etc)</option>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">Package Manager</label>
          <select class="form-sel" id="d-pkg">
            <option value="npm">npm</option>
            <option value="yarn">yarn</option>
            <option value="pnpm">pnpm</option>
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">Node Version (optional)</label>
          <input class="form-inp" id="d-node-ver" placeholder="e.g. 20, 18.17.0, lts/* (auto-detected)">
          <span class="form-hint">Leave blank to auto-detect from .nvmrc / package.json engines</span>
        </div>
        <div class="form-group">
          <label class="form-label">Build Command</label>
          <input class="form-inp" id="d-build" placeholder="npm run build (auto-detected)">
        </div>
        <div class="form-group">
          <label class="form-label">Start Command</label>
          <input class="form-inp" id="d-start" placeholder="npm start (auto-detected)">
        </div>
        <div class="form-group form-full">
          <label class="form-label">Environment Variables</label>
          <div id="env-rows" style="display:flex;flex-direction:column;gap:6px"></div>
          <button class="btn btn-ghost btn-sm" style="margin-top:6px;align-self:flex-start" onclick="addEnvRow()">&#xFF0B; Add Variable</button>
        </div>
      </div>
    </div>
    <div class="modal-foot">
      <button class="btn btn-ghost" onclick="closeDeploy()">Cancel</button>
      <button class="btn btn-green" id="deploy-btn" onclick="deployApp()">&#x1F680; Deploy</button>
    </div>
  </div>
</div>

<!-- Env Modal -->
<div class="modal-bg" id="env-modal" style="display:none" onclick="if(event.target===this)closeEnv()">
  <div class="modal">
    <div class="modal-head">
      <span class="modal-title" id="env-modal-title">&#x2699; Environment Variables</span>
      <button onclick="closeEnv()" style="margin-left:auto;background:transparent;border:none;color:var(--muted);font-size:18px;cursor:pointer">&#x2715;</button>
    </div>
    <div class="modal-body">
      <div id="env-edit-rows" style="display:flex;flex-direction:column;gap:6px"></div>
      <button class="btn btn-ghost btn-sm" style="margin-top:6px;align-self:flex-start" onclick="addEnvEditRow()">&#xFF0B; Add Variable</button>
    </div>
    <div class="modal-foot">
      <button class="btn btn-ghost" onclick="closeEnv()">Cancel</button>
      <button class="btn btn-green" onclick="saveEnv()">&#x1F4BE; Save &amp; Write .env</button>
    </div>
  </div>
</div>

<!-- Node Version Modal -->
<div class="modal-bg" id="node-modal" style="display:none" onclick="if(event.target===this)closeNodeModal()">
  <div class="modal" style="width:min(480px,95vw)">
    <div class="modal-head">
      <span style="font-size:18px">&#x1F4E6;</span>
      <span class="modal-title" id="node-modal-title">Set Node Version</span>
      <button onclick="closeNodeModal()" style="margin-left:auto;background:transparent;border:none;color:var(--muted);font-size:18px;cursor:pointer">&#x2715;</button>
    </div>
    <div class="modal-body">
      <div style="font-size:12px;color:var(--dim);line-height:1.6">
        Set the Node.js version for this app. NODE-MNGR will use <b>nvm</b> to install it (if needed) and run all commands under that version.
        The version is also auto-detected from <code style="color:var(--green)">.nvmrc</code>, <code style="color:var(--green)">.node-version</code>, or <code style="color:var(--green)">package.json engines.node</code>.
      </div>
      <div class="form-group">
        <label class="form-label">Node Version</label>
        <input class="form-inp" id="node-ver-input" placeholder="e.g. 20, 18.17.0, lts/*, 16">
        <div class="ver-presets" id="node-ver-presets"></div>
        <span class="form-hint">Common: <b>lts/*</b> (latest LTS) &nbsp;|&nbsp; <b>20</b> (latest v20) &nbsp;|&nbsp; <b>18.17.0</b> (exact)</span>
      </div>
      <div id="node-modal-log-wrap" style="display:none">
        <div style="font-size:11px;color:var(--muted);margin-bottom:4px">nvm output:</div>
        <div class="node-modal-log" id="node-modal-log"></div>
      </div>
    </div>
    <div class="modal-foot">
      <button class="btn btn-ghost" onclick="closeNodeModal()">Cancel</button>
      <button class="btn btn-yellow" id="node-modal-btn" onclick="applyNodeVersion()">&#x2B07; Install &amp; Use</button>
    </div>
  </div>
</div>

<div id="toast"></div>
<script>
let allApps={};let curEnvAppId=null;let curNodeAppId=null;
const FW_ICONS={nextjs:"▲",nuxt:"💚","vite-react":"⚡",vite:"⚡",cra:"⚛",react:"⚛",vue:"💚",svelte:"🔥",astro:"🚀",remix:"💿",node:"🟢"};
const FW_LABELS={nextjs:"Next.js",nuxt:"Nuxt","vite-react":"Vite + React",vite:"Vite",cra:"Create React App",react:"React",vue:"Vue",svelte:"Svelte",astro:"Astro",remix:"Remix",node:"Node.js"};
const _buildingApps=new Set();

window.addEventListener("DOMContentLoaded",()=>{loadApps();setInterval(loadApps,6000);});

function tab(id,btn){
  document.querySelectorAll(".panel").forEach(p=>p.classList.remove("active"));
  document.querySelectorAll(".nav-item").forEach(b=>b.classList.remove("active"));
  document.getElementById("panel-"+id).classList.add("active");
  btn.classList.add("active");
  if(id==="logs")populateLogSel();
  if(id==="system")loadSystem();
}

async function refreshAll(){await loadApps();toast("Refreshed","ok");}

async function api(path,method="GET",body=null){
  try{
    const opts={method,headers:{}};
    if(body){opts.body=JSON.stringify(body);opts.headers["Content-Type"]="application/json";}
    const r=await fetch(path,opts);return await r.json();
  }catch{return null;}
}

function toast(msg,type=""){
  const box=document.getElementById("toast");
  const el=document.createElement("div");
  el.className="tmsg "+type;el.textContent=msg;box.appendChild(el);
  requestAnimationFrame(()=>el.classList.add("show"));
  setTimeout(()=>{el.classList.remove("show");setTimeout(()=>el.remove(),300);},3500);
}

function esc(s){return String(s??"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}

async function loadApps(){
  const r=await api("/api/apps");if(!r?.ok)return;
  allApps=r.apps||{};
  const total=Object.keys(allApps).length;
  const running=Object.values(allApps).filter(a=>a.status==="running").length;
  const stopped=Object.values(allApps).filter(a=>a.status==="stopped").length;
  const errors=Object.values(allApps).filter(a=>a.status==="error").length;
  document.getElementById("stat-total").textContent=total;
  document.getElementById("stat-run").textContent=running;
  document.getElementById("stat-stop").textContent=stopped;
  document.getElementById("stat-err").textContent=errors;
  document.getElementById("nb-running").textContent=running;
  renderApps();
}

function renderApps(){
  const grid=document.getElementById("app-grid");
  const entries=Object.entries(allApps);
  if(!entries.length){
    grid.innerHTML=`<div class="empty" style="grid-column:1/-1"><div class="ico">🟢</div><p>No apps yet — click <b>Deploy App</b> to get started.</p></div>`;
    return;
  }
  grid.innerHTML=entries.map(([aid,a])=>{
    const fw=a.framework||"node";
    const icon=FW_ICONS[fw]||"📦";
    const label=FW_LABELS[fw]||fw;
    const status=a.status||"stopped";
    const isRun=status==="running";
    const isBld=_buildingApps.has(aid);
    const isErr=status==="error";
    const statusText=isBld?"⟳ Building…":isRun?"● Running":isErr?"✗ Error":"○ Stopped";
    const statusCls=isBld?"building":status;
    const repoShort=a.repo?a.repo.replace(/^https?:\/\//,"").replace(/\.git$/,""):"";
    const nodeVer=a.node_version||"";
    const actions=isRun?`
      <button class="btn btn-ghost btn-sm" onclick="appAction('${aid}','stop')">⏹ Stop</button>
      <button class="btn btn-ghost btn-sm" onclick="appAction('${aid}','restart')">↺ Restart</button>
      <a href="http://localhost:${a.port}" target="_blank" class="btn btn-ghost btn-sm">↗ Open</a>
    `:isBld?`<button class="btn btn-ghost btn-sm" disabled>⟳ Building…</button>`:`
      <button class="btn btn-green btn-sm" onclick="appAction('${aid}','restart')">▶ Start</button>
    `;
    const extra=`
      ${a.repo?`<button class="btn btn-ghost btn-sm btn-icon" onclick="pullApp('${aid}')" title="Git Pull">⬇</button>`:""}
      <button class="btn btn-yellow btn-sm btn-icon" onclick="openNodeModal('${aid}')" title="Set Node Version" style="font-size:11px">⬡ Node</button>
      <button class="btn btn-ghost btn-sm btn-icon" onclick="openEnv('${aid}')" title="Env Vars">⚙</button>
      <button class="btn btn-ghost btn-sm btn-icon" onclick="showLog('${aid}')" title="Logs">📋</button>
      <button class="btn btn-ghost btn-sm btn-icon" onclick="deleteApp('${aid}')" title="Delete" style="color:var(--red)">🗑</button>
    `;
    return `<div class="app-card ${statusCls}" id="ac-${aid}">
      <div class="ac-head">
        <span class="ac-icon">${icon}</span>
        <div class="ac-info">
          <div class="ac-name">${esc(a.name)}</div>
          <div class="ac-fw">${label} · ${a.pkg_manager||"npm"}</div>
          ${repoShort?`<div class="ac-repo" title="${esc(a.repo)}">${esc(repoShort)}</div>`:""}
        </div>
      </div>
      <div class="ac-meta">
        ${a.build_cmd?`<span class="tag">build</span>`:""}
        <span class="tag">:${a.port}</span>
        ${nodeVer?`<span class="tag node-tag">⬡ Node ${esc(nodeVer)}</span>`:`<span class="tag" style="cursor:pointer" onclick="openNodeModal('${aid}')">⬡ set node ver</span>`}
        ${a.env&&Object.keys(a.env).length?`<span class="tag">${Object.keys(a.env).length} env vars</span>`:""}
      </div>
      <div class="ac-actions">
        <span class="ac-status ${statusCls}">${statusText}</span>
        <div style="margin-left:auto;display:flex;gap:5px;flex-wrap:wrap">${actions}${extra}</div>
      </div>
      <div class="inline-log" id="il-${aid}"></div>
    </div>`;
  }).join("");
}

async function appAction(aid,action){
  if(action==="stop"){
    const r=await api(`/api/apps/${aid}/stop`,"POST");
    toast(r?.ok?"App stopped":"Failed",r?.ok?"ok":"err");
    setTimeout(loadApps,600);
  }else if(action==="restart"){
    _buildingApps.add(aid);renderApps();
    const logBox=document.getElementById(`il-${aid}`);
    if(logBox){logBox.style.display="block";logBox.textContent="Starting…";}
    const r=await api(`/api/apps/${aid}/restart`,"POST");
    if(r?.ok){pollLog(aid);}else{_buildingApps.delete(aid);toast("Failed to restart","err");}
  }
}

async function pullApp(aid){
  const logBox=document.getElementById(`il-${aid}`);
  if(logBox){logBox.style.display="block";logBox.textContent="Pulling…";}
  const r=await api(`/api/apps/${aid}/pull`,"POST");
  if(r?.ok){toast("Pulled latest ✓","ok");if(logBox)logBox.textContent="✓ Pulled. Restart to apply changes.";}
  else{toast(r?.error||"Pull failed","err");if(logBox)logBox.textContent=`✗ ${r?.error||"Pull failed"}`;}
}

async function deleteApp(aid){
  const name=allApps[aid]?.name||aid;
  if(!confirm(`Delete "${name}"?\n\nThis will stop the process. Data files will be kept.`))return;
  const r=await api(`/api/apps/${aid}/delete`,"POST",{delete_files:false});
  toast(r?.ok?`${name} deleted`:"Failed",r?.ok?"ok":"err");
  setTimeout(loadApps,600);
}

function pollLog(aid){
  const interval=setInterval(async()=>{
    const r=await api(`/api/apps/${aid}/log`);if(!r)return;
    const logBox=document.getElementById(`il-${aid}`);
    if(logBox){
      logBox.innerHTML=(r.log||[]).slice(-30).map(l=>{
        const cls=l.includes("✓")?"log-ok":l.includes("✗")||l.includes("error")||l.includes("Error")?"log-err":"";
        return `<span class="${cls}">${esc(l)}</span>`;
      }).join("\n");
      logBox.scrollTop=logBox.scrollHeight;
    }
    if(r.state==="done"||r.state==="error"){
      _buildingApps.delete(aid);clearInterval(interval);
      setTimeout(loadApps,500);
    }
  },1500);
}

function showLog(aid){
  document.querySelectorAll(".nav-item").forEach(b=>b.classList.remove("active"));
  document.querySelectorAll(".panel").forEach(p=>p.classList.remove("active"));
  document.getElementById("panel-logs").classList.add("active");
  document.querySelectorAll(".nav-item")[1].classList.add("active");
  const sel=document.getElementById("log-app-sel");
  populateLogSel();
  sel.value=aid;
  loadAppLog();
}

function populateLogSel(){
  const sel=document.getElementById("log-app-sel");
  const cur=sel.value;
  sel.innerHTML='<option value="">— Select an app —</option>'+
    Object.entries(allApps).map(([aid,a])=>`<option value="${aid}">${esc(a.name)}</option>`).join("");
  if(cur)sel.value=cur;
}

async function loadAppLog(){
  const aid=document.getElementById("log-app-sel").value;
  const box=document.getElementById("log-full");
  if(!aid){box.textContent="Select an app above to view logs.";return;}
  const r=await api(`/api/apps/${aid}/log`);
  if(!r){box.textContent="Failed to load logs.";return;}
  box.innerHTML=(r.log||[]).map(l=>{
    const cls=l.includes("✓")?"log-ok":l.includes("✗")||l.toLowerCase().includes("error")?"log-err":l.toLowerCase().includes("warn")?"log-warn":"";
    return `<span class="${cls}">${esc(l)}</span>`;
  }).join("\n");
  box.scrollTop=box.scrollHeight;
}

async function loadSystem(){
  const r=await api("/api/system");
  const grid=document.getElementById("sys-grid");
  if(!r?.ok){grid.innerHTML=`<div class="empty"><p>Failed to load system info.</p></div>`;return;}
  const items=[
    {key:"Node.js",val:r.node},
    {key:"npm",val:r.npm},
    {key:"yarn",val:r.yarn},
    {key:"pnpm",val:r.pnpm},
    {key:"git",val:r.git},
    {key:"nvm",val:r.nvm},
    {key:"Server IP",val:r.ip||"—"},
  ];
  grid.innerHTML=items.map(i=>{
    const ok=i.val&&i.val!=="not found";
    return `<div class="sys-card"><div class="sys-key">${i.key}</div><div class="sys-val ${ok?"ok":"bad"}">${esc(i.val)}</div></div>`;
  }).join("");

  const nvmSec=document.getElementById("nvm-section");
  if(r.nvm&&r.nvm!=="not found"){
    nvmSec.style.display="block";
    const list=document.getElementById("nvm-ver-list");
    if(r.nvm_versions&&r.nvm_versions.length){
      list.innerHTML=r.nvm_versions.map(v=>{
        const isCur=v===r.nvm_current;
        return `<span class="nvm-ver-chip ${isCur?"active":""}" title="${isCur?"Currently active":""}">${v}${isCur?" ✓":""}</span>`;
      }).join("");
    }else{
      list.innerHTML=`<span style="font-size:11px;color:var(--muted)">No versions installed yet</span>`;
    }
  }else{
    nvmSec.style.display="none";
  }
}

async function nvmInstallGlobal(){
  const ver=document.getElementById("nvm-install-ver").value.trim();
  if(!ver){toast("Enter a version","err");return;}
  const r=await api("/api/nvm/install","POST",{version:ver});
  if(r?.ok){toast(`Installing Node ${ver} globally…`,"ok");setTimeout(loadSystem,8000);}
  else toast(r?.error||"Failed","err");
}

function openDeploy(){
  document.getElementById("deploy-modal").style.display="flex";
  document.getElementById("d-name").focus();
}
function closeDeploy(){document.getElementById("deploy-modal").style.display="none";}

function addEnvRow(k="",v=""){
  const row=document.createElement("div");row.className="env-row";
  row.innerHTML=`<input class="env-key" placeholder="KEY" value="${esc(k)}">
    <input class="env-val" placeholder="value" value="${esc(v)}">
    <button class="btn btn-ghost btn-sm btn-icon" onclick="this.parentElement.remove()" style="color:var(--red)">✕</button>`;
  document.getElementById("env-rows").appendChild(row);
}

function getEnvFromRows(containerId){
  const env={};
  document.querySelectorAll(`#${containerId} .env-row`).forEach(row=>{
    const k=row.querySelector(".env-key").value.trim();
    const v=row.querySelector(".env-val").value;
    if(k)env[k]=v;
  });
  return env;
}

async function deployApp(){
  const name=document.getElementById("d-name").value.trim();
  if(!name){toast("App name required","err");return;}
  const btn=document.getElementById("deploy-btn");
  btn.disabled=true;btn.textContent="Deploying…";
  const env=getEnvFromRows("env-rows");
  const body={
    name,
    repo:document.getElementById("d-repo").value.trim(),
    local_path:document.getElementById("d-local").value.trim(),
    framework:document.getElementById("d-fw").value,
    pkg_manager:document.getElementById("d-pkg").value,
    node_version:document.getElementById("d-node-ver").value.trim(),
    build_cmd:document.getElementById("d-build").value.trim(),
    start_cmd:document.getElementById("d-start").value.trim(),
    env,
  };
  const r=await api("/api/apps/create","POST",body);
  btn.disabled=false;btn.textContent="🚀 Deploy";
  if(r?.ok){
    closeDeploy();
    toast(`Deploying ${name}…`,"ok");
    _buildingApps.add(r.app_id);
    await loadApps();
    pollLog(r.app_id);
    const logBox=document.getElementById(`il-${r.app_id}`);
    if(logBox)logBox.style.display="block";
  }else{
    toast(r?.error||"Deploy failed","err");
  }
}

async function openEnv(aid){
  curEnvAppId=aid;
  const app=allApps[aid];
  document.getElementById("env-modal-title").textContent=`⚙ Env — ${app?.name||aid}`;
  const rows=document.getElementById("env-edit-rows");
  rows.innerHTML="";
  const r=await api(`/api/apps/${aid}/env`);
  const env=r?.env||{};
  Object.entries(env).forEach(([k,v])=>addEnvEditRow(k,v));
  document.getElementById("env-modal").style.display="flex";
}
function closeEnv(){document.getElementById("env-modal").style.display="none";curEnvAppId=null;}

function addEnvEditRow(k="",v=""){
  const row=document.createElement("div");row.className="env-row";
  row.innerHTML=`<input class="env-key" placeholder="KEY" value="${esc(k)}">
    <input class="env-val" placeholder="value" value="${esc(v)}">
    <button class="btn btn-ghost btn-sm btn-icon" onclick="this.parentElement.remove()" style="color:var(--red)">✕</button>`;
  document.getElementById("env-edit-rows").appendChild(row);
}

async function saveEnv(){
  if(!curEnvAppId)return;
  const env=getEnvFromRows("env-edit-rows");
  const r=await api(`/api/apps/${curEnvAppId}/env`,"POST",{env});
  if(r?.ok){toast("Env saved & .env written","ok");closeEnv();loadApps();}
  else toast(r?.error||"Failed","err");
}

// ── Node Version Modal ──────────────────────────────────────────────────────
async function openNodeModal(aid){
  curNodeAppId=aid;
  const app=allApps[aid];
  document.getElementById("node-modal-title").textContent=`Set Node Version — ${app?.name||aid}`;
  document.getElementById("node-ver-input").value=app?.node_version||"";
  document.getElementById("node-modal-log-wrap").style.display="none";
  document.getElementById("node-modal-log").textContent="";
  document.getElementById("node-modal-btn").disabled=false;
  document.getElementById("node-modal-btn").textContent="⬇ Install & Use";

  // Load installed versions as quick-pick chips
  const presets=document.getElementById("node-ver-presets");
  presets.innerHTML=`<span style="font-size:10px;color:var(--muted)">Loading installed versions…</span>`;
  const r=await api("/api/nvm/versions");
  if(r?.ok&&r.versions.length){
    presets.innerHTML=r.versions.map(v=>`<span class="ver-preset" onclick="document.getElementById('node-ver-input').value='${v}'">${v}</span>`).join("");
  }else{
    presets.innerHTML=`<span style="font-size:10px;color:var(--muted)">No nvm versions found — type any version above</span>`;
  }

  document.getElementById("node-modal").style.display="flex";
  setTimeout(()=>document.getElementById("node-ver-input").focus(),80);
}

function closeNodeModal(){
  document.getElementById("node-modal").style.display="none";
  curNodeAppId=null;
}

async function applyNodeVersion(){
  if(!curNodeAppId)return;
  const ver=document.getElementById("node-ver-input").value.trim();
  if(!ver){toast("Enter a Node version","err");return;}
  const btn=document.getElementById("node-modal-btn");
  btn.disabled=true;btn.textContent="Installing…";
  document.getElementById("node-modal-log-wrap").style.display="block";
  document.getElementById("node-modal-log").textContent="Sending request…";

  const r=await api(`/api/apps/${curNodeAppId}/set-node`,"POST",{version:ver});
  if(!r?.ok){
    toast(r?.error||"Failed","err");
    btn.disabled=false;btn.textContent="⬇ Install & Use";
    return;
  }
  toast(`Installing Node ${ver}…`,"ok");

  // Poll the app log for progress
  const logEl=document.getElementById("node-modal-log");
  const poll=setInterval(async()=>{
    const lr=await api(`/api/apps/${curNodeAppId}/log`);
    if(lr?.log){
      logEl.textContent=lr.log.slice(-20).join("\n");
      logEl.scrollTop=logEl.scrollHeight;
    }
    if(lr?.state==="idle"||lr?.state==="error"){
      clearInterval(poll);
      btn.disabled=false;btn.textContent="⬇ Install & Use";
      if(lr.state==="idle"){
        toast(`Node ${ver} set ✓`,"ok");
        await loadApps();
      }else{
        toast("Failed — check log","err");
      }
    }
  },1500);
}
</script>
</body>
</html>"""


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    print(f"""
╔════════════════════════════════════════════════════╗
║       NODE-MNGR  v{VERSION}  —  KillTheHost              ║
║                                                    ║
║   Browser UI → http://localhost:{PORT}                 ║
╚════════════════════════════════════════════════════╝
""")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[NODE-MNGR] Listening on http://0.0.0.0:{PORT}", flush=True)

    def _open_browser():
        time.sleep(1)
        webbrowser.open(f"http://localhost:{PORT}")

    threading.Thread(target=_open_browser, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[NODE-MNGR] Shutting down…")
        for aid in list(_processes.keys()):
            stop_app(aid)
        server.shutdown()


if __name__ == "__main__":
    main()
