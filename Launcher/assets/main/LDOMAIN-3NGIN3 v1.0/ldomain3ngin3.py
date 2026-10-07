#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════╗
║     KillTheHost  —  LDOMAIN-3NGIN3  v1.0         ║
║     Local Domain & Reverse Proxy Manager         ║
║                                                  ║
║  • Local domain registry (hostname → IP:port)    ║
║  • Built-in reverse proxy (HTTP/HTTPS, WS)       ║
║  • LAN DNS resolver (UDP 5353 / optional 53)     ║
║  • Hosts-file adapter (single-machine resolution)║
║  • Local CA + per-domain TLS certificates        ║
║  • SEC-MNGR integration (ban enforcement + logs) ║
║  • One-click integration from other managers     ║
║  • Per-domain health checks                      ║
║                                                  ║
║  Dashboard : http://127.0.0.1:8181               ║
║  Proxy HTTP: :80  (falls back to :8180 if no     ║
║              root; iptables redirect available)  ║
║  Proxy HTTPS: :443 (falls back to :8143)         ║
║  DNS (LAN) : UDP :5353 (redirect 53→5353 shown)  ║
║                                                  ║
║  Zero external dependencies — Python 3.8+ only.  ║
╚══════════════════════════════════════════════════╝

Data lives in ~/.ldomain3ngin3/ (0700). Files are 0600.

Command line:
  python3 ldomain3ngin3.py
  python3 ldomain3ngin3.py --host 127.0.0.1 --port 8181 --no-browser
  python3 ldomain3ngin3.py --no-proxy --no-dns

Environment:
  LDOMAIN_HOST, LDOMAIN_PORT, LDOMAIN_NO_BROWSER=1
  LDOMAIN_NO_PROXY=1, LDOMAIN_NO_DNS=1
"""

import argparse
import ipaddress
import json
import os
import platform
import re
import select
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from collections import deque
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

# ─────────────────────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────────────────────

VERSION      = "1.0"
SYSTEM       = platform.system()
HOME         = Path.home()
DATA_DIR     = HOME / ".ldomain3ngin3"

REGISTRY_FILE   = DATA_DIR / "registry.json"
CA_DIR          = DATA_DIR / "ca"
CERTS_DIR       = DATA_DIR / "certs"
ACCESS_LOG_FILE = DATA_DIR / "access_logs.jsonl"
CONFIG_FILE     = DATA_DIR / "config.json"
HEALTH_FILE     = DATA_DIR / "health.json"

# SEC-MNGR integration paths
SEC_DATA_DIR    = HOME / ".secmngr"
SEC_BLOCKLIST   = SEC_DATA_DIR / "blocklist.txt"

DEFAULT_UI_PORT      = 8181
DEFAULT_PROXY_HTTP   = 80        # falls back to 8180
DEFAULT_PROXY_HTTPS  = 443       # falls back to 8143
FALLBACK_PROXY_HTTP  = 8180
FALLBACK_PROXY_HTTPS = 8143
DEFAULT_DNS_PORT     = 5353

DEFAULT_SUFFIX = ".internal"
SUPPORTED_SUFFIXES = [".internal", ".home.arpa", ".local.dev", ".lan", ".test"]

MAX_BODY     = 1024 * 1024   # 1 MiB
LOG_RING     = 5000
HEALTH_INTERVAL = 30         # seconds between health checks
BLOCKLIST_RELOAD = 30        # seconds between blocklist reloads

DEFAULT_CONFIG = {
    "suffix"        : DEFAULT_SUFFIX,
    "proxy_http"    : DEFAULT_PROXY_HTTP,
    "proxy_https"   : DEFAULT_PROXY_HTTPS,
    "dns_port"      : DEFAULT_DNS_PORT,
    "ui_port"       : DEFAULT_UI_PORT,
    "ui_host"       : "127.0.0.1",
    "open_browser"  : True,
    "health_interval": HEALTH_INTERVAL,
    "default_timeout": 30,
    "ca_common_name": "KillTheHost Local CA",
    "ca_org"        : "KillTheHost",
    "hosts_file_managed": False,   # write /etc/hosts entries
    "dns_enabled"   : True,
    "proxy_enabled" : True,
    "log_access"    : True,
    "sec_enforce"   : True,        # enforce SEC-MNGR bans at proxy layer
}

# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

_io_lock = threading.RLock()


def now() -> float:
    return time.time()


def iso(ts: float = None) -> str:
    t = ts if ts is not None else now()
    return datetime.fromtimestamp(t, tz=timezone.utc).isoformat(timespec="seconds")


def ensure_data_dir():
    for d in (DATA_DIR, CA_DIR, CERTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass


def _open_secure(path: Path, flags: int):
    fd = os.open(str(path), flags | os.O_CREAT, 0o600)
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass
    return fd


def atomic_write_json(path: Path, data):
    tmp = path.with_suffix(".tmp")
    try:
        fd = _open_secure(tmp, os.O_WRONLY | os.O_TRUNC)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(str(tmp), str(path))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def append_jsonl(path: Path, entries: list):
    with _io_lock:
        fd = _open_secure(path, os.O_WRONLY | os.O_APPEND)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")


def load_json(path: Path, default=None):
    try:
        if path.exists():
            return json.loads(path.read_text("utf-8"))
    except Exception:
        pass
    return default if default is not None else {}


def try_bind(port: int, host: str = "0.0.0.0") -> bool:
    """Return True if we can bind to this port."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.close()
        return True
    except OSError:
        return False


def resolve_proxy_ports(cfg: dict) -> tuple:
    """Return (http_port, https_port) that we'll actually bind."""
    hp = int(cfg.get("proxy_http", DEFAULT_PROXY_HTTP))
    sp = int(cfg.get("proxy_https", DEFAULT_PROXY_HTTPS))
    if not try_bind(hp):
        hp = FALLBACK_PROXY_HTTP
    if not try_bind(sp):
        sp = FALLBACK_PROXY_HTTPS
    return hp, sp


def run_cmd(cmd: list, timeout: int = 15) -> tuple:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return -1, "", str(exc)


def openssl_available() -> bool:
    rc, _, _ = run_cmd(["openssl", "version"])
    return rc == 0


# ─────────────────────────────────────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

class Config:
    def __init__(self):
        self._data = dict(DEFAULT_CONFIG)
        self._lock = threading.Lock()
        self._load()

    def _load(self):
        saved = load_json(CONFIG_FILE, {})
        with self._lock:
            self._data.update(saved)

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key, value):
        with self._lock:
            self._data[key] = value
        atomic_write_json(CONFIG_FILE, self._data)

    def update(self, d: dict):
        with self._lock:
            self._data.update(d)
        atomic_write_json(CONFIG_FILE, self._data)

    def all(self) -> dict:
        with self._lock:
            return dict(self._data)


# ─────────────────────────────────────────────────────────────────────────────
#  DOMAIN REGISTRY
# ─────────────────────────────────────────────────────────────────────────────

class Registry:
    """Thread-safe local domain registry stored in registry.json."""

    REQUIRED = ("hostname", "target_host", "target_port")

    def __init__(self):
        self._lock  = threading.RLock()
        self._domains: dict = {}   # hostname → entry
        self._load()

    def _load(self):
        data = load_json(REGISTRY_FILE, {})
        with self._lock:
            self._domains = data if isinstance(data, dict) else {}

    def _save(self):
        atomic_write_json(REGISTRY_FILE, self._domains)

    # ── public ──

    def list(self) -> list:
        with self._lock:
            return list(self._domains.values())

    def get(self, hostname: str) -> dict:
        with self._lock:
            return self._domains.get(hostname.lower().strip())

    def lookup(self, hostname: str) -> dict:
        """Case-insensitive lookup; strip suffix variants."""
        h = hostname.lower().strip().rstrip(".")
        with self._lock:
            d = self._domains.get(h)
            if d:
                return d
            # Try stripping port from Host header (hostname:port)
            bare = h.split(":")[0]
            return self._domains.get(bare)

    def add(self, entry: dict) -> tuple:
        """Validate and add / update an entry. Returns (ok, entry_or_error)."""
        for f in self.REQUIRED:
            if not entry.get(f):
                return False, f"Missing required field: {f}"
        hostname = entry["hostname"].lower().strip()
        try:
            port = int(entry["target_port"])
            if not (1 <= port <= 65535):
                raise ValueError
        except (ValueError, TypeError):
            return False, "target_port must be 1–65535"
        host = entry["target_host"].strip() or "127.0.0.1"
        now_ts = round(now(), 3)
        rec = {
            "id"           : entry.get("id") or uuid.uuid4().hex[:12],
            "hostname"     : hostname,
            "target_host"  : host,
            "target_port"  : port,
            "label"        : entry.get("label", hostname),
            "description"  : entry.get("description", ""),
            "tags"         : entry.get("tags", []),
            "enabled"      : bool(entry.get("enabled", True)),
            "created_at"   : entry.get("created_at", iso(now_ts)),
            "updated_at"   : iso(now_ts),
            "health"       : entry.get("health", "unknown"),
            "last_checked" : entry.get("last_checked"),
            "latency_ms"   : entry.get("latency_ms"),
            "tls_enabled"  : bool(entry.get("tls_enabled", False)),
            "timeout"      : int(entry.get("timeout", 30)),
            "extra_headers": entry.get("extra_headers", {}),
            "source"       : entry.get("source", "manual"),
        }
        with self._lock:
            existing = self._domains.get(hostname)
            if existing:
                rec["created_at"] = existing["created_at"]
                rec["id"]         = existing["id"]
            self._domains[hostname] = rec
        self._save()
        return True, rec

    def disable(self, hostname: str) -> tuple:
        with self._lock:
            d = self._domains.get(hostname.lower())
            if not d:
                return False, "not found"
            d["enabled"] = False
            d["updated_at"] = iso(now())
        self._save()
        return True, d

    def delete(self, hostname: str) -> bool:
        with self._lock:
            if hostname.lower() not in self._domains:
                return False
            del self._domains[hostname.lower()]
        self._save()
        return True

    def update_health(self, hostname: str, health: str, latency_ms=None):
        with self._lock:
            d = self._domains.get(hostname.lower())
            if not d:
                return
            d["health"] = health
            d["last_checked"] = iso(now())
            if latency_ms is not None:
                d["latency_ms"] = round(latency_ms, 1)
        self._save()


