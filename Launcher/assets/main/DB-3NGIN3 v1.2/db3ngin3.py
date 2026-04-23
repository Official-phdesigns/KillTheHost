#!/usr/bin/env python3
"""
DB-3NGIN3  —  Local database manager for Linux/Ubuntu
Requires : Python 3.8+  |  Docker
Run      : python3 db3ngin3.py
"""

import json
import os
import subprocess
import threading
import time
import webbrowser
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# ── Persistent storage ────────────────────────────────────────────────────────

DATA_DIR  = Path.home() / ".db3ngin3"
DATA_FILE = DATA_DIR / "instances.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Database type definitions ─────────────────────────────────────────────────

DB_CONFIGS = {
    "postgres": {
        "label":    "PostgreSQL",
        "icon":     "🐘",
        "color":    "#336791",
        "versions": ["16", "15", "14", "13"],
        "image":    "postgres:{version}-alpine",
        "port":     5432,
        "env":      ["POSTGRES_PASSWORD=postgres", "POSTGRES_USER=postgres"],
        "data_dir": "/var/lib/postgresql/data",
        "user":     "postgres",
        "password": "postgres",
    },
    "mysql": {
        "label":    "MySQL",
        "icon":     "🐬",
        "color":    "#00758F",
        "versions": ["8.3", "8.0", "5.7"],
        "image":    "mysql:{version}",
        "port":     3306,
        "env":      ["MYSQL_ROOT_PASSWORD=root", "MYSQL_USER=admin", "MYSQL_PASSWORD=admin"],
        "data_dir": "/var/lib/mysql",
        "user":     "admin",
        "password": "admin",
    },
    "mariadb": {
        "label":    "MariaDB",
        "icon":     "🦭",
        "color":    "#C0765A",
        "versions": ["11.3", "10.11", "10.6"],
        "image":    "mariadb:{version}",
        "port":     3307,
        "env":      ["MARIADB_ROOT_PASSWORD=root"],
        "data_dir": "/var/lib/mysql",
        "user":     "root",
        "password": "root",
    },
    "redis": {
        "label":    "Redis",
        "icon":     "⚡",
        "color":    "#DC382D",
        "versions": ["7.2", "7.0", "6.2"],
        "image":    "redis:{version}-alpine",
        "port":     6379,
        "env":      [],
        "data_dir": "/data",
        "user":     None,
        "password": None,
    },
    "mongodb": {
        "label":    "MongoDB",
        "icon":     "🍃",
        "color":    "#4CAF50",
        "versions": ["7.0", "6.0", "5.0"],
        "image":    "mongo:{version}",
        "port":     27017,
        "env":      ["MONGO_INITDB_ROOT_USERNAME=admin", "MONGO_INITDB_ROOT_PASSWORD=admin"],
        "data_dir": "/data/db",
        "user":     "admin",
        "password": "admin",
    },
}

# ── Instance persistence ───────────────────────────────────────────────────────

def load_instances():
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_instances(instances):
    with open(DATA_FILE, "w") as f:
        json.dump(instances, f, indent=2)

# ── Docker helpers ─────────────────────────────────────────────────────────────

def docker_available():
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False

def container_status(name):
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", name],
            capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip() if r.returncode == 0 else "not_found"
    except Exception:
        return "error"

# ── CRUD operations ────────────────────────────────────────────────────────────

def create_instance(name, db_type, version, port, username=None, password=None):
    if db_type not in DB_CONFIGS:
        return {"success": False, "error": f"Unknown database type: {db_type}"}
    instances = load_instances()
    for inst in instances.values():
        if inst["port"] == int(port):
            return {"success": False, "error": f"Port {port} is already in use by '{inst['name']}'"}
    cfg = DB_CONFIGS[db_type]
    # Use provided credentials or fall back to defaults
    default_user = cfg.get("user")
    default_pass = cfg.get("password")
    final_user = (username.strip() if username and username.strip() else None) or default_user
    final_pass = (password if password and password.strip() else None) or default_pass
    iid = f"{db_type}_{int(time.time()*1000)}"
    instances[iid] = {
        "id":      iid,
        "name":    name,
        "type":    db_type,
        "version": version,
        "port":    int(port),
        "status":  "stopped",
        "created": time.strftime("%Y-%m-%d"),
        "user":    final_user,
        "password": final_pass,
    }
    save_instances(instances)
    return {"success": True, "id": iid}

