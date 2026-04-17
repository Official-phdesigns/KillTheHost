# KillTheHost v1.0

<img src="https://killthehost.com/images/social-card.png" alt="Alt Text" width="500" height="300">

A modern, open-source hosting panel for developers who want full control over servers, deployments, and infrastructure without unnecessary overhead.

<div align="center">

<br/>

```
 ██╗  ██╗██╗██╗     ██╗     ████████╗██╗  ██╗███████╗██╗  ██╗ ██████╗ ███████╗████████╗
 ██║ ██╔╝██║██║     ██║        ██╔══╝██║  ██║██╔════╝██║  ██║██╔═══██╗██╔════╝╚══██╔══╝
 █████╔╝ ██║██║     ██║        ██║   ███████║█████╗  ███████║██║   ██║███████╗   ██║   
 ██╔═██╗ ██║██║     ██║        ██║   ██╔══██║██╔══╝  ██╔══██║██║   ██║╚════██║   ██║   
 ██║  ██╗██║███████╗███████╗   ██║   ██║  ██║███████╗██║  ██║╚██████╔╝███████║   ██║   
 ╚═╝  ╚═╝╚═╝╚══════╝╚══════╝   ╚═╝   ╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝   ╚═╝   
```

### **Local development → public web, without friction.**

<br/>

[![License: MIT](https://img.shields.io/badge/License-MIT-blueviolet.svg?style=for-the-badge)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.8+-3670A0?style=for-the-badge&logo=python&logoColor=ffdd54)](https://python.org)
[![PHP](https://img.shields.io/badge/PHP-7.4+-777BB4?style=for-the-badge&logo=php&logoColor=white)](https://php.net)
[![Cloudflare](https://img.shields.io/badge/Cloudflare-Tunnels-F38020?style=for-the-badge&logo=cloudflare&logoColor=white)](https://cloudflare.com)
[![Namecheap](https://img.shields.io/badge/Namecheap-Domain%20Sync-DE3723?style=for-the-badge)](https://namecheap.com)

<br/>

> **KillTheHost** brings together **PHP-MNGR** and **DB-3NGIN3** into one unified workflow —  
> run your stack locally, manage your databases, sync real domains, and go live in a click.

<br/>

[**⬇ Download Bundle**](https://killthehost.com/downloads/KillTheHost-v1.0.zip) &nbsp;·&nbsp;
[**🐘 PHP-MNGR**](https://killthehost.com/downloads/PHP-MNGR-v2.4.zip) &nbsp;·&nbsp;
[**🗄️ DB-3NGIN3**](https://killthehost.com/downloads/DB-3NGIN3-v1.1.zip) &nbsp;·&nbsp;
[**🌐 Website**](https://killthehost.com)

<br/>

---

</div>

<br/>

## 🧩 What's Inside

KillTheHost is a bundle of two open-source, single-file Python tools designed to eliminate the gap between local development and live deployment.

| Tool | Version | Purpose |
|---|---|---|
| 🐘 **PHP-MNGR** | `v2.4` | Local PHP project manager — start, stop, and monitor sites |
| 🗄️ **DB-3NGIN3** | `v1.1` | Local database service manager — PostgreSQL, MySQL, Redis, MongoDB |

Together, they connect to your **Namecheap** domains and route traffic through **Cloudflare Tunnels** — putting your localhost on the public internet without a single line of server config.

<br/>

---

## ✨ Features

<br/>

```
  localhost:8080  ──►  PHP-MNGR  ──►  Cloudflare Tunnel  ──►  yoursite.com
  localhost:5432  ──►  DB-3NGIN3 ──►  Cloudflare Tunnel  ──►  yoursite.com
```

<br/>

### 🌐 Domain Sync
Connect your **Namecheap** account and assign real domains to local projects — no manual DNS editing required. More registrar integrations are on the roadmap.

### ☁️ Cloudflare Tunnel Integration  
Link a free Cloudflare account and expose local services to the public web securely. No port forwarding. No router config. No tunnel scripts.

### 🚀 One-Click Public Access  
From localhost to a live URL in seconds — perfect for client previews, team demos, and real-world testing without a full deployment pipeline.

### 🐘 PHP Project Control  
View runtime state, port, PHP version, and filesystem path for every local site. Start and stop projects from a clean interface instead of juggling terminal tabs.

### 🗄️ Database Service Management  
See which database services are running and on which ports. Spin up or shut down PostgreSQL, MySQL, MariaDB, Redis, and MongoDB with a single click.

<br/>

---

## 📦 Installation

**1. Download the bundle**

```bash
# Download and unzip
curl -L https://killthehost.com/downloads/KillTheHost-v1.0.zip -o KillTheHost.zip
unzip KillTheHost.zip
cd KillTheHost
```

**2. Install dependencies**

```bash
pip install -r requirements.txt
```

**3. Run PHP-MNGR**

```bash
python php-mngr.py
```

**4. Run DB-3NGIN3**

```bash
python db-3ngin3.py
```

> Both tools can be run independently or together as part of the full KillTheHost workflow.

<br/>

---

## 🛠️ Usage

### Connecting a Domain

1. Open KillTheHost and navigate to **Domain Sync**
2. Enter your **Namecheap API key**
3. Select a domain and assign it to a local site
4. Done — no DNS panel needed

### Going Live with Cloudflare

1. Log in with your **Cloudflare account** (free tier works)
2. Select a local project or database service
3. Hit **Expose** — KillTheHost handles the tunnel setup
4. Your site is now reachable at your real domain

<br/>

---

## 🗺️ Roadmap

- [x] PHP site management (PHP-MNGR)
- [x] Database service management (DB-3NGIN3)
- [x] Cloudflare tunnel integration
- [x] Namecheap domain sync
- [ ] Additional domain registrar support (Cloudflare, GoDaddy, Porkbun...)
- [ ] HTTPS auto-provisioning
- [ ] Multi-site project grouping
- [ ] GUI installer for macOS & Windows
- [ ] Plugin/extension API

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

*Manage every PHP project with runtime info, port visibility, and one-click controls*

</div>

<br/>

---

## 🤝 Contributing

Contributions are welcome and appreciated! Here's how to get involved:

```bash
# Fork the repo, then:
git clone https://github.com/your-username/killthehost.git
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

This project is licensed under the **MIT License** — see the [LICENSE](LICENSE) file for details.

<br/>

---

<div align="center">

**Built with ❤️ by the KillTheHost team**

[killthehost.com](https://killthehost.com) &nbsp;·&nbsp; [Report a Bug](https://github.com/killthehost/killthehost/issues) &nbsp;·&nbsp; [Request a Feature](https://github.com/killthehost/killthehost/issues)

<br/>

*Stop treating localhost like a dead end.*

</div>

Copyright (c) 2026 KillTheHost - Developed by PhDesigns, LLC.