# ─────────────────────────────────────────────────────────────────────────────
#  SEC-MNGR BLOCKLIST ENFORCER
# ─────────────────────────────────────────────────────────────────────────────

class BlocklistEnforcer:
    """Reads ~/.secmngr/blocklist.txt and enforces bans at the proxy layer."""

    def __init__(self):
        self._lock  = threading.RLock()
        self._nets  = []         # list of ipaddress networks
        self._mtime = 0.0
        self._last_reload = 0.0

    def reload(self):
        if not SEC_BLOCKLIST.exists():
            with self._lock:
                self._nets = []
            return
        try:
            mtime = SEC_BLOCKLIST.stat().st_mtime
            if mtime <= self._mtime:
                return
            nets = []
            for line in SEC_BLOCKLIST.read_text("utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    nets.append(ipaddress.ip_network(line, strict=False))
                except ValueError:
                    pass
            with self._lock:
                self._nets  = nets
                self._mtime = mtime
        except Exception:
            pass

    def is_banned(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        with self._lock:
            return any(addr in n for n in self._nets if n.version == addr.version)

    def ban_count(self) -> int:
        with self._lock:
            return len(self._nets)


# ─────────────────────────────────────────────────────────────────────────────
#  ACCESS LOG WRITER  (SEC-MNGR unified format)
# ─────────────────────────────────────────────────────────────────────────────

_log_buffer: deque = deque(maxlen=LOG_RING)
_log_lock = threading.Lock()


def _mk_entry(ip, method, path, status, ua, event_type, detail=""):
    ts = round(now(), 3)
    return {
        "ts"        : ts,
        "timestamp" : iso(ts),
        "ip"        : ip or "unknown",
        "method"    : method or "",
        "path"      : (path or "")[:512],
        "status"    : status,
        "user_agent": (ua or "")[:300],
        "service"   : "ldomain3ngin3",
        "event_type": event_type,
        "source"    : "access_log",
        "detail"    : (detail or "")[:300],
    }


def log_access(ip, method, path, status, ua, event_type="request", detail=""):
    entry = _mk_entry(ip, method, path, status, ua, event_type, detail)
    with _log_lock:
        _log_buffer.append(entry)
    try:
        append_jsonl(ACCESS_LOG_FILE, [entry])
    except Exception:
        pass


def recent_logs(n=200) -> list:
    with _log_lock:
        return list(_log_buffer)[-n:]


# ─────────────────────────────────────────────────────────────────────────────
#  LOCAL CA  (openssl subprocess — zero pip installs)
# ─────────────────────────────────────────────────────────────────────────────

CA_KEY  = CA_DIR / "ca.key"
CA_CERT = CA_DIR / "ca.crt"


class LocalCA:
    def __init__(self, cfg: Config):
        self._cfg  = cfg
        self._lock = threading.Lock()

    def exists(self) -> bool:
        return CA_KEY.exists() and CA_CERT.exists()

    def generate(self) -> tuple:
        """Generate the local root CA. Returns (ok, message)."""
        if not openssl_available():
            return False, "openssl not found in PATH — install it (apt install openssl)"
        cn  = self._cfg.get("ca_common_name", "KillTheHost Local CA")
        org = self._cfg.get("ca_org", "KillTheHost")
        with self._lock:
            CA_DIR.mkdir(parents=True, exist_ok=True)
            # Generate key
            rc, _, err = run_cmd(["openssl", "genrsa", "-out", str(CA_KEY), "4096"], 30)
            if rc != 0:
                return False, f"genrsa failed: {err}"
            try:
                os.chmod(CA_KEY, 0o600)
            except OSError:
                pass
            # Self-signed cert — valid 10 years
            subj = f"/CN={cn}/O={org}/OU=Local Development CA"
            rc, _, err = run_cmd([
                "openssl", "req", "-x509", "-new", "-nodes",
                "-key", str(CA_KEY), "-sha256", "-days", "3650",
                "-subj", subj,
                "-out", str(CA_CERT),
            ], 30)
            if rc != 0:
                return False, f"req -x509 failed: {err}"
        return True, "Root CA generated successfully."

    def issue_cert(self, hostname: str) -> tuple:
        """Issue a domain cert signed by the local CA. Returns (ok, message)."""
        if not self.exists():
            return False, "Root CA does not exist — generate it first."
        if not openssl_available():
            return False, "openssl not found in PATH"

        cert_dir = CERTS_DIR / hostname
        cert_dir.mkdir(parents=True, exist_ok=True)
        key_file  = cert_dir / "server.key"
        csr_file  = cert_dir / "server.csr"
        cert_file = cert_dir / "server.crt"
        ext_file  = cert_dir / "v3.ext"

        ext_content = (
            "authorityKeyIdentifier=keyid,issuer\n"
            "basicConstraints=CA:FALSE\n"
            "keyUsage = digitalSignature, nonRepudiation, keyEncipherment, dataEncipherment\n"
            "subjectAltName = @alt_names\n"
            "[alt_names]\n"
            f"DNS.1 = {hostname}\n"
            f"DNS.2 = *.{hostname}\n"
        )
        ext_file.write_text(ext_content)

        with self._lock:
            # Key
            rc, _, err = run_cmd(["openssl", "genrsa", "-out", str(key_file), "2048"], 20)
            if rc != 0:
                return False, f"genrsa failed: {err}"
            try:
                os.chmod(key_file, 0o600)
            except OSError:
                pass
            # CSR
            rc, _, err = run_cmd([
                "openssl", "req", "-new", "-key", str(key_file),
                "-subj", f"/CN={hostname}",
                "-out", str(csr_file),
            ], 20)
            if rc != 0:
                return False, f"csr failed: {err}"
            # Sign
            rc, _, err = run_cmd([
                "openssl", "x509", "-req",
                "-in", str(csr_file),
                "-CA", str(CA_CERT), "-CAkey", str(CA_KEY),
                "-CAcreateserial",
                "-out", str(cert_file),
                "-days", "825",
                "-sha256", "-extfile", str(ext_file),
            ], 20)
            if rc != 0:
                return False, f"sign failed: {err}"

        return True, f"Certificate issued for {hostname}"

    def cert_files(self, hostname: str) -> tuple:
        """Return (key_path, cert_path) or (None, None)."""
        cert_dir = CERTS_DIR / hostname
        k = cert_dir / "server.key"
        c = cert_dir / "server.crt"
        if k.exists() and c.exists():
            return k, c
        return None, None

    def ca_pem(self) -> str:
        return CA_CERT.read_text("utf-8") if CA_CERT.exists() else ""

    def cert_info(self, hostname: str) -> dict:
        _, cert = self.cert_files(hostname)
        if not cert:
            return {"exists": False}
        rc, out, _ = run_cmd(["openssl", "x509", "-noout", "-dates", "-subject",
                               "-issuer", "-in", str(cert)], 10)
        if rc != 0:
            return {"exists": True, "valid": False}
        info = {"exists": True, "valid": True}
        for line in out.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                info[k.strip().lower()] = v.strip()
        return info

    def install_instructions(self) -> str:
        if not self.exists():
            return "Generate the root CA first."
        ca_path = str(CA_CERT)
        return (
            f"CA certificate: {ca_path}\n\n"
            "── Linux (system-wide) ──────────────────────────\n"
            f"  sudo cp {ca_path} /usr/local/share/ca-certificates/killthehost-ca.crt\n"
            "  sudo update-ca-certificates\n\n"
            "── macOS ────────────────────────────────────────\n"
            f"  sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain {ca_path}\n\n"
            "── Windows (run as Administrator) ───────────────\n"
            f'  certutil -addstore -f "Root" "{ca_path}"\n\n'
            "── Firefox (any OS) ─────────────────────────────\n"
            "  Settings → Privacy & Security → Certificates → View Certificates\n"
            "  → Authorities → Import → select ca.crt → Trust for websites\n\n"
            "── Android ──────────────────────────────────────\n"
            "  Settings → Security → Install from storage → select ca.crt\n\n"
            "── iOS / iPadOS ─────────────────────────────────\n"
            "  Send ca.crt to device → Settings → Profile Downloaded\n"
            "  → Install → Settings → General → About → Certificate Trust Settings → enable\n"
        )


# ─────────────────────────────────────────────────────────────────────────────
#  HOSTS FILE ADAPTER
# ─────────────────────────────────────────────────────────────────────────────

HOSTS_MARKER_START = "# BEGIN ldomain3ngin3"
HOSTS_MARKER_END   = "# END ldomain3ngin3"


def hosts_file_path() -> Path:
    return Path(r"C:\Windows\System32\drivers\etc\hosts") if SYSTEM == "Windows" \
           else Path("/etc/hosts")


def _read_hosts() -> str:
    try:
        return hosts_file_path().read_text("utf-8")
    except Exception:
        return ""


def _strip_managed_block(content: str) -> str:
    lines = content.splitlines(keepends=True)
    out, inside = [], False
    for ln in lines:
        if HOSTS_MARKER_START in ln:
            inside = True
        if not inside:
            out.append(ln)
        if HOSTS_MARKER_END in ln:
            inside = False
    return "".join(out)


def write_hosts_entries(domains: list, dry_run: bool = False) -> tuple:
    """
    Write a managed block to /etc/hosts.
    Returns (ok, message_or_diff).
    """
    enabled = [d for d in domains if d.get("enabled")]
    block_lines = [HOSTS_MARKER_START + " — managed by KillTheHost LDOMAIN-3NGIN3\n"]
    for d in enabled:
        block_lines.append(f"{d['target_host']}\t{d['hostname']}\n")
    block_lines.append(HOSTS_MARKER_END + "\n")
    block = "".join(block_lines)

    old = _read_hosts()
    new_content = _strip_managed_block(old)
    if not new_content.endswith("\n"):
        new_content += "\n"
    new_content += block

    if dry_run:
        return True, block

    try:
        hp = hosts_file_path()
        hp.write_text(new_content, "utf-8")
        return True, f"Wrote {len(enabled)} entries to {hp}"
    except PermissionError:
        # Suggest sudo
        tmp = DATA_DIR / "hosts-block.txt"
        tmp.write_text(block, "utf-8")
        return False, (
            f"Permission denied writing to {hosts_file_path()}.\n"
            f"Run:  sudo python3 -c \""
            f"import pathlib; h=pathlib.Path('{hosts_file_path()}');"
            f" t=pathlib.Path('{tmp}'); "
            f"old=h.read_text(); "
            f"h.write_text(old.rstrip()+'\\n'+t.read_text())\""
        )
    except Exception as exc:
        return False, str(exc)


def remove_hosts_entries() -> tuple:
    old = _read_hosts()
    new_content = _strip_managed_block(old)
    try:
        hosts_file_path().write_text(new_content, "utf-8")
        return True, "Removed managed block from hosts file."
    except Exception as exc:
        return False, str(exc)


# ─────────────────────────────────────────────────────────────────────────────
#  UDP DNS SERVER  (responds to A queries for registered domains)
# ─────────────────────────────────────────────────────────────────────────────

def _dns_name_to_labels(name: str) -> bytes:
    parts = name.rstrip(".").split(".")
    out = b""
    for p in parts:
        enc = p.encode("ascii")
        out += bytes([len(enc)]) + enc
    return out + b"\x00"


def _parse_dns_name(data: bytes, offset: int) -> tuple:
    """Parse a DNS name at offset, return (name_str, new_offset). Handles compression."""
    labels = []
    while True:
        if offset >= len(data):
            break
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if (length & 0xC0) == 0xC0:
            # Pointer
            if offset + 1 >= len(data):
                break
            ptr = ((length & 0x3F) << 8) | data[offset + 1]
            label, _ = _parse_dns_name(data, ptr)
            labels.append(label)
            offset += 2
            break
        offset += 1
        labels.append(data[offset:offset + length].decode("ascii", errors="replace"))
        offset += length
    return ".".join(labels), offset


def _build_dns_response(query: bytes, ip: str) -> bytes:
    if len(query) < 12:
        return b""
    txid = query[:2]
    # Parse question
    offset = 12
    qname, offset = _parse_dns_name(query, offset)
    if offset + 4 > len(query):
        return b""
    qtype  = struct.unpack("!H", query[offset:offset+2])[0]
    qclass = struct.unpack("!H", query[offset+2:offset+4])[0]
    offset += 4

    # We only answer A (type 1) queries for IN (class 1)
    if qtype != 1 or qclass != 1:
        # Return NOERROR with no answers for non-A queries
        flags = b"\x81\x00"  # QR=1, AA=1, OPCODE=0, RA=0
        return txid + flags + b"\x00\x01\x00\x00\x00\x00\x00\x00" + query[12:offset]

    try:
        ip_bytes = socket.inet_aton(ip)
    except (OSError, socket.error):
        # NXDOMAIN
        flags = b"\x81\x83"
        return txid + flags + b"\x00\x01\x00\x00\x00\x00\x00\x00" + query[12:offset]

    # Build response
    flags    = b"\x81\x80"   # QR=1 AA=0 TC=0 RD=1 RA=1 RCODE=0
    qdcount  = b"\x00\x01"
    ancount  = b"\x00\x01"
    nscount  = b"\x00\x00"
    arcount  = b"\x00\x00"
    header   = txid + flags + qdcount + ancount + nscount + arcount
    question = query[12:offset]
    # Answer: name pointer back to offset 12, type A, class IN, TTL 60, rdlength 4, rdata
    answer   = (b"\xc0\x0c" +         # pointer to question name
                b"\x00\x01" +         # type A
                b"\x00\x01" +         # class IN
                b"\x00\x00\x00\x3c" + # TTL 60
                b"\x00\x04" +         # rdlength
                ip_bytes)
    return header + question + answer


class DNSServer:
    def __init__(self, registry: "Registry", cfg: "Config"):
        self._reg    = registry
        self._cfg    = cfg
        self._sock   = None
        self._thread = None
        self._stop   = threading.Event()

    def start(self, port: int) -> bool:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("0.0.0.0", port))
            self._sock.settimeout(1.0)
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            print(f"  [LDOMAIN] DNS resolver started on UDP :{port}", flush=True)
            return True
        except OSError as exc:
            print(f"  [LDOMAIN] DNS: cannot bind UDP :{port}: {exc}", flush=True)
            return False

    def stop(self):
        self._stop.set()
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass

    def _run(self):
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(512)
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._handle, args=(data, addr), daemon=True).start()

    def _handle(self, data: bytes, addr: tuple):
        try:
            if len(data) < 12:
                return
            # Parse question name
            offset = 12
            qname, new_offset = _parse_dns_name(data, offset)
            if new_offset + 4 > len(data):
                return
            qname_lower = qname.rstrip(".").lower()
            # Look up in registry
            entry = self._reg.lookup(qname_lower)
            if entry and entry.get("enabled"):
                ip = entry["target_host"]
                # If target host is not an IP, resolve it
                try:
                    ipaddress.ip_address(ip)
                except ValueError:
                    try:
                        ip = socket.gethostbyname(ip)
                    except Exception:
                        ip = "127.0.0.1"
                resp = _build_dns_response(data, ip)
            else:
                # NXDOMAIN — send proper response
                resp = _build_dns_response(data, "")  # empty IP → NXDOMAIN path
            if resp:
                self._sock.sendto(resp, addr)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
