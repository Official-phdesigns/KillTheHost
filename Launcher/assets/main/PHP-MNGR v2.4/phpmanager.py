#!/usr/bin/env python3
"""
PHP-MNGR — Docker PHP Web Server Manager
Companion to DB-3NGIN3 | PhDesigns LLC
Requires: Python 3.8+, Docker
Run: python3 phpmanager.py
"""

import json
import os
import subprocess
import threading
import time
import webbrowser
import shutil
import base64
import mimetypes
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
from pathlib import Path

# ─── Config ──────────────────────────────────────────────────────────────────
DATA_DIR   = Path.home() / ".phpmngr"
SITES_DIR  = DATA_DIR / "sites"
CERTS_DIR  = DATA_DIR / "certs"
NGINX_DIR  = DATA_DIR / "nginx"
DATA_FILE  = DATA_DIR / "sites.json"
NC_CONFIG  = DATA_DIR / "namecheap.json"   # Namecheap credentials
PORT       = 4280
PROXY_NAME = "phpmngr-proxy"
NETWORK    = "phpmngr-net"

for d in [DATA_DIR, SITES_DIR, CERTS_DIR, NGINX_DIR]:
    d.mkdir(parents=True, exist_ok=True)

PHP_VERSIONS = ["8.3", "8.2", "8.1", "8.0", "7.4", "7.3", "7.2"]

# ─── Helpers ─────────────────────────────────────────────────────────────────
def _get_env():
    """Return environment with a full PATH so Docker is always found."""
    env = os.environ.copy()
    extra = ["/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
             os.path.expanduser("~/.docker/bin")]
    current = env.get("PATH", "")
    env["PATH"] = ":".join([p for p in extra if p not in current]) + ":" + current
    return env

def run(cmd, capture=True, input=None):
    r = subprocess.run(
        cmd, shell=True, capture_output=capture,
        text=True, input=input, env=_get_env()
    )
    return r.stdout.strip(), r.stderr.strip(), r.returncode

def _find_docker():
    """Return the docker binary path, or 'docker' as fallback."""
    for p in ["/usr/local/bin/docker", "/usr/bin/docker", "/bin/docker",
              os.path.expanduser("~/.docker/bin/docker")]:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    # Try which
    out, _, rc = run("which docker 2>/dev/null || command -v docker 2>/dev/null")
    if rc == 0 and out.strip():
        return out.strip()
    return "docker"

DOCKER_BIN = None  # resolved lazily

def docker_bin():
    global DOCKER_BIN
    if DOCKER_BIN is None:
        DOCKER_BIN = _find_docker()
    return DOCKER_BIN

def load_sites():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except:
            pass
    return {}

def save_sites(sites):
    DATA_FILE.write_text(json.dumps(sites, indent=2))

# ─── Namecheap API ────────────────────────────────────────────────────────────
NC_API  = "https://api.namecheap.com/xml.response"
NC_SBX  = "https://api.sandbox.namecheap.com/xml.response"
NC_NS   = "{https://api.namecheap.com/xml.response}"

def nc_load():
    if NC_CONFIG.exists():
        try: return json.loads(NC_CONFIG.read_text())
        except: pass
    return {}

def nc_save(cfg):
    NC_CONFIG.write_text(json.dumps(cfg, indent=2))

def nc_get_ip():
    """Get server public IP."""
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as r:
            return r.read().decode().strip()
    except:
        try:
            with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=5) as r:
                return r.read().decode().strip()
        except:
            return ""

def nc_call(cmd, extra=None, sandbox=False):
    """Call Namecheap XML API. Returns (parsed_root, error_str)."""
    cfg = nc_load()
    if not cfg.get("api_key") or not cfg.get("username"):
        return None, "Namecheap API not configured"
    ip = cfg.get("client_ip") or nc_get_ip()
    if not ip:
        return None, "Could not determine client IP"
    params = {
        "ApiUser":  cfg["username"],
        "ApiKey":   cfg["api_key"],
        "UserName": cfg["username"],
        "ClientIp": ip,
        "Command":  cmd,
    }
    if extra:
        params.update(extra)
    base = NC_SBX if cfg.get("sandbox") else NC_API
    url = base + "?" + urllib.parse.urlencode(params)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PHP-MNGR/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read().decode()
    except Exception as e:
        return None, f"Network error: {e}"
    try:
        root = ET.fromstring(raw)
    except Exception as e:
        return None, f"XML parse error: {e}"
    status = root.get("Status", "")
    if status == "ERROR":
        # Try with namespace first, then without (Namecheap API is inconsistent)
        errs = root.findall(f".//{NC_NS}Error")
        if not errs:
            errs = root.findall(".//Error")
        if not errs:
            # Last resort: find any element with "Error" in the tag
            errs = [el for el in root.iter() if el.tag.endswith("Error") and el.text]
        msg = "; ".join(e.text.strip() for e in errs if e.text and e.text.strip())
        if not msg:
            # Return raw XML snippet for debugging
            msg = f"API error (raw): {raw[:300]}"
        return None, msg
    return root, None

def nc_find(root, tag):
    """Find all elements matching tag, ignoring namespace."""
    # Try with namespace first
    els = root.findall(f".//{NC_NS}{tag}")
    if els: return els
    # Try without namespace
    els = root.findall(f".//{tag}")
    if els: return els
    # Tag-suffix scan (handles any namespace prefix)
    return [el for el in root.iter() if el.tag == tag or el.tag.endswith(f"}}{tag}")]

def nc_list_domains():
    """Return list of domain dicts, handling pagination."""
    domains = []
    page = 1
    while True:
        root, err = nc_call("namecheap.domains.getList", {
            "PageSize": "100", "Page": str(page)
        })
        if err: return domains if domains else [], err
        page_domains = nc_find(root, "Domain")
        if not page_domains:
            break
        for d in page_domains:
            name = d.get("Name","") or d.get("name","")
            if name:
                domains.append({
                    "name":       name,
                    "expires":    d.get("Expires","") or d.get("expires",""),
                    "active":     d.get("IsExpired","false").lower() == "false",
                    "locked":     d.get("IsLocked","false").lower() == "true",
                    "auto_renew": d.get("AutoRenew","false").lower() == "true",
                    "our_dns":    d.get("IsOurDNS","false").lower() == "true",
                })
        # Check if more pages exist
        paging = nc_find(root, "Paging")
        if paging:
            try:
                total = int(nc_find(root, "TotalItems")[0].text or 0)
                page_size = int(nc_find(root, "PageSize")[0].text or 100)
                if len(domains) >= total or page_size * page >= total:
                    break
            except:
                break
        else:
            break
        page += 1
    return domains, None

def nc_get_hosts(sld, tld):
    """Return DNS host records for a domain."""
    root, err = nc_call("namecheap.domains.dns.getHosts", {"SLD": sld, "TLD": tld})
    if err: return [], err
    hosts = []
    for h in nc_find(root, "host"):
        hosts.append({
            "id":     h.get("HostId",""),
            "name":   h.get("Name",""),
            "type":   h.get("Type",""),
            "address":h.get("Address",""),
            "ttl":    h.get("TTL","1800"),
            "mxpref": h.get("MXPref","10"),
        })
    return hosts, None

def nc_set_hosts(sld, tld, hosts):
    """Set all DNS host records. hosts = list of dicts."""
    params = {"SLD": sld, "TLD": tld}
    for i, h in enumerate(hosts, 1):
        params[f"HostName{i}"]   = h.get("name","@")
        params[f"RecordType{i}"] = h.get("type","A")
        params[f"Address{i}"]    = h.get("address","")
        params[f"TTL{i}"]        = h.get("ttl","1800")
        if h.get("type","").upper() == "MX":
            params[f"MXPref{i}"] = h.get("mxpref","10")
    _, err = nc_call("namecheap.domains.dns.setHosts", params)
    return err

def nc_get_ns(sld, tld):
    """Get current nameservers."""
    root, err = nc_call("namecheap.domains.dns.getList", {"SLD": sld, "TLD": tld})
    if err: return [], err
    ns = []
    for n in nc_find(root, "Nameserver"):
        if n.text: ns.append(n.text.strip())
    # Also check attribute-based NS
    for el in nc_find(root, "DomainDNSGetListResult"):
        ns_attr = el.get("Nameserver","")
        if ns_attr:
            ns.extend(x.strip() for x in ns_attr.split(",") if x.strip())
    return list(dict.fromkeys(ns)), None  # dedupe preserving order

def nc_set_ns(sld, tld, nameservers):
    """Set custom nameservers."""
    params = {"SLD": sld, "TLD": tld, "Nameservers": ",".join(nameservers)}
    _, err = nc_call("namecheap.domains.dns.setCustom", params)
    return err

def nc_reset_ns(sld, tld):
    """Reset to Namecheap default DNS."""
    _, err = nc_call("namecheap.domains.dns.setDefault", {"SLD": sld, "TLD": tld})
    return err

# ─── Cloudflare API ───────────────────────────────────────────────────────────
CF_API = "https://api.cloudflare.com/client/v4"
CF_CONFIG = DATA_DIR / "cloudflare.json"

def cf_load():
    if CF_CONFIG.exists():
        try: return json.loads(CF_CONFIG.read_text())
        except: pass
    return {}

def cf_save(cfg):
    CF_CONFIG.write_text(json.dumps(cfg, indent=2))

def cf_call(method, path, data=None, token=None):
    """Call Cloudflare API. Returns (result_dict, error_str)."""
    if not token:
        token = cf_load().get("token","")
    if not token:
        return None, "Cloudflare token not configured"
    url = CF_API + path
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
        "User-Agent":    "PHP-MNGR/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try: resp = json.loads(e.read().decode())
        except: return None, f"HTTP {e.code}: {e.reason}"
    except Exception as e:
        return None, f"Network error: {e}"
    if not resp.get("success"):
        errs = resp.get("errors", [])
        msg = "; ".join(f"{e.get('code','')}: {e.get('message','')}" for e in errs) or "Unknown CF error"
        return None, msg
    return resp.get("result"), None

def cf_add_zone(domain, token=None):
    """Add domain to Cloudflare, return (nameservers, zone_id, error)."""
    result, err = cf_call("POST", "/zones", {"name": domain, "jump_start": True}, token=token)
    if err:
        # Maybe zone already exists — try to fetch it
        existing, err2 = cf_call("GET", f"/zones?name={domain}", token=token)
        if not err2 and existing:
            zones = existing if isinstance(existing, list) else existing.get("result", [existing])
            for z in (zones if isinstance(zones, list) else []):
                if z.get("name") == domain:
                    ns = z.get("name_servers", [])
                    return ns, z.get("id",""), None
        return [], "", err
    ns  = result.get("name_servers", [])
    zid = result.get("id", "")
    return ns, zid, None

def cf_verify_token(token):
    """Verify a Cloudflare API token by checking user info."""
    # /user/tokens/verify needs special permission; /user is more universal
    result, err = cf_call("GET", "/user/tokens/verify", token=token)
    if not err and isinstance(result, dict) and result.get("status") == "active":
        return True, None
    # Fallback: try listing zones (any valid token can do this)
    result2, err2 = cf_call("GET", "/zones?per_page=1", token=token)
    if err2 is None:
        return True, None
    # Return the most useful error message
    return False, err or err2 or "Invalid token"

def cf_list_zones(token=None):
    """List all zones (domains) in the CF account."""
    zones, page = [], 1
    while True:
        result, err = cf_call("GET", f"/zones?per_page=50&page={page}", token=token)
        if err: return [], err
        batch = result if isinstance(result, list) else []
        if not batch: break
        for z in batch:
            zones.append({
                "id":     z.get("id",""),
                "name":   z.get("name",""),
                "status": z.get("status",""),
                "active": z.get("status","") == "active",
            })
        if len(batch) < 50: break
        page += 1
    return zones, None

def cf_get_account_email(token=None):
    result, err = cf_call("GET", "/user", token=token)
    if err: return ""
    return result.get("email","") if isinstance(result, dict) else ""

# ─── Cloudflare Tunnel Backend ────────────────────────────────────────────────
import secrets as _secrets

TUNNELS_FILE = DATA_DIR / "tunnels.json"

def tunnels_load():
    if TUNNELS_FILE.exists():
        try: return json.loads(TUNNELS_FILE.read_text())
        except: pass
    return {}

def tunnels_save(t):
    TUNNELS_FILE.write_text(json.dumps(t, indent=2))

def cf_get_account_id(token=None):
    # Try /accounts first
    result, err = cf_call("GET", "/accounts?per_page=1", token=token)
    if not err and result:
        accounts = result if isinstance(result, list) else []
        if accounts: return accounts[0]["id"], None
    # Fallback: extract account_id from zones (always accessible)
    result2, err2 = cf_call("GET", "/zones?per_page=1", token=token)
    if not err2 and result2:
        zones = result2 if isinstance(result2, list) else []
        if zones and zones[0].get("account", {}).get("id"):
            return zones[0]["account"]["id"], None
    return None, err or err2 or "No Cloudflare accounts found"

def cf_get_zone_id(domain, token=None):
    """Get zone ID for a domain (tries apex domain)."""
    parts = domain.strip(".").split(".")
    # Try full domain, then apex
    for d in [domain, ".".join(parts[-2:])]:
        result, err = cf_call("GET", f"/zones?name={d}", token=token)
        if not err and result:
            zones = result if isinstance(result, list) else []
            if zones: return zones[0]["id"], None
    return None, f"Zone not found for {domain} — add it to Cloudflare first"

def cf_create_tunnel(account_id, name, token=None):
    """Create a Named Tunnel, return (tunnel_id, tunnel_token, error)."""
    secret = base64.b64encode(_secrets.token_bytes(32)).decode()
    result, err = cf_call("POST", f"/accounts/{account_id}/cfd_tunnel",
        {"name": name, "tunnel_secret": secret}, token=token)
    if err: return None, None, err
    tid  = result.get("id","")
    ttok = result.get("token","")
    if not ttok:
        # The token endpoint returns the JWT directly as the result value (a plain string)
        # We need to get the raw response and extract it carefully
        api_token = token or cf_load().get("token","")
        url = f"{CF_API}/accounts/{account_id}/cfd_tunnel/{tid}/token"
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                resp = json.loads(r.read().decode())
            if resp.get("success") and resp.get("result"):
                ttok = resp["result"]  # This IS the JWT string directly
        except Exception as e:
            pass  # Will be caught by caller check
    return tid, ttok, None

def cf_configure_tunnel_ingress(account_id, tunnel_id, hostname, service, token=None):
    """Set tunnel ingress rules via API."""
    config = {
        "config": {
            "ingress": [
                {"hostname": hostname, "service": service,
                 "originRequest": {"noTLSVerify": True}},
                {"service": "http_status:404"}
            ]
        }
    }
    _, err = cf_call("PUT", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
        config, token=token)
    return err

def cf_create_tunnel_dns(zone_id, hostname, tunnel_id, token=None):
    """Create CNAME DNS record pointing to the tunnel."""
    # Get zone name to determine the correct record name
    zone_result, _ = cf_call("GET", f"/zones/{zone_id}", token=token)
    zone_name = ""
    if zone_result and isinstance(zone_result, dict):
        zone_name = zone_result.get("name","")
    # Determine record name: @ for apex, subdomain part for subdomains
    if hostname == zone_name or hostname.rstrip(".") == zone_name.rstrip("."):
        record_name = "@"
    elif zone_name and hostname.endswith("." + zone_name):
        record_name = hostname[: -(len(zone_name) + 1)]
    else:
        record_name = hostname

    # Remove existing CNAME for this hostname first
    for rtype in ["CNAME", "A"]:
        existing, _ = cf_call("GET", f"/zones/{zone_id}/dns_records?name={hostname}&type={rtype}", token=token)
        if existing and isinstance(existing, list):
            for r in existing:
                cf_call("DELETE", f"/zones/{zone_id}/dns_records/{r['id']}", token=token)
    _, err = cf_call("POST", f"/zones/{zone_id}/dns_records", {
        "type": "CNAME",
        "name": record_name,
        "content": f"{tunnel_id}.cfargotunnel.com",
        "proxied": True,
        "ttl": 1,
    }, token=token)
    return err

def cf_run_tunnel_container(tunnel_token, container_name):
    """Start cloudflared tunnel using host network to bypass Docker bridge NAT issues."""
    db = docker_bin()
    run(f"{db} rm -f {container_name} 2>/dev/null || true")
    # Use --network host so cloudflared uses the host network stack directly
    # This avoids Docker bridge iptables blocking port 7844 to Cloudflare edge
    # The PHP containers are reachable via their exposed ports on localhost
    _, err, rc = run(
        f"{db} run -d --name {container_name} "
        f"--network host "
        f"--restart unless-stopped "
        f"cloudflare/cloudflared:latest "
        f"tunnel --no-autoupdate run --token {tunnel_token}"
    )
    if rc != 0:
        # Fallback: try bridge network with http2
        run(f"{db} rm -f {container_name} 2>/dev/null || true")
        ensure_network()
        _, err2, rc2 = run(
            f"{db} run -d --name {container_name} "
            f"--network {NETWORK} "
            f"--restart unless-stopped "
            f"cloudflare/cloudflared:latest "
            f"tunnel --no-autoupdate --protocol http2 run --token {tunnel_token}"
        )
        if rc2 != 0:
            return False, err2
    import time as _time
    _time.sleep(4)
    status = container_status(container_name)
    if status not in ("running",):
        import subprocess as _sp
        logs, _, _ = run(f"{db} logs --tail=30 {container_name} 2>&1")
        return False, f"Container exited.\n{logs}"
    return True, ""

def cf_get_tunnel_logs(container_name, lines=50):
    db = docker_bin()
    logs, _, _ = run(f"{db} logs --tail={lines} {container_name} 2>&1")
    return logs

def cf_stop_tunnel_container(container_name):
    db = docker_bin()
    # Disable restart policy first so Docker doesn't auto-restart it
    run(f"{db} update --restart=no {container_name} 2>/dev/null || true")
    run(f"{db} stop {container_name} 2>/dev/null || true")
    run(f"{db} rm -f {container_name} 2>/dev/null || true")

def cf_delete_tunnel(account_id, tunnel_id, token=None):
    """Clean up tunnel from Cloudflare."""
    # Must disconnect connections first
    cf_call("DELETE", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/connections", token=token)
    _, err = cf_call("DELETE", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}", token=token)
    return err

def cf_tunnel_status(container_name):
    db = docker_bin()
    out, _, rc = run(f"{db} inspect --format='{{{{.State.Status}}}}' {container_name} 2>/dev/null")
    if rc != 0: return "stopped"
    return out.strip("'")

def nc_debug_raw(cmd, extra=None):
    """Return raw XML response for debugging."""
    cfg = nc_load()
    if not cfg.get("api_key"): return "Not configured"
    ip = cfg.get("client_ip") or nc_get_ip()
    params = {
        "ApiUser": cfg["username"], "ApiKey": cfg["api_key"],
        "UserName": cfg["username"], "ClientIp": ip, "Command": cmd,
    }
    if extra: params.update(extra)
    base = NC_SBX if cfg.get("sandbox") else NC_API
    url = base + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=15) as r:
            return r.read().decode()
    except Exception as e:
        return str(e)

def nc_split_domain(domain):
    """Split 'sub.example.co.uk' → (sld='example', tld='co.uk') roughly."""
    if not domain:
        return "", ""
    parts = domain.lower().strip().split(".")
    if len(parts) >= 2:
        return ".".join(parts[:-1]), parts[-1]
    return domain, ""

def docker_running():
    db = docker_bin()
    _, _, rc = run(f"{db} ps -q 2>&1")
    if rc == 0:
        return True
    _, _, rc2 = run(f"{db} --version 2>&1")
    return rc2 == 0

def container_status(name):
    db = docker_bin()
    out, _, rc = run(f"{db} inspect --format='{{{{.State.Status}}}}' {name} 2>/dev/null")
    if rc != 0:
        return "missing"
    return out.strip("'")

def next_port(sites):
    used = {s.get("port", 0) for s in sites.values()}
    p = 8100
    while p in used:
        p += 1
    return p

def ensure_network():
    _, err, rc = run(f"{docker_bin()} network inspect {NETWORK} 2>&1")
    if rc != 0:
        _, _, rc2 = run(f"{docker_bin()} network create {NETWORK}")
        if rc2 != 0:
            # Already exists race condition — that's fine
            pass

def ensure_proxy():
    status = container_status(PROXY_NAME)
    if status in ("missing", "exited", "created"):
        run(f"{docker_bin()} rm -f {PROXY_NAME} 2>/dev/null || true")
        ensure_network()
        nginx_conf = NGINX_DIR / "nginx.conf"
        if not nginx_conf.exists():
            write_base_nginx_conf()
        run(
            f"{docker_bin()} run -d --name {PROXY_NAME} "
            f"--network {NETWORK} "
            f"-p 80:80 -p 443:443 "
            f"-v {NGINX_DIR}:/etc/nginx/conf.d:ro "
            f"-v {CERTS_DIR}:/etc/nginx/certs:ro "
            f"--restart unless-stopped "
            f"nginx:alpine"
        )
    elif status == "running":
        pass
    return container_status(PROXY_NAME) == "running"

def reload_proxy():
    run(f"{docker_bin()} exec {PROXY_NAME} nginx -s reload 2>/dev/null || true")

def write_base_nginx_conf():
    conf = """# PHP-MNGR Base Nginx Config
server {
    listen 80 default_server;
    return 444;
}
"""
    (NGINX_DIR / "00-default.conf").write_text(conf)

def write_site_nginx_conf(site):
    sid     = site["id"]
    name    = site["name"]
    domain  = site.get("domain", "")
    mode    = site.get("mode", "local")
    has_ssl = site.get("ssl", False) and (CERTS_DIR / f"{sid}.crt").exists()
    # Use internal port 80 — containers talk over Docker network, not host-mapped ports
    internal = "80"

    lines = []

    if mode == "public" and has_ssl:
        lines.append(f"""server {{
    listen 80;
    server_name {domain};
    return 301 https://$host$request_uri;
}}

server {{
    listen 443 ssl;
    server_name {domain};
    ssl_certificate     /etc/nginx/certs/{sid}.crt;
    ssl_certificate_key /etc/nginx/certs/{sid}.key;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         HIGH:!aNULL:!MD5;
    client_max_body_size 100M;

    location / {{
        proxy_pass         http://{name}:{internal};
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }}
}}""")
    elif domain:
        lines.append(f"""server {{
    listen 80;
    server_name {domain};
    client_max_body_size 100M;

    location / {{
        proxy_pass         http://{name}:{internal};
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
    }}
}}""")

    conf_path = NGINX_DIR / f"{sid}.conf"
    if lines:
        conf_path.write_text("\n".join(lines))
    elif conf_path.exists():
        conf_path.unlink()

