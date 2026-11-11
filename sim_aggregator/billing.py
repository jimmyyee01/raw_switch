"""Pay-per-data billing and Pesapal payments for RAW-Switch.

The commercial model here is metered: every megabyte a SIM moves is priced
against a rate-card row (``data_rates``) and stored as a ``usage_records`` row.
Invoices aggregate the unbilled usage of one customer for one period, and are
settled through Pesapal API 3.0 (auth -> SubmitOrderRequest -> IPN/callback ->
GetTransactionStatus).

``environment`` on the gateway row selects the transport:

* ``simulated`` - no credentials needed. SubmitOrderRequest is answered locally
  with a payment page served by this API, so the whole flow (create order ->
  pay -> IPN -> invoice marked paid) is testable without a merchant contract.
  Card/phone data never leaves the box.
* ``sandbox``   - https://cybqa.pesapal.com/pesapalv3 with Pesapal demo keys.
* ``live``      - https://pay.pesapal.com/v3 with production keys.

Switching to real money is a settings change (PATCH /billing/gateway), never a
code change.
"""

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import html
import json
import math
import os
import secrets
import time
from urllib.parse import parse_qs
import aiohttp

from .config import SessionLocal
from .audit import audit as _audit
from .authz import customer_scope, is_customer
from .models import (
    Customer,
    DataRate,
    Device,
    Invoice,
    InvoiceLine,
    Payment,
    PaymentGateway,
    UsageRecord,
)

router = APIRouter(prefix="/billing", tags=["billing"])

PESAPAL_BASES = {
    "sandbox": "https://cybqa.pesapal.com/pesapalv3",
    "live": "https://pay.pesapal.com/v3",
}

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
LEGACY_PUBLIC_BASE = "http://45.159.228.52:8000"


def _public_base(request: Request | None = None) -> str:
    """Base URL used in hosted payment links.

    An explicit PUBLIC_BASE_URL wins; otherwise the caller's own host is used
    so local and staging deployments never redirect payers to production.
    """
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    if request is not None:
        return str(request.base_url).rstrip("/")
    return "http://127.0.0.1:8000"


def _public_url(request: Request | None, value: str | None, path: str) -> str:
    """Keep an explicitly configured URL, otherwise build one on this host."""
    if value and not value.startswith((LEGACY_PUBLIC_BASE, "http://127.0.0.1", "http://localhost")):
        return value
    return _public_base(request) + path
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://45.159.228.52").rstrip("/") + "/"
PAYMENT_CURRENCIES = ["USD", "KES", "UGX", "TZS", "GBP", "EUR"]

# Fallback tariff used when the rate card is empty or a row is disabled.
FALLBACK_RATE = {"price_per_mb": 0.0025, "block_mb": 0.01, "min_charge": 0.01, "currency": "USD"}

# Seeded on first boot so the console has a usable per-MB price list.
RATE_CATALOG_SEED = [
    ("Standard metered", None, 0.0025, 0.01, 0.01, "Default per-MB tariff for any SIM without a dedicated plan rate"),
    ("Light sensor 500 MB", "500 MB / month", 0.0016, 0.01, 0.01, "Low-bandwidth sensors and pulse meters"),
    ("Tracker 1 GB", "1 GB / month", 0.0021, 0.01, 0.01, "Vehicle and asset trackers"),
    ("Gateway 5 GB", "5 GB / month", 0.0014, 0.01, 0.01, "Gateways, cameras and telemetry hubs"),
    ("Pooled data", "Pooled data", 0.0010, 0.01, 0.01, "Shared pool, invoiced per MB across the account"),
]

# Pesapal bearer tokens live 5 minutes; refresh a little earlier than that.
_TOKEN_CACHE: dict = {}


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _now() -> datetime:
    return datetime.utcnow()


def _round_money(value: float) -> float:
    return round(float(value or 0) + 0.0, 2)


def _iso(value) -> str | None:
    if not value:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)


def _month_start(now: datetime | None = None) -> datetime:
    now = now or _now()
    return datetime(now.year, now.month, 1)


# --------------------------------------------------------------------------- payloads


def _rate_payload(rate: DataRate) -> dict:
    return {
        "id": rate.id,
        "name": rate.name,
        "currency": rate.currency,
        "pricePerMb": float(rate.price_per_mb or 0),
        "pricePerGb": round(float(rate.price_per_mb or 0) * 1024, 2),
        "blockMb": float(rate.block_mb or 0),
        "minCharge": float(rate.min_charge or 0),
        "planName": rate.plan_name,
        "description": rate.description,
        "active": bool(rate.is_active),
        "createdAt": _iso(rate.created_at),
    }


def _usage_payload(record: UsageRecord) -> dict:
    return {
        "id": record.id,
        "iccid": record.iccid,
        "device": record.device_name,
        "customer": record.customer,
        "network": record.network,
        "megabytes": round(float(record.megabytes or 0), 3),
        "billableMb": round(float(record.billable_mb or 0), 3),
        "rateName": record.rate_name,
        "pricePerMb": float(record.price_per_mb or 0),
        "amount": _round_money(record.amount),
        "currency": record.currency,
        "source": record.source,
        "invoiceId": record.invoice_id,
        "recordedAt": _iso(record.recorded_at),
    }


def _payment_payload(payment: Payment) -> dict:
    return {
        "id": payment.id,
        "reference": payment.reference,
        "invoiceId": payment.invoice_id,
        "customer": payment.customer,
        "purpose": payment.purpose,
        "amount": _round_money(payment.amount),
        "amountLocal": _round_money(payment.amount_local),
        "currency": payment.currency,
        "provider": payment.provider,
        "mode": payment.mode,
        "status": payment.status,
        "payerName": payment.payer_name,
        "payerEmail": payment.payer_email,
        "payerPhone": payment.payer_phone,
        "description": payment.description,
        "orderTrackingId": payment.order_tracking_id,
        "merchantReference": payment.merchant_reference,
        "redirectUrl": payment.redirect_url,
        "confirmationCode": payment.confirmation_code,
        "paymentMethod": payment.payment_method,
        "statusMessage": payment.status_message,
        "createdAt": _iso(payment.created_at),
        "completedAt": _iso(payment.completed_at),
    }


def _invoice_payload(invoice: Invoice, lines: list | None = None, payments: list | None = None) -> dict:
    payload = {
        "id": invoice.id,
        "number": invoice.number,
        "customer": invoice.customer,
        "periodStart": invoice.period_start,
        "periodEnd": invoice.period_end,
        "usageMb": round(float(invoice.usage_mb or 0), 3),
        "subtotal": _round_money(invoice.subtotal),
        "platformFeePct": float(invoice.platform_fee_pct or 0),
        "platformFee": _round_money(invoice.platform_fee),
        "taxPct": float(invoice.tax_pct or 0),
        "tax": _round_money(invoice.tax),
        "total": _round_money(invoice.total),
        "paidTotal": _round_money(invoice.paid_total),
        "balance": _round_money(max(0.0, float(invoice.total or 0) - float(invoice.paid_total or 0))),
        "currency": invoice.currency,
        "status": invoice.status,
        "notes": invoice.notes,
        "issuedAt": _iso(invoice.issued_at),
        "dueAt": _iso(invoice.due_at),
        "paidAt": _iso(invoice.paid_at),
    }
    if lines is not None:
        payload["lines"] = [
            {
                "description": line.description,
                "rateName": line.rate_name,
                "quantityMb": round(float(line.quantity_mb or 0), 3),
                "pricePerMb": float(line.price_per_mb or 0),
                "amount": _round_money(line.amount),
            }
            for line in lines
        ]
    if payments is not None:
        payload["payments"] = [_payment_payload(payment) for payment in payments]
    return payload


def _mask(secret: str | None) -> str | None:
    if not secret:
        return None
    text = str(secret)
    return text[:4] + "*" * max(0, len(text) - 8) + text[-4:] if len(text) > 8 else "*" * len(text)


def _gateway(db: Session) -> PaymentGateway:
    gateway = db.query(PaymentGateway).filter(PaymentGateway.provider == "pesapal").first()
    if gateway:
        return gateway
    gateway = PaymentGateway(
        provider="pesapal",
        environment=os.getenv("PESAPAL_ENV", "simulated"),
        consumer_key=os.getenv("PESAPAL_CONSUMER_KEY") or None,
        consumer_secret=os.getenv("PESAPAL_CONSUMER_SECRET") or None,
        currency=os.getenv("PESAPAL_CURRENCY", "USD"),
        ipn_url=None,
        callback_url=None,
        fx_rate=float(os.getenv("PESAPAL_FX_RATE", "1") or 1),
    )
    db.add(gateway)
    db.commit()
    db.refresh(gateway)
    return gateway