#  REVERSE PROXY
# ─────────────────────────────────────────────────────────────────────────────

PROXY_BUFSIZE = 65536
WS_MAGIC      = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _recv_all_headers(sock, timeout=10) -> bytes:
    sock.settimeout(timeout)
    buf = b""
    while b"\r\n\r\n" not in buf:
        try:
            chunk = sock.recv(4096)
        except (socket.timeout, OSError):
            break
        if not chunk:
            break
        buf += chunk
        if len(buf) > 65536:
            break
    return buf


def _send_all(sock, data: bytes):
    total = 0
    while total < len(data):
        try:
            sent = sock.send(data[total:])
            if sent == 0:
                break
            total += sent
        except (OSError, BrokenPipeError):
            break


def _tunnel_bidirectional(s1, s2, timeout=3600):
    """Tunnel two sockets bidirectionally (WebSocket / CONNECT)."""
    s1.settimeout(30)
    s2.settimeout(30)
    socks = [s1, s2]
    while True:
        try:
            r, _, _ = select.select(socks, [], socks, 30)
        except Exception:
            break
        if not r:
            continue
        for s in r:
            other = s2 if s is s1 else s1
            try:
                data = s.recv(PROXY_BUFSIZE)
                if not data:
                    return
                _send_all(other, data)
            except (OSError, BrokenPipeError):
                return


def _parse_request_line(raw: bytes) -> tuple:
    """Return (method, path, version, headers_dict, body_start) or None."""
    header_end = raw.find(b"\r\n\r\n")
    if header_end == -1:
        return None
    header_part = raw[:header_end].decode("latin-1", errors="replace")
    lines = header_part.split("\r\n")
    if not lines:
        return None
    parts = lines[0].split(" ", 2)
    if len(parts) < 2:
        return None
    method  = parts[0].upper()
    path    = parts[1] if len(parts) > 1 else "/"
    version = parts[2] if len(parts) > 2 else "HTTP/1.1"
    headers = {}
    for line in lines[1:]:
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip().lower()] = v.strip()
    return method, path, version, headers, header_end + 4


def _forward_request(client_conn, client_ip, entry: dict, raw: bytes,
                     method: str, path: str, version: str, headers: dict, body_offset: int,
                     enforcer: "BlocklistEnforcer", cfg: "Config"):
    """Forward one HTTP request to backend; handle WebSocket upgrades."""
    target_host = entry["target_host"]
    target_port = int(entry["target_port"])
    timeout     = int(entry.get("timeout", cfg.get("default_timeout", 30)))
    extra_hdrs  = entry.get("extra_headers", {})

    # WebSocket upgrade
    is_ws = headers.get("upgrade", "").lower() == "websocket"

    try:
        backend = socket.create_connection((target_host, target_port), timeout=min(timeout, 10))
    except (OSError, socket.timeout) as exc:
        err = f"HTTP/1.1 502 Bad Gateway\r\nContent-Type: text/plain\r\n\r\nUpstream error: {exc}"
        _send_all(client_conn, err.encode())
        return 502

    # Rebuild request with updated headers
    out_headers = {}
    hop_by_hop = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
                  "te", "trailers", "transfer-encoding", "upgrade"}
    for k, v in headers.items():
        if k not in hop_by_hop:
            out_headers[k] = v
    out_headers["host"]            = f"{target_host}:{target_port}"
    out_headers["x-forwarded-for"] = client_ip
    out_headers["x-forwarded-host"]= headers.get("host", "")
    out_headers["x-real-ip"]       = client_ip
    # Extra domain-level headers
    for k, v in extra_hdrs.items():
        out_headers[k.lower()] = v
    if is_ws:
        out_headers["connection"] = "Upgrade"
        out_headers["upgrade"]    = "websocket"
    else:
        out_headers["connection"] = "close"

    req_line = f"{method} {path} HTTP/1.1\r\n"
    req_hdrs = "".join(f"{k}: {v}\r\n" for k, v in out_headers.items())
    req_head = (req_line + req_hdrs + "\r\n").encode("latin-1")

    _send_all(backend, req_head)
    # Stream any body
    body = raw[body_offset:]
    if body:
        _send_all(backend, body)

    if is_ws:
        # Tunnel websocket connection
        _tunnel_bidirectional(client_conn, backend)
        backend.close()
        return 101

    # Stream response back to client
    status_code = 502
    try:
        resp_buf = b""
        backend.settimeout(timeout)
        while True:
            chunk = backend.recv(PROXY_BUFSIZE)
            if not chunk:
                break
            if not resp_buf:
                resp_buf = chunk
                # Extract status code from first line
                first_crlf = resp_buf.find(b"\r\n")
                if first_crlf != -1:
                    first_line = resp_buf[:first_crlf].decode("latin-1", errors="replace")
                    parts = first_line.split(" ", 2)
                    if len(parts) >= 2:
                        try:
                            status_code = int(parts[1])
                        except ValueError:
                            pass
                _send_all(client_conn, resp_buf)
                resp_buf = b""
            else:
                _send_all(client_conn, chunk)
    except (socket.timeout, OSError):
        pass
    finally:
        backend.close()
    return status_code