def remove_site_nginx_conf(sid):
    p = NGINX_DIR / f"{sid}.conf"
    if p.exists():
        p.unlink()

def generate_self_signed(sid, domain):
    key = CERTS_DIR / f"{sid}.key"
    crt = CERTS_DIR / f"{sid}.crt"
    subj = f"/CN={domain or 'localhost'}"
    run(f'openssl req -x509 -newkey rsa:2048 -keyout {key} -out {crt} -days 3650 -nodes -subj "{subj}"')
    return key.exists() and crt.exists()

def issue_letsencrypt(sid, domain, email):
    """Run certbot in a Docker container"""
    webroot = SITES_DIR / sid / "www"
    wellknown = webroot / ".well-known" / "acme-challenge"
    wellknown.mkdir(parents=True, exist_ok=True)

    out, err, rc = run(
        f"{docker_bin()} run --rm "
        f"-v {CERTS_DIR}:/etc/letsencrypt/live/out "
        f"-v {webroot}:/var/www/html "
        f"certbot/certbot certonly --webroot "
        f"--webroot-path=/var/www/html "
        f"--email {email} --agree-tos --no-eff-email "
        f"-d {domain} "
        f"--cert-name {sid} "
        f"--config-dir /tmp/le --work-dir /tmp/le-work --logs-dir /tmp/le-logs "
        f"--cert-path /etc/letsencrypt/live/out/{sid}.crt "
        f"--key-path /etc/letsencrypt/live/out/{sid}.key"
    )
    return rc == 0

def create_php_container(site):
    sid    = site["id"]
    name   = site["name"]
    port   = site["port"]
    phpver = site.get("php_version", "8.2")
    www    = SITES_DIR / sid / "www"
    www.mkdir(parents=True, exist_ok=True)

    # Write default index.php if empty
    index = www / "index.php"
    if not index.exists():
        index.write_text(f"""<?php
// PHP-MNGR | Site: {site['name']}
header('X-Powered-By: PHP-MNGR');
?>
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{site['name']}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:#0e0e0e;color:#ececec;font-family:'Inter',system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}}
.card{{background:#171717;border:1px solid #3a3a3a;border-radius:12px;padding:40px 48px;text-align:center;max-width:440px}}
.icon{{font-size:36px;margin-bottom:16px}}
h1{{font-size:22px;font-weight:600;color:#10a37f;margin-bottom:10px}}
p{{font-size:14px;color:#acacac;line-height:1.7;margin-bottom:20px}}
.badge{{display:inline-block;background:rgba(16,163,127,0.12);color:#10a37f;border:1px solid rgba(16,163,127,0.25);border-radius:6px;padding:3px 12px;font-size:11px;font-family:'IBM Plex Mono',monospace;margin-bottom:10px}}
.php{{font-family:'IBM Plex Mono',monospace;font-size:11px;color:#676767}}
</style>
</head>
<body>
<div class="card">
  <div class="icon">⚡</div>
  <h1>{site['name']}</h1>
  <p>Your PHP site is live and ready.<br>Drop your files in the web root to get started.</p>
  <div class="badge">PHP-MNGR</div>
  <div class="php">PHP <?php echo phpversion(); ?> · Apache</div>
</div>
</body>
</html>
""")

    # Write php.ini
    phpini = SITES_DIR / sid / "php.ini"
    if not phpini.exists():
        phpini.write_text("""upload_max_filesize = 100M
post_max_size = 100M
memory_limit = 256M
max_execution_time = 300
display_errors = On
error_reporting = E_ALL
""")

    ensure_network()
    run(f"{docker_bin()} rm -f {name} 2>/dev/null || true")

    cmd = (
        f"{docker_bin()} run -d --name {name} "
        f"--network {NETWORK} "
        f"-p {port}:80 "
        f"-v {www}:/var/www/html "
        f"-v {SITES_DIR / sid / 'php.ini'}:/usr/local/etc/php/conf.d/custom.ini "
        f"--restart unless-stopped "
        f"php:{phpver}-apache"
    )
    _, err, rc = run(cmd)
    return rc == 0, err

# ─── File Manager ─────────────────────────────────────────────────────────────
def fm_list(sid, rel_path=""):
    base = SITES_DIR / sid / "www"
    target = (base / rel_path).resolve()
    if not str(target).startswith(str(base)):
        return {"error": "Access denied"}
    if not target.exists():
        return {"error": "Path not found"}
    entries = []
    for item in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        stat = item.stat()
        mode = oct(stat.st_mode)[-3:]  # e.g. "755"
        entries.append({
            "name": item.name,
            "type": "dir" if item.is_dir() else "file",
            "size": stat.st_size,
            "modified": int(stat.st_mtime),
            "ext": item.suffix.lower() if item.is_file() else "",
            "mode": mode,
        })
    return {"path": rel_path or "/", "entries": entries}

def fm_chmod(sid, paths, mode_str):
    """Apply chmod recursively to paths."""
    base = SITES_DIR / sid / "www"
    try:
        mode = int(mode_str, 8)
    except ValueError:
        return {"error": f"Invalid mode: {mode_str}"}
    changed, errors = 0, []
    for rel in paths:
        target = (base / rel).resolve()
        if not str(target).startswith(str(base)):
            errors.append(f"{rel}: access denied"); continue
        if not target.exists():
            errors.append(f"{rel}: not found"); continue
        try:
            if target.is_dir():
                for root, dirs, files in os.walk(target):
                    os.chmod(root, mode)
                    for f in files:
                        os.chmod(os.path.join(root, f), mode)
            os.chmod(target, mode)
            changed += 1
        except Exception as e:
            errors.append(f"{rel}: {e}")
    return {"ok": changed > 0, "changed": changed, "errors": errors}

def fm_download_zip(sid, paths):
    """Zip selected paths into memory and return bytes."""
    import zipfile as zf, io
    base = SITES_DIR / sid / "www"
    buf = io.BytesIO()
    with zf.ZipFile(buf, 'w', zf.ZIP_DEFLATED) as z:
        for rel in paths:
            target = (base / rel).resolve()
            if not str(target).startswith(str(base)): continue
            if not target.exists(): continue
            if target.is_dir():
                for f in target.rglob("*"):
                    if f.is_file():
                        z.write(f, f.relative_to(base))
            elif target.is_file():
                z.write(target, target.relative_to(base))
    return buf.getvalue()

def fm_read(sid, rel_path):
    base = SITES_DIR / sid / "www"
    target = (base / rel_path).resolve()
    if not str(target).startswith(str(base)):
        return {"error": "Access denied"}
    if not target.is_file():
        return {"error": "Not a file"}
    try:
        content = target.read_text(encoding="utf-8")
        return {"content": content, "path": rel_path}
    except:
        return {"error": "Cannot read binary file"}

def fm_write(sid, rel_path, content):
    base = SITES_DIR / sid / "www"
    target = (base / rel_path).resolve()
    if not str(target).startswith(str(base)):
        return {"error": "Access denied"}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"ok": True}

def fm_delete(sid, rel_path):
    base = SITES_DIR / sid / "www"
    target = (base / rel_path).resolve()
    if not str(target).startswith(str(base)):
        return {"error": "Access denied"}
    if target == base:
        return {"error": "Cannot delete root"}
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"ok": True}

def fm_mkdir(sid, rel_path):
    base = SITES_DIR / sid / "www"
    target = (base / rel_path).resolve()
    if not str(target).startswith(str(base)):
        return {"error": "Access denied"}
    target.mkdir(parents=True, exist_ok=True)
    return {"ok": True}

def fm_upload(sid, rel_dir, filename, data):
    import zipfile as zf
    base = SITES_DIR / sid / "www"
    target_dir = (base / rel_dir).resolve()
    if not str(target_dir).startswith(str(base)):
        return {"error": "Access denied"}
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / filename
    dest.write_bytes(data)
    return {"ok": True, "path": str(dest.relative_to(base)), "size": len(data)}

def fm_extract(sid, rel_path):
    """Extract a zip file into its parent directory."""
    import zipfile as zf
    base = SITES_DIR / sid / "www"
    zip_path = (base / rel_path).resolve()
    if not str(zip_path).startswith(str(base)):
        return {"error": "Access denied"}
    if not zip_path.exists():
        return {"error": "File not found"}
    if not zip_path.suffix.lower() == ".zip":
        return {"error": "Not a zip file"}
    extract_dir = zip_path.parent
    try:
        with zf.ZipFile(zip_path, 'r') as z:
            # Safety: reject absolute paths and path traversal
            for member in z.namelist():
                member_path = (extract_dir / member).resolve()
                if not str(member_path).startswith(str(base)):
                    return {"error": f"Unsafe path in zip: {member}"}
            z.extractall(extract_dir)
            count = len(z.namelist())
        return {"ok": True, "extracted": count}
    except zf.BadZipFile:
        return {"error": "Invalid or corrupt zip file"}
    except Exception as e:
        return {"error": str(e)}

def fm_compress(sid, rel_paths, zip_name):
    """Compress files/folders into a zip in the same directory."""
    import zipfile as zf
    base = SITES_DIR / sid / "www"
    if not rel_paths:
        return {"error": "Nothing to compress"}
    # Put zip next to the first item
    first = (base / rel_paths[0]).resolve()
    if not str(first).startswith(str(base)):
        return {"error": "Access denied"}
    zip_dest = first.parent / zip_name
    if not zip_dest.suffix.lower() == ".zip":
        zip_dest = Path(str(zip_dest) + ".zip")
    try:
        with zf.ZipFile(zip_dest, 'w', zf.ZIP_DEFLATED) as z:
            total = 0
            for rel in rel_paths:
                target = (base / rel).resolve()
                if not str(target).startswith(str(base)):
                    continue
                if target.is_dir():
                    for f in target.rglob("*"):
                        if f.is_file():
                            z.write(f, f.relative_to(base))
                            total += 1
                elif target.is_file():
                    z.write(target, target.relative_to(base))
                    total += 1
        return {"ok": True, "zip": zip_dest.name, "files": total}
    except Exception as e:
        return {"error": str(e)}

def fm_rename(sid, old_path, new_name):
    base = SITES_DIR / sid / "www"
    old = (base / old_path).resolve()
    new = old.parent / new_name
    if not str(old).startswith(str(base)) or not str(new).startswith(str(base)):
        return {"error": "Access denied"}
    old.rename(new)
    return {"ok": True}

def fm_move(sid, paths, dest_dir):
    """Move one or more files/folders to dest_dir."""
    base = SITES_DIR / sid / "www"
    dest = (base / dest_dir).resolve()
    if not str(dest).startswith(str(base)):
        return {"error": "Access denied"}
    dest.mkdir(parents=True, exist_ok=True)
    moved, errors = 0, []
    for rel in paths:
        src = (base / rel).resolve()
        if not str(src).startswith(str(base)):
            errors.append(f"{rel}: access denied")
            continue
        if not src.exists():
            errors.append(f"{rel}: not found")
            continue
        target = dest / src.name
        # If dest already has a file with same name, add suffix
        if target.exists():
            stem = src.stem if src.is_file() else src.name
            suffix = src.suffix if src.is_file() else ""
            i = 1
            while target.exists():
                target = dest / f"{stem}_{i}{suffix}"
                i += 1
        shutil.move(str(src), str(target))
        moved += 1
    if errors:
        return {"ok": moved > 0, "moved": moved, "errors": errors}
    return {"ok": True, "moved": moved}

def fm_list_dirs(sid, rel_path=""):
    """Return recursive directory tree for move dialog."""
    base = SITES_DIR / sid / "www"
    target = (base / rel_path).resolve()
    if not str(target).startswith(str(base)):
        return []
    dirs = []
    try:
        for item in sorted(target.iterdir(), key=lambda x: x.name.lower()):
            if item.is_dir():
                rel = str(item.relative_to(base))
                dirs.append(rel)
                dirs.extend(fm_list_dirs(sid, rel))
    except:
        pass
    return dirs

