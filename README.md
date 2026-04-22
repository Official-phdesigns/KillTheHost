<h1 align="center">KillTheHost v1.3</h1>
<p align="center">
  <img src="https://img.shields.io/badge/Latest-v1.3.0-brightgreen" />
</p>
<br/>
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

> **KillTheHost** brings together **PHP-MNGR**, **DB-3NGIN3**, **MAIL-SRVR**, and **STAX-MNGR** into one unified workflow —  
> run your stack locally, manage your databases, host your own email, deploy Docker stacks, sync real domains, and go live in a click.

<br/>

[**🌐 Website**](https://killthehost.com)

<br/>

---

</div>

<br/>

## 🧩 What's Inside

KillTheHost is a bundle of four open-source, single-file Python tools unified by a cross-platform browser-based launcher — designed to eliminate the gap between local development and live deployment.

| Tool | Version | Purpose |
|---|---|---|
| ⚡ **Launcher** | `v1.2` | Unified browser UI to start, stop, and monitor all tools — zero dependencies |
| 🐘 **PHP-MNGR** | `v2.4` | Local & Public PHP project manager — spin up, manage, and publish PHP sites via Docker |
| 🗄️ **DB-3NGIN3** | `v1.2` | Local database service manager — PostgreSQL, MySQL, MariaDB, Redis, MongoDB |
| ✉️ **MAIL-SRVR** | `v1.1` | Self-hosted email server — send, receive, IMAP, DKIM, SPF, DMARC, and a full browser mail client |
| 🐳 **STAX-MNGR** | `v1.0` | Docker stack manager — deploy and manage pre-configured application stacks with one click |

Together, they connect to your **Namecheap** domains and route traffic through **Cloudflare Tunnels** — putting your localhost on the public internet without a single line of server config.

<br/>

---

## ✨ Features

<br/>

```
  localhost:5000  ──►  Launcher UI  (control panel for all tools)
  localhost:4280  ──►  PHP-MNGR    ──►  Cloudflare Tunnel  ──►  yoursite.com
  localhost:7734  ──►  DB-3NGIN3   ──►  PostgreSQL · MySQL · Redis · MongoDB
  localhost:6060  ──►  MAIL-SRVR   ──►  SMTP/IMAP  ──►  mail.yourdomain.com
  localhost:6161  ──►  STAX-MNGR  ──►  Docker Stacks  ──►  VaultWarden · Nextcloud · Gitea…
```

<br/>

### ⚡ Unified Launcher
A single browser-based control panel that starts and stops PHP-MNGR, DB-3NGIN3, MAIL-SRVR, and STAX-MNGR with one click. Real-time console output, live status indicators, uptime timers, and port monitoring — all in one place. Zero external dependencies, pure Python standard library.

### 🌐 Domain Sync
Connect your **Namecheap** account and assign real domains to local projects — no manual DNS editing required. More registrar integrations are on the roadmap.

### ☁️ Cloudflare Tunnel Integration
Link a free Cloudflare account and expose local services to the public web securely. No port forwarding. No router config. No tunnel scripts. Works on CGNAT connections (e.g. T-Mobile 5G home internet) where traditional port forwarding is impossible.

### 🚀 One-Click Public Access
From localhost to a live URL in seconds — perfect for client previews, team demos, and real-world testing without a full deployment pipeline.

### 🐘 PHP Project Control
Spin up `php:VERSION-apache` Docker containers per site with a single click. Supports **PHP 7.2 through 8.3**. Each site gets its own port (auto-assigned from 8100+), a browser-based **file manager** (browse, edit, upload, download, rename, delete, chmod), an **inline code editor** for PHP/HTML/CSS/JS, and a **custom `php.ini`** per site.

### 🗄️ Database Service Management
Spin up or shut down **PostgreSQL, MySQL, MariaDB, Redis, and MongoDB** Docker containers with a single click. Live status is polled every 15 seconds, connection strings are always one click away, and persistent data survives container restarts — stored in `~/.db3ngin3/data/`.

### ✉️ Self-Hosted Email Server
Run a complete email server on your own VPS. MAIL-SRVR handles everything: SMTP delivery and inbound receiving, IMAP inbox access, DKIM signing, SPF and DMARC records, and an automated deliverability checklist. Includes a full browser-based email client with compose, rich text editing, file attachments, draft saving, folder navigation (Inbox, Sent, Drafts, Trash, Junk), and per-account HTML signatures.

<br/>

---

## 📦 Installation

### Requirements

| Requirement | Notes |
|---|---|
| **Python 3.8+** | Standard library only — no pip installs required |
| **Docker** | Used by PHP-MNGR, DB-3NGIN3, and MAIL-SRVR to run all containers |

**Install Docker on Ubuntu/Linux**

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
# Log out and back in before continuing
```

---

**1. Clone the repo**

```bash
git clone https://github.com/Official-phdesigns/KillTheHost.git
cd KillTheHost
```

**2. Start the Launcher**

**Linux / macOS:**
```bash
chmod +x launch.sh
./launch.sh
```

**Windows:**
```
Double-click launch.bat  — or run it from any terminal
```

The launcher opens automatically at **http://localhost:5000** and lets you start, stop, and monitor all tools from one place.

<br/>

### Folder Structure

```
KillTheHost/
├── launch.sh                   ← Linux / macOS entry point
├── launch.bat                  ← Windows entry point
├── LICENSE
├── README.md
└── Launcher/
    ├── launcher.py             ← Browser-based control panel (port 5000)
    └── assets/
        └── main/
            ├── PHP-MNGR v2.4/
            │   └── phpmanager.py
            ├── DB-3NGIN3 v1.2/
            │   └── db3ngin3.py
            ├── MAIL-SRVR v1.0/
            │   └── mailserver.py
            └── STAX-MNGR v1.0/
                └── staxmngr.py
```

<br/>

---

## 🛠️ Usage

### Using the Launcher

Once `launch.sh` / `launch.bat` is run, a control panel opens in your browser at `http://localhost:5000`.

From there you can:
- **Start / Stop** PHP-MNGR, DB-3NGIN3, MAIL-SRVR, and STAX-MNGR individually or together
- **Open** each panel directly in a new browser tab
- **Monitor** live status, uptime, and real-time console output for all services
- **Filter** console output by service

To stop the launcher itself, press `Ctrl+C` in the terminal. It will gracefully shut down any running services first.

### Connecting a Domain
1. Whitelist your public IP address in the Namecheap API settings to allow external requests
2. Create a scoped API token in Cloudflare (avoid using the global API key)
3. Assign the following permissions to the token for the target domain:
   - Account → Cloudflare Tunnel → Edit & Read
   - Zone → Zone → Read
   - Zone → DNS → Edit & Read
4. Open KillTheHost and navigate to **Domains & Tunnels** in PHP-MNGR
5. In **⚙ Settings → Cloudflare**, paste your API token and save
6. In **⚙ Settings → Namecheap**, enter your API key and username
7. Select the desired domain and map it to your local service (port/container)
8. Apply changes — DNS records are provisioned automatically; no manual configuration required

### Going Live with Cloudflare

1. Log in with your **Cloudflare account** (free tier works)
2. Go to **Domains & Tunnels** and click **☁ Tunnel Site** on any domain
3. Select your PHP site from the dropdown
4. Hit **Tunnel Now** — KillTheHost handles the tunnel setup
5. Your site is now reachable at your real domain

### Setting Up Email

1. Open MAIL-SRVR at **http://localhost:6060**
2. Go to **Settings** and switch to **Live mode**
3. Navigate to the **Domain** tab, select your Cloudflare zone, and confirm your public IP
4. Click **▶ Start Server** — MX, A, SPF, and DMARC records are provisioned in Cloudflare automatically
5. After ~60 seconds, click **Provision DKIM** to generate and publish your signing key
6. Add a mailbox in the **Accounts** tab
7. Use the **Compose** tab to send your first email

> **Note:** Live email delivery requires a VPS with a public static IP and outbound port 25 open. MAIL-SRVR includes a deliverability checklist that guides you through every requirement.

<br/>

---

## 🐘 PHP-MNGR Reference

### Supported PHP Versions

PHP-MNGR runs each site inside a `php:VERSION-apache` Docker container. Supported versions:

`7.2` · `7.3` · `7.4` · `8.0` · `8.1` · `8.2` · `8.3`

### Port Assignments

| Service | Port |
|---|---|
| Launcher UI | 5000 |
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
Launcher UI → http://localhost:5000
```

### Data Locations

| What | Where |
|---|---|
| Site registry | `~/.phpmngr/sites.json` |
| Tunnel registry | `~/.phpmngr/tunnels.json` |
| Cloudflare credentials | `~/.phpmngr/cloudflare.json` |
| Namecheap credentials | `~/.phpmngr/namecheap.json` |
| Site web roots | `~/.phpmngr/sites/<id>/www/` |
| Per-site PHP config | `~/.phpmngr/sites/<id>/php.ini` |

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

### Data Locations

| What | Where |
|---|---|
| Instance metadata | `~/.db3ngin3/instances.json` |
| Database files | `~/.db3ngin3/data/<instance-id>/` |

Deleting an instance removes the Docker container but **preserves data files on disk**.

<br/>

---

## ✉️ MAIL-SRVR Reference

### Requirements for Live Mode

| Requirement | Notes |
|---|---|
| **VPS** | Static public IP required. Port 25 must be open outbound. |
| **Domain** | Must be managed via Cloudflare DNS (free account works). |
| **Cloudflare token** | Shared with PHP-MNGR — enter once in PHP-MNGR Settings. |
| **Recommended VPS** | Hetzner, Contabo — port 25 open by default. OVH/Vultr require a support ticket. |

### DNS Records (Auto-Provisioned)

| Record | Type | Purpose |
|---|---|---|
| `mail.yourdomain.com` | A (DNS-only) | Mail server address |
| `yourdomain.com` | MX | Incoming mail routing |
| `yourdomain.com` | TXT (SPF) | Sender Policy Framework |
| `_dmarc.yourdomain.com` | TXT (DMARC) | DMARC policy |
| `mail._domainkey.yourdomain.com` | TXT (DKIM) | DKIM signing key |

### Port Assignments

| Service | Port |
|---|---|
| MAIL-SRVR UI | 6060 |
| SMTP submission (outbound) | 587 |
| IMAP (inbox) | 143 |
| Postfix inbound | 25 |
| Mailpit UI (dev mode) | 8025 |

### Data Locations

| What | Where |
|---|---|
| Configuration | `~/.mailsrvr/config.json` |
| Accounts | `~/.mailsrvr/accounts.json` |
| Checklist state | `~/.mailsrvr/checklist.json` |
| Mail storage | `~/.mailsrvr/mail-data/` |
| DKIM keys & Postfix config | `~/.mailsrvr/config/` |

<br/>

---

### 🐳 Docker Stack Manager *(New in v1.3)*
Deploy and manage pre-configured Docker application stacks with a single click. STAX-MNGR ships with 10 ready-to-use stacks — from password managers and media servers to local AI and self-hosted Git. Each stack is a fully configured Docker Compose setup with persistent data volumes.

| Stack | Port | Category |
|---|---|---|
| 🔐 VaultWarden | 8200 | Privacy |
| 📡 Uptime Kuma | 3002 | Monitoring |
| 🎵 Navidrome | 4533 | Media |
| 📖 Wiki.js | 3003 | Productivity |
| 🎬 Jellyfin | 8096 | Media |
| 🤖 Ollama | 11434 | AI |
| 💬 Open WebUI | 3000 | AI |
| 📝 WordPress | 8080 | CMS |
| ☁️ Nextcloud | 8081 | Productivity |
| 🐙 Gitea | 3001 | Development |

<br/>

---

## 🗺️ Survive Reboots

```bash
# Auto-restart site containers
docker update --restart unless-stopped <site-name>

# Start manually after reboot
./launch.sh        # Linux / macOS
launch.bat         # Windows
```

**Optional: systemd service (Linux)**

```ini
# Save as ~/.config/systemd/user/killthehost.service
[Unit]
Description=KillTheHost Launcher

[Service]
ExecStart=/bin/sh /path/to/KillTheHost/launch.sh
WorkingDirectory=/path/to/KillTheHost
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now killthehost
```

<br/>

---

## 🗺️ Roadmap

- [x] PHP site management (PHP-MNGR)
- [x] Database service management (DB-3NGIN3)
- [x] Cloudflare tunnel integration
- [x] Namecheap domain sync
- [x] Unified cross-platform launcher (v1.1)
- [x] Self-hosted email server with full browser client (MAIL-SRVR v1.1)
- [x] Docker stack manager with pre-configured application stacks (STAX-MNGR v1.0)
- [ ] Additional domain registrar support (GoDaddy, Porkbun, Cloudflare Registrar…)
- [ ] Multi-domain email support in MAIL-SRVR
- [ ] MAIL-SRVR relay/smarthost option for providers that block port 25

<br/>

---

## 🖥️ Screenshots

<div align="center">

### KillTheHost Launcher — Main Control Panel

![KillTheHost Launcher interface](https://i.ibb.co/5WWvFztg/Screenshot-From-2026-04-22-03-34-56.png)

*Dedicated control panel for managing all KillTheHost services, including status, uptime, and runtime controls*

<br/>

### MAIL-SRVR — Browser Email Client

![MAIL-SRVR interface]( )

*Full email client — compose with rich text and attachments, inbox with folders, deliverability checklist, DKIM provisioning*

<br/>

### STAX-MNGR v1.0 — Docker Manager & Stack Deployment

![STAX-MNGR interface](https://i.ibb.co/Txcm0n2N/Screenshot-From-2026-04-22-03-14-11.png)

*Deploy, monitor, and control Docker stacks with full visibility into containers and runtime*

<br/>

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
