# Nexora IoT Connectivity Platform

A responsive front-end prototype for managing IoT SIM, iSIM and eSIM fleets, customers, analytics, reports and international roaming.

## Run locally

The dashboard now runs from the API itself, so a single command gives you one
URL for both. From the repository root:

```powershell
.\start-local.ps1              # starts the API + dashboard on http://127.0.0.1:8000
.\start-local.ps1 -NoBrowser   # start without opening a browser
.\start-local.ps1 -Stop        # stop it again
```

Then open <http://127.0.0.1:8000> and sign in with `admin@raw-switch.io` / `admin123`.

You can still open `index.html` from a plain static server or the file system:
`app.js` resolves the API base URL on its own (same origin when the API serves the
page, `http://<host>:8000` on localhost or a private LAN, the production API
otherwise). Set `window.RAW_SWITCH_API` or a `<meta name="raw-switch-api" content="…">`
tag to force a specific backend. An internet connection is used for Google Fonts
and Lucide icons; the dashboard remains functional without them.

### Verify the dashboard against the API

`validate_sim_ui.py` (repository root) drives headless Chrome through the real
interface — sign-in, SIM studio, eSIM provisioning, activation code and QR — and
fails if the page logs any JavaScript error. It needs `pip install websocket-client`
and Chrome:

```powershell
python validate_sim_ui.py          # 16 checks, 16 passed
```

## VPS deployment (45.159.228.52)

### Files included
- `deploy.ps1` — PowerShell deploy script (run locally on Windows)
- `raw-switch.service` — systemd unit for persistent background operation
- `nginx-raw-switch.conf` — optional Nginx site config

### Quick deploy from Windows

1. Ensure OpenSSH Client is installed (Windows 10/11 includes it by default).
2. Run the deploy script:

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
.\deploy.ps1
```

The script will:
- Upload `raw-switch-iot.zip` to `/tmp` on the VPS
- Create `/var/www/raw-switch-iot` and extract the app
- Install and start a `raw-switch.service` systemd unit on port 80
- Open firewall port 80 (ufw/firewalld if present)

### Access

After deployment, open: `http://45.159.228.52`

### Manual SSH deploy

```bash
ssh -p 22 root@45.159.228.52
mkdir -p /var/www/raw-switch-iot
cd /var/www/raw-switch-iot
# upload files via SFTP/SCP, then:
python3 -m http.server 80 --bind 0.0.0.0
```

Or run as a systemd service using `raw-switch.service`.

## Included

- Fleet overview with usage charts and SIM status analytics
- Searchable/filterable SIM inventory and status controls
- SIM format workspace with live counts and one-click views for physical SIMs, eSIM profiles, iSIM and SoftSIM
- IoT SIM, iSIM and eSIM provisioning forms
- Customer profiles and fleet drill-down
- Administrator SIM & data management shows package usage, allocation and remaining data across all customer accounts; customer users remain restricted to their own fleet
- Administrator customer cards show each customer's package allocation, usage and remaining data
- Administrator workflow for creating a customer and its Customer Admin account together
- Shared backend authentication for administrator, user and customer accounts across browsers and devices
- Customer cards include administrator-only credential management for customer admin name, email, password, status and permissions
- Administrator Settings controls for adding users, creating reusable roles, and assigning checkbox-based permissions
- Administrator company branding setup for name, browsed logo image, logo removal, standard sizing, primary color, and navigation color
- Global roaming policies and regional network insights
- CSV report downloads
- Responsive desktop, tablet and mobile layouts

## Production integration

This version uses in-browser sample data. Real carrier provisioning, eSIM profile delivery, roaming steering, persistent accounts, authentication and live analytics require backend integrations with your connectivity provider and a database.