# ─── HTTP Handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client disconnected before response — safe to ignore

    def send_html(self, html):
        body = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # client disconnected before response — safe to ignore

    def read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return b""
        # Read in chunks to handle large file uploads reliably
        data = b""
        remaining = length
        chunk_size = 1024 * 1024  # 1MB chunks
        while remaining > 0:
            chunk = self.rfile.read(min(chunk_size, remaining))
            if not chunk:
                break
            data += chunk
            remaining -= len(chunk)
        return data

    def read_json(self):
        try:
            return json.loads(self.read_body())
        except:
            return {}

    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        path = p.path
        qs   = urllib.parse.parse_qs(p.query)

        if path == "/":
            self.send_html(HTML)
            return

        if path == "/api/status":
            sites = load_sites()
            for sid, site in sites.items():
                sites[sid]["status"] = container_status(site["name"])
            proxy_ok = container_status(PROXY_NAME) == "running"
            docker_ok = docker_running()
            self.send_json({
                "sites": sites,
                "proxy": proxy_ok,
                "docker": docker_ok,
                "php_versions": PHP_VERSIONS
            })
            return

        if path == "/api/fm/list":
            sid      = qs.get("sid", [""])[0]
            rel_path = qs.get("path", [""])[0]
            self.send_json(fm_list(sid, rel_path))
            return

        if path == "/api/fm/read":
            sid      = qs.get("sid", [""])[0]
            rel_path = qs.get("path", [""])[0]
            self.send_json(fm_read(sid, rel_path))
            return

        if path == "/api/fm/dirs":
            sid = qs.get("sid", [""])[0]
            self.send_json({"dirs": fm_list_dirs(sid)})
            return

        if path == "/api/fm/download":
            sid      = qs.get("sid", [""])[0]
            rel_path = qs.get("path", [""])[0]
            base = SITES_DIR / sid / "www"
            target = (base / rel_path).resolve()
            if not str(target).startswith(str(base)) or not target.is_file():
                self.send_json({"error": "Not found"}); return
            data = target.read_bytes()
            fname = target.name
            ct = mimetypes.guess_type(fname)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", len(data))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if path == "/api/fm/download-zip":
            sid   = qs.get("sid", [""])[0]
            paths = qs.get("paths", [""])[0].split("|") if qs.get("paths") else []
            if not sid or not paths:
                self.send_json({"error": "Missing params"}); return
            data = fm_download_zip(sid, paths)
            fname = "download.zip"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
            self.send_header("Content-Length", len(data))
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return


        # ── Namecheap: Get config & IP ───────────────────────────────────────
        if path == "/api/nc/config":
            cfg = nc_load()
            safe = {k: v for k, v in cfg.items() if k != "api_key"}
            safe["has_key"] = bool(cfg.get("api_key"))
            safe["server_ip"] = nc_get_ip()
            self.send_json(safe)
            return

        if path == "/api/nc/domains":
            domains, err = nc_list_domains()
            if err: self.send_json({"error": err}); return
            self.send_json({"domains": domains})
            return

        if path == "/api/nc/debug":
            # Returns raw XML for troubleshooting
            cmd = qs.get("cmd", ["namecheap.domains.getList"])[0]
            raw = nc_debug_raw(cmd, {"PageSize":"20","Page":"1"})
            body = raw.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return

        if path == "/api/nc/hosts":
            domain = qs.get("domain", [""])[0]
            if not domain: self.send_json({"error": "domain required"}); return
            sld, tld = nc_split_domain(domain)
            hosts, err = nc_get_hosts(sld, tld)
            if err: self.send_json({"error": err}); return
            self.send_json({"hosts": hosts})
            return

        if path == "/api/nc/ns":
            domain = qs.get("domain", [""])[0]
            if not domain: self.send_json({"error": "domain required"}); return
            sld, tld = nc_split_domain(domain)
            ns, err = nc_get_ns(sld, tld)
            if err: self.send_json({"error": err}); return
            self.send_json({"nameservers": ns})
            return

        if path == "/api/cf/config":
            cfg = cf_load()
            self.send_json({"has_token": bool(cfg.get("token"))})
            return

        if path == "/api/cf/zones":
            token = cf_load().get("token","")
            if not token: self.send_json({"error": "not_configured"}); return
            zones, err = cf_list_zones(token)
            if err: self.send_json({"error": err}); return
            email = cf_get_account_email(token)
            tunnels = tunnels_load()
            cf_zone_names = {z["name"] for z in zones}
            # Set live container status on each tunnel
            for sid, t in tunnels.items():
                t["container_status"] = cf_tunnel_status(t.get("container",""))
            for z in zones:
                for sid, t in tunnels.items():
                    td = t.get("domain","")
                    if td == z["name"] or td.endswith("."+z["name"]):
                        z["tunnel_site_id"]    = sid
                        z["tunnel_status"]     = t["container_status"]
                        z["container_status"]  = t["container_status"]
                        z["tunnel_domain"]     = td
                        z["tunnel_id"]         = t.get("tunnel_id","")
                        break
            nc_domains    = []
            nc_configured = False
            try:
                nc_cfg = nc_load()
                nc_configured = bool(nc_cfg.get("api_key") and nc_cfg.get("username"))
                if nc_configured:
                    nc_list, nc_err = nc_list_domains()
                    if not nc_err:
                        for d in nc_list:
                            nc_domains.append({
                                "name":    d["name"],
                                "in_cf":   d["name"] in cf_zone_names,
                                "expires": d.get("expires",""),
                                "active":  d.get("active", True),
                            })
            except Exception:
                pass
            self.send_json({
                "zones": zones, "email": email, "tunnels": tunnels,
                "nc_domains": nc_domains, "nc_configured": nc_configured,
            })
            return

        if path == "/api/cf/tunnels":
            tunnels = tunnels_load()
            for tid, t in tunnels.items():
                t["container_status"] = cf_tunnel_status(t.get("container",""))
            self.send_json({"tunnels": tunnels})
            return

        if path == "/api/cf/tunnel/logs":
            sid = qs.get("sid", [""])[0]
            tunnels = tunnels_load()
            t = tunnels.get(sid)
            if not t: self.send_json({"error": "No tunnel"}); return
            logs = cf_get_tunnel_logs(t.get("container",""))
            self.send_json({"logs": logs})
            return

        self.send_json({"error": "Not found"}, 404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path

        # ── Create Site ──────────────────────────────────────────────────────
        if path == "/api/sites/create":
            data = self.read_json()
            sites = load_sites()
            name = data.get("name", "").strip().lower().replace(" ", "-")
            if not name:
                self.send_json({"error": "Name required"}); return
            if any(s["name"] == name for s in sites.values()):
                self.send_json({"error": f"Site '{name}' already exists"}); return

            sid = f"site_{int(time.time()*1000)}"
            port = next_port(sites)

            site = {
                "id":          sid,
                "name":        name,
                "display":     data.get("display", name),
                "php_version": data.get("php_version", "8.2"),
                "mode":        data.get("mode", "local"),
                "domain":      data.get("domain", "").strip(),
                "email":       data.get("email", "").strip(),
                "port":        port,
                "ssl":         False,
                "status":      "stopped",
                "created":     time.strftime("%Y-%m-%d"),
            }
            sites[sid] = site
            save_sites(sites)
            ok, err = create_php_container(site)
            if ok:
                sites[sid]["status"] = "running"
                write_site_nginx_conf(site)
                ensure_proxy()
                reload_proxy()
                save_sites(sites)
                self.send_json({"ok": True, "site": sites[sid]})
            else:
                self.send_json({"error": err or "Failed to create container"})
            return

        # ── Start Site ───────────────────────────────────────────────────────
        if path == "/api/sites/start":
            data  = self.read_json()
            sid   = data.get("id")
            sites = load_sites()
            if sid not in sites:
                self.send_json({"error": "Not found"}); return
            site = sites[sid]
            status = container_status(site["name"])
            if status == "running":
                self.send_json({"ok": True, "status": "running"}); return
            if status == "exited":
                _, _, rc = run(f"{docker_bin()} start {site['name']}")
                if rc == 0:
                    sites[sid]["status"] = "running"
                    save_sites(sites)
                    self.send_json({"ok": True, "status": "running"}); return
            # Recreate
            ok, err = create_php_container(site)
            if ok:
                sites[sid]["status"] = "running"
                save_sites(sites)
                self.send_json({"ok": True, "status": "running"})
            else:
                self.send_json({"error": err})
            return

        # ── Stop Site ────────────────────────────────────────────────────────
        if path == "/api/sites/stop":
            data  = self.read_json()
            sid   = data.get("id")
            sites = load_sites()
            if sid not in sites:
                self.send_json({"error": "Not found"}); return
            run(f"{docker_bin()} stop {sites[sid]['name']}")
            sites[sid]["status"] = "stopped"
            save_sites(sites)
            self.send_json({"ok": True})
            return

        # ── Delete Site ──────────────────────────────────────────────────────
        if path == "/api/sites/delete":
            data  = self.read_json()
            sid   = data.get("id")
            sites = load_sites()
            if sid not in sites:
                self.send_json({"error": "Not found"}); return
            site = sites[sid]
            run(f"{docker_bin()} stop {site['name']} 2>/dev/null || true")
            run(f"{docker_bin()} rm   {site['name']} 2>/dev/null || true")
            remove_site_nginx_conf(sid)
            reload_proxy()
            # Remove certs
            for ext in [".crt", ".key"]:
                p = CERTS_DIR / f"{sid}{ext}"
                if p.exists(): p.unlink()
            del sites[sid]
            save_sites(sites)
            self.send_json({"ok": True})
            return

        # ── Update Domain ────────────────────────────────────────────────────
        if path == "/api/sites/domain":
            data  = self.read_json()
            sid   = data.get("id")
            sites = load_sites()
            if sid not in sites:
                self.send_json({"error": "Not found"}); return
            sites[sid]["domain"] = data.get("domain", "").strip()
            sites[sid]["mode"]   = data.get("mode", sites[sid]["mode"])
            save_sites(sites)
            write_site_nginx_conf(sites[sid])
            ensure_proxy()
            reload_proxy()
            self.send_json({"ok": True})
            return

        # ── Issue Self-Signed SSL ────────────────────────────────────────────
        if path == "/api/sites/ssl/selfsigned":
            data  = self.read_json()
            sid   = data.get("id")
            sites = load_sites()
            if sid not in sites:
                self.send_json({"error": "Not found"}); return
            domain = sites[sid].get("domain") or "localhost"
            ok = generate_self_signed(sid, domain)
            if ok:
                sites[sid]["ssl"] = True
                save_sites(sites)
                write_site_nginx_conf(sites[sid])
                reload_proxy()
                self.send_json({"ok": True})
            else:
                self.send_json({"error": "openssl failed — is it installed?"})
            return

        # ── Issue Let's Encrypt SSL ──────────────────────────────────────────
        if path == "/api/sites/ssl/letsencrypt":
            data  = self.read_json()
            sid   = data.get("id")
            sites = load_sites()
            if sid not in sites:
                self.send_json({"error": "Not found"}); return
            domain = sites[sid].get("domain", "")
            email  = data.get("email", sites[sid].get("email", ""))
            if not domain:
                self.send_json({"error": "Domain required for Let's Encrypt"}); return
            if not email:
                self.send_json({"error": "Email required for Let's Encrypt"}); return
            ok = issue_letsencrypt(sid, domain, email)
            if ok:
                sites[sid]["ssl"] = True
                sites[sid]["ssl_type"] = "letsencrypt"
                save_sites(sites)
                write_site_nginx_conf(sites[sid])
                reload_proxy()
                self.send_json({"ok": True})
            else:
                self.send_json({"error": "certbot failed — check domain DNS and port 80"})
            return

        # ── Revoke SSL ───────────────────────────────────────────────────────
        if path == "/api/sites/ssl/revoke":
            data  = self.read_json()
            sid   = data.get("id")
            sites = load_sites()
            if sid not in sites:
                self.send_json({"error": "Not found"}); return
            for ext in [".crt", ".key"]:
                p = CERTS_DIR / f"{sid}{ext}"
                if p.exists(): p.unlink()
            sites[sid]["ssl"] = False
            sites[sid].pop("ssl_type", None)
            save_sites(sites)
            write_site_nginx_conf(sites[sid])
            reload_proxy()
            self.send_json({"ok": True})
            return

        # ── Restart Proxy ────────────────────────────────────────────────────
        if path == "/api/proxy/restart":
            run(f"{docker_bin()} rm -f {PROXY_NAME} 2>/dev/null || true")
            ok = ensure_proxy()
            self.send_json({"ok": ok})
            return

        # ── File Manager: Write ──────────────────────────────────────────────
        if path == "/api/fm/write":
            data = self.read_json()
            self.send_json(fm_write(data.get("sid",""), data.get("path",""), data.get("content","")))
            return

        # ── File Manager: Delete ─────────────────────────────────────────────
        if path == "/api/fm/delete":
            data = self.read_json()
            self.send_json(fm_delete(data.get("sid",""), data.get("path","")))
            return

        # ── File Manager: Mkdir ──────────────────────────────────────────────
        if path == "/api/fm/mkdir":
            data = self.read_json()
            self.send_json(fm_mkdir(data.get("sid",""), data.get("path","")))
            return

        # ── File Manager: Rename ─────────────────────────────────────────────
        if path == "/api/fm/rename":
            data = self.read_json()
            self.send_json(fm_rename(data.get("sid",""), data.get("old",""), data.get("new","")))
            return

        # ── File Manager: Upload ─────────────────────────────────────────────
        if path == "/api/fm/upload":
            ct = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in ct:
                self.send_json({"error": "Expected multipart"}); return
            body = self.read_body()
            # Parse boundary robustly — strip quotes and whitespace
            boundary = ""
            for part in ct.split(";"):
                part = part.strip()
                if part.startswith("boundary="):
                    boundary = part[len("boundary="):].strip().strip('"')
                    break
            if not boundary:
                self.send_json({"error": "No boundary"}); return
            sep = ("--" + boundary).encode()
            parts = body.split(sep)
            sid = rel_dir = filename = file_data = None
            for part in parts:
                part = part.lstrip(b"\r\n")
                if b"Content-Disposition" not in part: continue
                # Split headers from body on double CRLF
                if b"\r\n\r\n" in part:
                    headers_raw, _, payload = part.partition(b"\r\n\r\n")
                elif b"\n\n" in part:
                    headers_raw, _, payload = part.partition(b"\n\n")
                else:
                    continue
                # Strip trailing boundary marker
                payload = payload.rstrip(b"\r\n")
                if payload.endswith(b"--"):
                    payload = payload[:-2].rstrip(b"\r\n")
                header_str = headers_raw.decode(errors="ignore")
                # Only parse the Content-Disposition line — not ALL headers joined
                cd_line = ""
                for hline in header_str.replace("\r\n", "\n").split("\n"):
                    if hline.lower().startswith("content-disposition"):
                        cd_line = hline
                        break
                if 'name="sid"' in cd_line:
                    sid = payload.decode(errors="ignore").strip()
                elif 'name="path"' in cd_line:
                    rel_dir = payload.decode(errors="ignore").strip()
                elif 'name="file"' in cd_line:
                    fn_match = None
                    for h in cd_line.split(";"):
                        h = h.strip()
                        if h.lower().startswith("filename="):
                            fn_match = h[len("filename="):].strip().strip('"').strip("'")
                            # Safety: strip any stray \r\n or extra content
                            fn_match = fn_match.split("\r")[0].split("\n")[0].strip()
                            break
                    if fn_match:
                        filename = fn_match
                    file_data = payload
            if sid and filename and file_data is not None:
                result = fm_upload(sid, rel_dir or "", filename, file_data)
                # Auto-extract if it's a zip
                if result.get("ok") and filename.lower().endswith(".zip"):
                    result["is_zip"] = True
                self.send_json(result)
            else:
                self.send_json({"error": f"Parse failed — sid={sid} file={filename} data={'yes' if file_data else 'no'}"})
            return

        # ── File Manager: Extract Zip ────────────────────────────────────────
        if path == "/api/fm/extract":
            data = self.read_json()
            self.send_json(fm_extract(data.get("sid",""), data.get("path","")))
            return

        # ── File Manager: Compress ───────────────────────────────────────────
        if path == "/api/fm/compress":
            data = self.read_json()
            self.send_json(fm_compress(data.get("sid",""), data.get("paths",[]), data.get("name","archive.zip")))
            return

        if path == "/api/fm/move":
            data = self.read_json()
            self.send_json(fm_move(data.get("sid",""), data.get("paths",[]), data.get("dest","")))
            return

        if path == "/api/fm/chmod":
            data = self.read_json()
            self.send_json(fm_chmod(data.get("sid",""), data.get("paths",[]), data.get("mode","755")))
            return


        # ── Namecheap: Save config ───────────────────────────────────────────
        if path == "/api/nc/config":
            data = self.read_json()
            cfg = nc_load()
            if data.get("api_key"): cfg["api_key"] = data["api_key"]
            if data.get("username"): cfg["username"] = data["username"]
            if "sandbox" in data: cfg["sandbox"] = data["sandbox"]
            if data.get("client_ip"): cfg["client_ip"] = data["client_ip"]
            nc_save(cfg)
            # Quick verify - try to list domains
            if cfg.get("api_key") and cfg.get("username"):
                domains, err = nc_list_domains()
                if err:
                    self.send_json({"ok": True, "warning": err, "domain_count": 0})
                else:
                    self.send_json({"ok": True, "domain_count": len(domains)})
            else:
                self.send_json({"ok": True})
            return

        if path == "/api/nc/set-hosts":
            data = self.read_json()
            domain = data.get("domain","") or ""
            if not domain: self.send_json({"error": "domain required"}); return
            sld, tld = nc_split_domain(domain)
            err = nc_set_hosts(sld, tld, data.get("hosts",[]))
            if err: self.send_json({"error": err}); return
            self.send_json({"ok": True})
            return

        if path == "/api/nc/set-ns":
            data = self.read_json()
            domain = data.get("domain","") or ""
            if not domain: self.send_json({"error": "domain required"}); return
            sld, tld = nc_split_domain(domain)
            err = nc_set_ns(sld, tld, data.get("nameservers",[]))
            if err: self.send_json({"error": err}); return
            self.send_json({"ok": True})
            return

        if path == "/api/nc/reset-ns":
            data = self.read_json()
            domain = data.get("domain","") or ""
            if not domain: self.send_json({"error": "domain required"}); return
            sld, tld = nc_split_domain(domain)
            err = nc_reset_ns(sld, tld)
            if err: self.send_json({"error": err}); return
            self.send_json({"ok": True})
            return

        # ── Cloudflare ───────────────────────────────────────────────────────
        if path == "/api/cf/config":
            cfg = cf_load()
            self.send_json({"has_token": bool(cfg.get("token"))})
            return

        if path == "/api/cf/zones":
            token = cf_load().get("token","")
            if not token: self.send_json({"error": "not_configured"}); return
            zones, err = cf_list_zones(token)
            if err: self.send_json({"error": err}); return
            email = cf_get_account_email(token)
            tunnels = tunnels_load()
            # Enrich zones with live tunnel status
            cf_zone_names = {z["name"] for z in zones}
            # Set live container status on each tunnel
            for sid, t in tunnels.items():
                t["container_status"] = cf_tunnel_status(t.get("container",""))
            for z in zones:
                for sid, t in tunnels.items():
                    td = t.get("domain","")
                    if td == z["name"] or td.endswith("."+z["name"]):
                        z["tunnel_site_id"]    = sid
                        z["tunnel_status"]     = t["container_status"]
                        z["container_status"]  = t["container_status"]
                        z["tunnel_domain"]     = td
                        z["tunnel_id"]         = t.get("tunnel_id","")
                        break
            # Safely fetch Namecheap domains (optional — don't crash if NC not configured)
            nc_domains    = []
            nc_configured = False
            try:
                nc_cfg = nc_load()
                nc_configured = bool(nc_cfg.get("api_key") and nc_cfg.get("username"))
                if nc_configured:
                    nc_list, nc_err = nc_list_domains()
                    if not nc_err:
                        for d in nc_list:
                            nc_domains.append({
                                "name":    d["name"],
                                "in_cf":   d["name"] in cf_zone_names,
                                "expires": d.get("expires",""),
                                "active":  d.get("active", True),
                            })
            except Exception:
                pass  # Namecheap errors are non-fatal
            self.send_json({
                "zones":         zones,
                "email":         email,
                "tunnels":       tunnels,
                "nc_domains":    nc_domains,
                "nc_configured": nc_configured,
            })
            return

        if path == "/api/cf/save-token":
            data = self.read_json()
            token = data.get("token","").strip()
            if not token: self.send_json({"error": "Token required"}); return
            # Save first, then verify with a real API call
            cf_save({"token": token})
            # Test by listing zones — this works with any valid token
            zones, err = cf_list_zones(token)
            if err:
                # Try to get raw response for better debugging
                try:
                    import urllib.request as _ur
                    req = _ur.Request(
                        "https://api.cloudflare.com/client/v4/zones?per_page=1",
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                    )
                    with _ur.urlopen(req, timeout=10) as _r:
                        raw = _r.read().decode()
                    resp = json.loads(raw)
                    if not resp.get("success"):
                        errs = resp.get("errors", [])
                        msg = "; ".join(f"[{e.get('code','')}] {e.get('message','')}" for e in errs)
                        self.send_json({"error": msg or err}); return
                except Exception as e2:
                    self.send_json({"error": f"{err} | raw: {e2}"}); return
            self.send_json({"ok": True, "zone_count": len(zones)})
            return

        if path == "/api/cf/test-token":
            # Debug endpoint — returns raw CF response
            data = self.read_json()
            token = data.get("token","").strip()
            if not token: self.send_json({"error": "no token"}); return
            try:
                req = urllib.request.Request(
                    "https://api.cloudflare.com/client/v4/user/tokens/verify",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    raw_verify = r.read().decode()
            except Exception as e:
                raw_verify = str(e)
            try:
                req2 = urllib.request.Request(
                    "https://api.cloudflare.com/client/v4/zones?per_page=1",
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                )
                with urllib.request.urlopen(req2, timeout=10) as r2:
                    raw_zones = r2.read().decode()
            except Exception as e2:
                raw_zones = str(e2)
            self.send_json({"verify": raw_verify, "zones": raw_zones})
            return

        if path == "/api/cf/setup-domain":
            # Add domain to CF, get its nameservers, set them on Namecheap — all in one
            data = self.read_json()
            domain = data.get("domain","") or ""
            token  = data.get("token","") or cf_load().get("token","")
            if not domain: self.send_json({"error": "domain required"}); return
            if not token:  self.send_json({"error": "Cloudflare token required"}); return
            # 1. Add to Cloudflare
            ns, zone_id, err = cf_add_zone(domain, token=token)
            if err: self.send_json({"error": f"Cloudflare: {err}"}); return
            if not ns: self.send_json({"error": "No nameservers returned from Cloudflare"}); return
            # 2. Set those NS on Namecheap
            sld, tld = nc_split_domain(domain)
            nc_err = nc_set_ns(sld, tld, ns)
            if nc_err: self.send_json({"error": f"Namecheap: {nc_err}", "cf_ns": ns}); return
            self.send_json({"ok": True, "nameservers": ns, "zone_id": zone_id})
            return

        # ── CF Tunnels ───────────────────────────────────────────────────────
        if path == "/api/cf/tunnels":
            tunnels = tunnels_load()
            # Enrich with live container status
            for tid, t in tunnels.items():
                t["container_status"] = cf_tunnel_status(t.get("container",""))
            self.send_json({"tunnels": tunnels})
            return

        if path == "/api/cf/tunnel/create":
            data = self.read_json()
            site_id = data.get("site_id","")
            domain  = data.get("domain","") or ""
            service = data.get("service","")
            if not all([site_id, domain, service]):
                self.send_json({"error": "site_id, domain and service required"}); return
            token = cf_load().get("token","")
            if not token: self.send_json({"error": "Cloudflare token not configured"}); return
            # 1. Get account ID
            account_id, err = cf_get_account_id(token)
            if err: self.send_json({"error": f"Account: {err}"}); return
            # 2. Get zone ID
            zone_id, err = cf_get_zone_id(domain, token)
            if err: self.send_json({"error": f"Zone not found — is {domain} added to your Cloudflare account?"}); return
            # 3. Create tunnel (clean up existing one with same name first)
            tname = f"phpmngr-{site_id}"
            # Check for existing tunnel with same name and delete it
            existing, _ = cf_call("GET", f"/accounts/{account_id}/cfd_tunnel?name={tname}&is_deleted=false", token=token)
            if existing and isinstance(existing, list):
                for et in existing:
                    cf_call("DELETE", f"/accounts/{account_id}/cfd_tunnel/{et['id']}/connections", token=token)
                    cf_call("DELETE", f"/accounts/{account_id}/cfd_tunnel/{et['id']}", token=token)
            tunnel_id, tunnel_token, err = cf_create_tunnel(account_id, tname, token)
            if err: self.send_json({"error": f"Create tunnel: {err}"}); return
            if not tunnel_token:
                self.send_json({"error": "Tunnel created but could not get tunnel token — try again"}); return
            # 4. Configure ingress
            err = cf_configure_tunnel_ingress(account_id, tunnel_id, domain, service, token)
            if err: self.send_json({"error": f"Ingress config: {err}"}); return
            # 5. Create DNS CNAME
            dns_err = cf_create_tunnel_dns(zone_id, domain, tunnel_id, token)
            # 6. Start Docker container regardless of DNS result
            container = f"cftunnel-{site_id}"
            ok, cerr = cf_run_tunnel_container(tunnel_token, container)
            if not ok: self.send_json({"error": f"Container failed to start: {cerr}"}); return
            # 7. Save tunnel record
            tunnels = tunnels_load()
            tunnels[site_id] = {
                "site_id":      site_id,
                "tunnel_id":    tunnel_id,
                "tunnel_name":  tname,
                "tunnel_token": tunnel_token,
                "domain":       domain,
                "service":      service,
                "account_id":   account_id,
                "container":    container,
                "created":      time.strftime("%Y-%m-%d"),
            }
            tunnels_save(tunnels)
            if dns_err:
                # Tunnel is running but DNS wasn't set — tell user to add CNAME manually
                self.send_json({
                    "ok": True,
                    "tunnel_id": tunnel_id,
                    "domain": domain,
                    "dns_fix_needed": True,
                    "cname_target": f"{tunnel_id}.cfargotunnel.com",
                    "dns_err": dns_err,
                })
            else:
                self.send_json({"ok": True, "tunnel_id": tunnel_id, "domain": domain})
            return

        if path == "/api/cf/tunnel/stop":
            data = self.read_json()
            site_id = data.get("site_id","")
            tunnels = tunnels_load()
            t = tunnels.get(site_id)
            if not t: self.send_json({"error": "No tunnel record for this site"}); return
            container = t.get("container","")
            db = docker_bin()
            # Stop the tracked container
            if container:
                run(f"{db} update --restart=no {container} 2>/dev/null || true")
                run(f"{db} stop {container} 2>/dev/null || true")
                run(f"{db} rm -f {container} 2>/dev/null || true")
            # Also kill ANY other cloudflared containers (orphans from previous sessions)
            # Use name filter to catch all cftunnel-* containers regardless of image tag
            orphans, _, _ = run(f"{db} ps -q --filter name=cftunnel 2>/dev/null")
            for cid in orphans.strip().splitlines():
                cid = cid.strip()
                if cid:
                    run(f"{db} update --restart=no {cid} 2>/dev/null || true")
                    run(f"{db} rm -f {cid} 2>/dev/null || true")
            self.send_json({"ok": True, "container": container})
            return

        if path == "/api/cf/tunnel/start":
            data = self.read_json()
            site_id = data.get("site_id","")
            tunnels = tunnels_load()
            t = tunnels.get(site_id)
            if not t: self.send_json({"error": "No tunnel for this site"}); return
            # Use stored token first (faster), fall back to re-fetching from CF
            ttok = t.get("tunnel_token","")
            if not ttok:
                token = cf_load().get("token","")
                tr, err = cf_call("GET", f"/accounts/{t['account_id']}/cfd_tunnel/{t['tunnel_id']}/token", token=token)
                if err: self.send_json({"error": err}); return
                ttok = tr if isinstance(tr, str) else (tr.get("token","") if isinstance(tr, dict) else "")
            ok, cerr = cf_run_tunnel_container(ttok, t["container"])
            if not ok: self.send_json({"error": cerr}); return
            self.send_json({"ok": True})
            return

        if path == "/api/cf/tunnel/delete":
            data = self.read_json()
            site_id = data.get("site_id","")
            tunnels = tunnels_load()
            t = tunnels.get(site_id)
            if not t: self.send_json({"error": "No tunnel for this site"}); return
            token = cf_load().get("token","")
            cf_stop_tunnel_container(t["container"])
            cf_delete_tunnel(t["account_id"], t["tunnel_id"], token)
            del tunnels[site_id]
            tunnels_save(tunnels)
            self.send_json({"ok": True})
            return

        if path == "/api/cf/tunnel/repair":
            data = self.read_json()
            site_id = data.get("site_id","")
            tunnels = tunnels_load()
            t = tunnels.get(site_id)
            if not t: self.send_json({"error": "No tunnel"}); return
            token = cf_load().get("token","")
            # Prefer live site port over stored service URL
            sites = load_sites()
            site = sites.get(site_id)
            if site:
                service = f"http://localhost:{site['port']}"
            elif t.get("service",""):
                service = t["service"]
            else:
                self.send_json({"error": "No service URL — delete and recreate tunnel"}); return
            # Fix ingress config
            err = cf_configure_tunnel_ingress(t["account_id"], t["tunnel_id"], t["domain"], service, token)
            if err: self.send_json({"error": f"Ingress update failed: {err}"}); return
            # Fix DNS CNAME
            zone_id, zone_err = cf_get_zone_id(t["domain"], token)
            dns_fixed = False
            dns_err = None
            if not zone_err:
                dns_err = cf_create_tunnel_dns(zone_id, t["domain"], t["tunnel_id"], token)
                dns_fixed = not dns_err
            t["service"] = service
            tunnels_save(tunnels)
            if dns_err:
                self.send_json({
                    "error": f"DNS CNAME failed: {dns_err}\n\nToken needs Zone → DNS → Edit permission.",
                    "dns_fix_needed": True,
                    "cname_target": f"{t['tunnel_id']}.cfargotunnel.com",
                })
                return
            self.send_json({"ok": True, "service": service, "dns_fixed": dns_fixed})
            return
            # Point domain A record @ to server IP
            data = self.read_json()
            domain = data.get("domain","") or ""
            ip = data.get("ip","") or ""
            if not domain or not ip: self.send_json({"error": "domain and ip required"}); return
            sld, tld = nc_split_domain(domain)
            hosts, err = nc_get_hosts(sld, tld)
            if err: self.send_json({"error": err}); return
            # Remove existing @ A records and add new one
            hosts = [h for h in hosts if not (h["name"] == "@" and h["type"] == "A")]
            hosts.insert(0, {"name":"@","type":"A","address":ip,"ttl":"300"})
            # Also add www CNAME if not present
            has_www = any(h["name"] == "www" for h in hosts)
            if not has_www:
                hosts.append({"name":"www","type":"CNAME","address":"@","ttl":"1800"})
            err = nc_set_hosts(sld, tld, hosts)
            if err: self.send_json({"error": err}); return
            self.send_json({"ok": True, "ip": ip})
            return


        self.send_json({"error": "Not found"}, 404)

# ─── HTML UI ───────────────────────────────────────────────────────────────────
HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PHP-MNGR</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
/* ── Reset & Tokens ── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#212121;--sidebar:#171717;--surface:#2f2f2f;--raised:#383838;--hover:#404040;
  --line:#3a3a3a;--line2:#4a4a4a;
  --t1:#ececec;--t2:#acacac;--t3:#676767;
  --green:#10a37f;--green-tint:rgba(16,163,127,0.12);
  --red:#e05252;--red-tint:rgba(224,82,82,0.12);
  --yellow:#e0a050;--yel-bg:rgba(224,160,80,0.1);--yel-bd:rgba(224,160,80,0.3);
  --sans:'Inter',system-ui,sans-serif;--mono:'IBM Plex Mono',monospace;--font:'Inter',system-ui,sans-serif;
  --r:8px;--r-xs:4px;--r-sm:6px;--r-md:8px;--r-lg:12px;
  --bg2:var(--sidebar);--bg3:var(--surface);--bg4:var(--raised);
  --border:var(--line);--border2:var(--line2);--text:var(--t1);--text2:var(--t2);--text3:var(--t3);
}
html,body{height:100%;background:var(--bg);color:var(--t1);font-family:var(--sans);font-size:14px;line-height:1.5;overflow:hidden;-webkit-font-smoothing:antialiased}
/* Layout */
.app-shell{display:flex;height:100vh}
.main-area{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
/* Sidebar */
#sidebar{width:220px;flex-shrink:0;background:var(--sidebar);border-right:1px solid var(--line);display:flex;flex-direction:column;overflow:hidden}
.sb-logo{display:flex;align-items:center;gap:10px;padding:16px 14px;border-bottom:1px solid var(--line);flex-shrink:0}
.sb-logo-mark{width:30px;height:30px;border-radius:var(--r-sm);background:var(--surface);border:1px solid var(--line2);display:flex;align-items:center;justify-content:center;font-size:15px;flex-shrink:0}
.sb-logo-name{font-size:14px;font-weight:600;color:var(--t1)}
.sb-logo-sub{font-family:var(--mono);font-size:9px;color:var(--t3);margin-top:1px}
.sb-nav{flex:1;overflow-y:auto;padding:8px}
.sb-section{font-family:var(--mono);font-size:9px;font-weight:500;letter-spacing:1.2px;text-transform:uppercase;color:var(--t3);padding:10px 10px 4px}
.sb-item{display:flex;align-items:center;gap:10px;padding:8px 10px;border-radius:var(--r-md);cursor:pointer;color:var(--t2);font-size:13px;font-weight:400;transition:background .1s,color .1s;user-select:none;margin-bottom:1px}
.sb-item:hover,.sb-item.active{background:var(--surface);color:var(--t1)}
.sb-ico{font-size:13px;flex-shrink:0;width:16px;text-align:center}
.sb-foot{padding:10px 10px 14px;border-top:1px solid var(--line);flex-shrink:0;display:flex;flex-direction:column;gap:5px}
.sb-status{display:flex;align-items:center;gap:6px;font-family:var(--mono);font-size:10px;color:var(--t3);padding:2px}
/* Topbar */
#topbar{height:52px;border-bottom:1px solid var(--line);display:flex;align-items:center;padding:0 20px;gap:12px;flex-shrink:0}
.topbar-addr{font-family:var(--mono);font-size:11px;color:var(--t3);padding:2px 8px;background:var(--sidebar);border:1px solid var(--line);border-radius:var(--r-xs)}
.topbar-right{margin-left:auto;display:flex;align-items:center;gap:6px}
/* Buttons */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;padding:6px 14px;border-radius:var(--r-md);border:1px solid var(--line2);background:transparent;color:var(--t2);font-family:var(--sans);font-size:13px;font-weight:500;line-height:1.4;cursor:pointer;white-space:nowrap;transition:background .1s,color .1s,border-color .1s}
.btn:hover:not(:disabled){background:var(--surface);color:var(--t1)}
.btn:disabled{opacity:.35;cursor:not-allowed}
.btn-primary{background:var(--raised);color:var(--t1);border-color:var(--line2)}
.btn-primary:hover:not(:disabled){background:var(--hover)}
.btn-start{background:var(--green-tint);color:var(--green);border-color:rgba(16,163,127,0.25)}
.btn-start:hover:not(:disabled){background:rgba(16,163,127,0.2)}
.btn-stop{background:var(--red-tint);color:var(--red);border-color:rgba(224,82,82,0.25)}
.btn-stop:hover:not(:disabled){background:rgba(224,82,82,0.2)}
.btn-danger{background:var(--red-tint);color:var(--red);border-color:rgba(224,82,82,0.25)}
.btn-danger:hover:not(:disabled){background:rgba(224,82,82,0.2)}
.btn-outline{background:transparent;color:var(--t2);border-color:var(--line2)}
.btn-outline:hover:not(:disabled){background:var(--surface);color:var(--t1)}
.btn-icon-only{padding:6px 8px;border-color:transparent;color:var(--t3)}
.btn-icon-only:hover:not(:disabled){background:var(--surface);color:var(--t1);border-color:transparent}
.btn-sm{padding:4px 10px;font-size:12px}
.btn-lg{padding:8px 18px;font-size:13px}
.new-btn{background:var(--green-tint);color:var(--green);border:1px solid rgba(16,163,127,0.25);padding:6px 14px;border-radius:var(--r-md);font-family:var(--sans);font-size:13px;font-weight:500;cursor:pointer;transition:background .1s}
.new-btn:hover{background:rgba(16,163,127,0.2)}
.copy-btn{padding:3px 8px;font-size:11px;font-family:var(--mono);background:transparent;color:var(--t3);border:1px solid var(--line2);border-radius:var(--r-sm);cursor:pointer;transition:all .1s}
.copy-btn:hover{background:var(--surface);color:var(--t1)}
.modal-close{width:28px;height:28px;border-radius:var(--r-sm);border:1px solid var(--line2);background:transparent;color:var(--t3);cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center;transition:background .1s,color .1s}
.modal-close:hover{background:var(--surface);color:var(--t1)}
/* Content */
#content{flex:1;overflow-y:auto;padding:20px}
/* Site rows */
.section-head{font-family:var(--mono);font-size:9px;font-weight:500;letter-spacing:1.2px;text-transform:uppercase;color:var(--t3);display:flex;align-items:center;gap:10px;margin-bottom:8px;margin-top:4px}
.section-head::after{content:'';flex:1;height:1px;background:var(--line)}
.section-count{font-family:var(--mono);font-size:10px;color:var(--t3);background:var(--surface);border:1px solid var(--line2);border-radius:var(--r-xs);padding:0 5px}
.site-row{background:var(--sidebar);border:1px solid var(--line);border-radius:var(--r-lg);margin-bottom:4px;overflow:hidden;transition:border-color .12s,opacity .15s;cursor:pointer}
.site-row:hover{border-color:var(--line2)}
.site-row-main{display:flex;align-items:center;gap:10px;padding:13px 14px}
.site-icon{width:36px;height:36px;border-radius:var(--r-md);background:var(--surface);border:1px solid var(--line2);display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0}
.site-info{flex:1;min-width:0}
.site-name{font-size:14px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-bottom:2px}
.site-sub{font-family:var(--mono);font-size:11px;color:var(--t3);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.site-tags{display:flex;align-items:center;gap:5px;flex-wrap:wrap}
.site-port{flex-shrink:0;display:flex;align-items:center;gap:6px;font-family:var(--mono);font-size:13px;font-weight:500}
.site-actions{display:flex;align-items:center;gap:5px;flex-shrink:0}
.site-detail{border-top:1px solid var(--line);padding:12px 14px;background:var(--bg)}
.detail-pills{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.detail-pill{display:flex;flex-direction:column;gap:2px}
.dp-label{font-family:var(--mono);font-size:9px;color:var(--t3);text-transform:uppercase;letter-spacing:.8px}
.dp-val{font-family:var(--mono);font-size:12px;color:var(--t2);background:var(--surface);border:1px solid var(--line2);border-radius:var(--r-sm);padding:3px 8px}
.detail-url{display:flex;align-items:center;gap:8px;background:var(--surface);border:1px solid var(--line2);border-radius:var(--r-sm);padding:7px 12px}
.detail-url-val{font-family:var(--mono);font-size:11px;color:var(--t2);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* Tags */
.tag{font-family:var(--mono);font-size:10px;padding:1px 6px;border-radius:var(--r-xs);background:var(--raised);color:var(--t3);border:1px solid var(--line2);flex-shrink:0}
.tag-green{background:var(--green-tint);color:var(--green);border-color:rgba(16,163,127,0.25)}
.tag-yellow{background:var(--yel-bg);color:var(--yellow);border-color:var(--yel-bd)}
.tag-ghost{background:var(--surface);color:var(--t3);border-color:var(--line2)}
.tag-blue{background:rgba(80,130,224,0.12);color:#6ba3e0;border-color:rgba(80,130,224,0.25)}
/* LEDs */
.port-led{width:7px;height:7px;border-radius:50%;flex-shrink:0;display:inline-block;transition:background .3s}
.led-green{background:var(--green);box-shadow:0 0 6px rgba(16,163,127,0.5);animation:blink 2.4s ease-in-out infinite}
.led-red{background:var(--red)}
.led-yellow{background:var(--yellow)}
/* Forms */
.form-group{display:flex;flex-direction:column;gap:6px;margin-bottom:14px}
.form-label{font-family:var(--mono);font-size:10px;font-weight:500;text-transform:uppercase;letter-spacing:.8px;color:var(--t3)}
.form-input,.form-select{padding:8px 10px;background:var(--bg);border:1px solid var(--line2);border-radius:var(--r-sm);color:var(--t1);font-family:var(--mono);font-size:13px;outline:none;transition:border-color .1s;width:100%;appearance:none}
.form-input:focus,.form-select:focus{border-color:var(--t2)}
.form-hint{font-size:11px;color:var(--t3)}
/* Drawer */
#drawer{position:fixed;right:0;top:0;height:100%;width:340px;background:var(--sidebar);border-left:1px solid var(--line);transform:translateX(100%);transition:transform .2s cubic-bezier(0.22,1,0.36,1);z-index:200;display:flex;flex-direction:column;overflow-y:auto;pointer-events:none}
#drawer.open{transform:none;pointer-events:auto}
#drawer-bg{position:fixed;inset:0;background:rgba(0,0,0,.55);backdrop-filter:blur(2px);z-index:199;opacity:0;pointer-events:none;transition:opacity .15s}
#drawer-bg.open{opacity:1;pointer-events:auto}
.drawer-head{display:flex;align-items:center;gap:10px;padding:16px 18px;border-bottom:1px solid var(--line);flex-shrink:0}
.drawer-title{font-size:14px;font-weight:600;flex:1}
#drawer-body{padding:18px;flex:1}
.dsec{margin-bottom:20px}
.dsec-title{font-family:var(--mono);font-size:9px;font-weight:500;text-transform:uppercase;letter-spacing:1.2px;color:var(--t3);margin-bottom:10px}
.drow{display:flex;align-items:center;gap:8px;background:var(--surface);border:1px solid var(--line2);border-radius:var(--r-sm);padding:7px 12px;margin-bottom:6px}
.dlabel{font-family:var(--mono);font-size:10px;color:var(--t3);flex-shrink:0;min-width:60px}
.dval{font-family:var(--mono);font-size:11px;color:var(--t2);flex:1;overflow:hidden;text-overflow:ellipsis}
.config-info{background:var(--surface);border:1px solid var(--line2);border-radius:var(--r-sm);padding:10px 12px;font-size:12px;color:var(--t2);line-height:1.8}
/* Create modal */
#create-modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);backdrop-filter:blur(4px);z-index:300;align-items:center;justify-content:center}
#create-modal-bg:not(.hidden){display:flex}
#create-modal{background:var(--sidebar);border:1px solid var(--line2);border-radius:var(--r-lg);width:460px;max-width:95vw;padding:24px;box-shadow:0 16px 48px rgba(0,0,0,.7)}
.create-title{font-size:15px;font-weight:600;margin-bottom:18px}
.mode-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:14px}
.mode-btn{padding:10px;border-radius:var(--r-md);border:1px solid var(--line2);background:var(--bg);color:var(--t3);font-size:12px;cursor:pointer;text-align:center;transition:all .1s}
.mode-btn.selected{border-color:var(--green);background:var(--green-tint);color:var(--green)}
.modal-foot{display:flex;justify-content:flex-end;gap:8px;margin-top:18px;padding-top:16px;border-top:1px solid var(--line)}
/* Toasts */
.notify-wrap{position:fixed;bottom:20px;right:20px;z-index:999;display:flex;flex-direction:column;gap:6px;align-items:flex-end;pointer-events:none}
.notif{background:var(--surface);border:1px solid var(--line2);border-radius:var(--r-lg);padding:10px 16px;font-size:13px;color:var(--t1);box-shadow:0 4px 20px rgba(0,0,0,.5);max-width:380px;line-height:1.5;pointer-events:auto;animation:toast-in .2s cubic-bezier(0.22,1,0.36,1)}
.notif.ok{border-left:3px solid var(--green)}
.notif.err{border-left:3px solid var(--red)}
.notif.info{border-left:3px solid var(--line2)}
/* Tables */
.data-table{width:100%;border-collapse:collapse;font-size:12px}
.data-table th{font-family:var(--mono);font-size:9px;text-transform:uppercase;letter-spacing:1px;color:var(--t3);padding:6px 12px;border-bottom:1px solid var(--line);text-align:left}
.data-table td{padding:9px 12px;border-bottom:1px solid var(--line);color:var(--t2)}
.data-table tr:last-child td{border-bottom:none}
.td-name{font-weight:500;color:var(--t1)}.td-mono{font-family:var(--mono);font-size:11px}.td-dim{color:var(--t3);font-size:11px}.td-empty{color:var(--t3);text-align:center;padding:30px;font-size:12px}
/* Banners */
.warn-banner{background:var(--yel-bg);border:1px solid var(--yel-bd);color:var(--yellow);border-radius:var(--r-md);padding:10px 14px;font-size:12px;margin-bottom:14px}
.info-strip{font-size:11px;color:var(--t3);font-family:var(--mono);margin-top:8px}
.info-strip code{background:var(--surface);border:1px solid var(--line2);border-radius:var(--r-xs);padding:1px 5px;font-size:10px}
/* View panel */
.view-panel{max-width:760px}.view-panel-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px}
/* File manager */
.fm-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.8);backdrop-filter:blur(6px);z-index:500;align-items:stretch;justify-content:flex-end}
.fm-overlay.open{display:flex}
.fm-panel{width:min(920px,100vw);background:var(--sidebar);border-left:1px solid var(--line2);display:flex;flex-direction:column}
.fm-topbar{display:flex;align-items:center;gap:8px;padding:12px 16px;border-bottom:1px solid var(--line);flex-shrink:0}
.fm-title{font-size:13px;font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fm-main{flex:1;display:flex;overflow:hidden}
.fm-sidebar{width:150px;flex-shrink:0;border-right:1px solid var(--line);overflow-y:auto;padding:8px 0}
.fm-content{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
.fm-breadcrumb{padding:7px 14px;font-family:var(--mono);font-size:11px;color:var(--t3);border-bottom:1px solid var(--line);flex-shrink:0}
.fm-toolbar{display:flex;align-items:center;gap:6px;padding:7px 12px;border-bottom:1px solid var(--line);flex-wrap:wrap;flex-shrink:0;min-height:40px}
.fm-list{flex:1;overflow-y:auto}
.fm-row{display:flex;align-items:center;gap:8px;padding:7px 14px;cursor:pointer;font-size:12px;color:var(--t2);transition:background .1s}
.fm-row:hover,.fm-row.selected{background:var(--surface);color:var(--t1)}
.fm-row.sel-check{background:var(--green-tint)}
.fm-icon{font-size:14px;flex-shrink:0;width:18px;text-align:center}
.fm-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:var(--mono)}
.fm-size{font-family:var(--mono);font-size:10px;color:var(--t3);min-width:44px;text-align:right}
.fm-date{font-family:var(--mono);font-size:10px;color:var(--t3);min-width:76px;text-align:right}
.fm-perms{font-family:var(--mono);font-size:10px;color:var(--t3);min-width:30px;text-align:right}
.fm-editor{flex:1;display:flex;flex-direction:column;overflow:hidden}
.fm-editor-head{display:flex;align-items:center;gap:8px;padding:10px 14px;border-bottom:1px solid var(--line);flex-shrink:0}
.fm-editor-file{font-family:var(--mono);font-size:11px;color:var(--t3);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#fm-editor-area{flex:1;background:var(--bg);color:var(--t1);border:none;outline:none;padding:16px;font-family:var(--mono);font-size:13px;line-height:1.7;resize:none;width:100%}
.fm-empty{flex:1;display:flex;align-items:center;justify-content:center;color:var(--t3);font-size:13px;font-style:italic}
/* SSL */
.ssl-item{background:var(--surface);border:1px solid var(--line2);border-radius:var(--r-md);padding:14px;margin-bottom:8px}
.ssl-item-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
.ssl-item-actions{display:flex;gap:6px;flex-wrap:wrap}
/* Misc */
.empty-state{height:100%;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;text-align:center;padding:40px}
.empty-icon{font-size:40px;opacity:.4}
.spin{display:inline-block;animation:spin .65s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes blink{0%,100%{opacity:1}50%{opacity:.4}}
@keyframes toast-in{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
a{color:var(--green);text-decoration:none}
a:hover{text-decoration:underline}
::-webkit-scrollbar{width:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--line2);border-radius:2px}
.hidden{display:none !important}
.mono{font-family:var(--mono)}
</style>
</head>
<body>

<div class="app-shell">

<div id="sidebar">
  <div class="sb-logo">
    <div>
      <div class="sb-logo-name">PHP-MNGR</div>
      <div class="sb-logo-sub">Local development → public web, without friction</div>
    </div>
  </div>
  <div class="sb-nav">
    <div class="sb-section">Sites</div>
    <div class="sb-item active" onclick="showView('sites')"><span class="sb-ico">🌐</span>All Sites</div>
    <div class="sb-item" onclick="showView('running')"><span class="sb-ico">▶</span>Running</div>
    <div class="sb-item" onclick="showView('stopped')"><span class="sb-ico">⏹</span>Stopped</div>
    <div class="sb-section" style="margin-top:8px">System</div>
    <div class="sb-item" onclick="showView('domains')"><span class="sb-ico">☁</span>Domains &amp; Tunnels</div>
  </div>
  <div class="sb-foot">
    <div class="sb-status" id="sb-docker-status"><span class="port-led led-yellow"></span>Checking Docker...</div>
    <div style="font-size:10px;font-family:var(--mono);color:var(--t3);padding:0 2px">~/.phpmngr/</div>
  </div>
</div>

<div class="main-area">
  <div id="topbar">
    <span id="topbar-title" style="font-size:14px;font-weight:600;">All Sites</span>
    <span class="topbar-addr">PHP-MNGR &nbsp;·&nbsp; 127.0.0.1:4280</span>
    <div class="topbar-right">
      <button class="new-btn" onclick="openCreateModal()">+ New</button>
    </div>
  </div>
  <div id="content"><div class="empty-state"><div class="empty-icon">🐘</div><p style="font-size:15px;font-weight:500;color:var(--t2)">Loading...</p></div></div>
</div>

</div>

<div class="notify-wrap" id="notif"></div>

<div id="create-modal-bg" class="hidden" onclick="if(event.target===this)closeCreateModal()">
  <div id="create-modal">
    <div class="create-title">🐘 New PHP Site</div>
    <div>
      <div class="form-group"><label class="form-label">Site Name</label><input class="form-input" id="new-name" placeholder="e.g. my-website"/><div class="form-hint">Container name — lowercase, hyphens ok</div></div>
      <div class="form-group"><label class="form-label">Display Name</label><input class="form-input" id="new-display" placeholder="My Website"/></div>
      <div class="form-group"><label class="form-label">PHP Version</label>
        <select class="form-select" id="new-phpver">
          <option value="8.3">PHP 8.3 (Latest)</option><option value="8.2" selected>PHP 8.2</option>
          <option value="8.1">PHP 8.1</option><option value="8.0">PHP 8.0</option>
          <option value="7.4">PHP 7.4</option><option value="7.3">PHP 7.3</option><option value="7.2">PHP 7.2</option>
        </select>
      </div>
      <div class="form-group"><label class="form-label">Server Mode</label>
        <div class="mode-grid">
          <div class="mode-btn selected" id="mode-local" onclick="selectMode('local')">🏠 Local Only<small>No public access</small></div>
          <div class="mode-btn" id="mode-public" onclick="selectMode('public')">🌍 Public<small>Internet accessible</small></div>
        </div>
      </div>
      <div class="form-group" id="domain-group"><label class="form-label">Domain / Hostname</label><input class="form-input" id="new-domain" placeholder="mysite.example.com"/><div class="form-hint">Local: hostname or leave blank. Public: your real domain.</div></div>
      <div class="form-group hidden" id="email-group"><label class="form-label">Email (for Let's Encrypt)</label><input class="form-input" id="new-email" placeholder="admin@example.com" type="email"/></div>
    </div>
    <div class="modal-foot">
      <button class="btn btn-outline" onclick="closeCreateModal()">Cancel</button>
      <button class="btn btn-primary btn-lg" id="create-btn" onclick="createSite()">Create</button>
    </div>
  </div>
</div>

<div id="drawer-bg" onclick="closeDrawer()"></div>
<div id="drawer">
  <div class="drawer-head"><span class="drawer-title" id="drawer-title">Site Details</span><button class="modal-close" onclick="closeDrawer()">×</button></div>
  <div id="drawer-body"></div>
</div>

<div class="fm-overlay" id="fm-panel">
<div class="fm-panel">
  <div class="fm-topbar">
    <button class="btn btn-outline btn-sm" onclick="closeFM()">← Back</button>
    <span class="fm-title" id="fm-title">File Manager</span>
    <button class="btn btn-outline btn-sm" onclick="fmUploadClick()">↑ Upload</button>
    <button class="btn btn-primary btn-sm" onclick="fmNewFile()">+ File</button>
    <button class="btn btn-outline btn-sm" onclick="fmNewFolder()">📁 Folder</button>
    <input type="file" id="fm-upload-input" multiple style="display:none" onchange="fmUploadFiles(event)">
  </div>
  <div class="fm-main">
    <div class="fm-sidebar" id="fm-sidebar"></div>
    <div class="fm-content" id="fm-content">
      <div class="fm-breadcrumb" id="fm-breadcrumb"></div>
      <div class="fm-toolbar" id="fm-toolbar">
        <span id="fm-selection-info" style="color:var(--t3);font-size:11px;flex:1;font-family:var(--mono)"></span>
        <button class="btn btn-outline btn-sm" id="fm-extract-btn" onclick="fmExtractZip()" style="display:none">📦 Extract</button>
        <button class="btn btn-outline btn-sm" id="fm-compress-btn" onclick="fmCompressSelected()" style="display:none">🗜 Compress</button>
        <button class="btn btn-outline btn-sm" id="fm-download-btn" onclick="fmDownloadSelected()" style="display:none">⬇ Download</button>
        <button class="btn btn-outline btn-sm" id="fm-chmod-btn" onclick="openChmodModal(null,null)" style="display:none">🔐 Chmod</button>
        <button class="btn btn-outline btn-sm" id="fm-move-btn" onclick="openMoveModal()" style="display:none">✂ Move</button>
        <button class="btn btn-outline btn-sm" id="fm-rename-btn" onclick="fmRename()" style="display:none">✏ Rename</button>
        <button class="btn btn-danger btn-sm" id="fm-delete-btn" onclick="fmDeleteSelected()" style="display:none">🗑 Delete</button>
      </div>
      <div id="fm-file-list" class="fm-list"></div>
      <div id="fm-editor" class="fm-editor hidden">
        <div class="fm-editor-head">
          <span class="fm-editor-file" id="fm-editor-path"></span>
          <button class="btn btn-primary btn-sm" onclick="fmSaveFile()">💾 Save</button>
          <button class="btn btn-outline btn-sm" onclick="closeFMEditor()">Close</button>
        </div>
        <textarea class="fm-editor-area" id="fm-editor-area" spellcheck="false"></textarea>
      </div>
    </div>
  </div>
</div>
</div><!-- end fm-overlay -->

<!-- Move Modal -->
<div id="fm-move-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);backdrop-filter:blur(6px);z-index:600;align-items:center;justify-content:center">
  <div style="background:var(--bg2);border:1px solid var(--border2);border-radius:12px;width:420px;max-height:72vh;display:flex;flex-direction:column;box-shadow:0 24px 60px rgba(0,0,0,.6)">
    <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--border)">
      <span style="font-size:13px;font-weight:600">✂ Move to...</span>
      <button class="modal-close" onclick="closeMoveModal()">×</button>
    </div>
    <div style="padding:10px 12px;border-bottom:1px solid var(--border)">
      <div style="font-size:11px;color:var(--text3);margin-bottom:6px">Moving: <span id="fm-move-count" style="color:var(--text2);font-weight:500"></span></div>
      <input class="form-input" id="fm-move-filter" placeholder="Filter directories..." oninput="filterMoveDirs()" style="font-size:12px;padding:6px 10px"/>
    </div>
    <div id="fm-move-dirs" style="overflow-y:auto;flex:1;padding:6px;min-height:120px"></div>
    <div style="padding:10px 14px;border-top:1px solid var(--border);display:flex;gap:8px;justify-content:flex-end">
      <button class="btn btn-outline" onclick="closeMoveModal()">Cancel</button>
      <button class="btn btn-primary" id="fm-move-confirm" onclick="confirmMove()" disabled>Move Here</button>
    </div>
  </div>
</div>

<!-- Chmod Modal -->
<div id="fm-chmod-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);backdrop-filter:blur(6px);z-index:600;align-items:center;justify-content:center">
  <div style="background:var(--bg2);border:1px solid var(--border2);border-radius:12px;width:380px;box-shadow:0 24px 60px rgba(0,0,0,.6)">
    <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--border)">
      <span style="font-size:13px;font-weight:600">🔐 Set Permissions</span>
      <button class="modal-close" onclick="closeChmodModal()">×</button>
    </div>
    <div style="padding:16px 18px">
      <div style="font-size:11px;color:var(--text3);margin-bottom:12px">Target: <span id="fm-chmod-target" style="color:var(--text2);font-family:var(--mono)"></span></div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:14px" id="fm-chmod-grid"></div>
      <div style="display:flex;align-items:center;gap:10px;background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:8px 12px">
        <span style="font-size:11px;color:var(--text3)">Octal:</span>
        <input class="form-input" id="fm-chmod-octal" maxlength="4" style="width:80px;font-family:var(--mono);font-size:14px;text-align:center;padding:4px 8px" oninput="syncChmodFromOctal()"/>
        <span style="font-size:11px;color:var(--text3)">e.g. 755, 644, 777</span>
      </div>
      <div style="font-size:11px;color:var(--text3);margin-top:8px" id="fm-chmod-hint"></div>
    </div>
    <div style="padding:10px 14px;border-top:1px solid var(--border);display:flex;gap:8px;justify-content:flex-end">
      <button class="btn btn-outline" onclick="closeChmodModal()">Cancel</button>
      <button class="btn btn-primary" onclick="confirmChmod()">Apply</button>
    </div>
  </div>
</div>

<!-- Point to Site Modal -->
<div id="nc-pts-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);backdrop-filter:blur(6px);z-index:400;align-items:center;justify-content:center">
  <div style="background:var(--bg2);border:1px solid var(--border2);border-radius:12px;width:460px;box-shadow:0 24px 60px rgba(0,0,0,.6)">
    <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--border)">
      <span style="font-size:13px;font-weight:600">🎯 Point Domain to Site</span>
      <button class="modal-close" onclick="ncClosePTS()">×</button>
    </div>
    <div style="padding:16px 18px">
      <div style="margin-bottom:14px">
        <div style="font-size:11px;color:var(--text3);margin-bottom:4px">Domain</div>
        <div style="font-family:var(--mono);font-size:14px;font-weight:600;color:var(--green)" id="pts-domain-label"></div>
      </div>
      <div class="form-group">
        <label class="form-label">Docker Web Server</label>
        <select class="form-select" id="pts-site-select" style="font-size:13px">
          <option value="">— select a site —</option>
        </select>
        <div class="form-hint" id="pts-site-hint"></div>
      </div>
      <div style="background:var(--bg3);border:1px solid var(--border);border-radius:6px;padding:10px 12px;font-size:12px;color:var(--text2);line-height:1.8" id="pts-summary">
        Select a site above to see what will happen.
      </div>
    </div>
    <div style="padding:10px 18px;border-top:1px solid var(--border);display:flex;gap:8px;justify-content:flex-end">
      <button class="btn btn-outline" onclick="ncClosePTS()">Cancel</button>
      <button class="btn btn-primary btn-lg" id="pts-apply-btn" onclick="ncApplyPTS()" disabled>Apply</button>
    </div>
  </div>
</div>

<!-- Auto-Set NS Wizard Modal -->
<div id="nc-autons-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);backdrop-filter:blur(6px);z-index:400;align-items:center;justify-content:center">
  <div style="background:var(--bg2);border:1px solid var(--border2);border-radius:12px;width:520px;max-height:90vh;overflow-y:auto;box-shadow:0 24px 60px rgba(0,0,0,.6)">
    <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--bg2);z-index:1">
      <div>
        <div style="font-size:13px;font-weight:600">⚡ Set Nameservers</div>
        <div style="font-size:11px;color:var(--text3);margin-top:1px" id="autons-domain-label"></div>
      </div>
      <button class="modal-close" onclick="ncCloseAutoNS()">×</button>
    </div>
    <div id="autons-body" style="padding:16px 18px"></div>
  </div>
</div>

<!-- Combined API Settings Modal -->
<div id="cf-settings-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);backdrop-filter:blur(6px);z-index:400;align-items:center;justify-content:center">
  <div style="background:var(--bg2);border:1px solid var(--border2);border-radius:12px;width:540px;max-height:92vh;overflow-y:auto;box-shadow:0 24px 60px rgba(0,0,0,.6)">
    <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--border)">
      <div style="font-size:13px;font-weight:600">⚙ API Settings</div>
      <button class="modal-close" onclick="closeCFSettings()">×</button>
    </div>
    <!-- Tabs -->
    <div style="display:flex;border-bottom:1px solid var(--border)">
      <button id="stab-cf" onclick="switchSettingsTab('cf')"
        style="flex:1;padding:10px;font-size:12px;font-weight:500;background:var(--bg3);border:none;border-bottom:2px solid #f6821f;color:var(--text);cursor:pointer;font-family:var(--font)">
        ☁ Cloudflare
      </button>
      <button id="stab-nc" onclick="switchSettingsTab('nc')"
        style="flex:1;padding:10px;font-size:12px;font-weight:500;background:transparent;border:none;border-bottom:2px solid transparent;color:var(--text2);cursor:pointer;font-family:var(--font)">
        🌐 Namecheap
      </button>
    </div>

    <!-- Cloudflare Tab -->
    <div id="stab-cf-body" style="padding:18px">
      <div id="cf-settings-status-bar"></div>
      <div style="background:var(--yel-bg);border:1px solid var(--yel-bd);border-radius:7px;padding:10px 14px;font-size:12px;color:var(--yellow);margin-bottom:14px;line-height:1.7">
        ⚠ <strong>Required token permissions:</strong><br>
        • Zone → Zone → <strong>Read</strong><br>
        • Zone → DNS → <strong>Edit</strong><br>
        • Account → Cloudflare Tunnel → <strong>Edit</strong><br>
        <a href="https://dash.cloudflare.com/profile/api-tokens" target="_blank" style="color:#f6821f;margin-top:4px;display:inline-block">
          Create token at Cloudflare Dashboard ↗
        </a>
        — use <strong>"Edit zone DNS"</strong> template then add the Tunnel permission.
      </div>
      <div class="form-group">
        <label class="form-label">Cloudflare API Token</label>
        <div style="display:flex;gap:8px">
          <input class="form-input" id="cf-settings-token" type="password" placeholder="Paste token here" style="flex:1"/>
          <button class="btn btn-outline" onclick="cfToggleTokenVisible()">👁</button>
        </div>
      </div>
      <div style="display:flex;gap:8px;margin-top:4px">
        <button class="btn btn-outline" onclick="closeCFSettings()">Cancel</button>
        <button class="btn btn-primary btn-lg" id="cf-settings-save-btn" onclick="cfSettingsSave()"
          style="flex:1;background:#f6821f;border-color:#f6821f">Save &amp; Verify</button>
      </div>
      <div id="cf-settings-result" style="margin-top:10px;font-size:12px;min-height:18px"></div>
    </div>

    <!-- Namecheap Tab -->
    <div id="stab-nc-body" style="padding:18px;display:none">
      <div id="nc-settings-status-bar"></div>
      <div style="background:var(--bg3);border:1px solid var(--border);border-radius:7px;padding:10px 14px;font-size:12px;color:var(--text2);margin-bottom:14px;line-height:1.8">
        <strong>Get API credentials:</strong><br>
        1. Log into Namecheap → Profile → Tools → API Access<br>
        2. Enable API and whitelist your server IP<br>
        3. Copy API key below
      </div>
      <div class="form-group">
        <label class="form-label">Namecheap Username</label>
        <input class="form-input" id="nc-settings-username" placeholder="your_username"/>
      </div>
      <div class="form-group">
        <label class="form-label">API Key</label>
        <input class="form-input" id="nc-settings-apikey" type="password" placeholder="Paste API key"/>
      </div>
      <div class="form-group">
        <label class="form-label">Client IP (whitelisted in Namecheap)</label>
        <input class="form-input" id="nc-settings-ip" placeholder="auto-detect"/>
        <div class="form-hint">Your server public IP — must be whitelisted in Namecheap API Access settings.</div>
      </div>
      <div style="display:flex;gap:8px;margin-top:4px">
        <button class="btn btn-outline" onclick="closeCFSettings()">Cancel</button>
        <button class="btn btn-primary btn-lg" id="nc-settings-save-btn" onclick="ncSettingsSave()" style="flex:1">Save Namecheap</button>
      </div>
      <div id="nc-settings-result" style="margin-top:10px;font-size:12px;min-height:18px"></div>
    </div>
  </div>
</div>
</div>

<!-- FM Rename Modal -->
<div id="fm-rename-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:600;align-items:center;justify-content:center">
  <div style="background:var(--sidebar);border:1px solid var(--line2);border-radius:var(--r-lg);width:380px;padding:22px;box-shadow:0 16px 48px rgba(0,0,0,.7)">
    <div style="font-size:14px;font-weight:600;margin-bottom:14px">✏ Rename</div>
    <input class="form-input" id="fm-rename-input" style="margin-bottom:16px" placeholder="New name"
      onkeydown="if(event.key==='Enter')fmRenameConfirm();if(event.key==='Escape')document.getElementById('fm-rename-modal').style.display='none'"/>
    <div style="display:flex;justify-content:flex-end;gap:8px">
      <button class="btn btn-outline" onclick="document.getElementById('fm-rename-modal').style.display='none'">Cancel</button>
      <button class="btn btn-primary" onclick="fmRenameConfirm()">Rename</button>
    </div>
  </div>
</div>

<!-- FM Delete Confirm Modal -->
<div id="fm-delete-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:600;align-items:center;justify-content:center">
  <div style="background:var(--sidebar);border:1px solid var(--line2);border-radius:var(--r-lg);width:360px;padding:22px;box-shadow:0 16px 48px rgba(0,0,0,.7)">
    <div style="font-size:14px;font-weight:600;margin-bottom:8px">🗑 Confirm Delete</div>
    <div id="fm-delete-msg" style="font-size:13px;color:var(--t2);margin-bottom:18px"></div>
    <div style="display:flex;justify-content:flex-end;gap:8px">
      <button class="btn btn-outline" onclick="document.getElementById('fm-delete-modal').style.display='none'">Cancel</button>
      <button class="btn btn-danger" onclick="fmDeleteConfirm()">Delete</button>
    </div>
  </div>
</div>

<!-- FM New File Modal -->
<div id="fm-newfile-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:600;align-items:center;justify-content:center">
  <div style="background:var(--sidebar);border:1px solid var(--line2);border-radius:var(--r-lg);width:380px;padding:22px;box-shadow:0 16px 48px rgba(0,0,0,.7)">
    <div style="font-size:14px;font-weight:600;margin-bottom:14px">+ New File</div>
    <input class="form-input" id="fm-newfile-input" style="margin-bottom:16px" placeholder="filename.php"
      onkeydown="if(event.key==='Enter')fmNewFileConfirm();if(event.key==='Escape')document.getElementById('fm-newfile-modal').style.display='none'"/>
    <div style="display:flex;justify-content:flex-end;gap:8px">
      <button class="btn btn-outline" onclick="document.getElementById('fm-newfile-modal').style.display='none'">Cancel</button>
      <button class="btn btn-primary" onclick="fmNewFileConfirm()">Create</button>
    </div>
  </div>
</div>

<!-- FM New Folder Modal -->
<div id="fm-newfolder-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:600;align-items:center;justify-content:center">
  <div style="background:var(--sidebar);border:1px solid var(--line2);border-radius:var(--r-lg);width:380px;padding:22px;box-shadow:0 16px 48px rgba(0,0,0,.7)">
    <div style="font-size:14px;font-weight:600;margin-bottom:14px">📁 New Folder</div>
    <input class="form-input" id="fm-newfolder-input" style="margin-bottom:16px" placeholder="folder-name"
      onkeydown="if(event.key==='Enter')fmNewFolderConfirm();if(event.key==='Escape')document.getElementById('fm-newfolder-modal').style.display='none'"/>
    <div style="display:flex;justify-content:flex-end;gap:8px">
      <button class="btn btn-outline" onclick="document.getElementById('fm-newfolder-modal').style.display='none'">Cancel</button>
      <button class="btn btn-primary" onclick="fmNewFolderConfirm()">Create</button>
    </div>
  </div>
</div>

<!-- FM Compress Modal -->
<div id="fm-compress-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:600;align-items:center;justify-content:center">
  <div style="background:var(--sidebar);border:1px solid var(--line2);border-radius:var(--r-lg);width:380px;padding:22px;box-shadow:0 16px 48px rgba(0,0,0,.7)">
    <div style="font-size:14px;font-weight:600;margin-bottom:14px">🗜 Compress to Zip</div>
    <input class="form-input" id="fm-compress-input" style="margin-bottom:16px" placeholder="archive.zip"
      onkeydown="if(event.key==='Enter')fmCompressConfirm();if(event.key==='Escape')document.getElementById('fm-compress-modal').style.display='none'"/>
    <div style="display:flex;justify-content:flex-end;gap:8px">
      <button class="btn btn-outline" onclick="document.getElementById('fm-compress-modal').style.display='none'">Cancel</button>
      <button class="btn btn-primary" onclick="fmCompressConfirm()">Compress</button>
    </div>
  </div>
</div>

<!-- CF Delete Confirm Modal -->
<div id="cf-delete-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);backdrop-filter:blur(4px);z-index:400;align-items:center;justify-content:center">
  <div style="background:var(--sidebar);border:1px solid var(--line2);border-radius:var(--r-lg);width:380px;padding:22px;box-shadow:0 16px 48px rgba(0,0,0,.7)">
    <div style="font-size:14px;font-weight:600;margin-bottom:10px">🗑 Delete Tunnel</div>
    <div id="cf-delete-msg" style="font-size:13px;color:var(--t2);margin-bottom:18px"></div>
    <div style="display:flex;justify-content:flex-end;gap:8px">
      <button class="btn btn-outline" onclick="document.getElementById('cf-delete-modal').style.display='none'">Cancel</button>
      <button class="btn btn-danger" onclick="cfTunnelDeleteConfirm()">Delete</button>
    </div>
  </div>
</div>

<!-- CF Nameservers Modal -->
<div id="cf-ns-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);backdrop-filter:blur(4px);z-index:400;align-items:center;justify-content:center">
  <div style="background:var(--sidebar);border:1px solid var(--line2);border-radius:var(--r-lg);width:420px;padding:22px;box-shadow:0 16px 48px rgba(0,0,0,.7)">
    <div style="font-size:14px;font-weight:600;margin-bottom:6px">✓ Domain Added to Cloudflare</div>
    <div style="font-size:12px;color:var(--t2);margin-bottom:14px">Set these nameservers on Namecheap for <strong id="cf-ns-domain"></strong>:<br><span style="font-size:11px;color:var(--t3)">Profile → Domain List → Manage → Nameservers → Custom DNS</span></div>
    <div id="cf-ns-list" style="background:var(--surface);border:1px solid var(--line2);border-radius:var(--r-sm);padding:10px 14px;margin-bottom:16px"></div>
    <div style="display:flex;justify-content:flex-end">
      <button class="btn btn-primary" onclick="document.getElementById('cf-ns-modal').style.display='none'">Got it</button>
    </div>
  </div>
</div>

<!-- Delete Site Modal -->
<div id="delete-site-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);backdrop-filter:blur(4px);z-index:400;align-items:center;justify-content:center">
  <div style="background:var(--sidebar);border:1px solid var(--line2);border-radius:var(--r-lg);width:360px;padding:22px;box-shadow:0 16px 48px rgba(0,0,0,.7)">
    <div style="font-size:14px;font-weight:600;margin-bottom:8px">🗑 Delete Site</div>
    <div id="delete-site-msg" style="font-size:13px;color:var(--t2);margin-bottom:18px"></div>
    <div style="display:flex;justify-content:flex-end;gap:8px">
      <button class="btn btn-outline" onclick="document.getElementById('delete-site-modal').style.display='none'">Cancel</button>
      <button class="btn btn-danger" onclick="deleteSiteConfirm()">Delete</button>
    </div>
  </div>
</div>

<!-- CF Connect Site Modal -->
<div id="cf-connect-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);backdrop-filter:blur(6px);z-index:400;align-items:center;justify-content:center">
  <div style="background:var(--bg2);border:1px solid var(--border2);border-radius:12px;width:460px;box-shadow:0 24px 60px rgba(0,0,0,.6)">
    <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--border)">
      <div style="display:flex;align-items:center;gap:10px">
        <span style="font-size:20px">☁</span>
        <div><div style="font-size:13px;font-weight:600">Connect Site via Tunnel</div>
          <div style="font-size:11px;color:#f6821f;font-family:var(--mono)" id="cf-connect-domain-label"></div>
        </div>
      </div>
      <button class="modal-close" onclick="closeCFConnect()">×</button>
    </div>
    <div style="padding:18px">
      <div style="background:var(--bg3);border:1px solid var(--border);border-radius:7px;padding:10px 14px;font-size:12px;color:var(--text2);line-height:1.8;margin-bottom:14px">
        Creates a Cloudflare Named Tunnel, configures DNS automatically,
        and starts a <code style="font-family:var(--mono);background:var(--bg4);padding:1px 5px;border-radius:3px">cloudflared</code> container.
        Your site will be live at <strong>https://</strong><span id="cf-connect-domain-label2" style="font-family:var(--mono);color:#f6821f"></span> within seconds.
      </div>
      <div class="form-group">
        <label class="form-label">PHP Site to Connect</label>
        <select class="form-select" id="cf-connect-site-sel" style="font-size:13px" onchange="cfConnectSelChange()"></select>
      </div>
      <div id="cf-connect-custom-row" style="display:none;margin-top:-6px;margin-bottom:10px">
        <label class="form-label" style="margin-bottom:4px">Custom Service URL</label>
        <input class="form-input" id="cf-connect-custom-url" placeholder="http://localhost:3000"
          style="font-family:var(--mono);font-size:12px"/>
        <div class="form-hint">Any local service — Node.js, Python, another container port, etc.</div>
      </div>
      <div id="cf-connect-status" style="font-size:12px;min-height:18px;margin-bottom:10px"></div>
      <div style="display:flex;gap:8px">
        <button class="btn btn-outline" onclick="closeCFConnect()">Cancel</button>
        <button class="btn btn-primary btn-lg" id="cf-connect-btn" onclick="cfDoConnect()"
          style="flex:1;background:#f6821f;border-color:#f6821f">☁ Tunnel Now</button>
      </div>
    </div>
  </div>