def _gateway_payload(db: Session, gateway: PaymentGateway, request: Request | None = None) -> dict:
    configured = bool(gateway.consumer_key and gateway.consumer_secret)
    return {
        "provider": gateway.provider,
        "environment": gateway.environment,
        "mode": gateway.environment,
        "consumerKeyMasked": _mask(gateway.consumer_key),
        "consumerSecretMasked": _mask(gateway.consumer_secret),
        "hasCredentials": configured,
        "ipnId": gateway.ipn_id,
        "ipnUrl": _public_url(request, gateway.ipn_url, "/billing/pesapal/ipn"),
        "callbackUrl": _public_url(request, gateway.callback_url, "/billing/pesapal/callback"),
        "currency": gateway.currency,
        "fxRate": float(gateway.fx_rate or 1),
        "currencies": PAYMENT_CURRENCIES,
        "active": bool(gateway.is_active),
        "apiBase": PESAPAL_BASES.get(gateway.environment, "local simulated gateway"),
        "pendingPayments": db.query(Payment).filter(Payment.status == "pending").count(),
        "updatedAt": _iso(gateway.updated_at),
    }


# --------------------------------------------------------------------------- pricing


def _rate_for(db: Session, device: Device, rate_id=None) -> DataRate | None:
    """Resolve the tariff for a SIM: explicit row, then its plan binding, then default."""
    if rate_id:
        chosen = db.query(DataRate).filter(DataRate.id == int(rate_id)).first()
        if chosen:
            return chosen
    plan_name = str(getattr(device, "data_plan", "") or "").strip()
    if plan_name:
        bound = (
            db.query(DataRate)
            .filter(func.lower(DataRate.plan_name) == plan_name.lower(), DataRate.is_active.is_(True))
            .order_by(DataRate.id)
            .first()
        )
        if bound:
            return bound
    return (
        db.query(DataRate)
        .filter(DataRate.plan_name.is_(None), DataRate.is_active.is_(True))
        .order_by(DataRate.id)
        .first()
    )


def _price(megabytes: float, rate: DataRate | None) -> tuple[float, float, float, str]:
    """Price a data volume per MB.

    Returns ``(billable_mb, amount, price_per_mb, currency)``. Volume is rounded
    up to the rate's metering block, then multiplied by the per-MB price; any
    non-zero session is floored at the rate's minimum charge.
    """
    volume = max(0.0, float(megabytes or 0))
    price_per_mb = float(rate.price_per_mb) if rate else FALLBACK_RATE["price_per_mb"]
    block = float(rate.block_mb or 0) if rate else FALLBACK_RATE["block_mb"]
    min_charge = float(rate.min_charge or 0) if rate else FALLBACK_RATE["min_charge"]
    currency = (rate.currency if rate else FALLBACK_RATE["currency"]) or "USD"
    if volume <= 0:
        return 0.0, 0.0, price_per_mb, currency
    if block > 0:
        billable = round(math.ceil(volume / block) * block, 6)
    else:
        billable = round(volume, 6)
    amount = max(billable * price_per_mb, min_charge)
    return billable, round(amount, 4), price_per_mb, currency


def _meter(
    db: Session,
    device: Device,
    megabytes: float,
    network: str | None = None,
    rate_id=None,
    source: str = "api",
) -> UsageRecord:
    """Price and store one metered usage event, and roll the SIM usage counter."""
    rate = _rate_for(db, device, rate_id)
    billable, amount, price_per_mb, currency = _price(megabytes, rate)
    record = UsageRecord(
        device_id=device.id,
        iccid=device.iccid,
        customer=(device.customer or "Unassigned"),
        device_name=device.device_name,
        network=network or (device.telecom_network.name if device.telecom_network else None) or device.network_tech,
        megabytes=round(float(megabytes or 0), 3),
        billable_mb=billable,
        rate_id=rate.id if rate else None,
        rate_name=rate.name if rate else "Default per-MB tariff",
        price_per_mb=price_per_mb,
        amount=amount,
        currency=currency,
        source=source,
        recorded_at=_now(),
    )
    db.add(record)
    device.usage_mb = round(float(device.usage_mb or 0) + float(megabytes or 0), 3)
    return record


def _invoice_number(db: Session) -> str:
    year = _now().year
    prefix = f"INV-{year}-"
    rows = db.query(Invoice.number).filter(Invoice.number.like(prefix + "%")).all()
    highest = 0
    for (value,) in rows:
        tail = str(value or "").rsplit("-", 1)[-1]
        if tail.isdigit():
            highest = max(highest, int(tail))
    return f"{prefix}{highest + 1:04d}"


def _payment_reference(db: Session) -> str:
    """Pesapal merchant reference: alphanumeric plus dashes, max 50 chars, unique."""
    stamp = _now().strftime("%Y%m%d")
    for _ in range(20):
        candidate = f"PAY-{stamp}-{secrets.token_hex(3).upper()}"
        if not db.query(Payment).filter(Payment.reference == candidate).first():
            return candidate
    return f"PAY-{stamp}-{secrets.token_hex(6).upper()}"


def _recompute_invoice(db: Session, invoice: Invoice) -> Invoice:
    """Re-derive paid totals and status from the completed payments on an invoice."""
    collected = (
        db.query(func.coalesce(func.sum(Payment.amount), 0.0))
        .filter(Payment.invoice_id == invoice.id, Payment.status == "completed")
        .scalar()
        or 0.0
    )
    invoice.paid_total = _round_money(collected)
    if invoice.status == "void":
        db.commit()
        return invoice
    total = float(invoice.total or 0)
    if total > 0 and invoice.paid_total >= round(total - 0.01, 2):
        invoice.status = "paid"
        invoice.paid_at = invoice.paid_at or _now()
    elif invoice.paid_total > 0:
        invoice.status = "part_paid"
        invoice.paid_at = None
    else:
        invoice.status = "issued"
        invoice.paid_at = None
    db.commit()
    db.refresh(invoice)
    return invoice


def ensure_catalog(db: Session) -> None:
    """Public entry point used by the app startup hook."""
    _ensure_rate_catalog(db)


def _ensure_rate_catalog(db: Session) -> None:
    """Seed the per-MB price list once so the console is usable on a fresh database."""
    if db.query(DataRate).count():
        return
    for name, plan_name, price_per_mb, block_mb, min_charge, description in RATE_CATALOG_SEED:
        db.add(
            DataRate(
                name=name,
                plan_name=plan_name,
                price_per_mb=price_per_mb,
                block_mb=block_mb,
                min_charge=min_charge,
                currency="USD",
                description=description,
                is_active=True,
            )
        )
    db.commit()


# --------------------------------------------------------------------------- rate card


@router.get("/rates")
async def list_rates(db: Session = Depends(get_db)):
    """Per-MB price list, default tariff first."""
    _ensure_rate_catalog(db)
    rows = db.query(DataRate).order_by(DataRate.plan_name.is_(None).desc(), DataRate.id).all()
    return [_rate_payload(rate) for rate in rows]


@router.post("/rates", status_code=201)
async def create_rate(payload: dict = Body(...), db: Session = Depends(get_db)):
    """Add a per-MB tariff, optionally bound to a sellable plan name."""
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Rate name is required")
    if db.query(DataRate).filter(func.lower(DataRate.name) == name.lower()).first():
        raise HTTPException(status_code=409, detail="A rate with this name already exists")
    try:
        price_per_mb = float(payload.get("pricePerMb", 0))
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail="pricePerMb must be a number") from error
    if price_per_mb < 0:
        raise HTTPException(status_code=422, detail="pricePerMb cannot be negative")
    try:
        block_mb = float(payload.get("blockMb", FALLBACK_RATE["block_mb"]) or 0)
        min_charge = float(payload.get("minCharge", FALLBACK_RATE["min_charge"]) or 0)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail="blockMb and minCharge must be numbers") from error
    currency = str(payload.get("currency") or "USD").upper()
    if currency not in PAYMENT_CURRENCIES:
        raise HTTPException(status_code=422, detail=f"currency must be one of {', '.join(PAYMENT_CURRENCIES)}")
    rate = DataRate(
        name=name,
        currency=currency,
        price_per_mb=price_per_mb,
        block_mb=max(0.0, block_mb),
        min_charge=max(0.0, min_charge),
        plan_name=(str(payload.get("planName") or "").strip() or None),
        description=(str(payload.get("description") or "").strip() or None),
        is_active=bool(payload.get("active", True)),
    )
    db.add(rate)
    db.commit()
    db.refresh(rate)
    return _rate_payload(rate)