class ReverseProxy:
    """TCP-level reverse proxy for HTTP and HTTPS."""

    def __init__(self, registry: "Registry", enforcer: "BlocklistEnforcer",
                 cfg: "Config", local_ca: "LocalCA"):
        self._reg      = registry
        self._enforcer = enforcer
        self._cfg      = cfg
        self._ca       = local_ca
        self._servers  = []
        self._threads  = []
        self._stop     = threading.Event()
        self.http_port  = None
        self.https_port = None

    def start(self) -> tuple:
        hp, sp = resolve_proxy_ports(self._cfg)
        ok_http  = self._start_listener(hp, tls=False)
        ok_https = self._start_listener(sp, tls=True)
        if ok_http:
            self.http_port = hp
        if ok_https:
            self.https_port = sp
        return ok_http, ok_https

    def stop(self):
        self._stop.set()
        for s in self._servers:
            try:
                s.close()
            except OSError:
                pass

    def _start_listener(self, port: int, tls: bool) -> bool:
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", port))
            srv.listen(128)
            srv.settimeout(1.0)
            self._servers.append(srv)
            label = "HTTPS" if tls else "HTTP"
            t = threading.Thread(target=self._accept_loop, args=(srv, tls), daemon=True)
            t.start()
            self._threads.append(t)
            print(f"  [LDOMAIN] Proxy {label} started on :{port}", flush=True)
            return True
        except OSError as exc:
            label = "HTTPS" if tls else "HTTP"
            print(f"  [LDOMAIN] Proxy {label}: cannot bind :{port}: {exc}", flush=True)
            return False

    def _accept_loop(self, srv: socket.socket, tls: bool):
        while not self._stop.is_set():
            try:
                conn, addr = srv.accept()
            except (socket.timeout, OSError):
                continue
            t = threading.Thread(target=self._handle, args=(conn, addr, tls), daemon=True)
            t.start()

    def _handle(self, conn: socket.socket, addr: tuple, tls: bool):
        client_ip = addr[0]
        ua, method, path, status = "", "GET", "/", 0
        try:
            if self._cfg.get("sec_enforce", True) and self._enforcer.is_banned(client_ip):
                resp = b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\n\r\nBlocked by SEC-MNGR\n"
                _send_all(conn, resp)
                log_access(client_ip, "GET", "/", 403, "", "blocked", "banned by SEC-MNGR")
                return

            raw = _recv_all_headers(conn)
            if not raw:
                return

            parsed = _parse_request_line(raw)
            if not parsed:
                log_access(client_ip, "", "/", 400, "", "malformed")
                return
            method, path, version, headers, body_offset = parsed
            ua = headers.get("user-agent", "")
            host_hdr = headers.get("host", "").split(":")[0]

            entry = self._reg.lookup(host_hdr)
            if not entry or not entry.get("enabled"):
                resp = b"HTTP/1.1 404 Not Found\r\nContent-Type: text/plain\r\n\r\nNo domain registered.\n"
                _send_all(conn, resp)
                log_access(client_ip, method, path, 404, ua, "not_found",
                           f"unknown host: {host_hdr}")
                return

            if tls:
                key, cert = self._ca.cert_files(entry["hostname"])
                if key and cert:
                    try:
                        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                        ctx.load_cert_chain(str(cert), str(key))
                        conn = ctx.wrap_socket(conn, server_side=True)
                        # Re-read after TLS handshake
                        raw = _recv_all_headers(conn)
                        if not raw:
                            return
                        parsed = _parse_request_line(raw)
                        if not parsed:
                            return
                        method, path, version, headers, body_offset = parsed
                        host_hdr = headers.get("host", "").split(":")[0]
                        entry = self._reg.lookup(host_hdr) or entry
                    except ssl.SSLError:
                        return

            status = _forward_request(
                conn, client_ip, entry, raw,
                method, path, version, headers, body_offset,
                self._enforcer, self._cfg
            )
            log_access(client_ip, method, path, status, ua, "request",
                       f"→ {entry['hostname']}")
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


# ─────────────────────────────────────────────────────────────────────────────
#  HEALTH CHECKER
# ─────────────────────────────────────────────────────────────────────────────

class HealthChecker:
    def __init__(self, registry: "Registry", cfg: "Config", local_ca: "LocalCA"):
        self._reg    = registry
        self._cfg    = cfg
        self._ca     = local_ca
        self._stop   = threading.Event()
        self._thread = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            self._check_all()
            self._stop.wait(int(self._cfg.get("health_interval", HEALTH_INTERVAL)))

    def _check_all(self):
        for entry in self._reg.list():
            if not entry.get("enabled"):
                continue
            try:
                self._check_one(entry)
            except Exception:
                pass

    def _check_one(self, entry: dict):
        hostname = entry["hostname"]
        host     = entry["target_host"]
        port     = int(entry["target_port"])
        timeout  = min(int(entry.get("timeout", 10)), 10)
        t0 = now()
        try:
            s = socket.create_connection((host, port), timeout)
            s.close()
            latency = (now() - t0) * 1000
            self._reg.update_health(hostname, "up", latency)
        except (socket.timeout, ConnectionRefusedError, OSError):
            self._reg.update_health(hostname, "down")

    def check_one_now(self, hostname: str) -> dict:
        entry = self._reg.get(hostname)
        if not entry:
            return {"health": "unknown", "error": "not found"}
        self._check_one(entry)
        return self._reg.get(hostname) or {}


# ─────────────────────────────────────────────────────────────────────────────
#  INTEGRATION API  (other managers call POST /api/integrate)
# ─────────────────────────────────────────────────────────────────────────────
#
#  Payload: {
#    "source"     : "php_mngr" | "node_mngr" | "stax_mngr" | ...,
#    "hostname"   : "mysite.internal",
#    "target_host": "127.0.0.1",
#    "target_port": 8100,
#    "label"      : "My Site",
#    "action"     : "add" | "remove"
#  }
#
#  This endpoint is also used internally (e.g. auto-remove when a site is deleted).


# ─────────────────────────────────────────────────────────────────────────────
#  EMBEDDED DASHBOARD HTML
# ─────────────────────────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LDOMAIN-3NGIN3 — Local Domain Manager</title>
<link rel="shortcut icon" href="https://www.phdesigns.net/img/favicon.ico" type="image/x-icon">
<link rel="icon" href="https://www.phdesigns.net/img/favicon.ico" type="image/x-icon">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#212121;--sidebar:#2d2d2d;--card:#2d2d2d;--card-hover:#353535;
  --card-dk:#1a1a1a;--border:#404040;--text:#ececec;--dim:#8e8ea0;--muted:#565869;
  --teal:#10b981;--teal-dim:#064e3b;--pink:#e879a8;
  --red:#ef4444;--red-dim:#450a0a;--amber:#f59e0b;
  --ok:#10b981;--warn:#f59e0b;--crit:#ef4444;
  --log-bg:#1a1a1a;--mono:'Menlo','Consolas','Courier New',monospace;
}
body{font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:var(--bg);color:var(--text);min-height:100vh;display:flex;flex-direction:column;font-size:14px;line-height:1.5;}
::-webkit-scrollbar{width:4px}::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}
header{background:var(--sidebar);border-bottom:1px solid var(--border);padding:12px 22px;
  display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;}
.brand{display:flex;align-items:center;gap:10px}
.brand-icon{width:27px;height:27px;border-radius:7px;
  background:linear-gradient(145deg,#ff56b9 0%,#ff4fb3 72%,#c86bff 100%);
  display:flex;align-items:center;justify-content:center;
  box-shadow:inset 0 0 0 1px rgba(255,255,255,0.14);
  flex-shrink:0;}
.brand-name{font-size:16px;font-weight:700;letter-spacing:-0.3px}
.brand-name span{color:var(--teal)}
.brand-sub{font-size:11px;color:var(--muted);margin-top:1px}
.header-badges{display:flex;gap:8px;flex-wrap:wrap}
.badge{font-size:11px;padding:3px 9px;border-radius:4px;font-weight:600;font-family:var(--mono);
  border:1px solid var(--border);background:var(--card)}
.badge.ok{color:var(--ok);border-color:var(--teal-dim)}
.badge.warn{color:var(--amber)}
.badge.off{color:var(--muted)}
nav{background:var(--sidebar);border-bottom:1px solid var(--border);padding:0 22px;display:flex;gap:2px;overflow-x:auto}
.tab{padding:10px 16px;font-size:13px;font-weight:500;color:var(--muted);cursor:pointer;
  border-bottom:2px solid transparent;white-space:nowrap;transition:color .15s,border-color .15s}
.tab.active,.tab:hover{color:var(--text)}
.tab.active{border-color:var(--teal);color:var(--teal)}
main{flex:1;padding:20px 22px;display:flex;flex-direction:column;gap:16px}
.panel{display:none}.panel.active{display:flex;flex-direction:column;gap:16px}
.row{display:flex;gap:16px;flex-wrap:wrap}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:18px 20px}
.card:hover{border-color:var(--muted)}
.card-title{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin-bottom:10px}
.metric{font-size:28px;font-weight:700;color:var(--text)}
.metric-label{font-size:11px;color:var(--muted);margin-top:2px}
.stat-row{display:flex;justify-content:space-between;align-items:center;padding:5px 0;
  border-bottom:1px solid var(--border)}
.stat-row:last-child{border:none}
.stat-label{font-size:12px;color:var(--dim)}
.stat-val{font-size:12px;font-family:var(--mono);color:var(--text)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;padding:8px 10px;font-size:11px;font-weight:600;letter-spacing:.5px;
  text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--border)}
td{padding:9px 10px;border-bottom:1px solid var(--border);color:var(--dim)}
tr:last-child td{border:none}
tr:hover td{background:var(--card-hover)}
.hl{color:var(--teal);font-weight:600;font-family:var(--mono);font-size:12px}
.pill{display:inline-flex;align-items:center;padding:2px 8px;border-radius:12px;
  font-size:11px;font-weight:600;font-family:var(--mono)}
.pill.up{background:rgba(16,185,129,.12);color:var(--ok)}
.pill.down{background:rgba(239,68,68,.12);color:var(--red)}
.pill.unknown{background:rgba(82,82,91,.15);color:var(--muted)}
.pill.disabled{background:rgba(245,158,11,.08);color:var(--amber)}
.btn{padding:7px 16px;border:none;border-radius:6px;font-size:12px;font-weight:600;
  cursor:pointer;transition:filter .12s,transform .1s;display:inline-flex;align-items:center;gap:5px}
