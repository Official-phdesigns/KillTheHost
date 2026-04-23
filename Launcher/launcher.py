#!/usr/bin/env python3
"""
╔════╗                                        ╔════╗
║      KillTheHost  —  Unified Launcher v1.4       ║
║                                                  ║
║      Located at KillTheHost/Launcher/assets/     ║
║   Run via launch.bat / launch.sh in repo root    ║
║                                                  ║
║    A control panel opens automatically in your   ║
║         browser at http://localhost:5000         ║
║                                                  ║
║           Zero external dependencies.            ║
║      Pure Python 3.8+ standard library only.     ║
╚════╝                                        ╚════╝
"""

import sys
import os
import signal
import json
import socket
import platform
import subprocess
import threading
import webbrowser
import time
from pathlib import Path
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

# ────
#  CONFIG
# ────

LAUNCHER_PORT = 5000
SYSTEM        = platform.system()          # "Linux" | "Darwin" | "Windows"
BASE          = Path(__file__).parent.resolve()
VERSION       = "1.4"

def _get_docker_version() -> str:
    """Return Docker version string, or 'Not found' if unavailable."""
    try:
        r = subprocess.run(
            ["docker", "--version"],
            capture_output=True, text=True, timeout=4
        )
        if r.returncode == 0:
            # "Docker version 24.0.5, build abc1234" -> "24.0.5"
            parts = r.stdout.strip().split()
            return parts[2].rstrip(",") if len(parts) >= 3 else r.stdout.strip()
    except Exception:
        pass
    return "Not found"

DOCKER_VERSION = _get_docker_version()

SERVICES = {
    "php_mngr": {
        "label"    : "PHP-MNGR",
        "subtitle" : "PHP Project Manager",
        "version"  : "v2.5",
        "dir"      : "assets/main/PHP-MNGR v2.5",
        "script"   : "phpmanager.py",
        "port"     : 4280,
        "color"    : "#4A9EFF",
        "needs_sg" : True,    # Linux: wrap with  sg docker -c "..."
    },
    "db_3ngin3": {
        "label"    : "DB-3NGIN3",
        "subtitle" : "Database Service Manager",
        "version"  : "v1.2",
        "dir"      : "assets/main/DB-3NGIN3 v1.2",
        "script"   : "db3ngin3.py",
        "port"     : 7734,
        "color"    : "#FF5C5C",
        "needs_sg" : False,
    },
    "mail_srvr": {
        "label"    : "MAIL-SRVR",
        "subtitle" : "Integrated Mail Server",
        "version"  : "v1.0",
        "dir"      : "assets/main/MAIL-SRVR v1.0",
        "script"   : "mailserver.py",
        "port"     : 6060,
        "color"    : "#A78BFA",
        "needs_sg" : False,
    },
    "stax_mngr": {
        "label"    : "STAX-MNGR",
        "subtitle" : "Docker Stack Manager",
        "version"  : "v1.0",
        "dir"      : "assets/main/STAX-MNGR v1.0",
        "script"   : "staxmngr.py",
        "port"     : 6161,
        "color"    : "#00c896",
        "needs_sg" : True,
    },
    "node_mngr": {
        "label"    : "NODE-MNGR",
        "subtitle" : "Node.js & React Project Manager",
        "version"  : "v1.0",
        "dir"      : "assets/main/NODE-MNGR v1.0",
        "script"   : "nodemngr.py",
        "port"     : 7272,
        "color"    : "#F7DF1E",
        "needs_sg" : False,
    },
}


# ────
#  PROCESS MANAGER
# ────

