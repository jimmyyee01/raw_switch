import aiohttp
import asyncio
import json
from sqlalchemy.orm import Session

from .config import MIN_BANDWIDTH_MBPS, MAX_LATENCY_MS, get_carrier_key
from .models import Carrier, Device, NetworkEvent
from .database import SessionLocal


async def fetch_carrier_status(carrier: Carrier, session: aiohttp.ClientSession) -> dict:
    """Fetch current status from a carrier's real API endpoint."""
    if not carrier.is_active:
        return {"status": "inactive", "bandwidth_mbps": 0, "latency_ms": 999, "rssi": -100}

    api_key = get_carrier_key(carrier.name)
    if not api_key:
        return {"status": "unauthenticated", "bandwidth_mbps": 0, "latency_ms": 999, "rssi": -100, "authenticated": False}

    # Build the status endpoint from the carrier's configured API endpoint
    base_url = carrier.api_endpoint.rstrip("/")
    status_path = "/status"
    url = f"{base_url}{status_path}"

    headers = {}
    if carrier.api_key:
        headers["Authorization"] = f"Bearer {carrier.api_key}"
    elif api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        headers["X-API-Key"] = "placeholder"

    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return {
                    "bandwidth_mbps": data.get("bandwidth_mbps", 0),
                    "latency_ms": data.get("latency_ms", 999),
                    "rssi": data.get("rssi", -100),
                    "status": data.get("status", "connected"),
                    "authenticated": True,
                }
            elif resp.status == 401:
                return {"status": "unauthorized", "bandwidth_mbps": 0, "latency_ms": 999, "rssi": -100, "authenticated": False}
            else:
                return {"status": f"http_{resp.status}", "bandwidth_mbps": 0, "latency_ms": 999, "rssi": -100, "authenticated": True}
    except asyncio.TimeoutError:
        return {"status": "timeout", "bandwidth_mbps": 0, "latency_ms": 999, "rssi": -100, "authenticated": True}
    except aiohttp.ClientError as e:
        return {"status": f"error:{e}", "bandwidth_mbps": 0, "latency_ms": 999, "rssi": -100, "authenticated": True}



from sqlalchemy.orm import Session

from .config import MIN_BANDWIDTH_MBPS, MAX_LATENCY_MS, get_carrier_key
from .models import Carrier, Device, NetworkEvent
from .database import SessionLocal


async def fetch_carrier_status(carrier: Carrier, session: aiohttp.ClientSession) -> dict:
    """Fetch current status from a carrier's real API endpoint."""
    if not carrier.is_active:
        return {"status": "inactive", "bandwidth_mbps": 0, "latency_ms": 999, "rssi": -100}

    api_key = get_carrier_key(carrier.name)
    if not api_key:
        return {"status": "unauthenticated", "bandwidth_mbps": 0, "latency_ms": 999, "rssi": -100, "authenticated": False}

    # Build the status endpoint from the carrier's configured API endpoint
    base_url = carrier.api_endpoint.rstrip("/")
    status_path = "/status"
    url = f"{base_url}{status_path}"

    headers = {}
    if carrier.api_key:
        headers["Authorization"] = f"Bearer {carrier.api_key}"
    elif api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        headers["X-API-Key"] = "placeholder"

    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.json()
                return {
                    "bandwidth_mbps": data.get("bandwidth_mbps", 0),
                    "latency_ms": data.get("latency_ms", 999),
                    "rssi": data.get("rssi", -100),
                    "status": data.get("status", "connected"),
                    "authenticated": True,
                }
            elif resp.status == 401:
                return {"status": "unauthorized", "bandwidth_mbps": 0, "latency_ms": 999, "rssi": -100, "authenticated": False}
            else:
                return {"status": f"http_{resp.status}", "bandwidth_mbps": 0, "latency_ms": 999, "rssi": -100, "authenticated": True}
    except asyncio.TimeoutError:
        return {"status": "timeout", "bandwidth_mbps": 0, "latency_ms": 999, "rssi": -100, "authenticated": True}
    except aiohttp.ClientError as e:
        return {"status": f"error:{e}", "bandwidth_mbps": 0, "latency_ms": 999, "rssi": -100, "authenticated": True}