</div>

<!-- Cloudflare Tunnel Modal -->
<div id="cf-tunnel-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.65);backdrop-filter:blur(6px);z-index:400;align-items:center;justify-content:center">
  <div style="background:var(--bg2);border:1px solid var(--border2);border-radius:12px;width:500px;max-height:90vh;overflow-y:auto;box-shadow:0 24px 60px rgba(0,0,0,.6)">
    <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--bg2);z-index:1">
      <div style="display:flex;align-items:center;gap:10px">
        <span style="font-size:20px">☁</span>
        <div>
          <div style="font-size:13px;font-weight:600">Cloudflare Tunnel</div>
          <div style="font-size:11px;color:var(--text3)" id="cf-tunnel-site-label"></div>
        </div>
      </div>
      <button class="modal-close" onclick="closeTunnelModal()">×</button>
    </div>
    <div id="cf-tunnel-body" style="padding:16px 18px"></div>
  </div>
</div>

<script>
// ─────────────────────────────── State ───────────────────────────────────────
let state = { sites: {}, proxy: false, docker: false, view: 'sites' };
let drawerSid = null;
let fmSid = null;
let fmPath = '';
let fmSelection = []; // array of {path, type}
let pollTimer = null;

// ─────────────────────────────── Boot ────────────────────────────────────────
// Views that show live container status — refresh on poll
const LIVE_VIEWS = new Set(['sites','running','stopped']);
// Views that are user-driven — only render when explicitly navigated to
const STATIC_VIEWS = new Set(['domains']);
let viewRendered = false; // tracks if current static view has been rendered