.btn:hover{filter:brightness(1.12)}
.btn:active{transform:scale(.97)}
.btn-teal{background:var(--teal);color:#fff}
.btn-ghost{background:transparent;color:var(--dim);border:1px solid var(--border)}
.btn-ghost:hover{background:var(--card-hover);color:var(--text)}
.btn-red{background:var(--red);color:#fff}
.btn-amber{background:var(--amber);color:#000}
.form-group{display:flex;flex-direction:column;gap:4px;margin-bottom:12px}
label{font-size:12px;color:var(--dim);font-weight:500}
input,select,textarea{background:var(--log-bg);border:1px solid var(--border);border-radius:6px;
  color:var(--text);padding:8px 12px;font-size:13px;width:100%;outline:none;font-family:inherit}
input:focus,select:focus,textarea:focus{border-color:var(--teal)}
select option{background:var(--log-bg)}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
#log-box{background:var(--log-bg);border:1px solid var(--border);border-radius:8px;padding:12px 14px;
  overflow-y:auto;font-family:var(--mono);font-size:11.5px;line-height:1.7;max-height:350px;min-height:150px}
.ll{display:flex;gap:10px;align-items:baseline}.ll+.ll{margin-top:1px}
.ll-ts{color:var(--muted);flex-shrink:0;font-size:10.5px}
.ll-svc{color:var(--teal);flex-shrink:0;min-width:80px;font-size:10.5px}
.ll-msg{color:var(--dim);word-break:break-all}
.ll.blocked .ll-msg{color:var(--red)}
.ll.not_found .ll-msg{color:var(--amber)}
.ll.malformed .ll-msg{color:var(--amber)}
.ll.request .ll-msg{color:#52525b}
code{background:var(--log-bg);padding:2px 6px;border-radius:4px;font-family:var(--mono);font-size:11px}
pre{background:var(--log-bg);border:1px solid var(--border);border-radius:8px;
  padding:14px 16px;font-family:var(--mono);font-size:11.5px;overflow-x:auto;white-space:pre-wrap}
.notice{background:rgba(16,185,129,.07);border:1px solid rgba(16,185,129,.2);border-radius:8px;
  padding:12px 16px;font-size:13px;color:var(--dim)}
.notice.warn{background:rgba(245,158,11,.07);border-color:rgba(245,158,11,.2)}
.notice.info{background:rgba(59,130,246,.07);border-color:rgba(59,130,246,.2)}
.domain-actions{display:flex;gap:6px;flex-wrap:wrap}
#toast{position:fixed;bottom:24px;right:24px;padding:10px 18px;border-radius:8px;
  font-size:13px;font-weight:600;color:#fff;background:#2d2d2d;border:1px solid var(--border);
  opacity:0;transition:opacity .25s;z-index:999;pointer-events:none}
#toast.show{opacity:1}
footer{text-align:center;padding:9px;font-size:11px;color:var(--muted);
  border-top:1px solid var(--border);background:var(--sidebar)}
footer a{color:var(--dim);text-decoration:none}
footer a:hover{color:var(--teal)}
@media(max-width:700px){.form-row{grid-template-columns:1fr}.row{flex-direction:column}}
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="brand-icon"><span style="color:#f4f4f5;font-size:11px;font-weight:700;font-family:Menlo,Consolas,monospace;letter-spacing:-0.5px;">&gt;_</span></div>
    <div>
      <div class="brand-name">LDOMAIN<span>-3NGIN3</span></div>
      <div class="brand-sub">Local Domain &amp; Reverse Proxy Manager &mdash; v%%VERSION%%</div>
    </div>
  </div>
  <div class="header-badges" id="hdr-badges">&#8230;</div>
</header>

<nav>
  <div class="tab active" onclick="show('overview')">Overview</div>
  <div class="tab" onclick="show('domains')">Domains</div>
  <div class="tab" onclick="show('add')">+ Add Domain</div>
  <div class="tab" onclick="show('proxy')">Proxy</div>
  <div class="tab" onclick="show('dns')">DNS</div>
  <div class="tab" onclick="show('tls')">TLS / CA</div>
  <div class="tab" onclick="show('logs')">Access Logs</div>
  <div class="tab" onclick="show('integrate')">Integration</div>
</nav>

<main>
<!-- OVERVIEW -->
<div class="panel active" id="panel-overview">
  <div class="row">
    <div class="card" style="flex:1;min-width:140px">
      <div class="card-title">Domains</div>
      <div class="metric" id="ov-total">—</div>
      <div class="metric-label">registered</div>
    </div>
    <div class="card" style="flex:1;min-width:140px">
      <div class="card-title">Healthy</div>
      <div class="metric" id="ov-up" style="color:var(--ok)">—</div>
      <div class="metric-label">backends reachable</div>
    </div>
    <div class="card" style="flex:1;min-width:140px">
      <div class="card-title">Banned IPs</div>
      <div class="metric" id="ov-bans">—</div>
      <div class="metric-label">from SEC-MNGR</div>
    </div>
    <div class="card" style="flex:1;min-width:140px">
      <div class="card-title">Requests (ring)</div>
      <div class="metric" id="ov-reqs">—</div>
      <div class="metric-label">in memory</div>
    </div>
  </div>
  <div class="card">
    <div class="card-title">Service Status</div>
    <div id="ov-status">Loading…</div>
  </div>
  <div class="card">
    <div class="card-title">Domain Health</div>
    <table id="ov-health-tbl">
      <thead><tr><th>Hostname</th><th>Target</th><th>Health</th><th>Latency</th><th>Last Checked</th></tr></thead>
      <tbody id="ov-health-body"></tbody>
    </table>
  </div>
</div>

<!-- DOMAINS -->
<div class="panel" id="panel-domains">
  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
      <span style="font-weight:600">Domain Registry</span>
      <button class="btn btn-teal" onclick="show('add')">+ Add Domain</button>
    </div>
    <table>
      <thead><tr><th>Hostname</th><th>Target</th><th>Label</th><th>TLS</th><th>Health</th><th>Actions</th></tr></thead>
      <tbody id="domains-body"></tbody>
    </table>
  </div>
</div>

<!-- ADD / EDIT -->
<div class="panel" id="panel-add">
  <div class="card" style="max-width:680px">
    <div style="font-weight:600;margin-bottom:16px" id="add-title">Register a Local Domain</div>
    <input type="hidden" id="edit-id">
    <div class="form-row">
      <div class="form-group">
        <label>Hostname (e.g. myapp.internal)</label>
        <input id="f-hostname" placeholder="myapp.internal">
      </div>
      <div class="form-group">
        <label>Label</label>
        <input id="f-label" placeholder="My App">
      </div>
    </div>
    <div class="form-row">
      <div class="form-group">
        <label>Target Host</label>
        <input id="f-host" value="127.0.0.1">
      </div>
      <div class="form-group">
        <label>Target Port</label>
        <input id="f-port" type="number" min="1" max="65535" placeholder="3000">
      </div>
    </div>
    <div class="form-group">
      <label>Description</label>
      <input id="f-desc" placeholder="optional">
    </div>
    <div class="form-row">
      <div class="form-group">
        <label>Tags (comma-separated)</label>
        <input id="f-tags" placeholder="web, app">
      </div>
      <div class="form-group">
        <label>Timeout (s)</label>
        <input id="f-timeout" type="number" value="30" min="1" max="300">
      </div>
    </div>
    <div class="form-row">
      <div class="form-group">
        <label><input type="checkbox" id="f-tls" style="width:auto;margin-right:6px">Enable TLS (requires cert)</label>
      </div>
      <div class="form-group">
        <label><input type="checkbox" id="f-enabled" checked style="width:auto;margin-right:6px">Enabled</label>
      </div>
    </div>
    <div style="display:flex;gap:10px;margin-top:6px">
      <button class="btn btn-teal" onclick="saveDomain()">Save Domain</button>
      <button class="btn btn-ghost" onclick="clearForm()">Clear</button>
    </div>
  </div>
</div>

<!-- PROXY -->
<div class="panel" id="panel-proxy">
  <div class="card">
    <div class="card-title">Reverse Proxy Status</div>
    <div id="proxy-status">Loading…</div>
  </div>
  <div class="card">
    <div class="card-title">How It Works</div>
    <p style="color:var(--dim);font-size:13px;line-height:1.7">
      The reverse proxy listens on port 80 (HTTP) and 443 (HTTPS). When a request arrives,
      it reads the <code>Host</code> header and looks up the domain in the registry.
      The request is forwarded to the configured backend <code>target_host:target_port</code>,
      with the original client IP passed as <code>X-Forwarded-For</code>.<br><br>
      <strong style="color:var(--text)">WebSocket</strong> upgrades are tunnelled transparently.<br>
      <strong style="color:var(--text)">SEC-MNGR</strong> banned IPs are blocked before any backend contact.<br>
      <strong style="color:var(--text)">Custom headers</strong> can be injected per domain (edit the domain entry).<br><br>
      If port 80/443 cannot be bound (requires root), the proxy falls back to ports <strong style="color:var(--text)">8180</strong> / <strong style="color:var(--text)">8143</strong>
      and the iptables commands below can redirect traffic.
    </p>
  </div>
  <div class="card" id="iptables-card">
    <div class="card-title">Redirect Port 80 → Proxy (if running as non-root)</div>
    <pre id="iptables-cmds">Loading…</pre>
  </div>
</div>

<!-- DNS -->
<div class="panel" id="panel-dns">
  <div class="card">
    <div class="card-title">LAN DNS Resolver Status</div>
    <div id="dns-status">Loading…</div>
  </div>
  <div class="card">
    <div class="card-title">Point Devices to This DNS</div>
    <div class="notice info" style="margin-bottom:12px">
      The DNS resolver listens on UDP port <strong>5353</strong>. For LAN devices to use it automatically,
      either redirect port 53 to 5353 (requires root), or configure each device's DNS server to this machine's IP.
    </div>
    <pre id="dns-cmds">Loading…</pre>
  </div>
  <div class="card">
    <div class="card-title">Hosts File Adapter (single-machine resolution)</div>
    <p style="color:var(--dim);font-size:13px;margin-bottom:14px">
      Write a managed block to <code>/etc/hosts</code> so all registered domains resolve
      on this machine without using the DNS server or proxy.
    </p>
    <div style="display:flex;gap:10px;flex-wrap:wrap">
      <button class="btn btn-teal" onclick="writeHosts(false)">Write /etc/hosts Entries</button>
      <button class="btn btn-ghost" onclick="writeHosts(true)">Preview</button>
      <button class="btn btn-red" onclick="removeHosts()">Remove Managed Block</button>
    </div>
    <pre id="hosts-preview" style="margin-top:12px;display:none"></pre>
  </div>
</div>

<!-- TLS / CA -->
<div class="panel" id="panel-tls">
  <div class="card">
    <div class="card-title">Local Certificate Authority</div>
    <div id="ca-status">Loading…</div>
    <div style="display:flex;gap:10px;margin-top:14px;flex-wrap:wrap">
      <button class="btn btn-teal" onclick="generateCA()">Generate Root CA</button>
      <button class="btn btn-ghost" onclick="downloadCA()">&#11123; Download ca.crt</button>
    </div>
  </div>
  <div class="card">
    <div class="card-title">Install Root CA as Trusted</div>
    <pre id="ca-install-instructions" style="white-space:pre-wrap">Loading…</pre>
  </div>
  <div class="card">
    <div class="card-title">Issue Certificate for a Domain</div>
    <div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap">
      <div class="form-group" style="flex:1;margin:0">
        <label>Hostname</label>
        <input id="cert-hostname" placeholder="myapp.internal">
      </div>
      <button class="btn btn-teal" onclick="issueCert()">Issue Certificate</button>
    </div>
    <div id="cert-result" style="margin-top:12px"></div>
  </div>
  <div class="card">
    <div class="card-title">Issued Certificates</div>
    <table>
      <thead><tr><th>Hostname</th><th>Not Before</th><th>Not After</th><th>Status</th></tr></thead>
      <tbody id="cert-tbl-body"></tbody>
    </table>
  </div>
</div>

<!-- LOGS -->
<div class="panel" id="panel-logs">
  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
      <span style="font-weight:600">Proxy Access Logs</span>
      <button class="btn btn-ghost" onclick="clearLogs()">Clear</button>
    </div>
    <div id="log-box"></div>
  </div>
</div>

<!-- INTEGRATION -->
<div class="panel" id="panel-integrate">
  <div class="card">
    <div class="card-title">Integration with Other KillTheHost Managers</div>
    <p style="color:var(--dim);font-size:13px;margin-bottom:14px">
      Other managers (PHP-MNGR, NODE-MNGR, STAX-MNGR) can register or remove a local domain
      by calling the integration API. This is also how LDOMAIN-3NGIN3 can be extended.
    </p>
    <pre style="margin-bottom:14px">POST http://127.0.0.1:%%UI_PORT%%/api/integrate
Content-Type: application/json

{
  "source"      : "php_mngr",
  "hostname"    : "mysite.internal",
  "target_host" : "127.0.0.1",
  "target_port" : 8100,
  "label"       : "My Site",
  "action"      : "add"   // or "remove"
}</pre>
  </div>
  <div class="card">
    <div class="card-title">SEC-MNGR Integration</div>
    <div class="notice">
      <strong>Active:</strong> All proxy access logs are written to
      <code>~/.ldomain3ngin3/access_logs.jsonl</code> in SEC-MNGR's unified log format.
      SEC-MNGR automatically ingests this file if <code>ldomain3ngin3</code> is listed
      in its SERVICES config.
    </div>
    <div class="notice" style="margin-top:10px">
      <strong>Ban enforcement:</strong> LDOMAIN-3NGIN3 reads
      <code>~/.secmngr/blocklist.txt</code> every 30 s and blocks all listed IPs/CIDRs
      at the proxy layer before any request reaches a backend.
    </div>
    <div id="sec-status" style="margin-top:14px">Checking…</div>
  </div>
</div>
</main>

<div id="toast"></div>

<footer>
  LDOMAIN-3NGIN3 v%%VERSION%% &nbsp;|&nbsp;
  KillTheHost &nbsp;|&nbsp;
  <a href="http://127.0.0.1:%%UI_PORT%%">http://127.0.0.1:%%UI_PORT%%</a>
  &nbsp;|&nbsp; AGPL-3.0
</footer>

<script>
const UI_PORT = %%UI_PORT%%;
let _logs = [], _lastLogLen = 0;

// ── tabs ────
function show(id) {
  document.querySelectorAll('.tab').forEach((t,i) => {
    const panels = ['overview','domains','add','proxy','dns','tls','logs','integrate'];
    t.classList.toggle('active', panels[i] === id);
  });
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  const p = document.getElementById('panel-' + id);
  if (p) p.classList.add('active');
  if (id === 'logs') renderLogs();
}

// ── toast ────
function toast(msg, ok=true) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.style.background = ok ? '#064e3b' : '#450a0a';
  t.style.borderColor = ok ? '#10b981' : '#ef4444';
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 2800);
}