@router.patch("/rates/{rate_id}")
async def update_rate(rate_id: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    """Reprice a tariff without a redeploy; historical usage keeps its booked price."""
    rate = db.query(DataRate).filter(DataRate.id == rate_id).first()
    if not rate:
        raise HTTPException(status_code=404, detail="Rate not found")
    if "name" in payload:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="Rate name cannot be empty")
        duplicate = db.query(DataRate).filter(func.lower(DataRate.name) == name.lower(), DataRate.id != rate_id).first()
        if duplicate:
            raise HTTPException(status_code=409, detail="A rate with this name already exists")
        rate.name = name
    try:
        if "pricePerMb" in payload:
            rate.price_per_mb = max(0.0, float(payload.get("pricePerMb") or 0))
        if "blockMb" in payload:
            rate.block_mb = max(0.0, float(payload.get("blockMb") or 0))
        if "minCharge" in payload:
            rate.min_charge = max(0.0, float(payload.get("minCharge") or 0))
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail="pricePerMb, blockMb and minCharge must be numbers") from error
    if "planName" in payload:
        rate.plan_name = str(payload.get("planName") or "").strip() or None
    if "description" in payload:
        rate.description = str(payload.get("description") or "").strip() or None
    if "currency" in payload:
        currency = str(payload.get("currency") or "USD").upper()
        if currency not in PAYMENT_CURRENCIES:
            raise HTTPException(status_code=422, detail=f"currency must be one of {', '.join(PAYMENT_CURRENCIES)}")
        rate.currency = currency
    if "active" in payload:
        rate.is_active = bool(payload.get("active"))
    db.commit()
    db.refresh(rate)
    return _rate_payload(rate)


@router.delete("/rates/{rate_id}")
async def delete_rate(rate_id: int, db: Session = Depends(get_db)):
    """Remove a tariff that was never used; used rates are paused instead of deleted."""
    rate = db.query(DataRate).filter(DataRate.id == rate_id).first()
    if not rate:
        raise HTTPException(status_code=404, detail="Rate not found")
    used = db.query(UsageRecord).filter(UsageRecord.rate_id == rate_id).count()
    if used:
        raise HTTPException(status_code=409, detail=f"{used} metered rows use this rate; pause it instead")
    db.delete(rate)
    db.commit()
    return {"id": rate_id, "deleted": True}


# --------------------------------------------------------------------------- metered usage


def _usage_entries(payload: dict) -> list[dict]:
    """Accept a single metered event or a CDR batch under ``records``."""
    entries = payload.get("records")
    if entries is None:
        entries = [payload]
    if not isinstance(entries, list) or not entries:
        raise HTTPException(status_code=422, detail="Provide a usage reading or a non-empty records list")
    if len(entries) > 500:
        raise HTTPException(status_code=422, detail="Send at most 500 records per request")
    return [entry if isinstance(entry, dict) else {} for entry in entries]


@router.post("/usage", status_code=201)
async def record_usage(payload: dict = Body(...), db: Session = Depends(get_db)):
    """Meter data (MB) for one SIM, or a batch, and price it against the rate card."""
    entries = _usage_entries(payload)
    default_rate_id = payload.get("rateId")
    default_source = str(payload.get("source") or "api").strip().lower() or "api"
    records: list[UsageRecord] = []
    for entry in entries:
        iccid = str(entry.get("iccid") or "").strip()
        if not iccid:
            raise HTTPException(status_code=422, detail="Every usage record needs an iccid")
        device = db.query(Device).filter(Device.iccid == iccid).first()
        if not device:
            raise HTTPException(status_code=404, detail=f"SIM {iccid} not found")
        if str(device.status or "").lower() == "terminated":
            raise HTTPException(status_code=409, detail=f"SIM {iccid} is terminated and cannot report usage")
        try:
            megabytes = float(entry.get("megabytes", entry.get("mb", 0)) or 0)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail="megabytes must be a number") from error
        if megabytes < 0:
            raise HTTPException(status_code=422, detail="megabytes cannot be negative")
        records.append(
            _meter(
                db,
                device,
                megabytes,
                network=str(entry.get("network") or "").strip() or None,
                rate_id=entry.get("rateId") or default_rate_id,
                source=str(entry.get("source") or default_source),
            )
        )
    db.commit()
    for record in records:
        db.refresh(record)
    return {
        "count": len(records),
        "totalMb": round(sum(float(record.megabytes or 0) for record in records), 3),
        "totalAmount": _round_money(sum(float(record.amount or 0) for record in records)),
        "currency": records[0].currency if records else "USD",
        "records": [_usage_payload(record) for record in records],
    }


@router.get("/usage")
async def list_usage(
    customer: str | None = Query(default=None),
    iccid: str | None = Query(default=None),
    invoiced: bool | None = Query(default=None),
    limit: int = Query(default=50),
    db: Session = Depends(get_db),
):
    """Recent metered usage rows, newest first, optionally scoped."""
    if is_customer():
        customer = customer_scope()
    query = db.query(UsageRecord)
    if customer:
        query = query.filter(UsageRecord.customer == customer)
    if iccid:
        query = query.filter(UsageRecord.iccid == iccid)
    if invoiced is True:
        query = query.filter(UsageRecord.invoice_id.isnot(None))
    elif invoiced is False:
        query = query.filter(UsageRecord.invoice_id.is_(None))
    capped = max(1, min(int(limit or 50), 500))
    rows = query.order_by(UsageRecord.recorded_at.desc(), UsageRecord.id.desc()).limit(capped).all()
    return [_usage_payload(row) for row in rows]


@router.post("/usage/simulate", status_code=201)
async def simulate_usage(payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """Meter a data volume on every live SIM (demo/traffic generator)."""
    try:
        megabytes = float(payload.get("megabytes", 25) or 25)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail="megabytes must be a number") from error
    if megabytes <= 0 or megabytes > 102400:
        raise HTTPException(status_code=422, detail="megabytes must be between 0 and 102400")
    customer = str(payload.get("customer") or "").strip()
    query = db.query(Device).filter(func.lower(Device.status) != "terminated")
    if customer:
        query = query.filter(Device.customer == customer)
    devices = query.order_by(Device.id).all()
    if not devices:
        raise HTTPException(status_code=409, detail="No billable SIMs found")
    records = [_meter(db, device, megabytes, source="simulated") for device in devices]
    db.commit()
    return {
        "sims": len(devices),
        "megabytesPerSim": megabytes,
        "totalMb": round(megabytes * len(devices), 3),
        "totalAmount": _round_money(sum(float(record.amount or 0) for record in records)),
        "currency": records[0].currency if records else "USD",
    }


# --------------------------------------------------------------------------- summary