async function init() {
  await poll();
  pollTimer = setInterval(poll, 8000);
}

async function poll() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    state.sites = d.sites;
    state.proxy = d.proxy;
    state.docker = d.docker;
    // Fetch tunnel status separately — non-fatal if it fails
    try {
      const tr = await fetch('/api/cf/tunnels');
      const td = await tr.json();
      if (td.tunnels) cfState.tunnels = td.tunnels;
    } catch(e) {}
    updateSidebar();
    if (LIVE_VIEWS.has(state.view)) {
      renderContent();
    }
  } catch(e) {
    console.error('poll error:', e);
  }
}

function updateSidebar() {
  const dd = document.getElementById('sb-docker-status');
  dd.innerHTML = state.docker
    ? '<span class="port-led led-green"></span>Docker running'
    : '<span class="port-led led-red"></span>Docker not found';
}

function showView(v) {
  if (v !== 'domains') cfForceSetup = false;
  state.view = v;
  viewRendered = false;
  document.querySelectorAll('.sb-item').forEach(el => el.classList.remove('active'));
  event && event.target.closest('.sb-item') && event.target.closest('.sb-item').classList.add('active');
  const titles = {sites:'All Sites', running:'Running Sites', stopped:'Stopped Sites', domains:'Domains & Cloudflare Tunnels'};
  document.getElementById('topbar-title').textContent = titles[v] || v;
  // Hide + New button on domains view
  const newBtn = document.querySelector('.new-btn');
  if (newBtn) newBtn.style.display = v === 'domains' ? 'none' : '';
  renderContent();
}