async def select_best_carrier(device: Device, session: aiohttp.ClientSession) -> Carrier | None:
    """Select the best carrier based on bandwidth, latency, and reliability.

    Queries the database for all active carriers, prefers those that share the
    device's telecom network, fetches their real-time status, and picks the
    highest-scoring carrier.

    Returns:
        The best Carrier object, or None if no suitable carrier found.
    """
    # Prefer carriers on the device's telecom network, fall back to all carriers
    if device.telecom_network_id:
        with SessionLocal() as db:
            carriers = db.query(Carrier).filter(
                Carrier.telecom_network_id == device.telecom_network_id,
                Carrier.is_active == True,
            ).all()
    else:
        with SessionLocal() as db:
            carriers = db.query(Carrier).filter(Carrier.is_active == True).all()

    if not carriers:
        return None

    best_carrier = None
    best_score = -float("inf")

    for carrier in carriers:
        status = await fetch_carrier_status(carrier, session)

        # Skip unavailable/unreachable carriers
        if status.get("status") == "unauthorized":
            continue
        if status.get("status") == "inactive":
            continue
        if status.get("status") == "timeout":
            continue
        if status.get("status") == "error":
            continue

        # Score: weighted combination of bandwidth, latency, and reliability
        bandwidth = status.get("bandwidth_mbps", 0)
        latency = status.get("latency_ms", 999)
        reliability = carrier.reliability_score if carrier.reliability_score else 1.0

        # Normalize to 0-1 scale
        bw_score = min(bandwidth / float(MIN_BANDWIDTH_MBPS), 1.0) if MIN_BANDWIDTH_MBPS > 0 else 0.0
        lat_score = 1.0 - min(latency / float(MAX_LATENCY_MS), 1.0)

        score = (0.5 * bw_score) + (0.3 * lat_score) + (0.2 * reliability)

        # Penalize carriers with health check failures
        if status.get("status") in ("http_500", "http_502", "http_503", "timeout", "error"):
            score -= 0.5

        if score > best_score:
            best_score = score
            best_carrier = carrier

    return best_carrier


async def switch_network(device_id: int, target_carrier: Carrier, db_session: Session) -> NetworkEvent:
    """Initiate network switch for a device to the target carrier.

    Persists a switching event, then pushes the new carrier profile to the
    carrier's provisioning API. Returns the updated NetworkEvent record.

    Returns:
        NetworkEvent with status reflecting switch outcome.
    """
    device = db_session.query(Device).filter(Device.id == device_id).first()
    if not device:
        raise ValueError(f"Device {device_id} not found")

    # Record the switching attempt
    event = NetworkEvent(
        device_id=device_id,
        network_name=target_carrier.name,
        bandwidth_mbps=target_carrier.bandwidth_mbps,
        latency_ms=100,
        status="switching",
    )
    db_session.add(event)
    db_session.commit()
    db_session.refresh(event)

    # Push the carrier profile to the carrier's provisioning API
    profile_status = await _push_carrier_profile(device, target_carrier)
    event.network_name = f"{target_carrier.name} (profile pushed)" if profile_status else f"{target_carrier.name} (profile push failed)"
    event.status = "profile_sent" if profile_status else "profile_failed"
    db_session.commit()
    db_session.refresh(event)

    return event


async def _push_carrier_profile(device: Device, target_carrier: Carrier) -> bool:
    """Push the new carrier profile to the carrier's provisioning API.

    Builds the profile payload from the device's current configuration and
    sends it to the carrier's /profile/push endpoint. Returns True if the
    carrier accepted the profile.

    Returns:
        True if the carrier accepted the profile push, False otherwise.
    """
    # Prefer carrier's own API key, fall back to global config lookup
    api_key = target_carrier.api_key or get_carrier_key(target_carrier.name)

    if not api_key:
        return False

    # Build the profile payload
    profile_payload = {
        "device_id": str(device.id),
        "device_name": device.device_name,
        "iccid": device.iccid,
        "carrier": target_carrier.name,
        "msisdn": device.msisdn or "+15550000000",
        "apn": device.apn or "default",
        "bandwidth_mbps": target_carrier.bandwidth_mbps,
        "reliability_score": target_carrier.reliability_score,
    }

    # Build the endpoint from the carrier's configured API endpoint
    base_url = target_carrier.api_endpoint.rstrip("/")
    push_path = "/profile/push"
    url = f"{base_url}{push_path}"

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    # Auth: Bearer token or X-API-Key
    if target_carrier.api_key:
        headers["Authorization"] = f"Bearer {target_carrier.api_key}"
    else:
        headers["X-API-Key"] = api_key

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                headers=headers,
                data=json.dumps(profile_payload),
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                return resp.status in (200, 201, 202)
    except (asyncio.TimeoutError, aiohttp.ClientError):
        return False