def start_instance(iid):
    instances = load_instances()
    if iid not in instances:
        return {"success": False, "error": "Instance not found"}
    inst  = instances[iid]
    cfg   = DB_CONFIGS[inst["type"]]
    name  = f"db3ngin3_{iid}"
    image = cfg["image"].replace("{version}", inst["version"])
    status = container_status(name)

    if status == "running":
        return {"success": True}

    if status == "exited":
        r = subprocess.run(["docker", "start", name], capture_output=True, text=True)
        if r.returncode == 0:
            instances[iid]["status"] = "running"
            save_instances(instances)
            return {"success": True}
        return {"success": False, "error": r.stderr.strip()}

    # Create brand-new container
    data_path = DATA_DIR / "data" / iid
    data_path.mkdir(parents=True, exist_ok=True)

    # Build env vars with stored credentials
    user     = inst.get("user")
    password = inst.get("password")
    db_type  = inst["type"]

    if db_type == "postgres":
        env = [f"POSTGRES_USER={user}", f"POSTGRES_PASSWORD={password}"]
    elif db_type == "mysql":
        env = [f"MYSQL_ROOT_PASSWORD={password}", f"MYSQL_USER={user}", f"MYSQL_PASSWORD={password}"]
    elif db_type == "mariadb":
        env = [f"MARIADB_ROOT_PASSWORD={password}"]
        if user and user != "root":
            env += [f"MARIADB_USER={user}", f"MARIADB_PASSWORD={password}"]
    elif db_type == "redis":
        env = []  # password handled via command arg below
    elif db_type == "mongodb":
        env = [f"MONGO_INITDB_ROOT_USERNAME={user}", f"MONGO_INITDB_ROOT_PASSWORD={password}"]
    else:
        env = cfg["env"]

    cmd = [
        "docker", "run", "-d",
        "--name", name,
        "-p", f"127.0.0.1:{inst['port']}:{cfg['port']}",
        "--restart", "no",
        "-v", f"{data_path}:{cfg['data_dir']}",
    ]
    for e in env:
        cmd += ["-e", e]
    cmd.append(image)

    # Redis password is passed as a server argument, not an env var
    if db_type == "redis" and password:
        cmd += ["redis-server", "--requirepass", password]

    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0:
        instances[iid]["status"] = "running"
        save_instances(instances)
        return {"success": True}
    return {"success": False, "error": r.stderr.strip()}

def stop_instance(iid):
    instances = load_instances()
    if iid not in instances:
        return {"success": False, "error": "Instance not found"}
    r = subprocess.run(
        ["docker", "stop", f"db3ngin3_{iid}"],
        capture_output=True, text=True
    )
    if r.returncode == 0:
        instances[iid]["status"] = "stopped"
        save_instances(instances)
        return {"success": True}
    return {"success": False, "error": r.stderr.strip()}

def delete_instance(iid, remove_data=False):
    instances = load_instances()
    if iid not in instances:
        return {"success": False, "error": "Instance not found"}
    name = f"db3ngin3_{iid}"
    subprocess.run(["docker", "stop", name], capture_output=True)
    subprocess.run(["docker", "rm",   name], capture_output=True)
    if remove_data:
        import shutil
        d = DATA_DIR / "data" / iid
        if d.exists():
            shutil.rmtree(d)
    del instances[iid]
    save_instances(instances)
    return {"success": True}

def refresh_statuses():
    """Sync stored status with live Docker state."""
    instances = load_instances()
    changed = False
    for iid, inst in instances.items():
        live = container_status(f"db3ngin3_{iid}")
        new_status = "running" if live == "running" else "stopped"
        if inst["status"] != new_status:
            inst["status"] = new_status
            changed = True
    if changed:
        save_instances(instances)
    return instances

# ── Embedded HTML ──────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KillTheHost - DB-3NGIN3</title>
    <link rel="shortcut icon" href="https://www.phdesigns.net/img/favicon.ico" type="image/x-icon">
    <link rel="icon" href="https://www.phdesigns.net/img/favicon.ico" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>

/* ── Reset & Tokens ── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg:      #212121;
  --sidebar: #171717;
  --surface: #2f2f2f;
  --raised:  #383838;
  --hover:   #404040;
  --line:    #3a3a3a;
  --line2:   #4a4a4a;
  --t1: #ececec; --t2: #acacac; --t3: #676767;
  --green:      #10a37f;
  --green-tint: rgba(16,163,127,0.12);
  --red:        #e05252;
  --red-tint:   rgba(224,82,82,0.12);
  --sans: 'Inter', system-ui, sans-serif;
  --mono: 'IBM Plex Mono', monospace;
  --r-xs:4px; --r-sm:6px; --r-md:8px; --r-lg:12px;
}
html, body {
  height:100%; background:var(--bg); color:var(--t1);
  font-family:var(--sans); font-size:14px; line-height:1.5;
  overflow:hidden; -webkit-font-smoothing:antialiased;
}

/* ── Layout ── */
.app  { display:flex; height:100vh; }
.main { flex:1; display:flex; flex-direction:column; overflow:hidden; min-width:0; }

/* ── Sidebar ── */
.sidebar {
  width:260px; flex-shrink:0;
  background:var(--sidebar); border-right:1px solid var(--line);
  display:flex; flex-direction:column; overflow:hidden;
  transition:width 0.22s cubic-bezier(0.4,0,0.2,1), border-color 0.22s;
}
.sidebar.collapsed { width:0; border-color:transparent; }

@media (max-width:680px) {
  .sidebar {
    position:fixed; top:0; left:0; height:100%; width:280px; z-index:50;
    transform:translateX(0);
    transition:transform 0.25s cubic-bezier(0.4,0,0.2,1);
    box-shadow:4px 0 24px rgba(0,0,0,0.5);
    border-right:1px solid var(--line);
  }
  .sidebar.collapsed { transform:translateX(-100%); width:280px; border-color:var(--line); }
  .sidebar-backdrop  { display:block; }
}