@router.get("/summary")
async def billing_summary(customer: str | None = Query(default=None), db: Session = Depends(get_db)):
    """Pay-per-data position: month-to-date usage, unbilled balance and collections."""
    _ensure_rate_catalog(db)
    if is_customer():
        customer = customer_scope()
    scope = (customer or "").strip() or None
    period_start = _month_start()

    def in_scope(query, model):
        return query.filter(model.customer == scope) if scope else query

    usage_model = UsageRecord
    month_q = in_scope(
        db.query(
            func.coalesce(func.sum(usage_model.megabytes), 0.0),
            func.coalesce(func.sum(usage_model.amount), 0.0),
        ).filter(usage_model.recorded_at >= period_start),
        usage_model,
    )
    metered_mb, metered_amount = month_q.one()
    unbilled_q = in_scope(
        db.query(
            func.coalesce(func.sum(usage_model.megabytes), 0.0),
            func.coalesce(func.sum(usage_model.amount), 0.0),
        ).filter(usage_model.invoice_id.is_(None)),
        usage_model,
    )
    unbilled_mb, unbilled_amount = unbilled_q.one()

    invoice_q = in_scope(db.query(Invoice).filter(Invoice.status != "void"), Invoice)
    invoices = invoice_q.all()
    invoiced = sum(float(row.total or 0) for row in invoices)
    collected = sum(float(row.paid_total or 0) for row in invoices)
    outstanding = sum(max(0.0, float(row.total or 0) - float(row.paid_total or 0)) for row in invoices)

    payment_q = in_scope(db.query(Payment), Payment)
    payments = payment_q.all()
    default_rate = (
        db.query(DataRate)
        .filter(DataRate.plan_name.is_(None), DataRate.is_active.is_(True))
        .order_by(DataRate.id)
        .first()
    )

    customers_query = db.query(usage_model.customer, func.sum(usage_model.megabytes), func.sum(usage_model.amount)).filter(
        usage_model.recorded_at >= period_start
    )
    if scope:
        customers_query = customers_query.filter(usage_model.customer == scope)
    breakdown = []
    for name, mb, amount in customers_query.group_by(usage_model.customer).all():
        owned = db.query(Invoice).filter(Invoice.customer == name, Invoice.status != "void").all()
        breakdown.append(
            {
                "customer": name,
                "usageMb": round(float(mb or 0), 3),
                "usageAmount": _round_money(amount),
                "outstanding": _round_money(
                    sum(max(0.0, float(row.total or 0) - float(row.paid_total or 0)) for row in owned)
                ),
                "sims": db.query(Device).filter(Device.customer == name).count(),
            }
        )
    breakdown.sort(key=lambda row: row["usageAmount"], reverse=True)

    device_q = db.query(Device)
    if scope:
        device_q = device_q.filter(Device.customer == scope)

    return {
        "period": {"start": period_start.date().isoformat(), "end": _now().date().isoformat()},
        "currency": "USD",
        "customer": scope,
        "meteredMb": round(float(metered_mb or 0), 3),
        "meteredAmount": _round_money(metered_amount),
        "unbilledMb": round(float(unbilled_mb or 0), 3),
        "unbilledAmount": _round_money(unbilled_amount),
        "invoiced": _round_money(invoiced),
        "collected": _round_money(collected),
        "outstanding": _round_money(outstanding),
        "invoiceCounts": {
            "issued": sum(1 for row in invoices if row.status == "issued"),
            "partPaid": sum(1 for row in invoices if row.status == "part_paid"),
            "paid": sum(1 for row in invoices if row.status == "paid"),
        },
        "paymentCounts": {
            "pending": sum(1 for row in payments if row.status == "pending"),
            "completed": sum(1 for row in payments if row.status == "completed"),
            "failed": sum(1 for row in payments if row.status in {"failed", "cancelled"}),
        },
        "sims": device_q.count(),
        "billableSims": device_q.filter(func.lower(Device.status) != "terminated").count(),
        "customers": breakdown,
        "defaultRate": _rate_payload(default_rate) if default_rate else None,
        "gateway": _gateway_payload(db, _gateway(db)),
    }


# --------------------------------------------------------------------------- invoices


@router.get("/invoices")
async def list_invoices(
    customer: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50),
    db: Session = Depends(get_db),
):
    """Invoices, newest first, optionally filtered by customer and status."""
    if is_customer():
        customer = customer_scope()
    query = db.query(Invoice)
    if customer:
        query = query.filter(Invoice.customer == customer)
    if status:
        query = query.filter(Invoice.status == status)
    capped = max(1, min(int(limit or 50), 200))
    rows = query.order_by(Invoice.issued_at.desc(), Invoice.id.desc()).limit(capped).all()
    return [_invoice_payload(row) for row in rows]


@router.post("/invoices", status_code=201)
async def create_invoice(payload: dict = Body(...), db: Session = Depends(get_db)):
    """Bill every unbilled metered row of one customer into a pay-per-data invoice."""
    customer = str(payload.get("customer") or "").strip()
    if not customer:
        raise HTTPException(status_code=422, detail="customer is required")
    try:
        tax_pct = float(payload.get("taxPct", 0) or 0)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail="taxPct must be a number") from error
    if tax_pct < 0 or tax_pct > 100:
        raise HTTPException(status_code=422, detail="taxPct must be between 0 and 100")
    try:
        platform_fee_pct = float(payload.get("platformFeePct", 0) or 0)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail="platformFeePct must be a number") from error
    if platform_fee_pct < 0 or platform_fee_pct > 100:
        raise HTTPException(status_code=422, detail="platformFeePct must be between 0 and 100")
    try:
        raw_due = payload.get("dueDays", 7)
        due_days = int(7 if raw_due in (None, "") else raw_due)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail="dueDays must be a whole number") from error
    if due_days < 0:
        raise HTTPException(status_code=422, detail="dueDays cannot be negative")

    unbilled = (
        db.query(UsageRecord)
        .filter(UsageRecord.customer == customer, UsageRecord.invoice_id.is_(None))
        .order_by(UsageRecord.recorded_at)
        .all()
    )
    if not unbilled:
        raise HTTPException(status_code=409, detail=f"No unbilled metered usage for {customer}")

    buckets: dict = {}
    for record in unbilled:
        key = (record.rate_name or "Default per-MB tariff", float(record.price_per_mb or 0), record.currency or "USD")
        bucket = buckets.setdefault(key, {"mb": 0.0, "billable": 0.0, "amount": 0.0, "rows": 0})
        bucket["mb"] += float(record.megabytes or 0)
        bucket["billable"] += float(record.billable_mb or 0)
        bucket["amount"] += float(record.amount or 0)
        bucket["rows"] += 1

    subtotal = _round_money(sum(bucket["amount"] for bucket in buckets.values()))
    # Optional charges: the platform fee applies to data usage, and tax applies to
    # the taxable total (usage + fee). Both percentages default to zero.
    platform_fee = _round_money(subtotal * platform_fee_pct / 100)
    taxable = _round_money(subtotal + platform_fee)
    tax = _round_money(taxable * tax_pct / 100)
    currencies = {bucket_currency for (_, _, bucket_currency) in buckets}
    if len(currencies) > 1:
        raise HTTPException(status_code=409, detail="Unbilled usage mixes currencies; bill each currency separately")
    currency = next(iter(currencies), "USD")

    invoice = Invoice(
        number=_invoice_number(db),
        customer=customer,
        period_start=str(payload.get("periodStart") or _month_start().date().isoformat()),
        period_end=str(payload.get("periodEnd") or _now().date().isoformat()),
        usage_mb=round(sum(bucket["mb"] for bucket in buckets.values()), 3),
        subtotal=subtotal,
        platform_fee_pct=platform_fee_pct,
        platform_fee=platform_fee,
        tax_pct=tax_pct,
        tax=tax,
        total=_round_money(subtotal + platform_fee + tax),
        paid_total=0.0,
        currency=currency,
        status="issued",
        notes=(str(payload.get("notes") or "").strip() or None),
        issued_at=_now(),
        due_at=_now() + timedelta(days=max(0, due_days)),
    )
    db.add(invoice)
    db.flush()

    lines = []
    for (rate_name, price_per_mb, line_currency), bucket in sorted(buckets.items(), key=lambda item: -item[1]["amount"]):
        line = InvoiceLine(
            invoice_id=invoice.id,
            description=f"Metered data - {rate_name}",
            rate_name=rate_name,
            quantity_mb=round(bucket["mb"], 3),
            price_per_mb=price_per_mb,
            amount=_round_money(bucket["amount"]),
        )
        db.add(line)
        lines.append(line)

    if platform_fee > 0:
        fee_line = InvoiceLine(
            invoice_id=invoice.id,
            description=f"Platform fee ({platform_fee_pct:g}%)",
            rate_name="Platform fee",
            quantity_mb=0.0,
            price_per_mb=0.0,
            amount=platform_fee,
        )
        db.add(fee_line)
        lines.append(fee_line)

    _audit(db, "invoice.create", invoice.number,
           f"{customer} total={invoice.total} {invoice.currency}")
    for record in unbilled:
        record.invoice_id = invoice.id
    db.commit()
    db.refresh(invoice)
    return _invoice_payload(invoice, lines=lines)


@router.get("/invoices/{invoice_id}")
async def get_invoice(invoice_id: int, db: Session = Depends(get_db)):
    """One invoice with its per-rate lines and payment history."""
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if is_customer() and str(invoice.customer or "").lower() != customer_scope().lower():
        raise HTTPException(status_code=404, detail="Invoice not found")
    lines = db.query(InvoiceLine).filter(InvoiceLine.invoice_id == invoice.id).order_by(InvoiceLine.id).all()
    payments = db.query(Payment).filter(Payment.invoice_id == invoice.id).order_by(Payment.id.desc()).all()
    return _invoice_payload(invoice, lines=lines, payments=payments)


