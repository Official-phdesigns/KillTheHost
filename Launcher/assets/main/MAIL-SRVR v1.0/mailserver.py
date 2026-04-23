#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║       MAIL-SRVR  v1.0  —  KillTheHost Mail           ║
║                                                      ║
║   Shares your Cloudflare zone with PHP-MNGR sites.   ║
║   mail.yourdomain.com runs alongside yourdomain.com. ║
║                                                      ║
║   Credentials are read from ~/.phpmngr/              ║
║   (shared with PHP-MNGR — no duplicate config).      ║
║                                                      ║
║   DNS is provisioned automatically via Cloudflare    ║
║   API. The A record is ALWAYS DNS-only (grey cloud)  ║
║   because Cloudflare cannot proxy SMTP or IMAP.      ║
║                                                      ║
║   Live mode WILL NOT START without a domain.         ║
║                                                      ║
║   Browser UI → http://localhost:6060                 ║
║   Pure Python 3.8+. Zero pip installs.               ║
╚══════════════════════════════════════════════════════╝
"""

import sys, os, json, socket, threading, time, subprocess, webbrowser
import imaplib, smtplib, ssl
import email as _email_mod
import email.header as _hdr
import email.message
import email.utils as _eutils
import base64, re, secrets as _secrets
from pathlib       import Path
from datetime      import datetime
from http.server   import HTTPServer, BaseHTTPRequestHandler
from socketserver  import ThreadingMixIn
from urllib.parse   import urlparse, parse_qs, unquote_plus, urlencode
from urllib.request import urlopen, Request as _URLReq
from urllib.error   import URLError, HTTPError

# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

PORT    = 6060
VERSION = "1.0"

# Our own data dir
DATA_DIR    = Path.home() / ".mailsrvr"
CONFIG_FILE = DATA_DIR / "config.json"
SPAM_FILE   = DATA_DIR / "spam_config.json"
DNS_FILE    = DATA_DIR / "dns_status.json"
CHECKLIST_FILE = DATA_DIR / "checklist.json"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Shared PHP-MNGR credential store — we READ these, never overwrite them
PHPMNGR_DIR      = Path.home() / ".phpmngr"
PHPMNGR_CF_FILE  = PHPMNGR_DIR / "cloudflare.json"
PHPMNGR_NC_FILE  = PHPMNGR_DIR / "namecheap.json"
PHPMNGR_SITES    = PHPMNGR_DIR / "sites.json"
PHPMNGR_TUNNELS  = PHPMNGR_DIR / "tunnels.json"

# Docker
CONTAINER_MAILPIT  = "killthehost-mailpit"
CONTAINER_MAILSRVR = "killthehost-mailserver"
IMG_MAILPIT        = "axllent/mailpit:latest"
IMG_MAILSRVR       = "mailserver/docker-mailserver:latest"

# Service ports
MAILPIT_SMTP  = 1025
MAILPIT_HTTP  = 8025
IMAP_PORT     = 143
SMTP_PORT     = 587      # STARTTLS submission

# Cloudflare API
CF_API = "https://api.cloudflare.com/client/v4"

# ─────────────────────────────────────────────────────────────────────────────
#  ENV / DOCKER HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def _env() -> dict:
    e = os.environ.copy()
    for p in ["/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
              os.path.expanduser("~/.docker/bin")]:
        if p not in e.get("PATH", ""):
            e["PATH"] = p + ":" + e.get("PATH", "")
    return e

def run(cmd: str, inp=None) -> tuple:
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, input=inp, env=_env())
    return r.stdout.strip(), r.stderr.strip(), r.returncode

def docker(cmd: str) -> tuple:
    return run(f"docker {cmd}")

def container_running(name: str) -> bool:
    out, _, rc = docker(f"inspect -f '{{{{.State.Running}}}}' {name} 2>/dev/null")
    return rc == 0 and out.strip().strip("'") == "true"

def container_exists(name: str) -> bool:
    _, _, rc = docker(f"inspect {name} 2>/dev/null")
    return rc == 0

def pull_image(image: str):
    out, _, rc = docker(f"image inspect {image} 2>/dev/null")
    if rc != 0:
        threading.Thread(target=lambda: docker(f"pull {image}"), daemon=True).start()

# ─────────────────────────────────────────────────────────────────────────────
#  PERSISTENCE
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "mode":      "dev",   # "dev" | "live"
    # live-mode fields — all REQUIRED before start_mailserver() is allowed
    "domain":    "",      # apex zone, e.g. "example.com"
    "zone_id":   "",      # Cloudflare zone ID for that domain
    "mail_host": "",      # FQDN, e.g. "mail.example.com"
    "public_ip": "",      # server's public IPv4
    "dkim_done": False,   # True once DKIM key has been uploaded
}

DEFAULT_SPAM = {
    "enabled":          True,
    "tag_score":        2.0,
    "spam_score":       5.0,
    "reject_score":     15.0,
    "quarantine":       True,
    "subject_tag":      "[SPAM]",
    "whitelist":        [],
    "blacklist":        [],
    "custom_rules":     "",
    "engines":          ["spamassassin"],
    "dkim_check":       True,
    "spf_check":        True,
    "block_known_bad":  True,
}

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try: return {**DEFAULT_CONFIG, **json.loads(CONFIG_FILE.read_text())}
        except: pass
    return dict(DEFAULT_CONFIG)

def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    try: os.chmod(CONFIG_FILE, 0o600)
    except: pass

def load_spam() -> dict:
    if SPAM_FILE.exists():
        try: return {**DEFAULT_SPAM, **json.loads(SPAM_FILE.read_text())}
        except: pass
    return dict(DEFAULT_SPAM)

def save_spam(s: dict):
    SPAM_FILE.write_text(json.dumps(s, indent=2))

def load_dns_status() -> dict:
    if DNS_FILE.exists():
        try: return json.loads(DNS_FILE.read_text())
        except: pass
    return {}

def save_dns_status(s: dict):
    DNS_FILE.write_text(json.dumps(s, indent=2))

def load_checklist() -> dict:
    if CHECKLIST_FILE.exists():
        try: return json.loads(CHECKLIST_FILE.read_text())
        except: pass
    return {}

def save_checklist(c: dict):
    CHECKLIST_FILE.write_text(json.dumps(c, indent=2))

KNOWN_VPS_PROVIDERS = {
    "ovh":        ("OVH",        "https://manager.us.ovhcloud.com → Hosted Private Cloud → VPS → your VPS → IP tab → click the ⋮ menu next to your IP → Modify Reverse DNS → enter mail.yourdomain.com"),
    "sysrescue":  ("OVH",        "https://manager.us.ovhcloud.com → Hosted Private Cloud → VPS → your VPS → IP tab → click the ⋮ menu next to your IP → Modify Reverse DNS"),
    "digitalocean": ("DigitalOcean", "Networking → Floating IPs or Droplet → More → Edit rDNS"),
    "vultr":      ("Vultr",      "Products → Network → Reverse DNS → Add entry"),
    "linode":     ("Linode",     "Networking tab of your Linode → Reverse DNS → Edit"),
    "akamai":     ("Akamai/Linode", "Networking tab → Reverse DNS"),
    "hetzner":    ("Hetzner",    "Server → IPs → Reverse DNS"),
    "contabo":    ("Contabo",    "VPS Control Panel → Reverse DNS"),
    "aws":        ("AWS",        "EC2 → Elastic IPs → Actions → Update Reverse DNS"),
    "amazonaws":  ("AWS",        "EC2 → Elastic IPs → Actions → Update Reverse DNS"),
    "google":     ("Google Cloud", "VPC Network → External IP addresses → Edit reverse DNS"),
    "azure":      ("Azure",      "Public IP resource → Configuration → Reverse FQDN"),
}

def _ptr_provider_hint(resolved: str, ip: str, expected: str) -> str:
    """Return a provider-specific PTR setup instruction based on the current rDNS value."""
    resolved_lower = resolved.lower()
    for key, (name, instructions) in KNOWN_VPS_PROVIDERS.items():
        if key in resolved_lower:
            instr = instructions.replace("yourdomain.com",
                                         expected.split(".")[-2] + "." + expected.split(".")[-1]
                                         if expected.count(".") >= 2 else "your domain")
            return (f"{name} VPS detected. Set PTR in your {name} control panel:\n"
                    f"{instr}\n"
                    f"Set the value to: {expected}")
    return (f"Contact your hosting provider and ask them to set a PTR (reverse DNS) "
            f"record for {ip} pointing to {expected}.")

CGNAT_RANGES = [
    # RFC 6598 shared address space (official CGNAT block)
    ("100.64.0.0",  "100.127.255.255"),
    # T-Mobile 5G Home Internet pools
    ("172.56.0.0",  "172.63.255.255"),
    # Other carrier pools commonly used for CGNAT
    ("10.0.0.0",    "10.255.255.255"),
]

CGNAT_CARRIERS = {
    "172.56.": "T-Mobile", "172.57.": "T-Mobile", "172.58.": "T-Mobile",
    "172.59.": "T-Mobile", "172.60.": "T-Mobile", "172.61.": "T-Mobile",
    "172.62.": "T-Mobile", "172.63.": "T-Mobile",
    "100.6":   "Carrier CGNAT", "100.7":  "Carrier CGNAT",
    "100.8":   "Carrier CGNAT", "100.9":  "Carrier CGNAT",
}

def detect_cgnat(ip: str) -> str | None:
    """
    Return carrier name if IP is CGNAT, else None.
    Checks known carrier blocks — T-Mobile 5G home internet in particular
    uses 172.56–63.x.x and cannot set PTR records or receive port 25.
    """
    if not ip:
        return None
    for prefix, carrier in CGNAT_CARRIERS.items():
        if ip.startswith(prefix):
            return carrier
    # RFC 6598
    if ip.startswith("100.6") or ip.startswith("100.7") or \
       ip.startswith("100.8") or ip.startswith("100.9"):
        return "Carrier CGNAT"
    return None

def check_ptr(public_ip: str, expected_host: str) -> dict:
    """Reverse-DNS lookup with CGNAT detection."""
    if not public_ip:
        return {"ok": False, "resolved": "", "expected": expected_host,
                "error": "No public IP configured", "cgnat": None}

    carrier = detect_cgnat(public_ip)
    if carrier:
        return {
            "ok":      False,
            "resolved": "",
            "expected": expected_host,
            "cgnat":   carrier,
            "error":   (
                f"{carrier} CGNAT detected. "
                "This carrier uses shared IP pools — PTR records cannot be set by customers "
                "and port 25 is blocked at the carrier level. "
                "Inbound SMTP from the internet is not possible on this connection."
            ),
        }

    try:
        import socket as _s
        resolved = _s.gethostbyaddr(public_ip)[0]
        ok = resolved.rstrip(".") == expected_host.rstrip(".")
        hint = None if ok else _ptr_provider_hint(resolved, public_ip, expected_host)
        return {"ok": ok, "resolved": resolved, "expected": expected_host,
                "cgnat": None, "hint": hint}
    except OSError:
        return {"ok": False, "resolved": "", "expected": expected_host,
                "cgnat": None, "error": "No PTR record set yet",
                "hint": f"Set a PTR record for {public_ip} → {expected_host} in your hosting control panel."}
    except Exception:
        return {"ok": False, "resolved": "", "expected": expected_host,
                "cgnat": None, "error": "No PTR record set yet",
                "hint": f"Set a PTR record for {public_ip} → {expected_host} in your hosting control panel."}

# ─────────────────────────────────────────────────────────────────────────────
#  SHARED CREDENTIAL READERS  (PHP-MNGR store — read-only)
# ─────────────────────────────────────────────────────────────────────────────

def phpmngr_cf_token() -> str:
    """Return the Cloudflare API token stored by PHP-MNGR. Never None — may be empty."""
    if PHPMNGR_CF_FILE.exists():
        try: return json.loads(PHPMNGR_CF_FILE.read_text()).get("token", "")
        except: pass
    return ""

def phpmngr_sites() -> dict:
    """Return PHP-MNGR sites registry."""
    if PHPMNGR_SITES.exists():
        try: return json.loads(PHPMNGR_SITES.read_text())
        except: pass
    return {}

def phpmngr_tunnels() -> dict:
    """Return PHP-MNGR tunnels registry (contains domain → zone_id mappings)."""
    if PHPMNGR_TUNNELS.exists():
        try: return json.loads(PHPMNGR_TUNNELS.read_text())
        except: pass
    return {}

# ─────────────────────────────────────────────────────────────────────────────
#  CLOUDFLARE API  (mirrors phpmanager.py's cf_call — uses shared token)
# ─────────────────────────────────────────────────────────────────────────────

def cf_call(method: str, path: str, data=None) -> tuple:
    """Call Cloudflare API. Returns (result, error_str)."""
    token = phpmngr_cf_token()
    if not token:
        return None, ("Cloudflare token not configured. "
                      "Set it in PHP-MNGR → Settings → Cloudflare first.")
    url  = CF_API + path
    body = json.dumps(data).encode() if data else None
    req  = _URLReq(url, data=body, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
        "User-Agent":    "KillTheHost-MAIL-SRVR/1.1",
    })
    try:
        with urlopen(req, timeout=15) as r:
            resp = json.loads(r.read().decode())
    except HTTPError as e:
        try:    resp = json.loads(e.read().decode())
        except: return None, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return None, f"Network error: {e}"
    if not resp.get("success"):
        errs = resp.get("errors", [])
        msg  = "; ".join(
            f"{er.get('code','')}: {er.get('message','')}" for er in errs
        ) or "Unknown Cloudflare error"
        return None, msg
    return resp.get("result"), None

def cf_list_zones() -> tuple:
    """Return list of [{id, name, status, active}], error."""
    zones, page = [], 1
    while True:
        result, err = cf_call("GET", f"/zones?per_page=50&page={page}")
        if err: return [], err
        batch = result if isinstance(result, list) else []
        if not batch: break
        for z in batch:
            zones.append({
                "id":     z.get("id", ""),
                "name":   z.get("name", ""),
                "status": z.get("status", ""),
                "active": z.get("status", "") == "active",
            })
        if len(batch) < 50: break
        page += 1
    return zones, None

def cf_get_zone_id(domain: str) -> tuple:
    """Resolve zone ID for domain (tries apex). Returns (zone_id, error)."""
    parts = domain.strip(".").split(".")
    for d in [domain, ".".join(parts[-2:])]:
        result, err = cf_call("GET", f"/zones?name={d}")
        if not err and result:
            zones = result if isinstance(result, list) else []
            if zones: return zones[0]["id"], None
    return None, f"Zone not found for {domain} — add it to Cloudflare first."

def cf_list_dns(zone_id: str, name: str = "", rtype: str = "") -> tuple:
    """List DNS records, optionally filtered. Returns (records, error)."""
    qs = ""
    if name:  qs += f"&name={name}"
    if rtype: qs += f"&type={rtype}"
    result, err = cf_call("GET", f"/zones/{zone_id}/dns_records?per_page=100{qs}")
    if err: return [], err
    return result if isinstance(result, list) else [], None

def cf_upsert_dns(zone_id: str, rtype: str, name: str,
                  content: str, proxied: bool = False,
                  ttl: int = 300, priority: int = None) -> tuple:
    """Create or update a DNS record. Returns (record_id, error)."""
    # Delete any conflicting records first
    existing, _ = cf_list_dns(zone_id, name=name, rtype=rtype)
    for rec in (existing or []):
        cf_call("DELETE", f"/zones/{zone_id}/dns_records/{rec['id']}")

    body = {"type": rtype, "name": name, "content": content,
            "proxied": proxied, "ttl": ttl}
    if priority is not None:
        body["priority"] = priority
    result, err = cf_call("POST", f"/zones/{zone_id}/dns_records", body)
    if err: return None, err
    return result.get("id", "") if isinstance(result, dict) else "", None

def cf_delete_dns(zone_id: str, rtype: str, name: str) -> str | None:
    """Delete all DNS records matching type+name. Returns error or None."""
    existing, err = cf_list_dns(zone_id, name=name, rtype=rtype)
    if err: return err
    for rec in (existing or []):
        _, err = cf_call("DELETE", f"/zones/{zone_id}/dns_records/{rec['id']}")
        if err: return err
    return None

# ─────────────────────────────────────────────────────────────────────────────
#  DOMAIN DISCOVERY  (reads from PHP-MNGR registries)
# ─────────────────────────────────────────────────────────────────────────────

def discover_domains() -> dict:
    """
    Return a merged dict of all zones available for mail assignment.
    Pulls from:
      1. PHP-MNGR tunnels registry (has domain + zone_id already resolved)
      2. PHP-MNGR sites registry (domain only, zone_id TBD via CF API)
      3. Cloudflare zones list (full account visibility)

    Returns {"example.com": {"zone_id": "...", "source": "tunnel|site|cf"}, ...}
    """
    domains: dict[str, dict] = {}

    # ── 1. PHP-MNGR tunnels (richest — already has zone info) ────────────────
    for t in phpmngr_tunnels().values():
        d = t.get("domain", "")
        if not d: continue
        # Normalise to apex
        apex = _apex(d)
        if apex not in domains:
            domains[apex] = {
                "zone_id": "",          # filled in below if not already known
                "source":  "tunnel",
                "account_id": t.get("account_id", ""),
            }

    # ── 2. PHP-MNGR sites ────────────────────────────────────────────────────
    for s in phpmngr_sites().values():
        d = s.get("domain", "")
        if not d: continue
        apex = _apex(d)
        if apex not in domains:
            domains[apex] = {"zone_id": "", "source": "site", "account_id": ""}

    # ── 3. Cloudflare zones (authoritative — always try) ─────────────────────
    zones, err = cf_list_zones()
    if not err:
        for z in zones:
            name = z["name"]
            if name not in domains:
                domains[name] = {"zone_id": z["id"], "source": "cf",
                                 "account_id": ""}
            else:
                # Enrich zone_id for tunnel/site entries
                if not domains[name].get("zone_id"):
                    domains[name]["zone_id"] = z["id"]

    return domains

def _apex(domain: str) -> str:
    """Return apex domain from any hostname."""
    parts = domain.strip(".").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else domain

# ─────────────────────────────────────────────────────────────────────────────
#  PUBLIC IP
# ─────────────────────────────────────────────────────────────────────────────

def get_public_ip() -> str:
    for url in ["https://api.ipify.org",
                "https://checkip.amazonaws.com",
                "https://icanhazip.com"]:
        try:
            with urlopen(url, timeout=6) as r:
                ip = r.read().decode().strip()
                if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip):
                    return ip
        except Exception:
            pass
    return ""

# ─────────────────────────────────────────────────────────────────────────────
#  DNS PROVISIONING  (the core of the mandatory domain link)
# ─────────────────────────────────────────────────────────────────────────────

REQUIRED_RECORDS = ["MX", "A", "SPF", "DMARC"]   # DKIM added after container start

def provision_dns(domain: str, zone_id: str, public_ip: str) -> dict:
    """
    Create / overwrite all required mail DNS records on the Cloudflare zone.

    Mail subdomain is always 'mail.{domain}'.
    All records are DNS-only (proxied=False) because Cloudflare CANNOT
    proxy SMTP (port 25/587) or IMAP (port 143/993).
    """
    mail_host = f"mail.{domain}"
    errors    = {}
    created   = {}

    # ── MX  (@  → mail.domain, priority 10) ──────────────────────────────────
    _, err = cf_upsert_dns(zone_id, "MX", domain, mail_host,
                           proxied=False, ttl=300, priority=10)
    if err: errors["MX"] = err
    else:   created["MX"] = f"{domain} → {mail_host} (pri 10)"

    # ── A   (mail → public_ip, DNS-only) ─────────────────────────────────────
    _, err = cf_upsert_dns(zone_id, "A", mail_host, public_ip,
                           proxied=False, ttl=300)
    if err: errors["A"] = err
    else:   created["A"] = f"{mail_host} → {public_ip}"

    # ── SPF TXT (@) ──────────────────────────────────────────────────────────
    # Merge with existing SPF if present; otherwise create fresh
    spf_val = f"v=spf1 mx a:{mail_host} ip4:{public_ip} ~all"
    existing_spf, _ = cf_list_dns(zone_id, name=domain, rtype="TXT")
    for rec in (existing_spf or []):
        if rec.get("content", "").startswith("v=spf1"):
            # Already has SPF — patch it to include our IP/hostname
            old = rec["content"]
            if f"a:{mail_host}" not in old:
                spf_val = old.rstrip(">").replace("~all", "").replace("-all", "").strip()
                spf_val += f" a:{mail_host} ip4:{public_ip} ~all"
            else:
                # Nothing to add
                created["SPF"] = f"existing SPF unchanged: {old}"
                spf_val = None
            break

    if spf_val:
        _, err = cf_upsert_dns(zone_id, "TXT", domain, spf_val,
                               proxied=False, ttl=300)
        if err: errors["SPF"] = err
        else:   created["SPF"] = spf_val

    # ── DMARC TXT (_dmarc.domain) ────────────────────────────────────────────
    dmarc_val = (f"v=DMARC1; p=quarantine; "
                 f"rua=mailto:postmaster@{domain}; "
                 f"ruf=mailto:postmaster@{domain}; fo=1")
    _, err = cf_upsert_dns(zone_id, "TXT", f"_dmarc.{domain}", dmarc_val,
                           proxied=False, ttl=300)
    if err: errors["DMARC"] = err
    else:   created["DMARC"] = dmarc_val

    status = {
        "provisioned": datetime.now().isoformat(),
        "domain":      domain,
        "zone_id":     zone_id,
        "mail_host":   mail_host,
        "public_ip":   public_ip,
        "created":     created,
        "errors":      errors,
        "dkim":        "pending",
    }
    save_dns_status(status)
    cfg = load_config()
    cfg.update({
        "domain":    domain,
        "zone_id":   zone_id,
        "mail_host": mail_host,
        "public_ip": public_ip,
        "dkim_done": False,
    })
    save_config(cfg)
    return {"ok": not errors, "created": created, "errors": errors}

DKIM_STATUS_FILE = DATA_DIR / "dkim_job.json"
_dkim_lock = threading.Lock()

def _dkim_set_status(state: str, msg: str = "", record: str = "", value: str = ""):
    DKIM_STATUS_FILE.write_text(json.dumps({
        "state": state, "msg": msg,
        "record": record, "value": value,
        "ts": datetime.now().isoformat(),
    }))

def dkim_get_status() -> dict:
    if DKIM_STATUS_FILE.exists():
        try: return json.loads(DKIM_STATUS_FILE.read_text())
        except: pass
    return {"state": "idle"}

def provision_dkim(domain: str, zone_id: str) -> dict:
    """
    Async DKIM provisioning — runs in a background thread and writes
    progress to DKIM_STATUS_FILE so the UI can poll without timing out.
    Returns immediately with {"ok": True, "pending": True}.
    """
    if not container_running(CONTAINER_MAILSRVR):
        return {"ok": False, "error": "Mail server container not running"}
    with _dkim_lock:
        status = dkim_get_status()
        if status.get("state") == "running":
            return {"ok": True, "pending": True, "msg": "Already running…"}
    _dkim_set_status("running", "Starting DKIM key generation…")
    threading.Thread(target=_provision_dkim_bg,
                     args=(domain, zone_id), daemon=True).start()
    return {"ok": True, "pending": True, "msg": "DKIM generation started"}

def _provision_dkim_bg(domain: str, zone_id: str):
    """Background worker — writes status at each step so the UI can poll."""
    try:
        _dkim_set_status("running", "Running: setup config dkim…")

        # Try setup command (v12+ syntax first, then legacy)
        gen_err = ""
        for cmd in [
            f"exec {CONTAINER_MAILSRVR} setup config dkim domain {domain}",
            f"exec {CONTAINER_MAILSRVR} setup config dkim",
        ]:
            _, gen_err, rc = docker(cmd)
            if rc == 0:
                break

        _dkim_set_status("running", "Waiting for key file to be written…")
        time.sleep(4)   # give the container time to write the file

        # Known key file paths across docker-mailserver versions
        candidates = [
            f"/var/mail-state/lib/opendkim/{domain}/mail.txt",
            f"/var/mail-state/lib/opendkim/keys/{domain}/mail.txt",
            f"/tmp/docker-mailserver/opendkim/keys/{domain}/mail.txt",
            f"/tmp/docker-mailserver/opendkim/keys/{domain}/mail._domainkey.txt",
            f"/var/mail-state/lib/opendkim/{domain}.txt",
        ]

        out = ""
        _dkim_set_status("running", "Searching for key file…")
        for kp in candidates:
            kout, _, krc = docker(f"exec {CONTAINER_MAILSRVR} cat {kp} 2>/dev/null")
            if krc == 0 and kout.strip():
                out = kout
                break

        if not out:
            # Scoped find — only search likely directories, not /proc /sys etc.
            _dkim_set_status("running", "Scanning opendkim directories…")
            for search_root in ["/var/mail-state", "/tmp/docker-mailserver", "/etc/opendkim"]:
                fout, _, frc = docker(
                    f"exec {CONTAINER_MAILSRVR} "
                    f"find {search_root} -name '*.txt' 2>/dev/null"
                )
                if frc == 0 and fout.strip():
                    first = fout.strip().splitlines()[0]
                    out, _, _ = docker(f"exec {CONTAINER_MAILSRVR} cat {first} 2>/dev/null")
                    if out.strip():
                        break

        if not out:
            tried = "\n".join(candidates)
            _dkim_set_status("error",
                f"Could not find DKIM key file.\n"
                f"setup stderr: {gen_err or '(none)'}\n"
                f"Paths tried:\n{tried}")
            return

        # Parse BIND zone file format: ( "v=DKIM1; ..." "p=XXX" )
        parts = re.findall(r'"([^"]+)"', out)
        if not parts:
            _dkim_set_status("error", f"Could not parse key file content:\n{out[:300]}")
            return

        _dkim_set_status("running", "Uploading TXT record to Cloudflare DNS…")
        dkim_val     = "".join(parts)
        record_name  = f"mail._domainkey.{domain}"

        _, err = cf_upsert_dns(zone_id, "TXT", record_name, dkim_val,
                               proxied=False, ttl=300)
        if err:
            _dkim_set_status("error",
                f"DNS upload failed: {err}\n"
                f"Key value (add manually):\n{dkim_val[:200]}")
            return

        # Persist
        dns_s = load_dns_status()
        dns_s["dkim"] = "provisioned"
        dns_s["dkim_record"] = record_name
        dns_s["dkim_value"]  = dkim_val[:60] + "…"
        save_dns_status(dns_s)

        cfg = load_config()
        cfg["dkim_done"] = True
        save_config(cfg)

        _dkim_set_status("done",
            f"DKIM record uploaded to DNS",
            record=record_name,
            value=dkim_val[:80] + "…")

    except Exception as e:
        _dkim_set_status("error", f"Unexpected error: {e}")



def verify_dns_records(domain: str, zone_id: str) -> dict:
    """
    Check which required records are present in Cloudflare DNS.
    Returns dict of {record_name: True|False|"missing"}.
    """
    mail_host = f"mail.{domain}"
    results   = {}

    checks = [
        ("MX",    domain,                "MX"),
        ("A",     mail_host,             "A"),
        ("SPF",   domain,                "TXT"),
        ("DMARC", f"_dmarc.{domain}",   "TXT"),
        ("DKIM",  f"mail._domainkey.{domain}", "TXT"),
    ]
    for label, name, rtype in checks:
        recs, err = cf_list_dns(zone_id, name=name, rtype=rtype)
        if err:
            results[label] = {"ok": False, "error": err}
        elif recs:
            results[label] = {"ok": True, "value": recs[0].get("content", "")[:80]}
        else:
            results[label] = {"ok": False, "error": "Record not found"}

    return results

# ─────────────────────────────────────────────────────────────────────────────
#  CONTAINER MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

def _domain_ready() -> tuple:
    """Returns (domain, zone_id, public_ip, error). Checks all required fields."""
    cfg = load_config()
    if not cfg.get("domain"):
        return None, None, None, (
            "No domain configured. Go to the Domain tab, pick a Cloudflare zone, "
            "and click Provision DNS Records before starting the mail server."
        )
    if not cfg.get("zone_id"):
        return None, None, None, "Zone ID missing — re-run Provision DNS Records."
    if not cfg.get("public_ip"):
        return None, None, None, (
            "Public IP not set — re-run Provision DNS Records so the A record "
            "is created with your current public IP."
        )
    return cfg["domain"], cfg["zone_id"], cfg["public_ip"], None

def start_mailpit() -> dict:
    if container_running(CONTAINER_MAILPIT):
        return {"ok": True, "msg": "Mailpit already running"}
    pull_image(IMG_MAILPIT)
    if container_exists(CONTAINER_MAILPIT):
        docker(f"rm -f {CONTAINER_MAILPIT}")
    _, err, rc = run(
        f"docker run -d --name {CONTAINER_MAILPIT} "
        f"-p {MAILPIT_SMTP}:1025 -p {MAILPIT_HTTP}:8025 "
        f"--restart unless-stopped {IMG_MAILPIT}"
    )
    if rc != 0: return {"ok": False, "error": err}
    return {"ok": True, "msg": f"Mailpit started — SMTP :{MAILPIT_SMTP}  UI :{MAILPIT_HTTP}"}

def stop_mailpit() -> dict:
    if container_exists(CONTAINER_MAILPIT):
        docker(f"stop {CONTAINER_MAILPIT}")
        docker(f"rm   {CONTAINER_MAILPIT}")
    return {"ok": True}

def start_mailserver() -> dict:
    domain, zone_id, public_ip, err = _domain_ready()
    if err:
        return {"ok": False, "error": err}

    cfg  = load_config()
    spam = load_spam()

    if container_running(CONTAINER_MAILSRVR):
        return {"ok": True, "msg": "Mail server already running"}

    pull_image(IMG_MAILSRVR)
    if container_exists(CONTAINER_MAILSRVR):
        docker(f"rm -f {CONTAINER_MAILSRVR}")

    mail_host = cfg.get("mail_host") or f"mail.{domain}"

    # Auto-detect PTR for the public IP. If it exists and differs from mail_host,
    # use the PTR as the SMTP hostname (HELO/EHLO must match PTR for deliverability).
    # Also update the MX record to point to the PTR hostname so mail routes correctly.
    ptr_hostname = ""
    try:
        import socket as _sock
        ptr_hostname = _sock.gethostbyaddr(public_ip)[0].rstrip(".")
    except Exception:
        pass

    # Use PTR as the Postfix hostname if:
    # 1. PTR exists, AND
    # 2. PTR differs from mail_host (user couldn't change it), AND
    # 3. PTR looks like a valid FQDN (not an error string)
    if ptr_hostname and ptr_hostname != mail_host and "." in ptr_hostname:
        smtp_hostname = ptr_hostname
        # Update MX record to use the PTR hostname so external servers can deliver
        cf_upsert_dns(zone_id, "MX", domain, smtp_hostname,
                      proxied=False, ttl=300, priority=10)
        # Update A record to resolve PTR hostname → public_ip
        cf_upsert_dns(zone_id, "A", smtp_hostname, public_ip,
                      proxied=False, ttl=300)
        # Save the resolved hostname back to config
        cfg["smtp_hostname"] = smtp_hostname
        save_config(cfg)
    else:
        smtp_hostname = mail_host

    for sub in ["mail-data", "mail-state", "mail-logs", "dms-config"]:
        (DATA_DIR / sub).mkdir(parents=True, exist_ok=True)

    sa = 1 if spam["enabled"] and "spamassassin" in spam["engines"] else 0
    rs = 1 if spam["enabled"] and "rspamd"       in spam["engines"] else 0

    # Subject tag may contain brackets/spaces — must be shell-quoted
    subject_tag = spam["subject_tag"].replace("'", "")  # strip single quotes for safety

    env_flags = " ".join([
        "-e ONE_DIR=1",
        "-e ENABLE_CLAMAV=0",
        "-e ENABLE_FAIL2BAN=1",
        "-e ENABLE_POSTGREY=0",
        "-e ENABLE_OPENDKIM=1",
        "-e ENABLE_OPENDMARC=1",
        "-e ENABLE_AMAVIS=0",          # amavis blocks attachments; OpenDKIM handles DKIM signing standalone
        f"-e ENABLE_SPAMASSASSIN={sa}",
        f"-e ENABLE_RSPAMD={rs}",
        f"-e SPAMASSASSIN_SPAM_TO_INBOX={1 if spam['quarantine'] else 0}",
        f"-e MOVE_SPAM_TO_JUNK={1 if spam['quarantine'] else 0}",
        f"-e SA_TAG={spam['tag_score']}",
        f"-e SA_TAG2={spam['spam_score']}",
        f"-e SA_KILL={spam['reject_score']}",
        f"-e 'SA_SPAM_SUBJECT={subject_tag}'",
        "-e POSTFIX_INET_PROTOCOLS=ipv4",
        f"-e OVERRIDE_HOSTNAME={smtp_hostname}",
        "-e LOG_LEVEL=info",
    ])
    vols = " ".join([
        f"-v '{DATA_DIR}/mail-data:/var/mail'",
        f"-v '{DATA_DIR}/mail-state:/var/mail-state'",
        f"-v '{DATA_DIR}/mail-logs:/var/log/mail'",
        f"-v '{DATA_DIR}/dms-config:/tmp/docker-mailserver'",
    ])
    ports = f"-p {IMAP_PORT}:143 -p 993:993 -p {SMTP_PORT}:587 -p 25:25"

    cmd = (
        f"docker run -d --name {CONTAINER_MAILSRVR} "
        f"--hostname {smtp_hostname} {ports} {vols} {env_flags} "
        f"--cap-add NET_ADMIN --restart unless-stopped {IMG_MAILSRVR}"
    )
    out, err, rc = run(cmd)
    if rc != 0:
        return {"ok": False, "error": f"docker run failed: {err or out}"}

    # DKIM generation runs 35 s after start (container needs to initialise)
    if not cfg.get("dkim_done"):
        threading.Thread(target=_deferred_dkim,
                         args=(domain, zone_id, 35), daemon=True).start()

    # Apply whitelist/blacklist rules
    threading.Thread(target=_apply_spam_rules, args=(20,), daemon=True).start()

    # Auto-apply amavis passthrough config after container is ready
    def _fix_amavis_auto(delay: int):
        time.sleep(delay)
        if not container_running(CONTAINER_MAILSRVR): return
        cfg = (
            "use strict;\n"
            "# KillTheHost: pass all attachment types\n"
            "$final_banned_destiny = D_PASS;\n"
            "$final_spam_destiny   = D_PASS;\n"
            "$final_virus_destiny  = D_PASS;\n"
            "@bypass_banned_checks_maps = (1);\n"
            "@bypass_spam_checks_maps   = (1);\n"
            "1;\n"
        )
        try:
            # Write config using python inside container (avoids shell escaping issues)
            encoded = cfg.replace("'", "'\\''")  # escape single quotes
            docker(
                f"exec {CONTAINER_MAILSRVR} bash -c "
                f"'python3 -c \"import sys; open(chr(47)+chr(101)+chr(116)+chr(99)+"
                f"chr(47)+chr(97)+chr(109)+chr(97)+chr(118)+chr(105)+chr(115)+"
                f"chr(47)+chr(99)+chr(111)+chr(110)+chr(102)+chr(46)+chr(100)+"
                f"chr(47)+chr(57)+chr(57)+chr(45)+chr(109)+chr(115)+chr(46)+chr(99)+chr(102),"
                f"chr(119)).write(sys.stdin.read())\" <<\\\"AMAVIS_EOF\\\"\\n{cfg}\\nAMAVIS_EOF'"
            )
            # Simpler: use tee
            import subprocess as _sp
            _sp.run(
                ["docker", "exec", "-i", CONTAINER_MAILSRVR,
                 "bash", "-c",
                 "cat > /etc/amavis/conf.d/99-mailsrvr-override.cf"],
                input=cfg.encode(), capture_output=True
            )
            docker(f"exec {CONTAINER_MAILSRVR} bash -c "
                   "'amavisd-new reload 2>/dev/null || supervisorctl restart amavis 2>/dev/null || true'")
        except Exception:
            pass

    threading.Thread(target=_fix_amavis_auto, args=(35,), daemon=True).start()

    return {
        "ok": True,
        "msg": (f"Mail server starting for {mail_host} — "
                f"IMAP :{IMAP_PORT}  SMTP :{SMTP_PORT}"),
    }

def stop_mailserver() -> dict:
    if container_exists(CONTAINER_MAILSRVR):
        docker(f"stop {CONTAINER_MAILSRVR}")
        docker(f"rm   {CONTAINER_MAILSRVR}")
    return {"ok": True}

def do_start() -> dict:
    return start_mailpit() if load_config()["mode"] == "dev" else start_mailserver()

def do_stop() -> dict:
    return stop_mailpit() if load_config()["mode"] == "dev" else stop_mailserver()

def _deferred_dkim(domain: str, zone_id: str, delay: int):
    time.sleep(delay)
    provision_dkim(domain, zone_id)

def run_diagnostics() -> dict:
    cfg    = load_config()
    domain = cfg.get("domain", "")
    ip     = cfg.get("public_ip", "")
    results = {}

    # 1. Container running?
    results["container"] = {
        "ok": container_running(CONTAINER_MAILSRVR),
        "label": "docker-mailserver container",
    }

    # 2. Port 25 / Postfix running — check host socket AND inside container
    p25_host = False
    banner = ""
    try:
        with socket.create_connection(("127.0.0.1", 25), timeout=4) as s:
            banner = s.recv(256).decode(errors="replace").strip()
            p25_host = banner.startswith("220")
    except Exception:
        pass
    p25_container = False
    if not p25_host and container_running(CONTAINER_MAILSRVR):
        out, _, rc = docker(f"exec {CONTAINER_MAILSRVR} postfix status 2>/dev/null")
        p25_container = rc == 0 and "running" in out.lower()
        if not p25_container:
            out2, _, _ = docker(f"exec {CONTAINER_MAILSRVR} ss -tlnp 2>/dev/null | grep ':25 '")
            p25_container = bool(out2.strip())
    p25_ok = p25_host or p25_container
    results["port25_local"] = {
        "ok":    p25_ok,
        "label": "Port 25 / Postfix running",
        "value": (banner[:80] if p25_host
                  else "Postfix running inside container (port may still be binding)" if p25_container
                  else "Not yet listening — container may still be starting (takes 60-90 s)"),
    }

    # 3. Mailboxes
    accs = []
    if container_running(CONTAINER_MAILSRVR):
        out, _, rc = docker(f"exec {CONTAINER_MAILSRVR} setup email list")
        for line in out.splitlines():
            m = re.search(r'[\w.+\-]+@[\w.\-]+', line)
            if m: accs.append(m.group(0).lower())
    results["mailboxes"] = {
        "ok":    bool(accs),
        "label": "Mailboxes configured in Postfix",
        "value": ", ".join(accs) if accs else "None found",
    }

    # 4. Virtual domain — postconf may return a file path; read file if needed
    vdom_ok  = False
    vdom_val = ""
    if domain and container_running(CONTAINER_MAILSRVR):
        out, _, rc = docker(f"exec {CONTAINER_MAILSRVR} postconf virtual_mailbox_domains")
        vdom_val = out.strip()
        if domain in vdom_val:
            vdom_ok = True
        elif "/vhost" in vdom_val or "postfix/vhost" in vdom_val:
            # Value is a file reference — read the actual vhost file
            vhost_out, _, _ = docker(
                f"exec {CONTAINER_MAILSRVR} cat /etc/postfix/vhost 2>/dev/null")
            if domain in vhost_out:
                vdom_ok  = True
                vdom_val = f"{domain} found in /etc/postfix/vhost"
            elif accs:
                # Auto-add domain to vhost and reload Postfix
                # Auto-add domain to vhost and reload Postfix
                add_cmd = f"echo '{domain}' >> /etc/postfix/vhost && postfix reload"
                docker(f"exec {CONTAINER_MAILSRVR} bash -c \"{add_cmd}\"")
                vdom_val = f"Auto-added {domain} to /etc/postfix/vhost and reloaded"
            else:
                vdom_val = f"/etc/postfix/vhost exists but {domain} not in it yet"
        results["virtual_domain"] = {
            "ok":    vdom_ok,
            "label": f"Postfix accepts @{domain}",
            "value": vdom_val[:120],
        }
    else:
        results["virtual_domain"] = {
            "ok": False, "label": f"Postfix accepts @{domain}",
            "value": "Container not running",
        }

    # 5. Mail log
    mail_log = ""
    if container_running(CONTAINER_MAILSRVR):
        for lp in ["/var/log/mail/mail.log", "/var/log/mail.log", "/var/mail-state/log/mail.log"]:
            out, _, rc = docker(f"exec {CONTAINER_MAILSRVR} tail -30 {lp} 2>/dev/null")
            if rc == 0 and out.strip():
                mail_log = out; break
        if not mail_log:
            out, _, rc = docker(
                f"exec {CONTAINER_MAILSRVR} journalctl -u postfix --no-pager -n 30 2>/dev/null")
            if rc == 0: mail_log = out
    results["mail_log"] = {
        "ok":    bool(mail_log),
        "label": "Recent mail log",
        "value": mail_log or "No log found",
    }

    # 6. UFW / firewall
    ufw_out, _, _ = run("ufw status 2>/dev/null || iptables -L INPUT -n 2>/dev/null | head -30")
    results["firewall"] = {
        "ok":    None,
        "label": "Firewall rules (port 25)",
        "value": ufw_out[:400] if ufw_out.strip() else "Could not read firewall rules",
    }

    # 7. PTR — ok if any PTR resolves (auto-fix uses whatever PTR exists)
    ptr_ok  = False
    ptr_val = ""
    if ip:
        try:
            import socket as _sock
            ptr_val = _sock.gethostbyaddr(ip)[0].rstrip(".")
            ptr_ok  = bool(ptr_val) and "." in ptr_val
        except Exception as e:
            ptr_val = str(e)
    smtp_host = cfg.get("smtp_hostname") or cfg.get("mail_host", "")
    results["ptr"] = {
        "ok":    ptr_ok,
        "label": f"PTR record ({ip})",
        "value": (f"{ptr_val} ✓" if ptr_val == smtp_host
                  else f"{ptr_val} → auto-used as SMTP hostname" if ptr_ok
                  else "No PTR record"),
    }

    # 8. MX resolves to our IP
    mx_ok = False
    mx_val = ""
    if domain:
        try:
            import socket as _sock2
            # Always check mail.{domain} — this is the actual MX A record
            # smtp_hostname may be the PTR (e.g. contabo hostname) which
            # correctly resolves to 127.0.1.1 in /etc/hosts on the server itself
            mail_a_host = f"mail.{domain}"
            resolved    = _sock2.gethostbyname(mail_a_host)
            mx_ok       = resolved == ip
            mx_val      = f"{mail_a_host} → {resolved} (expected {ip})"
        except Exception as e:
            mx_val = str(e)
    results["mx_resolves"] = {
        "ok":    mx_ok,
        "label": "A record resolves to this server",
        "value": mx_val or "Could not resolve",
    }

    # ── 9. Outbound port 25 — can we reach external SMTP servers? ────────────
    # Try connecting to a known reliable SMTP server on port 25.
    # If this times out, the VPS provider is blocking outbound port 25.
    out25_ok = False
    out25_val = ""
    try:
        with socket.create_connection(("alt1.gmail-smtp-in.l.google.com", 25), timeout=8) as ts:
            banner25 = ts.recv(128).decode(errors="replace").strip()
            out25_ok  = banner25.startswith("220")
            out25_val = banner25[:80]
    except socket.timeout:
        out25_val = (
            "TIMED OUT — OVH blocks outbound port 25 by default on VPS plans.\n"
            "To fix: log into https://manager.us.ovhcloud.com → open a support ticket\n"
            "and request 'Unblock outbound port 25 (SMTP) for VPS'. They typically\n"
            "approve it within a few hours once you explain it's for a legitimate mail server.\n"
            "Until unblocked, outbound mail will queue and eventually bounce."
        )
    except Exception as e:
        out25_val = f"Could not test: {e}"

    results["outbound_port25"] = {
        "ok":    out25_ok,
        "label": "Outbound port 25 (can reach Gmail SMTP)",
        "value": out25_val,
    }

    return {
        "ok":     all(v.get("ok") for v in results.values() if v.get("ok") is not None),
        "checks": results,
        "domain": domain,
        "ip":     ip,
    }


def get_status() -> dict:
    cfg  = load_config()
    mode = cfg["mode"]
    if mode == "dev":
        running   = container_running(CONTAINER_MAILPIT)
        ports     = {"smtp": MAILPIT_SMTP, "ui": MAILPIT_HTTP}
        msg_count = _mailpit_count()
        acc_count = None
    else:
        running   = container_running(CONTAINER_MAILSRVR)
        ports     = {"smtp": SMTP_PORT, "imap": IMAP_PORT}
        msg_count = None
        acc_count = len(load_accounts())

    domain_ok = bool(cfg.get("domain") and cfg.get("zone_id"))

    return {
        "mode":       mode,
        "running":    running,
        "ports":      ports,
        "domain":     cfg.get("domain", ""),
        "mail_host":  cfg.get("mail_host", ""),
        "public_ip":  cfg.get("public_ip", ""),
        "zone_id":    cfg.get("zone_id", ""),
        "domain_ok":  domain_ok,
        "dkim_done":  cfg.get("dkim_done", False),
        "msg_count":  msg_count,
        "acc_count":  acc_count,
        "dns_status": load_dns_status(),
        "version":    VERSION,
    }

# ─────────────────────────────────────────────────────────────────────────────
#  ACCOUNT MANAGEMENT  (docker-mailserver)
# ─────────────────────────────────────────────────────────────────────────────

ACCOUNTS_FILE = DATA_DIR / "accounts.json"

def load_accounts() -> list:
    if ACCOUNTS_FILE.exists():
        try: return json.loads(ACCOUNTS_FILE.read_text())
        except: pass
    return []

def save_accounts(acc: list):
    ACCOUNTS_FILE.write_text(json.dumps(acc, indent=2))
    try: os.chmod(ACCOUNTS_FILE, 0o600)
    except: pass

def account_add(email_addr: str, password: str) -> dict:
    if not container_running(CONTAINER_MAILSRVR):
        return {"ok": False, "error": "Mail server not running"}
    _, err, rc = docker(
        f"exec {CONTAINER_MAILSRVR} setup email add {email_addr} '{password}'"
    )
    if rc != 0:
        return {"ok": False, "error": err or "setup email add failed"}
    accs = [a for a in load_accounts() if a["email"] != email_addr]
    accs.append({"email": email_addr, "password": password,
                 "created": datetime.now().strftime("%Y-%m-%d %H:%M")})
    save_accounts(accs)
    return {"ok": True}

def account_del(email_addr: str) -> dict:
    if container_running(CONTAINER_MAILSRVR):
        docker(f"exec {CONTAINER_MAILSRVR} setup email del {email_addr}")
    save_accounts([a for a in load_accounts() if a["email"] != email_addr])
    return {"ok": True}

def account_list_sync() -> list:
    if not container_running(CONTAINER_MAILSRVR):
        return load_accounts()
    out, _, rc = docker(f"exec {CONTAINER_MAILSRVR} setup email list")
    if rc != 0:
        return load_accounts()
    local = {a["email"]: a for a in load_accounts()}
    result = []
    seen   = set()
    for line in out.splitlines():
        # Line format: "* user@domain.com ( 0 / ~ ) [0%]"
        # Extract only the email token — split on whitespace, find the @-containing part
        m = re.search(r'[\w.+\-]+@[\w.\-]+', line)
        if not m:
            continue
        addr = m.group(0).lower()
        if addr in seen:
            continue
        seen.add(addr)
        stored = local.get(addr, {"email": addr, "password": "", "created": ""})
        result.append(stored)
        # Keep local store in sync with what the container actually has
        if addr not in local:
            local[addr] = stored
    # Persist any newly discovered accounts back to disk
    if result:
        save_accounts(list(local.values()))
    return result

# ─────────────────────────────────────────────────────────────────────────────
#  SPAM MANAGEMENT
# ─────────────────────────────────────────────────────────────────────────────

def _apply_spam_rules(delay=0):
    if delay: time.sleep(delay)
    if not container_running(CONTAINER_MAILSRVR): return
    spam  = load_spam()
    lines = ["# KillTheHost MAIL-SRVR — generated SpamAssassin rules", ""]
    for addr in spam.get("whitelist", []):
        lines.append(f"whitelist_from {addr}")
    for addr in spam.get("blacklist", []):
        lines.append(f"blacklist_from {addr}")
    custom = spam.get("custom_rules", "").strip()
    if custom:
        lines += ["", "# Custom rules", custom]
    content = "\n".join(lines).replace("'", "'\\''")
    run(f"docker exec {CONTAINER_MAILSRVR} bash -c "
        f"\"printf '%s\\n' '{content}' > "
        f"/tmp/docker-mailserver/spamassassin-rules.cf\"")

def apply_spam_config(new_spam: dict = None) -> dict:
    spam = load_spam()
    if new_spam: spam.update(new_spam)
    save_spam(spam)
    if load_config()["mode"] != "live":
        return {"ok": True, "msg": "Saved. Applies in Live mode."}
    _apply_spam_rules()
    return {"ok": True, "msg": "Rules written to container."}

# ─────────────────────────────────────────────────────────────────────────────
#  MAILPIT API  (dev mode)
# ─────────────────────────────────────────────────────────────────────────────

def _mp(path: str, method="GET", body=None):
    url = f"http://localhost:{MAILPIT_HTTP}/api{path}"
    try:
        data = json.dumps(body).encode() if body else None
        hdrs = {"Content-Type": "application/json"} if body else {}
        req  = _URLReq(url, data=data, headers=hdrs, method=method)
        with urlopen(req, timeout=5) as r:
            raw = r.read().decode()
            return (json.loads(raw) if raw.strip() else {}), None
    except HTTPError as e: return None, f"HTTP {e.code}"
    except Exception as e: return None, str(e)

def _mailpit_count() -> int:
    d, _ = _mp("/v1/messages?limit=1")
    return d.get("total", 0) if d else 0

def mailpit_list(page=1, limit=25, query="") -> dict:
    start = (page - 1) * limit
    path  = (f"/v1/search?query={query}&start={start}&limit={limit}"
             if query else f"/v1/messages?start={start}&limit={limit}")
    d, err = _mp(path)
    if err: return {"ok": False, "error": err, "messages": [], "total": 0}
    msgs = []
    for m in (d.get("messages") or []):
        msgs.append({
            "id":              m.get("ID", ""),
            "from":            _fa(m.get("From", {})),
            "to":              ", ".join(_fa(a) for a in (m.get("To") or [])),
            "subject":         m.get("Subject", "(no subject)"),
            "date":            m.get("Created", ""),
            "read":            m.get("Read", False),
            "has_attachments": bool(m.get("Attachments", 0)),
        })
    return {"ok": True, "messages": msgs, "total": d.get("total", 0)}

def mailpit_get(msg_id: str) -> dict:
    d, err = _mp(f"/v1/message/{msg_id}")
    if err: return {"ok": False, "error": err}
    _mp("/v1/messages", method="PUT", body={"IDs": [msg_id], "Read": True})
    return {
        "ok":          True,
        "id":          d.get("ID", ""),
        "from":        _fa(d.get("From", {})),
        "to":          ", ".join(_fa(a) for a in (d.get("To") or [])),
        "subject":     d.get("Subject", "(no subject)"),
        "date":        d.get("Created", ""),
        "text":        d.get("Text", ""),
        "html":        d.get("HTML", ""),
        "attachments": [a.get("FileName","") for a in (d.get("Attachments") or [])],
        "size":        d.get("Size", 0),
    }

def mailpit_delete(ids: list) -> dict:
    _, err = _mp("/v1/messages", method="DELETE", body={"IDs": ids})
    return {"ok": err is None, "error": err}

def mailpit_delete_all() -> dict:
    _, err = _mp("/v1/messages", method="DELETE", body={"IDs": []})
    return {"ok": err is None, "error": err}

def _fa(a) -> str:
    if isinstance(a, dict):
        name = a.get("Name", ""); addr = a.get("Address", "")
        return f"{name} <{addr}>" if name else addr
    return str(a)

# ─────────────────────────────────────────────────────────────────────────────
#  IMAP CLIENT  (live mode)
# ─────────────────────────────────────────────────────────────────────────────

def imap_list(account: str, folder="INBOX", page=1, limit=25, query="") -> dict:
    acc = next((a for a in load_accounts() if a["email"] == account), None)
    if not acc: return {"ok": False, "error": "Account not found", "messages": [], "total": 0, "unread": 0}
    try:
        conn = imaplib.IMAP4("127.0.0.1", IMAP_PORT)
        conn.login(acc["email"], acc["password"])
        conn.select(folder, readonly=True)
        _, data = conn.search(None, "TEXT", f'"{query}"') if query else conn.search(None, "ALL")
        nums  = data[0].split() if data[0] else []
        total = len(nums)
        # Count unread via UNSEEN
        unread = 0
        try:
            _, ud = conn.search(None, "UNSEEN")
            unread = len(ud[0].split()) if ud and ud[0] else 0
        except Exception:
            pass
        nums  = list(reversed(nums))[(page-1)*limit: page*limit]
        msgs  = []
        for num in nums:
            try:
                _, hd = conn.fetch(num, "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])")
                if not hd or not isinstance(hd[0], tuple): continue
                msg  = _email_mod.message_from_bytes(hd[0][1])
                flags= hd[0][0].decode() if isinstance(hd[0][0], bytes) else str(hd[0][0])
                msgs.append({
                    "id":      num.decode(), "read": "\\Seen" in flags,
                    "from":    _dh(msg.get("From","")),
                    "to":      _dh(msg.get("To","")),
                    "subject": _dh(msg.get("Subject","(no subject)")),
                    "date":    msg.get("Date",""), "has_attachments": False,
                })
            except Exception: continue
        conn.logout()
        return {"ok": True, "messages": msgs, "total": total, "unread": unread}
    except Exception as e:
        return {"ok": False, "error": str(e), "messages": [], "total": 0, "unread": 0}

def imap_get(account: str, msg_id: str, folder="INBOX") -> dict:
    acc = next((a for a in load_accounts() if a["email"] == account), None)
    if not acc: return {"ok": False, "error": "Account not found"}
    try:
        conn = imaplib.IMAP4("127.0.0.1", IMAP_PORT)
        conn.login(acc["email"], acc["password"])
        conn.select(folder)
        conn.store(msg_id, "+FLAGS", "\\Seen")
        _, data = conn.fetch(msg_id, "(RFC822)")
        msg  = _email_mod.message_from_bytes(data[0][1])
        text = html_b = ""
        attachments = []
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                cd = str(part.get("Content-Disposition",""))
                if "attachment" in cd:
                    fn = part.get_filename()
                    if fn: attachments.append(_dh(fn))
                elif ct == "text/plain"  and not text:   text   = _pl(part)
                elif ct == "text/html"   and not html_b: html_b = _pl(part)
        else:
            ct = msg.get_content_type()
            if ct == "text/html": html_b = _pl(msg)
            else:                 text   = _pl(msg)
        conn.logout()
        return {"ok": True, "id": msg_id,
                "from": _dh(msg.get("From","")), "to": _dh(msg.get("To","")),
                "subject": _dh(msg.get("Subject","(no subject)")),
                "date": msg.get("Date",""), "text": text, "html": html_b,
                "attachments": attachments, "size": len(data[0][1])}
    except Exception as e: return {"ok": False, "error": str(e)}

def _imap_find_folder(conn, keywords: list) -> str:
    """Find a folder name matching any of the keywords."""
    try:
        _, lst = conn.list()
        for item in (lst or []):
            txt = item.decode(errors="replace") if isinstance(item, bytes) else item
            upper = txt.upper()
            for kw in keywords:
                if kw.upper() in upper:
                    for sep in ['"."', '"/"', ' ']:
                        if sep in txt:
                            name = txt.rsplit(sep, 1)[-1].strip().strip('"')
                            if name: return name
    except Exception:
        pass
    return keywords[0].title()  # fallback e.g. "Trash"

def imap_move(account: str, ids: list, src: str, dst: str) -> dict:
    """Move messages from src folder to dst folder via COPY + DELETE."""
    acc = next((a for a in load_accounts() if a["email"] == account), None)
    if not acc: return {"ok": False, "error": "Account not found"}
    try:
        conn = imaplib.IMAP4("127.0.0.1", IMAP_PORT)
        conn.login(acc["email"], acc["password"])
        # Ensure destination exists
        try: conn.create(dst)
        except Exception: pass
        conn.select(src)
        for mid in ids:
            conn.copy(mid, dst)
            conn.store(mid, "+FLAGS", "\\Deleted")
        conn.expunge()
        conn.logout()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def imap_delete(account: str, ids: list, folder="INBOX") -> dict:
    """Move to Trash unless already in Trash — then permanently delete."""
    acc = next((a for a in load_accounts() if a["email"] == account), None)
    if not acc: return {"ok": False, "error": "Account not found"}
    try:
        conn = imaplib.IMAP4("127.0.0.1", IMAP_PORT)
        conn.login(acc["email"], acc["password"])
        trash = _imap_find_folder(conn, ["Trash", "Deleted"])
        if folder.lower() in ("trash", "deleted", trash.lower()):
            # Already in trash — permanently delete
            conn.select(folder)
            for mid in ids: conn.store(mid, "+FLAGS", "\\Deleted")
            conn.expunge()
        else:
            # Move to Trash first
            try: conn.create(trash)
            except Exception: pass
            conn.select(folder)
            for mid in ids:
                conn.copy(mid, trash)
                conn.store(mid, "+FLAGS", "\\Deleted")
            conn.expunge()
        conn.logout()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def imap_empty_trash(account: str) -> dict:
    """Permanently delete everything in the Trash folder."""
    acc = next((a for a in load_accounts() if a["email"] == account), None)
    if not acc: return {"ok": False, "error": "Account not found"}
    try:
        conn = imaplib.IMAP4("127.0.0.1", IMAP_PORT)
        conn.login(acc["email"], acc["password"])
        trash = _imap_find_folder(conn, ["Trash", "Deleted"])
        conn.select(trash)
        _, data = conn.search(None, "ALL")
        nums = data[0].split() if data[0] else []
        if nums:
            for mid in nums: conn.store(mid, "+FLAGS", "\\Deleted")
            conn.expunge()
        conn.logout()
        return {"ok": True, "deleted": len(nums)}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def imap_append_draft(account: str, msg_bytes: bytes) -> dict:
    """Save a draft message to the Drafts folder."""
    acc = next((a for a in load_accounts() if a["email"] == account), None)
    if not acc: return {"ok": False, "error": "Account not found"}
    try:
        conn = imaplib.IMAP4("127.0.0.1", IMAP_PORT)
        conn.login(acc["email"], acc["password"])
        drafts = _imap_find_folder(conn, ["Drafts", "Draft"])
        try: conn.create(drafts)
        except Exception: pass
        conn.append(drafts, r"(\Draft)", imaplib.Time2Internaldate(time.time()), msg_bytes)
        conn.logout()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def imap_update_account(account: str, new_password: str = "",
                         display_name: str = "", signature: str = "") -> dict:
    """Update password, display name, and signature for an account."""
    accs = load_accounts()
    updated = False
    for a in accs:
        if a["email"] == account:
            if new_password:
                a["password"] = new_password
                # Also update in docker-mailserver
                docker(f"exec {CONTAINER_MAILSRVR} setup email update {account} {new_password}")
            if display_name is not None: a["display_name"] = display_name
            if signature   is not None: a["signature"]     = signature
            updated = True
            break
    if not updated: return {"ok": False, "error": "Account not found"}
    save_accounts(accs)
    return {"ok": True}



def imap_find_sent_folder(conn) -> str:
    """Return the Sent folder name as reported by the IMAP server."""
    try:
        _, lst = conn.list()
        for item in (lst or []):
            if isinstance(item, bytes):
                item = item.decode(errors="replace")
            upper = item.upper()
            if "SENT" in upper:
                # Extract name after last separator
                for sep in ['"."', '"/"', ' ']:
                    if sep in item:
                        name = item.rsplit(sep, 1)[-1].strip().strip('"')
                        if name:
                            return name
    except Exception:
        pass
    return "Sent"

def imap_append_sent(account: str, msg_bytes: bytes) -> dict:
    """Save a copy of an outgoing message to the Sent folder via IMAP APPEND."""
    acc = next((a for a in load_accounts() if a["email"] == account), None)
    if not acc:
        return {"ok": False, "error": "Account not found"}
    try:
        conn = imaplib.IMAP4("127.0.0.1", IMAP_PORT)
        conn.login(acc["email"], acc["password"])
        sent = imap_find_sent_folder(conn)
        # Create the folder if it doesn't exist yet (IMAP CREATE is idempotent)
        try:
            conn.create(sent)
        except Exception:
            pass
        conn.append(
            sent,
            r"(\Seen)",
            imaplib.Time2Internaldate(time.time()),
            msg_bytes,
        )
        conn.logout()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def imap_folders(account: str) -> list:
    acc = next((a for a in load_accounts() if a["email"] == account), None)
    if not acc: return ["INBOX"]
    try:
        conn = imaplib.IMAP4("127.0.0.1", IMAP_PORT)
        conn.login(acc["email"], acc["password"])
        _, lst = conn.list(); conn.logout()
        folders = []
        for item in (lst or []):
            if isinstance(item, bytes): item = item.decode()
            parts = item.rsplit('"."', 1)
            name  = parts[-1].strip().strip('"') if parts else ""
            if name and name not in folders: folders.append(name)
        return folders or ["INBOX"]
    except Exception: return ["INBOX"]

def _dh(s: str) -> str:
    try:
        parts = _hdr.decode_header(s)
        return " ".join(
            (p.decode(enc or "utf-8", errors="replace") if isinstance(p, bytes) else str(p))
            for p, enc in parts
        )
    except Exception: return str(s)

def _pl(part) -> str:
    try:
        raw = part.get_payload(decode=True)
        return raw.decode(part.get_content_charset() or "utf-8", errors="replace") if raw else ""
    except Exception: return ""

# ─────────────────────────────────────────────────────────────────────────────
#  COMPOSE / TEST EMAIL
# ─────────────────────────────────────────────────────────────────────────────

def send_test_email(to: str, subject: str, body: str,
                    from_addr: str = "", html_body: str = "",
                    attachments: list = None) -> dict:
    cfg = load_config()
    if cfg["mode"] == "dev":
        host   = "127.0.0.1"
        port   = MAILPIT_SMTP
        sender = from_addr or f"test@{cfg.get('domain','local')}"
        user   = None
        pwd    = None
    else:
        host   = "127.0.0.1"
        port   = SMTP_PORT
        # from_addr may be "Display Name <email>" — extract just the email
        sender_raw = _eutils.parseaddr(from_addr)[1] if from_addr else ""
        sender     = sender_raw or f"noreply@{cfg.get('domain','local')}"
        # Look up credentials for the from address
        accs = load_accounts()
        acc  = next((a for a in accs if a["email"] == sender), None)
        if not acc and accs:
            acc    = accs[0]
            sender = acc["email"]
        user = acc["email"]    if acc else None
        pwd  = acc["password"] if acc else None
        if not user or not pwd:
            return {
                "ok": False,
                "error": (
                    "No account credentials found for authenticated send. "
                    "Add a mailbox in the Accounts tab first."
                ),
            }
    try:
        import base64 as _b64
        import mimetypes as _mt

        att_list = []
        for att in (attachments or []):
            fname    = att.get("name", "attachment")
            data_b64 = att.get("data", "")
            if not data_b64: continue
            if "," in data_b64: data_b64 = data_b64.split(",", 1)[1]
            try:
                raw = _b64.b64decode(data_b64)
                att_list.append((fname, raw))
            except Exception:
                continue

        def _clean_html(h: str) -> str:
            h = h.strip()
            text_only = re.sub(r'<[^>]+>', '', h).strip()
            if not text_only:
                return ""
            h = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', h)
            h = re.sub(r'\u200b|\u200c|\u200d|\ufeff', '', h)
            return (
                '<!DOCTYPE html><html><head>'
                '<meta charset="UTF-8">'
                '</head><body>'
                f'{h}'
                '</body></html>'
            )

        clean_html = _clean_html(html_body) if html_body else ""
        plain_text = body or (re.sub(r'<[^>]+>', '', clean_html).strip() if clean_html else "")

        # Use email.policy.SMTP for correct UTF-8 handling
        import email.policy as _epol
        msg = email.message.EmailMessage(policy=_epol.SMTP)
        msg["From"]       = from_addr if from_addr else sender
        msg["To"]         = to
        msg["Subject"]    = subject
        msg["Date"]       = _eutils.formatdate(localtime=True)
        msg["Message-ID"] = _eutils.make_msgid(
            domain=sender.split("@")[-1] if "@" in sender else "mail")

        # Build body — set_content creates text/plain, add_alternative wraps in
        # multipart/alternative, add_attachment then wraps that in multipart/mixed
        msg.set_content(plain_text)
        if clean_html:
            msg.add_alternative(clean_html, subtype="html")

        # Add attachments — Python's EmailMessage automatically promotes the
        # structure to multipart/mixed when the first attachment is added
        for fname, raw in att_list:
            ctype, _ = _mt.guess_type(fname)
            maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
            msg.add_attachment(raw, maintype=maintype, subtype=subtype,
                               filename=fname)

        tls_ctx = ssl.create_default_context()
        tls_ctx.check_hostname = False
        tls_ctx.verify_mode    = ssl.CERT_NONE

        smtp_timeout = 60 if att_list else 15

        with smtplib.SMTP(host, port, timeout=smtp_timeout) as s:
            s.ehlo()
            if port != MAILPIT_SMTP:
                try:
                    s.starttls(context=tls_ctx)
                    s.ehlo()
                except smtplib.SMTPNotSupportedError:
                    pass
                except smtplib.SMTPException:
                    pass
            if user and pwd:
                s.login(user, pwd)

            msg_bytes = msg.as_bytes(policy=_epol.SMTP)

            # send_message() is designed for policy-aware EmailMessage objects
            # and handles Content-Transfer-Encoding correctly for binary parts
            from_env  = _eutils.parseaddr(sender)[1] or sender
            to_addrs  = [a.strip() for a in to.split(",") if a.strip()]
            refused   = s.send_message(msg, from_addr=from_env, to_addrs=to_addrs)
            if refused:
                return {"ok": False, "error": f"Some recipients refused: {refused}"}

        # Save to Sent folder via IMAP APPEND (background thread — non-blocking)
        if cfg["mode"] != "dev" and user:
            threading.Thread(
                target=imap_append_sent, args=(user, msg_bytes), daemon=True
            ).start()

        return {"ok": True}
    except smtplib.SMTPAuthenticationError as e:
        return {"ok": False, "error": f"Authentication failed: {e.smtp_error.decode(errors='replace')}"}
    except smtplib.SMTPRecipientsRefused as e:
        return {"ok": False, "error": f"Recipient refused: {e.recipients}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ─────────────────────────────────────────────────────────────────────────────
#  HTTP SERVER
# ─────────────────────────────────────────────────────────────────────────────

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args): pass

    def do_GET(self):
        p    = urlparse(self.path)
        path = p.path.rstrip("/") or "/"
        qs   = parse_qs(p.query)

        if path == "/":                return self._html()
        if path == "/api/status":      return self._json(get_status())
        if path == "/api/diagnostics": return self._json(run_diagnostics())
        if path == "/api/checklist":   return self._json({"ok": True, "checklist": load_checklist()})
        if path == "/api/dns/ptr":
            cfg = load_config()
            ip  = cfg.get("public_ip", "")
            mh  = cfg.get("mail_host", "")
            r   = check_ptr(ip, mh)
            if r["ok"]:
                cl = load_checklist(); cl["ptr"] = True; save_checklist(cl)
            return self._json({"ok": True, **r})
        if path == "/api/config":      return self._json({"ok": True, "config": load_config()})
        if path == "/api/spam":        return self._json({"ok": True, "spam": load_spam()})
        if path == "/api/accounts":
            sync = container_running(CONTAINER_MAILSRVR)
            return self._json({"ok": True,
                               "accounts": account_list_sync() if sync else load_accounts()})
        if path == "/api/domains":
            doms = discover_domains()
            return self._json({"ok": True, "domains": [
                {"name": k, "zone_id": v.get("zone_id",""), "source": v.get("source","")}
                for k, v in doms.items()
            ]})
        if path == "/api/dns/verify":
            cfg = load_config()
            if not cfg.get("zone_id"):
                return self._json({"ok": False, "error": "No domain provisioned yet"})
            results = verify_dns_records(cfg["domain"], cfg["zone_id"])
            return self._json({"ok": True, "records": results})

        if path == "/api/ports/check":
            # Check UFW status first
            ufw_out, _, ufw_rc = run("ufw status 2>/dev/null")
            ufw_inactive = (
                ufw_rc != 0 or                        # ufw not installed
                "inactive" in ufw_out.lower() or      # ufw installed but off
                "Status: inactive" in ufw_out or
                not ufw_out.strip()                   # no output = not running
            )

            results = {}
            for port in [25, 143, 587, 993]:
                if ufw_inactive:
                    # No firewall — port is not blocked at OS level.
                    # Still check if the service is actually listening.
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=2):
                            results[str(port)] = "open"
                    except Exception:
                        # Service not listening, but not firewall-blocked either
                        results[str(port)] = "no_firewall"
                else:
                    # UFW is active — check if port is reachable
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=2):
                            results[str(port)] = "open"
                    except Exception:
                        # Check if UFW explicitly allows it
                        allowed = str(port) in ufw_out or f"{port}/tcp" in ufw_out
                        results[str(port)] = "allowed" if allowed else "blocked"

            return self._json({"ok": True, "ports": results,
                               "ufw_active": not ufw_inactive,
                               "ufw_status": ufw_out.split('\n')[0].strip() if ufw_out else "Not installed"})

        if path == "/api/inbox":       return self._inbox(qs)
        if path == "/api/message":     return self._message(qs)
        if path == "/api/folders":
            acc = qs.get("account",[""])[0]
            return self._json({"ok": True, "folders": imap_folders(acc) if acc else ["INBOX"]})
        self._raw(404, "text/plain", b"Not found")

    def do_POST(self):
        body = self._read_body()
        p    = urlparse(self.path)
        path = p.path.rstrip("/")

        if path == "/api/start": return self._json(do_start())

        if path == "/api/amavis/log":
            if not container_running(CONTAINER_MAILSRVR):
                return self._json({"ok": False, "error": "Not running"})
            out, _, _ = docker(
                f"exec {CONTAINER_MAILSRVR} bash -c "
                "'grep -i amavis /var/log/mail/mail.log 2>/dev/null | tail -50 || "
                " grep -i amavis /var/log/mail.log 2>/dev/null | tail -50 || "
                " tail -50 /var/log/amavis/amavis.log 2>/dev/null'"
            )
            cfg_out, _, _ = docker(
                f"exec {CONTAINER_MAILSRVR} bash -c "
                "'cat /etc/amavis/conf.d/99-mailsrvr-override.cf 2>/dev/null || "
                " echo FILE_NOT_FOUND'"
            )
            postfix_cf, _, _ = docker(
                f"exec {CONTAINER_MAILSRVR} postconf content_filter 2>/dev/null"
            )
            return self._json({"ok": True,
                               "amavis_log": out or "(empty)",
                               "override_config": cfg_out or "(not found)",
                               "postfix_content_filter": postfix_cf.strip()})

        if path == "/api/fix/amavis":
            if not container_running(CONTAINER_MAILSRVR):
                return self._json({"ok": False, "error": "Container not running"})

            steps = []
            import subprocess as _sp

            # Remove smtp-amavis content_filter from master.cf for BOTH smtp and submission ports
            # This is the definitive fix — postconf -e can't override master.cf -o options
            sed_cmd = (
                "sed -i '/content_filter=smtp-amavis/d' /etc/postfix/master.cf && "
                "sed -i '/smtp-amavis/d' /etc/postfix/main.cf 2>/dev/null; "
                "postfix reload"
            )
            _, err1, rc1 = docker(f"exec {CONTAINER_MAILSRVR} bash -c '{sed_cmd}'")
            steps.append(f"remove amavis from master.cf: rc={rc1} {err1[:80] if err1 else ''}")

            # Also write the amavis passthrough config as backup
            cfg = (
                "use strict;\n"
                "$final_banned_destiny  = D_PASS;\n"
                "$final_spam_destiny    = D_PASS;\n"
                "$final_virus_destiny   = D_PASS;\n"
                "$final_bad_header_destiny = D_PASS;\n"
                "@bypass_banned_checks_maps  = (1);\n"
                "@bypass_spam_checks_maps    = (1);\n"
                "@bypass_virus_checks_maps   = (1);\n"
                "@bypass_header_checks_maps  = (1);\n"
                "1;\n"
            )
            r2 = _sp.run(
                ["docker", "exec", "-i", CONTAINER_MAILSRVR,
                 "bash", "-c",
                 "cat > /etc/amavis/conf.d/99-mailsrvr-override.cf"],
                input=cfg.encode(), capture_output=True
            )
            steps.append(f"write amavis override: rc={r2.returncode}")

            # Verify the fix
            cf_out, _, _ = docker(
                f"exec {CONTAINER_MAILSRVR} postconf content_filter 2>/dev/null"
            )
            mcf_out, _, _ = docker(
                f"exec {CONTAINER_MAILSRVR} grep -c smtp-amavis /etc/postfix/master.cf 2>/dev/null"
            )
            steps.append(f"content_filter={cf_out.strip()} | amavis refs in master.cf: {mcf_out.strip()}")

            return self._json({
                "ok": True,
                "msg": "Amavis removed from mail path. Restart the server for full effect, or try sending now.",
                "steps": steps
            })
        if path == "/api/stop":  return self._json(do_stop())

        if path == "/api/config":
            cfg = load_config()
            allowed = {"mode"}          # domain/zone written by /api/dns/provision
            cfg.update({k: v for k, v in body.items() if k in allowed})
            save_config(cfg)
            return self._json({"ok": True})

        if path == "/api/dns/provision":
            domain    = body.get("domain", "").strip()
            zone_id   = body.get("zone_id", "").strip()
            public_ip = body.get("public_ip", "").strip() or get_public_ip()
            if not domain or not zone_id:
                return self._json({"ok": False, "error": "domain and zone_id are required"})
            if not public_ip:
                return self._json({"ok": False,
                                   "error": "Could not detect public IP. Enter it manually."})
            return self._json(provision_dns(domain, zone_id, public_ip))

        if path == "/api/dns/dkim":
            cfg = load_config()
            if not cfg.get("domain") or not cfg.get("zone_id"):
                return self._json({"ok": False, "error": "No domain configured"})
            return self._json(provision_dkim(cfg["domain"], cfg["zone_id"]))

        if path == "/api/dns/dkim/status":
            return self._json({"ok": True, "status": dkim_get_status()})

        if path == "/api/dns/refresh_ip":
            cfg = load_config()
            ip  = get_public_ip()
            if not ip:
                # Fall back to saved IP if detection fails (e.g. network timeout)
                ip = cfg.get("public_ip", "")
                if ip:
                    return self._json({"ok": True, "ip": ip, "cached": True})
                return self._json({"ok": False, "error": "Could not detect public IP — check your internet connection"})
            if cfg.get("zone_id") and cfg.get("mail_host"):
                _, err = cf_upsert_dns(cfg["zone_id"], "A", cfg["mail_host"], ip,
                                       proxied=False, ttl=300)
                if err: return self._json({"ok": False, "error": err})
            cfg["public_ip"] = ip
            save_config(cfg)
            return self._json({"ok": True, "ip": ip})

        if path == "/api/dns/postmaster":
            txt_val = body.get("txt", "").strip()
            if not txt_val:
                return self._json({"ok": False, "error": "TXT value is required"})
            cfg = load_config()
            if not cfg.get("zone_id") or not cfg.get("domain"):
                return self._json({"ok": False, "error": "No domain configured"})
            _, err = cf_upsert_dns(cfg["zone_id"], "TXT", cfg["domain"],
                                   txt_val, proxied=False, ttl=300)
            if err:
                return self._json({"ok": False, "error": err})
            # Persist so state survives page refresh
            cl = load_checklist()
            cl["postmaster"] = {"done": True, "txt": txt_val,
                                "ts": datetime.now().isoformat()}
            save_checklist(cl)
            return self._json({"ok": True,
                               "msg": f"TXT record added to {cfg['domain']}"})

        if path == "/api/checklist/update":
            key = body.get("key", ""); val = body.get("value", True)
            if not key: return self._json({"ok": False, "error": "key required"})
            cl = load_checklist(); cl[key] = val; save_checklist(cl)
            return self._json({"ok": True})

        if path == "/api/spam":
            spam = load_spam(); spam.update(body)
            return self._json(apply_spam_config(spam))

        if path == "/api/accounts":
            em = body.get("email","").strip(); pw = body.get("password","").strip()
            if not em or not pw: return self._json({"ok": False, "error": "Email and password required"})
            return self._json(account_add(em, pw))

        if path == "/api/account/delete":
            return self._json(account_del(body.get("email","")))

        if path == "/api/message/delete":
            cfg = load_config()
            ids = body.get("ids",[]); acc = body.get("account",""); fld = body.get("folder","INBOX")
            return self._json(mailpit_delete(ids) if cfg["mode"] == "dev"
                              else imap_delete(acc, ids, fld))

        if path == "/api/message/empty_trash":
            acc = body.get("account","")
            return self._json(imap_empty_trash(acc))

        if path == "/api/message/save_draft":
            acc  = body.get("account","")
            data = body.get("draft",{})
            # Build a minimal RFC 2822 message for the draft
            import email.message as _em
            msg = _em.EmailMessage()
            msg["From"]    = data.get("from","")
            msg["To"]      = data.get("to","")
            msg["Subject"] = data.get("subject","(no subject)")
            msg.set_content(data.get("body",""))
            return self._json(imap_append_draft(acc, msg.as_bytes()))

        if path == "/api/account/update":
            em  = body.get("email","")
            pw  = body.get("password","")
            dn  = body.get("display_name","")
            sig = body.get("signature","")
            return self._json(imap_update_account(em, pw, dn, sig))

        if path == "/api/inbox/clear":
            return self._json(mailpit_delete_all() if load_config()["mode"] == "dev"
                              else {"ok": False, "error": "Clear all only in Dev mode"})

        if path == "/api/compose":
            return self._json(send_test_email(
                body.get("to",""), body.get("subject","Test from KillTheHost"),
                body.get("body","Hello from KillTheHost MAIL-SRVR!"),
                body.get("from_addr",""), body.get("html_body",""),
                body.get("attachments", [])))

        self._raw(404, "text/plain", b"Not found")

    # ── sub-handlers ──────────────────────────────────────────────────────────

    def _inbox(self, qs):
        cfg  = load_config()
        page = int(qs.get("page",["1"])[0]); limit = int(qs.get("limit",["25"])[0])
        q    = qs.get("q",[""])[0]; folder = qs.get("folder",["INBOX"])[0]
        acc  = qs.get("account",[""])[0]
        if cfg["mode"] == "dev": return self._json(mailpit_list(page, limit, q))
        if not acc:
            accs = load_accounts(); acc = accs[0]["email"] if accs else ""
        if not acc: return self._json({"ok": False, "error": "No accounts", "messages": [], "total": 0})
        return self._json(imap_list(acc, folder, page, limit, q))

    def _message(self, qs):
        cfg = load_config(); mid = qs.get("id",[""])[0]
        acc = qs.get("account",[""])[0]; fld = qs.get("folder",["INBOX"])[0]
        if not mid: return self._json({"ok": False, "error": "Missing id"})
        return self._json(mailpit_get(mid) if cfg["mode"] == "dev"
                          else imap_get(acc, mid, fld))

    # ── helpers ───────────────────────────────────────────────────────────────

    def _read_body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length",0))
            return json.loads(self.rfile.read(n).decode()) if n else {}
        except Exception: return {}

    def _html(self):
        self._raw(200, "text/html; charset=utf-8", HTML.encode())

    def _json(self, data: dict):
        b = json.dumps(data).encode()
        self._raw(200, "application/json", b)

    def _raw(self, code: int, ct: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type",   ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control",  "no-cache")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

# ─────────────────────────────────────────────────────────────────────────────
#  EMBEDDED HTML / CSS / JS
# ─────────────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>KillTheHost - MAIL-SRVR</title>
    <link rel="shortcut icon" href="https://www.phdesigns.net/img/favicon.ico" type="image/x-icon">
    <link rel="icon" href="https://www.phdesigns.net/img/favicon.ico" type="image/x-icon">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#1a1a1a;--panel:#242424;--card:#2a2a2a;--border:#383838;
  --text:#e8e8e8;--dim:#888;--muted:#555;
  --green:#10a37f;--green-bg:#0c1f18;--green-dim:#1e4a30;
  --red:#ef4444;--amber:#f59e0b;--blue:#3b82f6;--purple:#a78bfa;
  --log-bg:#141414;--inp:#1e1e1e
}
body{font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
     background:var(--bg);color:var(--text);min-height:100vh;
     display:flex;flex-direction:column;font-size:13px;line-height:1.5}
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:2px}

/* ── layout ── */
header{padding:10px 20px;border-bottom:1px solid var(--border);
       background:var(--panel);display:flex;align-items:center;
       justify-content:space-between;gap:12px;flex-wrap:wrap}
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
              -webkit-background-clip:text;background-clip:text;
              -webkit-text-fill-color:transparent}
.brand-app{font-size:13px;font-weight:700;color:var(--text);letter-spacing:.32px}
.brand-suite{font-size:10px;color:var(--muted);letter-spacing:.22px;text-transform:uppercase}
.hdr-right{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.status-pill{display:flex;align-items:center;gap:6px;padding:4px 10px;
             border-radius:20px;font-size:11px;border:1px solid var(--border);
             background:var(--card)}
.dot{width:6px;height:6px;border-radius:50%;background:var(--muted)}
.dot.live{background:var(--green);animation:pulse 2s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
.mode-badge{font-size:10px;padding:3px 8px;border-radius:12px;font-weight:700;letter-spacing:.5px}
.mode-dev{background:#1a2a1f;color:var(--green);border:1px solid var(--green-dim)}
.mode-live{background:#1a1a2a;color:var(--purple);border:1px solid #2a2060}

/* Shell fills the space between header and footer using viewport height minus measured offsets.
   JS sets --shell-h on :root after DOM load to the exact remaining pixels. */
.shell{display:flex;flex-direction:row;height:var(--shell-h,calc(100vh - 82px));overflow:hidden}
.sidebar{width:178px;flex-shrink:0;background:var(--panel);
         border-right:1px solid var(--border);display:flex;
         flex-direction:column;padding:12px 0;overflow-y:auto}
.nav-label{font-size:9px;letter-spacing:1px;color:var(--muted);
           text-transform:uppercase;padding:6px 18px 4px}
.nav-item{display:flex;align-items:center;gap:8px;padding:7px 10px 7px 18px;
          border-radius:0;cursor:pointer;color:var(--dim);font-size:12px;
          transition:all .12s;user-select:none;margin:1px 0;position:relative}
.nav-item:hover{background:var(--card);color:var(--text)}
.nav-item.active{background:var(--green-bg);color:var(--green)}
.nav-item.warn{color:var(--amber) !important}
.nav-item.warn::after{content:"!";position:absolute;right:10px;
  font-size:9px;background:var(--amber);color:#000;border-radius:50%;
  width:14px;height:14px;display:flex;align-items:center;justify-content:center;font-weight:700}
.ico{font-size:13px;width:16px;text-align:center;flex-shrink:0}
.nb{margin-left:auto;background:var(--border);color:var(--dim);
    font-size:9px;padding:1px 5px;border-radius:8px;min-width:18px;text-align:center}
.nb.unread{background:var(--green-dim);color:var(--green)}

/* Content area: relative so panels can be absolutely positioned inside it */
.content{flex:1;position:relative;overflow:hidden;height:100%}

/* Every panel fills the content area completely via absolute positioning.
   This bypasses all flex-height-chain issues entirely. */
.panel{position:absolute;top:0;left:0;right:0;bottom:0;
       display:none;flex-direction:column;gap:14px;
       padding:18px 20px;overflow-y:auto;background:var(--bg)}
.panel.active{display:flex}
.panel-title{font-size:13px;font-weight:700;color:var(--text);letter-spacing:.3px}
.panel-sub{font-size:11px;color:var(--dim)}

/* Inbox and compose: no padding, no gap, internal layout takes over */
#panel-inbox,#panel-compose{padding:0;gap:0;overflow:hidden}

/* ── cards / info boxes ── */
.card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:14px}
.card-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.stat-label{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.stat-value{font-size:22px;font-weight:700;color:var(--text);margin:4px 0 2px}
.stat-sub{font-size:11px;color:var(--dim)}
.info-box{background:var(--card);border:1px solid var(--border);border-radius:8px;
          padding:12px 14px;font-size:11px;color:var(--dim);line-height:1.9}
.info-box b{color:var(--text)}
.info-box .warn{color:var(--amber)}
.info-box .note{color:var(--blue)}
.info-box .ok{color:var(--green)}
.info-box .err{color:var(--red)}
.alert-box{border-radius:8px;padding:12px 14px;font-size:12px;line-height:1.6}
.alert-warn{background:#1f1700;border:1px solid #5c3d00;color:var(--amber)}
.alert-err{background:#1f0a0a;border:1px solid #5c1a1a;color:var(--red)}
.alert-ok{background:var(--green-bg);border:1px solid var(--green-dim);color:var(--green)}
.section-title{font-size:10px;letter-spacing:1px;color:var(--muted);
               text-transform:uppercase;padding-bottom:6px;
               border-bottom:1px solid var(--border);margin-bottom:10px}

/* ── buttons ── */
.btn-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.btn{padding:7px 16px;border:none;border-radius:6px;font-size:12px;font-weight:600;
     cursor:pointer;display:inline-flex;align-items:center;gap:5px;
     font-family:inherit;transition:filter .12s,transform .1s}
.btn:disabled{opacity:.3;cursor:not-allowed}
.btn:not(:disabled):hover{filter:brightness(1.15)}
.btn:not(:disabled):active{transform:scale(.97)}
.btn-green{background:var(--green);color:#fff}
.btn-red{background:var(--red);color:#fff}
.btn-amber{background:#92400e;color:#fde68a}
.btn-ghost{background:transparent;color:var(--dim);border:1px solid var(--border)}
.btn-ghost:not(:disabled):hover{background:var(--card);color:var(--text)}
.btn-sm{padding:4px 10px;font-size:11px}

/* ── form ── */
.form-row{display:flex;flex-direction:column;gap:5px}
.form-row label{font-size:11px;color:var(--dim)}
.inp{background:var(--inp);border:1px solid var(--border);color:var(--text);
     border-radius:6px;padding:7px 10px;font-size:12px;font-family:inherit;
     outline:none;width:100%}
.inp:focus{border-color:var(--green)}
.inp-sm{padding:5px 8px;font-size:11px}
textarea.inp{resize:vertical;min-height:80px}
select.inp{cursor:pointer}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}
@media(max-width:600px){.form-grid{grid-template-columns:1fr}}

/* ── DNS status table ── */
.dns-table{width:100%;border-collapse:collapse;font-size:11.5px}
.dns-table th{padding:7px 10px;text-align:left;font-size:10px;color:var(--muted);
              text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border)}
.dns-table td{padding:8px 10px;border-bottom:1px solid var(--border);color:var(--dim)}
.dns-table tr:last-child td{border-bottom:none}
.dns-ok{color:var(--green)}
.dns-miss{color:var(--red)}
.dns-pend{color:var(--amber)}

/* ── inbox ── */
.inbox-shell{display:flex;flex-direction:row;width:100%;height:100%;overflow:hidden}
.msg-list-col{width:300px;flex-shrink:0;border-right:1px solid var(--border);
              display:flex;flex-direction:column;overflow:hidden}
.msg-reader-col{flex:1;display:flex;flex-direction:column;overflow:hidden}
.inbox-toolbar{padding:8px 10px;border-bottom:1px solid var(--border);
               display:flex;align-items:center;gap:6px;background:var(--panel)}
.search-wrap{flex:1;position:relative}
.search-wrap input{width:100%;background:var(--inp);border:1px solid var(--border);
                   border-radius:5px;padding:5px 8px 5px 24px;font-size:11px;
                   color:var(--text);font-family:inherit;outline:none}
.search-wrap input:focus{border-color:var(--green)}
.search-ico{position:absolute;left:7px;top:50%;transform:translateY(-50%);
            color:var(--muted);font-size:11px;pointer-events:none}
.acc-bar{padding:5px 10px;border-bottom:1px solid var(--border);
         background:var(--log-bg);display:flex;align-items:center;gap:6px;font-size:11px;color:var(--dim)}
.acc-sel{flex:1;background:var(--inp);border:1px solid var(--border);
         color:var(--text);border-radius:5px;padding:3px 6px;
         font-size:11px;font-family:inherit;outline:none}
.msg-list{flex:1;overflow-y:auto}
.msg-item{padding:9px 10px;border-bottom:1px solid var(--border);
          cursor:pointer;display:flex;flex-direction:column;gap:2px;
          transition:background .1s;user-select:none}
.msg-item:hover{background:var(--card)}
.msg-item.selected{background:var(--green-bg)}
.msg-item.unread .mi-subj{font-weight:700;color:var(--text)}
.mi-row{display:flex;justify-content:space-between}
.mi-from{font-size:11px;color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:180px}
.mi-date{font-size:10px;color:var(--muted);flex-shrink:0}
.mi-subj{font-size:11.5px;color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.empty-state{flex:1;display:flex;flex-direction:column;align-items:center;
             justify-content:center;gap:8px;color:var(--muted);padding:40px}
.empty-state .ico{font-size:32px;opacity:.4}
.reader-head{padding:14px 16px;border-bottom:1px solid var(--border);
             background:var(--panel);display:flex;flex-direction:column;gap:6px}
.reader-subj{font-size:14px;font-weight:700;color:var(--text)}
.reader-meta{font-size:11px;color:var(--dim);line-height:1.8}
.reader-meta b{color:var(--text)}
.reader-bar{padding:6px 16px;border-bottom:1px solid var(--border);
            background:var(--log-bg);display:flex;gap:6px}
.reader-body{flex:1;overflow-y:auto;padding:16px}
.reader-text{font-family:"Menlo","Consolas",monospace;font-size:12px;
             color:var(--dim);line-height:1.7;white-space:pre-wrap;word-break:break-word}
.reader-placeholder{flex:1;display:flex;align-items:center;justify-content:center;
                    color:var(--muted);font-size:12px;flex-direction:column;gap:8px}
.reader-placeholder .ico{font-size:28px;opacity:.3}
.pgn{padding:7px 10px;border-top:1px solid var(--border);
     display:flex;align-items:center;justify-content:space-between;
     background:var(--log-bg);font-size:11px;color:var(--dim)}

/* ── spam toggles / sliders ── */
.spam-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:700px){.spam-grid{grid-template-columns:1fr}}
.toggle-row{display:flex;align-items:center;justify-content:space-between;
            padding:6px 0;border-bottom:1px solid var(--border)}
.toggle-row:last-child{border-bottom:none}
.toggle-label{font-size:12px;color:var(--dim)}
.tw{position:relative;width:36px;height:20px;flex-shrink:0}
.tw input{opacity:0;width:0;height:0;position:absolute}
.ts{position:absolute;inset:0;background:var(--border);border-radius:20px;cursor:pointer;transition:background .2s}
.ts::before{content:"";position:absolute;width:14px;height:14px;background:#fff;
            border-radius:50%;left:3px;top:3px;transition:transform .2s}
.tw input:checked+.ts{background:var(--green)}
.tw input:checked+.ts::before{transform:translateX(16px)}
.score-row{display:flex;align-items:center;gap:10px;margin-bottom:6px}
.score-row label{font-size:11px;color:var(--dim);width:100px;flex-shrink:0}
input[type=range]{flex:1;accent-color:var(--green);height:4px;border-radius:2px}
.sv{font-size:11px;color:var(--text);min-width:28px;text-align:right;font-weight:700}
.tag-list{display:flex;flex-wrap:wrap;gap:5px;min-height:28px;background:var(--inp);
          border:1px solid var(--border);border-radius:6px;padding:4px 6px;align-items:center}
.tag{display:flex;align-items:center;gap:4px;background:var(--border);
     color:var(--text);font-size:11px;padding:2px 7px;border-radius:12px}
.tag-del{cursor:pointer;color:var(--muted);font-size:12px;line-height:1}
.tag-del:hover{color:var(--red)}
.tag-input{background:transparent;border:none;outline:none;color:var(--text);
           font-size:11px;font-family:inherit;flex:1;min-width:120px}

/* ── accounts table ── */
table{width:100%;border-collapse:collapse;font-size:11.5px}
th{padding:7px 10px;text-align:left;font-size:10px;color:var(--muted);
   text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border)}
td{padding:8px 10px;border-bottom:1px solid var(--border);color:var(--dim)}
tr:last-child td{border-bottom:none}
tr:hover td{background:var(--card)}
.pill{font-size:10px;padding:2px 7px;border-radius:8px;
      background:var(--green-bg);color:var(--green);display:inline-block;font-weight:600}

/* ── toast ── */
#toast{position:fixed;bottom:20px;right:20px;z-index:999;
       display:flex;flex-direction:column;gap:6px;pointer-events:none}
.tmsg{background:var(--panel);border:1px solid var(--border);color:var(--text);
      border-radius:6px;padding:8px 14px;font-size:12px;opacity:0;
      transform:translateY(8px);transition:all .2s;max-width:300px}
.tmsg.show{opacity:1;transform:none}
.tmsg.err{border-color:var(--red);color:var(--red)}
.tmsg.ok{border-color:var(--green);color:var(--green)}

/* ── compose ─────────────────────────────────────────────────────── */
.compose-shell{display:flex;flex-direction:column;width:100%;height:100%;overflow:hidden}
.c-sig{color:var(--dim);border-top:1px solid var(--border);margin-top:6px;padding-top:6px;font-size:12px}
.att-chip{display:inline-flex;align-items:center;gap:5px;background:var(--card);
          border:1px solid var(--border);border-radius:12px;padding:2px 8px 2px 6px;
          font-size:11px;color:var(--text);max-width:180px}
.att-chip-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.att-chip-size{color:var(--muted);font-size:10px;flex-shrink:0}
.att-chip-x{cursor:pointer;color:var(--muted);margin-left:3px;flex-shrink:0}
.att-chip-x:hover{color:var(--red)}
.compose-header{
  padding:12px 18px;border-bottom:1px solid var(--border);
  background:var(--panel);display:flex;align-items:center;
  justify-content:space-between;gap:12px;flex-shrink:0
}
.compose-fields{border-bottom:1px solid var(--border);flex-shrink:0}
.cf-row{
  display:flex;align-items:center;gap:0;
  border-bottom:1px solid var(--border);min-height:38px
}
.cf-row:last-child{border-bottom:none}
.cf-label{
  font-size:11px;color:var(--muted);padding:0 14px;
  flex-shrink:0;width:68px;text-align:right;letter-spacing:.3px
}
.cf-input{
  flex:1;background:transparent;border:none;outline:none;
  color:var(--text);font-size:12px;font-family:inherit;
  padding:8px 10px 8px 0
}
.cf-chips{
  flex:1;display:flex;flex-wrap:wrap;align-items:center;
  gap:4px;padding:5px 8px 5px 0;cursor:text;min-height:36px
}
.cf-chip{
  display:inline-flex;align-items:center;gap:4px;
  background:var(--border);color:var(--text);
  font-size:11px;padding:2px 8px;border-radius:12px
}
.cf-chip-x{cursor:pointer;color:var(--muted);margin-left:2px}
.cf-chip-x:hover{color:var(--red)}
.cf-chip-input{
  background:transparent;border:none;outline:none;
  color:var(--text);font-size:12px;font-family:inherit;
  min-width:180px;padding:2px 0
}
.compose-pill{
  font-size:10px;padding:2px 8px;border-radius:10px;
  border:1px solid var(--border);background:transparent;
  color:var(--muted);cursor:pointer;letter-spacing:.3px;
  margin-right:4px;font-family:inherit;transition:all .12s
}
.compose-pill:hover{background:var(--card);color:var(--text)}
.compose-pill.active{background:var(--green-bg);color:var(--green);border-color:var(--green-dim)}

.compose-toolbar{
  padding:5px 12px;border-bottom:1px solid var(--border);
  display:flex;align-items:center;gap:2px;flex-shrink:0;
  background:var(--log-bg)
}
.tb-btn{
  padding:4px 8px;background:transparent;border:none;
  border-radius:4px;color:var(--dim);cursor:pointer;
  font-size:12px;font-family:inherit;transition:all .1s;min-width:28px
}
.tb-btn:hover{background:var(--card);color:var(--text)}
.tb-sep{width:1px;height:16px;background:var(--border);margin:0 4px}
.tb-sel{
  background:transparent;border:1px solid var(--border);
  border-radius:4px;color:var(--dim);font-size:11px;
  padding:3px 5px;font-family:inherit;outline:none;cursor:pointer
}
.compose-editor{
  flex:1;overflow-y:auto;padding:18px 22px;
  color:var(--text);font-size:13px;line-height:1.7;
  outline:none;min-height:200px;background:var(--bg)
}
.compose-editor:empty::before{
  content:attr(data-placeholder);
  color:var(--muted);pointer-events:none
}
.compose-editor a{color:var(--green)}
.compose-editor ul,.compose-editor ol{padding-left:22px}
.compose-status{
  padding:5px 18px;border-top:1px solid var(--border);
  background:var(--log-bg);display:flex;justify-content:space-between;
  align-items:center;flex-shrink:0
}
/* ── checklist rows ── */
.checklist-row{display:flex;gap:10px;align-items:flex-start;padding:8px 0;
               border-bottom:1px solid var(--border)}
.checklist-row:last-child{border-bottom:none}
.cl-ico{font-size:14px;flex-shrink:0;margin-top:1px;width:16px;text-align:center;
        color:var(--amber)}
.cl-ico.ok{color:var(--green)}
.cl-ico.spin{animation:spin 1.2s linear infinite;display:inline-block;color:var(--muted)}
@keyframes spin{to{transform:rotate(360deg)}}

/* ── port chips ── */
.port-chip{
  display:inline-flex;align-items:center;justify-content:center;
  padding:2px 10px;border-radius:12px;font-size:11px;font-weight:600;
  border:1px solid var(--border);background:var(--card);color:var(--muted)
}
.port-chip.open{background:var(--green-bg);border-color:var(--green-dim);color:var(--green)}
.port-chip.closed{background:#2a0f0f;border-color:#5c1a1a;color:var(--red)}

footer{padding:8px;text-align:center;font-size:10px;color:var(--muted);
       border-top:1px solid var(--border);background:var(--panel)}
footer a{color:var(--dim);text-decoration:none}
footer a:hover{color:var(--green)}
</style>
</head>
<body>

<header>
  <div class="brand">
    <div class="kth-mark">&gt;_</div>
    <div class="brand-main">
      <div class="kth-logo">
        <span class="kth-word">Kill</span><span class="kth-word the">The</span><span class="kth-word">Host</span>
      </div>
      <div class="brand-app">MAIL-SRVR</div>
      <div class="brand-suite">KillTheHost Suite · Mail Server</div>
    </div>
  </div>
  <div class="hdr-right">
    <div class="status-pill">
      <div class="dot" id="dot"></div>
      <span id="st-text">Checking…</span>
    </div>
    <span class="mode-badge mode-dev" id="mode-badge">DEV</span>
    <button class="btn btn-green btn-sm" id="btn-start" onclick="startSrv()">▶ Start</button>
    <button class="btn btn-ghost btn-sm" id="btn-stop"  onclick="stopSrv()" style="display:none">■ Stop</button>
  </div>
</header>

<div class="shell">
  <nav class="sidebar">
    <div style="padding-bottom:4px">
      <div class="nav-label">Mail</div>
      <div class="nav-item active" data-tab="dash"    onclick="tab(this,'dash')">
        <span class="ico">⊞</span> Dashboard
      </div>
      <div class="nav-item"        data-tab="inbox"   onclick="tab(this,'inbox')">
        <span class="ico">✉</span> Inbox
        <span class="nb" id="nb-inbox">0</span>
      </div>
      <div class="nav-item"        data-tab="compose" onclick="tab(this,'compose')">
        <span class="ico">✏</span> Compose
      </div>
    </div>
    <div>
      <div class="nav-label">Server</div>
      <div class="nav-item warn"   data-tab="domain"   onclick="tab(this,'domain')" id="nav-domain">
        <span class="ico">🌐</span> Domain
      </div>
      <div class="nav-item"        data-tab="accounts" onclick="tab(this,'accounts')">
        <span class="ico">👤</span> Accounts
        <span class="nb" id="nb-acc">0</span>
      </div>
      <div class="nav-item"        data-tab="spam"     onclick="tab(this,'spam')">
        <span class="ico">🛡</span> Spam Filter
      </div>
      <div class="nav-item"        data-tab="settings" onclick="tab(this,'settings')">
        <span class="ico">⚙</span> Settings
      </div>
    </div>
  </nav>

  <div class="content">

    <!-- ── Dashboard ──────────────────────────────────────────────────── -->
    <div class="panel active" id="panel-dash">
      <div>
        <div class="panel-title">Dashboard</div>
        <div class="panel-sub" id="dash-sub">Loading…</div>
      </div>

      <!-- Domain warning / ok banner -->
      <div id="domain-warn-banner" class="alert-box alert-err" style="display:none">
        ⚠ <b>No domain linked.</b> The mail server cannot go live without a real domain.
        <a href="#" onclick="tab(document.querySelector('[data-tab=domain]'),'domain');return false"
           style="color:var(--amber);margin-left:8px">→ Set up Domain</a>
      </div>
      <div id="domain-ok-banner" class="alert-box alert-ok" style="display:none">
        ✓ Domain linked: <b id="dash-domain">—</b>
        &nbsp;|&nbsp; Public IP: <b id="dash-ip">—</b>
      </div>

      <div class="card-grid">
        <div class="card">
          <div class="stat-label">Status</div>
          <div class="stat-value" id="ds-status">—</div>
          <div class="stat-sub"  id="ds-mode">—</div>
        </div>
        <div class="card">
          <div class="stat-label">Messages</div>
          <div class="stat-value" id="ds-msgs">—</div>
          <div class="stat-sub">in inbox</div>
        </div>
        <div class="card">
          <div class="stat-label">Accounts</div>
          <div class="stat-value" id="ds-accs">—</div>
          <div class="stat-sub">mailboxes</div>
        </div>
        <div class="card">
          <div class="stat-label">DKIM</div>
          <div class="stat-value" id="ds-dkim" style="font-size:16px">—</div>
          <div class="stat-sub">signing key</div>
        </div>
      </div>

      <div class="info-box" id="dash-info-dev" style="display:none">
        <b>Dev Mode — Mailpit</b> &nbsp;(local catch-all, no real delivery)<br>
        SMTP: <b>localhost:1025</b> &nbsp;|&nbsp; Mailpit UI:
        <a href="http://localhost:8025" target="_blank" style="color:var(--green)">localhost:8025 ↗</a><br>
        <span class="note">Configure PHP apps to send to <b>localhost:1025</b> (no auth required).</span>
      </div>
      <div class="info-box" id="dash-info-live" style="display:none">
        <b>Live Mode — docker-mailserver</b><br>
        SMTP (587 STARTTLS): <b id="d-smtp">—</b> &nbsp;|&nbsp; IMAP (143): <b id="d-imap">—</b><br>
      </div>

      <div class="btn-row">
        <button class="btn btn-green" id="dash-start" onclick="startSrv()">▶ Start Server</button>
        <button class="btn btn-red"   id="dash-stop"  onclick="stopSrv()" style="display:none">■ Stop Server</button>
        <button class="btn btn-ghost btn-sm" onclick="runDiagnostics()" id="diag-btn">🔍 Run Diagnostics</button>
        <button class="btn btn-ghost btn-sm" onclick="fixAmavis()" id="fix-amavis-btn" title="Fix attachment delivery">📎 Configure Attachments</button>
      </div>

      <!-- Diagnostics panel -->
      <div id="diag-panel" style="display:none">
        <div class="section-title" style="margin-top:4px">Delivery Diagnostics</div>
        <div id="diag-log" style="font-family:'Menlo','Consolas',monospace;font-size:11px;
             background:var(--log-bg);border:1px solid var(--border);border-radius:8px;
             padding:12px 14px;max-height:260px;overflow-y:auto;line-height:1.8"></div>

        <div class="section-title" style="margin-top:12px">Recent Mail Log</div>
        <pre id="diag-maillog" style="font-family:'Menlo','Consolas',monospace;font-size:10.5px;
             background:var(--log-bg);border:1px solid var(--border);border-radius:8px;
             padding:12px 14px;max-height:240px;overflow-y:auto;
             white-space:pre-wrap;word-break:break-all;color:var(--dim);line-height:1.6"></pre>

        <div class="section-title" style="margin-top:12px">Common Fixes</div>
        <div class="info-box">
          <b>Port 25 blocked by VPS provider?</b> — Most cloud providers block port 25 by default.
          Check your provider's support docs to request port 25 unblocking:<br>
          DigitalOcean: <span style="color:var(--green)">Support ticket → "Enable SMTP"</span> &nbsp;|&nbsp;
          Vultr: <span style="color:var(--green)">Account → SMTP Settings</span> &nbsp;|&nbsp;
          Linode/Akamai: <span style="color:var(--green)">Support ticket</span><br><br>
          <b>OS firewall (UFW)?</b> — Run on your server:
          <code style="color:var(--green);display:block;margin-top:4px">
            sudo ufw allow 25,587,143,993/tcp<br>
            sudo ufw reload
          </code><br>
          <b>PTR record</b> — Set at your VPS provider's network/IP settings panel, not Cloudflare.<br>
          <b>MX record</b> — Verify via: <code style="color:var(--green)">dig MX phcast.com +short</code>
        </div>
      </div>
    </div>

    <!-- ── Domain ─────────────────────────────────────────────────────── -->
    <div class="panel" id="panel-domain">
      <div>
        <div class="panel-title">Domain Setup</div>
        <div class="panel-sub">Auto-configures DNS for your mail server on a Cloudflare-managed domain</div>
      </div>

      <!-- Current status banner -->
      <div id="dom-status-banner" class="alert-box alert-ok" style="display:none">
        <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px">
          <div>
            ✓ Configured: <b id="dom-banner-host">—</b>
            &nbsp;|&nbsp; IP: <b id="dom-banner-ip">—</b>
            &nbsp;|&nbsp; DKIM: <span id="dom-banner-dkim">—</span>
          </div>
          <button class="btn btn-ghost btn-sm" onclick="showDomainForm()">✎ Change</button>
        </div>
      </div>

      <!-- Setup form (hidden when already configured) -->
      <div id="dom-form" class="card">
        <div class="section-title">Auto-Setup</div>

        <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;margin-bottom:12px">
          <div class="form-row" style="flex:1;min-width:200px">
            <label for="zone-select">Cloudflare zone (shared with your PHP site)</label>
            <select class="inp" id="zone-select" onchange="onZoneSelect()">
              <option value="">Loading zones…</option>
            </select>
          </div>
          <div class="form-row" style="width:200px">
            <label for="domain-ip">Server public IP</label>
            <div style="display:flex;gap:4px">
              <input class="inp inp-sm" id="domain-ip" placeholder="Detecting…" type="text" style="flex:1">
              <button class="btn btn-ghost btn-sm" onclick="refreshIP()" title="Detect IP" style="flex-shrink:0;padding:5px 9px">↻</button>
            </div>
          </div>
          <button class="btn btn-ghost btn-sm" onclick="refreshZones()" style="flex-shrink:0;align-self:flex-end;margin-bottom:1px">↻ Zones</button>
        </div>

        <div id="zone-preview" style="display:none;margin-bottom:12px" class="info-box">
          Mail FQDN: <b id="zp-host">—</b> &nbsp;|&nbsp; Zone ID: <code id="zp-zone" style="color:var(--muted)">—</code>
        </div>

        <div class="btn-row">
          <button class="btn btn-green" id="btn-setup-all" onclick="setupAll()" disabled>
            ⚡ Provision DNS + Schedule DKIM
          </button>
          <button class="btn btn-ghost btn-sm" onclick="verifyDNS()">✓ Verify Records</button>
        </div>
        <div id="provision-result" style="display:none;margin-top:10px"></div>
      </div>

      <!-- DNS record status -->
      <div class="card">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
          <div class="section-title" style="margin-bottom:0;border-bottom:none;padding-bottom:0">DNS Record Status</div>
          <button class="btn btn-ghost btn-sm" onclick="verifyDNS()">↻ Check</button>
        </div>
        <table class="dns-table">
          <thead><tr><th>Record</th><th>Name</th><th>Value</th><th>Status</th></tr></thead>
          <tbody id="dns-tbody">
            <tr><td colspan="4" style="color:var(--muted);padding:12px;text-align:center">Click Check to verify</td></tr>
          </tbody>
        </table>
      </div>

      <!-- DKIM -->
      <div class="card">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
          <div>
            <b style="font-size:12px">DKIM Signing Key</b>
            <span id="dkim-status-pill" style="font-size:10px;margin-left:8px;color:var(--muted)">pending</span>
          </div>
          <button class="btn btn-ghost btn-sm" onclick="triggerDKIM()">⚙ Generate &amp; Upload DKIM</button>
        </div>
        <div class="panel-sub">
          Generated inside docker-mailserver, uploaded to Cloudflare DNS automatically.
          Run after the server has started for the first time.
        </div>
        <div id="dkim-result" style="margin-top:8px;display:none"></div>
      </div>

      <!-- Deliverability -->
      <div class="card">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
          <div class="section-title" style="margin-bottom:0;border-bottom:none;padding-bottom:0">Deliverability Checklist</div>
          <button class="btn btn-ghost btn-sm" onclick="checkPorts()">↻ Check Ports</button>
        </div>
        <div style="display:flex;flex-direction:column;gap:12px;font-size:11px">

          <!-- Outbound port 25 -->
          <div class="checklist-row" id="cl-p25">
            <span class="cl-ico" id="di-p25-ico">!</span>
            <div style="flex:1">
              <b>Outbound port 25 unblocked</b>
              <span id="di-p25-status" style="margin-left:6px;color:var(--muted)">Not checked</span><br>
              <div id="di-p25-detail" style="font-size:11px;color:var(--dim);margin-top:3px">
                OVH blocks outbound port 25 by default. Without it Postfix can't reach Gmail, Outlook, etc.
                <br>Fix: <a href="https://manager.us.ovhcloud.com" target="_blank" style="color:var(--green)">manager.us.ovhcloud.com</a>
                → open a support ticket → request <b>"Unblock outbound port 25 (SMTP)"</b>.
                Usually approved in a few hours.
              </div>
            </div>
          </div>

          <!-- PTR — auto-verified -->
          <div class="checklist-row" id="cl-ptr">
            <span class="cl-ico" id="di-ptr-ico">⟳</span>
            <div style="flex:1">
              <b>PTR / Reverse DNS</b>
              <span id="di-ptr-status" style="margin-left:6px;color:var(--muted)">Checking…</span><br>
              Required: <code id="di-ptr-val" style="color:var(--green)">—</code><br>
              <div id="di-ptr-detail" style="margin-top:4px;color:var(--dim)">
                Contact your ISP or set via your router/modem admin panel.
                Gmail rejects without PTR (<code style="color:var(--red)">550 5.7.1 IP not authorized</code>).
              </div>
            </div>
          </div>

          <!-- DKIM -->
          <div class="checklist-row">
            <span class="cl-ico" id="di-dkim-ico">◌</span>
            <div><b>DKIM signing</b> — click Generate &amp; Upload DKIM above after server first start</div>
          </div>

          <!-- UFW -->
          <div class="checklist-row">
            <span class="cl-ico" id="di-ufw-ico">!</span>
            <div style="flex:1">
              <b>UFW Firewall ports</b><br>
              <div id="di-ports" style="display:flex;gap:6px;flex-wrap:wrap;margin:5px 0">
                <span class="port-chip" id="port-25">25</span>
                <span class="port-chip" id="port-587">587</span>
                <span class="port-chip" id="port-143">143</span>
                <span class="port-chip" id="port-993">993</span>
              </div>
              <span id="di-ufw-note" style="font-size:11px;color:var(--dim)">
                <code style="color:var(--green)">sudo ufw allow 25,587,143,993/tcp &amp;&amp; sudo ufw reload</code>
              </span>
            </div>
          </div>

          <!-- Google Postmaster — persistent -->
          <div class="checklist-row" id="cl-gpt">
            <span class="cl-ico" id="di-gpt-ico">!</span>
            <div style="flex:1">
              <b>Google Postmaster Tools</b> —
              <a href="https://postmaster.google.com" target="_blank" style="color:var(--green)">postmaster.google.com ↗</a><br>
              <div id="gpt-done-msg" style="display:none;color:var(--green);margin-top:3px">
                ✓ TXT record added — click Verify in Postmaster Tools to complete verification
              </div>
              <div id="gpt-form" style="margin-top:6px">
                Paste the TXT record Google gives you:
                <div style="display:flex;gap:6px;margin-top:5px;align-items:center">
                  <input class="inp inp-sm" id="gpt-txt" style="flex:1"
                         placeholder="google-site-verification=…">
                  <button class="btn btn-green btn-sm" onclick="addGptRecord()">Add to CF DNS</button>
                </div>
              </div>
              <div id="gpt-result" style="display:none;margin-top:6px"></div>
            </div>
          </div>

          <!-- Blocklist — persistent click -->
          <div class="checklist-row" id="cl-bl">
            <span class="cl-ico" id="di-bl-ico">!</span>
            <div>
              <b>Blocklist check</b> —
              <a href="https://mxtoolbox.com/blacklists.aspx" target="_blank"
                 style="color:var(--green)" onclick="markBlocklistChecked()">MXToolbox ↗</a>
              <span id="di-bl-msg" style="display:none;color:var(--green);margin-left:6px">✓ Checked</span>
            </div>
          </div>

        </div>
      </div>
    </div>

    <!-- ── Inbox ──────────────────────────────────────────────────────── -->
    <div class="panel" id="panel-inbox" style="padding:0;gap:0;overflow:hidden;flex-direction:column">
      <div class="inbox-shell">
        <div class="msg-list-col">
          <div class="inbox-toolbar">
            <div class="search-wrap">
              <span class="search-ico">⌕</span>
              <input id="sq" type="text" placeholder="Search…"
                     onkeydown="if(event.key==='Enter')loadInbox(1)">
            </div>
            <select class="inp inp-sm" id="folder-sel" onchange="onFolderChange()"
                    style="width:90px;display:none"></select>
            <button class="btn btn-ghost btn-sm" id="inbox-refresh-btn"
                    onclick="refreshInbox()" title="Refresh">↻</button>
            <button class="btn btn-red btn-sm" id="empty-trash-btn"
                    onclick="emptyTrash()" title="Empty Trash"
                    style="display:none;font-size:11px">🗑 Empty Trash</button>
          </div>
          <div class="acc-bar" id="acc-bar" style="display:none">
            <span style="flex-shrink:0">Account:</span>
            <select class="acc-sel" id="acc-sel" onchange="onAccChange()"></select>
          </div>
          <div class="msg-list" id="msg-list">
            <div class="empty-state"><div class="ico">✉</div><div>No messages</div></div>
          </div>
          <div class="pgn">
            <span id="pgn-info">0 messages</span>
            <div style="display:flex;gap:4px">
              <button class="btn btn-ghost btn-sm" id="pgn-p" onclick="changePage(-1)" disabled>‹</button>
              <button class="btn btn-ghost btn-sm" id="pgn-n" onclick="changePage(1)"  disabled>›</button>
            </div>
          </div>
        </div>
        <div class="msg-reader-col">
          <div id="reader-ph" class="reader-placeholder">
            <div class="ico">📭</div><div>Select a message</div>
          </div>
          <div id="reader" style="display:none;flex-direction:column;flex:1;overflow:hidden">
            <div class="reader-head">
              <div class="reader-subj" id="r-subj">—</div>
              <div class="reader-meta">
                <b>From:</b> <span id="r-from">—</span><br>
                <b>To:</b>   <span id="r-to">—</span><br>
                <b>Date:</b> <span id="r-date">—</span>
              </div>
            </div>
            <div class="reader-bar">
              <button class="btn btn-ghost btn-sm" onclick="deleteOpen()">🗑 Delete</button>
              <button class="btn btn-ghost btn-sm" onclick="replyTo()">↩ Reply</button>
              <button class="btn btn-ghost btn-sm" id="html-tog"
                      onclick="toggleHtml()" style="display:none">⬜ HTML</button>
            </div>
            <div class="reader-body">
              <div class="reader-text" id="r-text"></div>
              <div id="r-html" style="display:none">
                <iframe id="r-frame" sandbox="allow-same-origin"
                        style="width:100%;min-height:400px;border:none;background:#fff;border-radius:6px"></iframe>
              </div>
            </div>
            <div id="r-att" style="display:none;padding:8px 16px;border-top:1px solid var(--border);
                 display:flex;gap:6px;flex-wrap:wrap;background:var(--log-bg)"></div>
          </div>
        </div>
      </div>
    </div>

    <!-- ── Compose ────────────────────────────────────────────────────── -->
    <div class="panel" id="panel-compose" style="padding:0;gap:0;overflow:hidden;flex-direction:column">
      <div class="compose-shell">

        <!-- Compose header -->
        <div class="compose-header">
          <div style="display:flex;align-items:center;gap:10px">
            <span style="font-size:15px;font-weight:700;color:var(--text)">New Message</span>
            <span class="mode-badge mode-live" id="c-mode-label" style="font-size:9px">LIVE</span>
          </div>
          <div style="display:flex;gap:6px">
            <button class="btn btn-ghost btn-sm" onclick="saveDraft()" title="Save Draft">💾 Draft</button>
            <button class="btn btn-ghost btn-sm" onclick="discardCompose()" title="Discard">✕ Discard</button>
            <button class="btn btn-green" id="c-send-btn" onclick="sendEmail()">
              <span id="c-send-ico">&#9658;</span> Send
            </button>
          </div>
        </div>

        <!-- Address fields -->
        <div class="compose-fields">
          <div class="cf-row">
            <span class="cf-label">From</span>
            <select class="cf-input" id="c-from-sel" style="cursor:pointer"></select>
          </div>
          <div class="cf-row">
            <span class="cf-label">To</span>
            <div class="cf-chips" id="c-to-chips" onclick="document.getElementById('c-to-inp').focus()">
              <input id="c-to-inp" class="cf-chip-input" type="email"
                     placeholder="recipient@example.com"
                     onkeydown="handleToKey(event)">
            </div>
          </div>
          <div class="cf-row" id="cc-row" style="display:none">
            <span class="cf-label">Cc</span>
            <input id="c-cc" class="cf-input" type="text" placeholder="cc@example.com">
          </div>
          <div class="cf-row" id="bcc-row" style="display:none">
            <span class="cf-label">Bcc</span>
            <input id="c-bcc" class="cf-input" type="text" placeholder="bcc@example.com">
          </div>
          <div class="cf-row">
            <span class="cf-label">Subject</span>
            <input id="c-sub" class="cf-input" type="text" placeholder="Subject">
            <div style="display:flex;gap:4px;flex-shrink:0">
              <button class="compose-pill" onclick="toggleRow('cc-row','Cc')"  id="pill-cc">Cc</button>
              <button class="compose-pill" onclick="toggleRow('bcc-row','Bcc')" id="pill-bcc">Bcc</button>
            </div>
          </div>
        </div>

        <!-- Toolbar -->
        <div class="compose-toolbar">
          <button class="tb-btn" onclick="fmt('bold')"       title="Bold"><b>B</b></button>
          <button class="tb-btn" onclick="fmt('italic')"     title="Italic"><i>I</i></button>
          <button class="tb-btn" onclick="fmt('underline')"  title="Underline"><u>U</u></button>
          <div class="tb-sep"></div>
          <button class="tb-btn" onclick="fmt('insertUnorderedList')" title="Bullet list">≡</button>
          <button class="tb-btn" onclick="fmt('insertOrderedList')"   title="Numbered list">№</button>
          <div class="tb-sep"></div>
          <button class="tb-btn" onclick="insertLink()"  title="Insert link">🔗</button>
          <div class="tb-sep"></div>
          <button class="tb-btn" onclick="document.getElementById('c-file-inp').click()" title="Attach file">📎</button>
          <input type="file" id="c-file-inp" multiple style="display:none" onchange="handleAttach(event)">
          <div class="tb-sep"></div>
          <select class="tb-sel" onchange="fmt('fontSize',this.value);this.value=3" title="Font size">
            <option value="3">Size</option>
            <option value="1">Small</option>
            <option value="3">Normal</option>
            <option value="5">Large</option>
            <option value="7">Huge</option>
          </select>
          <div style="flex:1"></div>
          <span id="c-char-count" style="font-size:10px;color:var(--muted)"></span>
        </div>

        <!-- Attachment chips -->
        <div id="c-attachments" style="display:none;padding:6px 12px;border-bottom:1px solid var(--border);
             background:var(--log-bg);display:none;flex-wrap:wrap;gap:6px;align-items:center">
          <span style="font-size:10px;color:var(--muted);flex-shrink:0">📎</span>
          <div id="c-att-chips" style="display:flex;flex-wrap:wrap;gap:5px;flex:1"></div>
        </div>

        <!-- Body editor -->
        <div id="c-editor" class="compose-editor" contenteditable="true"
             oninput="updateCharCount()"
             onpaste="handlePaste(event)"
             data-placeholder="Write your message…"></div>

        <!-- Status bar -->
        <div class="compose-status">
          <span id="c-status" style="font-size:11px;color:var(--muted)">Ready</span>
          <span id="c-autosave" style="font-size:10px;color:var(--muted)"></span>
        </div>
      </div>
    </div>

    <!-- ── Accounts ──────────────────────────────────────────────────── -->
    <div class="panel" id="panel-accounts">
      <div><div class="panel-title">Mailbox Accounts</div>
        <div class="panel-sub">Manage mailboxes on docker-mailserver (Live mode only)</div></div>
      <div id="acc-live" style="display:none">
        <div class="section-title" style="margin-bottom:10px">Add Mailbox</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px">
          <input class="inp inp-sm" id="ne" type="email"
                 placeholder="user@yourdomain.com" style="flex:1;min-width:180px">
          <input class="inp inp-sm" id="np" type="password"
                 placeholder="Password" style="width:150px">
          <button class="btn btn-green btn-sm" onclick="addAcc()">+ Add</button>
        </div>
        <div class="section-title" style="margin-bottom:10px">Existing Accounts</div>
        <table><thead><tr><th>Email</th><th>Created</th><th></th></tr></thead>
          <tbody id="acc-tbody"></tbody></table>

        <!-- Edit Account Modal -->
        <div id="acc-edit-modal" style="display:none;margin-top:16px" class="card">
          <div class="section-title" style="margin-bottom:10px">Edit Account</div>
          <div style="display:flex;flex-direction:column;gap:10px">
            <div class="form-row">
              <label>Email</label>
              <input class="inp inp-sm" id="edit-email" readonly style="color:var(--muted)">
            </div>
            <div class="form-row">
              <label>Display Name <span style="color:var(--muted)">(shown to recipients)</span></label>
              <input class="inp inp-sm" id="edit-name" placeholder="e.g. Mitch @ PHCast">
            </div>
            <div class="form-row">
              <label>New Password <span style="color:var(--muted)">(leave blank to keep current)</span></label>
              <input class="inp inp-sm" id="edit-pw" type="password" placeholder="New password">
            </div>
            <div class="form-row">
              <label>Email Signature <span style="color:var(--muted)">(HTML supported)</span></label>
              <textarea class="inp inp-sm" id="edit-sig" rows="5"
                style="resize:vertical;font-family:inherit;font-size:11px"
                placeholder="<p>Best,<br><strong>Mitch</strong><br>PHCast</p>"></textarea>
            </div>
            <div class="btn-row">
              <button class="btn btn-green btn-sm" onclick="saveAccEdit()">Save Changes</button>
              <button class="btn btn-ghost btn-sm" onclick="closeAccEdit()">Cancel</button>
            </div>
          </div>
        </div>
      </div>
      <div id="acc-dev" class="info-box" style="display:none">
        <b>Dev mode (Mailpit)</b> — no accounts needed. Mailpit catches all SMTP regardless of
        recipient.<br>Switch to <b>Live mode</b> in Settings to manage real mailboxes.
      </div>
    </div>

    <!-- ── Spam Filter ───────────────────────────────────────────────── -->
    <div class="panel" id="panel-spam">
      <div><div class="panel-title">Spam Filter</div>
        <div class="panel-sub">SpamAssassin + Rspamd inside docker-mailserver (Live mode)</div></div>
      <div class="spam-grid">
        <div class="card">
          <div class="section-title">Engines &amp; Thresholds</div>
          <div class="toggle-row"><span class="toggle-label">Spam filtering</span>
            <label class="tw"><input type="checkbox" id="sp-en" checked><span class="ts"></span></label></div>
          <div class="toggle-row"><span class="toggle-label">SpamAssassin</span>
            <label class="tw"><input type="checkbox" id="sp-sa" checked><span class="ts"></span></label></div>
          <div class="toggle-row"><span class="toggle-label">Rspamd</span>
            <label class="tw"><input type="checkbox" id="sp-rs"><span class="ts"></span></label></div>
          <div class="toggle-row"><span class="toggle-label">DKIM verify</span>
            <label class="tw"><input type="checkbox" id="sp-dk" checked><span class="ts"></span></label></div>
          <div class="toggle-row"><span class="toggle-label">SPF check</span>
            <label class="tw"><input type="checkbox" id="sp-spf" checked><span class="ts"></span></label></div>
          <div class="toggle-row"><span class="toggle-label">Block known-bad</span>
            <label class="tw"><input type="checkbox" id="sp-bb" checked><span class="ts"></span></label></div>
          <div class="toggle-row"><span class="toggle-label">Move spam to Junk</span>
            <label class="tw"><input type="checkbox" id="sp-q" checked><span class="ts"></span></label></div>
          <div style="margin-top:12px">
            <div class="score-row"><label for="sl-tag">Tag score</label>
              <input type="range" id="sl-tag"  min="0" max="10" step=".5" value="2"
                     oninput="document.getElementById('sv-tag').textContent=this.value">
              <span class="sv" id="sv-tag">2.0</span></div>
            <div class="score-row"><label for="sl-spam">Spam score</label>
              <input type="range" id="sl-spam" min="1" max="15" step=".5" value="5"
                     oninput="document.getElementById('sv-spam').textContent=this.value">
              <span class="sv" id="sv-spam">5.0</span></div>
            <div class="score-row"><label for="sl-kill">Reject / kill</label>
              <input type="range" id="sl-kill" min="5" max="30" step=".5" value="15"
                     oninput="document.getElementById('sv-kill').textContent=this.value">
              <span class="sv" id="sv-kill">15.0</span></div>
          </div>
          <div class="form-row" style="margin-top:10px">
            <label>Subject prefix</label>
            <input class="inp inp-sm" id="sp-stag" value="[SPAM]">
          </div>
        </div>
        <div style="display:flex;flex-direction:column;gap:10px">
          <div class="card">
            <div class="section-title">Whitelist — always allow</div>
            <div class="tag-list" id="wl-list"></div>
            <div style="display:flex;gap:6px;margin-top:6px">
              <input class="inp inp-sm" id="wl-inp" placeholder="user@domain.com"
                     style="flex:1" onkeydown="if(event.key==='Enter')addTag('wl')">
              <button class="btn btn-ghost btn-sm" onclick="addTag('wl')">Add</button>
            </div>
          </div>
          <div class="card">
            <div class="section-title">Blacklist — always block</div>
            <div class="tag-list" id="bl-list"></div>
            <div style="display:flex;gap:6px;margin-top:6px">
              <input class="inp inp-sm" id="bl-inp" placeholder="spammer@example.com"
                     style="flex:1" onkeydown="if(event.key==='Enter')addTag('bl')">
              <button class="btn btn-ghost btn-sm" onclick="addTag('bl')">Add</button>
            </div>
          </div>
          <div class="card">
            <div class="section-title">Custom SpamAssassin rules (.cf)</div>
            <textarea class="inp" id="sp-custom" rows="5"
              placeholder="# raw SA rules&#10;score RCVD_IN_DNSWL_HI -8&#10;header MY_RULE Subject =~ /[Vv]iagra/&#10;score  MY_RULE 5.0"></textarea>
          </div>
          <div class="btn-row">
            <button class="btn btn-green" onclick="saveSpam()">💾 Save &amp; Apply</button>
            <button class="btn btn-ghost btn-sm" onclick="loadSpam()">↻ Reload</button>
          </div>
        </div>
      </div>
    </div>

    <!-- ── Settings ──────────────────────────────────────────────────── -->
    <div class="panel" id="panel-settings">
      <div><div class="panel-title">Settings</div>
        <div class="panel-sub">Mode switch only — domain is configured in the Domain tab</div></div>
      <div style="max-width:480px;display:flex;flex-direction:column;gap:14px">
        <div class="card">
          <div class="section-title">Mode</div>
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            <label style="flex:1;cursor:pointer">
              <input type="radio" name="smode" id="m-dev" value="dev" onchange="saveMode()">
              <div class="info-box" style="margin-top:4px">
                <b>Dev — Mailpit</b><br>
                <span class="note">Local catch-all. No real delivery. Ideal for PHP dev.</span>
              </div>
            </label>
            <label style="flex:1;cursor:pointer">
              <input type="radio" name="smode" id="m-live" value="live" onchange="saveMode()">
              <div class="info-box" style="margin-top:4px">
                <b>Live — docker-mailserver</b><br>
                <span class="note">Real delivery. Requires domain setup (see Domain tab).</span>
              </div>
            </label>
          </div>
        </div>
        <div class="alert-box alert-warn">
          <b>Cloudflare token</b> is read from <code>~/.phpmngr/cloudflare.json</code>
          (shared with PHP-MNGR). Configure it there — MAIL-SRVR never stores its own copy.
        </div>
      </div>
    </div>

  </div><!-- content -->
</div><!-- shell -->

<footer>
  KillTheHost MAIL-SRVR v1.1 &nbsp;|&nbsp;
  <a href="https://killthehost.com" target="_blank">killthehost.com</a>
  &nbsp;|&nbsp; AGPL-3.0
</footer>
<div id="toast"></div>

<script>
// ── state ──────────────────────────────────────────────────────────────────
let st = {}, inboxPage=1, inboxTotal=0, inboxLimit=25;
let curMsg=null, curAcc="", curFolder="INBOX", htmlMode=false;
let wl=[], bl=[];
let zones=[];   // [{name, zone_id, source}]
let selZone = null;

// ── boot ───────────────────────────────────────────────────────────────────
window.addEventListener("DOMContentLoaded", ()=>{
  // Measure header + footer height and set shell height to exact remainder
  function setShellHeight(){
    const hdr = document.querySelector("header");
    const ftr = document.querySelector("footer");
    const sh  = hdr && ftr
      ? (window.innerHeight - hdr.offsetHeight - ftr.offsetHeight) + "px"
      : "calc(100vh - 82px)";
    document.documentElement.style.setProperty("--shell-h", sh);
  }
  setShellHeight();
  window.addEventListener("resize", setShellHeight);
  poll(); setInterval(poll, 3000);
  // Auto-refresh the inbox every 15 s when it is the active panel
  setInterval(()=>{
    if(document.getElementById("panel-inbox")?.classList.contains("active")){
      loadInbox();   // keeps current page + folder, just fetches new data
    }
  }, 15000);
  loadSpam(); loadDomainTab();
});

// ── nav ────────────────────────────────────────────────────────────────────
function tab(el, id){
  document.querySelectorAll(".nav-item").forEach(n=>n.classList.remove("active"));
  document.querySelectorAll(".panel").forEach(p=>p.classList.remove("active"));
  el.classList.add("active");
  document.getElementById("panel-"+id).classList.add("active");
  if(id==="inbox")   { fetchAccounts().then(()=>{ loadInbox(); loadFolders(); }); }
  if(id==="accounts"){ loadAccounts(); }
  if(id==="domain")  { loadDomainTab(); }
  if(id==="compose") { initCompose(); }
}

// ── status poll ────────────────────────────────────────────────────────────
async function poll(){
  const s = await api("/api/status"); if(!s) return;
  st = s;
  const running = s.running;
  document.getElementById("dot").className      = "dot"+(running?" live":"");
  document.getElementById("st-text").textContent= running?"Running":"Stopped";
  const mb = document.getElementById("mode-badge");
  mb.textContent  = s.mode==="dev"?"DEV":"LIVE";
  mb.className    = "mode-badge "+(s.mode==="dev"?"mode-dev":"mode-live");
  ["btn-start","dash-start"].forEach(id=>{
    const el=document.getElementById(id); if(el) el.style.display=running?"none":"";
  });
  ["btn-stop","dash-stop"].forEach(id=>{
    const el=document.getElementById(id); if(el) el.style.display=running?"":"none";
  });

  // Dashboard cards
  document.getElementById("ds-status").textContent = running?"Running":"Stopped";
  document.getElementById("ds-status").style.color = running?"var(--green)":"var(--red)";
  document.getElementById("ds-mode").textContent   = s.mode==="dev"?"Dev — Mailpit":"Live — docker-mailserver";
  document.getElementById("ds-msgs").textContent   = s.msg_count??"-";
  document.getElementById("ds-accs").textContent   = s.acc_count??"-";
  document.getElementById("ds-dkim").textContent   = s.dkim_done?"✓":"Pending";
  document.getElementById("ds-dkim").style.color   = s.dkim_done?"var(--green)":"var(--amber)";

  const domOk = s.domain_ok;
  document.getElementById("domain-warn-banner").style.display = (!domOk&&s.mode!=="dev")?"":"none";
  document.getElementById("domain-ok-banner").style.display   = domOk?"":"none";
  if(domOk){
    document.getElementById("dash-domain").textContent = s.mail_host||s.domain;
    document.getElementById("dash-ip").textContent     = s.public_ip||"—";
    document.getElementById("d-smtp").textContent      = s.mail_host+":587";
    document.getElementById("d-imap").textContent      = s.mail_host+":143";
  }
  document.getElementById("dash-info-dev").style.display  = s.mode==="dev"?"":"none";
  document.getElementById("dash-info-live").style.display = s.mode==="live"?"":"none";
  document.getElementById("dash-sub").textContent =
    running ? (s.mode==="dev"?"Mailpit running":"Serving "+s.mail_host) : "Server stopped";

  // Nav domain badge
  const navDom = document.getElementById("nav-domain");
  if(domOk||s.mode==="dev") navDom.classList.remove("warn");
  else                      navDom.classList.add("warn");

  const nb = document.getElementById("nb-inbox");
  if(nb && s.mode === "dev"){
    const cnt = s.msg_count ?? 0;
    nb.textContent    = cnt > 0 ? cnt : "";
    nb.className      = "nb" + (cnt > 0 ? " unread" : "");
    nb.style.display  = cnt > 0 ? "" : "none";
  }
  document.getElementById("nb-acc").textContent = s.acc_count??0;
}

// ── server start/stop ─────────────────────────────────────────────────────
async function startSrv(){
  if(st.mode==="live" && !st.domain_ok){
    toast("Domain must be configured before starting Live mode","err");
    tab(document.querySelector("[data-tab=domain]"),"domain"); return;
  }
  toast("Starting…");
  const r = await api("/api/start","POST");
  toast(r?.msg||(r?.ok?"Started":r?.error), r?.ok?"ok":"err");
  poll();
}
async function stopSrv(){
  toast("Stopping…");
  const r = await api("/api/stop","POST");
  toast(r?.ok?"Stopped":r?.error, r?.ok?"ok":"err");
  poll();
}

async function fixAmavis(){
  const btn = document.getElementById("fix-amavis-btn");
  btn.disabled=true; btn.textContent="Fixing…";
  const r = await api("/api/fix/amavis","POST");
  btn.disabled=false; btn.textContent="📎 Configure Attachments";
  if(r?.ok){
    toast(r.msg,"ok");
    // Show steps
    const panel = document.getElementById("diag-panel");
    const log   = document.getElementById("diag-log");
    if(panel && log){
      panel.style.display="";
      log.innerHTML += `<div style="margin-top:8px;color:var(--green)">
        <b>Amavis Fix Applied:</b><br>
        ${(r.steps||[]).map(s=>`• ${esc(s)}`).join("<br>")}
      </div>`;
    }
    // Now check what amavis says
    const r2 = await api("/api/amavis/log");
    if(r2?.ok && log){
      log.innerHTML += `<div style="margin-top:8px">
        <b>Override config:</b><pre style="font-size:10px;white-space:pre-wrap;color:var(--green)">${esc(r2.override_config)}</pre>
        <b>Postfix content_filter:</b> <code>${esc(r2.postfix_content_filter)}</code>
      </div>`;
    }
  } else {
    toast(r?.error||"Failed","err");
  }
}

async function runDiagnostics(){
  const btn = document.getElementById("diag-btn");
  const panel = document.getElementById("diag-panel");
  const log   = document.getElementById("diag-log");
  const mlog  = document.getElementById("diag-maillog");
  btn.disabled = true; btn.textContent = "🔍 Running…";
  panel.style.display = "";
  log.innerHTML  = '<span style="color:var(--muted)">Running checks…</span>';
  mlog.textContent = "";

  const r = await api("/api/diagnostics");
  btn.disabled = false; btn.textContent = "🔍 Run Diagnostics";

  if(!r){ log.innerHTML='<span style="color:var(--red)">Request failed</span>'; return; }

  const icons = {true:"<span style='color:var(--green)'>✓</span>",
                 false:"<span style='color:var(--red)'>✗</span>",
                 null:"<span style='color:var(--amber)'>?</span>"};
  const rows = Object.entries(r.checks||{})
    .filter(([k])=> k !== "mail_log" && k !== "firewall")
    .map(([k, v])=>`
      <div style="display:flex;gap:10px;padding:3px 0;border-bottom:1px solid var(--border)">
        <span style="flex-shrink:0;width:16px">${icons[String(v.ok)]??icons["null"]}</span>
        <span style="flex-shrink:0;min-width:260px;color:var(--text)">${esc(v.label)}</span>
        <span style="color:var(--dim);font-size:10.5px">${esc(String(v.value||""))}</span>
      </div>`).join("");

  // Firewall row
  const fw = r.checks?.firewall;
  const fwRow = fw ? `
    <div style="margin-top:6px;padding:6px 0;border-top:1px solid var(--border)">
      <div style="color:var(--muted);font-size:10px;margin-bottom:4px">FIREWALL RULES</div>
      <pre style="font-size:10px;white-space:pre-wrap;color:var(--dim)">${esc(fw.value)}</pre>
    </div>` : "";

  log.innerHTML = rows + fwRow;

  // Mail log
  const ml = r.checks?.mail_log;
  mlog.textContent = ml?.value || "No log available";
  mlog.scrollTop   = mlog.scrollHeight;
}

// ── domain tab ────────────────────────────────────────────────────────────
async function loadDomainTab(){
  // Load in parallel: zones + config + IP
  const [zonesR, cfgR, statusR] = await Promise.all([
    api("/api/domains"),
    api("/api/config"),
    api("/api/status"),
  ]);

  const cfg    = cfgR?.config  || {};
  const status = statusR       || {};

  // Fill IP field
  const detectedIP = cfg.public_ip || status.public_ip || "";
  document.getElementById("domain-ip").value = detectedIP;

  // Populate zone dropdown
  const sel = document.getElementById("zone-select");
  if(!zonesR?.ok || !zonesR.domains?.length){
    sel.innerHTML = "<option value=''>No zones found — check Cloudflare token in PHP-MNGR</option>";
  } else {
    zones = zonesR.domains;
    sel.innerHTML = zones.map(z =>
      `<option value="${esc(z.name)}" data-zid="${esc(z.zone_id)}">${esc(z.name)}</option>`
    ).join("");
    // Auto-select: saved domain first, then first zone if only one
    let autoIdx = 0;
    if(cfg.domain){
      const idx = zones.findIndex(z => z.name === cfg.domain);
      if(idx >= 0) autoIdx = idx;
    }
    sel.selectedIndex = autoIdx;
    onZoneSelect();
    // Auto-enable button if IP known
    if(detectedIP) document.getElementById("btn-setup-all").disabled = false;
  }

  // Show configured banner if domain already set up
  updateDomainBanner(cfg, status);
  updateDelivChecklist(cfg, status);
  checkPorts();   // live port status on tab open
}

function updateDomainBanner(cfg, status){
  const banner = document.getElementById("dom-status-banner");
  const form   = document.getElementById("dom-form");
  if(cfg.domain && cfg.zone_id){
    document.getElementById("dom-banner-host").textContent = cfg.mail_host || `mail.${cfg.domain}`;
    document.getElementById("dom-banner-ip").textContent   = cfg.public_ip || "—";
    const dkimEl = document.getElementById("dom-banner-dkim");
    dkimEl.textContent = (cfg.dkim_done || status.dkim_done) ? "✓ Ready" : "Pending";
    dkimEl.style.color = (cfg.dkim_done || status.dkim_done) ? "var(--green)" : "var(--amber)";
    banner.style.display = "";
    form.style.display   = "none";   // hide form — already configured
  } else {
    banner.style.display = "none";
    form.style.display   = "";
  }
}

function showDomainForm(){
  document.getElementById("dom-status-banner").style.display = "none";
  document.getElementById("dom-form").style.display = "";
}

function updateDelivChecklist(cfg, status){
  const ip = cfg.public_ip || status.public_ip || "";
  const mh = cfg.mail_host || (cfg.domain ? `mail.${cfg.domain}` : "");
  const ptr = document.getElementById("di-ptr-val");
  if(ptr) ptr.textContent = (ip && mh) ? `${ip} → ${mh}` : "Configure domain first";

  const dkimDone = cfg.dkim_done || status.dkim_done;
  const di = document.getElementById("di-dkim-ico");
  if(di){ di.textContent = dkimDone ? "✓" : "◌"; di.style.color = dkimDone ? "var(--green)" : ""; }

  const dp = document.getElementById("dkim-status-pill");
  if(dp){ dp.textContent = dkimDone ? "✓ uploaded to DNS" : "pending — click Generate after server start";
          dp.style.color = dkimDone ? "var(--green)" : "var(--amber)"; }

  // Load persistent checklist state
  api("/api/checklist").then(r => applyChecklist(r?.checklist || {}));

  // Auto-check PTR and outbound port 25
  if(ip && mh) checkPTR();
  checkOutboundPort25();
}

async function checkOutboundPort25(){
  const ico    = document.getElementById("di-p25-ico");
  const status = document.getElementById("di-p25-status");
  const detail = document.getElementById("di-p25-detail");
  if(ico){ ico.textContent="⟳"; ico.className="cl-ico spin"; }
  if(status) status.textContent = "Checking…";

  const r = await api("/api/diagnostics");
  const check = r?.checks?.outbound_port25;

  if(!check){
    if(ico){ ico.textContent="?"; ico.className="cl-ico"; }
    if(status) status.textContent = "Run Diagnostics to check";
    return;
  }

  if(check.ok){
    if(ico){ ico.textContent="✓"; ico.className="cl-ico ok"; }
    if(status){ status.textContent="✓ Outbound port 25 open"; status.style.color="var(--green)"; }
    if(detail) detail.style.display="none";
    api("/api/checklist/update","POST",{key:"outbound_p25",value:true});
  } else {
    if(ico){ ico.textContent="!"; ico.className="cl-ico"; }
    if(status){ status.textContent="✗ Blocked by OVH"; status.style.color="var(--red)"; }
    if(detail){
      detail.style.display="";
      detail.innerHTML = `<div class="alert-box alert-err" style="margin-top:6px;font-size:11px;white-space:pre-wrap">${esc(check.value)}</div>`;
    }
  }
}

async function checkPTR(){
  const ico    = document.getElementById("di-ptr-ico");
  const status = document.getElementById("di-ptr-status");
  const detail = document.getElementById("di-ptr-detail");
  if(ico) { ico.textContent="⟳"; ico.className="cl-ico spin"; }
  if(status) status.textContent = "Checking…";

  const r = await api("/api/dns/ptr");
  if(!r){ if(ico){ico.textContent="!";ico.className="cl-ico";} return; }

  if(r.ok){
    // PTR matches mail_host exactly
    if(ico){ ico.textContent="✓"; ico.className="cl-ico ok"; }
    if(status){ status.textContent=`✓ Resolves to ${r.resolved}`; status.style.color="var(--green)"; }
    if(detail) detail.style.display="none";
    api("/api/checklist/update","POST",{key:"ptr",value:true});

  } else if(r.cgnat){
    // CGNAT / T-Mobile — not an error the user can fix, mark as "known limitation"
    if(ico){ ico.textContent="⚠"; ico.className="cl-ico"; ico.style.color="var(--amber)"; }
    if(status){ status.textContent=`${r.cgnat} detected`; status.style.color="var(--amber)"; }
    if(detail) detail.innerHTML = `
      <div class="alert-box alert-warn" style="margin-top:6px;font-size:11px">
        <b>${r.cgnat} 5G home internet uses CGNAT</b> — you share a carrier IP pool.<br><br>
        <b>What this means:</b><br>
        &bull; PTR records cannot be set — T-Mobile controls this IP block<br>
        &bull; Port 25 inbound is blocked at the carrier level<br>
        &bull; External senders (Gmail, Outlook) cannot reach your server directly<br><br>
        <b>What still works:</b><br>
        &bull; Sending mail outbound via your server ✓<br>
        &bull; IMAP/inbox access from your local network ✓<br>
        &bull; Receiving mail between accounts on your own server ✓<br><br>
        <b>To receive mail from Gmail/Outlook</b> you need a server with a real static IP
        (a $5/month VPS like DigitalOcean or Vultr, running MAIL-SRVR there instead).
        T-Mobile does not offer PTR or port 25 for home connections.
      </div>`;

  } else {
    if(r.resolved){
      // PTR exists but points to a different hostname — auto-fix will use it
      // Mark as green since the mail server handles this automatically
      if(ico){ ico.textContent="✓"; ico.className="cl-ico ok"; }
      if(status){
        status.textContent = `✓ Auto-configured — using ${r.resolved} as SMTP hostname`;
        status.style.color = "var(--green)";
      }
      if(detail){
        detail.innerHTML = `<div style="color:var(--green);font-size:11px;margin-top:4px">
          PTR record exists as <b>${esc(r.resolved)}</b>. The mail server automatically
          uses this hostname for HELO/EHLO to match your PTR — no action needed.
        </div>`;
      }
      // Persist as done
      api("/api/checklist/update","POST",{key:"ptr",value:true});
    } else {
      // No PTR at all
      if(ico){ ico.textContent="!"; ico.className="cl-ico"; }
      if(status){ status.textContent="✗ No PTR record set yet"; status.style.color="var(--red)"; }
      const hint = r.hint || `Set a PTR record for ${r.expected||""} in your hosting control panel.`;
      if(detail) detail.innerHTML = `<div class="alert-box alert-warn" style="margin-top:6px;font-size:11px;white-space:pre-wrap">${esc(hint)}</div>`;
    }
  }
}

function applyChecklist(cl){
  // Postmaster
  if(cl.postmaster?.done){
    const ico  = document.getElementById("di-gpt-ico");
    const form = document.getElementById("gpt-form");
    const done = document.getElementById("gpt-done-msg");
    if(ico){ ico.textContent="✓"; ico.className="cl-ico ok"; }
    if(form) form.style.display = "none";
    if(done) done.style.display = "";
  }
  // Blocklist
  if(cl.blocklist){
    const ico = document.getElementById("di-bl-ico");
    const msg = document.getElementById("di-bl-msg");
    if(ico){ ico.textContent="✓"; ico.className="cl-ico ok"; }
    if(msg) msg.style.display = "";
  }
  // PTR — restore green if previously verified
  if(cl.ptr){
    const ico    = document.getElementById("di-ptr-ico");
    const status = document.getElementById("di-ptr-status");
    const detail = document.getElementById("di-ptr-detail");
    if(ico){ ico.textContent="✓"; ico.className="cl-ico ok"; }
    if(status){ status.textContent="✓ Configured"; status.style.color="var(--green)"; }
    if(detail) detail.style.display="none";
  }
}

function markBlocklistChecked(){
  api("/api/checklist/update","POST",{key:"blocklist",value:true});
  const ico = document.getElementById("di-bl-ico");
  const msg = document.getElementById("di-bl-msg");
  if(ico){ ico.textContent="✓"; ico.className="cl-ico ok"; }
  if(msg) msg.style.display = "";
}

async function addGptRecord(){
  const inp = document.getElementById("gpt-txt");
  const val = inp?.value.trim();
  if(!val){ toast("Paste the Google TXT record first","err"); return; }
  const btn = document.querySelector('button[onclick="addGptRecord()"]');
  if(btn){ btn.disabled=true; btn.textContent="Adding…"; }
  const r = await api("/api/dns/postmaster","POST",{txt: val});
  if(btn){ btn.disabled=false; btn.textContent="Add to CF DNS"; }
  const box = document.getElementById("gpt-result");
  if(box){
    box.style.display="";
    box.innerHTML = r?.ok
      ? `<div class="alert-box alert-ok">✓ ${esc(r.msg||"Added")} — now click Verify in Postmaster Tools</div>`
      : `<div class="alert-box alert-err">${esc(r?.error||"Failed")}</div>`;
  }
  if(r?.ok){
    toast("TXT record added to Cloudflare","ok");
    // Update UI to persistent done state
    const ico  = document.getElementById("di-gpt-ico");
    const form = document.getElementById("gpt-form");
    const done = document.getElementById("gpt-done-msg");
    if(ico){ ico.textContent="✓"; ico.className="cl-ico ok"; }
    if(form) form.style.display = "none";
    if(done) done.style.display = "";
  } else {
    toast(r?.error||"Failed","err");
  }
}

async function refreshIP(){
  const btn = document.querySelector('button[onclick="refreshIP()"]');
  if(btn){ btn.disabled=true; btn.textContent="…"; }
  const r = await api("/api/dns/refresh_ip","POST");
  if(btn){ btn.disabled=false; btn.textContent="↻"; }
  if(r?.ok){
    document.getElementById("domain-ip").value = r.ip;
    toast("IP detected: " + r.ip, "ok");
  } else {
    // Fall back: try reading from status
    const s = await api("/api/status");
    if(s?.public_ip){
      document.getElementById("domain-ip").value = s.public_ip;
      toast("IP loaded from config: " + s.public_ip, "ok");
    } else {
      toast(r?.error || "Could not detect IP", "err");
    }
  }
}

async function refreshZones(){
  const sel = document.getElementById("zone-select");
  sel.innerHTML = "<option value=''>Loading…</option>";
  const r = await api("/api/domains");
  if(!r?.ok || !r.domains?.length){
    sel.innerHTML = "<option value=''>No zones found — check Cloudflare token in PHP-MNGR</option>";
    toast(r?.error || "No zones found", "err");
    return;
  }
  zones = r.domains;
  sel.innerHTML = zones.map(z =>
    `<option value="${esc(z.name)}" data-zid="${esc(z.zone_id)}">${esc(z.name)}</option>`
  ).join("");
  sel.selectedIndex = 0;
  onZoneSelect();
}

function onZoneSelect(){
  const sel = document.getElementById("zone-select");
  const opt = sel.options[sel.selectedIndex];
  if(!opt || !opt.value){
    selZone = null;
    document.getElementById("btn-setup-all").disabled = true;
    document.getElementById("zone-preview").style.display = "none";
    return;
  }
  const name = opt.value;
  const zid  = opt.dataset.zid || zones.find(z => z.name === name)?.zone_id || "";
  selZone = {name, zone_id: zid};
  document.getElementById("zp-host").textContent = `mail.${name}`;
  document.getElementById("zp-zone").textContent = zid || "(resolving…)";
  document.getElementById("zone-preview").style.display = "";
  const ip = document.getElementById("domain-ip").value.trim();
  document.getElementById("btn-setup-all").disabled = !name || !ip;
}

async function setupAll(){
  const ip = document.getElementById("domain-ip").value.trim();
  if(!selZone){ toast("Select a zone first","err"); return; }
  if(!ip){ toast("IP address required — click ↻ to detect","err"); return; }

  const btn = document.getElementById("btn-setup-all");
  btn.disabled = true; btn.textContent = "⚡ Provisioning…";

  toast("Provisioning DNS records…");
  const r = await api("/api/dns/provision","POST",{
    domain: selZone.name, zone_id: selZone.zone_id, public_ip: ip
  });

  const box = document.getElementById("provision-result");
  box.style.display = "";
  if(r?.ok){
    const lines = Object.entries(r.created||{}).map(([k,v]) =>
      `<span class="ok">✓ ${k}</span>: ${esc(v)}`
    );
    const errs = Object.entries(r.errors||{}).map(([k,v]) =>
      `<span style="color:var(--red)">✗ ${k}</span>: ${esc(v)}`
    );
    box.innerHTML = `<div class="alert-box ${errs.length?"alert-warn":"alert-ok"}">
      ${[...lines,...errs].join("<br>")}
      ${errs.length ? "<br><small>Some records failed — check zone permissions</small>" : ""}
    </div>`;
    toast("DNS records provisioned","ok");

    // Hide form, show banner
    const [cfgR, statusR] = await Promise.all([api("/api/config"), api("/api/status")]);
    updateDomainBanner(cfgR?.config||{}, statusR||{});
    updateDelivChecklist(cfgR?.config||{}, statusR||{});
    poll();
    verifyDNS();
  } else {
    box.innerHTML = `<div class="alert-box alert-err">${esc(r?.error||"Provisioning failed")}</div>`;
    toast(r?.error||"Failed","err");
  }
  btn.disabled = false; btn.textContent = "⚡ Provision DNS + Schedule DKIM";
}

async function refreshDelivChecks(){
  const [cfgR, statusR] = await Promise.all([api("/api/config"), api("/api/status")]);
  updateDelivChecklist(cfgR?.config||{}, statusR||{});
}

async function triggerDKIM(){
  if(!st.running){ toast("Start the mail server first","err"); return; }
  const btn = document.querySelector('button[onclick="triggerDKIM()"]');
  const box = document.getElementById("dkim-result");
  box.style.display = "";

  btn.disabled = true;
  const r = await api("/api/dns/dkim","POST");
  if(!r?.ok){
    box.innerHTML = `<div class="alert-box alert-err">${esc(r?.error||"Failed to start")}</div>`;
    btn.disabled = false;
    return;
  }

  // Poll for completion — runs in background, can take 20-60 s
  box.innerHTML = `<div class="alert-box alert-warn">
    ⟳ Generating DKIM key… this takes 20–60 seconds
    <div id="dkim-step" style="font-size:10px;margin-top:4px;color:var(--muted)"></div>
  </div>`;
  btn.textContent = "⟳ Generating…";

  let attempts = 0;
  const timer = setInterval(async () => {
    attempts++;
    const s   = await api("/api/dns/dkim/status");
    const job = s?.status || {};
    const el  = document.getElementById("dkim-step");
    if(el) el.textContent = job.msg || "";

    if(job.state === "done"){
      clearInterval(timer);
      btn.disabled = false; btn.textContent = "⚙ Generate & Upload DKIM";
      box.innerHTML = `<div class="alert-box alert-ok">
        ✓ DKIM record uploaded to Cloudflare DNS<br>
        <small style="color:var(--muted)">${esc(job.record||"")}</small>
      </div>`;
      toast("DKIM key uploaded","ok");
      const pill = document.getElementById("dkim-status-pill");
      if(pill){ pill.textContent="✓ uploaded to DNS"; pill.style.color="var(--green)"; }
      const di = document.getElementById("di-dkim-ico");
      if(di){ di.textContent="✓"; di.style.color="var(--green)"; }
      poll();
    } else if(job.state === "error"){
      clearInterval(timer);
      btn.disabled = false; btn.textContent = "⚙ Generate & Upload DKIM";
      box.innerHTML = `<div class="alert-box alert-err">
        <b>DKIM failed:</b><br>
        <pre style="white-space:pre-wrap;font-size:10px;margin-top:6px">${esc(job.msg||"Unknown error")}</pre>
      </div>`;
      toast("DKIM failed — see details","err");
    } else if(attempts >= 60){
      clearInterval(timer);
      btn.disabled = false; btn.textContent = "⚙ Generate & Upload DKIM";
      box.innerHTML = `<div class="alert-box alert-err">Timed out. Check Dashboard → Run Diagnostics.</div>`;
    }
  }, 2000);
}

async function verifyDNS(){
  const btn   = document.querySelector('button[onclick="verifyDNS()"]');
  const tbody = document.getElementById("dns-tbody");
  if(btn){ btn.disabled=true; btn.textContent="Checking…"; }
  tbody.innerHTML = `<tr><td colspan="4" style="color:var(--muted);padding:14px;text-align:center">
    ⟳ Checking with Cloudflare DNS…</td></tr>`;

  const r = await api("/api/dns/verify");
  if(btn){ btn.disabled=false; btn.textContent="↻ Check"; }

  if(!r?.ok){
    tbody.innerHTML = `<tr><td colspan="4" style="color:var(--red);padding:12px;text-align:center">
      ${esc(r?.error||"Verify failed — provision DNS first")}</td></tr>`;
    toast(r?.error||"Verify failed","err"); return;
  }
  if(!r.records||!Object.keys(r.records).length){
    tbody.innerHTML = `<tr><td colspan="4" style="color:var(--muted);padding:12px;text-align:center">
      No records returned</td></tr>`; return;
  }
  const domain = st.domain||"(not set)";
  const nameMap = {
    MX:`phcast.com`, A:`mail.phcast.com`,
    SPF:`phcast.com`, DMARC:`_dmarc.phcast.com`, DKIM:`mail._domainkey.phcast.com`
  };
  // Use real domain from status
  function dnsName(label){
    const d = st.domain||domain;
    const m = {MX:d, A:`mail.${d}`, SPF:d, DMARC:`_dmarc.${d}`, DKIM:`mail._domainkey.${d}`};
    return esc(m[label]||label);
  }
  tbody.innerHTML = Object.entries(r.records).map(([label,info])=>`
    <tr>
      <td><b>${esc(label)}</b></td>
      <td style="color:var(--dim);font-size:10.5px">${dnsName(label)}</td>
      <td style="font-size:10.5px;color:var(--muted);max-width:300px;overflow:hidden;text-overflow:ellipsis">
        ${esc((info.value||info.error||"").slice(0,80))}</td>
      <td class="${info.ok?"dns-ok":"dns-miss"}">${info.ok?"✓":"✗"}</td>
    </tr>`).join("");
}

async function checkPorts(){
  const btn = document.querySelector('button[onclick="checkPorts()"]');
  if(btn){ btn.disabled=true; btn.textContent="Checking…"; }
  const r = await api("/api/ports/check");
  if(btn){ btn.disabled=false; btn.textContent="↻ Check Ports"; }
  if(!r?.ports) return;

  const ports = r.ports;
  const ufwActive = r.ufw_active;
  let allGreen = true;

  for(const [p, state] of Object.entries(ports)){
    const el = document.getElementById(`port-${p}`);
    if(!el) continue;

    if(state === "open"){
      // Service listening and reachable
      el.className = "port-chip open";
      el.textContent = `✓ ${p}`;
    } else if(state === "no_firewall"){
      // No firewall — port is not blocked, service just not running yet
      el.className = "port-chip open";
      el.textContent = `✓ ${p}`;
      el.title = "No firewall — port is open";
    } else if(state === "allowed"){
      // UFW active but rule allows it
      el.className = "port-chip open";
      el.textContent = `✓ ${p}`;
    } else {
      // Blocked by UFW
      el.className = "port-chip closed";
      el.textContent = `✗ ${p}`;
      allGreen = false;
    }
  }

  const ico = document.getElementById("di-ufw-ico");
  if(ico){
    ico.textContent = allGreen ? "✓" : "!";
    ico.className   = "cl-ico" + (allGreen ? " ok" : "");
  }

  // Update UFW command hint based on active state
  const ufwNote = document.getElementById("di-ufw-note");
  if(ufwNote){
    if(!ufwActive){
      ufwNote.innerHTML = `<span style="color:var(--green)">✓ No firewall active — ports are not blocked</span>`;
    } else if(allGreen){
      ufwNote.innerHTML = `<span style="color:var(--green)">✓ UFW active — required ports are allowed</span>`;
    } else {
      ufwNote.innerHTML = `UFW is active. Run: <code style="color:var(--green)">sudo ufw allow 25,587,143,993/tcp &amp;&amp; sudo ufw reload</code>`;
    }
  }

  toast(allGreen ? "All ports accessible ✓" : "Some ports may be blocked", allGreen ? "ok" : "");
}

// ── mode switch ───────────────────────────────────────────────────────────
async function saveMode(){
  const mode = document.querySelector('input[name=smode]:checked')?.value||"dev";
  const wasRunning = st.running;
  if(wasRunning){ toast("Stopping current server…"); await api("/api/stop","POST"); }
  const r = await api("/api/config","POST",{mode});
  if(r?.ok){
    toast("Mode saved","ok"); await poll();
    if(wasRunning){
      await new Promise(res=>setTimeout(res,1200));
      await api("/api/start","POST"); poll();
    }
  } else toast(r?.error||"Failed","err");
}

// ── inbox ─────────────────────────────────────────────────────────────────
async function loadInbox(p){
  inboxPage = p??inboxPage;
  const isDev = st.mode==="dev";
  document.getElementById("folder-sel").style.display = isDev?"none":"";
  document.getElementById("acc-bar").style.display    = isDev?"none":"";
  let url = `/api/inbox?page=${inboxPage}&limit=${inboxLimit}&q=${encodeURIComponent(document.getElementById("sq").value||"")}&folder=${curFolder}`;
  if(!isDev&&curAcc) url+=`&account=${encodeURIComponent(curAcc)}`;
  const r = await api(url);
  if(!r) return;
  inboxTotal = r.total||0;
  renderList(r.messages||[], r.error);
  document.getElementById("pgn-info").textContent =
    inboxTotal + " messages" + (r.unread ? " · " + r.unread + " unread" : "");
  document.getElementById("pgn-p").disabled = inboxPage<=1;
  document.getElementById("pgn-n").disabled = inboxPage*inboxLimit>=inboxTotal;
  // Badge shows UNREAD count only — 0 means hidden
  const nbInbox = document.getElementById("nb-inbox");
  if(nbInbox){
    const unread = r.unread ?? (r.messages||[]).filter(m=>!m.read).length;
    nbInbox.textContent = unread > 0 ? unread : "";
    nbInbox.className   = "nb" + (unread > 0 ? " unread" : "");
    nbInbox.style.display = unread > 0 ? "" : "none";
  }
}
let msgCache = [];  // module-level store — avoids JSON-in-onclick encoding bugs

function renderList(msgs, err){
  const box = document.getElementById("msg-list");
  if(err&&!msgs.length){
    box.innerHTML=`<div class="empty-state"><div class="ico">⚠</div><div>${esc(err)}</div></div>`;
    return;
  }
  if(!msgs.length){
    box.innerHTML=`<div class="empty-state"><div class="ico">✉</div><div>No messages</div></div>`;
    return;
  }
  msgCache = msgs;
  box.innerHTML = msgs.map((m, i)=>{
    const isDraft = /draft/i.test(curFolder);
    const clickFn = isDraft ? `openDraftInCompose(${i})` : `openMsg(${i})`;
    return `
    <div class="msg-item ${m.read?"":"unread"}" data-idx="${i}" onclick="${clickFn}">
      <div class="mi-row">
        <span class="mi-from">${esc(m.from)}</span>
        <span class="mi-date">${fmtDate(m.date)}</span>
      </div>
      <div class="mi-subj">${esc(m.subject)}</div>
    </div>`;
  }).join("");
}
async function openMsg(idx){
  const meta = msgCache[idx];
  if(!meta){ toast("Message not found","err"); return; }
  // Mark selected visually
  document.querySelectorAll(".msg-item").forEach(el=>el.classList.remove("selected"));
  const el = document.querySelector(`.msg-item[data-idx="${idx}"]`);
  if(el){ el.classList.add("selected"); el.classList.remove("unread"); }
  let url = `/api/message?id=${encodeURIComponent(meta.id)}`;
  if(st.mode!=="dev") url+=`&account=${encodeURIComponent(curAcc)}&folder=${curFolder}`;
  const r = await api(url);
  if(!r?.ok){ toast(r?.error||"Failed","err"); return; }
  curMsg=r; htmlMode=false;
  document.getElementById("r-subj").textContent = r.subject;
  document.getElementById("r-from").textContent = r.from;
  document.getElementById("r-to").textContent   = r.to;
  document.getElementById("r-date").textContent = r.date;
  document.getElementById("r-text").textContent = r.text||"(no text body)";
  document.getElementById("r-html").style.display="none";
  document.getElementById("r-text").style.display="";
  document.getElementById("html-tog").style.display=r.html?"":"none";
  const att=document.getElementById("r-att");
  if(r.attachments?.length){
    att.style.display="";
    att.innerHTML=r.attachments.map(fn=>`<span style="font-size:11px;padding:3px 8px;background:var(--border);border-radius:4px">📎 ${esc(fn)}</span>`).join("");
  } else att.style.display="none";
  document.getElementById("reader-ph").style.display="none";
  document.getElementById("reader").style.display="flex";
}
function toggleHtml(){
  htmlMode=!htmlMode;
  document.getElementById("r-text").style.display = htmlMode?"none":"";
  document.getElementById("r-html").style.display = htmlMode?"":"none";
  document.getElementById("html-tog").textContent = htmlMode?"⬜ Text":"⬜ HTML";
  if(htmlMode&&curMsg?.html) document.getElementById("r-frame").srcdoc=curMsg.html;
}
async function deleteOpen(){
  if(!curMsg) return;
  const inTrash = /trash|deleted/i.test(curFolder);
  const label   = inTrash ? "Permanently delete this message?" : "Move to Trash?";
  if(inTrash && !confirm(label)) return;
  const r = await api("/api/message/delete","POST",{ids:[curMsg.id],account:curAcc,folder:curFolder});
  if(r?.ok){
    toast(inTrash ? "Permanently deleted" : "Moved to Trash","ok");
    document.getElementById("reader").style.display="none";
    document.getElementById("reader-ph").style.display="";
    curMsg=null; loadInbox();
  } else toast(r?.error||"Failed","err");
}
function replyTo(){
  if(!curMsg) return;
  tab(document.querySelector("[data-tab=compose]"),"compose");
  document.getElementById("c-to").value  = curMsg.from;
  document.getElementById("c-sub").value = "Re: "+curMsg.subject;
  document.getElementById("c-body").value= "\n\n---\n"+(curMsg.text||"");
}
function changePage(d){
  const np=inboxPage+d;
  if(np<1||np*inboxLimit-inboxLimit>=inboxTotal) return;
  loadInbox(np);
}
async function loadFolders(){
  if(st.mode==="dev"||!curAcc) return;
  const r=await api(`/api/folders?account=${encodeURIComponent(curAcc)}`);
  if(!r?.folders) return;
  const sel=document.getElementById("folder-sel");
  sel.innerHTML=r.folders.map(f=>`<option ${f===curFolder?"selected":""}>${esc(f)}</option>`).join("");
  // Note: onchange is handled by onFolderChange() in the HTML — no override needed
}
function onFolderChange(){
  const sel = document.getElementById("folder-sel");
  curFolder = sel.value;
  // Show Empty Trash button only when Trash folder is selected
  const trashBtn = document.getElementById("empty-trash-btn");
  if(trashBtn){
    const isTrash = /trash|deleted/i.test(curFolder);
    trashBtn.style.display = isTrash ? "" : "none";
  }
  loadInbox(1);
}

async function refreshInbox(){
  const btn = document.getElementById("inbox-refresh-btn");
  if(btn){ btn.disabled=true; btn.textContent="⟳"; }
  await loadInbox();
  if(btn){ btn.disabled=false; btn.textContent="↻"; }
}

async function emptyTrash(){
  if(!confirm("Permanently delete all messages in Trash? This cannot be undone.")) return;
  const r = await api("/api/message/empty_trash","POST",{account:curAcc});
  if(r?.ok){
    toast(`Trash emptied (${r.deleted||0} messages deleted)`,"ok");
    loadInbox(1);
  } else {
    toast(r?.error||"Failed to empty trash","err");
  }
}

async function saveDraft(){
  const fromSel = document.getElementById("c-from-sel");
  const from    = fromSel?.value === "__manual__" ? "" : (fromSel?.value || curAcc || "");
  const toInp   = document.getElementById("c-to-inp").value.trim();
  if(toInp) addToChip(toInp);
  const to      = toRecipients.join(", ");
  const sub     = document.getElementById("c-sub").value.trim();
  const body    = document.getElementById("c-editor")?.innerText.trim() || "";

  if(!from){ toast("Select a From account first","err"); return; }

  const r = await api("/api/message/save_draft","POST",{
    account: from,
    draft: { from, to, subject: sub||"(no subject)", body }
  });
  if(r?.ok){
    toast("Draft saved","ok");
    document.getElementById("c-status").textContent = "✓ Draft saved";
    document.getElementById("c-status").style.color = "var(--green)";
    setTimeout(()=>{
      document.getElementById("c-status").textContent="Ready";
      document.getElementById("c-status").style.color="";
    }, 3000);
    resetCompose();
    // Switch to inbox Drafts folder so user can see it
    if(st.mode !== "dev"){
      tab(document.querySelector("[data-tab=inbox]"),"inbox");
      setTimeout(()=>{ curFolder="Drafts"; loadFolders(); loadInbox(1); }, 400);
    }
  } else {
    toast(r?.error||"Failed to save draft","err");
  }
}

function discardCompose(){
  const hasContent = toRecipients.length ||
    document.getElementById("c-sub")?.value.trim() ||
    document.getElementById("c-editor")?.innerText.trim();
  if(hasContent && !confirm("Discard this message?")) return;
  resetCompose();
}

// Open a draft from the Drafts folder into compose
async function openDraftInCompose(msgIdx){
  const m = msgCache[msgIdx];
  if(!m) return;
  // Fetch full message to get body
  const r = await api(`/api/message?id=${encodeURIComponent(m.id)}&account=${encodeURIComponent(curAcc)}&folder=${encodeURIComponent(curFolder)}`);
  if(!r?.ok) return;
  // Switch to compose tab
  tab(document.querySelector("[data-tab=compose]"),"compose");
  // Small delay to let compose init
  setTimeout(()=>{
    // Fill From
    const fromSel = document.getElementById("c-from-sel");
    if(fromSel && curAcc){
      for(let i=0;i<fromSel.options.length;i++){
        if(fromSel.options[i].value===curAcc){ fromSel.selectedIndex=i; break; }
      }
    }
    // Fill To
    toRecipients = [];
    const toAddr = m.to || r.to || "";
    toAddr.split(",").map(a=>a.trim()).filter(Boolean).forEach(addToChip);
    // Fill Subject
    const subEl = document.getElementById("c-sub");
    if(subEl) subEl.value = (m.subject||"").replace(/^\(no subject\)$/, "");
    // Fill Body
    const editor = document.getElementById("c-editor");
    if(editor) editor.innerText = r.text || r.body || "";
    // Delete the draft from Drafts folder
    api("/api/message/delete","POST",{ids:[m.id],account:curAcc,folder:curFolder});
    toast("Draft loaded","ok");
  }, 300);
}


function onAccChange(){
  curAcc=document.getElementById("acc-sel").value;
  curFolder="INBOX"; loadInbox(1); loadFolders();
}

// ── compose ───────────────────────────────────────────────────────────────
let toRecipients = [];
let autosaveTimer = null;

function initCompose(){
  const sel = document.getElementById("c-from-sel");
  if(!sel) return;
  // Store accounts in a module-level map so signatures aren't HTML-escaped in data attrs
  api("/api/accounts").then(r=>{
    const accs = r?.accounts||[];
    const domain = st.domain || "";
    if(domain && !accs.length) accs.push({email:`noreply@${domain}`,display_name:"",signature:""});
    // Store raw signatures in a map keyed by email
    window._accSigs  = {};
    window._accNames = {};
    accs.forEach(a=>{
      window._accSigs[a.email]  = a.signature    || "";
      window._accNames[a.email] = a.display_name || "";
    });
    sel.innerHTML = accs.map(a=>
      `<option value="${esc(a.email)}">${esc(a.display_name||a.email)}</option>`
    ).join("") + `<option value="__manual__">Other…</option>`;
    applyAccountSignature(sel);
    sel.onchange = () => applyAccountSignature(sel);
  });
  const mb = document.getElementById("c-mode-label");
  if(mb){
    mb.textContent = st.mode==="dev"?"DEV":"LIVE";
    mb.className   = "mode-badge "+(st.mode==="dev"?"mode-dev":"mode-live");
  }
}

function applyAccountSignature(sel){
  const opt = sel.options[sel.selectedIndex];
  if(!opt || opt.value === "__manual__") return;
  const sig = (window._accSigs||{})[opt.value] || "";
  if(!sig) return;
  const editor = document.getElementById("c-editor");
  if(!editor) return;
  // Always set signature — place cursor before it with a separator
  // Only skip if editor already has real typed content (not just the sig)
  const currentText = editor.innerText.replace(/^\s+|\s+$/g,"");
  const hasSig = editor.innerHTML.includes("c-sig");
  if(!currentText || hasSig){
    editor.innerHTML = `<p><br></p><p>--</p><div class="c-sig">${sig}</div>`;
    // Place cursor at top
    const range = document.createRange();
    const sel2  = window.getSelection();
    const firstP = editor.querySelector("p");
    if(firstP){
      range.setStart(firstP, 0);
      range.collapse(true);
      sel2.removeAllRanges();
      sel2.addRange(range);
    }
  }
}

// ── attachments ───────────────────────────────────────────────────────────
let composeAttachments = [];  // [{name, size, data (base64)}]

function handleAttach(e){
  const files = Array.from(e.target.files);
  files.forEach(file=>{
    const reader = new FileReader();
    reader.onload = ev=>{
      composeAttachments.push({name: file.name, size: file.size, data: ev.target.result});
      renderAttachChips();
    };
    reader.readAsDataURL(file);
  });
  e.target.value = "";  // reset so same file can be re-added
}

function renderAttachChips(){
  const row   = document.getElementById("c-attachments");
  const chips = document.getElementById("c-att-chips");
  if(!row || !chips) return;
  if(!composeAttachments.length){ row.style.display="none"; return; }
  row.style.display = "flex";
  chips.innerHTML = composeAttachments.map((a,i)=>`
    <span class="att-chip">
      <span class="att-chip-name">${esc(a.name)}</span>
      <span class="att-chip-size">${fmtSize(a.size)}</span>
      <span class="att-chip-x" onclick="removeAttach(${i})">✕</span>
    </span>`).join("");
}

function removeAttach(i){
  composeAttachments.splice(i, 1);
  renderAttachChips();
}

function fmtSize(bytes){
  if(bytes < 1024) return bytes+"B";
  if(bytes < 1048576) return (bytes/1024).toFixed(1)+"KB";
  return (bytes/1048576).toFixed(1)+"MB";
}

function handleToKey(e){
  if(["Enter","Tab",","," "].includes(e.key)){
    e.preventDefault();
    const val = document.getElementById("c-to-inp").value.trim().replace(/,$/, "");
    addToChip(val);
  }
  if(e.key==="Backspace" && !document.getElementById("c-to-inp").value && toRecipients.length){
    removeToChip(toRecipients[toRecipients.length-1]);
  }
}

function addToChip(addr){
  if(!addr || !addr.includes("@")) return;
  if(toRecipients.includes(addr)) return;
  toRecipients.push(addr);
  renderToChips();
  document.getElementById("c-to-inp").value = "";
}

function removeToChip(addr){
  toRecipients = toRecipients.filter(a=>a!==addr);
  renderToChips();
}

function renderToChips(){
  const box = document.getElementById("c-to-chips");
  const inp = document.getElementById("c-to-inp");
  // Remove existing chips but keep the input
  box.querySelectorAll(".cf-chip").forEach(el=>el.remove());
  toRecipients.forEach(addr=>{
    const chip = document.createElement("span");
    chip.className = "cf-chip";
    chip.innerHTML = `${esc(addr)}<span class="cf-chip-x" onclick="removeToChip('${esc(addr)}')">✕</span>`;
    box.insertBefore(chip, inp);
  });
}

function toggleRow(id, label){
  const row = document.getElementById(id);
  const visible = row.style.display !== "none";
  row.style.display = visible ? "none" : "";
  const pills = {"cc-row":"pill-cc","bcc-row":"pill-bcc"};
  document.getElementById(pills[id])?.classList.toggle("active", !visible);
}

function fmt(cmd, val=null){
  document.getElementById("c-editor").focus();
  document.execCommand(cmd, false, val);
}

function insertLink(){
  const url = prompt("Enter URL:", "https://");
  if(url) fmt("createLink", url);
}

function handlePaste(e){
  e.preventDefault();
  const text = e.clipboardData.getData("text/plain");
  document.execCommand("insertText", false, text);
}

function updateCharCount(){
  const editor = document.getElementById("c-editor");
  const count  = editor.innerText.length;
  document.getElementById("c-char-count").textContent = count > 0 ? `${count} chars` : "";
  // Autosave draft indicator
  clearTimeout(autosaveTimer);
  document.getElementById("c-autosave").textContent = "Saving draft…";
  autosaveTimer = setTimeout(()=>{
    document.getElementById("c-autosave").textContent = "Draft saved";
    setTimeout(()=>document.getElementById("c-autosave").textContent="",2000);
  }, 1500);
}

async function sendEmail(){
  // Flush any pending chip from the input
  const toInp = document.getElementById("c-to-inp").value.trim();
  if(toInp) addToChip(toInp);

  const fromSel  = document.getElementById("c-from-sel");
  const fromOpt  = fromSel?.options[fromSel.selectedIndex];
  let fromAddr   = fromSel?.value==="__manual__" ? "" : (fromSel?.value||"");
  const fromName = fromOpt?.dataset?.name || "";
  // Build RFC 2822 From: "Display Name <email>"
  if(fromAddr && fromName) fromAddr = `${fromName} <${fromAddr}>`;

  const to      = toRecipients.join(", ");
  const sub     = document.getElementById("c-sub").value.trim();
  const editor  = document.getElementById("c-editor");
  const html_body = editor.innerHTML.trim();
  const plain   = editor.innerText.trim();
  const cc      = document.getElementById("c-cc")?.value.trim()  || "";
  const bcc     = document.getElementById("c-bcc")?.value.trim() || "";

  if(!to){   toast("Add at least one recipient","err"); return; }
  if(!sub){  toast("Subject is required","err"); return; }
  if(!plain){ toast("Message body is empty","err"); return; }

  const btn = document.getElementById("c-send-btn");
  btn.disabled = true;
  document.getElementById("c-status").textContent = "Sending…";

  const r = await api("/api/compose","POST",{
    to, subject: sub, body: plain, html_body,
    from_addr: fromAddr, cc, bcc,
    attachments: composeAttachments.map(a=>({name:a.name, data:a.data}))
  });

  btn.disabled = false;
  if(r?.ok){
    document.getElementById("c-status").textContent = `✓ Sent to ${to}`;
    document.getElementById("c-status").style.color = "var(--green)";
    toast("Message sent!","ok");
    // Clear all fields immediately after send
    resetCompose();
    // Re-apply signature for the current account
    const fromSel = document.getElementById("c-from-sel");
    if(fromSel) setTimeout(()=>applyAccountSignature(fromSel), 50);
    if(st.mode==="dev") tab(document.querySelector("[data-tab=inbox]"),"inbox");
    setTimeout(()=>{
      document.getElementById("c-status").textContent="Ready";
      document.getElementById("c-status").style.color="";
    },4000);
  } else {
    document.getElementById("c-status").textContent = `✗ ${r?.error||"Send failed"}`;
    document.getElementById("c-status").style.color = "var(--red)";
    toast(r?.error||"Send failed","err");
    setTimeout(()=>{
      document.getElementById("c-status").textContent="Ready";
      document.getElementById("c-status").style.color="";
    },6000);
  }
}

function resetCompose(){
  toRecipients = [];
  renderToChips();
  const editor = document.getElementById("c-editor");
  if(editor) editor.innerHTML = "";
  ["c-sub","c-cc","c-bcc"].forEach(id=>{
    const el = document.getElementById(id); if(el) el.value="";
  });
  document.getElementById("c-status").textContent = "Ready";
  document.getElementById("c-status").style.color = "";
  document.getElementById("c-char-count").textContent = "";
  ["cc-row","bcc-row"].forEach(id=>{
    document.getElementById(id).style.display="none";
  });
  document.getElementById("pill-cc")?.classList.remove("active");
  // Clear attachments
  composeAttachments = [];
  renderAttachChips();
  document.getElementById("pill-bcc")?.classList.remove("active");
}

// ── accounts ──────────────────────────────────────────────────────────────
// Core: fetch accounts + populate the inbox acc-sel. Returns the list.
async function fetchAccounts(){
  if(st.mode==="dev") return [];
  const r    = await api("/api/accounts");
  const accs = r?.accounts||[];
  // Keep sig/name maps up to date whenever accounts are fetched
  window._accSigs  = window._accSigs  || {};
  window._accNames = window._accNames || {};
  accs.forEach(a=>{
    window._accSigs[a.email]  = a.signature    || "";
    window._accNames[a.email] = a.display_name || "";
  });
  const sel  = document.getElementById("acc-sel");
  if(sel){
    sel.innerHTML = accs.map(a=>
      `<option ${a.email===curAcc?"selected":""}>${esc(a.email)}</option>`
    ).join("");
    if(!curAcc && accs.length){ curAcc=accs[0].email; sel.value=curAcc; }
  }
  document.getElementById("nb-acc").textContent = accs.length;
  return accs;
}

// Full panel render: updates the accounts table + toggles panel visibility
async function loadAccounts(){
  const isDev = st.mode==="dev";
  document.getElementById("acc-live").style.display = isDev?"none":"";
  document.getElementById("acc-dev").style.display  = isDev?"":"none";
  if(isDev) return;
  const accs  = await fetchAccounts();
  const tbody = document.getElementById("acc-tbody");
  if(!accs.length){
    tbody.innerHTML=`<tr><td colspan="3" style="color:var(--muted);text-align:center;padding:16px">No accounts</td></tr>`;
    return;
  }
  tbody.innerHTML=accs.map(a=>`<tr>
    <td><span class="pill">✉</span> ${esc(a.email)}${a.display_name?` <span style="color:var(--muted);font-size:10px">(${esc(a.display_name)})</span>`:""}</td>
    <td>${esc(a.created||"—")}</td>
    <td style="display:flex;gap:4px">
      <button class="btn btn-ghost btn-sm" onclick="openAccEdit('${esc(a.email)}')">✎ Edit</button>
      <button class="btn btn-ghost btn-sm" onclick="delAcc('${esc(a.email)}')">🗑</button>
    </td>
  </tr>`).join("");
}
function openAccEdit(email){
  // Read current values from the _accSigs/_accNames maps (populated by fetchAccounts)
  // Fall back to fetching fresh if maps aren't populated yet
  const doOpen = (name, sig) => {
    document.getElementById("edit-email").value = email;
    document.getElementById("edit-name").value  = name  || "";
    document.getElementById("edit-sig").value   = sig   || "";
    document.getElementById("edit-pw").value    = "";
    document.getElementById("acc-edit-modal").style.display = "";
    document.getElementById("acc-edit-modal").scrollIntoView({behavior:"smooth"});
  };

  const name = (window._accNames||{})[email] || "";
  const sig  = (window._accSigs||{})[email]  || "";
  // If maps are populated use them directly; otherwise fetch fresh
  if(window._accSigs && email in window._accSigs){
    doOpen(name, sig);
  } else {
    api("/api/accounts").then(r=>{
      const acc = (r?.accounts||[]).find(a=>a.email===email) || {};
      if(!window._accSigs)  window._accSigs  = {};
      if(!window._accNames) window._accNames = {};
      window._accSigs[email]  = acc.signature    || "";
      window._accNames[email] = acc.display_name || "";
      doOpen(acc.display_name||"", acc.signature||"");
    });
  }
}
function closeAccEdit(){
  document.getElementById("acc-edit-modal").style.display = "none";
}
async function saveAccEdit(){
  const email = document.getElementById("edit-email").value;
  const pw    = document.getElementById("edit-pw").value.trim();
  const name  = document.getElementById("edit-name").value.trim();
  const sig   = document.getElementById("edit-sig").value;
  const r = await api("/api/account/update","POST",{
    email, password: pw, display_name: name, signature: sig
  });
  if(r?.ok){
    toast("Account updated","ok");
    closeAccEdit();
    loadAccounts();
    // Refresh in-memory signature map so compose picks it up immediately
    if(!window._accSigs) window._accSigs = {};
    if(!window._accNames) window._accNames = {};
    window._accSigs[email]  = sig;
    window._accNames[email] = name;
    // Re-apply if this account is currently selected in compose
    const fromSel = document.getElementById("c-from-sel");
    if(fromSel?.value === email){
      applyAccountSignature(fromSel);
    }
  } else {
    toast(r?.error||"Update failed","err");
  }
}
async function addAcc(){
  const em=document.getElementById("ne").value.trim();
  const pw=document.getElementById("np").value.trim();
  if(!em||!pw){ toast("Email and password required","err"); return; }
  const r=await api("/api/accounts","POST",{email:em,password:pw});
  if(r?.ok){ toast("Created","ok"); document.getElementById("ne").value=""; document.getElementById("np").value=""; loadAccounts(); }
  else toast(r?.error||"Failed","err");
}
async function delAcc(email){
  if(!confirm(`Delete ${email}?`)) return;
  const r=await api("/api/account/delete","POST",{email});
  if(r?.ok){ toast("Deleted","ok"); loadAccounts(); } else toast(r?.error,"err");
}

// ── spam ──────────────────────────────────────────────────────────────────
async function loadSpam(){
  const r=await api("/api/spam"); if(!r?.spam) return;
  const s=r.spam;
  document.getElementById("sp-en").checked  =!!s.enabled;
  document.getElementById("sp-sa").checked  =s.engines?.includes("spamassassin");
  document.getElementById("sp-rs").checked  =s.engines?.includes("rspamd");
  document.getElementById("sp-dk").checked  =!!s.dkim_check;
  document.getElementById("sp-spf").checked =!!s.spf_check;
  document.getElementById("sp-bb").checked  =!!s.block_known_bad;
  document.getElementById("sp-q").checked   =!!s.quarantine;
  document.getElementById("sl-tag").value   =s.tag_score??2;
  document.getElementById("sl-spam").value  =s.spam_score??5;
  document.getElementById("sl-kill").value  =s.reject_score??15;
  document.getElementById("sv-tag").textContent  =s.tag_score??2;
  document.getElementById("sv-spam").textContent =s.spam_score??5;
  document.getElementById("sv-kill").textContent =s.reject_score??15;
  document.getElementById("sp-stag").value  =s.subject_tag??"[SPAM]";
  document.getElementById("sp-custom").value=s.custom_rules??"";
  wl=s.whitelist??[]; bl=s.blacklist??[];
  renderTags("wl"); renderTags("bl");
}
function addTag(list){
  const inp=document.getElementById(list+"-inp");
  const val=inp.value.trim(); if(!val) return;
  if(list==="wl"){ if(!wl.includes(val)) wl.push(val); }
  else           { if(!bl.includes(val)) bl.push(val); }
  inp.value=""; renderTags(list);
}
function removeTag(list,val){
  if(list==="wl") wl=wl.filter(v=>v!==val); else bl=bl.filter(v=>v!==val);
  renderTags(list);
}
function renderTags(list){
  const arr=list==="wl"?wl:bl;
  // Render only the tag chips — the static <input id="wl-inp/bl-inp"> below
  // the list handles user input. Injecting a second input here caused duplicate IDs.
  document.getElementById(list+"-list").innerHTML=
    arr.map(v=>`<span class="tag">${esc(v)}<span class="tag-del" onclick="removeTag('${list}','${esc(v)}')">✕</span></span>`).join("");
}
async function saveSpam(){
  const engines=[];
  if(document.getElementById("sp-sa").checked) engines.push("spamassassin");
  if(document.getElementById("sp-rs").checked) engines.push("rspamd");
  const body={
    enabled: document.getElementById("sp-en").checked,
    engines, dkim_check: document.getElementById("sp-dk").checked,
    spf_check: document.getElementById("sp-spf").checked,
    block_known_bad: document.getElementById("sp-bb").checked,
    quarantine: document.getElementById("sp-q").checked,
    tag_score:    parseFloat(document.getElementById("sl-tag").value),
    spam_score:   parseFloat(document.getElementById("sl-spam").value),
    reject_score: parseFloat(document.getElementById("sl-kill").value),
    subject_tag:  document.getElementById("sp-stag").value,
    custom_rules: document.getElementById("sp-custom").value,
    whitelist:wl, blacklist:bl,
  };
  const r=await api("/api/spam","POST",body);
  toast(r?.msg||(r?.ok?"Saved":"Failed"), r?.ok?"ok":"err");
}

// ── settings ──────────────────────────────────────────────────────────────
window.addEventListener("DOMContentLoaded",()=>{
  api("/api/config").then(r=>{
    if(r?.config){
      const m=r.config.mode||"dev";
      document.getElementById(m==="dev"?"m-dev":"m-live").checked=true;
    }
  });
});

// ── utilities ─────────────────────────────────────────────────────────────
async function api(path,method="GET",body=null){
  try{
    const o={method,headers:{}};
    if(body){o.body=JSON.stringify(body);o.headers["Content-Type"]="application/json";}
    return await (await fetch(path,o)).json();
  } catch{return null;}
}
function esc(s){ return String(s??"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }
function fmtDate(d){
  if(!d) return "";
  try{
    const dt=new Date(d),now=new Date();
    return dt.toDateString()===now.toDateString()
      ? dt.toLocaleTimeString([],{hour:"2-digit",minute:"2-digit"})
      : dt.toLocaleDateString([],{month:"short",day:"numeric"});
  }catch{return d;}
}
function toast(msg,type=""){
  const box=document.getElementById("toast");
  const el=document.createElement("div");
  el.className="tmsg"+(type?" "+type:"");
  el.textContent=msg; box.appendChild(el);
  requestAnimationFrame(()=>el.classList.add("show"));
  setTimeout(()=>{ el.classList.remove("show"); setTimeout(()=>el.remove(),300); },3000);
}
</script>
</body>
</html>"""