.sidebar-backdrop {
  display:none; position:fixed; inset:0;
  background:rgba(0,0,0,0.55); z-index:49;
  backdrop-filter:blur(2px); animation:fade-in 0.2s ease;
}
@keyframes fade-in { from{opacity:0} to{opacity:1} }

.logo {
  display:flex; align-items:flex-start; gap:10px;
  padding:16px 14px; border-bottom:1px solid var(--line); flex-shrink:0;
}
.kth-mark {
  width:32px; height:32px; border-radius:8px;
  background:linear-gradient(145deg,#ff56b9 0%,#ef63d6 62%,#c86bff 100%);
  display:flex; align-items:center; justify-content:center;
  font-family:"Menlo","Consolas",monospace; font-size:12px; font-weight:700;
  color:#fff; letter-spacing:-.6px; flex-shrink:0;
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.14);
  margin-top:1px;
}
.logo-main { display:flex; flex-direction:column; gap:1px; }
.kth-logo { display:flex; align-items:center; gap:0; font-size:18px; font-weight:800; line-height:1; }
.kth-word { color:#f4f5fb; letter-spacing:-.35px; }
.kth-word.the {
  background:linear-gradient(135deg,#ff5ab8 0%,#f468cd 55%,#c96dff 100%);
  -webkit-background-clip:text; background-clip:text;
  -webkit-text-fill-color:transparent;
}
.logo-name { font-size:13px; font-weight:700; color:var(--t1); letter-spacing:.25px; }
.logo-sub  { font-family:var(--mono); font-size:9px; color:var(--t3); margin-top:1px; text-transform:uppercase; letter-spacing:.25px; }

.nav { flex:1; overflow-y:auto; padding:8px; }
.nav-item {
  display:flex; align-items:center; gap:10px;
  padding:8px 10px; border-radius:var(--r-md); cursor:pointer;
  color:var(--t2); font-size:13px; font-weight:400;
  transition:background 0.1s, color 0.1s; user-select:none; margin-bottom:1px;
}
.nav-item:hover, .nav-item.active { background:var(--surface); color:var(--t1); }
.nav-icon { font-size:13px; flex-shrink:0; }
.nav-name { flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.nav-dot  { width:6px; height:6px; border-radius:50%; background:var(--t3); flex-shrink:0; transition:background 0.2s; }
.nav-dot.on { background:var(--green); box-shadow:0 0 5px rgba(16,163,127,0.45); animation:blink 2.4s ease-in-out infinite; }

.sidebar-foot {
  padding:10px 10px 14px; border-top:1px solid var(--line);
  flex-shrink:0; display:flex; flex-direction:column; gap:8px;
}
.docker-status { display:flex; align-items:center; gap:6px; font-family:var(--mono); font-size:10px; color:var(--t3); padding:0 2px; }
.docker-dot    { width:5px; height:5px; border-radius:50%; background:var(--t3); flex-shrink:0; }
.docker-dot.ok { background:var(--green); }

/* ── Topbar ── */
.topbar {
  height:52px; border-bottom:1px solid var(--line);
  display:flex; align-items:center; padding:0 20px; gap:10px; flex-shrink:0;
}
.topbar-title { font-size:14px; font-weight:600; }
.topbar-url {
  font-family:var(--mono); font-size:11px; color:var(--t3);
  padding:2px 8px; background:var(--sidebar);
  border:1px solid var(--line); border-radius:var(--r-xs);
}
.topbar-right { margin-left:auto; display:flex; align-items:center; gap:6px; }

.sidebar-toggle {
  width:30px; height:30px; border-radius:var(--r-sm);
  border:1px solid var(--line2); background:transparent; color:var(--t2);
  cursor:pointer; display:flex; align-items:center; justify-content:center;
  flex-shrink:0; transition:background 0.1s, color 0.1s; margin-right:4px;
}
.sidebar-toggle:hover { background:var(--surface); color:var(--t1); }
.status-lbl { display:none !important; }

/* ── Buttons ── */
.btn {
  display:inline-flex; align-items:center; justify-content:center; gap:6px;
  padding:6px 14px; border-radius:var(--r-md); border:1px solid var(--line2);
  background:transparent; color:var(--t2);
  font-family:var(--sans); font-size:13px; font-weight:500; line-height:1.4;
  cursor:pointer; white-space:nowrap;
  transition:background 0.1s, color 0.1s, border-color 0.1s;
}
.btn:hover:not(:disabled) { background:var(--surface); color:var(--t1); }
.btn:disabled { opacity:0.35; cursor:not-allowed; }

.btn-primary { background:var(--raised); color:var(--t1); border-color:var(--line2); }
.btn-primary:hover:not(:disabled) { background:var(--hover); color:var(--t1); }
.btn-go   { background:var(--green-tint); color:var(--green); border-color:rgba(16,163,127,0.25); }
.btn-go:hover:not(:disabled)   { background:rgba(16,163,127,0.2); color:var(--green); }
.btn-stop { background:var(--red-tint); color:var(--red); border-color:rgba(224,82,82,0.25); }
.btn-stop:hover:not(:disabled) { background:rgba(224,82,82,0.2); color:var(--red); }
.btn-icon { padding:6px 8px; border-color:transparent; color:var(--t3); }
.btn-icon:hover:not(:disabled) { background:var(--surface); color:var(--red); border-color:transparent; }
.btn-block { width:100%; }
.btn-sm { padding:4px 10px; font-family:var(--mono); font-size:11px; }
.btn.btn-sm.copied { color:var(--green); border-color:rgba(16,163,127,0.3); }

/* ── Content ── */
.content { flex:1; overflow-y:auto; padding:20px 20px 40px; }
.group   { margin-bottom:28px; }
.group-label { display:flex; align-items:center; gap:10px; margin-bottom:10px; }
.group-label::after { content:''; flex:1; height:1px; background:var(--line); }

/* ── Micro-typography ── */
.label {
  font-family:var(--mono); font-size:9px; font-weight:500;
  letter-spacing:1.2px; text-transform:uppercase; color:var(--t3);
}

/* ── Cards ── */
.card {
  background:var(--sidebar); border:1px solid var(--line);
  border-radius:var(--r-lg); margin-bottom:4px; overflow:hidden;
  transition:border-color 0.12s; cursor:pointer;
}
.card:hover, .card.open { border-color:var(--line2); }
.card-row { display:flex; align-items:center; gap:10px; padding:13px 14px; }
.card-icon { width:38px; height:38px; border-radius:var(--r-md); display:flex; align-items:center; justify-content:center; font-size:18px; flex-shrink:0; }
.card-body { flex:1; min-width:0; overflow:hidden; }
.card-name { font-size:14px; font-weight:500; margin-bottom:5px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.card-tags { display:flex; align-items:center; gap:5px; flex-wrap:nowrap; overflow:hidden; }
.tag { font-family:var(--mono); font-size:10px; padding:1px 6px; border-radius:var(--r-xs); background:var(--raised); color:var(--t3); border:1px solid var(--line2); flex-shrink:0; }
.card-port { flex-shrink:0; text-align:right; min-width:60px; }
.port-hint { font-family:var(--mono); font-size:9px; color:var(--t3); margin-bottom:1px; }
.port-val  { font-family:var(--mono); font-size:15px; font-weight:500; }
.card-status { display:flex; align-items:center; flex-shrink:0; }
.status-dot  { width:7px; height:7px; border-radius:50%; flex-shrink:0; }
.card-actions { display:flex; align-items:center; gap:5px; flex-shrink:0; }
.card-actions .btn { padding:5px 11px; font-size:12px; }
.running .status-dot { background:var(--green); box-shadow:0 0 6px rgba(16,163,127,0.45); animation:blink 2.4s ease-in-out infinite; }
.stopped .status-dot { background:var(--t3); }

/* ── Drawer ── */
.drawer { border-top:1px solid var(--line); padding:16px 18px 18px; background:var(--bg); animation:open-down 0.14s ease; }
@keyframes open-down { from{opacity:0;transform:translateY(-4px)} to{opacity:1;transform:none} }
.fields    { display:flex; gap:8px; margin-bottom:14px; flex-wrap:wrap; }
.field-val { font-family:var(--mono); font-size:12px; color:var(--t1); background:var(--surface); border:1px solid var(--line2); border-radius:var(--r-sm); padding:5px 10px; margin-top:4px; cursor:text; user-select:all; white-space:nowrap; }
.conn-row  { display:flex; align-items:center; gap:10px; background:var(--surface); border:1px solid var(--line2); border-radius:var(--r-sm); padding:8px 12px; margin-top:6px; }
.conn-str  { font-family:var(--mono); font-size:11px; color:var(--t2); flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

/* ── Empty state ── */
.empty { height:100%; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:10px; text-align:center; padding:40px; }
.empty-icon  { font-size:40px; opacity:0.4; }
.empty-title { font-size:15px; font-weight:500; color:var(--t2); }
.empty-sub   { font-size:13px; color:var(--t3); max-width:240px; line-height:1.7; }

/* ── Modal ── */
.overlay { position:fixed; inset:0; background:rgba(0,0,0,0.7); backdrop-filter:blur(4px); display:flex; align-items:center; justify-content:center; z-index:100; opacity:0; pointer-events:none; transition:opacity 0.15s; }
.overlay.open { opacity:1; pointer-events:auto; }
.modal { background:var(--sidebar); border:1px solid var(--line2); border-radius:var(--r-lg); width:460px; max-width:95vw; padding:24px; box-shadow:0 16px 48px rgba(0,0,0,0.7); transform:translateY(12px); transition:transform 0.2s cubic-bezier(0.22,1,0.36,1); }
.overlay.open .modal { transform:none; }
.modal-title { font-size:15px; font-weight:600; margin-bottom:18px; }
.type-grid { display:grid; grid-template-columns:repeat(5,1fr); gap:6px; margin-bottom:18px; }
.type-btn { display:flex; flex-direction:column; align-items:center; gap:5px; padding:10px 6px; border-radius:var(--r-md); border:1px solid var(--line); background:var(--bg); color:var(--t3); font-family:var(--sans); font-size:10px; font-weight:500; cursor:pointer; transition:border-color 0.1s, background 0.1s, color 0.1s; user-select:none; }
.type-btn .ti { font-size:18px; }
.type-btn:hover { border-color:var(--line2); background:var(--surface); color:var(--t2); }
.type-btn.sel   { border-color:var(--line2); background:var(--surface); color:var(--t1); }
.form-cols { display:grid; grid-template-columns:1fr 1fr; gap:12px; }
.fg { display:flex; flex-direction:column; gap:6px; margin-bottom:12px; }
.fg input, .fg select { padding:8px 10px; background:var(--bg); border:1px solid var(--line2); border-radius:var(--r-sm); color:var(--t1); font-family:var(--mono); font-size:13px; outline:none; transition:border-color 0.1s; appearance:none; width:100%; }
.fg input:focus, .fg select:focus { border-color:var(--t2); }
.modal-foot { display:flex; justify-content:flex-end; gap:8px; margin-top:18px; padding-top:16px; border-top:1px solid var(--line); }

/* ── Toast ── */
.toast { position:fixed; bottom:20px; right:20px; z-index:200; display:flex; align-items:center; gap:8px; padding:10px 16px; background:var(--surface); border:1px solid var(--line2); border-radius:var(--r-lg); font-size:13px; color:var(--t1); box-shadow:0 4px 20px rgba(0,0,0,0.5); opacity:0; transform:translateY(60px); pointer-events:none; transition:opacity 0.18s, transform 0.25s cubic-bezier(0.22,1,0.36,1); }
.toast.show    { opacity:1; transform:none; }
.toast.success { border-left:3px solid var(--green); }
.toast.error   { border-left:3px solid var(--red); }
.toast.info    { border-left:3px solid var(--line2); }

/* ── Utilities ── */
.spinner { width:12px; height:12px; border-radius:50%; border:1.5px solid rgba(255,255,255,0.15); border-top-color:currentColor; animation:spin 0.65s linear infinite; display:inline-block; flex-shrink:0; }
@keyframes spin  { to{transform:rotate(360deg)} }
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.4} }
::-webkit-scrollbar       { width:4px; }
::-webkit-scrollbar-track { background:transparent; }
::-webkit-scrollbar-thumb { background:var(--line2); border-radius:2px; }

</style>
</head>
<body>
<div class="sidebar-backdrop" id="sb-backdrop" onclick="closeSidebar()"></div>
<div class="app">

  <aside class="sidebar">
    <div class="logo">
      <div class="kth-mark">&gt;_</div>
      <div class="logo-main">
        <div class="kth-logo">
          <span class="kth-word">Kill</span><span class="kth-word the">The</span><span class="kth-word">Host</span>
        </div>
        <div class="logo-name">DB-3NGIN3</div>
        <div class="logo-sub">KillTheHost Suite · Database Engine</div>
      </div>
    </div>
    <nav class="nav">
      <div class="label" style="padding:8px 10px 6px;display:block">Instances</div>
      <div id="nav-list"></div>
    </nav>
    <div class="sidebar-foot">
      <button class="btn btn-block" onclick="openModal()">
        <svg width="13" height="13" viewBox="0 0 13 13" fill="none"><path d="M6.5 1v11M1 6.5h11" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
        New Instance
      </button>
      <div class="docker-status">
        <div class="docker-dot" id="docker-dot"></div>
        <span id="docker-label">Checking Docker…</span>
      </div>
    </div>
  </aside>

  <div class="main">
    <header class="topbar">
      <button class="sidebar-toggle" onclick="toggleSidebar()" title="Toggle sidebar">
        <svg width="15" height="15" viewBox="0 0 15 15" fill="none">
          <rect x="1" y="3"  width="13" height="1.5" rx="0.75" fill="currentColor"/>
          <rect x="1" y="7"  width="13" height="1.5" rx="0.75" fill="currentColor"/>
          <rect x="1" y="11" width="13" height="1.5" rx="0.75" fill="currentColor"/>
        </svg>
      </button>
      <span class="topbar-title">Databases</span>
      <span class="topbar-url">127.0.0.1:7734</span>
      <div class="topbar-right">
        <button class="btn" onclick="doRefresh(this)">↻ Refresh</button>
        <button class="btn btn-primary" onclick="openModal()">＋ New</button>
      </div>
    </header>
    <div class="content" id="content"></div>
  </div>
</div>

<div class="overlay" id="overlay" onclick="maybeClose(event)">
  <div class="modal">
    <div class="modal-title">New Database Instance</div>
    <div class="type-grid" id="type-grid"></div>
    <div class="fg">
      <span class="label">Name</span>
      <input id="m-name" placeholder="e.g. Dev Postgres" />
    </div>
    <div class="form-cols">
      <div class="fg"><span class="label">Version</span><select id="m-ver"></select></div>
      <div class="fg"><span class="label">Port</span><input id="m-port" type="number" /></div>
    </div>
    <div class="form-cols" id="m-cred-row">
      <div class="fg" id="m-user-wrap">
        <span class="label">Username</span>
        <input id="m-user" placeholder="e.g. admin" autocomplete="off" />
      </div>
      <div class="fg">
        <span class="label">Password</span>
        <input id="m-pass" type="password" placeholder="••••••••" autocomplete="new-password" />
      </div>
    </div>
    <div class="modal-foot">
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="btn btn-primary" onclick="doCreate()" id="create-btn">Create</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const TYPES = {
  postgres: { label:'PostgreSQL', icon:'🐘', color:'#336791', versions:['16','15','14','13'], port:5432, defaultUser:'postgres', defaultPass:'postgres', conn:(i)=>`postgresql://${i.user||'postgres'}:${i.password||'postgres'}@127.0.0.1:${i.port}/postgres` },
  mysql:    { label:'MySQL',      icon:'🐬', color:'#00758F', versions:['8.3','8.0','5.7'],  port:3306, defaultUser:'admin',    defaultPass:'admin',    conn:(i)=>`mysql://${i.user||'admin'}:${i.password||'admin'}@127.0.0.1:${i.port}` },
  mariadb:  { label:'MariaDB',    icon:'🦭', color:'#C0765A', versions:['11.3','10.11','10.6'],port:3307,defaultUser:'root',     defaultPass:'root',     conn:(i)=>`mysql://${i.user||'root'}:${i.password||'root'}@127.0.0.1:${i.port}` },
  redis:    { label:'Redis',      icon:'⚡', color:'#DC382D', versions:['7.2','7.0','6.2'],  port:6379, defaultUser:null,       defaultPass:null,       conn:(i)=>i.password ? `redis://:${i.password}@127.0.0.1:${i.port}` : `redis://127.0.0.1:${i.port}` },
  mongodb:  { label:'MongoDB',    icon:'🍃', color:'#4CAF50', versions:['7.0','6.0','5.0'],  port:27017,defaultUser:'admin',    defaultPass:'admin',    conn:(i)=>`mongodb://${i.user||'admin'}:${i.password||'admin'}@127.0.0.1:${i.port}` },
};

let instances = {}, openId = null, selType = 'postgres', pendingIds = new Set();
let autoRefreshTimer = null;

// ── API ────────────────────────────────────────────
async function api(path, method='GET', body=null) {
  const opts = { method, headers: {'Content-Type':'application/json'} };
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(path, opts);
  return r.json();
}

// ── Toast ──────────────────────────────────────────
function toast(msg, type='info', dur=3200) {
  const el = document.getElementById('toast');
  el.textContent = msg; el.className = `toast ${type} show`;
  clearTimeout(el._t); el._t = setTimeout(() => el.classList.remove('show'), dur);
}

// ── Sidebar ────────────────────────────────────────
const isMobile = () => window.innerWidth <= 680;

function toggleSidebar() {
  const sb = document.querySelector('.sidebar');
  const bd = document.getElementById('sb-backdrop');
  const open = !sb.classList.contains('collapsed');
  if (isMobile()) {
    if (open) { sb.classList.add('collapsed'); bd.style.display='none'; }
    else       { sb.classList.remove('collapsed'); bd.style.display='block'; }
  } else {
    sb.classList.toggle('collapsed');
  }
}

function closeSidebar() {
  document.querySelector('.sidebar').classList.add('collapsed');
  document.getElementById('sb-backdrop').style.display = 'none';
}

if (isMobile()) document.querySelector('.sidebar').classList.add('collapsed');
window.addEventListener('resize', () => {
  if (!isMobile()) {
    document.querySelector('.sidebar').classList.remove('collapsed');
    document.getElementById('sb-backdrop').style.display = 'none';
  }
});

// ── Render ─────────────────────────────────────────
function hex2rgba(h,a) {
  return `rgba(${parseInt(h.slice(1,3),16)},${parseInt(h.slice(3,5),16)},${parseInt(h.slice(5,7),16)},${a})`;
}

function render() {
  renderNav();
  renderContent();
}

function renderNav() {
  const list = Object.values(instances);
  document.getElementById('nav-list').innerHTML = list.length
    ? list.map(inst => {
        const cfg = TYPES[inst.type] || {};
        return `<div class="nav-item ${openId===inst.id?'active':''}" onclick="selectInst('${inst.id}')">
          <span class="nav-icon">${cfg.icon||'🗄️'}</span>
          <span class="nav-name">${inst.name}</span>
          <span class="nav-dot ${inst.status==='running'?'on':''}"></span>
        </div>`;
      }).join('')
    : '<div style="padding:6px 10px;font-size:12px;color:var(--t3);font-family:var(--mono)">No instances</div>';
}

function renderContent() {
  const el = document.getElementById('content');
  const list = Object.values(instances);
  if (!list.length) {
    el.innerHTML = `<div class="empty">
      <div class="empty-icon">🗄️</div>
      <div class="empty-title">No instances yet</div>
      <div class="empty-sub">Create your first database server to get started.</div>
      <button class="btn btn-primary" onclick="openModal()" style="margin-top:10px">＋ Create Instance</button>
    </div>`; return;
  }
  const running = list.filter(i => i.status==='running');
  const stopped = list.filter(i => i.status==='stopped');
  el.innerHTML =
    (running.length ? `<div class="group"><div class="group-label label">Running · ${running.length}</div>${running.map(renderCard).join('')}</div>` : '') +
    (stopped.length ? `<div class="group"><div class="group-label label">Stopped · ${stopped.length}</div>${stopped.map(renderCard).join('')}</div>` : '');
}

function renderCard(inst) {
  const cfg     = TYPES[inst.type] || { icon:'🗄️', color:'#888', label:inst.type, user:null, pass:null, conn:i=>`127.0.0.1:${i.port}` };
  const isOpen  = openId === inst.id;
  const loading = pendingIds.has(inst.id);
  const running = inst.status === 'running';
  return `
  <div class="card ${isOpen?'open':''}" onclick="selectInst('${inst.id}')">
    <div class="card-row">
      <div class="card-icon" style="background:${hex2rgba(cfg.color,0.1)}">${cfg.icon}</div>
      <div class="card-body">
        <div class="card-name">${inst.name}</div>
        <div class="card-tags">
          <span class="tag">${cfg.label}</span>
          <span class="tag">v${inst.version}</span>
        </div>
      </div>
      <div class="card-port">
        <div class="label port-hint">Port</div>
        <div class="port-val">${inst.port}</div>
      </div>
      <div class="card-status ${inst.status}">
        <span class="status-dot"></span>
        <span class="status-lbl">${inst.status}</span>
      </div>
      <div class="card-actions" onclick="event.stopPropagation()">
        ${running
          ? `<button class="btn btn-stop" onclick="stopDB('${inst.id}')" ${loading?'disabled':''}>${loading?'<span class="spinner"></span>':'⬛'} Stop</button>`
          : `<button class="btn btn-go"   onclick="startDB('${inst.id}')" ${loading?'disabled':''}>${loading?'<span class="spinner"></span>':'▶'} Start</button>`}
        <button class="btn btn-icon" onclick="deleteDB('${inst.id}')" title="Delete">🗑</button>
      </div>
    </div>
    ${isOpen ? renderDrawer(inst, cfg) : ''}
  </div>`;
}

function renderDrawer(inst, cfg) {
  const connStr = cfg.conn(inst);
  const isRedis = inst.type === 'redis';
  const fields = isRedis
    ? [['Host','127.0.0.1'],['Port',inst.port],['Password', inst.password || '—'],['DB','0'],['Created',inst.created]]
    : [['Host','127.0.0.1'],['Port',inst.port],['User',inst.user||'—'],['Password',inst.password||'—'],['Created',inst.created]];
  return `<div class="drawer" onclick="event.stopPropagation()">
    <div class="label" style="margin-bottom:10px;display:block">Connection Details</div>
    <div class="fields">
      ${fields.map(([k,v])=>`<div>
        <div class="label" style="display:block;margin-bottom:4px">${k}</div>
        <div class="field-val">${v}</div>
      </div>`).join('')}
    </div>
    <div class="label" style="margin-bottom:6px;display:block">Connection String</div>
    <div class="conn-row">
      <span class="conn-str" title="${connStr}">${connStr}</span>
      <button class="btn btn-sm" id="copy-${inst.id}" onclick="copyConn('${connStr}','${inst.id}')">Copy</button>
    </div>
  </div>`;
}

// ── Actions ────────────────────────────────────────
function selectInst(id) {
  openId = openId===id ? null : id;
  if (isMobile()) closeSidebar();
  render();
}

async function startDB(id) {
  pendingIds.add(id); render();
  const r = await api(`/api/instances/${id}/start`, 'POST');
  pendingIds.delete(id);
  if (r.success) { instances[id].status='running'; toast(`${instances[id].name} started`, 'success'); }
  else toast(`Error: ${r.error}`, 'error');
  render();
}

async function stopDB(id) {
  pendingIds.add(id); render();
  const r = await api(`/api/instances/${id}/stop`, 'POST');
  pendingIds.delete(id);
  if (r.success) { instances[id].status='stopped'; toast(`${instances[id].name} stopped`, 'info'); }
  else toast(`Error: ${r.error}`, 'error');
  render();
}

async function deleteDB(id) {
  const inst = instances[id];
  if (!confirm(`Delete "${inst.name}"?\nThe container will be removed. Data files are kept.`)) return;
  const r = await api(`/api/instances/${id}`, 'DELETE');
  if (r.success) {
    delete instances[id];
    if (openId===id) openId = null;
    toast(`${inst.name} deleted`, 'info');
    render();
  } else {
    toast(`Error: ${r.error}`, 'error');
  }
}

function copyConn(str, id) {
  navigator.clipboard.writeText(str).catch(()=>{});
  const btn = document.getElementById(`copy-${id}`);
  if (btn) { btn.textContent='✓ Copied'; btn.classList.add('copied'); }
  setTimeout(() => { if (btn) { btn.textContent='Copy'; btn.classList.remove('copied'); } }, 2000);
  toast('Copied to clipboard', 'success');
}

// ── Refresh ────────────────────────────────────────
async function loadInstances() {
  try {
    const data = await api('/api/instances');
    instances = data.instances || {};
    const dot   = document.getElementById('docker-dot');
    const label = document.getElementById('docker-label');
    if (data.docker) { dot.classList.add('ok'); label.textContent = 'Docker running'; }
    else             { dot.classList.remove('ok'); label.textContent = 'Docker not found'; }
    render();
  } catch(e) {
    toast('Could not reach backend', 'error');
  }
}

async function doRefresh(btn) {
  if (btn) { btn.innerHTML='<span class="spinner"></span>'; btn.disabled=true; }
  await loadInstances();
  if (btn) { btn.innerHTML='↻ Refresh'; btn.disabled=false; }
  toast('Refreshed', 'info', 1500);
}

// ── Modal ──────────────────────────────────────────
function buildTypeGrid() {
  document.getElementById('type-grid').innerHTML = Object.entries(TYPES).map(([k,v])=>
    `<div class="type-btn ${selType===k?'sel':''}" onclick="pickType('${k}')"><span class="ti">${v.icon}</span>${v.label}</div>`
  ).join('');
}

function pickType(t) {
  selType = t;
  const cfg = TYPES[t];
  document.getElementById('m-ver').innerHTML = cfg.versions.map(v=>`<option>${v}</option>`).join('');
  document.getElementById('m-port').value = cfg.port;
  document.getElementById('m-user').value = cfg.defaultUser || '';
  document.getElementById('m-pass').value = cfg.defaultPass || '';
  // Redis has no username concept — hide that field
  const userWrap = document.getElementById('m-user-wrap');
  userWrap.style.display = (t === 'redis') ? 'none' : '';
  buildTypeGrid();
}

function openModal() {
  buildTypeGrid(); pickType(selType);
  document.getElementById('m-name').value = '';
  document.getElementById('m-user').value = TYPES[selType].defaultUser || '';
  document.getElementById('m-pass').value = TYPES[selType].defaultPass || '';
  document.getElementById('overlay').classList.add('open');
  setTimeout(() => document.getElementById('m-name').focus(), 180);
}

function closeModal()  { document.getElementById('overlay').classList.remove('open'); }
function maybeClose(e) { if (e.target===document.getElementById('overlay')) closeModal(); }

async function doCreate() {
  const name     = document.getElementById('m-name').value.trim();
  const ver      = document.getElementById('m-ver').value;
  const port     = parseInt(document.getElementById('m-port').value);
  const username = document.getElementById('m-user').value.trim();
  const password = document.getElementById('m-pass').value;
  if (!name) { toast('Please enter a name', 'error'); return; }
  if (selType !== 'redis' && !username) { toast('Please enter a username', 'error'); return; }
  if (!password) { toast('Please enter a password', 'error'); return; }

  const btn = document.getElementById('create-btn');
  btn.innerHTML = '<span class="spinner"></span> Creating…'; btn.disabled = true;

  const r = await api('/api/instances', 'POST', { name, type:selType, version:ver, port, username, password });
  btn.innerHTML = 'Create'; btn.disabled = false;

  if (r.success) {
    closeModal();
    await loadInstances();
    openId = r.id;
    toast(`${name} created`, 'success');
    render();
  } else {
    toast(`Error: ${r.error}`, 'error');
  }
}

// ── Boot ───────────────────────────────────────────
loadInstances();
autoRefreshTimer = setInterval(loadInstances, 15000);
</script>
</body>
</html>
"""

# ── HTTP Request Handler ───────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # suppress access log

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self.send_html(HTML)
        elif path == "/api/instances":
            inst = refresh_statuses()
            self.send_json({"instances": inst, "docker": docker_available()})
        elif path == "/api/db_configs":
            self.send_json(DB_CONFIGS)
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        parts = urllib.parse.urlparse(self.path).path.strip("/").split("/")
        # POST /api/instances
        if parts == ["api", "instances"]:
            b = self.read_body()
            self.send_json(create_instance(b.get("name"), b.get("type"), b.get("version"), b.get("port"), b.get("username"), b.get("password")))
        # POST /api/instances/<id>/start
        elif len(parts) == 4 and parts[0]=="api" and parts[1]=="instances" and parts[3]=="start":
            self.send_json(start_instance(parts[2]))
        # POST /api/instances/<id>/stop
        elif len(parts) == 4 and parts[0]=="api" and parts[1]=="instances" and parts[3]=="stop":
            self.send_json(stop_instance(parts[2]))
        else:
            self.send_json({"error": "Not found"}, 404)

    def do_DELETE(self):
        parts = urllib.parse.urlparse(self.path).path.strip("/").split("/")
        # DELETE /api/instances/<id>
        if len(parts) == 3 and parts[0]=="api" and parts[1]=="instances":
            self.send_json(delete_instance(parts[2]))
        else:
            self.send_json({"error": "Not found"}, 404)

# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    PORT = 7734
    print()
    print("  🗄️   DB-3NGIN3 for Linux")
    print("  ──────────────────────────")
    print(f"  URL  : http://127.0.0.1:{PORT}")
    print(f"  Data : {DATA_DIR}")
    print("  Stop : Ctrl+C")
    print()
    if not docker_available():
        print("  ⚠️  Docker not found. Install it first:")
        print("     curl -fsSL https://get.docker.com | sh")
        print("     sudo usermod -aG docker $USER")
        print()

    def open_browser():
        time.sleep(0.8)
        webbrowser.open(f"http://127.0.0.1:{PORT}")

    threading.Thread(target=open_browser, daemon=True).start()

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")

if __name__ == "__main__":
    main()