@router.post("/invoices/{invoice_id}/void")
async def void_invoice(invoice_id: int, payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """Void an unpaid invoice and release its metered rows back to unbilled."""
    invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.status == "paid":
        raise HTTPException(status_code=409, detail="A paid invoice cannot be voided")
    if invoice.status == "void":
        raise HTTPException(status_code=409, detail="Invoice is already void")
    completed = (
        db.query(Payment)
        .filter(Payment.invoice_id == invoice.id, Payment.status == "completed")
        .count()
    )
    if completed:
        raise HTTPException(status_code=409, detail="An invoice with completed payments cannot be voided")
    db.query(Payment).filter(
        Payment.invoice_id == invoice.id, Payment.status == "pending"
    ).update({Payment.status: "cancelled"}, synchronize_session=False)
    db.query(UsageRecord).filter(UsageRecord.invoice_id == invoice.id).update(
        {UsageRecord.invoice_id: None}, synchronize_session=False
    )
    invoice.status = "void"
    invoice.notes = str(payload.get("reason") or "").strip() or invoice.notes
    _audit(db, "invoice.void", invoice.number, f"{invoice.customer} total={invoice.total}")
    db.commit()
    db.refresh(invoice)
    return _invoice_payload(invoice)


# --------------------------------------------------------------------------- pesapal client


async def _pesapal_token(gateway: PaymentGateway) -> str:
    """Fetch (and cache) a Pesapal bearer token. Tokens are valid for 5 minutes."""
    base = PESAPAL_BASES.get(gateway.environment)
    if not base:
        raise HTTPException(status_code=400, detail="Gateway is in simulated mode; switch to sandbox or live to call Pesapal")
    if not (gateway.consumer_key and gateway.consumer_secret):
        raise HTTPException(status_code=400, detail="Pesapal consumer key and secret are not configured")
    cache_key = (gateway.environment, gateway.consumer_key, gateway.consumer_secret[-6:])
    cached = _TOKEN_CACHE.get(cache_key)
    if cached and cached[1] > time.time():
        return cached[0]
    url = f"{base}/api/Auth/RequestToken"
    body = {"consumer_key": gateway.consumer_key, "consumer_secret": gateway.consumer_secret}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as session:
            async with session.post(url, json=body, headers={"Accept": "application/json"}) as response:
                payload = await response.json(content_type=None)
                ok = response.status < 400
    except aiohttp.ClientError as error:
        raise HTTPException(status_code=502, detail=f"Pesapal authentication unreachable: {error}") from error
    token = (payload or {}).get("token")
    if not ok or not token:
        message = (payload or {}).get("message") or (payload or {}).get("error") or "no token returned"
        raise HTTPException(status_code=502, detail=f"Pesapal authentication failed: {message}")
    _TOKEN_CACHE[cache_key] = (token, time.time() + 280)
    return token


async def _pesapal_request(
    gateway: PaymentGateway,
    method: str,
    path: str,
    payload: dict | None = None,
    params: dict | None = None,
) -> dict:
    """Call one Pesapal API 3.0 endpoint with a fresh bearer token."""
    base = PESAPAL_BASES[gateway.environment]
    token = await _pesapal_token(gateway)
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    url = f"{base}{path}"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.request(method, url, json=payload, params=params, headers=headers) as response:
                data = await response.json(content_type=None)
                status = response.status
    except aiohttp.ClientError as error:
        raise HTTPException(status_code=502, detail=f"Pesapal request failed: {error}") from error
    if status >= 400:
        message = (data or {}).get("message") or (data or {}).get("error") or f"HTTP {status}"
        raise HTTPException(status_code=502, detail=f"Pesapal {path} rejected: {message}")
    return data or {}


async def _register_ipn(gateway: PaymentGateway, base: str) -> dict:
    """Register our IPN URL with Pesapal and persist the returned notification id."""
    url = (
        gateway.ipn_url
        if gateway.ipn_url and not gateway.ipn_url.startswith(LEGACY_PUBLIC_BASE)
        else f"{base}/billing/pesapal/ipn"
    )
    data = await _pesapal_request(
        gateway,
        "POST",
        "/api/URLSetup/RegisterIPN",
        payload={"url": url, "ipn_notification_type": "GET"},
    )
    ipn_id = data.get("ipn_id")
    if not ipn_id:
        raise HTTPException(status_code=502, detail="Pesapal did not return an ipn_id")
    gateway.ipn_id = ipn_id
    gateway.ipn_url = url
    return data


async def _submit_order(gateway: PaymentGateway, payment: Payment, base: str) -> dict:
    """Create the Pesapal order and return {order_tracking_id, redirect_url}."""
    if not gateway.ipn_id:
        await _register_ipn(gateway, base)
    first, _, last = (payment.payer_name or "RAW-Switch Customer").partition(" ")
    body = {
        "id": payment.reference,
        "currency": payment.currency,
        "amount": round(float(payment.amount_local or payment.amount or 0), 2),
        "description": (payment.description or "RAW-Switch metered data")[:100],
        "callback_url": (
            gateway.callback_url
            if gateway.callback_url and not gateway.callback_url.startswith(LEGACY_PUBLIC_BASE)
            else f"{base}/billing/pesapal/callback"
        ),
        "notification_id": gateway.ipn_id,
        "billing_address": {
            "email_address": payment.payer_email or "",
            "phone_number": payment.payer_phone or "",
            "country_code": os.getenv("PESAPAL_COUNTRY_CODE", "UG"),
            "first_name": first or "RAW-Switch",
            "middle_name": "",
            "last_name": last or "Customer",
            "line_1": "",
            "line_2": "",
            "city": "",
            "state": "",
            "postal_code": "",
            "zip_code": "",
        },
    }
    data = await _pesapal_request(gateway, "POST", "/api/Transactions/SubmitOrderRequest", payload=body)
    if data.get("error"):
        detail = data["error"]
        if isinstance(detail, dict):
            detail = detail.get("message") or detail
        raise HTTPException(status_code=502, detail=f"Pesapal rejected the order: {detail}")
    if not data.get("redirect_url") or not data.get("order_tracking_id"):
        raise HTTPException(status_code=502, detail="Pesapal did not return a payment redirect URL")
    return data


async def _transaction_status(gateway: PaymentGateway, order_tracking_id: str) -> dict:
    """GetTransactionStatus: the only trustworthy source of a Pesapal payment result."""
    return await _pesapal_request(
        gateway,
        "GET",
        "/api/Transactions/GetTransactionStatus",
        params={"orderTrackingId": order_tracking_id},
    )


def _apply_transaction(db: Session, payment: Payment, data: dict) -> Payment:
    """Fold a Pesapal (or simulated) status payload into the payment and invoice."""
    try:
        status_code = int(data.get("status_code") or 0)
    except (TypeError, ValueError):
        status_code = 0
    description = str(data.get("payment_status_description") or "").strip().upper()
    payment.payment_method = data.get("payment_method") or payment.payment_method
    payment.confirmation_code = data.get("confirmation_code") or payment.confirmation_code
    payment.status_message = str(data.get("description") or data.get("message") or "").strip() or payment.status_message
    if data.get("merchant_reference"):
        payment.merchant_reference = str(data["merchant_reference"])
    payment.raw = data
    previous_status = payment.status
    if status_code == 1 or description == "COMPLETED":
        payment.status = "completed"
        payment.completed_at = payment.completed_at or _now()
        _audit(db, "payment.completed", payment.reference,
               f"{payment.amount} {payment.currency}")
    elif status_code in (2, 3) or description in {"FAILED", "REVERSED", "INVALID"}:
        payment.status = "failed"
    else:
        payment.status = "pending"
    db.commit()
    if payment.invoice_id and (payment.status == "completed" or previous_status == "completed"):
        invoice = db.query(Invoice).filter(Invoice.id == payment.invoice_id).first()
        if invoice:
            _recompute_invoice(db, invoice)
    db.refresh(payment)
    return payment


def _simulated_status(payment: Payment, approved: bool) -> dict:
    """Build a GetTransactionStatus-shaped payload for the local test gateway."""
    return {
        "payment_method": "Simulated card",
        "amount": round(float(payment.amount_local or 0), 2),
        "created_date": _now().isoformat(),
        "confirmation_code": ("SBX" + secrets.token_hex(4).upper()) if approved else "",
        "payment_status_description": "Completed" if approved else "Failed",
        "description": (
            "Simulated authorisation approved - no real funds moved"
            if approved
            else "Simulated authorisation declined by the payer"
        ),
        "message": "Request processed successfully",
        "payment_account": "411111**1111" if approved else "declined**",
        "call_back_url": payment.redirect_url,
        "status_code": 1 if approved else 2,
        "merchant_reference": payment.reference,
        "currency": payment.currency,
        "status": "200",
    }


# --------------------------------------------------------------------------- gateway settings


@router.get("/gateway")
async def read_gateway(request: Request, db: Session = Depends(get_db)):
    """Pesapal merchant settings with secrets masked."""
    return _gateway_payload(db, _gateway(db), request)


@router.patch("/gateway")
async def update_gateway(request: Request, payload: dict = Body(...), db: Session = Depends(get_db)):
    """Save Pesapal credentials/environment; blank secrets keep the stored value."""
    gateway = _gateway(db)
    if "environment" in payload:
        environment = str(payload.get("environment") or "").strip().lower()
        if environment not in {"simulated", "sandbox", "live"}:
            raise HTTPException(status_code=422, detail="environment must be simulated, sandbox or live")
        gateway.environment = environment
    if "currency" in payload:
        currency = str(payload.get("currency") or "").strip().upper()
        if currency not in PAYMENT_CURRENCIES:
            raise HTTPException(status_code=422, detail=f"currency must be one of {', '.join(PAYMENT_CURRENCIES)}")
        gateway.currency = currency
    if "fxRate" in payload:
        try:
            fx_rate = float(payload.get("fxRate") or 0)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail="fxRate must be a number") from error
        if fx_rate <= 0:
            raise HTTPException(status_code=422, detail="fxRate must be greater than zero")
        gateway.fx_rate = fx_rate
    if "consumerKey" in payload:
        value = str(payload.get("consumerKey") or "").strip()
        if value:
            gateway.consumer_key = value
    if "consumerSecret" in payload:
        value = str(payload.get("consumerSecret") or "").strip()
        if value:
            gateway.consumer_secret = value
    if "ipnId" in payload:
        gateway.ipn_id = str(payload.get("ipnId") or "").strip() or None
    if "ipnUrl" in payload:
        gateway.ipn_url = str(payload.get("ipnUrl") or "").strip() or None
    if "callbackUrl" in payload:
        gateway.callback_url = str(payload.get("callbackUrl") or "").strip() or None
    if "active" in payload:
        gateway.is_active = bool(payload.get("active"))
    if gateway.currency and gateway.currency != "USD" and float(gateway.fx_rate or 1) <= 1:
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail="fxRate must be greater than 1 when the gateway currency is not USD",
        )
    if gateway.environment in {"sandbox", "live"} and not (gateway.consumer_key and gateway.consumer_secret):
        db.rollback()
        raise HTTPException(
            status_code=422,
            detail="sandbox and live modes require a consumer key and secret",
        )
    _audit(db, "gateway.update", gateway.environment,
           f"currency={gateway.currency} active={gateway.is_active}")
    _TOKEN_CACHE.clear()
    db.commit()
    db.refresh(gateway)
    return _gateway_payload(db, gateway, request)