# ─────────────────────────────────────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def preflight() -> list:
    issues = []
    if sys.version_info < (3, 8):
        issues.append(f"Python 3.8+ required — found {sys.version.split()[0]}")
    try:
        r = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
        if r.returncode != 0:
            issues.append("Docker not reachable. Run: sudo usermod -aG docker $USER")
    except FileNotFoundError:
        issues.append("Docker not found: https://docs.docker.com/engine/install/")
    except subprocess.TimeoutExpired:
        issues.append("Docker check timed out — is Docker running?")
    if not phpmngr_cf_token():
        issues.append(
            f"Cloudflare token not found at {PHPMNGR_CF_FILE}. "
            "Configure it in PHP-MNGR → Settings → Cloudflare."
        )
    return issues

def main():
    print(f"""
╔══════════════════════════════════════════════════════╗
║       KillTheHost  MAIL-SRVR  v{VERSION:<23}║
╚══════════════════════════════════════════════════════╝
  PHP-MNGR creds : {PHPMNGR_DIR}
  Data dir       : {DATA_DIR}
  Python         : {sys.version.split()[0]}
""")
    for w in preflight():
        print(f"  ⚠  {w}")
    print()

    try:
        server = ThreadedHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        print(f"\n  [FATAL] Cannot bind to port {PORT}: {e}")
        sys.exit(1)

    url = f"http://localhost:{PORT}"
    print(f"  Control panel : {url}")
    print(f"  Press Ctrl+C  : stop\n")
    threading.Thread(target=lambda: (time.sleep(0.9), webbrowser.open(url)),
                     daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down…")
        for fn in (stop_mailpit, stop_mailserver):
            try: fn()
            except Exception: pass
        server.shutdown()
        print("  Done.\n")


if __name__ == "__main__":
    main()