// ─────────────────────────────── Views ──────────────────────────────────────
function renderContent() {
  const el = document.getElementById('content');
  const sites = Object.values(state.sites);
  if (state.view === 'domains') {
    if (!viewRendered) { viewRendered = true; renderDomainsView(el); }
    return;
  }
  let filtered = sites;
  if (state.view === 'running') filtered = sites.filter(s => s.status === 'running');
  if (state.view === 'stopped') filtered = sites.filter(s => s.status !== 'running');
  if (filtered.length === 0) {
    el.innerHTML = `<div class="empty-state"><div class="empty-icon">🐘</div>
      <h2>${state.view==='sites'?'No sites yet':'No '+state.view+' sites'}</h2>
      <p>${state.view==='sites'?'Click New to spin up your first PHP server.':''}</p></div>`;
    return;
  }
  const running = filtered.filter(s => s.status === 'running');
  const stopped = filtered.filter(s => s.status !== 'running');
  let html = '';
  if (running.length) {
    html += `<div class="section-head">Running <span class="section-count">${running.length}</span></div>`;
    html += running.map(siteRow).join('');
  }
  if (stopped.length) {
    html += `<div class="section-head"${running.length?' style="margin-top:20px"':''}>Stopped <span class="section-count">${stopped.length}</span></div>`;
    html += stopped.map(siteRow).join('');
  }
  el.innerHTML = html;
}

function siteRow(s) {
  const running = s.status === 'running';
  const domain = s.domain || `localhost:${s.port}`;
  const url = s.ssl ? `https://${s.domain}` : `http://localhost:${s.port}`;
  const tunnel = Object.values(cfState?.tunnels||{}).find(t => t.site_id === s.id);
  const tunnelRunning = tunnel?.container_status === 'running' || tunnel?.tunnel_status === 'running';
  const publicUrl = tunnelRunning ? `https://${tunnel.domain}` : null;
  return `<div class="site-row">
    <div class="site-row-main">
      <div class="site-icon">🐘</div>
      <div class="site-info">
        <div class="site-name">${esc(s.display||s.name)}</div>
        <div class="site-sub">${publicUrl ? `<a href="${esc(publicUrl)}" target="_blank" style="color:var(--green)">${esc(publicUrl)} ↗</a>` : esc(domain)}</div>
      </div>
      <div class="site-tags">
        <span class="tag tag-yellow">PHP ${s.php_version}</span>
        ${tunnelRunning
          ? `<span class="tag tag-green" style="font-size:10px">☁ Live</span>`
          : tunnel
            ? `<span class="tag tag-ghost" style="font-size:10px">☁ Stopped</span>`
            : `<span class="tag tag-ghost" style="font-size:10px">local</span>`}
      </div>
      <div class="site-port"><span class="port-led ${running?'led-green':'led-red'}"></span>${s.port}</div>
      <div class="site-actions">
        ${running
          ? `<button class="btn btn-stop" onclick="stopSite('${s.id}');event.stopPropagation()">Stop</button>`
          : `<button class="btn btn-start" onclick="startSite('${s.id}');event.stopPropagation()">Start</button>`}
        ${publicUrl
          ? `<button class="btn btn-outline" onclick="window.open('${publicUrl}','_blank');event.stopPropagation()">Open ↗</button>`
          : running ? `<button class="btn btn-outline" onclick="window.open('${url}','_blank');event.stopPropagation()">Local ↗</button>` : ''}
        <button class="btn btn-outline btn-sm" onclick="openFM('${s.id}');event.stopPropagation()" title="Files" style="padding:5px 10px;font-size:14px">📁</button>
        <button class="btn btn-outline btn-sm" onclick="openDrawer('${s.id}');event.stopPropagation()" title="Manage" style="padding:5px 10px;font-size:14px">⚙</button>
        <button class="btn btn-outline btn-sm" onclick="showView('domains');event.stopPropagation()" title="Cloudflare Tunnel" style="padding:5px 10px;font-size:14px;color:#f6821f;border-color:rgba(246,130,31,0.35)">☁</button>
      </div>
    </div>
    ${running?`<div class="site-detail">
      <div class="detail-pills">
        <div class="detail-pill"><span class="dp-label">Host</span><span class="dp-val">127.0.0.1</span></div>
        <div class="detail-pill"><span class="dp-label">Port</span><span class="dp-val">${s.port}</span></div>
        <div class="detail-pill"><span class="dp-label">PHP</span><span class="dp-val">${s.php_version}</span></div>
        <div class="detail-pill"><span class="dp-label">Created</span><span class="dp-val">${s.created}</span></div>
      </div>
      <div class="detail-url">
        <span class="detail-url-val">${publicUrl||url}</span>
        <button class="copy-btn" onclick="copy('${publicUrl||url}')">Copy</button>
        <button class="btn btn-outline btn-sm" onclick="window.open('${publicUrl||url}','_blank')">Open ↗</button>
      </div>
    </div>`:''}
  </div>`;
}
function siteCard(s){return siteRow(s);}

let cfState = { configured: false, zones: [], tunnels: {}, ncDomains: [], email: '', ncConfigured: false };
let cfDeletePending = null;
let cfNSPending = null;
let cfForceSetup = false;

// ─── Domains View — Namecheap + Cloudflare Tunnel ────────────────────────────
async function renderDomainsView(el) {
  if (cfForceSetup) { renderCFSetup(el); return; }
  // Only show loading spinner on very first load
  if (!cfState.configured) {
    el.innerHTML = `<div style="color:var(--text3);padding:40px;text-align:center"><span class="spin">⟳</span> Loading...</div>`;
  }
  const r = await fetch('/api/cf/zones').then(res=>res.json());
  if (r.error === 'not_configured') { renderCFSetup(el); return; }
  if (r.error) {
    el.innerHTML = `<div style="max-width:500px">
      <div style="background:var(--red-bg);border:1px solid var(--red-bd);color:var(--red);border-radius:8px;padding:12px 14px;font-size:12px;margin-bottom:12px">✕ ${esc(r.error)}</div>
      <button class="btn btn-outline" onclick="cfForceSetup=true;renderDomainsView(document.getElementById('content'))">⚙ Reconfigure Cloudflare</button>
    </div>`;
    return;
  }
  cfState.zones        = r.zones       || [];
  cfState.tunnels      = Object.assign(cfState.tunnels||{}, r.tunnels||{});
  cfState.ncDomains    = r.nc_domains  || [];
  cfState.ncConfigured = r.nc_configured || false;
  cfState.email        = r.email       || '';
  cfState.configured   = true;
  renderCFDashboard(el);
  // Wire up static buttons with addEventListener (more reliable than inline onclick in innerHTML)
  setTimeout(() => {
    const settingsBtn = document.getElementById('cf-settings-btn');
    if (settingsBtn) settingsBtn.addEventListener('click', () => openCFSettings());
  }, 0);
}

function renderCFSetup(el) {
  el.innerHTML = `<div style="max-width:480px">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:18px">
      <span style="font-size:32px">☁</span>
      <div><div style="font-size:16px;font-weight:600">Connect Cloudflare</div>
      <div style="font-size:12px;color:var(--text3);margin-top:2px">Tunnel your PHP sites through Cloudflare — works with T-Mobile 5G, no port forwarding</div></div>
    </div>
    <div style="background:var(--bg3);border:1px solid var(--border);border-radius:8px;padding:14px;font-size:12px;color:var(--text2);line-height:2;margin-bottom:16px">
      <strong>Get your API token:</strong><br>
      1. <a href="https://dash.cloudflare.com/profile/api-tokens" target="_blank" style="color:#f6821f">Cloudflare → Profile → API Tokens → Create Token</a><br>
      2. Use <strong>"Edit zone DNS"</strong> template<br>
      3. Add permission: <strong>Account → Cloudflare Tunnel → Edit</strong><br>
      4. Copy and paste below
    </div>
    <div class="form-group">
      <label class="form-label">Cloudflare API Token</label>
      <input class="form-input" id="cf-setup-token" type="password" placeholder="Paste token here"/>
    </div>
    <div id="cf-setup-err" style="font-size:12px;color:var(--red);min-height:18px;margin-bottom:8px"></div>
    <button class="btn btn-primary btn-lg" id="cf-setup-btn" onclick="cfSetupSave()"
      style="width:100%;background:#f6821f;border-color:#f6821f">☁ Connect Cloudflare</button>
  </div>`;
}

async function cfSetupSave() {
  const token = document.getElementById('cf-setup-token')?.value.trim();
  if (!token) { document.getElementById('cf-setup-err').textContent = 'Paste your token first'; return; }
  const btn = document.getElementById('cf-setup-btn');
  const errEl = document.getElementById('cf-setup-err');
  btn.disabled = true; btn.innerHTML = '<span class="spin">⟳</span> Verifying...';
  errEl.textContent = '';
  const r = await apiFetch('/api/cf/save-token', {token});
  btn.disabled = false; btn.innerHTML = '☁ Connect Cloudflare';
  if (r.ok) {
    cfForceSetup = false;
    notify(`✓ Cloudflare connected! Found ${r.zone_count||0} zone(s).`, 'ok');
    renderDomainsView(document.getElementById('content'));
  } else {
    errEl.innerHTML = `<span style="color:var(--red)">✕ ${esc(r.error||'Connection failed')}</span>
      <br><span style="color:var(--text3);font-size:10px;margin-top:4px;display:block">
      Make sure your token has: Zone:Read + Zone:DNS:Edit + Account:Cloudflare Tunnel:Edit permissions.
      <a href="https://dash.cloudflare.com/profile/api-tokens" target="_blank" style="color:#f6821f">Check token ↗</a>
      </span>`;
  }
}

// ─── Main Dashboard ───────────────────────────────────────────────────────────
function renderCFDashboard(el) {
  const activeTunnels = Object.values(cfState.tunnels).filter(t => t.container_status === 'running').length;
  // Build unified domain list: start with NC domains, enrich with CF zone + tunnel data
  const cfZoneMap = {};
  cfState.zones.forEach(z => { cfZoneMap[z.name] = z; });

  // All NC domains + any CF-only zones not in NC
  const allDomains = [];
  const ncNames = new Set();
  cfState.ncDomains.forEach(d => { ncNames.add(d.name); allDomains.push({...d, source:'nc'}); });
  cfState.zones.forEach(z => { if (!ncNames.has(z.name)) allDomains.push({name:z.name, in_cf:true, source:'cf'}); });

  const inCF    = allDomains.filter(d => d.in_cf);
  const notInCF = allDomains.filter(d => !d.in_cf);

  el.innerHTML = `<div style="max-width:800px">
    <!-- Header -->
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;flex-wrap:wrap;gap:8px">
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
        <div style="display:flex;align-items:center;gap:7px">
          <span class="port-led led-green"></span>
          <span style="font-size:13px;font-weight:500">☁ ${esc(cfState.email||'Cloudflare')}</span>
          <span class="tag tag-ghost">${cfState.zones.length} CF zones</span>
          <span class="tag ${activeTunnels?'tag-green':'tag-ghost'}">${activeTunnels} tunnel${activeTunnels!==1?'s':''} active</span>
        </div>
        ${cfState.ncConfigured ? `<div style="display:flex;align-items:center;gap:7px">
          <span class="port-led led-green"></span>
          <span style="font-size:11px;color:var(--text3)">Namecheap connected</span>
          <span class="tag tag-ghost">${cfState.ncDomains.length} domains</span>
        </div>` : `<span class="tag tag-yellow" style="font-size:11px" title="Add Namecheap credentials in Settings to auto-update nameservers">⚠ Namecheap not connected</span>`}
      </div>
      <div style="display:flex;gap:7px">
        <button class="btn btn-outline" onclick="viewRendered=false;renderDomainsView(document.getElementById('content'))">↺ Refresh</button>
        <button class="btn btn-outline" style="color:#f6821f;border-color:#f6821f55" id="cf-settings-btn">⚙ Settings</button>
      </div>
    </div>

    <!-- Domains NOT yet in Cloudflare -->
    ${notInCF.length ? `
    <div style="background:var(--surface);border:1px solid var(--line);border-radius:var(--r-md);padding:12px 14px;margin-bottom:14px">
      <div style="font-family:var(--mono);font-size:9px;font-weight:500;letter-spacing:1.2px;text-transform:uppercase;color:var(--t3);margin-bottom:10px">
        ${notInCF.length} domain${notInCF.length!==1?'s':''} not in Cloudflare
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:6px">
        ${notInCF.map(d => `
          <div style="display:flex;align-items:center;gap:8px;background:var(--surface);border:1px solid var(--line2);border-radius:var(--r-md);padding:6px 10px">
            <span style="font-size:12px;font-family:var(--mono);color:var(--t2)">${esc(d.name)}</span>
            <button class="btn btn-sm" style="padding:3px 8px;font-size:11px;color:var(--t3);border-color:var(--line2);font-family:var(--mono)"
              onmouseover="this.style.color='var(--t1)';this.style.background='var(--raised)'"
              onmouseout="this.style.color='var(--t3)';this.style.background='transparent'"
              onclick="cfAddDomainToCloudflare('${esc(d.name)}')">+ Add</button>
          </div>`).join('')}
      </div>
    </div>` : ''}

    <!-- Domains in Cloudflare -->
    ${inCF.length === 0
      ? `<div style="color:var(--text3);text-align:center;padding:40px">
          No domains in Cloudflare yet.<br>
          <a href="https://dash.cloudflare.com" target="_blank" style="color:#f6821f;font-size:12px;margin-top:8px;display:inline-block">Add a domain to Cloudflare ↗</a>
         </div>`
      : `<div style="font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:var(--text3);margin-bottom:8px;display:flex;align-items:center;gap:8px">In Cloudflare <span style="height:1px;flex:1;background:var(--border);display:inline-block"></span></div>
         ${inCF.map(d => { const z = cfZoneMap[d.name]; return cfDomainRow(d, z); }).join('')}`}
  </div>`;
}

function cfDomainRow(d, zone) {
  const tunnels = Object.values(cfState.tunnels);
  const tunnel  = tunnels.find(t => t.domain === d.name || t.domain?.endsWith('.'+d.name));
  // container_status comes from /api/cf/tunnels, tunnel_status from zone enrichment — check both
  const tStatus = tunnel?.container_status || tunnel?.tunnel_status || (tunnel ? 'stopped' : null);
  const running = tStatus === 'running';
  const site    = tunnel ? state.sites[tunnel.site_id] : null;
  const zoneStatus = zone?.active ? '' : '<span class="tag tag-yellow" style="font-size:10px">Pending NS</span>';

  return `<div style="background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);margin-bottom:6px;transition:border-color .12s"
    onmouseover="this.style.borderColor='var(--border2)'" onmouseout="this.style.borderColor='var(--border)'">
    <div style="display:flex;align-items:center;gap:12px;padding:12px 16px;flex-wrap:wrap">
      <span class="port-led ${running?'led-green':tunnel?'led-red':'led-yellow'}"
        title="${running?'Tunnel running':tunnel?'Tunnel stopped':'No tunnel'}"></span>
      <div style="flex:1;min-width:160px">
        <div style="font-size:13px;font-weight:500;display:flex;align-items:center;gap:8px">
          ${esc(d.name)} ${zoneStatus}
        </div>
        <div style="font-size:11px;color:var(--text3);margin-top:2px">
          ${running
            ? `<span style="color:var(--green)">🚀 Live → <strong>${esc(site?.display||site?.name||'')}</strong> &nbsp;·&nbsp; <a href="https://${esc(d.name)}" target="_blank" style="color:var(--green)">https://${esc(d.name)} ↗</a></span>`
            : tunnel
              ? `<span style="color:var(--red)">⏹ Tunnel stopped → ${esc(site?.display||site?.name||'')}</span>`
              : `<span>In Cloudflare — ready to tunnel</span>`}
        </div>
      </div>
      <div style="display:flex;gap:6px;flex-shrink:0;flex-wrap:wrap">
        ${running ? `
          <button class="btn btn-outline btn-sm" onclick="window.open('https://${esc(d.name)}','_blank')">🔗 Open</button>
          <button class="btn btn-outline btn-sm" onclick="cfRepairTunnel('${esc(tunnel.site_id)}')">🔧 Fix</button>
          <button class="btn btn-stop btn-sm" onclick="cfTunnelStop('${esc(tunnel.site_id)}')">⏹ Stop</button>
          <button class="btn btn-danger btn-sm" onclick="cfTunnelDelete('${esc(tunnel.site_id)}','${esc(d.name)}')">🗑</button>
        ` : tunnel ? `
          <button class="btn btn-start btn-sm" onclick="cfTunnelStart('${esc(tunnel.site_id)}')">▶ Start</button>
          <button class="btn btn-outline btn-sm" onclick="cfRepairTunnel('${esc(tunnel.site_id)}')">🔧 Fix Ingress</button>
          <button class="btn btn-outline btn-sm" onclick="cfShowLogs('${esc(tunnel.site_id)}')">📋 Logs</button>
          <button class="btn btn-danger btn-sm" onclick="cfTunnelDelete('${esc(tunnel.site_id)}','${esc(d.name)}')">🗑 Delete</button>
        ` : `
          <button class="btn btn-outline btn-sm"
            onclick="openCFTunnelConnect('${esc(d.name)}')">☁ Tunnel Site</button>
        `}
      </div>
    </div>
  </div>`;
}

// ─── Add Namecheap domain to Cloudflare (+ auto-update NS) ───────────────────
async function cfAddDomainToCloudflare(domain) {
  notify(`Adding ${domain} to Cloudflare...`, 'info');
  const r = await apiFetch('/api/cf/setup-domain', {domain});
  if (r.ok) {
    const ns = r.nameservers || [];
    if (cfState.ncConfigured) {
      notify(`✓ ${domain} added — Namecheap NS updated! Propagation: 5–30 min.`, 'ok');
    } else {
      cfNSPending = {domain, ns};
      document.getElementById('cf-ns-domain').textContent = domain;
      document.getElementById('cf-ns-list').innerHTML = ns.map(n => `<div style="font-family:var(--mono);font-size:12px;padding:3px 0">${n}</div>`).join('');
      document.getElementById('cf-ns-modal').style.display = 'flex';
    }
    renderDomainsView(document.getElementById('content'));
  } else {
    notify('Error: '+(r.error||'?'), 'err');
  }
}

// ─── Tunnel actions ───────────────────────────────────────────────────────────
let cfConnectDomain = null;

function openCFTunnelConnect(domain) {
  cfConnectDomain = domain;
  const sites = Object.values(state.sites);
  ['cf-connect-domain-label','cf-connect-domain-label2'].forEach(id => {
    const el = document.getElementById(id); if (el) el.textContent = domain;
  });
  const sel = document.getElementById('cf-connect-site-sel');
  sel.innerHTML = '<option value="">— select a PHP site —</option>' +
    sites.map(s => `<option value="${s.id}">${esc(s.display||s.name)} [:${s.port}] [${s.status}]</option>`).join('') +
    '<option value="__custom__">⚙ Custom URL (localhost:port)...</option>';
  sel.value = '';
  document.getElementById('cf-connect-custom-row').style.display = 'none';
  document.getElementById('cf-connect-custom-url').value = '';
  document.getElementById('cf-connect-status').textContent = '';
  document.getElementById('cf-connect-btn').disabled = false;
  document.getElementById('cf-connect-btn').textContent = '☁ Tunnel Now';
  sel.value = '';
  document.getElementById('cf-connect-status').textContent = '';
  const btn = document.getElementById('cf-connect-btn');
  btn.disabled = false; btn.textContent = '☁ Tunnel Now';
  document.getElementById('cf-connect-modal').style.display = 'flex';
}

function closeCFConnect() {
  document.getElementById('cf-connect-modal').style.display = 'none';
  cfConnectDomain = null;
}

function cfConnectSelChange() {
  const val = document.getElementById('cf-connect-site-sel').value;
  document.getElementById('cf-connect-custom-row').style.display =
    val === '__custom__' ? '' : 'none';
}

async function cfDoConnect() {
  const siteId = document.getElementById('cf-connect-site-sel').value;
  if (!siteId) { notify('Select a site or custom URL', 'err'); return; }

  let service, effectiveSiteId, label;
  if (siteId === '__custom__') {
    const customUrl = document.getElementById('cf-connect-custom-url').value.trim();
    if (!customUrl) { notify('Enter a service URL', 'err'); return; }
    service = customUrl.startsWith('http') ? customUrl : `http://${customUrl}`;
    service = service.replace(/\/+$/, ''); // strip trailing slashes
    effectiveSiteId = `custom_${cfConnectDomain.replace(/\./g,'_')}`;
    label = service;
  } else {
    const site = state.sites[siteId];
    if (!site) return;
    service = `http://localhost:${site.port}`;
    effectiveSiteId = siteId;
    label = site.display || site.name;
  }

  const btn    = document.getElementById('cf-connect-btn');
  const status = document.getElementById('cf-connect-status');
  btn.disabled = true;
  const steps = ['Getting account info...','Creating Named Tunnel...','Configuring DNS...','Starting cloudflared container...'];
  let i = 0;
  btn.innerHTML = '<span class="spin">⟳</span> ' + steps[0];
  const iv = setInterval(() => { i=Math.min(i+1,steps.length-1); btn.innerHTML='<span class="spin">⟳</span> '+steps[i]; }, 3500);
  const r = await apiFetch('/api/cf/tunnel/create', {site_id: effectiveSiteId, domain: cfConnectDomain, service});
  clearInterval(iv);
  if (r.ok) {
    if (r.dns_fix_needed) {
      closeCFConnect();
      renderDomainsView(document.getElementById('content'));
      setTimeout(() => showDNSPermissionError(r.dns_err, r.cname_target), 300);
    } else {
      notify(`✓ ${cfConnectDomain} → ${label} — live!`, 'ok');
      closeCFConnect();
      renderDomainsView(document.getElementById('content'));
    }
  } else if (r.dns_fix_needed) {
    btn.disabled=false; btn.innerHTML='☁ Tunnel Now';
    closeCFConnect();
    showDNSPermissionError(r.error, r.cname_target);
  } else {
    btn.disabled=false; btn.innerHTML='☁ Tunnel Now';
    status.innerHTML = `<span style="color:var(--red)">✕ ${esc(r.error||'Failed')}</span>`;
  }
}