@router.post("/gateway/test")
async def test_gateway(db: Session = Depends(get_db)):
    """Prove the merchant credentials work (simulated mode answers locally)."""
    gateway = _gateway(db)
    if gateway.environment == "simulated":
        return {
            "status": "online",
            "mode": "simulated",
            "apiBase": "local simulated gateway",
            "message": "Simulated Pesapal gateway ready - no credentials required",
            "latencyMs": 0,
            "ipnRegistered": bool(gateway.ipn_id),
        }
    started = time.time()
    token = await _pesapal_token(gateway)
    return {
        "status": "online",
        "mode": gateway.environment,
        "apiBase": PESAPAL_BASES[gateway.environment],
        "tokenPreview": f"{token[:8]}...",
        "latencyMs": int((time.time() - started) * 1000),
        "ipnRegistered": bool(gateway.ipn_id),
    }


@router.post("/gateway/register-ipn")
async def register_ipn_url(request: Request, db: Session = Depends(get_db)):
    """Register the IPN URL with Pesapal and store the notification id."""
    gateway = _gateway(db)
    if gateway.environment == "simulated":
        gateway.ipn_id = gateway.ipn_id or "SIMULATED-IPN"
        db.commit()
        return {"status": "ok", "mode": "simulated", "ipnId": gateway.ipn_id,
                "ipnUrl": _public_url(request, gateway.ipn_url, "/billing/pesapal/ipn")}
    data = await _register_ipn(gateway, _public_base(request))
    db.commit()
    return {"status": "ok", "mode": gateway.environment, "ipnId": gateway.ipn_id,
            "ipnUrl": _public_url(request, gateway.ipn_url, "/billing/pesapal/ipn"), "raw": data}


# --------------------------------------------------------------------------- payments


@router.post("/payments", status_code=201)
async def create_payment(request: Request, payload: dict = Body(...), db: Session = Depends(get_db)):
    """Start a Pesapal checkout for an invoice balance, or a prepaid top-up."""
    gateway = _gateway(db)
    if not gateway.is_active:
        raise HTTPException(status_code=409, detail="The Pesapal gateway is disabled")

    # Validate payer details before checking invoice state, so a malformed
    # request is reported as a validation error rather than a conflict.
    email = str(payload.get("email") or "").strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="A valid payer email address is required")

    invoice = None
    invoice_id = payload.get("invoiceId")
    if invoice_id not in (None, "", 0, "0"):
        try:
            invoice_id = int(invoice_id)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail="invoiceId must be a whole number") from error
        invoice = db.query(Invoice).filter(Invoice.id == invoice_id).first()
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if is_customer() and str(invoice.customer or "").lower() != customer_scope().lower():
            raise HTTPException(status_code=404, detail="Invoice not found")
        if invoice.status == "void":
            raise HTTPException(status_code=409, detail="A void invoice cannot be paid")
        balance = round(float(invoice.total or 0) - float(invoice.paid_total or 0), 2)
        if invoice.status == "paid" or balance <= 0:
            raise HTTPException(status_code=409, detail="Invoice is already settled")
        pending = (
            db.query(Payment)
            .filter(Payment.invoice_id == invoice.id, Payment.status == "pending")
            .first()
        )
        if pending:
            raise HTTPException(
                status_code=409,
                detail=f"A checkout is already pending for this invoice ({pending.reference})",
            )
        amount = balance
        requested = payload.get("amountUsd")
        if requested not in (None, ""):
            try:
                requested = float(requested)
            except (TypeError, ValueError) as error:
                raise HTTPException(status_code=422, detail="amountUsd must be a number") from error
            if requested <= 0 or requested > balance + 0.01:
                raise HTTPException(status_code=422, detail=f"amountUsd must be between 0 and {balance}")
            amount = round(requested, 2)
        customer = invoice.customer
        purpose = "invoice"
        description = str(payload.get("description") or "").strip() or f"Invoice {invoice.number} - metered data"
    else:
        customer = str(payload.get("customer") or "").strip()
        if not customer:
            raise HTTPException(status_code=422, detail="customer is required for a top-up payment")
        registered = (
            db.query(Customer)
            .filter(func.lower(Customer.name) == customer.lower())
            .first()
        )
        if not registered:
            raise HTTPException(status_code=422, detail=f"Unknown customer '{customer}'")
        customer = registered.name
        try:
            amount = round(float(payload.get("amountUsd") or 0), 2)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail="amountUsd must be a number") from error
        if amount <= 0:
            raise HTTPException(status_code=422, detail="amountUsd must be greater than zero")
        purpose = "top_up"
        description = str(payload.get("description") or "").strip() or "Prepaid metered data credit"

    email = str(payload.get("email") or "").strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="A valid payer email address is required")
    phone = str(payload.get("phone") or "").strip()
    currency = gateway.currency or "USD"
    fx_rate = 1.0 if currency == "USD" else float(gateway.fx_rate or 1)
    amount_local = round(amount * fx_rate, 2)

    payment = Payment(
        reference=_payment_reference(db),
        invoice_id=invoice.id if invoice else None,
        customer=customer,
        purpose=purpose,
        amount=amount,
        amount_local=amount_local,
        currency=currency,
        provider="pesapal",
        mode=gateway.environment,
        status="pending",
        payer_name=str(payload.get("name") or "").strip() or None,
        payer_email=email,
        payer_phone=phone or None,
        description=description[:100],
        created_at=_now(),
    )
    db.add(payment)
    _audit(db, "payment.create", payment.reference,
           f"{purpose} {amount} {payment.currency} invoice={invoice.id if invoice else '-'}")
    db.commit()
    db.refresh(payment)

    if gateway.environment == "simulated":
        payment.order_tracking_id = f"SIM-{secrets.token_hex(6).upper()}"
        payment.merchant_reference = payment.reference
        payment.redirect_url = f"{_public_base(request)}/billing/pesapal/checkout/{payment.id}"
        payment.status_message = "Simulated checkout ready - approve or decline the test card"
        db.commit()
        db.refresh(payment)
    else:
        try:
            order = await _submit_order(gateway, payment, _public_base(request))
        except HTTPException as error:
            payment.status = "failed"
            payment.status_message = str(error.detail)
            db.commit()
            raise
        payment.order_tracking_id = str(order.get("order_tracking_id") or "")
        payment.merchant_reference = str(order.get("merchant_reference") or payment.reference)
        payment.redirect_url = str(order.get("redirect_url") or "")
        payment.status_message = "Awaiting payer authorisation on Pesapal"
        db.commit()
        db.refresh(payment)

    result = _payment_payload(payment)
    result["checkoutUrl"] = payment.redirect_url
    result["invoice"] = _invoice_payload(invoice) if invoice else None
    return result


