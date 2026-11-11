# RAW-Switch — IoT SIM / iSIM / eSIM connectivity platform

RAW-Switch is a self-hosted connectivity operations platform for IoT fleets. It manages
removable IoT SIMs, soldered iSIMs and remote-provisioned eSIMs, prices metered data,
invoices customers, and settles payments through Pesapal.

## Repository layout

| Path | What it is |
|---|---|
| `sim_aggregator/` | FastAPI backend, SQLite/SQLAlchemy data layer, billing, auth, alerts, email, TLS launchers |
| `nexora-iot/` | The RAW-Switch dashboard (vanilla JS, served by the backend at `/`) |

## Features

- **SIM inventory** — IoT SIM / iSIM / eSIM provisioning with GSMA-style ICCID, IMSI, EID,
  activation codes (LPA), PIN/PUK, eUICC and SM-DP+ metadata.
- **Lifecycle** — inventory → active → suspended → terminated, with guards and audit trail.
- **Network marketplace** — carriers with coverage and links to one or more local or
  international network API providers.
- **Network APIs** — local and international provider registry, connectivity tests,
  SIM attach/bind, eUICC registry and GSMA RSP operations.
- **Metered billing** — per-MB rate cards, usage metering, invoices with optional platform
  fee and tax percentages, partial payments, voids and payment history.
- **Pesapal payments** — simulated mode for local testing, sandbox/live for production,
  IPN/callback handling and a payment outbox.
- **Alert email** — usage, invoice and payment alerts emailed to admins and customers
  (SMTP or simulated outbox).
- **Admin tools** — activity logs, trash with restore, user/role management and
  customer access permissions.
- **Security** — bearer sessions delivered as HttpOnly/Secure/SameSite cookies, central
  authorization policy, customer data scoping, login throttling with a human check,
  optional TOTP two-factor authentication and password reset.

## Quick start (Windows)

```powershell
cd sim_aggregator
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt

# HTTP (development)
.\start_api.cmd
# -> http://127.0.0.1:8000

# HTTPS (self-signed development certificate)
.\start_https.ps1
# -> https://127.0.0.1:8443
```

Default demo accounts (change these before any real use):

| Account | Email | Password |
|---|---|---|
| Administrator | `admin@raw-switch.io` | `admin123` |
| Customer | `customer@raw-switch.io` | `customer123` |

## Configuration

Environment variables:

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | SQLAlchemy URL (default `sqlite:///./sim_aggregator.db`) |
| `UI_DIR` | Dashboard folder to serve (defaults to `../nexora-iot`) |
| `PUBLIC_BASE_URL` | Public https origin used in emails and payment redirects |
| `COOKIE_SECURE` | Force the `Secure` cookie flag |
| `FORCE_HTTPS` | Redirect plain HTTP to HTTPS |
| `SESSION_TTL_HOURS` | Login session lifetime (default 12) |
| `APP_ENV` | Set to `production` to refuse the self-signed certificate |
| `PESAPAL_ENV`, `PESAPAL_CONSUMER_KEY`, `PESAPAL_CONSUMER_SECRET`, `PESAPAL_CURRENCY`, `PESAPAL_FX_RATE` | Payment gateway |

## HTTPS and reverse proxy

- `TLS-SETUP.md` covers CA-issued certificates (Let's Encrypt) and nginx termination.
- `start_https.ps1 -Cert ... -Key ...` serves TLS directly and refuses the dev certificate
  when `APP_ENV=production`.
- `start_behind_proxy.ps1` runs behind nginx with `FORCE_HTTPS=1` and trusted proxy headers.

## Repository hygiene

The following are intentionally **not** committed: `.venv/`, `certs/`, `*.pem`, `*.key`,
`*.db`, `server.log`, `.env`. See `sim_aggregator/.gitignore`.

## Tests

The API was exercised with integration probes covering authentication/scoping, billing,
trash/logs/email, edge cases and two-factor authentication. Probe scripts are kept outside
this repository because they reference local database paths.

## License

No license file is included; all rights reserved unless stated otherwise.