async function cfTunnelStop(siteId) {
  notify('Stopping tunnel...', 'info');
  const r = await apiFetch('/api/cf/tunnel/stop', {site_id:siteId});
  if (r.ok) {
    notify(`✓ Tunnel stopped`, 'ok');
    if (cfState.tunnels[siteId]) cfState.tunnels[siteId].container_status = 'stopped';
    renderDomainsView(document.getElementById('content'));
  } else {
    notify('Stop failed: '+(r.error||'?'), 'err');
  }
}

async function cfTunnelStart(siteId) {
  notify('Starting tunnel...', 'info');
  const r = await apiFetch('/api/cf/tunnel/start', {site_id:siteId});
  if (r.ok) {
    notify('✓ Tunnel started', 'ok');
    renderDomainsView(document.getElementById('content'));
    // Re-poll after 5s to catch running status once container stabilises
    setTimeout(() => renderDomainsView(document.getElementById('content')), 5000);
  } else {
    notify('Error: '+(r.error||'?'), 'err');
  }
}

async function cfRepairTunnel(siteId) {
  notify('Fixing tunnel ingress & DNS...', 'info');
  const r = await apiFetch('/api/cf/tunnel/repair', {site_id: siteId});
  if (r.ok) {
    notify('✓ Ingress fixed + DNS CNAME set! Starting tunnel...', 'ok');
    await new Promise(res => setTimeout(res, 400));
    await cfTunnelStart(siteId);
  } else if (r.dns_fix_needed) {
    // Show a prominent modal explaining the token permission issue
    showDNSPermissionError(r.error, r.cname_target);
  } else {
    notify('Error: '+(r.error||'?'), 'err');
  }
}

function showDNSPermissionError(errMsg, cnameTarget) {
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.75);z-index:600;display:flex;align-items:center;justify-content:center;padding:20px';
  overlay.innerHTML = `<div style="background:var(--bg2);border:1px solid var(--border2);border-radius:12px;width:520px;box-shadow:0 24px 60px rgba(0,0,0,.6)">
    <div style="display:flex;align-items:center;gap:10px;padding:16px 18px;border-bottom:1px solid var(--border)">
      <span style="font-size:20px">⚠️</span>
      <span style="font-size:13px;font-weight:600">DNS Permission Missing</span>
      <button class="modal-close" style="margin-left:auto" onclick="this.closest('[style*=fixed]').remove()">×</button>
    </div>
    <div style="padding:16px 18px">
      <div style="background:var(--yel-bg);border:1px solid var(--yel-bd);border-radius:7px;padding:12px 14px;font-size:12px;color:var(--yellow);margin-bottom:14px;line-height:1.8">
        Your Cloudflare token is <strong>missing Zone → DNS → Edit</strong> permission.<br>
        Without it, PHP-MNGR cannot automatically create the DNS record that makes your domain work.
      </div>
      <div style="font-size:12px;color:var(--text2);line-height:2;margin-bottom:14px">
        <strong>Fix in 2 minutes:</strong><br>
        1. <a href="https://dash.cloudflare.com/profile/api-tokens" target="_blank" style="color:#f6821f">Open Cloudflare API Tokens ↗</a><br>
        2. Edit your token → Add permission: <strong>Zone → DNS → Edit</strong><br>
        3. Save → copy the token<br>
        4. PHP-MNGR → <button class="btn btn-outline btn-sm" onclick="this.closest('[style*=fixed]').remove();openCFSettings('cf')">⚙ Settings → Cloudflare</button> → paste & save<br>
        5. Click <strong>🔧 Fix Ingress</strong> again — it will be fully automatic
      </div>
      ${cnameTarget ? `<div style="background:var(--bg3);border:1px solid var(--border);border-radius:7px;padding:10px 14px;font-size:12px;margin-bottom:14px">
        <div style="color:var(--text3);font-size:10px;text-transform:uppercase;letter-spacing:.6px;margin-bottom:6px">Or add manually in Cloudflare DNS:</div>
        <div style="font-family:var(--mono);color:var(--text2);line-height:1.9">
          Type: <strong>CNAME</strong><br>
          Name: <strong>@</strong><br>
          Target: <span style="color:#f6821f">${esc(cnameTarget)}</span><br>
          Proxy: <strong>ON</strong> (orange cloud)
        </div>
        <button class="copy-btn" style="margin-top:6px" onclick="copy('${esc(cnameTarget)}')">Copy target</button>
      </div>` : ''}
    </div>
  </div>`;
  overlay.onclick = e => { if(e.target===overlay) overlay.remove(); };
  document.body.appendChild(overlay);
}

async function cfTunnelDelete(siteId, domain) {
  cfDeletePending = {siteId, domain};
  document.getElementById('cf-delete-msg').textContent = `Delete tunnel for ${domain}? This stops the container and removes it from Cloudflare.`;
  document.getElementById('cf-delete-modal').style.display = 'flex';
}

async function cfTunnelDeleteConfirm() {
  document.getElementById('cf-delete-modal').style.display = 'none';
  const {siteId} = cfDeletePending || {};
  if (!siteId) return;
  const r = await apiFetch('/api/cf/tunnel/delete', {site_id:siteId});
  if (r.ok) {
    notify('Tunnel deleted', 'ok');
    delete cfState.tunnels[siteId];
    renderDomainsView(document.getElementById('content'));
  } else notify('Error: '+(r.error||'?'), 'err');
}

async function cfShowLogs(siteId) {
  const r = await fetch(`/api/cf/tunnel/logs?sid=${siteId}`).then(res=>res.json());
  const logs = r.logs || r.error || 'No logs available';
  // Show in a simple overlay
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.8);z-index:600;display:flex;align-items:center;justify-content:center;padding:20px';
  overlay.innerHTML = `<div style="background:var(--bg);border:1px solid var(--border2);border-radius:10px;width:700px;max-height:80vh;display:flex;flex-direction:column">
    <div style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--border)">
      <span style="font-size:13px;font-weight:600">📋 Tunnel Logs</span>
      <button class="modal-close" onclick="this.closest('[style*=fixed]').remove()">×</button>
    </div>
    <pre style="flex:1;overflow-y:auto;padding:14px;font-family:var(--mono);font-size:11px;color:var(--text2);line-height:1.6;white-space:pre-wrap;word-break:break-all">${esc(logs)}</pre>
  </div>`;
  overlay.onclick = e => { if(e.target===overlay) overlay.remove(); };
  document.body.appendChild(overlay);
}

// ─── Combined API Settings Modal ─────────────────────────────────────────────
async function openCFSettings(tab) {
  document.getElementById('cf-settings-modal').style.display = 'flex';
  switchSettingsTab(tab || 'cf');
  // Pre-populate CF status
  const cfr = await fetch('/api/cf/config').then(r=>r.json());
  const bar = document.getElementById('cf-settings-status-bar');
  if (bar) bar.innerHTML = cfr.has_token
    ? `<div style="display:flex;align-items:center;gap:8px;background:#10a37f12;border:1px solid #10a37f44;border-radius:7px;padding:8px 12px;font-size:12px;margin-bottom:12px"><span class="port-led led-green"></span>Cloudflare connected — paste new token to replace.</div>`
    : '';
  // Pre-populate NC fields
  const ncr = await fetch('/api/nc/config').then(r=>r.json());
  const ncBar = document.getElementById('nc-settings-status-bar');
  if (ncBar) ncBar.innerHTML = ncr.has_key
    ? `<div style="display:flex;align-items:center;gap:8px;background:#10a37f12;border:1px solid #10a37f44;border-radius:7px;padding:8px 12px;font-size:12px;margin-bottom:12px"><span class="port-led led-green"></span>Namecheap connected.</div>`
    : '';
  const nu = document.getElementById('nc-settings-username');
  const ni = document.getElementById('nc-settings-ip');
  if (nu && ncr.username) nu.value = ncr.username;
  if (ni && ncr.client_ip) ni.value = ncr.client_ip;
  if (ni && !ni.value && ncr.server_ip) ni.placeholder = ncr.server_ip;
}

function switchSettingsTab(tab) {
  const cfBody = document.getElementById('stab-cf-body');
  const ncBody = document.getElementById('stab-nc-body');
  const cfTab  = document.getElementById('stab-cf');
  const ncTab  = document.getElementById('stab-nc');
  if (tab === 'cf') {
    if (cfBody) cfBody.style.display = '';
    if (ncBody) ncBody.style.display = 'none';
    if (cfTab)  { cfTab.style.background='var(--bg3)'; cfTab.style.borderBottomColor='#f6821f'; cfTab.style.color='var(--text)'; }
    if (ncTab)  { ncTab.style.background='transparent'; ncTab.style.borderBottomColor='transparent'; ncTab.style.color='var(--text2)'; }
  } else {
    if (cfBody) cfBody.style.display = 'none';
    if (ncBody) ncBody.style.display = '';
    if (ncTab)  { ncTab.style.background='var(--bg3)'; ncTab.style.borderBottomColor='var(--green)'; ncTab.style.color='var(--text)'; }
    if (cfTab)  { cfTab.style.background='transparent'; cfTab.style.borderBottomColor='transparent'; cfTab.style.color='var(--text2)'; }
  }
}

function closeCFSettings() {
  document.getElementById('cf-settings-modal').style.display = 'none';
}

function cfToggleTokenVisible() {
  const inp = document.getElementById('cf-settings-token');
  if (inp) inp.type = inp.type === 'password' ? 'text' : 'password';
}

async function cfSettingsSave() {
  const token = document.getElementById('cf-settings-token')?.value.trim();
  if (!token) { notify('Paste a token first', 'err'); return; }
  const btn = document.getElementById('cf-settings-save-btn');
  const res = document.getElementById('cf-settings-result');
  btn.disabled = true; btn.innerHTML = '<span class="spin">⟳</span> Verifying...';
  const r = await apiFetch('/api/cf/save-token', {token});
  btn.disabled = false; btn.innerHTML = 'Save &amp; Verify';
  if (r.ok) {
    if (res) res.innerHTML = `<span style="color:var(--green)">✓ Connected! Found ${r.zone_count||0} zone(s).</span>`;
    cfState.configured = true;
    setTimeout(() => { closeCFSettings(); renderDomainsView(document.getElementById('content')); }, 1200);
  } else {
    if (res) res.innerHTML = `<span style="color:var(--red)">✕ ${esc(r.error||'Failed')}</span><br><span style="font-size:11px;color:var(--text3)">Check: Zone→DNS→Edit + Account→Cloudflare Tunnel→Edit permissions</span>`;
  }
}

async function ncSettingsSave() {
  const username  = document.getElementById('nc-settings-username')?.value.trim();
  const api_key   = document.getElementById('nc-settings-apikey')?.value.trim();
  const client_ip = document.getElementById('nc-settings-ip')?.value.trim();
  if (!username) { notify('Username required', 'err'); return; }
  if (!api_key)  { notify('API key required', 'err'); return; }
  const btn = document.getElementById('nc-settings-save-btn');
  const res = document.getElementById('nc-settings-result');
  btn.disabled = true; btn.innerHTML = '<span class="spin">⟳</span> Saving & verifying...';
  const r = await apiFetch('/api/nc/config', {username, api_key, client_ip});
  btn.disabled = false; btn.innerHTML = 'Save Namecheap';
  if (r.ok) {
    const msg = r.domain_count !== undefined
      ? `✓ Connected! Found ${r.domain_count} domain(s).` + (r.warning ? ` (warning: ${r.warning})` : '')
      : '✓ Saved.';
    if (res) res.innerHTML = `<span style="color:var(--green)">${esc(msg)}</span>`;
    notify(msg, 'ok');
    setTimeout(() => { closeCFSettings(); renderDomainsView(document.getElementById('content')); }, 1500);
  } else {
    if (res) res.innerHTML = `<span style="color:var(--red)">✕ ${esc(r.error||'Failed')}</span>`;
  }
}

function ncShowSettings() {


  cfForceSetup = true;
  renderDomainsView(document.getElementById('content'));
}
async function openDrawer(sid) {
  drawerSid = sid;
  const s = state.sites[sid];
  if (!s) return;
  document.getElementById('drawer-title').textContent = s.display || s.name;
  renderDrawerBody(s);
  document.getElementById('drawer-bg').classList.add('open');
  document.getElementById('drawer').classList.add('open');
  // Fetch fresh tunnel status in background and re-render drawer
  try {
    const tr = await fetch('/api/cf/tunnels');
    const td = await tr.json();
    if (td.tunnels) {
      cfState.tunnels = td.tunnels;
      if (drawerSid === sid) renderDrawerBody(s); // re-render with fresh status
    }
  } catch(e) {}
}

function closeDrawer() {
  document.getElementById('drawer-bg').classList.remove('open');
  document.getElementById('drawer').classList.remove('open');
  drawerSid = null;
}

function renderDrawerBody(s) {
  const running = s.status === 'running';
  // Find tunnel for this site
  const tunnel = Object.values(cfState?.tunnels||{}).find(t => t.site_id === s.id);
  const tunnelRunning = tunnel?.container_status === 'running' || tunnel?.tunnel_status === 'running';
  const publicUrl = tunnelRunning ? `https://${tunnel.domain}` : null;
  const localUrl  = `http://localhost:${s.port}`;

  document.getElementById('drawer-body').innerHTML = `
    <!-- Connection -->
    <div class="dsec">
      <div class="dsec-title">Connection</div>
      <div class="drow"><span class="dlabel">Local</span>
        <span class="dval" style="font-family:var(--mono)">${localUrl}</span>
        <button class="copy-btn" onclick="copy('${localUrl}')">Copy</button>
      </div>
      ${publicUrl ? `<div class="drow"><span class="dlabel">Public</span>
        <span class="dval"><a href="${esc(publicUrl)}" target="_blank" style="color:var(--green)">${esc(publicUrl)} ↗</a></span>
        <button class="copy-btn" onclick="copy('${esc(publicUrl)}')">Copy</button>
      </div>` : ''}
      <div class="drow"><span class="dlabel">Container</span>
        <span class="dval" style="font-family:var(--mono)">${esc(s.name)}</span>
        <button class="copy-btn" onclick="copy('${s.name}')">Copy</button>
      </div>
    </div>

    <!-- Cloudflare Tunnel -->
    <div class="dsec">
      <div class="dsec-title">☁ Cloudflare Tunnel</div>
      ${tunnel ? `
        <div style="background:${tunnelRunning ? '#10a37f12' : 'var(--bg3)'};border:1px solid ${tunnelRunning ? '#10a37f44' : 'var(--border)'};border-radius:7px;padding:10px 12px;margin-bottom:10px">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
            <span class="port-led ${tunnelRunning ? 'led-green' : 'led-red'}"></span>
            <span style="font-size:12px;font-weight:500">${tunnelRunning ? '🚀 Live' : '⏹ Stopped'}</span>
            <span style="font-size:11px;font-family:var(--mono);color:var(--text3)">${esc(tunnel.domain||'')}</span>
          </div>
          ${tunnelRunning ? `<div style="font-size:11px;color:var(--text3)">SSL handled by Cloudflare · HTTP/2 · DDoS protection</div>` : ''}
        </div>
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          ${tunnelRunning
            ? `<button class="btn btn-outline btn-sm" onclick="window.open('https://${esc(tunnel.domain)}','_blank')">🔗 Open Site</button>
               <button class="btn btn-stop btn-sm" onclick="cfTunnelStop('${s.id}');closeDrawer()">⏹ Stop Tunnel</button>`
            : `<button class="btn btn-start btn-sm" onclick="closeDrawer();cfTunnelStart('${s.id}')">▶ Start Tunnel</button>`}
          <button class="btn btn-outline btn-sm" onclick="closeDrawer();showView('domains')">⚙ Manage</button>
        </div>
      ` : `
        <div style="font-size:12px;color:var(--text3);margin-bottom:10px;line-height:1.7">
          No tunnel configured. Connect a domain to make this site publicly accessible via Cloudflare.
        </div>
        <button class="btn btn-primary btn-sm" style="background:#f6821f;border-color:#f6821f"
          onclick="closeDrawer();showView('domains')">☁ Set Up Tunnel → Domains</button>
      `}
    </div>

    <!-- PHP Info -->
    <div class="dsec">
      <div class="dsec-title">PHP Config</div>
      <div class="config-info">
        <div style="margin-bottom:6px">Version: <span class="tag tag-yellow" style="font-size:11px">PHP ${s.php_version}</span></div>
        <div style="font-size:11px;color:var(--text3)">~/.phpmngr/sites/${s.id}/php.ini</div>
        <div style="font-size:11px;color:var(--text3)">Created: ${s.created}</div>
      </div>
      <button class="btn btn-outline btn-sm" style="margin-top:10px" onclick="openFM('${s.id}');closeDrawer()">📁 File Manager</button>
    </div>

    <!-- Actions -->
    <div class="dsec">
      <div class="dsec-title">Actions</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        ${running
          ? `<button class="btn btn-stop" onclick="stopSite('${s.id}')">Stop</button>
             <button class="btn btn-outline" onclick="window.open('${localUrl}','_blank')">Open Local ↗</button>`
          : `<button class="btn btn-start" onclick="startSite('${s.id}')">Start</button>`}
        <button class="btn btn-danger" onclick="openDeleteSiteModal('${s.id}','${esc(s.name)}')">Delete Site</button>
      </div>
    </div>`;
}

// ─────────────────────────────── Site CRUD ────────────────────────────────────
let selectedMode = 'local';
function selectMode(m) {
  selectedMode = m;
  document.getElementById('mode-local').classList.toggle('selected', m==='local');
  document.getElementById('mode-public').classList.toggle('selected', m==='public');
  document.getElementById('email-group').classList.toggle('hidden', m!=='public');
}

function openCreateModal() {
  document.getElementById('create-modal-bg').classList.remove('hidden');
  document.getElementById('new-name').focus();
}
function closeCreateModal() {
  document.getElementById('create-modal-bg').classList.add('hidden');
}