@router.get("/payments")
async def list_payments(
    customer: str | None = Query(default=None),
    invoiceId: int | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=25),
    db: Session = Depends(get_db),
):
    """Payment attempts, newest first."""
    if is_customer():
        customer = customer_scope()
    query = db.query(Payment)
    if customer:
        query = query.filter(Payment.customer == customer)
    if invoiceId:
        query = query.filter(Payment.invoice_id == invoiceId)
    if status:
        query = query.filter(Payment.status == status)
    capped = max(1, min(int(limit or 25), 200))
    rows = query.order_by(Payment.id.desc()).limit(capped).all()
    return [_payment_payload(row) for row in rows]


@router.get("/payments/{payment_id}")
async def get_payment(payment_id: int, refresh: bool = Query(default=False), db: Session = Depends(get_db)):
    """One payment; ``refresh=true`` re-polls Pesapal for the authoritative status."""
    payment = db.query(Payment).filter(Payment.id == payment_id).first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    if is_customer() and str(payment.customer or "").lower() != customer_scope().lower():
        raise HTTPException(status_code=404, detail="Payment not found")
    if refresh and payment.status in {"pending", "completed"} and payment.order_tracking_id:
        gateway = _gateway(db)
        if gateway.environment == "simulated":
            payment.status_message = payment.status_message or "Simulated checkout awaiting payer decision"
            db.commit()
        else:
            data = await _transaction_status(gateway, payment.order_tracking_id)
            payment = _apply_transaction(db, payment, data)
    result = _payment_payload(payment)
    if payment.invoice_id:
        invoice = db.query(Invoice).filter(Invoice.id == payment.invoice_id).first()
        result["invoice"] = _invoice_payload(invoice) if invoice else None
    return result


@router.post("/payments/{payment_id}/cancel")
async def cancel_payment(payment_id: int, db: Session = Depends(get_db)):
    """Abandon a pending checkout (the payer closed the Pesapal page)."""
    payment = db.query(Payment).filter(Payment.id == payment_id).first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    if is_customer() and str(payment.customer or "").lower() != customer_scope().lower():
        raise HTTPException(status_code=404, detail="Payment not found")
    if payment.status != "pending":
        raise HTTPException(status_code=409, detail=f"Payment is already {payment.status}")
    payment.status = "cancelled"
    payment.status_message = "Cancelled from the merchant console"
    _audit(db, "payment.cancel", payment.reference, payment.status_message)
    db.commit()
    db.refresh(payment)
    return _payment_payload(payment)


# --------------------------------------------------------------------------- hosted pages