// ── api ────
async function api(path, method='GET', body=null) {
  const opts = { method, headers: {'Content-Type':'application/json'} };
  if (body) opts.body = JSON.stringify(body);
  try {
    const r = await fetch(path, opts);
    return await r.json();
  } catch(e) { return {ok:false,error:String(e)}; }
}

// ── overview ────
async function loadOverview() {
  const d = await api('/api/status');
  if (!d) return;
  document.getElementById('ov-total').textContent = d.total_domains ?? '—';
  document.getElementById('ov-up').textContent    = d.domains_up   ?? '—';
  document.getElementById('ov-bans').textContent  = d.ban_count    ?? '—';
  document.getElementById('ov-reqs').textContent  = d.log_entries  ?? '—';

  // Status badges
  const badges = document.getElementById('hdr-badges');
  const parts = [];
  const proxyBadge = d.proxy_http ? `<span class="badge ok">Proxy :${d.proxy_http}</span>` : '<span class="badge warn">Proxy off</span>';
  const dnsBadge   = d.dns_port   ? `<span class="badge ok">DNS :${d.dns_port}</span>`     : '<span class="badge off">DNS off</span>';
  const caBadge    = d.ca_exists  ? '<span class="badge ok">CA ready</span>'                : '<span class="badge warn">No CA</span>';
  badges.innerHTML = proxyBadge + dnsBadge + caBadge;

  // Service status card
  const sv = [];
  sv.push(`<div class="stat-row"><span class="stat-label">Proxy HTTP</span><span class="stat-val">${d.proxy_http ? ':'+d.proxy_http : 'off'}</span></div>`);
  sv.push(`<div class="stat-row"><span class="stat-label">Proxy HTTPS</span><span class="stat-val">${d.proxy_https ? ':'+d.proxy_https : 'off'}</span></div>`);
  sv.push(`<div class="stat-row"><span class="stat-label">DNS (UDP)</span><span class="stat-val">${d.dns_port ? ':'+d.dns_port : 'off'}</span></div>`);
  sv.push(`<div class="stat-row"><span class="stat-label">SEC-MNGR blocklist</span><span class="stat-val">${d.ban_count ?? 0} entries</span></div>`);
  sv.push(`<div class="stat-row"><span class="stat-label">CA certificate</span><span class="stat-val">${d.ca_exists ? 'Generated' : 'Not generated'}</span></div>`);
  sv.push(`<div class="stat-row"><span class="stat-label">Domain suffix</span><span class="stat-val">${d.suffix ?? '.internal'}</span></div>`);
  document.getElementById('ov-status').innerHTML = sv.join('');

  // Health table
  const rows = (d.domains ?? []).map(dom => {
    const hcls = dom.health === 'up' ? 'up' : dom.health === 'down' ? 'down' : 'unknown';
    const lat  = dom.latency_ms != null ? dom.latency_ms.toFixed(0) + ' ms' : '—';
    const chk  = dom.last_checked ? dom.last_checked.split('T')[1]?.substring(0,8) ?? dom.last_checked : '—';
    return `<tr>
      <td class="hl">${esc(dom.hostname)}</td>
      <td><code>${esc(dom.target_host)}:${dom.target_port}</code></td>
      <td><span class="pill ${hcls}">${dom.enabled ? dom.health : 'disabled'}</span></td>
      <td>${lat}</td>
      <td>${chk}</td>
    </tr>`;
  });
  document.getElementById('ov-health-body').innerHTML = rows.join('') || '<tr><td colspan="5" style="color:var(--muted);text-align:center">No domains registered</td></tr>';
}

// ── domains ────
async function loadDomains() {
  const d = await api('/api/domains');
  const body = document.getElementById('domains-body');
  if (!d || !d.domains) { body.innerHTML = '<tr><td colspan="6" style="color:var(--muted)">No domains</td></tr>'; return; }
  body.innerHTML = d.domains.map(dom => {
    const hcls = dom.health === 'up' ? 'up' : dom.health === 'down' ? 'down' : dom.enabled ? 'unknown' : 'disabled';
    const hlbl = dom.enabled ? dom.health : 'disabled';
    return `<tr>
      <td class="hl">${esc(dom.hostname)}</td>
      <td><code>${esc(dom.target_host)}:${dom.target_port}</code></td>
      <td>${esc(dom.label)}</td>
      <td>${dom.tls_enabled ? '&#128274;' : '—'}</td>
      <td><span class="pill ${hcls}">${hlbl}</span></td>
      <td><div class="domain-actions">
        <button class="btn btn-ghost" onclick="editDomain(${JSON.stringify(JSON.stringify(dom))})">Edit</button>
        <button class="btn btn-ghost" onclick="checkHealth('${esc(dom.hostname)}')">Check</button>
        <button class="btn btn-ghost" onclick="toggleDomain('${esc(dom.hostname)}', ${dom.enabled})">
          ${dom.enabled ? 'Disable' : 'Enable'}
        </button>
        <button class="btn btn-red" onclick="deleteDomain('${esc(dom.hostname)}')">Del</button>
      </div></td>
    </tr>`;
  }).join('') || '<tr><td colspan="6" style="color:var(--muted)">No domains</td></tr>';
}

function editDomain(jsonStr) {
  const d = JSON.parse(jsonStr);
  document.getElementById('edit-id').value      = d.id;
  document.getElementById('f-hostname').value   = d.hostname;
  document.getElementById('f-label').value      = d.label || '';
  document.getElementById('f-host').value       = d.target_host;
  document.getElementById('f-port').value       = d.target_port;
  document.getElementById('f-desc').value       = d.description || '';
  document.getElementById('f-tags').value       = (d.tags || []).join(', ');
  document.getElementById('f-timeout').value    = d.timeout || 30;
  document.getElementById('f-tls').checked      = !!d.tls_enabled;
  document.getElementById('f-enabled').checked  = !!d.enabled;
  document.getElementById('add-title').textContent = 'Edit Domain';
  show('add');
}

function clearForm() {
  ['edit-id','f-hostname','f-label','f-desc','f-tags'].forEach(id => document.getElementById(id).value = '');
  document.getElementById('f-host').value    = '127.0.0.1';
  document.getElementById('f-port').value    = '';
  document.getElementById('f-timeout').value = '30';
  document.getElementById('f-tls').checked   = false;
  document.getElementById('f-enabled').checked = true;
  document.getElementById('add-title').textContent = 'Register a Local Domain';
}

