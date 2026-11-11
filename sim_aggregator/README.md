# Multi-Carrier SIM Aggregator

A platform that aggregates global multi-carrier coverage onto a single SIM, providing a modern dashboard, robust REST APIs, and automated network switching for high-bandwidth / high-reliability use cases.

## Architecture

- **Backend**: FastAPI (Python) with SQLAlchemy ORM, SQLite (replaceable with PostgreSQL).
- **Carrier Adapters**: Async HTTP clients (`aiohttp`) per carrier API to fetch status (bandwidth, latency, RSSI).
- **Network Switching Engine**: Evaluates carriers based on weighted score (bandwidth, latency, reliability) and triggers provisioning via carrier APIs.
- **Database**: Devices (ICCAR, carrier assignment), Carriers, Network events history.
- **Containerisation**: Dockerised for reproducible deployment.

## API Highlights

| Endpoint | Description |
|---|---|
| `POST /devices/` | Provision a new SIM device (ICCAR). |
| `GET /devices/{iccid}/status` | Return current network status and last event. |
| `POST /devices/{iccid}/switch` | Trigger automated carrier switch. |
| `GET /carriers/` | List known carriers with metrics. |
| `POST /esim/provision` | Create / retrieve a GSMA‑RSP profile for a device (ICCAR). Returns the profile dict and activation info. |
| `GET /esim/profiles` | List all stored eSIM profiles with ICCAR, carrier, activation / de‑activation timestamps. |
| `POST /esim/deactivate` | Mark a device’s eSIM profile as deactivated. |
| `GET /inventory` | List SIM, iSIM, and eSIM inventory records. |
| `POST /inventory` | Provision an inventory record with format, plan, customer, and roaming settings. |
| `POST /inventory/bulk` | Provision up to 500 SIM records in one request with per-row results. |
| `GET /system/health` | Return safe backend, database, inventory, RSP, and recent network-event metrics for administrators. |
| `GET/PATCH /roaming/policies` | Persist and enforce roaming policy state across existing and newly provisioned SIMs. |
| `PATCH /inventory/{iccid}` | Update status, assignment, plan, roaming, or usage fields. |
| `POST /inventory/{iccid}/usage` | Record data usage in MB and return over-limit state. |
| `GET/POST/PATCH /telecom/networks` | Configure local telecom network API connections without exposing stored API keys. |
| `POST /telecom/networks/{id}/test` | Verify a configured provider's `/health` endpoint. |
| `POST /telecom/networks/{id}/bind` | Persist a SIM-to-local-network association and record a connected network event. |
| `GET/POST /euicc` | Register and list eUICC EIDs and their SM-DP+ metadata. |
| `GET /rsp/operations` | List remote SIM provisioning operations and provider results. |
| `POST /rsp/download` | Submit an eUICC profile download to a configured provider's `/rsp/profiles/download` endpoint. |
| `GET/POST/PATCH /rsp/providers` | Admin-managed SGP.22/SGP.32 provider adapter registry with credential references and documentation links. |

## Switching Logic

1. Periodically (or on-demand) query each carrier's status API.
2. Compute a score: `weight_bandwidth * (bandwidth / max_bandwidth) - weight_latency * (latency / max_latency) + weight_reliability * reliability_score`.
3. Select the highest‑scoring carrier that is currently `connected`.
4. Issue a provisioning call to push the new carrier profile to the SIM.
5. Log a `NetworkEvent` with details.

## Deployment

```bash
# Build
docker build -t sim-aggregator .

# Run
docker run -d -p 8000:8000 -e CARRIER_A_KEY=... -e CARRIER_B_KEY=... -e CARRIER_C_KEY=... sim-aggregator
```

## Running it with the dashboard

The API can serve the dashboard itself, so one process and one URL cover both
halves. From the repository root:

```powershell
.\start-local.ps1              # http://127.0.0.1:8000 — dashboard + API + /docs
.\start-local.ps1 -Stop        # stop the background server
```

```bash
# equivalent, without the helper script
python -m uvicorn sim_aggregator.main:app --host 127.0.0.1 --port 8000
```

The static dashboard mount is registered **after** every API route (a
`StaticFiles(html=True)` mount at `/` shadows routes registered later). It looks
for the `nexora-iot` folder next to `sim_aggregator` and `/var/www/raw-switch-iot`,
and `UI_DIR` overrides the location:

```bash
UI_DIR=/var/www/raw-switch-iot uvicorn main:app --host 0.0.0.0 --port 8000
```

### Verification probes

| Script | Proves |
|---|---|
| `validate_sim_lifecycle.py` (root) | 48 API checks: provisioning for all three card families, metering, suspend/resume, credential re-issue, QR SVG, termination, guard rails |
| `validate_sim_ui.py` (root) | 16 browser checks: sign-in, SIM studio card types and fleet counts, eSIM provisioning through the wizard, activation code, rendered install QR, SIM table parity, zero JS errors |

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `sqlite:///./sim_aggregator.db` | DB connection string. |
| `UI_DIR` | `../nexora-iot`, then `/var/www/raw-switch-iot` | Dashboard folder served at `/`. |
| `MIN_BANDWIDTH_MBPS` | `10` | Minimum bandwidth threshold for switch. |
| `MAX_LATENCY_MS` | `100` | Maximum acceptable latency. |
| `FAILURE_THRESHOLD` | `3` | Consecutive failures before marking carrier unhealthy. |
| `CARRIER_A_KEY`, `CARRIER_B_KEY`, `CARRIER_C_KEY` | – | API keys for each carrier. |

## Roadmap

- Replace SQLite with PostgreSQL + Alembic migrations.
- Add a React/Redash dashboard for real‑time carrier health, device map, and switch history.
- Implement token‑based auth (OAuth2 / JWT) for API consumers.
- Add webhook notifications for carrier events.
- Support eSIM/remote SIM provisioning standards (GSMA Remote SIM Provisioning).
- Add provider-specific GSMA SGP.22/SGP.32 adapters and signed production profile payloads.

## RSP provider credentials

The provider registry stores a credential reference, not a secret. Select one of:

- `environment`: `credentialRef` names an environment variable configured on the VPS.
- `vault`: `credentialRef` identifies a secret in the organization's external vault adapter.
- `database_encrypted`: `credentialRef` identifies an encrypted secret managed by the deployment's key-management layer.

The generic `provider_http` adapter is deliberately transport-only. Certified operator adapters must implement the provider's SGP.22 or SGP.32 authentication, encryption, profile package signing, and lifecycle requirements. Do not generate or store Ki/OPc or production profile keys in the browser or source repository.