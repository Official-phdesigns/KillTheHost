#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════╗
║        KillTheHost  —  SEC-MNGR v1.0             ║
║        Security Manager & Threat Dashboard       ║
║                                                  ║
║   • Access logging across all KillTheHost apps   ║
║   • IP intelligence, bans, CIDR ranges, allowlist║
║   • Fail2Ban-style detection engine              ║
║   • 24/7 host & service monitoring               ║
║   • UFW / iptables enforcement (HTTP fallback)   ║
║                                                  ║
║   Dashboard: http://127.0.0.1:8080               ║
║   Zero external dependencies — Python 3.8+ only. ║
╚══════════════════════════════════════════════════╝

Data lives in ~/.secmngr/ (0700). All files are written 0600.
Command line:  python3 sec_mngr.py [--host 127.0.0.1] [--port 8080] [--no-browser]
Environment :  SECMNGR_HOST, SECMNGR_PORT, SECMNGR_NO_BROWSER=1
"""

import argparse
import csv
import io
import ipaddress
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections import defaultdict, deque, Counter
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────

VERSION      = "1.0"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
SYSTEM       = platform.system()            # Linux | Darwin | Windows
HOME         = Path.home()
DATA_DIR     = HOME / ".secmngr"

ACCESS_LOG_FILE = DATA_DIR / "access_logs.jsonl"
EVENTS_FILE     = DATA_DIR / "events.jsonl"
BANS_FILE       = DATA_DIR / "bans.json"
RULES_FILE      = DATA_DIR / "rules.json"
SETTINGS_FILE   = DATA_DIR / "settings.json"
HEALTH_FILE     = DATA_DIR / "health.json"
STATE_FILE      = DATA_DIR / "tail_state.json"
IPSTATS_FILE    = DATA_DIR / "ip_stats.json"
BLOCKLIST_FILE  = DATA_DIR / "blocklist.txt"      # consumable by other managers

MAX_BODY        = 1024 * 1024                     # 1 MiB request body cap
MEM_LOG_ENTRIES = 20000                           # in-memory access log ring
MEM_EVENTS      = 5000
MAX_READ_CHUNK  = 4 * 1024 * 1024                 # per file per cycle
MAX_TRACKED_IPS = 50000

# KillTheHost services SEC-MNGR watches. "log_dir" = the manager's data dir.
SERVICES = {
    "launcher":  {"label": "LAUNCHER",  "port": 5000, "log_dir": HOME / ".killthehost"},
    "php_mngr":  {"label": "PHP-MNGR",  "port": 4280, "log_dir": HOME / ".phpmngr"},
    "db_3ngin3": {"label": "DB-3NGIN3", "port": 7734, "log_dir": HOME / ".db3ngin3"},
    "mail_srvr": {"label": "MAIL-SRVR", "port": 6060, "log_dir": HOME / ".mailsrvr"},
    "stax_mngr": {"label": "STAX-MNGR", "port": 6161, "log_dir": HOME / ".staxmngr"},
    "node_mngr": {"label": "NODE-MNGR", "port": 7272, "log_dir": HOME / ".nodemngr"},
    "sec_mngr":  {"label": "SEC-MNGR",  "port": DEFAULT_PORT, "log_dir": None},
}

# Standard suite ports (Launcher, SEC-MNGR, NODE-MNGR, DB-3NGIN3, STAX-MNGR, MAIL-SRVR, PHP-MNGR)
SUITE_DEFAULT_PORTS = frozenset({5000, 8080, 7272, 7734, 6161, 6060, 4280})


def suite_ports():
    """All ports owned by KillTheHost suite tools (read live: SEC-MNGR's port can change at runtime)."""
    return SUITE_DEFAULT_PORTS | {int(m["port"]) for m in SERVICES.values() if m.get("port")}


# Docker container name prefixes → owning KillTheHost service
CONTAINER_SERVICE_PREFIXES = [
    ("phpmngr-",         "php_mngr"),
    ("db3ngin3_",        "db_3ngin3"),
    ("killthehost-mail", "mail_srvr"),
    ("stax-",            "stax_mngr"),
]

# Directories never walked while discovering log files (huge / not logs)
SKIP_DIRS = {"node_modules", ".git", "www", "mail-data", "certs", "dms-config",
             "__pycache__", "vendor", ".cache", ".npm", "dist", "build"}
LOG_FILE_RE = re.compile(r"(\.log|\.jsonl|access[^/]*|error[^/]*\.txt)$", re.I)

HIGH_RISK_PORTS = {23: "telnet", 2375: "Docker API (unencrypted)", 4444: "common backdoor/metasploit",
                   5900: "VNC", 6667: "IRC (botnet C2)", 31337: "classic backdoor", 1080: "SOCKS proxy",
                   3389: "RDP", 111: "rpcbind", 135: "MS-RPC", 445: "SMB"}

DEFAULT_SETTINGS = {
    "bind_host":            DEFAULT_HOST,
    "port":                 DEFAULT_PORT,
    "retention_days":       30,
    "max_log_mb":           256,
    "detection_interval":   30,
    "monitor_interval":     60,
    "detection_mode":       "enforce",      # enforce | monitor (monitor → soft-warn only)
    "escalation_enabled":   True,
    "firewall_enforcement": True,
    "docker_logs":          True,
    "backfill_kb":          256,
    "cpu_warn":             90,
    "mem_warn":             90,
    "disk_warn":            90,
    "disk_crit":            97,
    "known_ports": [
        "22", "53", "80", "443", "25", "110", "143", "465", "587", "993", "995",
        "631", "1025", "8025", "5000", "4280", "7734", "6060", "6161", "7272", "8080",
        "5432", "3306", "3307", "6379", "27017", "3100-3199", "8100-8199",
    ],
    "allowed_hosts":        [],             # extra Host headers accepted by the panel
    "open_browser":         True,
}

DEFAULT_RULES = [
    {"id": "AUTH_FAIL", "name": "Authentication failures",
     "description": "Failed HTTP/app authentication (401/403, 'authentication failed', 'invalid password').",
     "enabled": True, "threshold": 5, "window": 600, "duration": 3600,
     "event_types": ["auth_fail"]},
    {"id": "RATE_FLOOD", "name": "Request flood",
     "description": "Too many HTTP requests from one IP in a short window.",
     "enabled": True, "threshold": 100, "window": 60, "duration": 1800,
     "event_types": ["request", "auth_fail", "not_found", "malformed", "blocked"]},
    {"id": "PATH_PROBE", "name": "Path probing / scanning",
     "description": "Many 404s — typical of vulnerability scanners enumerating paths.",
     "enabled": True, "threshold": 10, "window": 300, "duration": 7200,
     "event_types": ["not_found"]},
    {"id": "MALFORMED", "name": "Malformed requests",
     "description": "Invalid request lines, 400/408/414/431/505, TLS-on-HTTP, SMTP protocol abuse.",
     "enabled": True, "threshold": 20, "window": 600, "duration": 3600,
     "event_types": ["malformed"]},
    {"id": "MAIL_FAIL", "name": "SMTP / IMAP auth failures",
     "description": "Postfix SASL and Dovecot authentication failures from MAIL-SRVR logs.",
     "enabled": True, "threshold": 3, "window": 300, "duration": 14400,
     "event_types": ["smtp_auth_fail"]},
    {"id": "DB_FAIL", "name": "Database connection failures",
     "description": "MySQL/MariaDB access denied, PostgreSQL pg_hba rejects, MongoDB auth failures.",
     "enabled": True, "threshold": 5, "window": 600, "duration": 7200,
     "event_types": ["db_auth_fail"]},
    {"id": "REPEAT_OFFENDER", "name": "Repeat offender",
     "description": "Any IP banned this many times is escalated to a permanent ban (window/duration unused).",
     "enabled": True, "threshold": 3, "window": 0, "duration": 0,
     "event_types": []},
]

SEVERITIES = ("INFO", "WARN", "CRITICAL")
BAN_TYPES  = ("temp", "perm", "soft")

# ─────────────────────────────────────────────────────────────────────────────
#  FILE / TIME HELPERS
# ─────────────────────────────────────────────────────────────────────────────

_io_lock = threading.RLock()


def ensure_data_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass
    for p in DATA_DIR.iterdir():
        if p.is_file():
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass


def _open_secure(path: Path, flags: int):
    fd = os.open(str(path), flags, 0o600)
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass
    return fd


def atomic_write_json(path: Path, data):
    tmp = path.with_name(path.name + ".tmp")
    with _io_lock:
        fd = _open_secure(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(str(tmp), str(path))
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass


def load_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def append_jsonl(path: Path, records):
    if not records:
        return
    with _io_lock:
        fd = _open_secure(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, default=str, separators=(",", ":")) + "\n")


def iter_jsonl(path: Path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue
    except OSError:
        return


def tail_jsonl(path: Path, max_items: int, max_bytes: int = 32 * 1024 * 1024):
    """Return up to max_items records from the end of a JSONL file."""
    out = deque(maxlen=max_items)
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()
            for raw in f:
                try:
                    out.append(json.loads(raw.decode("utf-8", "replace")))
                except ValueError:
                    continue
    except OSError:
        pass
    return list(out)


def now() -> float:
    return time.time()


def iso(ts) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).astimezone().isoformat(timespec="seconds")
    except (ValueError, OSError, OverflowError, TypeError):
        return ""


def run_cmd(argv, timeout=8):
    """Run a command WITHOUT a shell. Returns (rc, stdout, stderr)."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return 127, "", "command not found: %s" % argv[0]
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as exc:  # pragma: no cover - defensive
        return 1, "", str(exc)


def human_duration(secs) -> str:
    try:
        secs = int(secs)
    except (TypeError, ValueError):
        return "?"
    if secs <= 0:
        return "permanent"
    parts = []
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if secs >= n:
            q, secs = divmod(secs, n)
            parts.append("%d%s" % (q, unit))
    return " ".join(parts[:2]) or "0s"


# ─────────────────────────────────────────────────────────────────────────────
#  IP HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def clean_ip(raw):
    """Normalise an IP string (strip brackets/ports/zone, unmap ::ffff:). None if invalid."""
    if not raw:
        return None
    s = str(raw).strip().strip('"\'')
    if s.startswith("[") and "]" in s:
        s = s[1:s.index("]")]
    elif s.count(":") == 1 and "." in s:          # 1.2.3.4:5678
        s = s.split(":")[0]
    s = s.split("%")[0]
    try:
        ip = ipaddress.ip_address(s)
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return str(ip)


def parse_target(raw):
    """Parse an IP or CIDR ban/allow target → ip_network, or raise ValueError."""
    if raw is None:
        raise ValueError("missing ip")
    s = str(raw).strip()
    if not s:
        raise ValueError("missing ip")
    if "/" not in s:
        ip = clean_ip(s)
        if not ip:
            raise ValueError("invalid IP address: %s" % s)
        s = ip
    net = ipaddress.ip_network(s, strict=False)
    if isinstance(net, ipaddress.IPv4Network) and net.prefixlen < 8:
        raise ValueError("IPv4 range too broad (minimum /8)")
    if isinstance(net, ipaddress.IPv6Network) and net.prefixlen < 16:
        raise ValueError("IPv6 range too broad (minimum /16)")
    return net


def net_key(net) -> str:
    """Single hosts are stored as bare IPs, ranges as CIDR."""
    if net.num_addresses == 1:
        return str(net.network_address)
    return str(net)


IP_ANY_RE = re.compile(
    r"(?<![\w.:])((?:\d{1,3}\.){3}\d{1,3}|(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{0,4}|::1)(?![\w.])")


def first_ip_in(text):
    for m in IP_ANY_RE.finditer(text):
        ip = clean_ip(m.group(1))
        if ip:
            return ip
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  TIMESTAMP PARSING
# ─────────────────────────────────────────────────────────────────────────────

_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
ISO_RE    = re.compile(r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(\.\d+)?(Z|[+-]\d\d:?\d\d)?")
SYSLOG_RE = re.compile(r"^([A-Z][a-z]{2})\s+(\d{1,2}) (\d\d):(\d\d):(\d\d)")


def _tz_from(s):
    if not s:
        return None
    if s == "Z":
        return timezone.utc
    sign = 1 if s[0] == "+" else -1
    s = s[1:].replace(":", "")
    return timezone(sign * timedelta(hours=int(s[:2]), minutes=int(s[2:4])))


def parse_iso_ts(text):
    m = ISO_RE.search(text)
    if not m:
        return None
    y, mo, d, h, mi, s, frac, tz = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s))
    except ValueError:
        return None
    tzinfo = _tz_from(tz)
    ts = dt.replace(tzinfo=tzinfo).timestamp() if tzinfo else time.mktime(dt.timetuple())
    if frac:
        ts += float(frac)
    return ts


def parse_clf_ts(s):
    try:
        return datetime.strptime(s, "%d/%b/%Y:%H:%M:%S %z").timestamp()
    except ValueError:
        pass
    try:  # python http.server style: 29/Sep/2026 06:40:01
        return time.mktime(datetime.strptime(s, "%d/%b/%Y %H:%M:%S").timetuple())
    except ValueError:
        return None


def parse_syslog_ts(text):
    m = SYSLOG_RE.match(text)
    if not m:
        return None
    mon, day, h, mi, s = m.groups()
    if mon not in _MONTHS:
        return None
    n = datetime.now()
    try:
        dt = datetime(n.year, _MONTHS[mon], int(day), int(h), int(mi), int(s))
    except ValueError:
        return None
    if dt > n + timedelta(days=1):
        dt = dt.replace(year=n.year - 1)
    return time.mktime(dt.timetuple())


def docker_ts_to_ns(s):
    """RFC3339Nano → integer ns (for exact de-duplication of docker log lines)."""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)", s)
    if not m:
        return None
    base, frac, tz = m.groups()
    try:
        dt = datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=_tz_from(tz))
    except ValueError:
        return None
    return int(dt.timestamp()) * 1_000_000_000 + int(((frac or "") + "000000000")[:9])


# ─────────────────────────────────────────────────────────────────────────────
#  LOG LINE PARSER
# ─────────────────────────────────────────────────────────────────────────────

CLF_RE = re.compile(
    r'(?P<ip>[0-9a-fA-F:.]{3,45}) \S+ \S+ \[(?P<ts>[^\]]+)\] "(?P<req>[^"]*)" '
    r'(?P<status>\d{3}|-) (?P<size>\S+)(?: "(?P<ref>[^"]*)" "(?P<ua>[^"]*)")?')
REQ_LINE_RE  = re.compile(r"^(?P<method>[A-Z]{3,10}) (?P<path>\S+) HTTP/\d(?:\.\d)?$")
PY_ERR_RE    = re.compile(r'(?P<ip>[0-9a-fA-F:.]{3,45}) - - \[(?P<ts>[^\]]+)\] code (?P<status>\d{3}), message (?P<msg>.*)')
SASL_RE      = re.compile(r"\[(?P<ip>[0-9a-fA-F:.]+)\]: SASL \S+ authentication failed", re.I)
DOVECOT_RE   = re.compile(r"(?:auth failed|password mismatch|authentication failure|unknown user|"
                          r"disconnected \(auth failed).*?rip=(?P<ip>[0-9a-fA-F:.]+)", re.I)
SMTP_BAD_RE  = re.compile(r"(?:improper command pipelining|non-SMTP command|too many errors after \S+|"
                          r"SSL_accept error|lost connection after (?:UNKNOWN|DATA|CONNECT)) from [\w.\-]*\[(?P<ip>[0-9a-fA-F:.]+)\]", re.I)
SMTP_REJ_RE  = re.compile(r"NOQUEUE: reject: \S+ from [\w.\-]*\[(?P<ip>[0-9a-fA-F:.]+)\]", re.I)
SMTP_CONN_RE = re.compile(r"postfix/\S+\[\d+\]: connect from [\w.\-]*\[(?P<ip>[0-9a-fA-F:.]+)\]", re.I)
MYSQL_RE     = re.compile(r"Access denied for user '[^']*'@'(?P<ip>[^']+)'", re.I)
MYSQL_ABORT  = re.compile(r"Aborted connection .*?host: '(?P<ip>[^']+)'", re.I)
PG_HBA_RE    = re.compile(r'no pg_hba\.conf entry for host "(?P<ip>[^"]+)"', re.I)
PG_AUTH_RE   = re.compile(r"password authentication failed for user", re.I)
MONGO_RE     = re.compile(r'Authentication failed.*?"remote":"(?P<ip>[^"]+)"|"remote":"(?P<ip2>[^"]+)".*?Authentication failed', re.I)
GENERIC_AUTH = re.compile(r"(failed password|authentication fail(?:ed|ure)|invalid (?:user|password|credentials|login)|"
                          r"login failed|bad password|unauthori[sz]ed access)", re.I)


def _mk(ts, ip, service, event_type, source, method="", path="", status=None, ua="", detail=""):
    ts = min(float(ts or now()), now() + 5)
    return {
        "ts": round(ts, 3), "timestamp": iso(ts), "ip": ip or "unknown",
        "method": method, "path": (path or "")[:512], "status": status,
        "user_agent": (ua or "")[:300], "service": service, "event_type": event_type,
        "source": source, "detail": (detail or "")[:300],
    }


def classify_http(status, req_ok):
    if not req_ok:
        return "malformed"
    if status in (400, 408, 414, 431, 505):
        return "malformed"
    if status in (401, 403):
        return "auth_fail"
    if status == 404:
        return "not_found"
    return "request"


def parse_line(line, service, source, fallback_ts=None):
    """Turn one raw log line into a normalised access-log entry (or None)."""
    line = line.rstrip("\r\n")
    if not line.strip():
        return None
    text = line[:4000]
    # The launcher relays SEC-MNGR's own stdout into launcher.log — never re-ingest our own alerts.
    if service == "launcher" and "[SEC-MNGR]" in text:
        return None

    # JSON structured logs (other tools writing JSONL)
    st = text.lstrip()
    if st.startswith("{") and '"remote"' not in st:
        try:
            obj = json.loads(st)
            ip = clean_ip(obj.get("ip") or obj.get("remote_addr") or obj.get("client_ip") or obj.get("src_ip"))
            if ip:
                status = obj.get("status")
                try:
                    status = int(status) if status is not None else None
                except (TypeError, ValueError):
                    status = None
                et = obj.get("event_type") or classify_http(status, True)
                ts = obj.get("ts")
                if not isinstance(ts, (int, float)):
                    ts = parse_iso_ts(str(obj.get("timestamp") or obj.get("time") or "")) or fallback_ts
                return _mk(ts, ip, service, et, source, obj.get("method", ""), obj.get("path", ""),
                           status, obj.get("user_agent") or obj.get("ua", ""), obj.get("detail", ""))
        except ValueError:
            pass

    line_ts = fallback_ts or parse_iso_ts(text[:40]) or parse_syslog_ts(text) or now()

    m = CLF_RE.search(text)
    if m:
        ip = clean_ip(m.group("ip"))
        if ip:
            req = m.group("req")
            rm = REQ_LINE_RE.match(req)
            status = int(m.group("status")) if m.group("status").isdigit() else None
            ts = parse_clf_ts(m.group("ts")) or line_ts
            et = classify_http(status, bool(rm))
            return _mk(ts, ip, service, et, source,
                       rm.group("method") if rm else "", rm.group("path") if rm else req[:120],
                       status, m.group("ua") or "", "" if rm else "invalid request line")

    m = PY_ERR_RE.search(text)
    if m:
        ip = clean_ip(m.group("ip"))
        if ip:
            status = int(m.group("status"))
            return _mk(parse_clf_ts(m.group("ts")) or line_ts, ip, service,
                       "malformed" if status in (400, 408, 414, 431, 501, 505) else classify_http(status, True),
                       source, status=status, detail=m.group("msg"))

    for rx, et, svc in ((SASL_RE, "smtp_auth_fail", "mail_srvr"), (DOVECOT_RE, "smtp_auth_fail", "mail_srvr"),
                        (SMTP_BAD_RE, "malformed", "mail_srvr"), (SMTP_REJ_RE, "smtp_reject", "mail_srvr"),
                        (SMTP_CONN_RE, "smtp_connect", "mail_srvr")):
        m = rx.search(text)
        if m:
            ip = clean_ip(m.group("ip"))
            if ip:
                return _mk(line_ts, ip, service or svc, et, source, method="SMTP", detail=text[-200:])

    for rx in (MYSQL_RE, MYSQL_ABORT, PG_HBA_RE):
        m = rx.search(text)
        if m:
            return _mk(line_ts, clean_ip(m.group("ip")) or "unknown", service, "db_auth_fail", source,
                       method="DB", detail=text[-200:])
    m = MONGO_RE.search(text)
    if m:
        return _mk(line_ts, clean_ip(m.group("ip") or m.group("ip2")) or "unknown", service,
                   "db_auth_fail", source, method="DB", detail=text[-200:])
    if PG_AUTH_RE.search(text):
        return _mk(line_ts, first_ip_in(text) or "unknown", service, "db_auth_fail", source,
                   method="DB", detail=text[-200:])

    if GENERIC_AUTH.search(text):
        ip = first_ip_in(text)
        if ip:
            et = "db_auth_fail" if service == "db_3ngin3" else (
                "smtp_auth_fail" if service == "mail_srvr" else "auth_fail")
            return _mk(line_ts, ip, service, et, source, detail=text[-200:])
    return None


# ─────────────────────────────────────────────────────────────────────────────
#  EVENT BUS  (security timeline → events.jsonl)
# ─────────────────────────────────────────────────────────────────────────────

class EventBus:
    def __init__(self):
        self.lock = threading.Lock()
        self.events = deque(tail_jsonl(EVENTS_FILE, MEM_EVENTS), maxlen=MEM_EVENTS)
        self._once = {}

    def emit(self, severity, category, title, detail="", **extra):
        severity = severity if severity in SEVERITIES else "INFO"
        ev = {"id": uuid.uuid4().hex[:12], "ts": round(now(), 3), "timestamp": iso(now()),
              "severity": severity, "category": category, "title": title, "detail": detail}
        ev.update({k: v for k, v in extra.items() if v is not None})
        with self.lock:
            self.events.append(ev)
        append_jsonl(EVENTS_FILE, [ev])
        if severity != "INFO":
            print("[%s] [SEC-MNGR] %s %s — %s" % (datetime.now().strftime("%H:%M:%S"),
                                                   severity, title, detail), flush=True)
        return ev

    def emit_once(self, key, ttl, severity, category, title, detail="", **extra):
        """Emit unless the same key was emitted within ttl seconds (alert de-dup)."""
        t = now()
        with self.lock:
            last = self._once.get(key, 0)
            if t - last < ttl:
                return None
            self._once[key] = t
        return self.emit(severity, category, title, detail, **extra)

    def forget(self, key):
        with self.lock:
            self._once.pop(key, None)

    def query(self, hours=24, severity=None, category=None, limit=500):
        cutoff = now() - hours * 3600
        with self.lock:
            items = [e for e in self.events if e.get("ts", 0) >= cutoff]
        if severity:
            sev = set(s.strip().upper() for s in severity.split(","))
            items = [e for e in items if e.get("severity") in sev]
        if category:
            items = [e for e in items if e.get("category") == category]
        items.sort(key=lambda e: e.get("ts", 0), reverse=True)
        return items[:limit]



# ─────────────────────────────────────────────────────────────────────────────
#  SETTINGS & RULES
# ─────────────────────────────────────────────────────────────────────────────

class Config:
    INT_LIMITS = {
        "port": (1, 65535), "retention_days": (1, 3650), "max_log_mb": (1, 102400),
        "detection_interval": (5, 3600), "monitor_interval": (10, 3600), "backfill_kb": (0, 65536),
        "cpu_warn": (1, 100), "mem_warn": (1, 100), "disk_warn": (1, 100), "disk_crit": (1, 100),
    }
    BOOL_KEYS = ("escalation_enabled", "firewall_enforcement", "docker_logs", "open_browser")

    def __init__(self):
        self.lock = threading.Lock()
        s = dict(DEFAULT_SETTINGS)
        s.update({k: v for k, v in load_json(SETTINGS_FILE, {}).items() if k in DEFAULT_SETTINGS})
        self.settings = s
        stored = {r.get("id"): r for r in load_json(RULES_FILE, []) if isinstance(r, dict)}
        rules = []
        for d in DEFAULT_RULES:
            r = dict(d)
            if d["id"] in stored:
                for k in ("enabled", "threshold", "window", "duration"):
                    if k in stored[d["id"]]:
                        r[k] = stored[d["id"]][k]
            rules.append(r)
        self.rules = rules
        self.save()

    def get(self, key):
        with self.lock:
            return self.settings.get(key, DEFAULT_SETTINGS.get(key))

    def save(self):
        with self.lock:
            atomic_write_json(SETTINGS_FILE, self.settings)
            atomic_write_json(RULES_FILE, self.rules)

    def update_settings(self, data):
        if not isinstance(data, dict):
            raise ValueError("expected JSON object")
        changed = {}
        with self.lock:
            for k, v in data.items():
                if k not in DEFAULT_SETTINGS:
                    continue
                if k in self.INT_LIMITS:
                    lo, hi = self.INT_LIMITS[k]
                    v = max(lo, min(hi, int(v)))
                elif k in self.BOOL_KEYS:
                    v = bool(v) if not isinstance(v, str) else v.lower() in ("1", "true", "yes", "on")
                elif k == "detection_mode":
                    if v not in ("enforce", "monitor"):
                        raise ValueError("detection_mode must be enforce|monitor")
                elif k == "bind_host":
                    v = str(v).strip()
                    ipaddress.ip_address(v)          # raises ValueError if invalid
                elif k in ("known_ports", "allowed_hosts"):
                    if isinstance(v, str):
                        v = [x.strip() for x in re.split(r"[,\s]+", v) if x.strip()]
                    v = [str(x)[:64] for x in v][:500]
                    if k == "known_ports":
                        for p in v:
                            if not re.match(r"^(tcp:|udp:)?\d{1,5}(-\d{1,5})?$", p):
                                raise ValueError("invalid port spec: %s" % p)
                if self.settings.get(k) != v:
                    self.settings[k] = v
                    changed[k] = v
        self.save()
        return changed

    def rules_copy(self):
        with self.lock:
            return [dict(r) for r in self.rules]

    def rule(self, rid):
        with self.lock:
            for r in self.rules:
                if r["id"] == rid:
                    return dict(r)
        return None

    def update_rules(self, data):
        """Accepts {"rules":[{id,...}]}, [{id,...}] or {ID: {...}}."""
        if isinstance(data, dict) and "rules" in data:
            data = data["rules"]
        if isinstance(data, dict):
            data = [dict(v, id=k) for k, v in data.items() if isinstance(v, dict)]
        if not isinstance(data, list):
            raise ValueError("expected list of rules")
        updated = []
        with self.lock:
            idx = {r["id"]: r for r in self.rules}
            for upd in data:
                r = idx.get(str(upd.get("id", "")).upper())
                if not r:
                    continue
                if "enabled" in upd:
                    r["enabled"] = bool(upd["enabled"])
                if "threshold" in upd:
                    r["threshold"] = max(1, min(1000000, int(upd["threshold"])))
                if "window" in upd and r["id"] != "REPEAT_OFFENDER":
                    r["window"] = max(1, min(30 * 86400, int(upd["window"])))
                if "duration" in upd and r["id"] != "REPEAT_OFFENDER":
                    r["duration"] = max(0, min(3650 * 86400, int(upd["duration"])))
                updated.append(r["id"])
        self.save()
        return updated

    def known_port(self, port, proto="tcp"):
        # KillTheHost suite ports are always trusted, even if the saved list omits them
        if proto == "tcp" and port in suite_ports():
            return True
        for spec in self.get("known_ports"):
            p = spec
            if ":" in spec:
                pr, p = spec.split(":", 1)
                if pr != proto:
                    continue
            if "-" in p:
                a, b = p.split("-", 1)
                if int(a) <= port <= int(b):
                    return True
            elif p.isdigit() and int(p) == port:
                return True
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  FIREWALL ENFORCEMENT  (UFW → iptables → HTTP-level)
# ─────────────────────────────────────────────────────────────────────────────

class Firewall:
    COMMENT = "secmngr"

    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self._caps = None
        self._caps_ts = 0

    def _priv_prefix(self):
        """[] when root, ['sudo','-n'] when passwordless sudo works, None otherwise."""
        if SYSTEM == "Windows":
            return None
        try:
            if os.geteuid() == 0:
                return []
        except AttributeError:
            return None
        if shutil.which("sudo"):
            rc, _, _ = run_cmd(["sudo", "-n", "true"], timeout=4)
            if rc == 0:
                return ["sudo", "-n"]
        return None

    def capabilities(self, force=False):
        with self.lock:
            if self._caps and not force and now() - self._caps_ts < 300:
                return dict(self._caps)
        caps = {"platform": SYSTEM, "ufw": bool(shutil.which("ufw")) and SYSTEM == "Linux",
                "iptables": bool(shutil.which("iptables")) and SYSTEM == "Linux",
                "ip6tables": bool(shutil.which("ip6tables")) and SYSTEM == "Linux",
                "privilege": None, "ufw_active": None, "docker_user_chain": False,
                "enabled": bool(self.cfg.get("firewall_enforcement"))}
        prefix = self._priv_prefix() if (caps["ufw"] or caps["iptables"]) else None
        caps["privilege"] = None if prefix is None else ("root" if prefix == [] else "sudo")
        if prefix is not None:
            if caps["ufw"]:
                rc, out, _ = run_cmd(prefix + ["ufw", "status"])
                caps["ufw_active"] = rc == 0 and "status: active" in out.lower()
            if caps["iptables"]:
                rc, _, _ = run_cmd(prefix + ["iptables", "-n", "-L", "DOCKER-USER"])
                caps["docker_user_chain"] = rc == 0
        if not caps["enabled"] or prefix is None:
            caps["backend"] = "http"
        elif caps["ufw"] and caps["ufw_active"]:
            caps["backend"] = "ufw"
        elif caps["iptables"]:
            caps["backend"] = "iptables"
        else:
            caps["backend"] = "http"
        caps["_prefix"] = prefix
        with self.lock:
            self._caps, self._caps_ts = caps, now()
        return dict(caps)

    def public_caps(self):
        c = self.capabilities()
        c.pop("_prefix", None)
        return c

    @staticmethod
    def _ipt_tool(net):
        return "ip6tables" if net.version == 6 else "iptables"

    def plan(self, net, backend, docker_chain):
        """Return list of (add_argv, delete_argv, check_argv|None) without privilege prefix."""
        target = str(net)
        rules = []
        if backend == "ufw":
            rules.append((["ufw", "prepend", "deny", "from", target],
                          ["ufw", "delete", "deny", "from", target], None))
        if backend == "iptables":
            t = self._ipt_tool(net)
            spec = ["-s", target, "-m", "comment", "--comment", self.COMMENT, "-j", "DROP"]
            rules.append(([t, "-I", "INPUT", "1"] + spec, [t, "-D", "INPUT"] + spec, [t, "-C", "INPUT"] + spec))
        if docker_chain and backend in ("ufw", "iptables") and net.version == 4:
            # Docker-published ports bypass INPUT/UFW — also drop in DOCKER-USER.
            spec = ["-s", target, "-m", "comment", "--comment", self.COMMENT, "-j", "DROP"]
            rules.append((["iptables", "-I", "DOCKER-USER", "1"] + spec,
                          ["iptables", "-D", "DOCKER-USER"] + spec,
                          ["iptables", "-C", "DOCKER-USER"] + spec))
        return rules

    def block(self, net):
        caps = self.capabilities()
        rec = {"backend": caps["backend"], "applied": False, "rules": [], "manual_commands": [],
               "errors": [], "http_level": True, "ts": now()}
        if not caps["enabled"]:
            rec["note"] = "firewall enforcement disabled in settings — HTTP-level only"
            return rec
        prefix = caps["_prefix"]
        backend = caps["backend"]
        if prefix is None:
            # No privilege: record exact commands for the operator, enforce at HTTP level.
            wanted = "ufw" if caps["ufw"] else ("iptables" if caps["iptables"] else None)
            if wanted:
                for add, _d, _c in self.plan(net, wanted, False):
                    rec["manual_commands"].append("sudo " + " ".join(add))
                rec["note"] = "no root/passwordless sudo — run the commands above manually"
            else:
                rec["note"] = "no supported firewall on %s — HTTP-level enforcement only" % SYSTEM
            return rec
        if backend == "http":
            rec["note"] = "UFW inactive and iptables unavailable — HTTP-level only"
            return rec
        applied_any = False
        for add, delete, check in self.plan(net, backend, caps["docker_user_chain"]):
            if check:
                rc, _, _ = run_cmd(prefix + check)
                if rc == 0:
                    rec["rules"].append({"add": add, "delete": delete, "check": check})
                    applied_any = True
                    continue
            rc, out, err = run_cmd(prefix + add)
            if rc != 0 and add[:2] == ["ufw", "prepend"]:
                add = ["ufw", "insert", "1", "deny", "from", str(net)]
                rc, out, err = run_cmd(prefix + add)
                if rc != 0:
                    add = ["ufw", "deny", "from", str(net)]
                    rc, out, err = run_cmd(prefix + add)
            if rc == 0:
                rec["rules"].append({"add": add, "delete": delete, "check": check})
                applied_any = True
            else:
                rec["errors"].append("%s → %s" % (" ".join(add), (err or out).strip()[:200]))
                rec["manual_commands"].append("sudo " + " ".join(add))
        rec["applied"] = applied_any
        return rec

    def unblock(self, rec):
        if not rec or not rec.get("rules"):
            return []
        prefix = self.capabilities()["_prefix"]
        errors = []
        for r in rec["rules"]:
            if prefix is None:
                errors.append("no privilege — run manually: sudo " + " ".join(r["delete"]))
                continue
            rc, out, err = run_cmd(prefix + r["delete"])
            if rc != 0 and "Could not delete non-existent rule" not in (out + err) \
                    and "does a matching rule exist" not in (out + err):
                errors.append("%s → %s" % (" ".join(r["delete"]), (err or out).strip()[:200]))
        return errors

    def ensure(self, rec):
        """Re-apply iptables rules (non-persistent) after reboot if missing."""
        if not rec or not rec.get("rules"):
            return
        prefix = self.capabilities()["_prefix"]
        if prefix is None:
            return
        for r in rec["rules"]:
            if r.get("check"):
                rc, _, _ = run_cmd(prefix + r["check"])
                if rc != 0:
                    run_cmd(prefix + r["add"])


# ─────────────────────────────────────────────────────────────────────────────
#  BAN MANAGER  (bans.json)
# ─────────────────────────────────────────────────────────────────────────────

class BanManager:
    def __init__(self, cfg, events, firewall):
        self.cfg, self.events, self.fw = cfg, events, firewall
        self.lock = threading.RLock()
        d = load_json(BANS_FILE, {})
        self.bans      = d.get("bans", {}) if isinstance(d.get("bans"), dict) else {}
        self.allowlist = d.get("allowlist", []) if isinstance(d.get("allowlist"), list) else []
        self.offenses  = d.get("offenses", {}) if isinstance(d.get("offenses"), dict) else {}
        self.history   = d.get("history", []) if isinstance(d.get("history"), list) else []
        self.protected = [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"),
                          ipaddress.ip_network("0.0.0.0/32"), ipaddress.ip_network("::/128")]
        self._rebuild()
        self.save()

    # ── persistence ──
    def save(self):
        with self.lock:
            atomic_write_json(BANS_FILE, {"bans": self.bans, "allowlist": self.allowlist,
                                          "offenses": self.offenses, "history": self.history[-1000:]})
            self._write_blocklist()

    def _write_blocklist(self):
        lines = ["# SEC-MNGR blocklist — generated %s" % iso(now()),
                 "# One IP/CIDR per line. Hard bans only (temp+perm). Allowlist overrides."]
        lines += sorted(k for k, b in self.bans.items() if b.get("type") in ("temp", "perm"))
        tmp = BLOCKLIST_FILE.with_name(BLOCKLIST_FILE.name + ".tmp")
        fd = _open_secure(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(str(tmp), str(BLOCKLIST_FILE))

    def _rebuild(self):
        nets = []
        for k, b in self.bans.items():
            try:
                nets.append((ipaddress.ip_network(k, strict=False), k))
            except ValueError:
                continue
        self._ban_nets = nets
        allow = []
        for a in self.allowlist:
            try:
                allow.append(ipaddress.ip_network(a["cidr"], strict=False))
            except (ValueError, KeyError, TypeError):
                continue
        self._allow_nets = allow

    def set_protected(self, nets):
        base = [ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("::1/128"),
                ipaddress.ip_network("0.0.0.0/32"), ipaddress.ip_network("::/128")]
        self.protected = base + list(nets)

    # ── lookups ──
    def _match(self, ip):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return []
        return [(n, k) for n, k in self._ban_nets if n.version == addr.version and addr in n]

    def is_allowlisted(self, ip):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(n.version == addr.version and addr in n for n in self._allow_nets)

    def is_protected(self, ip):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return True
        return any(n.version == addr.version and addr in n for n in self.protected)

    def active_ban(self, ip, hard_only=True):
        """Return the ban record covering ip (None if allowlisted or not banned)."""
        if not ip or ip == "unknown" or self.is_allowlisted(ip):
            return None
        t = now()
        with self.lock:
            for _n, k in self._match(ip):
                b = self.bans.get(k)
                if not b:
                    continue
                if b.get("expires") and b["expires"] <= t:
                    continue
                if hard_only and b.get("type") == "soft":
                    continue
                return b
        return None

    def soft_match(self, ip):
        b = self.active_ban(ip, hard_only=False)
        return b if b and b.get("type") == "soft" else None

    def list(self):
        t = now()
        with self.lock:
            out = []
            for k, b in self.bans.items():
                r = dict(b)
                r["remaining"] = max(0, int(b["expires"] - t)) if b.get("expires") else None
                out.append(r)
        out.sort(key=lambda b: b.get("created", 0), reverse=True)
        return out

    # ── mutations ──
    def add_ban(self, target, ban_type="temp", duration=3600, reason="", source="manual",
                rule=None, escalate=True):
        """Create/update a ban. Returns (ok, ban_or_error)."""
        ban_type = {"temporary": "temp", "permanent": "perm", "soft-warn": "soft", "warn": "soft",
                    "soft_warn": "soft"}.get(str(ban_type).lower(), str(ban_type).lower())
        if ban_type not in BAN_TYPES:
            return False, "type must be temp, perm or soft"
        try:
            net = parse_target(target)
        except ValueError as exc:
            return False, str(exc)
        key = net_key(net)
        for p in self.protected:
            if p.version == net.version and net.overlaps(p):
                return False, "%s overlaps a protected local/loopback/bridge network (%s)" % (key, p)
        overlaps_allow = [str(a) for a in self._allow_nets if a.version == net.version and net.overlaps(a)]
        if overlaps_allow and net.num_addresses == 1:
            return False, "%s is on the allowlist (%s) — remove it from the allowlist first" % (key, overlaps_allow[0])
        try:
            duration = int(duration or 0)
        except (TypeError, ValueError):
            return False, "duration must be an integer number of seconds"
        if ban_type == "temp" and duration <= 0:
            return False, "temporary bans need a positive duration"
        reason = str(reason or "")[:500]
        notes = []

        with self.lock:
            prior = int(self.offenses.get(key, 0))
            offense_no = prior + 1 if ban_type in ("temp", "perm") else prior
            if ban_type == "temp" and escalate and self.cfg.get("escalation_enabled"):
                if offense_no == 2:
                    duration *= 2
                    notes.append("2nd offense — duration doubled")
                elif offense_no >= 3:
                    ban_type = "perm"
                    notes.append("offense #%d — escalated to permanent" % offense_no)
            ro = self.cfg.rule("REPEAT_OFFENDER")
            if ban_type == "temp" and ro and ro["enabled"] and offense_no >= ro["threshold"]:
                ban_type = "perm"
                rule = "REPEAT_OFFENDER"
                notes.append("REPEAT_OFFENDER: banned %d times (threshold %d) — permanent"
                             % (offense_no, ro["threshold"]))

            existing = self.bans.get(key)
            if existing and existing.get("enforcement"):
                if ban_type == "soft" or existing.get("type") == "soft":
                    self.fw.unblock(existing["enforcement"])
                    existing["enforcement"] = None
            t = now()
            ban = {
                "id": key, "target": key, "is_range": net.num_addresses > 1, "version": net.version,
                "type": ban_type, "created": round(t, 3), "created_at": iso(t),
                "expires": round(t + duration, 3) if ban_type == "temp" else None,
                "expires_at": iso(t + duration) if ban_type == "temp" else None,
                "duration": duration if ban_type == "temp" else 0,
                "reason": reason, "source": source, "rule": rule, "offense": offense_no,
                "notes": notes, "hits": existing.get("hits", 0) if existing else 0,
                "enforcement": existing.get("enforcement") if existing else None,
            }
            if ban_type in ("temp", "perm"):
                self.offenses[key] = offense_no
                if not ban["enforcement"] or not ban["enforcement"].get("applied"):
                    if overlaps_allow:
                        ban["enforcement"] = {"backend": "http", "applied": False, "rules": [],
                                              "manual_commands": [], "errors": [], "http_level": True,
                                              "note": "range overlaps allowlist %s — firewall skipped, "
                                                      "HTTP-level enforcement honours allowlist" % ", ".join(overlaps_allow)}
                    else:
                        ban["enforcement"] = self.fw.block(net)
            else:
                ban["enforcement"] = {"backend": "none", "applied": False, "rules": [], "http_level": False,
                                      "manual_commands": [], "errors": [], "note": "soft-warn: logged, not blocked"}
            self.bans[key] = ban
            self.history.append({"ts": t, "action": "ban", "target": key, "type": ban_type,
                                 "duration": ban["duration"], "reason": reason, "rule": rule, "source": source})
            self._rebuild()
            self.save()

        sev = "CRITICAL" if ban_type == "perm" else ("WARN" if ban_type == "temp" else "INFO")
        enf = ban["enforcement"] or {}
        how = enf.get("backend", "http") + (" (applied)" if enf.get("applied") else
                                            " (HTTP-level only)" if ban_type != "soft" else "")
        self.events.emit(sev, "ban", "%s %s ban: %s" % ("Auto" if source == "auto" else "Manual",
                                                        {"temp": "temporary", "perm": "permanent",
                                                         "soft": "soft-warn"}[ban_type], key),
                         "%s%s | duration: %s | enforcement: %s" % (
                             reason, (" | " + "; ".join(notes)) if notes else "",
                             human_duration(ban["duration"]) if ban_type == "temp" else
                             ("permanent" if ban_type == "perm" else "n/a"), how),
                         ip=key, rule=rule)
        return True, ban

    def upgrade_to_perm(self, key, rule, reason):
        with self.lock:
            b = self.bans.get(key)
            if not b or b.get("type") == "perm":
                return False
            b["type"], b["expires"], b["expires_at"], b["duration"] = "perm", None, None, 0
            b["rule"] = rule
            b["notes"] = (b.get("notes") or []) + [reason]
            self.history.append({"ts": now(), "action": "escalate", "target": key, "type": "perm",
                                 "reason": reason, "rule": rule, "source": "auto"})
            self.save()
        self.events.emit("CRITICAL", "ban", "Escalated to permanent: %s" % key, reason, ip=key, rule=rule)
        return True

    def remove_ban(self, target, reason="manual unban", source="manual"):
        try:
            key = net_key(parse_target(target))
        except ValueError:
            key = str(target).strip()
        with self.lock:
            b = self.bans.pop(key, None)
            if not b:
                return False, "no ban for %s" % key
            errs = self.fw.unblock(b.get("enforcement"))
            self.history.append({"ts": now(), "action": "unban", "target": key, "reason": reason,
                                 "source": source})
            self._rebuild()
            self.save()
        self.events.emit("INFO", "ban", "Ban removed: %s" % key,
                         reason + ((" | firewall: " + "; ".join(errs)) if errs else ""), ip=key)
        return True, {"target": key, "firewall_errors": errs}

    def expire(self):
        t = now()
        with self.lock:
            expired = [k for k, b in self.bans.items() if b.get("expires") and b["expires"] <= t]
        for k in expired:
            self.remove_ban(k, "ban expired", source="auto")
        return expired

    def record_hit(self, key):
        with self.lock:
            b = self.bans.get(key)
            if b:
                b["hits"] = b.get("hits", 0) + 1

    def add_allow(self, cidr, note=""):
        try:
            net = parse_target(cidr)
        except ValueError as exc:
            return False, str(exc)
        key = net_key(net)
        with self.lock:
            if any(a.get("cidr") == key for a in self.allowlist):
                return False, "%s already allowlisted" % key
            entry = {"cidr": key, "note": str(note or "")[:300], "added": round(now(), 3), "added_at": iso(now())}
            self.allowlist.append(entry)
            self._rebuild()
            # Lift any ban fully inside the new allowlist entry (allowlist overrides all bans).
            lifted = []
            for k in list(self.bans.keys()):
                try:
                    bn = ipaddress.ip_network(k, strict=False)
                except ValueError:
                    continue
                if bn.version == net.version and bn.subnet_of(net):
                    lifted.append(k)
            self.save()
        for k in lifted:
            self.remove_ban(k, "allowlisted (%s)" % key)
        self.events.emit("INFO", "allowlist", "Allowlisted %s" % key, note, ip=key)
        return True, dict(entry, lifted_bans=lifted)

    def remove_allow(self, cidr):
        try:
            key = net_key(parse_target(cidr))
        except ValueError:
            key = str(cidr).strip()
        with self.lock:
            before = len(self.allowlist)
            self.allowlist = [a for a in self.allowlist if a.get("cidr") != key]
            if len(self.allowlist) == before:
                return False, "%s not on allowlist" % key
            self._rebuild()
            self.save()
        self.events.emit("INFO", "allowlist", "Removed from allowlist: %s" % key, ip=key)
        return True, {"cidr": key}

    def reconcile(self):
        """On startup: expire stale bans and re-apply non-persistent iptables rules."""
        self.expire()
        for b in self.list():
            enf = b.get("enforcement") or {}
            if b.get("type") in ("temp", "perm") and enf.get("applied"):
                self.fw.ensure(enf)


# ─────────────────────────────────────────────────────────────────────────────
#  ACCESS LOG STORE + IP INTELLIGENCE
# ─────────────────────────────────────────────────────────────────────────────

LOG_FIELDS = ["timestamp", "ip", "method", "path", "status", "user_agent", "service", "event_type",
              "source", "detail"]


class AccessLogStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = deque(tail_jsonl(ACCESS_LOG_FILE, MEM_LOG_ENTRIES), maxlen=MEM_LOG_ENTRIES)
        raw = load_json(IPSTATS_FILE, {})
        self.ips = raw if isinstance(raw, dict) else {}
        self._dirty = False
        self._pending = []

    def add(self, entries):
        if not entries:
            return
        with self.lock:
            for e in entries:
                self.entries.append(e)
                self._pending.append(e)
                ip = e.get("ip")
                if not ip or ip == "unknown":
                    continue
                s = self.ips.get(ip)
                if s is None:
                    s = self.ips[ip] = {"ip": ip, "requests": 0, "first_seen": e["ts"], "last_seen": e["ts"],
                                        "services": [], "event_types": {}, "blocked": 0, "bans": 0,
                                        "last_path": "", "last_ua": ""}
                s["requests"] += 1
                s["first_seen"] = min(s["first_seen"], e["ts"])
                if e["ts"] >= s["last_seen"]:
                    s["last_seen"] = e["ts"]
                    s["last_path"] = e.get("path", "")
                    if e.get("user_agent"):
                        s["last_ua"] = e["user_agent"]
                if e["service"] not in s["services"]:
                    s["services"].append(e["service"])
                et = e["event_type"]
                s["event_types"][et] = s["event_types"].get(et, 0) + 1
                if et == "blocked":
                    s["blocked"] += 1
            self._dirty = True

    def note_ban(self, ip):
        with self.lock:
            s = self.ips.get(ip)
            if s:
                s["bans"] = s.get("bans", 0) + 1
                self._dirty = True

    def flush(self):
        with self.lock:
            pending, self._pending = self._pending, []
            dirty, self._dirty = self._dirty, False
            if len(self.ips) > MAX_TRACKED_IPS:
                keep = sorted(self.ips.values(), key=lambda s: s["last_seen"], reverse=True)[:MAX_TRACKED_IPS]
                self.ips = {s["ip"]: s for s in keep}
            snapshot = dict(self.ips) if dirty else None
        append_jsonl(ACCESS_LOG_FILE, pending)
        if snapshot is not None:
            atomic_write_json(IPSTATS_FILE, snapshot)

    @staticmethod
    def _matches(e, f):
        if f.get("service") and e.get("service") != f["service"]:
            return False
        if f.get("ip"):
            want = f["ip"]
            if "/" in want:
                try:
                    if ipaddress.ip_address(e.get("ip")) not in ipaddress.ip_network(want, strict=False):
                        return False
                except ValueError:
                    return False
            elif e.get("ip") != want:
                return False
        if f.get("event_type") and e.get("event_type") not in f["event_type"].split(","):
            return False
        if f.get("status") and str(e.get("status")) != str(f["status"]):
            return False
        if f.get("since") and e.get("ts", 0) < f["since"]:
            return False
        if f.get("until") and e.get("ts", 0) > f["until"]:
            return False
        if f.get("q"):
            q = f["q"].lower()
            hay = " ".join(str(e.get(k, "")) for k in ("path", "user_agent", "detail", "ip", "method")).lower()
            if q not in hay:
                return False
        return True

    def search(self, f, limit=500, deep=False):
        limit = max(1, min(int(limit or 500), 100000))
        out = deque(maxlen=limit)
        if deep:
            for e in iter_jsonl(ACCESS_LOG_FILE):
                if self._matches(e, f):
                    out.append(e)
            with self.lock:
                pend = list(self._pending)
            for e in pend:
                if self._matches(e, f):
                    out.append(e)
        else:
            with self.lock:
                snap = list(self.entries)
            for e in snap:
                if self._matches(e, f):
                    out.append(e)
        res = list(out)
        res.sort(key=lambda e: e.get("ts", 0), reverse=True)
        return res

    def recent(self, seconds):
        cutoff = now() - seconds
        with self.lock:
            return [e for e in self.entries if e.get("ts", 0) >= cutoff]

    def ip_list(self, q="", sort="last_seen", limit=500):
        with self.lock:
            items = [dict(s) for s in self.ips.values()]
        if q:
            items = [s for s in items if q in s["ip"]]
        key = sort if sort in ("requests", "last_seen", "first_seen", "blocked", "bans") else "last_seen"
        items.sort(key=lambda s: s.get(key, 0), reverse=True)
        return items[:limit]

    def ip_stats(self, ip):
        with self.lock:
            s = self.ips.get(ip)
            return dict(s) if s else None

    def rotate(self, retention_days, max_mb):
        """Drop entries older than retention; enforce size cap by dropping the oldest lines."""
        cutoff = now() - retention_days * 86400
        dropped = 0
        for path in (ACCESS_LOG_FILE, EVENTS_FILE):
            if not path.exists():
                continue
            tmp = path.with_name(path.name + ".rot")
            with _io_lock:
                kept = []
                with open(path, "r", encoding="utf-8", errors="replace") as src:
                    for line in src:
                        try:
                            ts = json.loads(line).get("ts", 0)
                        except ValueError:
                            dropped += 1
                            continue
                        if ts >= cutoff:
                            kept.append(line if line.endswith("\n") else line + "\n")
                        else:
                            dropped += 1
                limit = max_mb * 1024 * 1024
                total = sum(len(l) for l in kept)
                i = 0
                while total > limit * 0.9 and i < len(kept):
                    total -= len(kept[i])
                    i += 1
                dropped += i
                fd = _open_secure(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
                with os.fdopen(fd, "w", encoding="utf-8") as dst:
                    dst.writelines(kept[i:])
                os.replace(str(tmp), str(path))
                os.chmod(str(path), 0o600)
        with self.lock:
            for ip in [k for k, s in self.ips.items() if s.get("last_seen", 0) < cutoff]:
                del self.ips[ip]
            self._dirty = True
        return dropped


# ─────────────────────────────────────────────────────────────────────────────
#  LOG COLLECTOR  (tails manager log files + docker container logs)
# ─────────────────────────────────────────────────────────────────────────────

class LogCollector:
    def __init__(self, cfg, events):
        self.cfg, self.events = cfg, events
        st = load_json(STATE_FILE, {})
        self.files = st.get("files", {}) if isinstance(st.get("files"), dict) else {}
        self.docker = st.get("docker", {}) if isinstance(st.get("docker"), dict) else {}
        self.sources = {}          # path/container → status for the UI
        self.lock = threading.Lock()

    def save(self):
        atomic_write_json(STATE_FILE, {"files": self.files, "docker": self.docker})

    def discover_files(self):
        found = []
        for svc, meta in SERVICES.items():
            base = meta.get("log_dir")
            if not base or not Path(base).is_dir():
                continue
            base = Path(base)
            base_depth = len(base.parts)
            for root, dirs, files in os.walk(str(base)):
                depth = len(Path(root).parts) - base_depth
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")] if depth < 4 else []
                for fn in files:
                    if LOG_FILE_RE.search(fn) and not fn.endswith((".tmp", ".gz", ".zip")):
                        found.append((svc, os.path.join(root, fn)))
                if len(found) > 300:
                    break
        return found[:300]

    def _tail_file(self, svc, path):
        out = []
        try:
            st = os.stat(path)
        except OSError as exc:
            self.sources[path] = {"type": "file", "service": svc, "ok": False, "error": str(exc)}
            return out
        rec = self.files.get(path)
        backfill = int(self.cfg.get("backfill_kb")) * 1024
        if rec is None or rec.get("inode") != st.st_ino or st.st_size < rec.get("offset", 0):
            offset = max(0, st.st_size - backfill) if rec is None else 0
            rec = {"inode": st.st_ino, "offset": offset, "skip_partial": offset > 0}
        if st.st_size == rec["offset"]:
            self.files[path] = rec
            self.sources[path] = {"type": "file", "service": svc, "ok": True, "size": st.st_size,
                                  "offset": rec["offset"], "last_read": rec.get("last_read")}
            return out
        try:
            with open(path, "rb") as f:
                f.seek(rec["offset"])
                if rec.pop("skip_partial", False):
                    f.readline()                   # backfill started mid-line: skip the fragment
                chunk = f.read(MAX_READ_CHUNK)
                last_nl = chunk.rfind(b"\n")
                if last_nl == -1 and len(chunk) < MAX_READ_CHUNK:
                    return out                     # wait for a full line
                usable = chunk[:last_nl + 1] if last_nl != -1 else chunk
                rec["offset"] = f.tell() - len(chunk) + len(usable)
            rec["last_read"] = now()
            for raw in usable.decode("utf-8", "replace").splitlines():
                e = parse_line(raw, svc, "file:" + os.path.basename(path))
                if e:
                    out.append(e)
            self.sources[path] = {"type": "file", "service": svc, "ok": True, "size": st.st_size,
                                  "offset": rec["offset"], "last_read": rec["last_read"], "parsed": len(out)}
        except OSError as exc:
            self.sources[path] = {"type": "file", "service": svc, "ok": False, "error": str(exc)}
        self.files[path] = rec
        return out

    def _docker_containers(self):
        rc, out, _ = run_cmd(["docker", "ps", "--format", "{{.Names}}"], timeout=6)
        if rc != 0:
            return None
        res = []
        for name in out.split():
            for prefix, svc in CONTAINER_SERVICE_PREFIXES:
                if name.startswith(prefix):
                    res.append((name, svc))
                    break
        return res

    def _tail_docker(self, name, svc):
        out = []
        last_ns = self.docker.get(name)
        since = str(int(last_ns // 1_000_000_000)) if last_ns else str(int(now() - 600))
        rc, so, se = run_cmd(["docker", "logs", "--timestamps", "--since", since, "--tail", "5000", name],
                             timeout=15)
        if rc != 0:
            self.sources["docker:" + name] = {"type": "docker", "service": svc, "ok": False,
                                              "error": (se or so).strip()[:200]}
            return out
        newest = last_ns or 0
        for raw in (so + "\n" + se).splitlines():
            if not raw.strip():
                continue
            ts_str, _, rest = raw.partition(" ")
            ns = docker_ts_to_ns(ts_str)
            if ns is None:
                continue
            if last_ns and ns <= last_ns:
                continue
            newest = max(newest, ns)
            e = parse_line(rest, svc, "docker:" + name, fallback_ts=ns / 1e9)
            if e:
                out.append(e)
        if newest:
            self.docker[name] = newest
        self.sources["docker:" + name] = {"type": "docker", "service": svc, "ok": True,
                                          "last_read": now(), "parsed": len(out)}
        return out

    def collect(self):
        entries = []
        with self.lock:
            seen = set()
            for svc, path in self.discover_files():
                seen.add(path)
                entries.extend(self._tail_file(svc, path))
            for p in [p for p, s in self.sources.items() if s.get("type") == "file" and p not in seen]:
                self.sources.pop(p, None)
            if self.cfg.get("docker_logs"):
                conts = self._docker_containers()
                if conts is not None:
                    for name, svc in conts:
                        entries.extend(self._tail_docker(name, svc))
            self.save()
        return entries

    def status(self):
        with self.lock:
            return [dict(v, source=k) for k, v in sorted(self.sources.items())]



# ─────────────────────────────────────────────────────────────────────────────
#  DETECTION ENGINE  (fail2ban-style, background thread)
# ─────────────────────────────────────────────────────────────────────────────

class DetectionEngine:
    def __init__(self, cfg, events, bans, logs, collector):
        self.cfg, self.events, self.bans, self.logs, self.collector = cfg, events, bans, logs, collector
        self.recent = defaultdict(list)       # ip → [(ts, event_type, service)]
        self.lock = threading.Lock()
        self.last_run = None
        self.last_ingested = 0
        self.last_duration = 0
        self.total_auto_bans = 0
        self.stop = threading.Event()
        self.wake = threading.Event()

    def ingest(self, entries):
        """Store entries, update IP intelligence and the detection windows."""
        if not entries:
            return
        self.logs.add(entries)
        with self.lock:
            for e in entries:
                ip = e.get("ip")
                if ip and ip != "unknown":
                    self.recent[ip].append((e["ts"], e["event_type"], e["service"]))
        for e in entries:
            ip = e.get("ip")
            if not ip or ip == "unknown" or e["event_type"] == "blocked":
                continue
            soft = self.bans.soft_match(ip)
            if soft:
                self.bans.record_hit(soft["id"])
                self.events.emit_once("soft:" + ip, 300, "WARN", "soft-warn",
                                      "Soft-warn IP active: %s" % ip,
                                      "%s %s %s on %s (watch-listed: %s)" % (
                                          e.get("method") or "", e.get("path") or e.get("event_type"),
                                          e.get("status") or "", e["service"], soft.get("reason") or "no reason"),
                                      ip=ip, service=e["service"])
            else:
                hard = self.bans.active_ban(ip)
                if hard:
                    self.bans.record_hit(hard["id"])

    def detect(self):
        rules = [r for r in self.cfg.rules_copy() if r["enabled"] and r["event_types"]]
        max_window = max([r["window"] for r in self.cfg.rules_copy() if r["window"]] + [3600])
        t = now()
        triggers = []
        with self.lock:
            for ip in list(self.recent.keys()):
                evs = [x for x in self.recent[ip] if x[0] >= t - max_window]
                if not evs:
                    del self.recent[ip]
                    continue
                self.recent[ip] = evs
                for r in rules:
                    cutoff = t - r["window"]
                    types = set(r["event_types"])
                    hits = [x for x in evs if x[0] >= cutoff and x[1] in types]
                    if len(hits) >= r["threshold"]:
                        triggers.append((ip, r, hits))
                        break                      # first (highest-priority) rule wins
        for ip, r, hits in triggers:
            self._act(ip, r, hits)
        # REPEAT_OFFENDER sweep: existing temp bans whose offense count reached the threshold
        ro = self.cfg.rule("REPEAT_OFFENDER")
        if ro and ro["enabled"]:
            for b in self.bans.list():
                if b.get("type") == "temp" and int(b.get("offense", 0)) >= ro["threshold"]:
                    self.bans.upgrade_to_perm(b["id"], "REPEAT_OFFENDER",
                                              "REPEAT_OFFENDER: %s banned %d times (threshold %d)"
                                              % (b["id"], b["offense"], ro["threshold"]))

    def _act(self, ip, rule, hits):
        services = sorted(set(h[2] for h in hits))
        reason = ("[%s] %d %s event(s) in %s (threshold %d / %s) — services: %s" % (
            rule["id"], len(hits), "/".join(rule["event_types"]) if len(rule["event_types"]) < 3 else "http",
            human_duration(rule["window"]), rule["threshold"], human_duration(rule["window"]),
            ", ".join(services)))
        if self.bans.is_protected(ip):
            self.events.emit_once("prot:%s:%s" % (ip, rule["id"]), 3600, "INFO", "detection",
                                  "Rule %s matched protected local IP %s — not banned" % (rule["id"], ip),
                                  reason, ip=ip, rule=rule["id"])
            self._clear(ip)
            return
        if self.bans.is_allowlisted(ip):
            self.events.emit_once("allow:%s:%s" % (ip, rule["id"]), 3600, "INFO", "detection",
                                  "Rule %s matched allowlisted IP %s — not banned" % (rule["id"], ip),
                                  reason, ip=ip, rule=rule["id"])
            self._clear(ip)
            return
        if self.bans.active_ban(ip):
            self._clear(ip)
            return
        existing_soft = self.bans.soft_match(ip)
        mode = self.cfg.get("detection_mode")
        if mode == "monitor":
            if not existing_soft:
                self.bans.add_ban(ip, "soft", 0, "[monitor mode] " + reason, source="auto", rule=rule["id"])
            self.events.emit_once("mon:%s:%s" % (ip, rule["id"]), 600, "WARN", "detection",
                                  "Rule %s triggered for %s (monitor mode — not blocked)" % (rule["id"], ip),
                                  reason, ip=ip, rule=rule["id"])
            self._clear(ip)
            return
        btype = "perm" if rule["duration"] <= 0 else "temp"
        ok, res = self.bans.add_ban(ip, btype, rule["duration"], reason, source="auto", rule=rule["id"])
        if ok:
            self.total_auto_bans += 1
            self.logs.note_ban(ip)
            self.events.emit("CRITICAL" if res["type"] == "perm" else "WARN", "detection",
                             "Rule %s triggered → %s banned (%s)" % (
                                 rule["id"], ip, "permanent" if res["type"] == "perm" else human_duration(res["duration"])),
                             reason, ip=ip, rule=rule["id"])
        else:
            self.events.emit_once("banfail:" + ip, 600, "WARN", "detection",
                                  "Rule %s triggered for %s but ban failed" % (rule["id"], ip), res,
                                  ip=ip, rule=rule["id"])
        self._clear(ip)

    def _clear(self, ip):
        with self.lock:
            self.recent.pop(ip, None)

    def run_once(self):
        t0 = time.monotonic()
        entries = self.collector.collect()
        self.ingest(entries)
        self.detect()
        self.bans.expire()
        self.logs.flush()
        self.last_ingested = len(entries)
        self.last_run = now()
        self.last_duration = round(time.monotonic() - t0, 3)
        return len(entries)

    def loop(self):
        while not self.stop.is_set():
            try:
                self.run_once()
            except Exception as exc:
                self.events.emit_once("det-err", 300, "WARN", "system", "Detection cycle error", repr(exc))
            self.wake.wait(self.cfg.get("detection_interval"))
            self.wake.clear()


# ─────────────────────────────────────────────────────────────────────────────
#  MONITORING ENGINE  (24/7, background thread)
# ─────────────────────────────────────────────────────────────────────────────

def tcp_probe(port, host="127.0.0.1", timeout=0.4):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _split_addr(s):
    s = s.strip()
    if s.startswith("["):
        host, _, port = s[1:].partition("]:")
    else:
        sep = s.rfind(":") if s.count(":") >= 1 and not re.search(r"\.\d+$", s) else s.rfind(".")
        if sep == -1:
            return s, None
        host, port = s[:sep], s[sep + 1:]
    host = host.split("%")[0]
    return host, int(port) if port.isdigit() else None


def list_listening():
    """Return [{proto, addr, port, process}] of listening sockets (ss → netstat)."""
    res = []
    if SYSTEM == "Linux" and shutil.which("ss"):
        rc, out, _ = run_cmd(["ss", "-H", "-tulnp"])
        if rc != 0:
            rc, out, _ = run_cmd(["ss", "-tulnp"])
        if rc == 0:
            for line in out.splitlines():
                parts = line.split()
                if len(parts) < 5 or parts[0] in ("Netid", "State"):
                    continue
                proto = parts[0]
                state = parts[1]
                if proto.startswith("tcp") and state != "LISTEN":
                    continue
                host, port = _split_addr(parts[4])
                if port is None:
                    continue
                pm = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
                res.append({"proto": "tcp" if proto.startswith("tcp") else "udp", "addr": host,
                            "port": port, "process": ("%s[%s]" % pm.groups()) if pm else ""})
            return res
    args = ["netstat", "-ano"] if SYSTEM == "Windows" else (["netstat", "-an"] if SYSTEM == "Darwin"
                                                            else ["netstat", "-tuln"])
    rc, out, _ = run_cmd(args)
    if rc != 0:
        return res
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        p0 = parts[0].lower()
        if not (p0.startswith("tcp") or p0.startswith("udp")):
            continue
        is_tcp = p0.startswith("tcp")
        if is_tcp and not any(x in ("LISTEN", "LISTENING") for x in parts):
            continue
        local = parts[1] if SYSTEM == "Windows" else parts[3]
        host, port = _split_addr(local)
        if port is None:
            continue
        res.append({"proto": "tcp" if is_tcp else "udp", "addr": host.replace("*", "0.0.0.0") or "0.0.0.0",
                    "port": port, "process": parts[-1] if SYSTEM == "Windows" else ""})
    return res


def is_public_bind(addr):
    return addr in ("0.0.0.0", "::", "*", "", "[::]") or not (
        addr.startswith("127.") or addr in ("::1", "localhost"))


class CpuSampler:
    def __init__(self):
        self.prev = None

    def _read(self):
        try:
            with open("/proc/stat") as f:
                vals = [int(x) for x in f.readline().split()[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            return sum(vals), idle
        except (OSError, ValueError, IndexError):
            return None

    def percent(self):
        cur = self._read()
        if cur is None:
            try:
                la = os.getloadavg()[0]
                return round(min(100.0, la / (os.cpu_count() or 1) * 100), 1)
            except (AttributeError, OSError):
                return None
        if self.prev is None:
            self.prev = cur
            time.sleep(0.25)
            cur = self._read()
        dt, di = cur[0] - self.prev[0], cur[1] - self.prev[1]
        self.prev = cur
        return round(100.0 * (1 - di / dt), 1) if dt > 0 else 0.0


def memory_usage():
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                info[k] = int(v.split()[0]) * 1024
        total = info["MemTotal"]
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        return {"total": total, "used": total - avail, "percent": round(100.0 * (total - avail) / total, 1)}
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        pass
    if SYSTEM == "Windows":
        try:
            import ctypes

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("a", ctypes.c_ulonglong), ("b", ctypes.c_ulonglong), ("c", ctypes.c_ulonglong),
                            ("d", ctypes.c_ulonglong), ("e", ctypes.c_ulonglong)]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return {"total": m.ullTotalPhys, "used": m.ullTotalPhys - m.ullAvailPhys,
                    "percent": float(m.dwMemoryLoad)}
        except Exception:
            pass
    return None


def local_networks():
    """Host interface addresses (as /32 or /128) + docker/bridge subnets — never banned."""
    nets = []
    if SYSTEM == "Linux" and shutil.which("ip"):
        rc, out, _ = run_cmd(["ip", "-o", "addr", "show"])
        if rc == 0:
            for line in out.splitlines():
                m = re.search(r"^\d+:\s+(\S+)\s+inet6?\s+(\S+)", line)
                if not m:
                    continue
                ifname, cidr = m.group(1), m.group(2)
                try:
                    iface = ipaddress.ip_interface(cidr)
                except ValueError:
                    continue
                nets.append(ipaddress.ip_network("%s/%d" % (iface.ip, iface.ip.max_prefixlen)))
                if re.match(r"^(docker|br-|veth|cni|podman|virbr)", ifname):
                    nets.append(iface.network)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ip = clean_ip(info[4][0])
            if ip:
                a = ipaddress.ip_address(ip)
                nets.append(ipaddress.ip_network("%s/%d" % (a, a.max_prefixlen)))
    except OSError:
        pass
    uniq = []
    for n in nets:
        if n not in uniq:
            uniq.append(n)
    return uniq


class Monitor:
    SECRET_FILES = [
        (".phpmngr", "cloudflare.json"), (".phpmngr", "namecheap.json"), (".phpmngr", "godaddy.json"),
        (".phpmngr", "porkbun.json"), (".phpmngr", "ionos.json"), (".phpmngr", "cf_registrar.json"),
        (".db3ngin3", "instances.json"), (".mailsrvr", "config.json"), (".mailsrvr", "accounts.json"),
        (".staxmngr", "stacks.json"), (".nodemngr", "apps.json"),
    ]

    def __init__(self, cfg, events, bans, firewall, logs, bind_info):
        self.cfg, self.events, self.bans, self.fw, self.logs = cfg, events, bans, firewall, logs
        self.bind_info = bind_info
        self.cpu = CpuSampler()
        self.health = load_json(HEALTH_FILE, {})
        self.prev_service_state = {k: v.get("status") for k, v in (self.health.get("services") or {}).items()}
        self.prev_levels = {}
        self.alerted_ports = set()
        self.last_rotation = 0
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.lock = threading.Lock()

    # ── checks ──
    def check_services(self, listening):
        out = {}
        for key, meta in SERVICES.items():
            port = self.bind_info["port"] if key == "sec_mngr" else meta["port"]
            binds = sorted(set(l["addr"] for l in listening if l["port"] == port and l["proto"] == "tcp"))
            up = tcp_probe(port) or (key == "sec_mngr")
            exposed = any(is_public_bind(a) for a in binds)
            status = "DOWN" if not up else ("WARN" if exposed else "UP")
            out[key] = {"label": meta["label"], "port": port, "status": status, "up": up,
                        "bind": binds, "exposed": exposed,
                        "detail": ("listening on all interfaces (%s) — reachable from the network"
                                   % ", ".join(binds)) if exposed else ("loopback only" if up else "not running")}
        return out

    def check_docker(self):
        if not shutil.which("docker"):
            return {"installed": False, "running": False, "detail": "docker CLI not found"}
        rc, out, err = run_cmd(["docker", "info", "--format", "{{.ServerVersion}}|{{.ContainersRunning}}"])
        if rc != 0:
            return {"installed": True, "running": False, "detail": (err or out).strip()[:200]}
        ver, _, running = out.strip().partition("|")
        return {"installed": True, "running": True, "version": ver, "containers_running": running,
                "detail": "Docker %s — %s container(s) running" % (ver, running)}

    def check_ufw(self):
        caps = self.fw.capabilities(force=True)
        res = {"installed": caps["ufw"], "active": caps["ufw_active"], "rules": [],
               "privilege": caps["privilege"], "backend": caps["backend"],
               "docker_user_chain": caps["docker_user_chain"]}
        if not caps["ufw"]:
            res["detail"] = "UFW not installed" if SYSTEM == "Linux" else "UFW not available on %s" % SYSTEM
            return res
        prefix = caps["_prefix"]
        if prefix is None:
            rc, out, _ = run_cmd(["systemctl", "is-active", "ufw"]) if shutil.which("systemctl") else (1, "", "")
            svc = out.strip() if rc in (0, 3) else "unknown"
            res["detail"] = "rules need root — run: sudo ufw status numbered (service: %s)" % svc
            res["active"] = True if svc == "active" else res["active"]
            return res
        rc, out, _ = run_cmd(prefix + ["ufw", "status", "numbered"])
        if rc == 0:
            res["rules"] = [l.strip() for l in out.splitlines() if l.strip().startswith("[")][:300]
            res["detail"] = out.splitlines()[0].strip() if out.strip() else ""
        return res

    def check_ports(self, listening, services):
        svc_ports = {m["port"] for m in services.values()}
        ports, suspicious = [], []
        for l in sorted(listening, key=lambda x: (x["proto"], x["port"])):
            known = self.cfg.known_port(l["port"], l["proto"]) or l["port"] in svc_ports
            public = is_public_bind(l["addr"])
            risk = HIGH_RISK_PORTS.get(l["port"])
            flag = None
            if l["proto"] == "tcp" and (not known or risk):
                flag = "CRITICAL" if (risk and public) else ("WARN" if public else "INFO")
            item = dict(l, known=known, public=public, risk=risk, flag=flag)
            ports.append(item)
            if flag:
                suspicious.append(item)
        # alert only on *new* suspicious ports
        current = set()
        for s in suspicious:
            key = "%s:%s:%s" % (s["proto"], s["addr"], s["port"])
            current.add(key)
            if key not in self.alerted_ports and s["flag"] != "INFO":
                self.events.emit(s["flag"], "ports", "Suspicious listening port %s/%s on %s" % (
                    s["port"], s["proto"], s["addr"]),
                    "%s%s — not in known-good list%s" % (
                        s.get("process") or "unknown process", (" | " + s["risk"]) if s["risk"] else "",
                        " | reachable from network" if s["public"] else ""), port=s["port"])
        self.alerted_ports = current
        return ports, suspicious

    def check_secret_files(self):
        findings = []
        if SYSTEM == "Windows":
            return findings
        for d, fn in self.SECRET_FILES:
            p = HOME / d / fn
            try:
                mode = p.stat().st_mode & 0o777
            except OSError:
                continue
            if mode & 0o044:
                findings.append({"file": str(p), "mode": oct(mode),
                                 "fix": "chmod 600 %s" % p})
        return findings

    def _level_event(self, key, level, sev_map, title, detail):
        prev = self.prev_levels.get(key, "ok")
        if level != prev:
            self.prev_levels[key] = level
            if level == "ok":
                self.events.emit("INFO", "health", title + " back to normal", detail)
            else:
                self.events.emit(sev_map[level], "health", title, detail)

    def run_once(self):
        t0 = time.monotonic()
        try:
            self.bans.set_protected(local_networks())
        except Exception:
            pass
        listening = list_listening()
        services = self.check_services(listening)
        for key, s in services.items():
            prev = self.prev_service_state.get(key)
            if prev and prev != s["status"]:
                if s["status"] == "DOWN" and key in SERVICES:
                    # Suite tools are optional — being stopped is not a security issue
                    self.events.emit("INFO", "service", "%s stopped" % s["label"],
                                     "%s → %s (%s)" % (prev, s["status"], s["detail"]), service=key)
                else:
                    sev = "WARN" if s["status"] in ("DOWN", "WARN") else "INFO"
                    self.events.emit(sev, "service", "%s is %s" % (s["label"], s["status"]),
                                     "%s → %s (%s)" % (prev, s["status"], s["detail"]), service=key)
            elif prev is None and s["status"] == "WARN":
                self.events.emit("WARN", "service", "%s exposed on all interfaces" % s["label"], s["detail"],
                                 service=key)
            self.prev_service_state[key] = s["status"]

        cpu = self.cpu.percent()
        mem = memory_usage()
        try:
            du = shutil.disk_usage(str(HOME))
            disk = {"total": du.total, "used": du.used, "free": du.free,
                    "percent": round(100.0 * du.used / du.total, 1) if du.total else 0, "path": str(HOME)}
        except OSError:
            disk = None
        if cpu is not None:
            self._level_event("cpu", "warn" if cpu >= self.cfg.get("cpu_warn") else "ok",
                              {"warn": "WARN"}, "High CPU usage", "%.1f%%" % cpu)
        if mem:
            self._level_event("mem", "warn" if mem["percent"] >= self.cfg.get("mem_warn") else "ok",
                              {"warn": "WARN"}, "High memory usage", "%.1f%%" % mem["percent"])
        if disk:
            lvl = "crit" if disk["percent"] >= self.cfg.get("disk_crit") else (
                "warn" if disk["percent"] >= self.cfg.get("disk_warn") else "ok")
            self._level_event("disk", lvl, {"warn": "WARN", "crit": "CRITICAL"}, "Low disk space",
                              "%.1f%% used on %s" % (disk["percent"], disk["path"]))
        docker = self.check_docker()
        self._level_event("docker", "ok" if docker["running"] or not docker["installed"] else "warn",
                          {"warn": "WARN"}, "Docker daemon unreachable", docker.get("detail", ""))
        ufw = self.check_ufw()
        if ufw["installed"]:
            self._level_event("ufw", "warn" if ufw["active"] is False else "ok", {"warn": "WARN"},
                              "UFW firewall inactive", ufw.get("detail", "") or "sudo ufw enable")
        ports, suspicious = self.check_ports(listening, services)
        secrets = self.check_secret_files()
        for f in secrets:
            self.events.emit_once("perm:" + f["file"], 86400, "WARN", "posture",
                                  "Secret file readable by other users", "%s (%s) — fix: %s" % (
                                      f["file"], f["mode"], f["fix"]))

        health = {
            "ts": now(), "checked_at": iso(now()), "duration": round(time.monotonic() - t0, 3),
            "services": services, "cpu": cpu, "memory": mem, "disk": disk, "docker": docker,
            "ufw": ufw, "ports": ports, "suspicious_ports": suspicious, "secret_files": secrets,
            "load": list(os.getloadavg()) if hasattr(os, "getloadavg") else None,
            "hostname": socket.gethostname(), "platform": "%s %s" % (SYSTEM, platform.release()),
            "protected_networks": [str(n) for n in self.bans.protected][:50],
        }
        with self.lock:
            self.health = health
        atomic_write_json(HEALTH_FILE, health)

        if now() - self.last_rotation > 6 * 3600:
            self.last_rotation = now()
            dropped = self.logs.rotate(self.cfg.get("retention_days"), self.cfg.get("max_log_mb"))
            if dropped:
                self.events.emit("INFO", "system", "Log rotation", "dropped %d entries older than %d days / "
                                 "over size cap" % (dropped, self.cfg.get("retention_days")))
        return health

    def snapshot(self):
        with self.lock:
            return dict(self.health)

    def loop(self):
        while not self.stop.is_set():
            try:
                self.run_once()
            except Exception as exc:
                self.events.emit_once("mon-err", 300, "WARN", "system", "Monitoring cycle error", repr(exc))
            self.wake.wait(self.cfg.get("monitor_interval"))
            self.wake.clear()


# ─────────────────────────────────────────────────────────────────────────────
#  SECURITY CORE  (wires engines together, aggregates status/stats)
# ─────────────────────────────────────────────────────────────────────────────

class SecurityCore:
    def __init__(self, host, port):
        ensure_data_dir()
        self.started = now()
        self.cfg = Config()
        self.bind_info = {"host": host, "port": port}
        SERVICES["sec_mngr"]["port"] = port
        self.events = EventBus()
        self.fw = Firewall(self.cfg)
        self.bans = BanManager(self.cfg, self.events, self.fw)
        self.logs = AccessLogStore()
        self.collector = LogCollector(self.cfg, self.events)
        self.detector = DetectionEngine(self.cfg, self.events, self.bans, self.logs, self.collector)
        self.monitor = Monitor(self.cfg, self.events, self.bans, self.fw, self.logs, self.bind_info)

    def start(self):
        self.events.emit("INFO", "system", "SEC-MNGR v%s started" % VERSION,
                         "listening on %s:%d | data: %s" % (self.bind_info["host"], self.bind_info["port"], DATA_DIR))
        if not ipaddress.ip_address(self.bind_info["host"]).is_loopback:
            self.events.emit("WARN", "posture", "SEC-MNGR bound to a non-loopback address",
                             "%s — the dashboard is reachable from the network" % self.bind_info["host"])

        def _boot():
            try:
                self.bans.set_protected(local_networks())
                self.bans.reconcile()
            except Exception as exc:
                self.events.emit("WARN", "system", "Ban reconcile failed", repr(exc))
        threading.Thread(target=_boot, daemon=True).start()
        threading.Thread(target=self.monitor.loop, daemon=True, name="monitor").start()
        threading.Thread(target=self.detector.loop, daemon=True, name="detector").start()

    def shutdown(self):
        self.detector.stop.set()
        self.monitor.stop.set()
        self.detector.wake.set()
        self.monitor.wake.set()
        try:
            self.logs.flush()
            self.collector.save()
        except Exception:
            pass

    # ── HTTP-level record/enforcement for requests to SEC-MNGR itself ──
    def record_own(self, ip, method, path, status, ua, event_type=None, detail=""):
        e = _mk(now(), clean_ip(ip) or ip, "sec_mngr", event_type or classify_http(status, True),
                "http:sec_mngr", method, path, status, ua, detail)
        self.detector.ingest([e])

    # ── aggregates ──
    def posture(self, health):
        findings = []

        def add(sev, title, detail, weight):
            findings.append({"severity": sev, "title": title, "detail": detail, "weight": weight})

        for k, s in (health.get("services") or {}).items():
            if s.get("exposed"):
                add("CRITICAL" if k in ("node_mngr", "stax_mngr", "php_mngr") else "WARN",
                    "%s panel exposed to the network" % s["label"],
                    "bound to %s on :%s with no authentication" % (", ".join(s["bind"]), s["port"]), 15)
        ufw = health.get("ufw") or {}
        if ufw.get("installed") and ufw.get("active") is False:
            add("WARN", "UFW firewall inactive", "enable with: sudo ufw enable", 15)
        elif SYSTEM == "Linux" and not ufw.get("installed"):
            add("WARN", "No UFW firewall installed", "sudo apt install ufw && sudo ufw enable", 10)
        for p in health.get("suspicious_ports") or []:
            if p["flag"] == "CRITICAL":
                add("CRITICAL", "High-risk port %s open to the network" % p["port"], p.get("risk") or "", 15)
            elif p["flag"] == "WARN":
                add("WARN", "Unknown public listener on port %s" % p["port"], p.get("process") or "", 5)
        for f in health.get("secret_files") or []:
            add("WARN", "Secret file readable by others", "%s (%s)" % (f["file"], f["mode"]), 5)
        caps = self.fw.public_caps()
        if caps["backend"] == "http":
            add("INFO", "Bans enforced at HTTP level only",
                "no root/passwordless sudo or no firewall — pending commands shown in IP Manager", 5)
        if not (health.get("docker") or {}).get("running", True) and (health.get("docker") or {}).get("installed"):
            add("WARN", "Docker daemon unreachable", (health.get("docker") or {}).get("detail", ""), 3)
        if not ipaddress.ip_address(self.bind_info["host"]).is_loopback:
            add("WARN", "SEC-MNGR bound to %s" % self.bind_info["host"], "prefer 127.0.0.1", 10)
        score = max(0, 100 - sum(f["weight"] for f in findings))
        return score, findings

    def status(self):
        health = self.monitor.snapshot()
        score, findings = self.posture(health)
        bans = self.bans.list()
        hard = [b for b in bans if b["type"] in ("temp", "perm")]
        soft = [b for b in bans if b["type"] == "soft"]
        ev24 = self.events.query(24, limit=MEM_EVENTS)
        sev = Counter(e["severity"] for e in ev24)
        bans24 = [e for e in ev24 if e.get("category") in ("ban", "detection") and e["severity"] != "INFO"]
        svc = health.get("services") or {}
        if sev["CRITICAL"] >= 5 or score < 40:
            level = "CRITICAL"
        elif sev["CRITICAL"] > 0 or len(bans24) >= 10 or score < 60:
            level = "HIGH"
        elif bans24 or sev["WARN"] > 5 or score < 85:
            level = "ELEVATED"
        else:
            level = "LOW"
        return {
            "version": VERSION, "time": iso(now()), "uptime": int(now() - self.started),
            "threat_level": level, "posture_score": score, "findings": findings,
            "active_bans": len(hard), "perm_bans": sum(1 for b in hard if b["type"] == "perm"),
            "temp_bans": sum(1 for b in hard if b["type"] == "temp"), "soft_warns": len(soft),
            "allowlist": len(self.bans.allowlist),
            "alerts_24h": {s: sev.get(s, 0) for s in SEVERITIES},
            "recent_alerts": [e for e in ev24 if e["severity"] != "INFO"][:10],
            "services": {k: {"label": v["label"], "status": v["status"], "port": v["port"]} for k, v in svc.items()},
            "services_up": sum(1 for v in svc.values() if v["up"]),
            "services_total": len(svc),
            "firewall": self.fw.public_caps(),
            "bind": self.bind_info, "data_dir": str(DATA_DIR),
            "detection": {"last_run": iso(self.detector.last_run) if self.detector.last_run else None,
                          "last_ingested": self.detector.last_ingested,
                          "cycle_seconds": self.detector.last_duration,
                          "mode": self.cfg.get("detection_mode"),
                          "interval": self.cfg.get("detection_interval"),
                          "tracked_ips": len(self.detector.recent)},
            "monitoring": {"last_run": health.get("checked_at"), "interval": self.cfg.get("monitor_interval")},
        }

    def stats(self, hours=24):
        hours = max(1, min(int(hours), 24 * 30))
        entries = self.logs.recent(hours * 3600)
        t = now()
        buckets = [0] * hours
        bad = [0] * hours
        for e in entries:
            i = int((t - e["ts"]) // 3600)
            if 0 <= i < hours:
                buckets[hours - 1 - i] += 1
                if e["event_type"] not in ("request", "smtp_connect"):
                    bad[hours - 1 - i] += 1
        ips = Counter(e["ip"] for e in entries if e["ip"] != "unknown")
        offenders = Counter(e["ip"] for e in entries
                            if e["ip"] != "unknown" and e["event_type"] not in ("request", "smtp_connect"))
        bans_hist = [h for h in self.bans.history if h.get("ts", 0) >= t - hours * 3600]
        return {
            "hours": hours, "total_entries": len(entries), "unique_ips": len(ips),
            "by_service": dict(Counter(e["service"] for e in entries)),
            "by_event_type": dict(Counter(e["event_type"] for e in entries)),
            "by_status": dict(Counter(str(e["status"]) for e in entries if e.get("status"))),
            "top_ips": [{"ip": ip, "count": c} for ip, c in ips.most_common(10)],
            "top_offenders": [{"ip": ip, "count": c, "banned": bool(self.bans.active_ban(ip))}
                              for ip, c in offenders.most_common(10)],
            "top_paths": [{"path": p, "count": c} for p, c in Counter(
                e["path"] for e in entries if e.get("path")).most_common(10)],
            "top_user_agents": [{"ua": u, "count": c} for u, c in Counter(
                e["user_agent"] for e in entries if e.get("user_agent")).most_common(5)],
            "hourly": buckets, "hourly_bad": bad,
            "bans_by_rule": dict(Counter(h.get("rule") or "manual" for h in bans_hist if h["action"] == "ban")),
            "bans_total": sum(1 for h in bans_hist if h["action"] == "ban"),
            "events_by_severity": dict(Counter(e["severity"] for e in self.events.query(hours, limit=MEM_EVENTS))),
            "log_file_bytes": ACCESS_LOG_FILE.stat().st_size if ACCESS_LOG_FILE.exists() else 0,
            "tracked_ips_total": len(self.logs.ips),
            "auto_bans_since_start": self.detector.total_auto_bans,
        }


# ─────────────────────────────────────────────────────────────────────────────
#  HTTP HANDLER
# ─────────────────────────────────────────────────────────────────────────────

CORE = None          # set in main()

SEC_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                               "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
}


def _parse_date(s, end=False):
    if not s:
        return None
    s = s.strip()
    try:
        return float(s)
    except ValueError:
        pass
    ts = parse_iso_ts(s if "T" in s or " " in s else s + ("T23:59:59" if end else "T00:00:00"))
    return ts


class Handler(BaseHTTPRequestHandler):
    server_version = "SEC-MNGR/" + VERSION
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    def log_error(self, fmt, *args):
        # http.server calls this for malformed requests (bad request line, etc.)
        try:
            msg = fmt % args
        except Exception:
            msg = str(fmt)
        if CORE and ("Bad request" in msg or "Unsupported method" in msg or "Request-URI" in msg
                     or "Line too long" in msg or "Bad HTTP" in msg):
            CORE.record_own(self.client_address[0], "", getattr(self, "raw_requestline", b"")[:80].decode(
                "latin-1", "replace").strip(), 400, "", "malformed", msg[:200])

    # ── helpers ──
    @property
    def ip(self):
        return clean_ip(self.client_address[0]) or self.client_address[0]

    def _send(self, code, ctype, body, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in SEC_HEADERS.items():
            self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self._status = code

    def _json(self, data, code=200):
        self._send(code, "application/json; charset=utf-8", json.dumps(data, default=str).encode("utf-8"))

    def _err(self, code, msg):
        self._json({"ok": False, "error": msg}, code)

    def _allowed_hosts(self):
        hosts = {"127.0.0.1", "localhost", "::1", "[::1]"}
        bh = CORE.bind_info["host"]
        hosts.add(bh)
        if bh in ("0.0.0.0", "::"):
            for n in CORE.bans.protected:
                if n.num_addresses == 1:
                    hosts.add(str(n.network_address))
            hosts.add(socket.gethostname().lower())
        hosts.update(h.lower() for h in CORE.cfg.get("allowed_hosts") or [])
        return hosts

    def _host_ok(self):
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        if host.startswith("["):
            h = host[1:host.find("]")] if "]" in host else host
        else:
            h = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        return h in self._allowed_hosts()

    def _origin_ok(self):
        origin = self.headers.get("Origin")
        if origin and origin != "null":
            o = urlparse(origin)
            if (o.hostname or "").lower() not in self._allowed_hosts():
                return False
        if (self.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
            return False
        return True

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ValueError("bad Content-Length")
        if n > MAX_BODY:
            raise ValueError("request body too large")
        raw = self.rfile.read(n) if n > 0 else b""
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _gate(self, mutating):
        """Ban enforcement + DNS-rebinding + CSRF protection. Returns True if request may proceed."""
        ban = CORE.bans.active_ban(self.ip)
        if ban:
            CORE.bans.record_hit(ban["id"])
            CORE.record_own(self.ip, self.command, self.path, 403, self.headers.get("User-Agent", ""),
                            "blocked", "HTTP-level ban (%s)" % ban["id"])
            self._send(403, "text/plain", b"Forbidden - blocked by SEC-MNGR\n")
            return False
        if not self._host_ok():
            self._send(421, "text/plain", b"Misdirected request - Host header not allowed\n")
            CORE.record_own(self.ip, self.command, self.path, 421, self.headers.get("User-Agent", ""),
                            "malformed", "rejected Host header: %s" % (self.headers.get("Host") or "")[:100])
            return False
        if mutating:
            if not self._origin_ok():
                self._err(403, "cross-origin request rejected")
                return False
            if self.headers.get("X-SecMngr-Request") != "1":
                self._err(403, "missing X-SecMngr-Request: 1 header (CSRF protection)")
                return False
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if self.command == "POST" and ctype != "application/json":
                self._err(415, "Content-Type must be application/json")
                return False
        return True

    def _finish_log(self, path):
        code = getattr(self, "_status", 0)
        # don't flood the log with the dashboard's own polling from loopback
        if self.command == "GET" and path.startswith("/api/") and CORE.bans.is_protected(self.ip) and code < 400:
            return
        CORE.record_own(self.ip, self.command, self.path[:512], code, self.headers.get("User-Agent", ""))

    # ── verbs ──
    def do_HEAD(self):
        self.do_GET()

    def do_OPTIONS(self):
        # No CORS: pre-flights are always refused, so browsers can't send our custom header cross-origin.
        self._send(403, "text/plain", b"CORS not allowed\n")

    def do_PUT(self):
        self._send(405, "text/plain", b"Method not allowed\n")

    do_PATCH = do_PUT

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path.rstrip("/") or "/"
        if not self._gate(False):
            return
        try:
            self._route_get(path, {k: v[-1] for k, v in parse_qs(u.query).items()})
        except Exception as exc:
            self._err(500, "internal error: %s" % exc)
        self._finish_log(path)

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        if not self._gate(True):
            return
        try:
            body = self._body()
        except (ValueError, UnicodeDecodeError) as exc:
            self._err(400, "invalid JSON body: %s" % exc)
            self._finish_log(path)
            return
        try:
            self._route_post(path, body)
        except ValueError as exc:
            self._err(400, str(exc))
        except Exception as exc:
            self._err(500, "internal error: %s" % exc)
        self._finish_log(path)

    def do_DELETE(self):
        path = urlparse(self.path).path.rstrip("/")
        if not self._gate(True):
            return
        try:
            if path.startswith("/api/bans/"):
                target = unquote(path[len("/api/bans/"):])
                ok, res = CORE.bans.remove_ban(target)
                self._json({"ok": True, "result": res} if ok else {"ok": False, "error": res}, 200 if ok else 404)
            elif path.startswith("/api/allowlist/"):
                ok, res = CORE.bans.remove_allow(unquote(path[len("/api/allowlist/"):]))
                self._json({"ok": True, "result": res} if ok else {"ok": False, "error": res}, 200 if ok else 404)
            else:
                self._err(404, "not found")
        except Exception as exc:
            self._err(500, "internal error: %s" % exc)
        self._finish_log(path)

    # ── routing ──
    def _log_filters(self, q):
        return {"service": q.get("service", ""), "ip": q.get("ip", "").strip(),
                "event_type": q.get("event_type", ""), "status": q.get("status", ""),
                "q": q.get("q", ""), "since": _parse_date(q.get("since")),
                "until": _parse_date(q.get("until"), end=True)}

    def _route_get(self, path, q):
        if path == "/":
            html = HTML.replace("%%VERSION%%", VERSION).replace("%%SYSTEM%%", SYSTEM)
            self._send(200, "text/html; charset=utf-8", html.encode("utf-8"))
        elif path == "/favicon.ico":
            self._send(200, "image/svg+xml", FAVICON.encode())
        elif path == "/api/status":
            self._json(CORE.status())
        elif path == "/api/logs":
            deep = q.get("deep") in ("1", "true")
            res = CORE.logs.search(self._log_filters(q), q.get("limit", 500), deep)
            self._json({"ok": True, "count": len(res), "logs": res})
        elif path == "/api/logs/export":
            fmt = q.get("format", "json").lower()
            res = CORE.logs.search(self._log_filters(q), q.get("limit", 100000), True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            if fmt == "csv":
                buf = io.StringIO()
                w = csv.DictWriter(buf, fieldnames=LOG_FIELDS, extrasaction="ignore")
                w.writeheader()
                for e in res:
                    w.writerow(e)
                self._send(200, "text/csv; charset=utf-8", buf.getvalue().encode("utf-8"),
                           {"Content-Disposition": 'attachment; filename="secmngr-logs-%s.csv"' % stamp})
            else:
                self._send(200, "application/json", json.dumps(res, indent=1).encode("utf-8"),
                           {"Content-Disposition": 'attachment; filename="secmngr-logs-%s.json"' % stamp})
        elif path == "/api/bans":
            self._json({"ok": True, "bans": CORE.bans.list(), "allowlist": CORE.bans.allowlist,
                        "firewall": CORE.fw.public_caps()})
        elif path == "/api/bans/export":
            fmt = q.get("format", "json").lower()
            bans = CORE.bans.list()
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            if fmt == "csv":
                buf = io.StringIO()
                cols = ["target", "type", "created_at", "expires_at", "reason", "rule", "source", "offense", "hits"]
                w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
                w.writeheader()
                for b in bans:
                    w.writerow(b)
                self._send(200, "text/csv; charset=utf-8", buf.getvalue().encode(),
                           {"Content-Disposition": 'attachment; filename="secmngr-bans-%s.csv"' % stamp})
            elif fmt == "txt":
                self._send(200, "text/plain; charset=utf-8",
                           ("\n".join(b["target"] for b in bans if b["type"] != "soft") + "\n").encode(),
                           {"Content-Disposition": 'attachment; filename="secmngr-blocklist-%s.txt"' % stamp})
            else:
                self._send(200, "application/json", json.dumps(
                    {"bans": bans, "allowlist": CORE.bans.allowlist}, indent=1, default=str).encode(),
                    {"Content-Disposition": 'attachment; filename="secmngr-bans-%s.json"' % stamp})
        elif path == "/api/allowlist":
            self._json({"ok": True, "allowlist": CORE.bans.allowlist})
        elif path == "/api/ips":
            self._json({"ok": True, "ips": CORE.logs.ip_list(q.get("q", "").strip(), q.get("sort", "last_seen"),
                                                            min(int(q.get("limit", 300)), 5000))})
        elif path.startswith("/api/ips/"):
            ip = clean_ip(unquote(path[len("/api/ips/"):]))
            if not ip:
                return self._err(400, "invalid ip")
            stats = CORE.logs.ip_stats(ip)
            ban = CORE.bans.active_ban(ip, hard_only=False)
            self._json({"ok": True, "ip": ip, "stats": stats, "ban": ban,
                        "allowlisted": CORE.bans.is_allowlisted(ip), "protected": CORE.bans.is_protected(ip),
                        "offenses": CORE.bans.offenses.get(ip, 0),
                        "history": [h for h in CORE.bans.history if h.get("target") == ip][-50:],
                        "events": [e for e in CORE.events.query(24 * 30, limit=MEM_EVENTS) if e.get("ip") == ip][:50],
                        "recent_logs": CORE.logs.search({"ip": ip}, 100)})
        elif path == "/api/events":
            self._json({"ok": True, "events": CORE.events.query(float(q.get("hours", 24)), q.get("severity"),
                                                                q.get("category"), int(q.get("limit", 500)))})
        elif path == "/api/health":
            self._json({"ok": True, "health": CORE.monitor.snapshot(), "sources": CORE.collector.status()})
        elif path == "/api/rules":
            self._json({"ok": True, "rules": CORE.cfg.rules_copy(),
                        "detection_mode": CORE.cfg.get("detection_mode"),
                        "escalation_enabled": CORE.cfg.get("escalation_enabled")})
        elif path == "/api/stats":
            self._json({"ok": True, "stats": CORE.stats(q.get("hours", 24))})
        elif path == "/api/settings":
            self._json({"ok": True, "settings": CORE.cfg.settings, "defaults": DEFAULT_SETTINGS,
                        "data_dir": str(DATA_DIR), "bind": CORE.bind_info,
                        "files": {p.name: {"size": p.stat().st_size, "mode": oct(p.stat().st_mode & 0o777)}
                                  for p in sorted(DATA_DIR.iterdir()) if p.is_file()}})
        elif path == "/api/firewall":
            pending = [{"target": b["target"], "commands": (b.get("enforcement") or {}).get("manual_commands", [])}
                       for b in CORE.bans.list() if (b.get("enforcement") or {}).get("manual_commands")]
            self._json({"ok": True, "firewall": CORE.fw.public_caps(), "pending": pending})
        else:
            self._err(404, "not found")

    def _route_post(self, path, body):
        if not isinstance(body, dict) and path not in ("/api/rules",):
            raise ValueError("expected JSON object")
        if path == "/api/bans":
            ip = body.get("ip") or body.get("target") or body.get("cidr")
            btype = body.get("type", "temp")
            duration = body.get("duration", 3600)
            ok, res = CORE.bans.add_ban(ip, btype, duration, body.get("reason") or body.get("note") or "",
                                        source="manual", escalate=bool(body.get("escalate", True)))
            if ok and "/" not in str(ip):
                CORE.logs.note_ban(res["target"])
            self._json({"ok": True, "ban": res} if ok else {"ok": False, "error": res}, 200 if ok else 400)
        elif path == "/api/allowlist":
            ok, res = CORE.bans.add_allow(body.get("ip") or body.get("cidr"), body.get("note", ""))
            self._json({"ok": True, "entry": res} if ok else {"ok": False, "error": res}, 200 if ok else 400)
        elif path == "/api/rules":
            if isinstance(body, dict):
                extra = {k: body[k] for k in ("detection_mode", "escalation_enabled") if k in body}
                if extra:
                    CORE.cfg.update_settings(extra)
            updated = CORE.cfg.update_rules(body)
            CORE.events.emit("INFO", "config", "Detection rules updated", ", ".join(updated) or "settings only")
            self._json({"ok": True, "updated": updated, "rules": CORE.cfg.rules_copy()})
        elif path == "/api/rules/reset":
            CORE.cfg.update_rules([dict(r) for r in DEFAULT_RULES])
            CORE.events.emit("INFO", "config", "Detection rules reset to defaults")
            self._json({"ok": True, "rules": CORE.cfg.rules_copy()})
        elif path == "/api/settings":
            changed = CORE.cfg.update_settings(body)
            if "firewall_enforcement" in changed:
                CORE.fw.capabilities(force=True)
            note = ""
            if {"bind_host", "port"} & set(changed):
                note = "bind address/port take effect after restarting SEC-MNGR"
            CORE.events.emit("INFO", "config", "Settings updated", ", ".join(changed.keys()) or "no changes")
            self._json({"ok": True, "changed": changed, "note": note, "settings": CORE.cfg.settings})
        elif path == "/api/ports/trust":
            spec = str(body.get("port", "")).strip()
            proto = body.get("proto")
            if proto in ("tcp", "udp") and ":" not in spec:
                spec = "%s:%s" % (proto, spec)
            ports = list(CORE.cfg.get("known_ports"))
            if spec not in ports:
                ports.append(spec)
            CORE.cfg.update_settings({"known_ports": ports})
            CORE.events.emit("INFO", "ports", "Port %s marked as known-good" % spec)
            self._json({"ok": True, "known_ports": CORE.cfg.get("known_ports")})
        elif path == "/api/detection/run":
            n = CORE.detector.run_once()
            self._json({"ok": True, "ingested": n})
        elif path == "/api/monitor/run":
            CORE.monitor.run_once()
            self._json({"ok": True, "health": CORE.monitor.snapshot()})
        elif path == "/api/firewall/refresh":
            self._json({"ok": True, "firewall": {k: v for k, v in CORE.fw.capabilities(force=True).items()
                                                 if k != "_prefix"}})
        else:
            self._err(404, "not found")



# ─────────────────────────────────────────────────────────────────────────────
#  EMBEDDED DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
           '<rect width="64" height="64" rx="14" fill="#e879a8"/>'
           '<path d="M32 13 17 20v11c0 10 6 17 15 20 9-3 15-10 15-20V20z" fill="white" opacity="0.9"/>'
           '<path d="M26 33h12v9H26zM28 33v-4a4 4 0 0 1 8 0v4" fill="none" stroke="#e879a8" stroke-width="2.5"/>'
           '</svg>')

HTML = r"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SEC-MNGR · Security Manager</title>
<link rel="icon" href="/favicon.ico" type="image/svg+xml">
<style>
:root{
  --bg:#0d0d0f;--sidebar:#111114;--panel:#17171b;--panel2:#1d1d22;
  --border:#26262f;--txt:#e8e8ed;--dim:#64647a;--accent:#10b981;
  --green-btn:#059669;--pink:#e879a8;--red:#ef4444;--crit:#ef4444;
  --warn:#f59e0b;--ok:#10b981;--info:#60a5fa;
  --mono:Menlo,Consolas,"DejaVu Sans Mono",monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--txt);font:13px/1.5 var(--sans);height:100%}
a{color:var(--accent)}
.shell{display:flex;height:100vh;overflow:hidden}
.sidebar{width:185px;flex-shrink:0;background:var(--sidebar);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden}
.sb-brand{padding:14px 12px 12px;border-bottom:1px solid var(--border);display:flex;gap:10px;align-items:flex-start}
.sb-icon{width:36px;height:36px;flex-shrink:0;background:var(--pink);border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:18px}
.sb-title{font-size:14px;font-weight:700;color:var(--txt);letter-spacing:-.3px}
.sb-the{color:var(--pink)}
.sb-tool{font-size:10px;font-weight:600;color:var(--txt);letter-spacing:.5px;margin-top:1px;text-transform:uppercase}
.sb-sub{font-size:9px;color:var(--dim);text-transform:uppercase;letter-spacing:.3px;line-height:1.3;margin-top:2px}
.sb-nav{flex:1;overflow-y:auto;padding:6px 0}
.sb-nav button{display:flex;align-items:center;gap:8px;width:100%;background:none;border:none;border-left:3px solid transparent;color:var(--dim);padding:9px 14px;font:13px var(--sans);cursor:pointer;text-align:left;transition:color .15s,background .15s}
.sb-nav button:hover{color:var(--txt);background:rgba(255,255,255,.03)}
.sb-nav button.on{color:var(--txt);background:rgba(16,185,129,.08);border-left-color:var(--accent)}
.sb-footer{padding:10px;border-top:1px solid var(--border)}
.sb-refresh{width:100%;background:transparent;border:1px solid var(--border);color:var(--dim);border-radius:6px;padding:7px;font:13px var(--sans);cursor:pointer;margin-bottom:8px}
.sb-refresh:hover{color:var(--txt);border-color:#444}
.sb-ver{font-size:10px;color:var(--dim);text-align:center;line-height:1.5}
.content{flex:1;display:flex;flex-direction:column;overflow:hidden}
.topbar{height:52px;padding:0 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:10px;flex-shrink:0}
.tp-title{font-size:15px;font-weight:600;color:var(--txt)}
.tp-addr{font-size:11px;color:var(--dim);background:var(--panel);border:1px solid var(--border);border-radius:4px;padding:2px 8px;font-family:var(--mono)}
.grow{flex:1}
.sub{color:var(--dim);font-size:11px}
.live-dot{font-size:12px;color:var(--accent)}
.pill{padding:3px 10px;border-radius:999px;font-size:11px;font-weight:700;border:1px solid}
.lv-LOW{color:var(--ok);border-color:var(--ok)}
.lv-ELEVATED{color:var(--warn);border-color:var(--warn)}
.lv-HIGH{color:#fb923c;border-color:#fb923c}
.lv-CRITICAL{color:#fff;background:var(--crit);border-color:var(--crit);animation:pulse 1.4s infinite}
@keyframes pulse{50%{box-shadow:0 0 14px var(--crit)}}
main{flex:1;overflow-y:auto;padding:18px 20px}
.tab{display:none}.tab.on{display:block}
.grid{display:grid;gap:12px}.g4{grid-template-columns:repeat(auto-fit,minmax(200px,1fr))}
.g2{grid-template-columns:repeat(auto-fit,minmax(380px,1fr))}
.card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px}
.card h3{margin:0 0 10px;font-size:11px;letter-spacing:.8px;text-transform:uppercase;color:var(--dim);font-weight:500}
.big{font-size:28px;font-weight:700;color:var(--accent)}
.big small{font-size:12px;color:var(--dim);font-weight:400}
table{width:100%;border-collapse:collapse}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--border);vertical-align:top}
th{color:var(--dim);font-weight:500;font-size:11px;text-transform:uppercase;position:sticky;top:0;background:var(--panel)}
tr:hover td{background:rgba(255,255,255,.02)}
.scroll{max-height:520px;overflow:auto}
.trunc{max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
input,select,textarea{background:var(--panel2);border:1px solid var(--border);color:var(--txt);border-radius:5px;padding:6px 8px;font:13px var(--sans)}
input:focus,select:focus{outline:1px solid var(--accent);border-color:var(--accent)}
.btn{background:var(--green-btn);color:#fff;border:none;border-radius:6px;padding:6px 12px;cursor:pointer;font:13px var(--sans)}
.btn:hover{background:var(--accent)}
.btn.ghost{background:transparent;border:1px solid var(--border);color:var(--dim)}
.btn.ghost:hover{color:var(--txt);border-color:#555}
.btn.sm{padding:2px 8px;font-size:11px}
.btn.danger{background:#7f1d1d;border:1px solid var(--red);color:#fff}
.btn.danger:hover{background:var(--red)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
.sev{font-size:10px;font-weight:700;padding:1px 6px;border-radius:3px}
.s-INFO{color:var(--info);border:1px solid #1e3a5f}
.s-WARN{color:var(--warn);border:1px solid #5c3d0a}
.s-CRITICAL{color:#fff;background:var(--crit)}
.st-UP{color:var(--ok)}.st-DOWN{color:var(--dim)}.st-WARN{color:var(--warn)}
.svc{border-left:3px solid var(--dim);padding-left:8px;margin:4px 0}
.svc.UP{border-left-color:var(--ok)}.svc.WARN{border-left-color:var(--warn)}.svc.DOWN{border-left-color:#333}
.bar{height:8px;background:var(--panel2);border-radius:4px;overflow:hidden;margin:6px 0}
.bar i{display:block;height:100%;background:var(--accent)}
.chart{display:flex;align-items:flex-end;gap:2px;height:110px}
.chart div{flex:1;background:var(--panel2);position:relative;min-height:1px}
.chart div b{position:absolute;bottom:0;left:0;right:0;background:var(--accent)}
.chart div b.bad{background:var(--red)}
.tl{border-left:2px solid var(--border);margin-left:6px;padding-left:14px}
.tl .ev{position:relative;padding:6px 0 10px}
.tl .ev:before{content:"";position:absolute;left:-20px;top:10px;width:10px;height:10px;border-radius:50%;background:var(--info)}
.tl .ev.WARN:before{background:var(--warn)}
.tl .ev.CRITICAL:before{background:var(--crit);box-shadow:0 0 8px var(--crit)}
.muted{color:var(--dim)}.ok{color:var(--ok)}.bad{color:var(--crit)}.wrn{color:var(--warn)}
code,.code{background:var(--panel2);border:1px solid var(--border);padding:1px 5px;border-radius:3px;font-size:12px;font-family:var(--mono)}
pre{background:var(--panel2);border:1px solid var(--border);padding:8px;border-radius:5px;overflow:auto;max-height:260px;white-space:pre-wrap;font-family:var(--mono)}
#toast{position:fixed;right:20px;bottom:20px;padding:10px 16px;border-radius:6px;background:var(--panel2);border:1px solid var(--accent);display:none;z-index:50;max-width:480px}
.modal{position:fixed;inset:0;background:#000b;display:none;align-items:center;justify-content:center;z-index:40}
.modal.on{display:flex}.modal .card{width:min(1000px,94vw);max-height:88vh;overflow:auto}
label.f{display:flex;flex-direction:column;gap:3px;font-size:11px;color:var(--dim)}
.kv td:first-child{color:var(--dim);width:220px}
</style></head><body>
<div class="shell">
<aside class="sidebar">
  <div class="sb-brand">
    <div class="sb-icon">&#x1F6E1;</div>
    <div>
      <div class="sb-title">Kill<span class="sb-the">The</span>Host</div>
      <div class="sb-tool">SEC-MNGR</div>
      <div class="sb-sub">KILLTHEHOST SUITE<br>&middot; SECURITY MANAGER</div>
    </div>
  </div>
  <nav class="sb-nav" id="nav">
    <button data-t="overview" class="on">&#x2316; Overview</button>
    <button data-t="logs">&#x1F4CB; Access Logs</button>
    <button data-t="ips">&#x1F310; IP Manager</button>
    <button data-t="rules">&#x26A1; Detection Rules</button>
    <button data-t="mon">&#x1F4CA; Monitoring</button>
    <button data-t="health">&#x2665; System Health</button>
    <button data-t="settings">&#x2699; Settings</button>
  </nav>
  <div class="sb-footer">
    <button class="sb-refresh" onclick="refresh()">&#x21bb; Refresh</button>
    <div class="sb-ver">SEC-MNGR v%%VERSION%%<br>KillTheHost</div>
  </div>
</aside>
<div class="content">
  <div class="topbar">
    <span class="tp-title" id="tp-title">Overview</span>
    <span class="tp-addr" id="tp-addr">127.0.0.1:8080</span>
    <div class="grow"></div>
    <span id="lvl" class="pill lv-LOW">THREAT: ...</span>
    <span class="live-dot">&#x25CF; Live</span>
    <span class="muted" id="upd" style="font-size:11px">&#x2014;</span>
  </div>
<main>
<!-- OVERVIEW -->
<section class="tab on" id="t-overview">
  <div class="grid g4">
    <div class="card"><h3>Threat level</h3><div class="big" id="o-level">—</div><div class="muted" id="o-det"></div></div>
    <div class="card"><h3>Security posture</h3><div class="big" id="o-score">—<small>/100</small></div><div class="bar"><i id="o-scorebar"></i></div></div>
    <div class="card"><h3>Active bans</h3><div class="big" id="o-bans">—</div><div class="muted" id="o-bans2"></div></div>
    <div class="card"><h3>Alerts (24h)</h3><div class="big" id="o-alerts">—</div><div class="muted" id="o-alerts2"></div></div>
    <div class="card"><h3>Services</h3><div class="big" id="o-svc">—</div><div class="muted" id="o-fw"></div></div>
  </div>
  <div class="card" style="margin-top:12px"><h3>Service health</h3><div class="grid g4" id="o-cards"></div></div>
  <div class="grid g2" style="margin-top:12px">
    <div class="card"><h3>Traffic, last 24h (red = suspicious)</h3><div class="chart" id="o-chart"></div>
      <div class="row muted" style="margin-top:8px" id="o-traffic"></div></div>
    <div class="card"><h3>Posture findings</h3><div id="o-find" class="scroll" style="max-height:220px"></div></div>
    <div class="card"><h3>Recent alerts</h3><div id="o-recent" class="tl"></div></div>
    <div class="card"><h3>Top offenders (24h)</h3><table><thead><tr><th>IP</th><th>Bad events</th><th>Status</th><th></th></tr></thead><tbody id="o-off"></tbody></table></div>
  </div>
</section>

<!-- ACCESS LOGS -->
<section class="tab" id="t-logs">
  <div class="card">
    <div class="row">
      <select id="l-svc"><option value="">all services</option></select>
      <input id="l-ip" placeholder="IP or CIDR" size="18">
      <select id="l-type"><option value="">all event types</option>
        <option>request</option><option>auth_fail</option><option>not_found</option><option>malformed</option>
        <option>smtp_auth_fail</option><option>smtp_reject</option><option>smtp_connect</option><option>db_auth_fail</option><option>blocked</option></select>
      <input id="l-status" placeholder="status" size="6">
      <input id="l-q" placeholder="search path / UA / detail" size="24">
      <label class="muted">from <input type="date" id="l-since"></label>
      <label class="muted">to <input type="date" id="l-until"></label>
      <select id="l-limit"><option>200</option><option selected>500</option><option>2000</option><option>10000</option></select>
      <label class="muted"><input type="checkbox" id="l-deep"> search full history (disk)</label>
      <button class="btn" onclick="loadLogs()">Search</button>
      <button class="btn ghost" onclick="exportLogs('csv')">Export CSV</button>
      <button class="btn ghost" onclick="exportLogs('json')">Export JSON</button>
    </div>
    <div class="muted" id="l-count"></div>
    <div class="scroll" style="max-height:640px"><table><thead><tr><th>Time</th><th>IP</th><th>Service</th><th>Event</th><th>Method</th><th>Path</th><th>Status</th><th>User-Agent / detail</th></tr></thead><tbody id="l-body"></tbody></table></div>
  </div>
</section>

<!-- IP MANAGER -->
<section class="tab" id="t-ips">
  <div class="grid g2">
    <div class="card"><h3>Ban an IP / CIDR</h3>
      <div class="row">
        <label class="f">IP or CIDR<input id="b-ip" placeholder="203.0.113.7 or 198.51.100.0/24" size="26"></label>
        <label class="f">Type<select id="b-type"><option value="temp">temporary</option><option value="perm">permanent</option><option value="soft">soft-warn (log only)</option></select></label>
        <label class="f">Duration<select id="b-dur"><option value="900">15 min</option><option value="3600" selected>1 hour</option><option value="21600">6 hours</option><option value="86400">1 day</option><option value="604800">7 days</option><option value="2592000">30 days</option></select></label>
      </div>
      <div class="row"><label class="f" style="flex:1">Reason / note<input id="b-reason" placeholder="why?" style="width:100%"></label>
        <button class="btn" onclick="addBan()">Ban</button></div>
      <div class="muted">Escalation: a 2nd offense doubles the duration, a 3rd makes the ban permanent. Allowlisted and local addresses can never be banned.</div>
    </div>
    <div class="card"><h3>Allowlist (overrides every ban)</h3>
      <div class="row"><input id="a-ip" placeholder="IP or CIDR" size="22"><input id="a-note" placeholder="note" size="20">
        <button class="btn" onclick="addAllow()">Allow</button></div>
      <table><thead><tr><th>CIDR</th><th>Note</th><th>Added</th><th></th></tr></thead><tbody id="a-body"></tbody></table>
    </div>
  </div>
  <div class="card" style="margin-top:12px"><h3>Active bans</h3>
    <div class="row"><span class="muted" id="b-fw"></span><div class="grow"></div>
      <button class="btn ghost sm" onclick="dl('/api/bans/export?format=csv')">Export CSV</button>
      <button class="btn ghost sm" onclick="dl('/api/bans/export?format=json')">Export JSON</button>
      <button class="btn ghost sm" onclick="dl('/api/bans/export?format=txt')">Blocklist TXT</button></div>
    <div class="scroll"><table><thead><tr><th>Target</th><th>Type</th><th>Remaining</th><th>Reason / rule</th><th>Offense</th><th>Hits</th><th>Enforcement</th><th>Created</th><th></th></tr></thead><tbody id="b-body"></tbody></table></div>
    <div id="b-pending"></div>
  </div>
  <div class="card" style="margin-top:12px"><h3>IP intelligence</h3>
    <div class="row"><input id="i-q" placeholder="filter IP" size="20">
      <select id="i-sort"><option value="last_seen">last seen</option><option value="requests">requests</option><option value="blocked">blocked</option><option value="bans">bans</option><option value="first_seen">first seen</option></select>
      <button class="btn ghost" onclick="loadIps()">Apply</button></div>
    <div class="scroll"><table><thead><tr><th>IP</th><th>Requests</th><th>First seen</th><th>Last seen</th><th>Services</th><th>Bad events</th><th>Blocked</th><th>Bans</th><th></th></tr></thead><tbody id="i-body"></tbody></table></div>
  </div>
</section>

<!-- RULES -->
<section class="tab" id="t-rules">
  <div class="card">
    <div class="row">
      <label class="muted">Mode <select id="r-mode"><option value="enforce">enforce (ban)</option><option value="monitor">monitor (soft-warn only)</option></select></label>
      <label class="muted"><input type="checkbox" id="r-esc"> escalation (2nd = 2× duration, 3rd = permanent)</label>
      <div class="grow"></div>
      <button class="btn" onclick="saveRules()">Save rules</button>
      <button class="btn ghost" onclick="runDetect()">Run detection now</button>
      <button class="btn ghost" onclick="resetRules()">Reset defaults</button>
    </div>
    <table><thead><tr><th>On</th><th>Rule</th><th>Description</th><th>Threshold</th><th>Window (s)</th><th>Ban duration (s)</th><th>Summary</th></tr></thead><tbody id="r-body"></tbody></table>
    <p class="muted">The engine polls every <span id="r-int">30</span>s. Each alert names the rule that fired, how many events it counted and in what window.</p>
  </div>
  <div class="card" style="margin-top:12px"><h3>Detection alerts</h3><div id="r-events" class="tl"></div></div>
</section>

<!-- MONITORING -->
<section class="tab" id="t-mon">
  <div class="grid g2">
    <div class="card"><h3>Events timeline</h3>
      <div class="row"><select id="e-sev"><option value="">all severities</option><option>INFO</option><option>WARN</option><option>CRITICAL</option><option value="WARN,CRITICAL">WARN + CRITICAL</option></select>
        <select id="e-cat"><option value="">all categories</option><option>detection</option><option>ban</option><option>service</option><option>health</option><option>ports</option><option>posture</option><option>firewall</option><option>config</option><option>system</option></select>
        <select id="e-hours"><option value="1">1h</option><option value="24" selected>24h</option><option value="168">7d</option><option value="720">30d</option></select>
        <button class="btn ghost" onclick="loadEvents()">Apply</button><button class="btn ghost" onclick="runMonitor()">Run checks now</button></div>
      <div id="e-tl" class="tl scroll" style="max-height:640px"></div></div>
    <div>
      <div class="card"><h3>Listening ports</h3><div class="scroll" style="max-height:330px"><table><thead><tr><th>Proto</th><th>Address</th><th>Port</th><th>Process</th><th>Flag</th><th></th></tr></thead><tbody id="p-body"></tbody></table></div></div>
      <div class="card" style="margin-top:12px"><h3>Firewall</h3><table class="kv"><tbody id="fw-kv"></tbody></table>
        <div class="row" style="margin-top:8px"><button class="btn ghost sm" onclick="fwRefresh()">Re-detect firewall</button></div>
        <h3 style="margin-top:12px">UFW rules</h3><pre id="ufw-rules">—</pre>
        <div id="fw-pending"></div></div>
    </div>
  </div>
</section>

<!-- HEALTH -->
<section class="tab" id="t-health">
  <div class="grid g4" id="h-res"></div>
  <div class="card" style="margin-top:12px"><h3>Services</h3><div class="grid g4" id="h-cards"></div></div>
  <div class="grid g2" style="margin-top:12px">
    <div class="card"><h3>Log sources</h3><div class="scroll" style="max-height:360px"><table><thead><tr><th>Source</th><th>Service</th><th>Type</th><th>State</th></tr></thead><tbody id="h-src"></tbody></table></div></div>
    <div class="card"><h3>Host</h3><table class="kv"><tbody id="h-host"></tbody></table>
      <h3 style="margin-top:12px">Secret file permissions</h3><div id="h-secret"></div></div>
  </div>
</section>

<!-- SETTINGS -->
<section class="tab" id="t-settings">
  <div class="grid g2">
    <div class="card"><h3>Settings</h3><table class="kv"><tbody id="s-form"></tbody></table>
      <div class="row" style="margin-top:10px"><button class="btn" onclick="saveSettings()">Save settings</button><span class="muted" id="s-note"></span></div></div>
    <div class="card"><h3>Data files <span class="muted" id="s-dir"></span></h3>
      <table><thead><tr><th>File</th><th>Size</th><th>Mode</th></tr></thead><tbody id="s-files"></tbody></table>
      <p class="muted">The directory is 0700 and every file 0600. SEC-MNGR stores no secrets and only reads the other managers' files.</p>
      <h3 style="margin-top:12px">API</h3>
      <pre>GET  /api/status | /api/logs?service=&amp;ip=&amp;limit= | /api/events | /api/health | /api/stats
GET  /api/bans      POST /api/bans {"ip","type":"temp|perm|soft","duration","reason"}
DELETE /api/bans/{ip or url-encoded cidr}
GET/POST /api/rules
Mutating calls need headers: X-SecMngr-Request: 1 and Content-Type: application/json</pre></div>
  </div>
</section>
</main>
</div>
</div>

<div class="modal" id="modal" onclick="if(event.target===this)closeModal()"><div class="card" id="modal-body"></div></div>
<div id="toast"></div>

<script>
"use strict";
const $=id=>document.getElementById(id);
const esc=v=>String(v==null?"":v).replace(/[&<>"'`]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;","`":"&#96;"}[c]));
const fmtT=ts=>ts?new Date(ts*1000).toLocaleString():"—";
const ago=ts=>{if(!ts)return"—";let s=Math.max(0,Date.now()/1000-ts);return s<60?Math.round(s)+"s ago":s<3600?Math.round(s/60)+"m ago":s<86400?Math.round(s/3600)+"h ago":Math.round(s/86400)+"d ago"};
const dur=s=>{if(s==null)return"∞";s=+s;if(s<60)return s+"s";if(s<3600)return Math.round(s/60)+"m";if(s<86400)return(s/3600).toFixed(s%3600?1:0)+"h";return(s/86400).toFixed(s%86400?1:0)+"d"};
const bytes=b=>{b=+b||0;const u=["B","KB","MB","GB","TB"];let i=0;while(b>=1024&&i<4){b/=1024;i++}return b.toFixed(i?1:0)+" "+u[i]};
const sev=s=>`<span class="sev s-${esc(s)}">${esc(s)}</span>`;
let TAB="overview",SERVICES={},RULES=[],SETTINGS={},DEFAULTS={};

async function api(path,opts){
  opts=opts||{};const o={method:opts.method||"GET",headers:{},credentials:"same-origin"};
  if(o.method!=="GET"){o.headers["X-SecMngr-Request"]="1";o.headers["Content-Type"]="application/json";
    if(opts.body!==undefined)o.body=JSON.stringify(opts.body);}
  const r=await fetch(path,o);let j;try{j=await r.json()}catch(e){j={ok:false,error:"HTTP "+r.status}}
  if(!r.ok&&j.ok!==false)j.ok=false;return j;
}
function toast(msg,bad){const t=$("toast");t.textContent=msg;t.style.borderColor=bad?"var(--crit)":"var(--ok)";t.style.display="block";clearTimeout(t._h);t._h=setTimeout(()=>t.style.display="none",4500)}
function dl(url){const a=document.createElement("a");a.href=url;a.download="";document.body.appendChild(a);a.click();a.remove()}
function svcLabel(k){return (SERVICES[k]&&SERVICES[k].label)||k}

const TITLES={overview:"Overview",logs:"Access Logs",ips:"IP Manager",rules:"Detection Rules",mon:"Monitoring",health:"System Health",settings:"Settings"};
document.querySelectorAll("#nav button").forEach(b=>b.onclick=()=>{
  document.querySelectorAll("#nav button").forEach(x=>x.classList.toggle("on",x===b));
  document.querySelectorAll(".tab").forEach(x=>x.classList.toggle("on",x.id==="t-"+b.dataset.t));
  TAB=b.dataset.t;location.hash=TAB;
  const titleEl=$("tp-title");if(titleEl)titleEl.textContent=TITLES[TAB]||TAB;
  refresh();});

/* ---------- OVERVIEW ---------- */
function svcCard(k,s,detail){
  return `<div class="card svc ${esc(s.status)}"><div style="display:flex;justify-content:space-between"><b>${esc(s.label)}</b><span class="st-${esc(s.status)}">● ${esc(s.status)}</span></div>
  <div class="muted">:${esc(s.port)}${detail?" · "+esc(detail):""}</div></div>`}
async function loadStatus(){
  const s=await api("/api/status");if(!s||!s.threat_level)return null;
  SERVICES=s.services||{};
  const l=$("lvl");l.className="pill lv-"+s.threat_level;l.textContent="THREAT: "+s.threat_level;
  $("upd").textContent="updated "+new Date().toLocaleTimeString();
  return s;
}
async function loadOverview(s){
  if(!s)return;
  $("o-level").innerHTML=`<span class="pill lv-${esc(s.threat_level)}" style="font-size:18px">${esc(s.threat_level)}</span>`;
  $("o-det").textContent=`detection: ${s.detection.mode} · last run ${s.detection.last_run?new Date(s.detection.last_run).toLocaleTimeString():"—"}`;
  $("o-score").innerHTML=`${s.posture_score}<small>/100</small>`;
  const sb=$("o-scorebar");sb.style.width=s.posture_score+"%";sb.style.background=s.posture_score>=85?"var(--ok)":s.posture_score>=60?"var(--warn)":"var(--crit)";
  $("o-bans").textContent=s.active_bans;
  $("o-bans2").textContent=`${s.perm_bans} permanent · ${s.temp_bans} temporary · ${s.soft_warns} soft-warn · ${s.allowlist} allowlisted`;
  const a=s.alerts_24h;$("o-alerts").innerHTML=`<span class="bad">${a.CRITICAL}</span> / <span class="wrn">${a.WARN}</span>`;
  $("o-alerts2").textContent=`critical / warn · ${a.INFO} info`;
  $("o-svc").textContent=`${s.services_up}/${s.services_total} up`;
  $("o-fw").textContent=`enforcement: ${s.firewall.backend}${s.firewall.privilege?" ("+s.firewall.privilege+")":""}`;
  $("o-cards").innerHTML=Object.entries(s.services).map(([k,v])=>svcCard(k,v)).join("");
  $("o-find").innerHTML=s.findings.length?s.findings.map(f=>`<div style="margin-bottom:8px">${sev(f.severity)} <b>${esc(f.title)}</b><div class="muted">${esc(f.detail)}</div></div>`).join(""):'<div class="ok">No posture issues found.</div>';
  $("o-recent").innerHTML=s.recent_alerts.length?s.recent_alerts.map(evHtml).join(""):'<div class="muted">No WARN or CRITICAL alerts in the last 24h.</div>';
  const st=(await api("/api/stats?hours=24")).stats;if(!st)return;
  const mx=Math.max(1,...st.hourly);
  $("o-chart").innerHTML=st.hourly.map((v,i)=>`<div title="${v} entries, ${st.hourly_bad[i]} suspicious (${24-i}h ago)" style="height:${Math.max(1,100*v/mx)}%"><b style="height:${v?100*st.hourly_bad[i]/v:0}%"></b></div>`).join("");
  $("o-traffic").textContent=`${st.total_entries} entries · ${st.unique_ips} unique IPs · ${st.bans_total} bans · ${st.auto_bans_since_start} auto-bans since start`;
  $("o-off").innerHTML=st.top_offenders.length?st.top_offenders.map(o=>`<tr><td><a href="#" onclick="ipDetail('${esc(o.ip)}');return false">${esc(o.ip)}</a></td><td>${o.count}</td><td>${o.banned?'<span class="bad">BANNED</span>':'<span class="muted">active</span>'}</td><td>${o.banned?"":`<button class="btn sm" onclick="quickBan('${esc(o.ip)}')">Ban 1h</button>`}</td></tr>`).join(""):'<tr><td colspan="4" class="muted">No offenders.</td></tr>';
}
function evHtml(e){return `<div class="ev ${esc(e.severity)}">${sev(e.severity)} <b>${esc(e.title)}</b> <span class="muted">· ${esc(e.category)} · ${ago(e.ts)}</span>${e.ip?` <a href="#" onclick="ipDetail('${esc(e.ip)}');return false">${esc(e.ip)}</a>`:""}<div class="muted">${esc(e.detail)}</div></div>`}

/* ---------- LOGS ---------- */
function logQuery(){
  const p=new URLSearchParams();const add=(k,v)=>{if(v)p.set(k,v)};
  add("service",$("l-svc").value);add("ip",$("l-ip").value.trim());add("event_type",$("l-type").value);
  add("status",$("l-status").value.trim());add("q",$("l-q").value.trim());add("since",$("l-since").value);add("until",$("l-until").value);
  return p;
}
async function loadLogs(){
  const sel=$("l-svc");if(sel.options.length<2)Object.keys(SERVICES).forEach(k=>sel.add(new Option(svcLabel(k),k)));
  const p=logQuery();p.set("limit",$("l-limit").value);if($("l-deep").checked)p.set("deep","1");
  const r=await api("/api/logs?"+p);if(!r.ok)return toast(r.error||"failed",1);
  $("l-count").textContent=`${r.count} entries`;
  $("l-body").innerHTML=r.logs.map(e=>`<tr><td>${esc(fmtT(e.ts))}</td><td><a href="#" onclick="ipDetail('${esc(e.ip)}');return false">${esc(e.ip)}</a></td><td>${esc(svcLabel(e.service))}</td>
   <td class="${/fail|malformed|blocked|reject/.test(e.event_type)?"bad":e.event_type==="not_found"?"wrn":"muted"}">${esc(e.event_type)}</td><td>${esc(e.method)}</td><td class="trunc" title="${esc(e.path)}">${esc(e.path)}</td><td>${esc(e.status)}</td><td class="trunc" title="${esc(e.user_agent+" "+e.detail)}">${esc(e.user_agent||e.detail)}</td></tr>`).join("")||'<tr><td colspan="8" class="muted">No entries match.</td></tr>';
}
function exportLogs(fmt){const p=logQuery();p.set("format",fmt);dl("/api/logs/export?"+p)}

/* ---------- IP MANAGER ---------- */
async function loadBans(){
  const r=await api("/api/bans");if(!r.ok)return;
  const fw=r.firewall;$("b-fw").textContent=`Enforcement backend: ${fw.backend} · ufw ${fw.ufw?(fw.ufw_active?"active":"installed/inactive"):"n/a"} · iptables ${fw.iptables?"yes":"no"} · privilege ${fw.privilege||"none (HTTP-level only)"}`;
  $("b-body").innerHTML=r.bans.map(b=>{const en=b.enforcement||{};
    return `<tr><td><a href="#" onclick="ipDetail('${esc(b.target.split("/")[0])}');return false">${esc(b.target)}</a></td><td>${b.type==="perm"?'<span class="bad">perm</span>':b.type==="soft"?'<span class="wrn">soft-warn</span>':"temp"}</td>
    <td>${b.type==="temp"?dur(b.remaining):b.type==="perm"?"∞":"—"}</td><td class="trunc" title="${esc(b.reason)}">${b.rule?`<code>${esc(b.rule)}</code> `:""}${esc(b.reason)}</td><td>#${esc(b.offense)}</td><td>${esc(b.hits)}</td>
    <td>${esc(en.backend||"?")}${en.applied?' <span class="ok">✓</span>':en.manual_commands&&en.manual_commands.length?' <span class="wrn">pending sudo</span>':""}${en.http_level?' <span class="muted">+http</span>':""}</td>
    <td>${esc(ago(b.created))}</td><td><button class="btn sm ghost" onclick="unban('${esc(b.target)}')">Unban</button>${b.type!=="perm"?` <button class="btn sm ghost" onclick="makePerm('${esc(b.target)}')">Perm</button>`:""}</td></tr>`}).join("")||'<tr><td colspan="9" class="muted">No active bans.</td></tr>';
  const pend=r.bans.filter(b=>b.enforcement&&b.enforcement.manual_commands&&b.enforcement.manual_commands.length);
  $("b-pending").innerHTML=pend.length?`<h3 style="margin-top:12px">Firewall commands needing sudo (enforced at HTTP level until run)</h3><pre>${esc(pend.map(b=>b.enforcement.manual_commands.join("\n")).join("\n"))}</pre>`:"";
  $("a-body").innerHTML=r.allowlist.map(a=>`<tr><td>${esc(a.cidr)}</td><td>${esc(a.note)}</td><td>${esc(ago(a.added))}</td><td><button class="btn sm ghost" onclick="rmAllow('${esc(a.cidr)}')">Remove</button></td></tr>`).join("")||'<tr><td colspan="4" class="muted">Empty. Loopback and local networks are always protected.</td></tr>';
}
async function addBan(){
  const body={ip:$("b-ip").value.trim(),type:$("b-type").value,duration:+$("b-dur").value,reason:$("b-reason").value.trim()};
  if(!body.ip)return toast("Enter an IP or CIDR",1);
  const r=await api("/api/bans",{method:"POST",body});
  if(r.ok){toast(`Banned ${r.ban.target} (${r.ban.type}${r.ban.type==="temp"?", "+dur(r.ban.duration):""}, offense #${r.ban.offense})`);$("b-ip").value="";$("b-reason").value="";loadBans()}else toast(r.error,1);
}
async function quickBan(ip){const r=await api("/api/bans",{method:"POST",body:{ip,type:"temp",duration:3600,reason:"manual quick-ban from dashboard"}});r.ok?toast("Banned "+ip):toast(r.error,1);refresh()}
async function makePerm(t){const r=await api("/api/bans",{method:"POST",body:{ip:t,type:"perm",reason:"upgraded to permanent",escalate:false}});r.ok?toast(t+" is now permanent"):toast(r.error,1);loadBans()}
async function unban(t){if(!confirm("Unban "+t+"?"))return;const r=await api("/api/bans/"+encodeURIComponent(t),{method:"DELETE"});r.ok?toast("Unbanned "+t):toast(r.error,1);loadBans()}
async function addAllow(){const r=await api("/api/allowlist",{method:"POST",body:{ip:$("a-ip").value.trim(),note:$("a-note").value.trim()}});
  if(r.ok){toast("Allowlisted "+r.entry.cidr);$("a-ip").value="";$("a-note").value=""}else toast(r.error,1);loadBans()}
async function rmAllow(c){const r=await api("/api/allowlist/"+encodeURIComponent(c),{method:"DELETE"});r.ok?toast("Removed "+c):toast(r.error,1);loadBans()}
async function loadIps(){
  const p=new URLSearchParams({sort:$("i-sort").value,limit:"300"});if($("i-q").value.trim())p.set("q",$("i-q").value.trim());
  const r=await api("/api/ips?"+p);if(!r.ok)return;
  $("i-body").innerHTML=r.ips.map(s=>{const bad=Object.entries(s.event_types||{}).filter(([k])=>k!=="request"&&k!=="smtp_connect").reduce((a,[,v])=>a+v,0);
    return `<tr><td><a href="#" onclick="ipDetail('${esc(s.ip)}');return false">${esc(s.ip)}</a></td><td>${s.requests}</td><td>${esc(ago(s.first_seen))}</td><td>${esc(ago(s.last_seen))}</td><td>${esc((s.services||[]).map(svcLabel).join(", "))}</td>
    <td class="${bad?"bad":""}">${bad}</td><td>${s.blocked}</td><td>${s.bans}</td><td><button class="btn sm" onclick="quickBan('${esc(s.ip)}')">Ban</button></td></tr>`}).join("")||'<tr><td colspan="9" class="muted">No IPs seen yet.</td></tr>';
}
async function ipDetail(ip){
  const r=await api("/api/ips/"+encodeURIComponent(ip));if(!r.ok)return toast(r.error,1);
  const s=r.stats||{};
  $("modal-body").innerHTML=`<div class="row"><h3 style="margin:0;color:#fff;font-size:16px">${esc(r.ip)}</h3>
   ${r.ban?`<span class="pill lv-CRITICAL">${esc(r.ban.type.toUpperCase())} BAN</span>`:""}${r.allowlisted?'<span class="pill lv-LOW">ALLOWLISTED</span>':""}${r.protected?'<span class="pill lv-LOW">LOCAL / PROTECTED</span>':""}
   <div class="grow"></div>${r.ban?`<button class="btn ghost sm" onclick="unban('${esc(r.ban.target)}');closeModal()">Unban</button>`:`<button class="btn sm" onclick="quickBan('${esc(r.ip)}');closeModal()">Ban 1h</button>`}<button class="btn ghost sm" onclick="closeModal()">✕</button></div>
   <table class="kv"><tbody><tr><td>Requests</td><td>${esc(s.requests||0)}</td></tr><tr><td>First / last seen</td><td>${esc(fmtT(s.first_seen))} → ${esc(fmtT(s.last_seen))}</td></tr>
   <tr><td>Services</td><td>${esc((s.services||[]).map(svcLabel).join(", ")||"—")}</td></tr><tr><td>Event types</td><td>${esc(Object.entries(s.event_types||{}).map(([k,v])=>k+": "+v).join(" · ")||"—")}</td></tr>
   <tr><td>Blocked requests / bans / offenses</td><td>${esc(s.blocked||0)} / ${esc(s.bans||0)} / ${esc(r.offenses)}</td></tr><tr><td>Last user-agent</td><td>${esc(s.last_ua||"—")}</td></tr></tbody></table>
   <h3 style="margin-top:12px">Ban history</h3>${r.history.length?`<table><tbody>${r.history.slice().reverse().map(h=>`<tr><td>${esc(fmtT(h.ts))}</td><td>${esc(h.action)}</td><td>${esc(h.type||"")}</td><td>${esc(h.rule||h.source||"")}</td><td>${esc(h.reason||"")}</td></tr>`).join("")}</tbody></table>`:'<div class="muted">none</div>'}
   <h3 style="margin-top:12px">Alerts</h3><div class="tl">${r.events.map(evHtml).join("")||'<div class="muted">none</div>'}</div>
   <h3 style="margin-top:12px">Recent log entries</h3><div class="scroll" style="max-height:260px"><table><tbody>${r.recent_logs.map(e=>`<tr><td>${esc(fmtT(e.ts))}</td><td>${esc(svcLabel(e.service))}</td><td>${esc(e.event_type)}</td><td>${esc(e.method)}</td><td class="trunc">${esc(e.path||e.detail)}</td><td>${esc(e.status)}</td></tr>`).join("")||'<tr><td class="muted">none</td></tr>'}</tbody></table></div>`;
  $("modal").classList.add("on");
}
function closeModal(){$("modal").classList.remove("on")}

/* ---------- RULES ---------- */
async function loadRules(force){
  const r=await api("/api/rules");if(!r.ok)return;
  if(!force&&document.activeElement&&document.activeElement.closest&&document.activeElement.closest("#r-body"))return; // don't clobber edits
  RULES=r.rules;$("r-mode").value=r.detection_mode;$("r-esc").checked=!!r.escalation_enabled;
  $("r-body").innerHTML=RULES.map((x,i)=>{const ro=x.id==="REPEAT_OFFENDER";
    return `<tr><td><input type="checkbox" data-i="${i}" data-k="enabled" ${x.enabled?"checked":""}></td><td><b>${esc(x.id)}</b><div class="muted">${esc(x.name)}</div></td><td class="muted" style="max-width:320px">${esc(x.description)}</td>
    <td><input type="number" min="1" data-i="${i}" data-k="threshold" value="${esc(x.threshold)}" style="width:80px"></td>
    <td>${ro?'<span class="muted">—</span>':`<input type="number" min="10" data-i="${i}" data-k="window" value="${esc(x.window)}" style="width:90px">`}</td>
    <td>${ro?'<span class="muted">permanent</span>':`<input type="number" min="60" data-i="${i}" data-k="duration" value="${esc(x.duration)}" style="width:100px">`}</td>
    <td class="muted">${ro?`${x.threshold} bans → permanent`:`${x.threshold} in ${dur(x.window)} → ban ${dur(x.duration)}`}</td></tr>`}).join("");
  const ev=await api("/api/events?category=detection&hours=168&limit=50");
  $("r-events").innerHTML=(ev.events||[]).map(evHtml).join("")||'<div class="muted">No detections in the last 7 days.</div>';
}
async function saveRules(){
  const rules=RULES.map(r=>Object.assign({},r));
  document.querySelectorAll("#r-body input").forEach(inp=>{const r=rules[+inp.dataset.i];r[inp.dataset.k]=inp.type==="checkbox"?inp.checked:+inp.value});
  const res=await api("/api/rules",{method:"POST",body:{rules,detection_mode:$("r-mode").value,escalation_enabled:$("r-esc").checked}});
  res.ok?toast("Rules saved"+(res.updated.length?": "+res.updated.join(", "):"")):toast(res.error,1);document.activeElement.blur();loadRules(true);
}
async function resetRules(){if(!confirm("Reset all rules to defaults?"))return;const r=await api("/api/rules/reset",{method:"POST",body:{}});r.ok?toast("Rules reset"):toast(r.error,1);loadRules(true)}
async function runDetect(){const r=await api("/api/detection/run",{method:"POST",body:{}});r.ok?toast(`Detection cycle done: ${r.ingested} new entries ingested`):toast(r.error,1);refresh()}

/* ---------- MONITORING ---------- */
async function loadEvents(){
  const p=new URLSearchParams({hours:$("e-hours").value,limit:"500"});if($("e-sev").value)p.set("severity",$("e-sev").value);if($("e-cat").value)p.set("category",$("e-cat").value);
  const r=await api("/api/events?"+p);$("e-tl").innerHTML=(r.events||[]).map(evHtml).join("")||'<div class="muted">No events.</div>';
}
async function loadMon(){
  loadEvents();
  const h=(await api("/api/health")).health||{};
  $("p-body").innerHTML=(h.ports||[]).map(p=>`<tr><td>${esc(p.proto)}</td><td class="${p.public?"wrn":"muted"}">${esc(p.addr)}</td><td>${esc(p.port)}</td><td class="trunc">${esc(p.process||"—")}</td>
   <td>${p.flag?sev(p.flag):'<span class="ok">known</span>'}${p.risk?` <span class="bad">${esc(p.risk)}</span>`:""}</td><td>${p.flag?`<button class="btn sm ghost" onclick="trustPort('${esc(p.port)}','${esc(p.proto)}')">Trust</button>`:""}</td></tr>`).join("")||'<tr><td colspan="6" class="muted">No data yet (ss/netstat unavailable or first check pending).</td></tr>';
  const f=await api("/api/firewall");const fw=f.firewall||{},u=h.ufw||{};
  const kv=[["Backend",fw.backend],["Enforcement enabled",fw.enabled],["Privilege",fw.privilege||"none — HTTP-level blocking only"],["UFW",fw.ufw?(fw.ufw_active?"active":fw.ufw_active===false?"inactive":"installed (status needs root)"):"not installed"],
    ["iptables / ip6tables",(fw.iptables?"yes":"no")+" / "+(fw.ip6tables?"yes":"no")],["DOCKER-USER chain",fw.docker_user_chain],["UFW detail",u.detail||"—"]];
  $("fw-kv").innerHTML=kv.map(([k,v])=>`<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("");
  $("ufw-rules").textContent=(u.rules&&u.rules.length)?u.rules.join("\n"):(u.detail||"No rules visible.");
  $("fw-pending").innerHTML=(f.pending||[]).length?`<h3 style="margin-top:12px">Pending commands (run with sudo)</h3><pre>${esc(f.pending.map(p=>p.commands.join("\n")).join("\n"))}</pre>`:"";
}
async function trustPort(port,proto){const r=await api("/api/ports/trust",{method:"POST",body:{port,proto}});r.ok?toast("Port "+port+" marked as known-good"):toast(r.error,1);runMonitor()}
async function runMonitor(){const r=await api("/api/monitor/run",{method:"POST",body:{}});r.ok?toast("Monitoring checks done"):toast(r.error,1);refresh()}
async function fwRefresh(){const r=await api("/api/firewall/refresh",{method:"POST",body:{}});r.ok?toast("Firewall backend: "+r.firewall.backend):toast(r.error,1);loadMon()}

/* ---------- HEALTH ---------- */
function res(title,pct,sub,warn,crit){pct=pct==null?null:+pct;const c=pct==null?"var(--dim)":pct>=crit?"var(--crit)":pct>=warn?"var(--warn)":"var(--ok)";
  return `<div class="card"><h3>${esc(title)}</h3><div class="big">${pct==null?"n/a":pct.toFixed(1)+"%"}</div><div class="bar"><i style="width:${pct||0}%;background:${c}"></i></div><div class="muted">${esc(sub)}</div></div>`}
async function loadHealth(){
  const r=await api("/api/health");const h=r.health||{};if(!h.services){$("h-res").innerHTML='<div class="muted">First monitoring cycle pending…</div>';return}
  const m=h.memory||{},d=h.disk||{},dk=h.docker||{};
  $("h-res").innerHTML=res("CPU",h.cpu,h.load?"load "+h.load.map(x=>x.toFixed(2)).join(" "):"",SETTINGS.cpu_warn||90,101)+
    res("Memory",m.percent,m.total?bytes(m.used)+" / "+bytes(m.total):"",SETTINGS.mem_warn||90,101)+
    res("Disk",d.percent,d.total?bytes(d.free)+" free on "+d.path:"",SETTINGS.disk_warn||90,SETTINGS.disk_crit||97)+
    `<div class="card"><h3>Docker</h3><div class="big ${dk.running?"ok":dk.installed?"bad":"muted"}">${dk.running?"RUNNING":dk.installed?"DOWN":"N/A"}</div><div class="muted">${esc(dk.detail||"")}</div></div>`;
  $("h-cards").innerHTML=Object.entries(h.services).map(([k,s])=>`<div class="card svc ${esc(s.status)}"><div style="display:flex;justify-content:space-between"><b>${esc(s.label)}</b><span class="st-${esc(s.status)}">● ${esc(s.status)}</span></div>
    <div class="muted">port ${esc(s.port)} · ${esc(s.detail)}</div>${s.bind&&s.bind.length?`<div class="muted">bind: ${esc(s.bind.join(", "))}</div>`:""}</div>`).join("");
  $("h-src").innerHTML=(r.sources||[]).map(s=>`<tr><td class="trunc" title="${esc(s.source)}">${esc(s.source)}</td><td>${esc(svcLabel(s.service))}</td><td>${esc(s.type)}</td>
    <td>${s.ok?`<span class="ok">ok</span> ${s.size!=null?bytes(s.size):""}${s.parsed!=null?" · "+s.parsed+" parsed":""}`:`<span class="bad">${esc(s.error||"error")}</span>`}</td></tr>`).join("")||'<tr><td colspan="4" class="muted">No log sources discovered yet. Managers write logs under ~/.phpmngr, ~/.db3ngin3, ~/.mailsrvr, ~/.staxmngr, ~/.nodemngr, ~/.killthehost.</td></tr>';
  $("h-host").innerHTML=[["Hostname",h.hostname],["Platform",h.platform],["Last check",h.checked_at?new Date(h.checked_at).toLocaleString()+" ("+h.duration+"s)":"—"],["Protected networks",(h.protected_networks||[]).join(", ")]]
    .map(([k,v])=>`<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`).join("");
  $("h-secret").innerHTML=(h.secret_files||[]).length?h.secret_files.map(f=>`<div class="wrn">${esc(f.file)} (${esc(f.mode)}): fix with <code>${esc(f.fix)}</code></div>`).join(""):'<div class="ok">All known manager secret files are private.</div>';
}

/* ---------- SETTINGS ---------- */
const SET_HELP={bind_host:"panel bind address (restart needed)",port:"panel port (restart needed)",retention_days:"access log / event retention",max_log_mb:"size cap for access_logs.jsonl",
 detection_interval:"seconds between detection cycles",monitor_interval:"seconds between monitoring cycles",detection_mode:"enforce | monitor",escalation_enabled:"double duration / permanent on repeat",
 firewall_enforcement:"apply bans via UFW/iptables when privileged",docker_logs:"tail manager container logs",backfill_kb:"KB of history read from newly discovered logs",
 cpu_warn:"%",mem_warn:"%",disk_warn:"%",disk_crit:"%",known_ports:"comma-separated ports / ranges / tcp:N",allowed_hosts:"extra Host headers accepted (comma-separated)",open_browser:"open browser on start"};
async function loadSettings(render){
  const r=await api("/api/settings");if(!r.ok)return;SETTINGS=r.settings;DEFAULTS=r.defaults;
  $("s-dir").textContent=r.data_dir;
  $("s-files").innerHTML=Object.entries(r.files).map(([n,f])=>`<tr><td>${esc(n)}</td><td>${bytes(f.size)}</td><td class="${f.mode==="0o600"?"ok":"wrn"}">${esc(f.mode)}</td></tr>`).join("");
  if(!render&&document.activeElement&&document.activeElement.closest&&document.activeElement.closest("#s-form"))return;
  $("s-form").innerHTML=Object.keys(DEFAULTS).map(k=>{const v=SETTINGS[k],d=DEFAULTS[k];let inp;
    if(typeof d==="boolean")inp=`<input type="checkbox" data-s="${k}" ${v?"checked":""}>`;
    else if(k==="detection_mode")inp=`<select data-s="${k}"><option ${v==="enforce"?"selected":""}>enforce</option><option ${v==="monitor"?"selected":""}>monitor</option></select>`;
    else if(Array.isArray(d))inp=`<textarea data-s="${k}" rows="2" style="width:100%">${esc((v||[]).join(", "))}</textarea>`;
    else inp=`<input data-s="${k}" ${typeof d==="number"?'type="number"':""} value="${esc(v)}" style="width:${typeof d==="number"?"110px":"200px"}">`;
    return `<tr><td>${esc(k)}<div class="muted" style="font-size:10px">${esc(SET_HELP[k]||"")}</div></td><td>${inp}</td></tr>`}).join("");
}
async function saveSettings(){
  const body={};document.querySelectorAll("#s-form [data-s]").forEach(el=>{const k=el.dataset.s,d=DEFAULTS[k];
    body[k]=el.type==="checkbox"?el.checked:Array.isArray(d)?el.value.split(",").map(x=>x.trim()).filter(Boolean):typeof d==="number"?+el.value:el.value.trim()});
  const r=await api("/api/settings",{method:"POST",body});
  if(r.ok){toast("Saved: "+(Object.keys(r.changed).join(", ")||"no changes"));$("s-note").textContent=r.note||""}else toast(r.error,1);
  document.activeElement.blur();loadSettings(true);
}

/* ---------- REFRESH LOOP ---------- */
let busy=false;
async function refresh(){
  if(busy)return;busy=true;
  try{
    const s=await loadStatus();
    if(TAB==="overview")await loadOverview(s);
    else if(TAB==="logs")await loadLogs();
    else if(TAB==="ips"){await loadBans();await loadIps()}
    else if(TAB==="rules")await loadRules();
    else if(TAB==="mon")await loadMon();
    else if(TAB==="health"){if(!DEFAULTS.port)await loadSettings();await loadHealth()}
    else if(TAB==="settings")await loadSettings();
  }catch(e){$("upd").textContent="connection lost: "+e.message}
  busy=false;
}
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeModal()});
if(location.host)$("tp-addr").textContent=location.host;
const h0=location.hash.slice(1);const nb=document.querySelector(`#nav button[data-t="${h0}"]`);
if(nb)nb.click();else refresh();
setInterval(refresh,30000);
</script></body></html>"""


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

class SecServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 64


def _open_browser_quiet(url):
    """Open the dashboard silently.
    Uses setsid+xdg-open so Flatpak/Snap browsers have no controlling terminal
    and cannot write Gtk warnings to /dev/tty. GTK_MODULES is removed entirely."""
    if sys.platform.startswith("linux"):
        xdg = shutil.which("xdg-open")
        if xdg:
            setsid_bin = shutil.which("setsid")
            cmd = ([setsid_bin, xdg, url] if setsid_bin else [xdg, url])
            env = {k: v for k, v in os.environ.items()
                   if k not in ("GTK_MODULES", "GDK_MODULES", "GTK2_MODULES")}
            if not env.get("DBUS_SESSION_BUS_ADDRESS"):
                bus = "/run/user/%d/bus" % os.getuid()
                if os.path.exists(bus):
                    env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + bus
            if not env.get("XDG_RUNTIME_DIR"):
                xdg_rt = "/run/user/%d" % os.getuid()
                if os.path.isdir(xdg_rt):
                    env["XDG_RUNTIME_DIR"] = xdg_rt
            try:
                subprocess.Popen(cmd, env=env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, close_fds=True)
                return
            except OSError:
                pass
    webbrowser.open(url)


def main():
    if sys.version_info < (3, 8):
        sys.exit("SEC-MNGR requires Python 3.8+")
    ap = argparse.ArgumentParser(description="SEC-MNGR — KillTheHost security manager & threat dashboard")
    ap.add_argument("--host", help="bind address (default 127.0.0.1, env SECMNGR_HOST)")
    ap.add_argument("--port", type=int, help="port (default 8080, env SECMNGR_PORT)")
    ap.add_argument("--no-browser", action="store_true", help="don't open the dashboard in a browser")
    args = ap.parse_args()

    ensure_data_dir()
    saved = load_json(SETTINGS_FILE, {})
    saved = saved if isinstance(saved, dict) else {}
    host = args.host or os.environ.get("SECMNGR_HOST") or saved.get("bind_host") or DEFAULT_HOST
    host = {"localhost": "127.0.0.1", "*": "0.0.0.0", "": DEFAULT_HOST}.get(host.strip().lower(), host.strip())
    try:
        port = int(args.port or os.environ.get("SECMNGR_PORT") or saved.get("port") or DEFAULT_PORT)
        ipaddress.ip_address(host)
        if not 1 <= port <= 65535:
            raise ValueError("port out of range")
    except ValueError as exc:
        sys.exit("FATAL: invalid bind address/port: %s" % exc)

    global CORE
    CORE = SecurityCore(host, port)
    try:
        if ":" in host:
            SecServer.address_family = socket.AF_INET6
        httpd = SecServer((host, port), Handler)
    except OSError as exc:
        print("FATAL: cannot bind %s:%d — %s" % (host, port, exc), file=sys.stderr, flush=True)
        sys.exit(1)

    CORE.start()
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else ("[%s]" % host if ":" in host else host)
    url = "http://%s:%d" % (shown, port)
    print("=" * 62, flush=True)
    print("  SEC-MNGR v%s — Security Manager & Threat Dashboard" % VERSION)
    print("  Dashboard : %s" % url)
    print("  Data dir  : %s (0700)" % DATA_DIR)
    print("  Firewall  : %s" % CORE.fw.public_caps().get("backend"))
    print("  Press Ctrl+C to stop")
    print("=" * 62, flush=True)

    if not args.no_browser and os.environ.get("SECMNGR_NO_BROWSER") != "1" and CORE.cfg.get("open_browser"):
        threading.Timer(1.0, lambda: _open_browser_quiet(url)).start()

    def _term(signum, frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _term)
    except (ValueError, AttributeError, OSError):
        pass

    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[SEC-MNGR] shutting down…", flush=True)
        CORE.events.emit("INFO", "system", "SEC-MNGR stopped")
        CORE.shutdown()
        httpd.server_close()


if __name__ == "__main__":
    main()