async function saveDomain() {
  const hostname = document.getElementById('f-hostname').value.trim();
  const port     = parseInt(document.getElementById('f-port').value);
  if (!hostname) { toast('Hostname required', false); return; }
  if (!port)     { toast('Target port required', false); return; }
  const payload = {
    id           : document.getElementById('edit-id').value || undefined,
    hostname, label: document.getElementById('f-label').value.trim() || hostname,
    target_host  : document.getElementById('f-host').value.trim() || '127.0.0.1',
    target_port  : port,
    description  : document.getElementById('f-desc').value.trim(),
    tags         : document.getElementById('f-tags').value.split(',').map(t=>t.trim()).filter(Boolean),
    timeout      : parseInt(document.getElementById('f-timeout').value) || 30,
    tls_enabled  : document.getElementById('f-tls').checked,
    enabled      : document.getElementById('f-enabled').checked,
  };
  const r = await api('/api/domains', 'POST', payload);
  if (r && r.ok) { toast('Domain saved'); clearForm(); loadDomains(); loadOverview(); }
  else toast(r?.error || 'Error', false);
}

async function deleteDomain(hostname) {
  if (!confirm(`Delete domain "${hostname}"?`)) return;
  const r = await api('/api/domains/' + encodeURIComponent(hostname), 'DELETE');
  if (r && r.ok) { toast('Deleted'); loadDomains(); loadOverview(); }
  else toast(r?.error || 'Error', false);
}

async function toggleDomain(hostname, enabled) {
  const r = await api('/api/domains/' + encodeURIComponent(hostname) + '/toggle', 'POST');
  if (r && r.ok) { toast(enabled ? 'Disabled' : 'Enabled'); loadDomains(); loadOverview(); }
  else toast(r?.error || 'Error', false);
}

async function checkHealth(hostname) {
  toast('Checking…');
  const r = await api('/api/domains/' + encodeURIComponent(hostname) + '/health', 'POST');
  if (r && r.health) toast(`${hostname}: ${r.health}${r.latency_ms ? ' ('+r.latency_ms.toFixed(0)+'ms)' : ''}`);
  else toast(r?.error || 'Error', false);
  loadDomains(); loadOverview();
}

// ── proxy ────
async function loadProxy() {
  const d = await api('/api/status');
  if (!d) return;
  const hp = d.proxy_http  ? `<span style="color:var(--ok)">Running on :${d.proxy_http}</span>` : '<span style="color:var(--amber)">Not running (port unavailable or disabled)</span>';
  const sp = d.proxy_https ? `<span style="color:var(--ok)">Running on :${d.proxy_https}</span>` : '<span style="color:var(--amber)">Not running</span>';
  document.getElementById('proxy-status').innerHTML =
    `<div class="stat-row"><span class="stat-label">HTTP Proxy</span><span class="stat-val">${hp}</span></div>` +
    `<div class="stat-row"><span class="stat-label">HTTPS Proxy</span><span class="stat-val">${sp}</span></div>` +
    `<div class="stat-row"><span class="stat-label">SEC-MNGR Enforce</span><span class="stat-val">${d.sec_enforce ? 'On' : 'Off'}</span></div>`;

  const hp_port = d.proxy_http || 8180;
  const sp_port = d.proxy_https || 8143;
  const cmds = `# If running as non-root, redirect privileged ports to proxy ports:
sudo iptables -t nat -A PREROUTING -p tcp --dport 80  -j REDIRECT --to-port ${hp_port}
sudo iptables -t nat -A PREROUTING -p tcp --dport 443 -j REDIRECT --to-port ${sp_port}

# Make persistent (Debian/Ubuntu):
sudo apt-get install -y iptables-persistent
sudo netfilter-persistent save

# Remove the rules:
sudo iptables -t nat -D PREROUTING -p tcp --dport 80  -j REDIRECT --to-port ${hp_port}
sudo iptables -t nat -D PREROUTING -p tcp --dport 443 -j REDIRECT --to-port ${sp_port}`;
  document.getElementById('iptables-cmds').textContent = cmds;
}

// ── dns ────
async function loadDNS() {
  const d = await api('/api/status');
  if (!d) return;
  const ds = d.dns_port
    ? `<span style="color:var(--ok)">Running on UDP :${d.dns_port}</span>`
    : '<span style="color:var(--amber)">Not running</span>';
  document.getElementById('dns-status').innerHTML =
    `<div class="stat-row"><span class="stat-label">DNS Resolver</span><span class="stat-val">${ds}</span></div>`;

  const ip = d.lan_ip || 'YOUR_HOST_IP';
  const dp  = d.dns_port || 5353;
  const cmds = `# Redirect port 53 → ${dp} (so LAN devices can use port 53):
sudo iptables -t nat -A PREROUTING -p udp --dport 53 -j REDIRECT --to-port ${dp}
sudo iptables -t nat -A OUTPUT     -p udp --dport 53 -j REDIRECT --to-port ${dp}

# On LAN devices, set DNS server to: ${ip}

# Or configure dnsmasq to forward .internal to ${ip}#${dp}:
# echo "server=/.internal/${ip}#${dp}" | sudo tee -a /etc/dnsmasq.conf
# sudo systemctl restart dnsmasq

# Test from this machine:
dig @127.0.0.1 -p ${dp} myapp.internal`;
  document.getElementById('dns-cmds').textContent = cmds;
}

async function writeHosts(dry) {
  const r = await api('/api/hosts', 'POST', {dry_run: dry});
  if (r && r.ok) {
    if (dry) { document.getElementById('hosts-preview').textContent = r.message; document.getElementById('hosts-preview').style.display = 'block'; }
    else toast(r.message);
  } else toast(r?.error || 'Error', false);
}

async function removeHosts() {
  const r = await api('/api/hosts', 'DELETE');
  if (r && r.ok) toast(r.message);
  else toast(r?.error || 'Error', false);
}

// ── TLS / CA ────
async function loadTLS() {
  const d = await api('/api/ca/status');
  if (!d) return;
  const caEl = document.getElementById('ca-status');
  caEl.innerHTML = d.exists
    ? '<div class="notice">Root CA is generated and ready.</div>'
    : '<div class="notice warn">Root CA has not been generated yet. Click "Generate Root CA" to create it.</div>';

  document.getElementById('ca-install-instructions').textContent = d.install_instructions || '';

  // Cert table
  const rows = (d.certs || []).map(c =>
    `<tr><td class="hl">${esc(c.hostname)}</td><td>${esc(c.notbefore||'—')}</td><td>${esc(c.notafter||'—')}</td>
     <td><span class="pill ${c.valid ? 'up' : 'down'}">${c.valid ? 'Valid' : 'Invalid'}</span></td></tr>`
  );
  document.getElementById('cert-tbl-body').innerHTML = rows.join('') || '<tr><td colspan="4" style="color:var(--muted)">No certificates issued</td></tr>';
}

async function generateCA() {
  toast('Generating CA (may take a moment)…');
  const r = await api('/api/ca/generate', 'POST');
  if (r && r.ok) { toast(r.message); loadTLS(); }
  else toast(r?.error || 'Error', false);
}

function downloadCA() { window.open('/api/ca/download', '_blank'); }

async function issueCert() {
  const h = document.getElementById('cert-hostname').value.trim();
  if (!h) { toast('Enter a hostname', false); return; }
  toast(`Issuing cert for ${h}…`);
  const r = await api('/api/ca/issue', 'POST', {hostname: h});
  const el = document.getElementById('cert-result');
  if (r && r.ok) { toast(r.message); el.innerHTML = `<div class="notice">${esc(r.message)}</div>`; loadTLS(); }
  else { el.innerHTML = `<div class="notice warn">${esc(r?.error||'Error')}</div>`; toast(r?.error||'Error', false); }
}

// ── logs ────
async function loadLogData() {
  const d = await api('/api/logs');
  if (!d || !d.entries) return;
  _logs = d.entries;
}

function renderLogs() {
  const box = document.getElementById('log-box');
  box.innerHTML = '';
  (_logs.slice().reverse()).forEach(e => {
    const div = document.createElement('div');
    div.className = 'll ' + (e.event_type || 'request');
    const ts  = e.timestamp ? e.timestamp.split('T')[1]?.substring(0,8) ?? e.timestamp : '—';
    const msg = `${e.method} ${e.path} → ${e.status}  [${e.ip}]${e.detail ? '  '+e.detail : ''}`;
    div.innerHTML = `<span class="ll-ts">[${esc(ts)}]</span><span class="ll-svc">[PROXY]</span><span class="ll-msg">${esc(msg)}</span>`;
    box.appendChild(div);
  });
  box.scrollTop = box.scrollHeight;
}

function clearLogs() { _logs = []; renderLogs(); }

// ── integrate ────
async function loadIntegrate() {
  const d = await api('/api/status');
  if (!d) return;
  const sec = document.getElementById('sec-status');
  const bl  = d.ban_count ?? 0;
  sec.innerHTML = `<div class="stat-row"><span class="stat-label">SEC-MNGR blocklist loaded</span><span class="stat-val">${bl} network(s)</span></div>` +
    `<div class="stat-row"><span class="stat-label">Blocklist path</span><span class="stat-val"><code>~/.secmngr/blocklist.txt</code></span></div>` +
    `<div class="stat-row"><span class="stat-label">LDOMAIN access log</span><span class="stat-val"><code>~/.ldomain3ngin3/access_logs.jsonl</code></span></div>`;
}

// ── poll ────
async function pollAll() {
  await loadOverview();
  await loadLogData();
}

function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

window.addEventListener('DOMContentLoaded', () => {
  pollAll();
  loadProxy();
  loadDNS();
  loadTLS();
  loadDomains();
  loadIntegrate();
  setInterval(pollAll, 10000);
  setInterval(loadProxy, 15000);
});
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────────────────────
#  GLOBAL STATE
# ─────────────────────────────────────────────────────────────────────────────

CFG       : Config           = None   # type: ignore
REGISTRY  : Registry         = None   # type: ignore
ENFORCER  : BlocklistEnforcer = None  # type: ignore
LOCAL_CA  : LocalCA          = None   # type: ignore
DNS_SRV   : DNSServer        = None   # type: ignore
PROXY_SRV : ReverseProxy     = None   # type: ignore
HEALTH_CK : HealthChecker    = None   # type: ignore
_started  : float            = 0.0