class ServiceProcess:
    """Thread-safe wrapper around one script's subprocess."""

    def __init__(self, key: str, cfg: dict):
        self.key          = key
        self.cfg          = cfg
        self._proc        = None
        self._lock        = threading.Lock()
        self.log          = []                   # list of {ts, text, level}
        self._start_time  = None

    # ── public ────

    @property
    def running(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    @property
    def uptime(self) -> str:
        if not self.running or self._start_time is None:
            return ""
        secs = int(time.monotonic() - self._start_time)
        m, s = divmod(secs, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"

    def start(self) -> dict:
        if self.running:
            return self._err("Already running.")

        script_path = BASE / self.cfg["dir"] / self.cfg["script"]
        if not script_path.exists():
            return self._err(
                f"Script not found: {script_path}  |  "
                "Make sure launcher.py is in KillTheHost/Launcher/"
            )

        # Wait up to 5s for the port to be free (e.g. after a recent stop)
        port = self.cfg["port"]
        for _ in range(10):
            if not port_in_use(port):
                break
            self._log_entry(f"Port {port} still in use — waiting...", "warn")
            time.sleep(0.5)
        else:
            return self._err(
                f"Port {port} is still occupied. "
                "Another process may be holding it. Try again in a moment."
            )

        cmd = self._build_cmd(script_path)
        self._log_entry("Starting: " + " ".join(str(c) for c in cmd), "info")

        flags   = subprocess.CREATE_NO_WINDOW if SYSTEM == "Windows" else 0
        # On Linux/macOS put the child in its own process group so that
        # killing the sg wrapper also kills all its children.
        preexec = os.setsid if SYSTEM != "Windows" else None

        try:
            with self._lock:
                self._proc = subprocess.Popen(
                    cmd,
                    cwd=str(script_path.parent),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    creationflags=flags,
                    preexec_fn=preexec,
                )
                self._start_time = time.monotonic()
        except FileNotFoundError as exc:
            return self._err(f"Launch failed: {exc}")

        threading.Thread(target=self._stream, daemon=True).start()
        self._log_entry(
            f"Panel starting at http://localhost:{self.cfg['port']}", "success"
        )
        return {"ok": True}

    def stop(self) -> dict:
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return self._err("Not running.")
        self._log_entry("Stopping...", "info")
        try:
            if SYSTEM != "Windows":
                # Kill the entire process group so sg's python3 child
                # doesn't survive as an orphan holding the port
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    proc.terminate()
            else:
                proc.terminate()
            proc.wait(timeout=6)
        except subprocess.TimeoutExpired:
            self._log_entry("Terminate timed out — killing.", "warn")
            try:
                if SYSTEM != "Windows":
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                else:
                    proc.kill()
            except (ProcessLookupError, OSError):
                proc.kill()
        self._start_time = None
        self._log_entry("Stopped.", "info")
        return {"ok": True}

    def status(self) -> dict:
        return {
            "running": self.running,
            "uptime" : self.uptime,
            "log"    : self.log[-200:],      # last 200 entries per poll
        }

    # ── private ────

    def _build_cmd(self, script_path: Path) -> list:
        python = sys.executable
        if SYSTEM == "Linux" and self.cfg.get("needs_sg"):
            # sg docker makes the docker group active without re-login
            return ["sg", "docker", "-c",
                    f'"{python}" "{script_path.name}"']
        return [python, str(script_path.name)]

    def _stream(self):
        try:
            for line in self._proc.stdout:
                s = line.rstrip()
                if s:
                    self._log_entry(s, "output")
        except Exception:
            pass
        code = self._proc.wait()
        self._start_time = None
        self._log_entry(
            f"Process exited (code {code}).",
            "info" if code == 0 else "warn",
        )

    def _log_entry(self, text: str, level: str = "output"):
        entry = {
            "ts"   : datetime.now().strftime("%H:%M:%S"),
            "text" : text,
            "level": level,
        }
        self.log.append(entry)
        if len(self.log) > 1000:
            self.log = self.log[-800:]
        print(f"[{entry['ts']}] [{self.cfg['label']}] {text}", flush=True)

    def _err(self, msg: str) -> dict:
        self._log_entry(msg, "error")
        return {"ok": False, "error": msg}


# ────
#  GLOBAL PROCESS STATE
# ────

procs: dict = {k: ServiceProcess(k, v) for k, v in SERVICES.items()}


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except (ConnectionRefusedError, OSError):
            return False


# ────
#  EMBEDDED HTML UI
# ────

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KillTheHost Launcher</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:        #212121;
    --panel:     #2d2d2d;
    --card:      #2d2d2d;
    --card-dk:   #1a1a1a;
    --card-hover:#353535;
    --border:    #404040;
    --text:      #ececec;
    --dim:       #8e8ea0;
    --muted:     #565869;
    --green:     #10a37f;
    --green-bg:  #0d2318;
    --red:       #ef4444;
    --red-bg:    #2a0f0f;
    --warning:   #d97706;
    --error:     #ef4444;
    --log-bg:    #171717;
  }

  body {
    font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont,
                 "Segoe UI", Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    display: flex;
    flex-direction: column;
    font-size: 14px;
    line-height: 1.5;
  }

  /* ── Scrollbar ──── */
  ::-webkit-scrollbar             { width: 4px; }
  ::-webkit-scrollbar-track       { background: transparent; }
  ::-webkit-scrollbar-thumb       { background: var(--border); border-radius: 2px; }
  ::-webkit-scrollbar-thumb:hover { background: var(--muted); }

  /* ── Header ──── */
  header {
    padding: 13px 24px;
    border-bottom: 1px solid var(--border);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
    background: var(--panel);
  }

  .brand {
    display: flex;
    align-items: flex-start;
    gap: 10px;
  }

  .brand-mark {
    width: 27px;
    height: 27px;
    border-radius: 7px;
    background: linear-gradient(145deg, #ff56b9 0%, #ff4fb3 72%, #c86bff 100%);
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.14);
    flex-shrink: 0;
    position: relative;
    top: 1px;
  }

  .brand-mark-glyph {
    color: #f4f4f5;
    font-size: 12px;
    font-weight: 700;
    font-family: "Menlo", "Consolas", monospace;
    letter-spacing: -0.65px;
    transform: translateY(-0.3px);
  }

  .brand-text h1 {
    font-size: 23px;
    line-height: 1.02;
    font-weight: 780;
    letter-spacing: -0.7px;
    margin: 0;
  }

  .brand-word {
    color: #f4f5fb;
    -webkit-text-fill-color: #f4f5fb;
  }

  .brand-word.the {
    background: linear-gradient(104deg, #ff5ab8 0%, #ff54b4 76%, #c96dff 100%);
    color: #ff5ab8;
    -webkit-background-clip: text;
    background-clip: text;
    -webkit-text-fill-color: transparent;
  }

  .brand-text p {
    font-size: 10.5px;
    color: #6f7284;
    margin-top: 2px;
    letter-spacing: 0.08px;
  }

  .sys-info {
    font-size: 11px;
    color: var(--muted);
    text-align: right;
    line-height: 1.9;
    font-variant-numeric: tabular-nums;
  }

  /* ── Main ──── */
  main {
    padding: 18px 24px;
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }

  /* ── Cards ──── */
  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 12px;
  }

  .card {
    background: var(--card);
    border-radius: 8px;
    border: 1px solid var(--border);
    overflow: hidden;
    transition: border-color 0.15s;
  }
  .card:hover { border-color: var(--muted); }

  .card-body { padding: 16px 18px; }

  .card-title-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 2px;
  }
  .card-title {
    font-size: 14px;
    font-weight: 700;
    color: var(--text);
  }
  .card-ver {
    font-size: 10px;
    color: var(--muted);
    font-family: "Menlo", "Consolas", monospace;
    background: var(--card-dk);
    padding: 2px 7px;
    border-radius: 4px;
    border: 1px solid var(--border);
  }
  .card-sub {
    font-size: 12px;
    color: var(--dim);
    margin-bottom: 13px;
  }

  /* ── Status ──── */
  .status-row {
    display: flex;
    align-items: center;
    gap: 7px;
    margin-bottom: 6px;
  }
  .dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    background: var(--muted);
    flex-shrink: 0;
    transition: background 0.3s, box-shadow 0.3s;
  }
  .dot.live {
    background: var(--green);
    box-shadow: 0 0 5px var(--green);
    animation: pulse 2.5s ease-in-out infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50%       { opacity: 0.3; }
  }
  .status-text { font-size: 12px; color: var(--dim); }
  .uptime {
    font-size: 11px;
    font-family: "Menlo", "Consolas", monospace;
    color: var(--green);
  }
  .port-badge {
    margin-left: auto;
    font-size: 12px;
    font-weight: 600;
    font-family: "Menlo", "Consolas", monospace;
    color: var(--dim);
  }

  hr.div {
    border: none;
    border-top: 1px solid var(--border);
    margin: 11px 0;
  }

  /* ── Service Buttons ──── */
  .btn-row { display: flex; gap: 8px; margin-bottom: 12px; }

  .btn {
    flex: 1;
    padding: 7px 0;
    border: none;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    transition: filter 0.12s, transform 0.1s;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 5px;
  }
  .btn:disabled         { opacity: 0.2; cursor: not-allowed; transform: none !important; }
  .btn:not(:disabled):hover  { filter: brightness(1.15); }
  .btn:not(:disabled):active { transform: scale(0.97); }

  .btn-start {
    background: var(--green);
    color: #fff;
  }
  .btn-stop {
    background: transparent;
    color: var(--dim);
    border: 1px solid var(--border);
  }
  .btn-stop:not(:disabled):hover {
    background: var(--card-hover);
    color: var(--text);
    border-color: var(--muted);
    filter: none;
  }

  .link-open {
    font-size: 11px;
    color: var(--muted);
    text-decoration: none;
    display: inline-flex;
    align-items: center;
    gap: 4px;
    transition: color 0.15s;
  }
  .link-open:hover { color: var(--green); }

  /* ── Global bar ──── */
  .global-bar {
    background: var(--panel);
    border-radius: 8px;
    border: 1px solid var(--border);
    padding: 9px 18px;
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  .global-label {
    font-size: 11px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.6px;
    margin-right: auto;
  }

  .gbtn {
    padding: 6px 16px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    transition: filter 0.12s, transform 0.1s;
    border: none;
    display: flex;
    align-items: center;
    gap: 5px;
  }
  .gbtn:not(:disabled):active { transform: scale(0.97); }
  .gbtn:not(:disabled):hover  { filter: brightness(1.15); }

  .gbtn-start-all { background: var(--green); color: #fff; }
  .gbtn-stop-all  {
    background: #c0392b;
    color: #fff;
    border: none;
  }
  .gbtn-stop-all:hover {
    background: #a93226;
    color: #fff;
    filter: none;
  }

  /* ── Log ──── */
  .log-section { display: flex; flex-direction: column; flex: 1; }

  .log-header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 7px;
    flex-wrap: wrap;
  }
  .log-title {
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.8px;
    color: var(--muted);
    text-transform: uppercase;
  }
  .log-filters { display: flex; gap: 4px; margin-left: auto; }

  .fbtn, .cbtn {
    font-size: 11px;
    padding: 2px 9px;
    border-radius: 4px;
    border: 1px solid var(--border);
    background: transparent;
    color: var(--muted);
    cursor: pointer;
    transition: all 0.12s;
  }
  .fbtn.active {
    background: var(--green-bg);
    color: var(--green);
    border-color: var(--green);
  }
  .fbtn:not(.active):hover, .cbtn:hover {
    background: var(--card-hover);
    color: var(--text);
    border-color: var(--muted);
  }

  #log-box {
    flex: 1;
    background: var(--log-bg);
    border-radius: 8px;
    border: 1px solid var(--border);
    padding: 11px 14px;
    overflow-y: auto;
    font-family: "Menlo", "Consolas", "Courier New", monospace;
    font-size: 11.5px;
    line-height: 1.7;
    min-height: 200px;
    max-height: 320px;
  }

  .ll { display: flex; gap: 10px; align-items: baseline; }
  .ll + .ll { margin-top: 1px; }
  .ll-ts  { color: var(--muted); flex-shrink: 0; font-size: 10.5px; }
  .ll-src { color: var(--muted); flex-shrink: 0; min-width: 74px; font-size: 10.5px; }
  .ll-msg { color: var(--dim);   word-break: break-all; }
  .ll.success .ll-msg { color: var(--green); }
  .ll.warn    .ll-msg { color: var(--warning); }
  .ll.error   .ll-msg { color: var(--error); }
  .ll.info    .ll-msg { color: #8e8ea0; }
  .ll.output  .ll-msg { color: #707070; }

  /* ── Footer ──── */
  footer {
    text-align: center;
    padding: 9px;
    font-size: 11px;
    color: var(--muted);
    border-top: 1px solid var(--border);
    background: var(--panel);
  }
  footer a { color: var(--dim); text-decoration: none; }
  footer a:hover { color: var(--green); }

  @media (max-width: 860px) {
    .cards { grid-template-columns: 1fr; }
    header { flex-direction: column; align-items: flex-start; }
    .brand-text h1 { font-size: 20px; }
    .sys-info { text-align: left; }
    main { padding: 14px; }
  }
</style>
</head>
<body>

<header>
  <div class="brand">
    <div class="brand-mark" aria-hidden="true">
      <span class="brand-mark-glyph">&gt;_</span>
    </div>
    <div class="brand-text">
      <h1><span class="brand-word">Kill</span><span class="brand-word the">The</span><span class="brand-word">Host</span></h1>
      <p>Local development &rarr; public web, without friction.</p>
    </div>
  </div>
  <div class="sys-info" id="sys-info">&#8230;</div>
</header>

<main>
  <div class="cards" id="cards-root"></div>

  <div class="global-bar">
    <span class="global-label">Global controls</span>
    <button class="gbtn gbtn-start-all" onclick="startAll()">&#9654;&#9654; Start All</button>
    <button class="gbtn gbtn-stop-all"  onclick="stopAll()">&#9632;&#9632; Stop All</button>
    <button class="gbtn" id="open-all-btn" onclick="openAllPanels()" style="display:none;background:#444;color:#fff;border:1px solid #666;">&#8599; Open All Panels</button>
  </div>

  <div class="log-section">
    <div class="log-header">
      <span class="log-title">Console Output</span>
      <div class="log-filters">
        <button class="fbtn active" data-f="all"       onclick="setFilter('all',this)">All</button>
        <button class="fbtn"        data-f="php_mngr"  onclick="setFilter('php_mngr',this)">PHP-MNGR</button>
        <button class="fbtn"        data-f="db_3ngin3" onclick="setFilter('db_3ngin3',this)">DB-3NGIN3</button>
        <button class="fbtn"        data-f="mail_srvr" onclick="setFilter('mail_srvr',this)">MAIL-SRVR</button>
        <button class="fbtn"        data-f="stax_mngr" onclick="setFilter('stax_mngr',this)">STAX-MNGR</button>
        <button class="fbtn"        data-f="node_mngr" onclick="setFilter('node_mngr',this)">NODE-MNGR</button>
      </div>
      <button class="cbtn" onclick="clearLog()">Clear All</button>
    </div>
    <div id="log-box"></div>
  </div>
</main>

<footer>
  KillTheHost &nbsp;|&nbsp;
  <a href="https://killthehost.com" target="_blank">killthehost.com</a>
  &nbsp;|&nbsp; AGPL-3.0 &nbsp;|&nbsp; &copy; 2026 PhDesigns LLC
  &nbsp;&mdash;&nbsp;
  KillTheHost Launcher v%%VERSION%% &nbsp;|&nbsp; %%SYSTEM%%
</footer>

<script>
const SERVICES = %%SERVICES_JSON%%;
let logFilter  = "all";
let logBuffer  = [];
let lastIdx    = {};

function removeOpenAllButton() {
  // Remove any dynamically added "Open All Panels" UI elements
  document.querySelectorAll('.open-all-panels, button[data-open-all]').forEach(el => el.remove());
  // Also remove buttons by exact text content if found
  document.querySelectorAll('button').forEach(b => {
    if (b.textContent && b.textContent.trim().includes('Open All Panels')) b.remove();
  });
  // Fallback: inject CSS to hide any remaining elements
  if (!document.getElementById('hide-open-all-style')) {
    const st = document.createElement('style');
    st.id = 'hide-open-all-style';
    st.textContent = `button.open-all-panels, button[data-open-all] { display: none !important; }`;
    document.head.appendChild(st);
  }
}

window.addEventListener("DOMContentLoaded", () => {
  document.getElementById("sys-info").innerHTML =
    "KillTheHost v%%VERSION%%<br>Python %%PYVER%% &nbsp;|&nbsp; %%SYSTEM%%<br>Docker %%DOCKER_VER%%";
  buildCards();
  removeOpenAllButton();
  pollAll();
  setInterval(pollAll, 1500);
});

// ── Build cards ────
function buildCards() {
  const root = document.getElementById("cards-root");
  for (const [key, cfg] of Object.entries(SERVICES)) {
    const el = document.createElement("div");
    el.className = "card";
    el.innerHTML = `
      <div class="card-body">
        <div class="card-title-row">
          <span class="card-title">${cfg.label}</span>
          <span class="card-ver">${cfg.version}</span>
        </div>
        <div class="card-sub">${cfg.subtitle}</div>
        <div class="status-row">
          <div class="dot" id="dot-${key}"></div>
          <span class="status-text" id="st-${key}">Stopped</span>
          <span class="uptime" id="up-${key}"></span>
          <span class="port-badge">:${cfg.port}</span>
        </div>
        <hr class="div">
        <div class="btn-row">
          <button class="btn btn-start" id="bs-${key}"
            onclick="svcAction('${key}','start')">&#9654; Start</button>
          <button class="btn btn-stop" id="bp-${key}" disabled
            onclick="svcAction('${key}','stop')">&#9632; Stop</button>
        </div>
        <a class="link-open" href="http://localhost:${cfg.port}" target="_blank">
          &#8599; Open Panel &mdash; localhost:${cfg.port}
        </a>
      </div>`;
    root.appendChild(el);
  }
}

// ── API calls ────
async function api(path, method="GET") {
  try {
    const r = await fetch(path, { method });
    return await r.json();
  } catch(e) { return { ok: false }; }
}

async function svcAction(key, action) {
  document.getElementById(action === "start" ? "bs-" + key : "bp-" + key).disabled = true;
  await api("/api/" + key + "/" + action, "POST");
}

async function startAll() {
  const ordered = ["php_mngr", "db_3ngin3", "mail_srvr", "stax_mngr", "node_mngr"];
  for (let i = 0; i < ordered.length; i++) {
    if (!SERVICES[ordered[i]]) continue;
    await api("/api/" + ordered[i] + "/start", "POST");
  }
  // Remove the legacy "Open All Panels" button if any UI tried to add it
  removeOpenAllButton();
}

function openAllPanels() {
  window.open("http://localhost:7734", "_blank");
  window.open("http://localhost:4280", "_blank");
  window.open("http://localhost:6060", "_blank");
  window.open("http://localhost:6161", "_blank");
  window.open("http://localhost:7272", "_blank");
}
async function stopAll() {
  for (const k of Object.keys(SERVICES)) await api(`/api/${k}/stop`, "POST");
}

// ── Polling ────
async function pollAll() { removeOpenAllButton();
  const res = await api("/api/status");
  if (!res) return;
  for (const [key, s] of Object.entries(res)) {
    const dot = document.getElementById(`dot-${key}`);
    if (!dot) continue;
    const bStart = document.getElementById(`bs-${key}`);
    const bStop  = document.getElementById(`bp-${key}`);
    if (s.running) {
      dot.classList.add("live");
      document.getElementById(`st-${key}`).textContent = "Running";
      document.getElementById(`up-${key}`).textContent = s.uptime;
      bStart.disabled = true;
      bStop.disabled  = false;
    } else {
      dot.classList.remove("live");
      document.getElementById(`st-${key}`).textContent = "Stopped";
      document.getElementById(`up-${key}`).textContent = "";
      bStart.disabled = false;
      bStop.disabled  = true;
    }
    const prev = lastIdx[key] ?? 0;
    const news = s.log.slice(prev);
    lastIdx[key] = s.log.length;
    news.forEach(e => addEntry(key, e));
  }
}

// ── Log ────
function addEntry(key, e) {
  logBuffer.push({ key, ...e });
  if (logBuffer.length > 2000) logBuffer = logBuffer.slice(-1800);
  if (logFilter === "all" || logFilter === key) renderLine(key, e);
}

function renderLine(key, e) {
  const box  = document.getElementById("log-box");
  const cfg  = SERVICES[key] || {};
  const line = document.createElement("div");
  line.className = "ll " + e.level;
  line.innerHTML =
    `<span class="ll-ts">[${e.ts}]</span>` +
    `<span class="ll-src">[${cfg.label||key}]</span>` +
    `<span class="ll-msg">${esc(e.text)}</span>`;
  box.appendChild(line);
  box.scrollTop = box.scrollHeight;
}

function esc(s) {
  return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

function clearLog() {
  logBuffer = [];
  document.getElementById("log-box").innerHTML = "";
  // lastIdx intentionally kept — preserves position in each service log
  // so the next poll only appends NEW lines, not replays everything
}

function setFilter(f, btn) {
  logFilter = f;
  document.querySelectorAll(".fbtn").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  const box = document.getElementById("log-box");
  box.innerHTML = "";
  (f === "all" ? logBuffer : logBuffer.filter(e => e.key === f))
    .forEach(e => renderLine(e.key, e));
}
</script>
</body>
</html>"""


# ────
#  HTTP REQUEST HANDLER
# ────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass   # silence default request logs

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/":
            self._html()
        elif path == "/api/status":
            self._json({k: p.status() for k, p in procs.items()})
        elif path.startswith("/images/"):
            self._serve_image(path[8:])
        else:
            self._raw(404, "text/plain", b"Not found")

    def do_POST(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        # /api/<key>/<start|stop>
        if len(parts) == 3 and parts[0] == "api" and parts[2] in ("start", "stop"):
            key = parts[1]
            if key not in procs:
                self._json({"ok": False, "error": "Unknown service"})
                return
            result = procs[key].start() if parts[2] == "start" else procs[key].stop()
            self._json(result)
        else:
            self._raw(404, "text/plain", b"Not found")

    # ── helpers ────

    def _serve_image(self, filename: str):
        img_path = BASE / "images" / filename
        if not img_path.exists() or not img_path.is_file():
            self._raw(404, "text/plain", b"Not found")
            return
        ext_map = {
            ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".svg": "image/svg+xml", ".webp": "image/webp",
            ".gif": "image/gif",    ".ico": "image/x-icon",
        }
        ct = ext_map.get(img_path.suffix.lower(), "application/octet-stream")
        self._raw(200, ct, img_path.read_bytes())

    def _html(self):
        services_for_js = json.dumps({
            k: {f: v[f] for f in ("label","subtitle","version","port","color")}
            for k, v in SERVICES.items()
        })
        html = (
            HTML_TEMPLATE
            .replace("%%VERSION%%",       VERSION)
            .replace("%%SYSTEM%%",        SYSTEM)
            .replace("%%PYVER%%",         sys.version.split()[0])
            .replace("%%DOCKER_VER%%",    DOCKER_VERSION)
            .replace("%%SERVICES_JSON%%", services_for_js)
        )
        self._raw(200, "text/html; charset=utf-8", html.encode("utf-8"))

    def _json(self, data: dict):
        body = json.dumps(data).encode("utf-8")
        self._raw(200, "application/json", body)

    def _raw(self, code: int, ct: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


# ────
#  PRE-FLIGHT CHECKS
# ────

def preflight() -> list:
    issues = []

    if sys.version_info < (3, 8):
        issues.append(
            f"Python 3.8+ required — found {sys.version.split()[0]}. "
            "Upgrade: https://python.org/downloads/"
        )

    if port_in_use(LAUNCHER_PORT):
        issues.append(
            f"Port {LAUNCHER_PORT} is already in use. "
            "Change LAUNCHER_PORT at the top of launcher.py."
        )

    try:
        r = subprocess.run(
            ["docker", "info"], capture_output=True, text=True, timeout=4
        )
        if r.returncode != 0:
            issues.append(
                "Docker installed but not reachable. "
                "Linux fix: sudo usermod -aG docker $USER  (then log out & back in)"
            )
    except FileNotFoundError:
        issues.append("Docker not found. Install: https://docs.docker.com/engine/install/")
    except subprocess.TimeoutExpired:
        issues.append("Docker check timed out — Docker may not be running.")

    for key, cfg in SERVICES.items():
        script = BASE / cfg["dir"] / cfg["script"]
        if not script.exists():
            issues.append(
                f"[{cfg['label']}] Script not found: {script}  "
                "— make sure launcher.py is in KillTheHost/Launcher/"
            )

    return issues


# ────
#  ENTRY POINT
# ────

def main():
    print(f"""
╔════╗
║         KillTheHost Launcher  v{VERSION:<23}║
╚════╝
  Platform : {SYSTEM}
  Python   : {sys.version.split()[0]}
  Root     : {BASE}
""")

    issues = preflight()
    if issues:
        print("  Pre-flight warnings:")
        for w in issues:
            print(f"    • {w}")
        print()

    # Seed the in-memory log so the browser sees startup info
    for key, cfg in SERVICES.items():
        script = BASE / cfg["dir"] / cfg["script"]
        ok     = script.exists()
        procs[key]._log_entry(
            f"Script {'found' if ok else 'NOT FOUND'}: {script}",
            "info" if ok else "error",
        )
    for w in issues:
        list(procs.values())[0]._log_entry("WARNING: " + w, "warn")

    try:
        server = HTTPServer(("127.0.0.1", LAUNCHER_PORT), Handler)
    except OSError as exc:
        print(f"\n  [FATAL] Cannot bind to port {LAUNCHER_PORT}: {exc}")
        print(f"  Change LAUNCHER_PORT at the top of launcher.py.\n")
        sys.exit(1)

    url = f"http://localhost:{LAUNCHER_PORT}"
    print(f"  Control panel : {url}")
    print(f"  Press Ctrl+C  : stop launcher\n")

    def _open_browser():
        time.sleep(0.9)
        webbrowser.open(url)

    threading.Thread(target=_open_browser, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down...")
        for p in procs.values():
            if p.running:
                p.stop()
        server.shutdown()
        print("  Done. Goodbye.\n")


if __name__ == "__main__":
    main()
