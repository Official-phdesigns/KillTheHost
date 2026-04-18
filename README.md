# KillTheHost v1.0

<p align="center">
  <img src="https://killthehost.com/images/social-card.png" alt="" width="500">
</p>
<div align="center">
<br/>

### **Local development → public web, without friction.**

<br/>

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blueviolet.svg?style=for-the-badge)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.8+-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)](https://python.org)
[![PHP](https://img.shields.io/badge/PHP-7.2--8.3-777BB4?style=for-the-badge&logo=php&logoColor=white)](https://php.net)
[![Cloudflare](https://img.shields.io/badge/Cloudflare-Tunnels-F38020?style=for-the-badge&logo=cloudflare&logoColor=white)](https://cloudflare.com)
[![Namecheap](https://img.shields.io/badge/Namecheap-Domain%20Sync-DE3723?style=for-the-badge)](https://namecheap.com)
[![Docker](https://img.shields.io/badge/Docker-Required-2496ED?style=for-the-badge&logo=docker&logoColor=white)](https://docs.docker.com/engine/install/)

<br/>

> **KillTheHost** brings together **PHP-MNGR** and **DB-3NGIN3** into one unified workflow —  
> run your stack locally, manage your databases, sync real domains, and go live in a click.

<br/>

[**🌐 Website**](https://killthehost.com)

<br/>

---

</div>

<br/>

## 🧩 What's Inside

KillTheHost is a bundle of two open-source, single-file Python tools designed to eliminate the gap between local development and live deployment.

| Tool | Version | Purpose |
|---|---|---|
| 🐘 **PHP-MNGR** | `v2.4` | Local & Public PHP project manager — spin up, manage, and publish PHP sites via Docker |
| 🗄️ **DB-3NGIN3** | `v1.1` | Local database service manager — PostgreSQL, MySQL, Redis, MongoDB |

Together, they connect to your **Namecheap** domains and route traffic through **Cloudflare Tunnels** — putting your localhost on the public internet without a single line of server config.

<br/>

---

## ✨ Features

<br/>

```
  localhost:4280  ──►  PHP-MNGR  ──►  NameCheap  ──►  Cloudflare Tunnel  ──►  yoursite.com
  localhost:5432  ──►  DB-3NGIN3 ──►  Local  ──►  Cloudflare Tunnel  ──►  yoursite.com
```

<br/>

### 🌐 Domain Sync
Connect your **Namecheap** account and assign real domains to local projects — no manual DNS editing required. More registrar integrations are on the roadmap.

### ☁️ Cloudflare Tunnel Integration  
Link a free Cloudflare account and expose local services to the public web securely. No port forwarding. No router config. No tunnel scripts. Works on CGNAT connections (e.g. T-Mobile 5G home internet) where traditional port forwarding is impossible.

### 🚀 One-Click Public Access  
From localhost to a live URL in seconds — perfect for client previews, team demos, and real-world testing without a full deployment pipeline.

### 🐘 PHP Project Control  
Spin up `php:VERSION-apache` Docker containers per site with a single click. Supports **PHP 7.2 through 8.3**. Each site gets its own port (auto-assigned from 8100+), a browser-based **file manager** (browse, edit, upload, download, rename, delete, chmod), an **inline code editor** for PHP/HTML/CSS/JS, and a **custom `php.ini`** per site. Start and stop sites from a clean UI instead of juggling terminal tabs.

### 🗄️ Database Service Management  
Spin up or shut down **PostgreSQL, MySQL, MariaDB, Redis, and MongoDB** Docker containers with a single click. Live status is polled every 15 seconds, connection strings are always one click away, and persistent data survives container restarts — stored in `~/.db3ngin3/data/`.

<br/>

---

## 📦 Installation

### Requirements

| Requirement | Notes |
|---|---|
| **Python 3.8+** | Standard library only — no pip installs required |
| **Docker** | Used by both PHP-MNGR and DB-3NGIN3 to run all containers |

**Install Docker on Ubuntu/Linux**

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# Log out and back in before continuing
```

---

**1. Download the bundle**

```bash
git clone https://github.com/Official-phdesigns/KillTheHost.git
unzip KillTheHost.zip
cd KillTheHost
```

**2. Run PHP-MNGR**

```bash
sg docker -c "python3 phpmanager.py"
```

> Opens automatically at **http://localhost:4280**

**3. Run DB-3NGIN3**

```bash
python3 db3ngin3.py
```

> Opens automatically at **http://localhost:7734**

> Both tools are single-file Python scripts with no external dependencies — no pip, no virtualenv.

<br/>

---

## 🛠️ Usage

### Connecting a Domain
1. Whitelist your public IP address in the Namecheap API settings to allow external requests
2. Create a scoped API token in Cloudflare (avoid using the global API key)
3. Assign the following permissions to the token for the target domain:
   - Account → Cloudflare Tunnel → Edit & Read
   - Zone → Zone → Read
   - Zone → DNS → Edit & Read
4. Open KillTheHost and navigate to **Domains & Tunnels**
5. In **⚙ Settings → Cloudflare**, paste your API token and save
6. In **⚙ Settings → Namecheap**, enter your API key and username
7. Select the desired domain and map it to your local service (port/container)
8. Apply changes — DNS records are provisioned automatically; no manual configuration required

### Notes
- DNS changes typically propagate within seconds via Cloudflare, but allow up to a few minutes in edge cases
- Ensure your local service is reachable (correct port binding / container exposure) before mapping
- Restrict API tokens to the minimum required scope for security

### Going Live with Cloudflare

1. Log in with your **Cloudflare account** (free tier works)
2. Go to **Domains & Tunnels** and click **☁ Tunnel Site** on any domain
3. Select your PHP site from the dropdown
4. Hit **Tunnel Now** — KillTheHost handles the tunnel setup
5. Your site is now reachable at your real domain

<br/>

---

## 🐘 PHP-MNGR Reference

### Supported PHP Versions

PHP-MNGR runs each site inside a `php:VERSION-apache` Docker container. Supported versions:

`7.2` · `7.3` · `7.4` · `8.0` · `8.1` · `8.2` · `8.3`

### Port Assignments

| Service | Port |
|---|---|
| PHP-MNGR UI | 4280 |
| PHP sites (auto-assigned) | 8100, 8101, 8102… |

### Architecture

```
Internet
  └── yourdomain.com (Cloudflare Edge)
        └── Cloudflare Named Tunnel (cloudflared container)
              └── http://localhost:8100
                    └── PHP Docker container (website)

PHP-MNGR UI → http://localhost:4280
```

**Docker containers created:**
- `<site-name>` — one `php:VERSION-apache` container per site (ports 8100+)
- `cftunnel-<site-id>` — one `cloudflare/cloudflared` container per tunnel (`--network host`)

### Data Locations

| What | Where |
|---|---|
| Site registry | `~/.phpmngr/sites.json` |
| Tunnel registry | `~/.phpmngr/tunnels.json` |
| Cloudflare credentials | `~/.phpmngr/cloudflare.json` |
| Namecheap credentials | `~/.phpmngr/namecheap.json` |
| Site web roots | `~/.phpmngr/sites/<id>/www/` |
| Per-site PHP config | `~/.phpmngr/sites/<id>/php.ini` |

### Survive Reboots

```bash
# Auto-restart site containers
docker update --restart unless-stopped <site-name>

# Start PHP-MNGR manually after reboot
cd "/path/to/phpmanager"
sg docker -c "python3 phpmanager.py"
```

**Optional: systemd service**

```ini
# Save as ~/.config/systemd/user/phpmngr.service
[Unit]
Description=PHP-MNGR

[Service]
ExecStart=/usr/bin/sg docker -c "python3 /path/to/phpmanager.py"
WorkingDirectory=/path/to/phpmanager
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now phpmngr
```

<br/>

---

## 🗄️ DB-3NGIN3 Reference

### Supported Databases

DB-3NGIN3 manages the following engines via Docker:

| Database | Versions | Default Port | User | Password |
|---|---|---|---|---|
| **PostgreSQL** | 13, 14, 15, 16 | 5432 | `postgres` | `postgres` |
| **MySQL** | 5.7, 8.0, 8.3 | 3306 | `admin` | `admin` |
| **MariaDB** | 10.6, 10.11, 11.3 | 3307 | `root` | `root` |
| **Redis** | 6.2, 7.0, 7.2 | 6379 | — | — |
| **MongoDB** | 5.0, 6.0, 7.0 | 27017 | `admin` | `admin` |

> ⚠️ **Security Notice:** These are default credentials for local development. Do not expose database containers publicly without changing credentials first.

Port conflicts are detected automatically at instance creation time.

### Data Locations

| What | Where |
|---|---|
| Instance metadata | `~/.db3ngin3/instances.json` |
| Database files | `~/.db3ngin3/data/<instance-id>/` |

Deleting an instance removes the Docker container but **preserves data files on disk**.

### Run DB-3NGIN3 as a Background Service

```ini
# Save as ~/.config/systemd/user/db3ngin3.service
[Unit]
Description=DB-3NGIN3 database manager

[Service]
ExecStart=/usr/bin/python3 /path/to/db3ngin3.py
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now db3ngin3
```

<br/>

---

## 🗺️ Roadmap

- [x] PHP site management (PHP-MNGR)
- [x] Database service management (DB-3NGIN3)
- [x] Cloudflare tunnel integration
- [x] Namecheap domain sync
- [ ] File Editor in Project File Manager
- [ ] Additional domain registrar support (Cloudflare, GoDaddy, Porkbun...)

<br/>

---

## 🖥️ Screenshots

<div align="center">

### DB-3NGIN3 — Database Instance Control

![DB-3NGIN3 interface](https://killthehost.com/images/db.png)

*View running state and ports for all local database services*

<br/>

### PHP-MNGR — Site Management View

![PHP-MNGR interface](https://killthehost.com/images/php.png)

*Manage & create every PHP project with runtime info, port visibility, inline editing, and Cloudflare tunneling*

</div>

<br/>

---

## 🤝 Contributing

Contributions are welcome and appreciated! Here's how to get involved:

```bash
# Fork the repo, then:
git clone https://github.com/Official-phdesigns/KillTheHost.git
cd killthehost
git checkout -b feature/your-feature-name
```

1. Make your changes
2. Write or update tests if applicable
3. Open a pull request with a clear description

For major changes, please open an issue first to discuss what you'd like to change.

<br/>

---

## 📄 License

This project is licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)** — see the [LICENSE](LICENSE) file for details.

Any modified versions of this software that are run over a network must also be made available as open source under the same license.

<br/>

---

<div align="center">

**Copyright © 2026 KillTheHost — Developed by PhDesigns, LLC**

[killthehost.com](https://killthehost.com) &nbsp;·&nbsp; [Report a Bug](https://github.com/Official-phdesigns/KillTheHost/issues) &nbsp;·&nbsp; [Request a Feature](https://github.com/Official-phdesigns/KillTheHost/issues)

<br/>

*Stop treating localhost like a dead end.*

</div>