PAGE_STYLE = """
  *{box-sizing:border-box}
  body{margin:0;font-family:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif;background:#f4f6fb;color:#0f172a;
       display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px}
  .sheet{background:#fff;border:1px solid #e2e8f0;border-radius:20px;box-shadow:0 18px 50px rgba(15,23,42,.10);
         max-width:520px;width:100%;padding:32px}
  .brand{display:flex;align-items:center;gap:10px;font-weight:700;letter-spacing:-.02em;margin-bottom:22px}
  .dot{width:30px;height:30px;border-radius:9px;background:linear-gradient(135deg,#2563eb,#22c55e)}
  h1{font-size:22px;margin:0 0 6px;letter-spacing:-.02em}
  p.sub{margin:0 0 22px;color:#64748b;font-size:14px;line-height:1.5}
  .pill{display:inline-flex;align-items:center;gap:8px;border-radius:999px;padding:6px 14px;font-size:12px;
        font-weight:700;text-transform:uppercase;letter-spacing:.08em}
  .ok{background:#dcfce7;color:#15803d}.bad{background:#fee2e2;color:#b91c1c}.wait{background:#fef3c7;color:#b45309}
  dl{display:grid;grid-template-columns:1fr auto;gap:10px 16px;margin:22px 0 0;padding-top:18px;
     border-top:1px dashed #e2e8f0;font-size:14px}
  dt{color:#64748b}dd{margin:0;font-weight:600;text-align:right;word-break:break-all}
  .amount{font-size:30px;font-weight:800;letter-spacing:-.03em;margin:18px 0 2px}
  .actions{display:flex;gap:10px;margin-top:26px}
  button{flex:1;border:0;border-radius:12px;padding:14px;font-size:15px;font-weight:700;cursor:pointer;font-family:inherit}
  .primary{background:#2563eb;color:#fff}.primary:hover{background:#1d4ed8}
  .ghost{background:#fff;border:1px solid #e2e8f0;color:#0f172a}.ghost:hover{background:#f8fafc}
  a.link{display:inline-block;margin-top:22px;color:#2563eb;font-weight:600;text-decoration:none;font-size:14px}
  .note{margin-top:18px;font-size:12px;color:#94a3b8;line-height:1.5}
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{html.escape(title)} · RAW-Switch</title><style>{PAGE_STYLE}</style></head>"
        "<body><main class=\"sheet\">"
        "<div class=\"brand\"><span class=\"dot\"></span>RAW-Switch</div>"
        f"{body}</main></body></html>"
    )


def _money(value, currency: str = "USD") -> str:
    return f"{float(value or 0):,.2f} {currency}"


async def _resolve_payment(db: Session, order_tracking_id: str, merchant_reference: str) -> Payment | None:
    if order_tracking_id:
        payment = db.query(Payment).filter(Payment.order_tracking_id == order_tracking_id).first()
        if payment:
            return payment
    if merchant_reference:
        return db.query(Payment).filter(Payment.reference == merchant_reference).first()
    return None


@router.get("/pesapal/callback", response_class=HTMLResponse)
async def pesapal_callback(
    OrderTrackingId: str = Query(default=""),
    OrderMerchantReference: str = Query(default=""),
    OrderNotificationType: str = Query(default="CALLBACKURL"),
    db: Session = Depends(get_db),
):
    """Where Pesapal sends the payer back. Always re-reads the real status first."""
    payment = await _resolve_payment(db, OrderTrackingId, OrderMerchantReference)
    if not payment:
        return HTMLResponse(
            _page(
                "Payment not found",
                "<h1>Payment not found</h1><p class=\"sub\">We could not match this callback to a payment on this "
                "account. Nothing was charged twice - check the Billing console for the latest status.</p>"
                f"<a class=\"link\" href=\"{html.escape(DASHBOARD_URL)}\">Back to RAW-Switch</a>",
            ),
            status_code=404,
        )
    gateway = _gateway(db)
    if gateway.environment != "simulated" and payment.order_tracking_id:
        try:
            data = await _transaction_status(gateway, payment.order_tracking_id)
            payment = _apply_transaction(db, payment, data)
        except HTTPException:
            pass  # the page still renders from stored state; the IPN will reconcile

    invoice = db.query(Invoice).filter(Invoice.id == payment.invoice_id).first() if payment.invoice_id else None
    paid = payment.status == "completed"
    pill = "ok" if paid else "bad" if payment.status in {"failed", "cancelled"} else "wait"
    label = {"completed": "Paid", "failed": "Declined", "cancelled": "Cancelled"}.get(payment.status, "Pending")
    body = (
        f"<span class=\"pill {pill}\">{html.escape(label)}</span>"
        f"<h1>{'Payment received' if paid else 'Payment not completed'}</h1>"
        f"<p class=\"sub\">{html.escape((payment.status_message or 'Awaiting the payment provider')[:180])}</p>"
        f"<div class=\"amount\">{_money(payment.amount, payment.currency)}</div>"
        "<dl>"
        f"<dt>Reference</dt><dd>{html.escape(payment.reference)}</dd>"
        f"<dt>Invoice</dt><dd>{html.escape(invoice.number) if invoice else 'Top-up credit'}</dd>"
        f"<dt>Payer</dt><dd>{html.escape(payment.payer_email or '')}</dd>"
        f"<dt>Method</dt><dd>{html.escape(payment.payment_method or payment.mode.title())}</dd>"
        f"<dt>Confirmation</dt><dd>{html.escape(payment.confirmation_code or '-')}</dd>"
        "</dl>"
        f"<a class=\"link\" href=\"{html.escape(DASHBOARD_URL)}\">Back to RAW-Switch billing</a>"
        "<p class=\"note\">Keep this reference for reconciliation. The Billing console shows the same status "
        "because it is read from the payment record, not from this page.</p>"
    )
    return HTMLResponse(_page("Payment status", body))


async def _request_payload(request: Request) -> dict:
    """Read a JSON or urlencoded body without requiring python-multipart.

    ``request.form()`` needs the multipart dependency on modern Starlette, and
    the hosted Pesapal pages only ever post urlencoded data, so the raw body is
    parsed here instead.
    """
    try:
        raw = await request.body()
    except Exception:
        return {}
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return {}
    if "json" in (request.headers.get("content-type") or "").lower():
        try:
            data = json.loads(text)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    try:
        return {key: values[-1] for key, values in parse_qs(text).items()}
    except Exception:
        return {}


async def _handle_ipn(db: Session, order_tracking_id: str, merchant_reference: str, notification_type: str) -> JSONResponse:
    """Shared IPN logic: verify with Pesapal, record the result, answer per spec."""
    payment = await _resolve_payment(db, order_tracking_id, merchant_reference)
    if not payment:
        return JSONResponse(
            {
                "orderNotificationType": notification_type or "IPNCHANGE",
                "orderTrackingId": order_tracking_id,
                "orderMerchantReference": merchant_reference,
                "status": 500,
                "message": "Unknown payment reference",
            }
        )
    gateway = _gateway(db)
    try:
        if gateway.environment == "simulated":
            if not payment.raw:
                payment.status_message = payment.status_message or "Simulated IPN acknowledged"
                db.commit()
        elif payment.order_tracking_id:
            data = await _transaction_status(gateway, payment.order_tracking_id)
            payment = _apply_transaction(db, payment, data)
    except HTTPException as error:
        return JSONResponse(
            {
                "orderNotificationType": notification_type or "IPNCHANGE",
                "orderTrackingId": order_tracking_id or payment.order_tracking_id,
                "orderMerchantReference": merchant_reference or payment.reference,
                "status": 500,
                "message": str(error.detail),
            }
        )
    return JSONResponse(
        {
            "orderNotificationType": notification_type or "IPNCHANGE",
            "orderTrackingId": order_tracking_id or payment.order_tracking_id,
            "orderMerchantReference": merchant_reference or payment.reference,
            "status": 200,
        }
    )


@router.get("/pesapal/ipn")
async def pesapal_ipn_get(
    OrderTrackingId: str = Query(default=""),
    OrderMerchantReference: str = Query(default=""),
    OrderNotificationType: str = Query(default="IPNCHANGE"),
    db: Session = Depends(get_db),
):
    """Pesapal IPN registered as GET. Answers with the JSON body Pesapal expects."""
    return await _handle_ipn(db, OrderTrackingId, OrderMerchantReference, OrderNotificationType)


@router.post("/pesapal/ipn")
async def pesapal_ipn_post(request: Request, db: Session = Depends(get_db)):
    """Pesapal IPN over POST (JSON or form encoded)."""
    payload = await _request_payload(request)
    return await _handle_ipn(
        db,
        str(payload.get("OrderTrackingId") or ""),
        str(payload.get("OrderMerchantReference") or ""),
        str(payload.get("OrderNotificationType") or "IPNCHANGE"),
    )


# --------------------------------------------------------------------------- simulated checkout


@router.get("/pesapal/checkout/{payment_id}", response_class=HTMLResponse)
async def simulated_checkout(payment_id: int, db: Session = Depends(get_db)):
    """Local stand-in for the Pesapal payment page (simulated mode only)."""
    gateway = _gateway(db)
    payment = db.query(Payment).filter(Payment.id == payment_id).first()
    if not payment:
        return HTMLResponse(_page("Payment not found", "<h1>Payment not found</h1>"), status_code=404)
    if gateway.environment != "simulated":
        return HTMLResponse(
            _page(
                "Simulated checkout disabled",
                "<h1>Simulated checkout disabled</h1><p class=\"sub\">The gateway is configured for "
                f"{html.escape(gateway.environment)} mode, so payments are authorised by Pesapal. Use the "
                "Pesapal redirect URL instead.</p>",
            ),
            status_code=409,
        )
    invoice = db.query(Invoice).filter(Invoice.id == payment.invoice_id).first() if payment.invoice_id else None
    if payment.status != "pending":
        actions = f"<p class=\"sub\">This payment is already <strong>{html.escape(payment.status)}</strong>.</p>"
    else:
        actions = (
            f"<form method=\"post\" action=\"/billing/pesapal/checkout/{payment.id}\" class=\"actions\">"
            "<button class=\"primary\" name=\"decision\" value=\"approve\" type=\"submit\">Approve test payment</button>"
            "<button class=\"ghost\" name=\"decision\" value=\"decline\" type=\"submit\">Decline</button>"
            "</form>"
        )
    body = (
        "<span class=\"pill wait\">Simulated Pesapal</span>"
        "<h1>Authorise test payment</h1>"
        "<p class=\"sub\">No card is charged. This page replaces the Pesapal hosted checkout while the gateway is in "
        "simulated mode, and drives exactly the same IPN/callback path.</p>"
        f"<div class=\"amount\">{_money(payment.amount_local, payment.currency)}</div>"
        "<dl>"
        f"<dt>Reference</dt><dd>{html.escape(payment.reference)}</dd>"
        f"<dt>Invoice</dt><dd>{html.escape(invoice.number) if invoice else 'Top-up credit'}</dd>"
        f"<dt>Description</dt><dd>{html.escape(payment.description or '')}</dd>"
        f"<dt>Payer</dt><dd>{html.escape(payment.payer_email or '')}</dd>"
        f"<dt>Invoice value</dt><dd>{_money(payment.amount, 'USD')}</dd>"
        "</dl>"
        f"{actions}"
        "<p class=\"note\">Switching this gateway to sandbox or live is a settings change in the Billing console; "
        "this page then refuses to run.</p>"
    )
    return HTMLResponse(_page("Authorise test payment", body))


@router.post("/pesapal/checkout/{payment_id}")
async def simulated_checkout_submit(payment_id: int, request: Request, db: Session = Depends(get_db)):
    """Approve/decline the simulated payment, then bounce through the real callback URL."""
    gateway = _gateway(db)
    payment = db.query(Payment).filter(Payment.id == payment_id).first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    if gateway.environment != "simulated":
        raise HTTPException(status_code=409, detail="Simulated checkout is disabled for this gateway")
    if payment.status != "pending":
        raise HTTPException(status_code=409, detail=f"Payment is already {payment.status}")
    form = await _request_payload(request)
    decision = str(form.get("decision") or request.query_params.get("decision") or "").strip().lower()
    if decision not in {"approve", "decline"}:
        raise HTTPException(status_code=422, detail="decision must be approve or decline")
    _apply_transaction(db, payment, _simulated_status(payment, approved=decision == "approve"))
    callback = _public_url(request, gateway.callback_url, "/billing/pesapal/callback")
    separator = "&" if "?" in callback else "?"
    target = (
        f"{callback}{separator}OrderTrackingId={payment.order_tracking_id}"
        f"&OrderMerchantReference={payment.reference}&OrderNotificationType=CALLBACKURL"
    )
    return RedirectResponse(url=target, status_code=303)