def _get_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def _blocklist_reload_loop():
    while True:
        try:
            ENFORCER.reload()
        except Exception:
            pass
        time.sleep(BLOCKLIST_RELOAD)


# ─────────────────────────────────────────────────────────────────────────────
#  HTTP API HANDLER
# ─────────────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/":
            self._page()
        elif path == "/api/status":
            self._json(self._status())
        elif path == "/api/domains":
            self._json({"ok": True, "domains": REGISTRY.list()})
        elif path == "/api/logs":
            self._json({"ok": True, "entries": recent_logs(500)})
        elif path == "/api/ca/status":
            self._json(self._ca_status())
        elif path == "/api/ca/download":
            if LOCAL_CA.exists():
                pem = LOCAL_CA.ca_pem().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/x-pem-file")
                self.send_header("Content-Disposition", 'attachment; filename="killthehost-ca.crt"')
                self.send_header("Content-Length", str(len(pem)))
                self.end_headers()
                self.wfile.write(pem)
            else:
                self._json({"ok": False, "error": "CA not generated"}, 404)
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        path  = urlparse(self.path).path.rstrip("/")
        body  = self._read_body()

        # Domain CRUD
        if path == "/api/domains":
            ok, res = REGISTRY.add(body)
            self._json({"ok": ok, **(res if isinstance(res, dict) else {"error": res})})

        elif re.match(r"^/api/domains/[^/]+/toggle$", path):
            hostname = unquote(path.split("/")[3])
            d = REGISTRY.get(hostname)
            if not d:
                self._json({"ok": False, "error": "not found"}, 404); return
            if d["enabled"]:
                ok, r = REGISTRY.disable(hostname)
            else:
                d["enabled"] = True
                ok, r = REGISTRY.add(d)
            self._json({"ok": ok})

        elif re.match(r"^/api/domains/[^/]+/health$", path):
            hostname = unquote(path.split("/")[3])
            result = HEALTH_CK.check_one_now(hostname)
            self._json({"ok": True, **result})

        # Hosts file
        elif path == "/api/hosts":
            ok, msg = write_hosts_entries(REGISTRY.list(), dry_run=body.get("dry_run", False))
            self._json({"ok": ok, "message": msg})

        # CA
        elif path == "/api/ca/generate":
            ok, msg = LOCAL_CA.generate()
            self._json({"ok": ok, "message": msg})

        elif path == "/api/ca/issue":
            hostname = body.get("hostname", "").strip()
            if not hostname:
                self._json({"ok": False, "error": "hostname required"}); return
            ok, msg = LOCAL_CA.issue_cert(hostname)
            self._json({"ok": ok, "message": msg if ok else None,
                        "error": msg if not ok else None})

        # Integration endpoint
        elif path == "/api/integrate":
            self._handle_integrate(body)

        # Config
        elif path == "/api/config":
            CFG.update(body)
            self._json({"ok": True, "config": CFG.all()})

        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def do_DELETE(self):
        path = urlparse(self.path).path.rstrip("/")

        if re.match(r"^/api/domains/[^/]+$", path):
            hostname = unquote(path.split("/")[3])
            ok = REGISTRY.delete(hostname)
            if ok and CFG.get("hosts_file_managed"):
                write_hosts_entries(REGISTRY.list())
            self._json({"ok": ok})

        elif path == "/api/hosts":
            ok, msg = remove_hosts_entries()
            self._json({"ok": ok, "message": msg})

        else:
            self._json({"ok": False, "error": "not found"}, 404)

    # ── helpers ────

    def _handle_integrate(self, body: dict):
        action   = body.get("action", "add")
        hostname = (body.get("hostname") or "").strip()
        if not hostname:
            self._json({"ok": False, "error": "hostname required"}); return
        if action == "remove":
            ok = REGISTRY.delete(hostname.lower())
            if CFG.get("hosts_file_managed"):
                write_hosts_entries(REGISTRY.list())
            self._json({"ok": ok})
        else:
            entry = {
                "hostname"   : hostname,
                "target_host": body.get("target_host", "127.0.0.1"),
                "target_port": body.get("target_port", 80),
                "label"      : body.get("label", hostname),
                "description": body.get("description", ""),
                "source"     : body.get("source", "api"),
                "enabled"    : True,
            }
            ok, res = REGISTRY.add(entry)
            if ok and CFG.get("hosts_file_managed"):
                write_hosts_entries(REGISTRY.list())
            self._json({"ok": ok, **(res if isinstance(res, dict) else {"error": res})})

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_BODY:
                return {}
            raw = self.rfile.read(length)
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def _status(self) -> dict:
        domains = REGISTRY.list()
        total   = len(domains)
        up      = sum(1 for d in domains if d.get("health") == "up")
        hp = PROXY_SRV.http_port  if PROXY_SRV else None
        sp = PROXY_SRV.https_port if PROXY_SRV else None
        return {
            "ok"           : True,
            "version"      : VERSION,
            "uptime"       : int(now() - _started),
            "total_domains": total,
            "domains_up"   : up,
            "ban_count"    : ENFORCER.ban_count() if ENFORCER else 0,
            "log_entries"  : len(_log_buffer),
            "proxy_http"   : hp,
            "proxy_https"  : sp,
            "dns_port"     : DNS_SRV and CFG.get("dns_port") if DNS_SRV and DNS_SRV._thread and DNS_SRV._thread.is_alive() else None,
            "ca_exists"    : LOCAL_CA.exists(),
            "suffix"       : CFG.get("suffix", DEFAULT_SUFFIX),
            "sec_enforce"  : CFG.get("sec_enforce", True),
            "lan_ip"       : _get_lan_ip(),
            "domains"      : domains,
        }

    def _ca_status(self) -> dict:
        exists   = LOCAL_CA.exists()
        certs    = []
        if CERTS_DIR.exists():
            for d in CERTS_DIR.iterdir():
                if d.is_dir():
                    info = LOCAL_CA.cert_info(d.name)
                    info["hostname"] = d.name
                    certs.append(info)
        return {
            "ok"                  : True,
            "exists"              : exists,
            "install_instructions": LOCAL_CA.install_instructions(),
            "certs"               : certs,
        }

    def _page(self):
        html = (
            DASHBOARD_HTML
            .replace("%%VERSION%%", VERSION)
            .replace("%%UI_PORT%%", str(CFG.get("ui_port", DEFAULT_UI_PORT)))
        )
        body = html.encode("utf-8")
        self._raw(200, "text/html; charset=utf-8", body)

    def _json(self, data: dict, code: int = 200):
        body = json.dumps(data).encode("utf-8")
        self._raw(code, "application/json", body)

    def _raw(self, code: int, ct: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)


# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    global CFG, REGISTRY, ENFORCER, LOCAL_CA, DNS_SRV, PROXY_SRV, HEALTH_CK, _started

    parser = argparse.ArgumentParser(description="LDOMAIN-3NGIN3 — Local Domain & Reverse Proxy Manager")
    parser.add_argument("--host",       default=os.environ.get("LDOMAIN_HOST", "127.0.0.1"))
    parser.add_argument("--port",       type=int, default=int(os.environ.get("LDOMAIN_PORT", DEFAULT_UI_PORT)))
    parser.add_argument("--no-browser", action="store_true",
                        default=os.environ.get("LDOMAIN_NO_BROWSER", "") == "1")
    parser.add_argument("--no-proxy",   action="store_true",
                        default=os.environ.get("LDOMAIN_NO_PROXY", "") == "1")
    parser.add_argument("--no-dns",     action="store_true",
                        default=os.environ.get("LDOMAIN_NO_DNS", "") == "1")
    args = parser.parse_args()

    print(f"""
╔════╗
║         LDOMAIN-3NGIN3  v{VERSION:<24}║
╚════╝
  Platform : {SYSTEM}
  Python   : {sys.version.split()[0]}
""", flush=True)

    ensure_data_dir()
    _started  = now()

    CFG      = Config()
    REGISTRY = Registry()
    ENFORCER = BlocklistEnforcer()
    LOCAL_CA = LocalCA(CFG)

    # Update UI port from args
    CFG.update({"ui_host": args.host, "ui_port": args.port})

    # Initial blocklist load
    ENFORCER.reload()
    threading.Thread(target=_blocklist_reload_loop, daemon=True).start()

    # Health checker
    HEALTH_CK = HealthChecker(REGISTRY, CFG, LOCAL_CA)
    HEALTH_CK.start()

    # DNS server
    if not args.no_dns and CFG.get("dns_enabled", True):
        DNS_SRV = DNSServer(REGISTRY, CFG)
        dns_port = int(CFG.get("dns_port", DEFAULT_DNS_PORT))
        DNS_SRV.start(dns_port)

    # Reverse proxy
    if not args.no_proxy and CFG.get("proxy_enabled", True):
        PROXY_SRV = ReverseProxy(REGISTRY, ENFORCER, CFG, LOCAL_CA)
        ok_http, ok_https = PROXY_SRV.start()
        if not ok_http and not ok_https:
            print("  [LDOMAIN] Warning: proxy could not bind any port.", flush=True)

    # Dashboard UI
    ui_host = args.host
    ui_port = args.port
    try:
        server = ThreadingHTTPServer((ui_host, ui_port), Handler)
    except OSError as exc:
        print(f"\n  [FATAL] Cannot bind dashboard to {ui_host}:{ui_port}: {exc}\n", flush=True)
        sys.exit(1)

    url = f"http://localhost:{ui_port}"
    print(f"  Dashboard : {url}", flush=True)
    print(f"  Data      : {DATA_DIR}", flush=True)
    print(f"  Domains   : {len(REGISTRY.list())} registered", flush=True)
    print(f"  Press Ctrl+C to stop\n", flush=True)

    def _on_sigterm(signum, frame):
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _on_sigterm)
    except (ValueError, OSError):
        pass

    if not args.no_browser and CFG.get("open_browser", True):
        def _open():
            time.sleep(1.0)
            try:
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  [LDOMAIN] Shutting down…", flush=True)
        if HEALTH_CK:
            HEALTH_CK.stop()
        if DNS_SRV:
            DNS_SRV.stop()
        if PROXY_SRV:
            PROXY_SRV.stop()
        server.server_close()
        print("  [LDOMAIN] Done.\n", flush=True)


if __name__ == "__main__":
    main()