async function createSite() {
  const name    = document.getElementById('new-name').value.trim();
  const display = document.getElementById('new-display').value.trim() || name;
  const phpver  = document.getElementById('new-phpver').value;
  const domain  = document.getElementById('new-domain').value.trim();
  const email   = document.getElementById('new-email').value.trim();
  if (!name) { notify('Site name required', 'err'); return; }

  const btn = document.getElementById('create-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spin">⟳</span> Creating...';
  notify('Pulling PHP image & creating container...', 'info');

  const r = await apiFetch('/api/sites/create', {name, display, php_version:phpver, mode:selectedMode, domain, email});
  btn.disabled = false;
  btn.innerHTML = 'Create Site';

  if (r.ok) {
    closeCreateModal();
    ['new-name','new-display','new-domain','new-email'].forEach(id => document.getElementById(id).value='');
    notify(`✓ Site "${display}" created!`, 'ok');
    await poll();
  } else {
    notify('Error: ' + (r.error||'Unknown'), 'err');
  }
}

async function startSite(sid) {
  notify('Starting...', 'info');
  const r = await apiFetch('/api/sites/start', {id:sid});
  if (r.ok) { notify('✓ Site started', 'ok'); await poll(); if (drawerSid===sid) openDrawer(sid); }
  else notify('Error: '+(r.error||'?'), 'err');
}

async function stopSite(sid) {
  const r = await apiFetch('/api/sites/stop', {id:sid});
  if (r.ok) { notify('Site stopped', 'ok'); await poll(); if (drawerSid===sid) openDrawer(sid); }
  else notify('Error: '+(r.error||'?'), 'err');
}

let deleteSitePending = null;

function openDeleteSiteModal(sid, name) {
  deleteSitePending = sid;
  document.getElementById('delete-site-msg').textContent = `Delete "${name}"? This removes the container and all site files. Cannot be undone.`;
  document.getElementById('delete-site-modal').style.display = 'flex';
}

async function deleteSiteConfirm() {
  document.getElementById('delete-site-modal').style.display = 'none';
  if (!deleteSitePending) return;
  await deleteSite(deleteSitePending);
  deleteSitePending = null;
}

async function deleteSite(sid) {
  const r = await apiFetch('/api/sites/delete', {id:sid});
  if (r.ok) { closeDrawer(); notify('Site deleted', 'ok'); await poll(); }
  else notify('Error: '+(r.error||'?'), 'err');
}

async function saveDomain(sid) {
  const domain = document.getElementById('drawer-domain').value.trim();
  const mode   = document.getElementById('drawer-mode').value;
  const r = await apiFetch('/api/sites/domain', {id:sid, domain, mode});
  if (r.ok) { notify('✓ Domain saved & proxy reloaded', 'ok'); await poll(); openDrawer(sid); }
  else notify('Error: '+(r.error||'?'), 'err');
}

async function restartProxy() {
  notify('Restarting proxy...', 'info');
  const r = await apiFetch('/api/proxy/restart', {});
  if (r.ok) { notify('✓ Proxy restarted', 'ok'); await poll(); }
  else notify('Failed to restart proxy', 'err');
}

// ─────────────────────────────── SSL ──────────────────────────────────────────
async function issueSSL(sid, type) {
  if (type === 'letsencrypt') {
    const s = state.sites[sid];
    if (!s.domain) { notify('Domain required for Let\'s Encrypt', 'err'); return; }
    const email = prompt(`Email for Let's Encrypt (site: ${s.domain}):`, s.email||'');
    if (!email) return;
    notify('Running certbot... this may take ~30s', 'info');
    const r = await apiFetch('/api/sites/ssl/letsencrypt', {id:sid, email});
    if (r.ok) { notify('✓ Let\'s Encrypt SSL issued!', 'ok'); await poll(); if (drawerSid===sid) openDrawer(sid); }
    else notify('Error: '+(r.error||'certbot failed'), 'err');
  } else {
    notify('Generating self-signed certificate...', 'info');
    const r = await apiFetch('/api/sites/ssl/selfsigned', {id:sid});
    if (r.ok) { notify('✓ Self-signed SSL issued', 'ok'); await poll(); if (drawerSid===sid) openDrawer(sid); }
    else notify('Error: '+(r.error||'?'), 'err');
  }
}

async function revokeSSL(sid) {
  if (!confirm('Revoke SSL certificate for this site?')) return;
  const r = await apiFetch('/api/sites/ssl/revoke', {id:sid});
  if (r.ok) { notify('SSL revoked', 'ok'); await poll(); if (drawerSid===sid) openDrawer(sid); }
  else notify('Error: '+(r.error||'?'), 'err');
}

// ─────────────────────────────── File Manager ─────────────────────────────────
// ─────────────────────────────── File Manager ────────────────────────────────
let fmMoveTarget = null;

function openFM(sid) {
  fmSid = sid;
  fmPath = '';
  fmSelection = [];
  const s = state.sites[sid];
  document.getElementById('fm-title').textContent = '📁 ' + (s ? (s.display||s.name) : '') + ' — Files';
  document.getElementById('fm-panel').classList.add('open');
  document.getElementById('fm-editor').classList.add('hidden');
  document.getElementById('fm-file-list').classList.remove('hidden');
  fmLoad('');
}

function closeFM() {
  document.getElementById('fm-panel').classList.remove('open');
  fmSid = null;
  fmSelection = [];
}

async function fmLoad(path) {
  fmPath = path;
  fmSelection = [];
  const r = await fetch(`/api/fm/list?sid=${fmSid}&path=${encodeURIComponent(path)}`);
  const d = await r.json();
  if (d.error) { notify(d.error, 'err'); return; }
  renderFMBreadcrumb(path);
  renderFMList(d.entries, path);
  updateFMToolbar();
}

function renderFMBreadcrumb(path) {
  const el = document.getElementById('fm-breadcrumb');
  const parts = path ? path.split('/').filter(Boolean) : [];
  let html = `<span class="fm-crumb" onclick="fmLoad('')">~</span>`;
  let cur = '';
  for (const p of parts) {
    cur += (cur ? '/' : '') + p;
    const cp = cur;
    html += `<span class="fm-crumb-sep">/</span><span class="fm-crumb" onclick="fmLoad('${cp}')">${esc(p)}</span>`;
  }
  el.innerHTML = html;
}

function renderFMList(entries, path) {
  const el = document.getElementById('fm-file-list');
  if (!entries.length) {
    el.innerHTML = `<div style="color:var(--text3);text-align:center;padding:40px;font-size:12px">Empty directory</div>`;
    return;
  }
  el.innerHTML = entries.map(e => {
    const icon = e.type === 'dir' ? '📁' : fileIcon(e.ext);
    const size = e.type === 'dir' ? '' : fmHumanSize(e.size);
    const date = new Date(e.modified*1000).toLocaleDateString();
    const fullPath = path ? path+'/'+e.name : e.name;
    const mode = e.mode || '---';
    return `<div class="fm-row" data-path="${esc(fullPath)}" data-type="${e.type}"
      onclick="fmRowClick(event,'${esc(fullPath)}','${e.type}')"
      ondblclick="fmRowDblClick('${esc(fullPath)}','${e.type}')">
      <span class="fm-icon">${icon}</span>
      <span class="fm-name">${esc(e.name)}</span>
      <span class="fm-size">${size}</span>
      <span class="fm-date">${date}</span>
      <span class="fm-mode" title="Click to chmod" onclick="openChmodModal(['${esc(fullPath)}'],event)">${mode}</span>
    </div>`;
  }).join('');
}

function fmRowClick(evt, path, type) {
  const row = evt.currentTarget;
  if (evt.ctrlKey || evt.metaKey) {
    // Multi-select toggle
    const idx = fmSelection.findIndex(s => s.path === path);
    if (idx >= 0) {
      fmSelection.splice(idx, 1);
      row.classList.remove('selected');
    } else {
      fmSelection.push({path, type});
      row.classList.add('selected');
    }
  } else {
    // Single select — clear others
    fmSelection = [{path, type}];
    document.querySelectorAll('.fm-row').forEach(r => r.classList.remove('selected'));
    row.classList.add('selected');
  }
  updateFMToolbar();
}

function fmRowDblClick(path, type) {
  if (type === 'dir') { fmSelection=[]; fmLoad(path); }
  else fmOpenEditor(path);
}

function updateFMToolbar() {
  const info    = document.getElementById('fm-selection-info');
  const renBtn  = document.getElementById('fm-rename-btn');
  const delBtn  = document.getElementById('fm-delete-btn');
  const extBtn  = document.getElementById('fm-extract-btn');
  const cmpBtn  = document.getElementById('fm-compress-btn');
  const movBtn  = document.getElementById('fm-move-btn');
  const dlBtn   = document.getElementById('fm-download-btn');
  const chmBtn  = document.getElementById('fm-chmod-btn');
  const n = fmSelection.length;
  const show = (...btns) => btns.forEach(b => b && (b.style.display = ''));
  const hide  = (...btns) => btns.forEach(b => b && (b.style.display = 'none'));
  if (n === 0) {
    info.textContent = fmPath || 'Web root /';
    hide(renBtn, delBtn, extBtn, cmpBtn, movBtn, dlBtn, chmBtn);
  } else if (n === 1) {
    info.textContent = fmSelection[0].path;
    show(delBtn, cmpBtn, movBtn, dlBtn, chmBtn, renBtn);
    fmSelection[0].path.toLowerCase().endsWith('.zip') ? show(extBtn) : hide(extBtn);
  } else {
    info.textContent = `${n} items selected`;
    show(delBtn, cmpBtn, movBtn, dlBtn, chmBtn);
    hide(renBtn, extBtn);
  }
}

async function fmOpenEditor(path) {
  const r = await fetch(`/api/fm/read?sid=${fmSid}&path=${encodeURIComponent(path)}`);
  const d = await r.json();
  if (d.error) { notify(d.error, 'err'); return; }
  document.getElementById('fm-editor-path').textContent = path;
  document.getElementById('fm-editor-area').value = d.content;
  document.getElementById('fm-editor').setAttribute('data-path', path);
  document.getElementById('fm-file-list').classList.add('hidden');
  document.getElementById('fm-editor').classList.remove('hidden');
  document.getElementById('fm-editor-area').focus();
}

function closeFMEditor() {
  document.getElementById('fm-editor').classList.add('hidden');
  document.getElementById('fm-file-list').classList.remove('hidden');
}

async function fmSaveFile() {
  const path    = document.getElementById('fm-editor').getAttribute('data-path');
  const content = document.getElementById('fm-editor-area').value;
  const r = await apiFetch('/api/fm/write', {sid:fmSid, path, content});
  if (r.ok) notify('✓ File saved', 'ok');
  else notify('Error: '+(r.error||'?'), 'err');
}

async function fmDeleteSelected() {
  if (!fmSelection.length) return;
  const n = fmSelection.length;
  const name = fmSelection[0].path.split('/').pop();
  document.getElementById('fm-delete-msg').textContent =
    n > 1 ? `Delete ${n} selected items? This cannot be undone.` : `Delete "${name}"? This cannot be undone.`;
  document.getElementById('fm-delete-modal').style.display = 'flex';
}

async function fmDeleteConfirm() {
  document.getElementById('fm-delete-modal').style.display = 'none';
  let failed = 0;
  for (const sel of fmSelection) {
    const r = await apiFetch('/api/fm/delete', {sid:fmSid, path:sel.path});
    if (!r.ok) failed++;
  }
  fmSelection = [];
  if (failed) notify(`${failed} item(s) failed to delete`, 'err');
  else notify('✓ Deleted', 'ok');
  fmLoad(fmPath);
}

async function fmNewFile() {
  const input = document.getElementById('fm-newfile-input');
  input.value = '';
  document.getElementById('fm-newfile-modal').style.display = 'flex';
  setTimeout(() => input.focus(), 50);
}

async function fmNewFileConfirm() {
  const name = document.getElementById('fm-newfile-input').value.trim();
  document.getElementById('fm-newfile-modal').style.display = 'none';
  if (!name) return;
  const path = fmPath ? fmPath+'/'+name : name;
  const r = await apiFetch('/api/fm/write', {sid:fmSid, path, content:''});
  if (r.ok) { notify('✓ Created', 'ok'); fmLoad(fmPath); fmOpenEditor(path); }
  else notify('Error: '+(r.error||'?'), 'err');
}

async function fmNewFolder() {
  const input = document.getElementById('fm-newfolder-input');
  input.value = '';
  document.getElementById('fm-newfolder-modal').style.display = 'flex';
  setTimeout(() => input.focus(), 50);
}

async function fmNewFolderConfirm() {
  const name = document.getElementById('fm-newfolder-input').value.trim();
  document.getElementById('fm-newfolder-modal').style.display = 'none';
  if (!name) return;
  const path = fmPath ? fmPath+'/'+name : name;
  const r = await apiFetch('/api/fm/mkdir', {sid:fmSid, path});
  if (r.ok) { notify('✓ Folder created', 'ok'); fmLoad(fmPath); }
  else notify('Error: '+(r.error||'?'), 'err');
}

async function fmRename() {
  if (!fmSelection.length) return;
  const oldName = fmSelection[0].path.split('/').pop();
  const input = document.getElementById('fm-rename-input');
  input.value = oldName;
  document.getElementById('fm-rename-modal').style.display = 'flex';
  setTimeout(() => { input.focus(); input.select(); }, 50);
}

async function fmRenameConfirm() {
  const newName = document.getElementById('fm-rename-input').value.trim();
  document.getElementById('fm-rename-modal').style.display = 'none';
  if (!newName || !fmSelection.length) return;
  const sel = fmSelection[0];
  const oldName = sel.path.split('/').pop();
  if (newName === oldName) return;
  const r = await apiFetch('/api/fm/rename', {sid:fmSid, old:sel.path, new:newName});
  if (r.ok) { notify('✓ Renamed', 'ok'); fmSelection=[]; fmLoad(fmPath); }
  else notify('Error: '+(r.error||'?'), 'err');
}

function fmUploadClick() { document.getElementById('fm-upload-input').click(); }

async function fmUploadFiles(evt) {
  const files = Array.from(evt.target.files);
  if (!files.length) return;
  let uploaded = 0, zips = [];
  for (const file of files) {
    notify(`Uploading ${file.name}...`, 'info');
    const fd = new FormData();
    fd.append('sid', fmSid);
    fd.append('path', fmPath);
    fd.append('file', file);
    try {
      const r = await fetch('/api/fm/upload', {method:'POST', body:fd});
      const d = await r.json();
      if (d.error) { notify(`Error: ${d.error}`, 'err'); continue; }
      uploaded++;
      if (d.is_zip) zips.push(d.path || (fmPath ? fmPath+'/'+file.name : file.name));
    } catch(e) { notify(`Upload failed: ${e}`, 'err'); }
  }
  evt.target.value = '';
  if (uploaded) notify(`✓ Uploaded ${uploaded} file(s)`, 'ok');
  fmLoad(fmPath);
  for (const zpath of zips) {
    if (confirm(`"${zpath.split('/').pop()}" is a zip — extract it now?`)) {
      await fmExtractZip(zpath);
    }
  }
}

async function fmExtractZip(path) {
  if (!path) {
    if (!fmSelection.length || !fmSelection[0].path.toLowerCase().endsWith('.zip')) {
      notify('Select a .zip file first', 'err'); return;
    }
    path = fmSelection[0].path;
  }
  notify(`Extracting ${path.split('/').pop()}...`, 'info');
  const r = await apiFetch('/api/fm/extract', {sid:fmSid, path});
  if (r.ok) { notify(`✓ Extracted ${r.extracted} file(s)`, 'ok'); fmLoad(fmPath); }
  else notify('Error: '+(r.error||'?'), 'err');
}

async function fmCompressSelected() {
  if (!fmSelection.length) { notify('Select items to compress', 'err'); return; }
  const defaultName = fmSelection[0].path.split('/').pop().replace(/\.[^.]+$/, '') + '.zip';
  const input = document.getElementById('fm-compress-input');
  input.value = defaultName;
  document.getElementById('fm-compress-modal').style.display = 'flex';
  setTimeout(() => { input.focus(); input.select(); }, 50);
}

async function fmCompressConfirm() {
  const name = document.getElementById('fm-compress-input').value.trim();
  document.getElementById('fm-compress-modal').style.display = 'none';
  if (!name) return;
  notify('Compressing...', 'info');
  const r = await apiFetch('/api/fm/compress', {sid:fmSid, paths:fmSelection.map(s=>s.path), name});
  if (r.ok) { notify(`✓ Created ${r.zip} (${r.files} files)`, 'ok'); fmLoad(fmPath); }
  else notify('Error: '+(r.error||'?'), 'err');
}

// ─── Download ─────────────────────────────────────────────────────────────────
function fmDownloadSelected() {
  if (!fmSelection.length) return;
  if (fmSelection.length === 1 && fmSelection[0].type === 'file') {
    // Single file — direct download
    const url = `/api/fm/download?sid=${fmSid}&path=${encodeURIComponent(fmSelection[0].path)}`;
    const a = document.createElement('a');
    a.href = url;
    a.download = fmSelection[0].path.split('/').pop();
    a.click();
  } else {
    // Multiple items or folder — zip and download
    const paths = fmSelection.map(s => s.path).join('|');
    const url = `/api/fm/download-zip?sid=${fmSid}&paths=${encodeURIComponent(paths)}`;
    notify('Preparing download...', 'info');
    const a = document.createElement('a');
    a.href = url;
    a.download = 'download.zip';
    a.click();
  }
}

// ─── Move Dialog ──────────────────────────────────────────────────────────────
let fmMoveAllDirs = [];

async function openMoveModal() {
  if (!fmSelection.length) return;
  const n = fmSelection.length;
  document.getElementById('fm-move-count').textContent =
    n === 1 ? fmSelection[0].path.split('/').pop() : `${n} items`;
  document.getElementById('fm-move-filter').value = '';
  document.getElementById('fm-move-confirm').disabled = true;
  fmMoveTarget = null;
  document.getElementById('fm-move-modal').style.display = 'flex';
  const r = await fetch(`/api/fm/dirs?sid=${fmSid}`);
  const d = await r.json();
  fmMoveAllDirs = d.dirs || [];
  renderMoveDirs(fmMoveAllDirs);
}

function renderMoveDirs(dirs) {
  const filter = (document.getElementById('fm-move-filter').value || '').toLowerCase();
  const el = document.getElementById('fm-move-dirs');
  const all = ['/', ...dirs];
  const filtered = filter ? all.filter(d => d.toLowerCase().includes(filter)) : all;
  if (!filtered.length) {
    el.innerHTML = '<div style="padding:14px;color:var(--text3);font-size:12px;text-align:center">No matching directories</div>';
    return;
  }
  el.innerHTML = filtered.map(d => {
    const depth  = d === '/' ? 0 : d.split('/').length;
    const label  = d === '/' ? '/ (web root)' : d;
    const active = fmMoveTarget === (d === '/' ? '' : d);
    return `<div class="fm-row${active?' selected':''}" style="padding-left:${8+depth*14}px"
      onclick="selectMoveDir(this,'${esc(d)}')">
      <span class="fm-icon">📁</span>
      <span class="fm-name" style="font-family:var(--mono);font-size:12px">${esc(label)}</span>
    </div>`;
  }).join('');
}

function filterMoveDirs() {
  renderMoveDirs(fmMoveAllDirs);
}

function selectMoveDir(el, dir) {
  fmMoveTarget = dir === '/' ? '' : dir;
  document.getElementById('fm-move-confirm').disabled = false;
  document.querySelectorAll('#fm-move-dirs .fm-row').forEach(r => r.classList.remove('selected'));
  el.classList.add('selected');
}

function closeMoveModal() {
  document.getElementById('fm-move-modal').style.display = 'none';
  fmMoveTarget = null;
}

async function confirmMove() {
  if (fmMoveTarget === null) return;
  const paths = fmSelection.map(s => s.path);
  notify(`Moving ${paths.length} item(s)...`, 'info');
  const r = await apiFetch('/api/fm/move', {sid:fmSid, paths, dest:fmMoveTarget});
  closeMoveModal();
  if (r.ok) {
    notify(`✓ Moved ${r.moved} item(s)`, 'ok');
    fmSelection = [];
    fmLoad(fmPath);
  } else {
    notify('Error: '+(r.error||'?'), 'err');
    fmLoad(fmPath);
  }
}

// ─── Chmod Dialog ─────────────────────────────────────────────────────────────
let fmChmodPaths = [];
const CHMOD_PRESETS = [
  {label:'755', hint:'rwxr-xr-x — dirs/executables'},
  {label:'644', hint:'rw-r--r-- — files'},
  {label:'777', hint:'rwxrwxrwx — full access'},
  {label:'600', hint:'rw------- — private'},
  {label:'664', hint:'rw-rw-r-- — group write'},
  {label:'775', hint:'rwxrwxr-x — group exec'},
];

function openChmodModal(paths, evt) {
  if (evt) evt.stopPropagation();
  fmChmodPaths = paths || fmSelection.map(s => s.path);
  if (!fmChmodPaths.length) { notify('Select items first', 'err'); return; }
  const label = fmChmodPaths.length === 1
    ? fmChmodPaths[0].split('/').pop()
    : `${fmChmodPaths.length} items`;
  document.getElementById('fm-chmod-target').textContent = label;
  // Build preset grid
  document.getElementById('fm-chmod-grid').innerHTML = CHMOD_PRESETS.map(p =>
    `<div onclick="setChmodPreset('${p.label}')" style="background:var(--bg3);border:1px solid var(--border);
      border-radius:6px;padding:8px;text-align:center;cursor:pointer;transition:all .1s"
      onmouseover="this.style.borderColor='var(--green)'" onmouseout="this.style.borderColor='var(--border)'">
      <div style="font-family:var(--mono);font-size:14px;font-weight:600;color:var(--text)">${p.label}</div>
      <div style="font-size:9px;color:var(--text3);margin-top:3px">${p.hint}</div>
    </div>`
  ).join('');
  // Default to 755
  document.getElementById('fm-chmod-octal').value = '755';
  document.getElementById('fm-chmod-hint').textContent = '';
  document.getElementById('fm-chmod-modal').style.display = 'flex';
}

function closeChmodModal() {
  document.getElementById('fm-chmod-modal').style.display = 'none';
  fmChmodPaths = [];
}

function setChmodPreset(val) {
  document.getElementById('fm-chmod-octal').value = val;
  syncChmodFromOctal();
}

function syncChmodFromOctal() {
  const val = document.getElementById('fm-chmod-octal').value.trim();
  const hint = document.getElementById('fm-chmod-hint');
  if (/^[0-7]{3,4}$/.test(val)) {
    const n = parseInt(val, 8);
    const bits = ['---','--x','-w-','-wx','r--','r-x','rw-','rwx'];
    const owner = bits[(n>>6)&7], group = bits[(n>>3)&7], other = bits[n&7];
    hint.textContent = `${owner}${group}${other}`;
    hint.style.color = 'var(--green)';
  } else {
    hint.textContent = val.length ? 'Invalid octal' : '';
    hint.style.color = 'var(--red)';
  }
}

async function confirmChmod() {
  const mode = document.getElementById('fm-chmod-octal').value.trim();
  if (!/^[0-7]{3,4}$/.test(mode)) { notify('Invalid octal mode', 'err'); return; }
  notify(`Applying chmod ${mode}...`, 'info');
  const r = await apiFetch('/api/fm/chmod', {sid:fmSid, paths:fmChmodPaths, mode});
  closeChmodModal();
  if (r.ok) {
    notify(`✓ chmod ${mode} applied to ${r.changed} item(s)`, 'ok');
    fmLoad(fmPath);
  } else {
    notify('Error: '+(r.error||'?'), 'err');
  }
}

// ─────────────────────────────── Utilities ────────────────────────────────────
function fileIcon(ext) {
  const map = {'.php':'🐘','.html':'🌐','.htm':'🌐','.css':'🎨','.js':'⚡','.json':'{}','.md':'📝','.txt':'📄','.jpg':'🖼','.jpeg':'🖼','.png':'🖼','.gif':'🖼','.svg':'🎭','.zip':'📦','.tar':'📦','.gz':'📦','.sql':'🗃','.env':'⚙','.htaccess':'🔒'};
  return map[ext] || '📄';
}

function fmHumanSize(b) {
  if (b < 1024) return b+'B';
  if (b < 1048576) return (b/1024).toFixed(1)+'K';
  return (b/1048576).toFixed(1)+'M';
}

function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('collapsed');
  document.getElementById('main').classList.toggle('expanded');
}

function copy(text) {
  navigator.clipboard.writeText(text).then(() => notify('Copied!', 'ok'));
}

function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

async function apiFetch(url, body) {
  try {
    const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    return await r.json();
  } catch(e) {
    return {error: String(e)};
  }
}

function notify(msg, type='info') {
  const el = document.createElement('div');
  el.className = `notif ${type}`;
  el.textContent = msg;
  document.getElementById('notif').appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

init();
</script>
</body>
</html>"""

# ─── Entry Point ─────────────────────────────────────────────────────────────
def main():
    write_base_nginx_conf()

    if not docker_running():
        print("  ⚠  Docker not detected — start Docker and refresh the UI.")

    def open_browser():
        time.sleep(1)
        webbrowser.open(f"http://localhost:{PORT}")

    threading.Thread(target=open_browser, daemon=True).start()

    server = ThreadedHTTPServer(("", PORT), Handler)
    server.socket.settimeout(None)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down PHP-MNGR.")

if __name__ == "__main__":
    main()
