from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Body, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from . import models, switching, config, billing
from .models import Device, Carrier, NetworkEvent, EsimProfile, TelecomNetwork, Euicc, RspOperation, RspProvider, RoamingPolicy
from .models import Customer, Plan
from .config import SessionLocal, engine
from .audit import AUDIT_ACTOR, audit as _audit
from .authz import CURRENT_USER, current_user, customer_scope, is_customer
import json
from datetime import datetime, timedelta
import uuid
import hashlib
import secrets
import re
from sqlalchemy import case, func, text
from sqlalchemy.exc import SQLAlchemyError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from urllib.parse import quote, urlsplit
import aiohttp
import os
import asyncio
import base64
import hmac
import smtplib
import struct
import time
from contextvars import ContextVar
from email.message import EmailMessage as MailMessage

models.Base.metadata.create_all(bind=engine)

PERMISSION_CATALOG = {"dashboard", "sims", "data", "reports", "billing", "team", "customers", "settings"}


def _validated_permissions(raw):
    """Normalise the permissions array and reject unknown areas."""
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        raise HTTPException(status_code=422, detail="permissions must be an array")
    cleaned = []
    for value in raw:
        key = str(value or "").strip().lower()
        if key not in PERMISSION_CATALOG:
            raise HTTPException(status_code=422, detail=f"Unknown permission '{value}'")
        if key not in cleaned:
            cleaned.append(key)
    return cleaned


def _migrate_user_account_columns() -> None:
    with engine.begin() as connection:
        columns = {row[1] for row in connection.execute(text("PRAGMA table_info(users)"))}
        for name, definition in (
            ("package_gb", "FLOAT NOT NULL DEFAULT 0"),
            ("used_gb", "FLOAT NOT NULL DEFAULT 0"),
            ("account_balance", "FLOAT NOT NULL DEFAULT 248.5"),
        ):
            if name not in columns:
                connection.execute(text(f"ALTER TABLE users ADD COLUMN {name} {definition}"))
        device_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(devices)"))}
        if "telecom_network_id" not in device_columns:
            connection.execute(text("ALTER TABLE devices ADD COLUMN telecom_network_id INTEGER"))

# Columns added by the SIM studio (IoT SIM / iSIM / eSIM activation metadata).
DEVICE_IDENTITY_COLUMNS = {
    "imsi": "VARCHAR",
    "msisdn": "VARCHAR",
    "apn": "VARCHAR",
    "form_factor": "VARCHAR",
    "pin1": "VARCHAR",
    "puk1": "VARCHAR",
    "eid": "VARCHAR",
    "smdp_address": "VARCHAR",
    "matching_id": "VARCHAR",
    "activation_code": "VARCHAR",
    "activation_token": "VARCHAR",
    "profile_state": "VARCHAR",
    "provisioned_by": "VARCHAR",
    "activated_at": "DATETIME",
    "terminated_at": "DATETIME",
}


def _migrate_device_identity_columns() -> None:
    with engine.begin() as connection:
        existing = {row[1] for row in connection.execute(text("PRAGMA table_info(devices)"))}
        for name, definition in DEVICE_IDENTITY_COLUMNS.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE devices ADD COLUMN {name} {definition}"))


_migrate_device_identity_columns()
def _migrate_carrier_columns() -> None:
    with engine.begin() as connection:
        carrier_columns = {row[1] for row in connection.execute(text("PRAGMA table_info(carriers)"))}
        if "country" not in carrier_columns:
            connection.execute(text('ALTER TABLE carriers ADD COLUMN country VARCHAR NOT NULL DEFAULT "Unknown"'))


def _ensure_schema_columns() -> None:
    """Add columns the models gained after a database was first created.

    create_all() only issues CREATE TABLE, so a column added to a model later
    never reaches an existing database, and every query touching it fails with
    "no such column". carriers.telecom_network_id was missing for exactly this
    reason and took the whole marketplace down with 500s.
    """
    with engine.begin() as connection:
        for table in models.Base.metadata.sorted_tables:
            existing = {row[1] for row in connection.execute(text(f"PRAGMA table_info({table.name})"))}
            if not existing:
                continue
            for column in table.columns:
                if column.name in existing or column.primary_key:
                    continue
                ddl = (
                    f"ALTER TABLE {table.name} "
                    f"ADD COLUMN {column.name} {column.type.compile(dialect=engine.dialect)}"
                )
                try:
                    connection.execute(text(ddl))
                except SQLAlchemyError as error:
                    # Never block startup on one column; report and carry on.
                    print(f"[schema] could not add {table.name}.{column.name}: {error}", flush=True)


def _migrate_carrier_network_links() -> None:
    """Fold the legacy single-provider column into the carrier_networks table."""
    with engine.begin() as connection:
        columns = {row[1] for row in connection.execute(text("PRAGMA table_info(carriers)"))}
        if "telecom_network_id" not in columns:
            return
        connection.execute(text(
            "INSERT OR IGNORE INTO carrier_networks (carrier_id, network_id) "
            "SELECT id, telecom_network_id FROM carriers WHERE telecom_network_id IS NOT NULL"
        ))


_migrate_user_account_columns()
_migrate_carrier_columns()
_ensure_schema_columns()
_migrate_carrier_network_links()




app = FastAPI(title="Multi-Carrier SIM Aggregator API")

@app.middleware("http")
async def _actor_middleware(request, call_next):
    """Remember who is acting so audit rows can name them."""
    token = AUDIT_ACTOR.set((request.headers.get("X-Actor") or "system")[:120])
    try:
        return await call_next(request)
    finally:
        AUDIT_ACTOR.reset(token)

# ---------------------------------------------------------------------------
# Authentication and access policy
# ---------------------------------------------------------------------------

SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "12"))

PUBLIC_PATHS = ("/auth/login", "/system/health",
                "/auth/password-reset/request", "/auth/password-reset/confirm")
PUBLIC_PREFIXES = ("/billing/pesapal/", "/webhooks/")
API_PREFIXES = (
    "/auth", "/customers", "/plans", "/inventory", "/devices", "/marketplace",
    "/telecom", "/euicc", "/rsp", "/esim", "/sims", "/roaming", "/system",
    "/webhooks", "/billing", "/alerts", "/logs", "/trash", "/email",
)

# (methods, path regex) that only administrators may call.
ADMIN_RULES = [
    ({"GET"}, r"^/auth/users$"),
    ({"POST", "PATCH", "DELETE"}, r"^/auth/users"),
    ({"GET", "POST", "PATCH", "DELETE"}, r"^/customers"),
    ({"PATCH"}, r"^/plans/"),
    ({"POST", "PATCH"}, r"^/inventory"),
    ({"POST"}, r"^/devices"),
    ({"POST"}, r"^/telecom/networks$"),
    ({"PATCH", "DELETE"}, r"^/telecom/networks/"),
    ({"POST"}, r"^/telecom/networks/\d+/(test|attach|bind)$"),
    ({"POST"}, r"^/rsp/"),
    ({"PATCH"}, r"^/rsp/"),
    ({"DELETE"}, r"^/rsp/"),
    ({"POST"}, r"^/euicc$"),
    ({"POST", "PATCH", "DELETE"}, r"^/marketplace"),
    ({"PATCH"}, r"^/roaming"),
    ({"POST"}, r"^/sims/provision$"),
    ({"POST"}, r"^/sims/[^/]+/(lifecycle|activation|usage)$"),
    ({"POST", "PATCH", "DELETE"}, r"^/billing/rates"),
    ({"PATCH"}, r"^/billing/gateway$"),
    ({"POST"}, r"^/billing/gateway/"),
    ({"POST"}, r"^/billing/usage"),
    ({"POST"}, r"^/billing/invoices"),
    ({"POST"}, r"^/billing/invoices/\d+/void$"),
    ({"GET"}, r"^/logs"),
    ({"GET"}, r"^/trash"),
    ({"POST", "DELETE"}, r"^/trash"),
    ({"GET", "PATCH", "POST"}, r"^/email"),
]

# What a Customer account may reach. Everything else in the API is 403.
CUSTOMER_RULES = [
    ({"GET"}, r"^/auth/me$"),
    ({"POST"}, r"^/auth/logout$"),
    ({"GET"}, r"^/auth/2fa/status$"),
    ({"POST"}, r"^/auth/2fa/(setup|enable|disable)$"),
    ({"GET"}, r"^/inventory$"),
    ({"GET"}, r"^/sims/(summary|catalog|recent)$"),
    ({"GET"}, r"^/sims/[^/]+$"),
    ({"GET"}, r"^/sims/[^/]+/qr\.svg$"),
    ({"GET"}, r"^/plans$"),
    ({"GET"}, r"^/billing/(summary|invoices|payments|usage|rates)$"),
    ({"GET"}, r"^/billing/(invoices|payments)/\d+$"),
    ({"POST"}, r"^/billing/payments$"),
    ({"POST"}, r"^/billing/payments/\d+/cancel$"),
    ({"GET"}, r"^/alerts$"),
]


def _is_api_path(path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in API_PREFIXES)


def _is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def _matches(rules, method: str, path: str) -> bool:
    return any(method in methods and re.match(pattern, path) for methods, pattern in rules)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _issue_session(db, user) -> tuple[str, datetime]:
    token = secrets.token_urlsafe(32)
    expires = datetime.utcnow() + timedelta(hours=SESSION_TTL_HOURS)
    db.add(models.AuthSession(
        token_hash=_hash_token(token), user_id=user.id,
        created_at=datetime.utcnow(), expires_at=expires,
    ))
    db.commit()
    return token, expires


def _user_from_request(db, request):
    token = request.cookies.get(SESSION_COOKIE) or ""
    if not token:
        header = request.headers.get("Authorization") or ""
        if not header.lower().startswith("bearer "):
            return None
        token = header[7:].strip()
    if not token:
        return None
    row = db.query(models.AuthSession).filter(
        models.AuthSession.token_hash == _hash_token(token)
    ).first()
    if not row or row.revoked_at is not None:
        return None
    expires = row.expires_at
    if expires is not None:
        if expires.tzinfo is not None:
            expires = expires.replace(tzinfo=None)
        if expires < datetime.utcnow():
            return None
    return db.query(models.User).filter(
        models.User.id == row.user_id, models.User.status == "Active"
    ).first()




@app.middleware("http")
async def _auth_middleware(request, call_next):
    path = request.url.path
    if not _is_api_path(path) or _is_public(path):
        return await call_next(request)

    db = SessionLocal()
    try:
        user = _user_from_request(db, request)
    finally:
        db.close()

    if not user:
        return JSONResponse(
            {"detail": "Authentication required"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )

    identity = {
        "id": user.id,
        "role": user.role,
        "email": user.email,
        "name": user.name,
        "customerName": user.customer_name,
        "permissions": user.permissions or [],
    }
    if request.cookies.get(SESSION_COOKIE) and request.method in {"POST", "PATCH", "DELETE"}:
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != (request.headers.get("host") or ""):
            return JSONResponse({"detail": "Cross-origin request rejected"}, status_code=403)

    token = CURRENT_USER.set(identity)
    actor_token = AUDIT_ACTOR.set(identity["email"] or "system")
    try:
        if identity["role"] == "Customer":
            if not _matches(CUSTOMER_RULES, request.method, path):
                return JSONResponse(
                    {"detail": "You do not have access to this resource"}, status_code=403
                )
        elif identity["role"] != "Administrator":
            if _matches(ADMIN_RULES, request.method, path):
                return JSONResponse(
                    {"detail": "Administrator access required"}, status_code=403
                )
        return await call_next(request)
    finally:
        CURRENT_USER.reset(token)
        AUDIT_ACTOR.reset(actor_token)

CHALLENGE_AFTER_FAILURES = 3
CHALLENGE_IP_FAILURES = 10
CHALLENGE_TTL_SECONDS = 300


def _failure_counts(db, identifier: str, ip: str) -> tuple[int, int]:
    window = datetime.utcnow() - timedelta(minutes=LOGIN_WINDOW_MINUTES)
    ident = db.query(models.LoginAttempt).filter(
        models.LoginAttempt.identifier == identifier,
        models.LoginAttempt.success.is_(False),
        models.LoginAttempt.created_at >= window,
    ).count()
    by_ip = db.query(models.LoginAttempt).filter(
        models.LoginAttempt.ip == (ip or ""),
        models.LoginAttempt.success.is_(False),
        models.LoginAttempt.created_at >= window,
    ).count()
    return ident, by_ip


def _needs_challenge(db, identifier: str, ip: str) -> bool:
    ident, by_ip = _failure_counts(db, identifier, ip)
    return ident >= CHALLENGE_AFTER_FAILURES or by_ip >= CHALLENGE_IP_FAILURES


def _issue_challenge(db, ip: str) -> dict:
    left = secrets.randbelow(8) + 2
    right = secrets.randbelow(8) + 2
    row = models.LoginChallenge(
        answer_hash=_hash_token(str(left + right)),
        ip=(ip or "")[:64],
        created_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(seconds=CHALLENGE_TTL_SECONDS),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"challengeId": row.id, "question": f"{left} + {right}", "expiresIn": CHALLENGE_TTL_SECONDS}


def _solve_challenge(db, challenge_id, answer, ip: str) -> bool:
    try:
        identifier = int(challenge_id)
    except (TypeError, ValueError):
        return False
    row = db.query(models.LoginChallenge).filter(models.LoginChallenge.id == identifier).first()
    if not row or row.used_at is not None:
        return False
    expires = row.expires_at
    if expires is not None and expires.tzinfo is not None:
        expires = expires.replace(tzinfo=None)
    if expires is not None and expires < datetime.utcnow():
        return False
    if (row.ip or "") != (ip or ""):
        return False
    if not secrets.compare_digest(row.answer_hash, _hash_token(str(answer).strip())):
        return False
    row.used_at = datetime.utcnow()
    db.commit()
    return True


def _totp_code(secret: str, counter: int) -> str:
    padded = secret + "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(padded)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def _verify_totp(secret: str | None, code: str | None) -> bool:
    if not secret or not code:
        return False
    try:
        counter = int(time.time()) // 30
    except (TypeError, ValueError):
        return False
    expected = str(code).strip()
    return any(
        secrets.compare_digest(_totp_code(secret, step), expected)
        for step in (counter - 1, counter, counter + 1)
    )


def _new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


SESSION_COOKIE = "raw_switch_session"
LOGIN_WINDOW_MINUTES = 15
LOGIN_MAX_FAILURES = 5
LOGIN_MAX_IP_FAILURES = 30


def _set_session_cookie(response, request, token: str) -> None:
    secure = request.url.scheme == "https" or os.getenv("COOKIE_SECURE", "0") == "1"
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_TTL_HOURS * 3600,
        httponly=True, secure=secure, samesite="lax", path="/",
    )


def _clear_session_cookie(response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def _record_login(db, identifier: str, ip: str, success: bool) -> None:
    db.add(models.LoginAttempt(identifier=identifier[:200], ip=(ip or "")[:64], success=success))
    if secrets.randbelow(20) == 0:
        db.query(models.LoginAttempt).filter(
            models.LoginAttempt.created_at < datetime.utcnow() - timedelta(days=1)
        ).delete(synchronize_session=False)


def _minutes_since(value) -> float:
    if value is None:
        return 10 ** 6
    if value.tzinfo is not None:
        value = value.replace(tzinfo=None)
    return (datetime.utcnow() - value).total_seconds() / 60


def _login_retry_after(db, identifier: str, ip: str) -> int:
    """Seconds the caller must wait, or 0 when under the limit."""
    window = datetime.utcnow() - timedelta(minutes=LOGIN_WINDOW_MINUTES)
    ident_fails = db.query(models.LoginAttempt).filter(
        models.LoginAttempt.identifier == identifier,
        models.LoginAttempt.success.is_(False),
        models.LoginAttempt.created_at >= window,
    ).all()
    ip_fails = db.query(models.LoginAttempt).filter(
        models.LoginAttempt.ip == (ip or ""),
        models.LoginAttempt.success.is_(False),
        models.LoginAttempt.created_at >= window,
    ).all()

    latest = None
    if len(ident_fails) >= LOGIN_MAX_FAILURES:
        latest = max((row.created_at for row in ident_fails), default=None)
    elif len(ip_fails) >= LOGIN_MAX_IP_FAILURES:
        latest = max((row.created_at for row in ip_fails), default=None)
    if latest is None:
        return 0
    remaining = LOGIN_WINDOW_MINUTES * 60 - _minutes_since(latest) * 60
    return max(1, int(remaining))


@app.middleware("http")
async def _https_middleware(request, call_next):
    """Force TLS when configured, and add the usual transport security headers."""
    if os.getenv("FORCE_HTTPS", "0") == "1" and request.url.scheme != "https":
        if not request.url.path.startswith("/billing/pesapal/"):
            return RedirectResponse(str(request.url.replace(scheme="https")), status_code=307)
    response = await call_next(request)
    if request.url.scheme == "https":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response





app.include_router(billing.router)


@app.middleware("http")
async def _no_cache_app_shell(request, call_next):
    """Always revalidate the dashboard shell so a deploy is picked up at once."""
    response = await call_next(request)
    if request.url.path in {"", "/", "/index.html"}:
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _password_hash(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()
    return f"{salt}${digest}"

def _password_matches(password: str, stored: str) -> bool:
    try:
        salt, expected = stored.split("$", 1)
    except ValueError:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()
    return secrets.compare_digest(actual, expected)

def _user_payload(user: models.User) -> dict:
    return {
        "id": user.id, "name": user.name, "email": user.email, "role": user.role,
        "accountRole": user.account_role, "permissions": user.permissions or [],
        "status": user.status, "customerName": user.customer_name,
        "packageGb": user.package_gb, "usedGb": user.used_gb,
        "accountBalance": user.account_balance,
        "lastLogin": user.last_login or "Never",
    }

def _ensure_default_users(db: Session) -> None:
    if db.query(models.User).count():
        return
    defaults = [
        ("Alex Morgan", "admin@raw-switch.io", "admin123", "Administrator", None, None),
        ("Taylor Freight", "customer@raw-switch.io", "customer123", "Customer", "Customer Admin", "Trackfinity"),
    ]
    for name, email, password, role, account_role, customer_name in defaults:
        db.add(models.User(
            name=name, email=email, password_hash=_password_hash(password),
            role=role, account_role=account_role,
            permissions=["dashboard", "sims", "data", "reports", "billing", "team"] if role == "Customer" else
            ["dashboard", "sims", "data", "reports", "billing", "team", "customers", "settings"],
            customer_name=customer_name, status="Active", last_login="Never",
        ))
    db.commit()

@app.on_event("startup")
def seed_users() -> None:
    db = SessionLocal()
    try:
        _ensure_default_users(db)
        _ensure_sim_catalog(db)
        billing.ensure_catalog(db)
    finally:
        db.close()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Activity log, trash and alert email
# ---------------------------------------------------------------------------

TRASH_KINDS = {"carrier", "network", "provider", "user"}


def _snapshot(obj, fields):
    data = {}
    for field in fields:
        value = getattr(obj, field, None)
        if hasattr(value, "isoformat"):
            value = value.isoformat()
        data[field] = value
    return data


def _trash_record(db, kind, name, payload):
    db.add(models.DeletedItem(
        kind=kind,
        name=str(name)[:120],
        payload=json.dumps(payload, default=str),
        deleted_by=(AUDIT_ACTOR.get() or "system")[:120],
    ))


def _log_payload(event):
    return {
        "id": event.id,
        "actor": event.actor,
        "action": event.action,
        "target": event.target,
        "detail": event.detail,
        "createdAt": event.created_at.isoformat() if event.created_at else None,
    }


@app.get("/logs")
async def list_logs(
    limit: int = Query(default=100),
    actor: str | None = Query(default=None),
    action: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """Admin activity log, newest first."""
    query = db.query(models.AuditEvent)
    if actor:
        query = query.filter(models.AuditEvent.actor.ilike(f"%{actor}%"))
    if action:
        query = query.filter(models.AuditEvent.action.ilike(f"%{action}%"))
    capped = max(1, min(int(limit or 100), 500))
    total = db.query(func.count(models.AuditEvent.id)).scalar() or 0
    if total > 5000:
        cutoff = db.query(models.AuditEvent.id).order_by(
            models.AuditEvent.id.desc()
        ).offset(5000).first()
        if cutoff:
            db.query(models.AuditEvent).filter(
                models.AuditEvent.id < cutoff[0]
            ).delete(synchronize_session=False)
            db.commit()
            query = db.query(models.AuditEvent)
            if actor:
                query = query.filter(models.AuditEvent.actor.ilike(f"%{actor}%"))
            if action:
                query = query.filter(models.AuditEvent.action.ilike(f"%{action}%"))
    rows = query.order_by(models.AuditEvent.id.desc()).limit(capped).all()
    return [_log_payload(row) for row in rows]


@app.get("/trash")
async def list_trash(db: Session = Depends(get_db)):
    """Items removed from the console that can still be restored."""
    rows = db.query(models.DeletedItem).order_by(models.DeletedItem.id.desc()).all()
    return [{
        "id": row.id,
        "kind": row.kind,
        "name": row.name,
        "deletedBy": row.deleted_by,
        "deletedAt": row.deleted_at.isoformat() if row.deleted_at else None,
    } for row in rows]


def _restore_carrier(db, payload):
    if db.query(models.Carrier).filter(models.Carrier.name == payload["name"]).first():
        raise HTTPException(status_code=409, detail="A carrier with this name already exists")
    network_ids = payload.pop("network_ids", []) or []
    carrier = models.Carrier(**payload)
    db.add(carrier)
    db.flush()
    if network_ids:
        carrier.networks = db.query(TelecomNetwork).filter(TelecomNetwork.id.in_(network_ids)).all()
    return carrier


def _restore_network(db, payload):
    if db.query(TelecomNetwork).filter(TelecomNetwork.name == payload["name"]).first():
        raise HTTPException(status_code=409, detail="A telecom network with this name already exists")
    carrier_ids = payload.pop("carrier_ids", []) or []
    network = TelecomNetwork(**payload)
    db.add(network)
    db.flush()
    if carrier_ids:
        carriers = db.query(models.Carrier).filter(models.Carrier.id.in_(carrier_ids)).all()
        for carrier in carriers:
            if network not in carrier.networks:
                carrier.networks.append(network)
    return network


def _restore_provider(db, payload):
    if db.query(RspProvider).filter(RspProvider.name == payload["name"]).first():
        raise HTTPException(status_code=409, detail="An RSP provider with this name already exists")
    provider = RspProvider(**payload)
    db.add(provider)
    return provider


def _restore_user(db, payload):
    if db.query(models.User).filter(models.User.email == payload["email"]).first():
        raise HTTPException(status_code=409, detail="A user with this email already exists")
    user = models.User(**payload)
    db.add(user)
    return user


@app.post("/trash/{item_id}/restore")
async def restore_trash_item(item_id: int, db: Session = Depends(get_db)):
    """Recreate a deleted record from its snapshot."""
    item = db.query(models.DeletedItem).filter(models.DeletedItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Trash item not found")
    payload = json.loads(item.payload or "{}")
    payload.pop("id", None)

    if item.kind == "carrier":
        record = _restore_carrier(db, payload)
    elif item.kind == "network":
        record = _restore_network(db, payload)
    elif item.kind == "provider":
        record = _restore_provider(db, payload)
    elif item.kind == "user":
        record = _restore_user(db, payload)
    else:
        raise HTTPException(status_code=422, detail=f"Unknown trash kind '{item.kind}'")

    _audit(db, f"trash.restore.{item.kind}", item.name, f"trash item {item.id}")
    db.delete(item)
    db.commit()
    return {"restored": True, "kind": item.kind, "name": item.name, "id": getattr(record, "id", None)}


@app.delete("/trash/{item_id}")
async def purge_trash_item(item_id: int, db: Session = Depends(get_db)):
    """Forget a trashed record permanently."""
    item = db.query(models.DeletedItem).filter(models.DeletedItem.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Trash item not found")
    _audit(db, f"trash.purge.{item.kind}", item.name, f"trash item {item.id}")
    db.delete(item)
    db.commit()
    return {"deleted": True, "id": item_id}


# --------------------------------------------------------------------------- email

def _email_settings(db: Session) -> models.EmailSettings:
    row = db.query(models.EmailSettings).first()
    if row:
        return row
    row = models.EmailSettings(enabled=False, mode="simulated", port=587,
                               from_address="alerts@raw-switch.io", use_tls=True)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _email_payload(row: models.EmailSettings) -> dict:
    return {
        "enabled": bool(row.enabled),
        "mode": row.mode,
        "host": row.host,
        "port": row.port,
        "username": row.username,
        "fromAddress": row.from_address,
        "useTls": bool(row.use_tls),
        "adminRecipients": row.admin_recipients,
        "hasPassword": bool(row.password),
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


def _send_mail(db, settings, to_address, subject, body, alert_key=None):
    """Send (or simulate) one email and record it in the outbox."""
    status, error = "simulated", None
    if settings.enabled and settings.mode == "smtp" and settings.host:
        try:
            message = MailMessage()
            message["From"] = settings.from_address
            message["To"] = to_address
            message["Subject"] = subject
            message.set_content(body)
            with smtplib.SMTP(settings.host, int(settings.port or 587), timeout=15) as server:
                if settings.use_tls:
                    server.starttls()
                if settings.username and settings.password:
                    server.login(settings.username, settings.password)
                server.send_message(message)
            status = "sent"
        except Exception as exc:  # noqa: BLE001
            status, error = "failed", str(exc)[:500]
    db.add(models.EmailMessage(
        to_address=to_address[:200], subject=subject[:200], body=body,
        status=status, error=error, alert_key=alert_key,
    ))
    return status


def _admin_recipients(db, settings):
    emails = set()
    for part in re.split(r"[,;\s]+", settings.admin_recipients or ""):
        if "@" in part:
            emails.add(part.strip())
    for user in db.query(models.User).filter(
        models.User.role == "Administrator", models.User.status == "Active"
    ).all():
        if user.email:
            emails.add(user.email)
    return emails


def _alert_recipients(db, settings, alert):
    emails = set(_admin_recipients(db, settings))
    customer_name = alert.get("customer")
    if customer_name:
        customer = db.query(Customer).filter(func.lower(Customer.name) == str(customer_name).lower()).first()
        if customer and customer.contact_email:
            emails.add(customer.contact_email)
    return sorted(emails)


def _dispatch_alerts(db, settings):
    """Email alerts that have not been delivered in the last 24 hours."""
    alerts = _collect_alerts(db)
    sent = skipped = failed = 0
    cutoff = datetime.utcnow() - timedelta(days=1)
    for alert in alerts:
        recent = db.query(models.EmailMessage).filter(
            models.EmailMessage.alert_key == alert["id"],
            models.EmailMessage.created_at >= cutoff,
        ).first()
        if recent:
            skipped += 1
            continue
        recipients = _alert_recipients(db, settings, alert)
        if not recipients:
            skipped += 1
            continue
        subject = f"[RAW-Switch] {alert['title']}"
        body = (f"{alert['title']}\n\n{alert['detail']}\n\n"
                f"Severity: {alert['severity']}\nOpen the console: /#{alert.get('view') or ''}\n")
        for recipient in recipients:
            status = _send_mail(db, settings, recipient, subject, body, alert["id"])
            if status == "failed":
                failed += 1
            else:
                sent += 1
        db.commit()
    db.commit()
    return {"alerts": len(alerts), "sent": sent, "skipped": skipped, "failed": failed}


@app.get("/email/settings")
async def read_email_settings(db: Session = Depends(get_db)):
    return _email_payload(_email_settings(db))


@app.patch("/email/settings")
async def update_email_settings(payload: dict = Body(...), db: Session = Depends(get_db)):
    row = _email_settings(db)
    if "enabled" in payload:
        row.enabled = bool(payload["enabled"])
    if "mode" in payload:
        mode = str(payload.get("mode") or "simulated").lower()
        if mode not in {"simulated", "smtp"}:
            raise HTTPException(status_code=422, detail="mode must be simulated or smtp")
        row.mode = mode
    if "host" in payload:
        row.host = str(payload.get("host") or "").strip() or None
    if "port" in payload:
        try:
            row.port = int(payload.get("port") or 587)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail="port must be a number") from error
    if "username" in payload:
        row.username = str(payload.get("username") or "").strip() or None
    if "password" in payload and str(payload.get("password") or "").strip():
        row.password = str(payload["password"]).strip()
    if "fromAddress" in payload:
        value = str(payload.get("fromAddress") or "").strip()
        if value and "@" not in value:
            raise HTTPException(status_code=422, detail="fromAddress must be an email address")
        row.from_address = value or "alerts@raw-switch.io"
    if "useTls" in payload:
        row.use_tls = bool(payload["useTls"])
    if "adminRecipients" in payload:
        row.admin_recipients = str(payload.get("adminRecipients") or "").strip() or None
    if row.enabled and row.mode == "smtp":
        if not row.host:
            raise HTTPException(status_code=422, detail="SMTP host is required when alert emails are enabled")
        if not row.from_address or "@" not in row.from_address:
            raise HTTPException(status_code=422, detail="A valid from address is required")
        if not (1 <= int(row.port or 0) <= 65535):
            raise HTTPException(status_code=422, detail="port must be between 1 and 65535")
    db.commit()
    db.refresh(row)
    _audit(db, "email.settings", "alert email", f"enabled={row.enabled} mode={row.mode}")
    db.commit()
    return _email_payload(row)


@app.post("/email/test")
async def test_email(db: Session = Depends(get_db)):
    settings = _email_settings(db)
    recipients = _admin_recipients(db, settings)
    if not recipients:
        raise HTTPException(status_code=422, detail="No admin recipients configured")
    results = {}
    for recipient in recipients:
        results[recipient] = _send_mail(
            db, settings, recipient, "[RAW-Switch] Test alert email",
            "This is a test message from your RAW-Switch console.\n", alert_key=None,
        )
    db.commit()
    _audit(db, "email.test", "alert email", json.dumps(results))
    db.commit()
    return {"results": results, "mode": settings.mode, "enabled": settings.enabled}


@app.post("/email/dispatch")
async def dispatch_email_alerts(db: Session = Depends(get_db)):
    settings = _email_settings(db)
    result = _dispatch_alerts(db, settings)
    _audit(db, "email.dispatch", "alert email", json.dumps(result))
    db.commit()
    return result


@app.get("/email/outbox")
async def email_outbox(limit: int = Query(default=50), db: Session = Depends(get_db)):
    rows = db.query(models.EmailMessage).order_by(models.EmailMessage.id.desc()).limit(
        max(1, min(int(limit or 50), 200))
    ).all()
    return [{
        "id": row.id, "to": row.to_address, "subject": row.subject, "status": row.status,
        "error": row.error, "alertKey": row.alert_key,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
    } for row in rows]


async def _alert_email_loop():
    while True:
        await asyncio.sleep(300)
        db = SessionLocal()
        try:
            settings = _email_settings(db)
            if settings.enabled:
                _dispatch_alerts(db, settings)
        except Exception:
            pass
        finally:
            db.close()


@app.on_event("startup")
async def _start_alert_email_loop():
    asyncio.create_task(_alert_email_loop())

@app.post("/auth/login")
async def authenticate(request: Request, response: Response, payload: dict = Body(...),
                       db: Session = Depends(get_db)):
    _ensure_default_users(db)
    identifier = str(payload.get("identifier") or "").strip().lower()
    password = str(payload.get("password") or "")
    ip = request.client.host if request.client else "unknown"
    retry_after = _login_retry_after(db, identifier, ip)
    if retry_after:
        _audit(db, "auth.throttled", identifier, f"retry_after={retry_after}s")
        db.commit()
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )
    user = db.query(models.User).filter(
        (models.User.email == identifier) | (models.User.name.ilike(identifier))
    ).first()
    if not user or user.status != "Active" or not _password_matches(password, user.password_hash):
        _record_login(db, identifier, ip, False)
        _audit(db, "auth.failed", identifier, f"ip={ip}")
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if _needs_challenge(db, identifier, ip):
        solved = _solve_challenge(db, payload.get("challengeId"), payload.get("challengeAnswer"), ip)
        if not solved:
            challenge = _issue_challenge(db, ip)
            _audit(db, "auth.challenge", identifier, f"ip={ip}")
            db.commit()
            raise HTTPException(
                status_code=401,
                detail={
                    "challengeRequired": True,
                    "message": "Complete the verification to continue",
                    "challenge": challenge,
                },
            )

    if getattr(user, "totp_enabled", False):
        if not _verify_totp(user.totp_secret, str(payload.get("totp") or "")):
            _audit(db, "auth.totp_failed", identifier, f"ip={ip}")
            db.commit()
            raise HTTPException(
                status_code=401,
                detail={"totpRequired": True, "message": "Enter the 6-digit authenticator code"},
            )

    _record_login(db, identifier, ip, True)
    user.last_login = "Just now"
    _audit(db, "auth.login", user.email, f"role={user.role}")
    token, expires = _issue_session(db, user)
    _set_session_cookie(response, request, token)
    payload = _user_payload(user)
    payload["token"] = token
    payload["tokenExpiresAt"] = expires.isoformat()
    return payload

@app.post("/auth/logout")
async def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    """Revoke the current session and clear the auth cookie."""
    token = request.cookies.get(SESSION_COOKIE) or ""
    if not token:
        header = request.headers.get("Authorization") or ""
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
    if token:
        row = db.query(models.AuthSession).filter(
            models.AuthSession.token_hash == _hash_token(token)
        ).first()
        if row and row.revoked_at is None:
            row.revoked_at = datetime.utcnow()
            _audit(db, "auth.logout", row.user_id)
            db.commit()
    _clear_session_cookie(response)
    return {"loggedOut": True}


@app.post("/auth/password-reset/request")
async def request_password_reset(request: Request, payload: dict = Body(...),
                                 db: Session = Depends(get_db)):
    """Email a single-use reset link. Always answers the same way (no enumeration)."""
    email = str(payload.get("email") or "").strip().lower()
    generic = {"ok": True, "message": "If that address exists, a reset link has been sent."}
    if not email or "@" not in email:
        return generic
    user = db.query(models.User).filter(
        models.User.email == email, models.User.status == "Active"
    ).first()
    if not user:
        return generic

    raw_token = secrets.token_urlsafe(32)
    db.add(models.PasswordResetToken(
        user_id=user.id, token_hash=_hash_token(raw_token),
        expires_at=datetime.utcnow() + timedelta(minutes=30),
        created_at=datetime.utcnow(),
    ))
    _audit(db, "auth.reset.request", email)
    db.commit()

    settings = _email_settings(db)
    base = str(request.base_url).rstrip("/")
    link = f"{base}/?reset={raw_token}"
    _send_mail(
        db, settings, user.email,
        "[RAW-Switch] Reset your password",
        "Someone asked to reset the password for this account.\n\n"
        f"Open this link within 30 minutes to choose a new password:\n{link}\n\n"
        "If this was not you, ignore this email.",
        alert_key=None,
    )
    db.commit()
    return generic


@app.post("/auth/password-reset/confirm")
async def confirm_password_reset(payload: dict = Body(...), db: Session = Depends(get_db)):
    """Consume a reset token and set a new password."""
    raw_token = str(payload.get("token") or "").strip()
    password = str(payload.get("password") or "")
    if not raw_token:
        raise HTTPException(status_code=422, detail="token is required")
    if len(password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")

    row = db.query(models.PasswordResetToken).filter(
        models.PasswordResetToken.token_hash == _hash_token(raw_token)
    ).first()
    if not row or row.used_at is not None:
        raise HTTPException(status_code=400, detail="This reset link is invalid or has already been used")
    expires = row.expires_at
    if expires is not None and expires.tzinfo is not None:
        expires = expires.replace(tzinfo=None)
    if expires is not None and expires < datetime.utcnow():
        raise HTTPException(status_code=400, detail="This reset link has expired")

    user = db.query(models.User).filter(models.User.id == row.user_id).first()
    if not user:
        raise HTTPException(status_code=400, detail="This reset link is invalid")
    user.password_hash = _password_hash(password)
    row.used_at = datetime.utcnow()
    db.query(models.AuthSession).filter(
        models.AuthSession.user_id == user.id,
        models.AuthSession.revoked_at.is_(None),
    ).update({models.AuthSession.revoked_at: datetime.utcnow()}, synchronize_session=False)
    _audit(db, "auth.reset.confirm", user.email)
    db.commit()
    return {"ok": True, "message": "Password updated. Please sign in."}


@app.get("/auth/2fa/status")
async def two_factor_status(db: Session = Depends(get_db)):
    identity = current_user()
    user = db.query(models.User).filter(models.User.id == identity.get("id")).first()
    if not user:
        raise HTTPException(status_code=401, detail="Session is no longer valid")
    return {"enabled": bool(user.totp_enabled), "hasSecret": bool(user.totp_secret)}


@app.post("/auth/2fa/setup")
async def two_factor_setup(db: Session = Depends(get_db)):
    """Generate a TOTP secret for the signed-in user (not active until confirmed)."""
    identity = current_user()
    user = db.query(models.User).filter(models.User.id == identity.get("id")).first()
    if not user:
        raise HTTPException(status_code=401, detail="Session is no longer valid")
    secret = _new_totp_secret()
    user.totp_secret = secret
    user.totp_enabled = False
    _audit(db, "auth.2fa.setup", user.email)
    db.commit()
    issuer = "RAW-Switch"
    label = quote(f"{issuer}:{user.email}")
    otpauth = (f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}"
               "&algorithm=SHA1&digits=6&period=30")
    return {"secret": secret, "otpauthUrl": otpauth}


@app.post("/auth/2fa/enable")
async def two_factor_enable(payload: dict = Body(...), db: Session = Depends(get_db)):
    identity = current_user()
    user = db.query(models.User).filter(models.User.id == identity.get("id")).first()
    if not user:
        raise HTTPException(status_code=401, detail="Session is no longer valid")
    if not user.totp_secret:
        raise HTTPException(status_code=409, detail="Start the two-factor setup first")
    if not _verify_totp(user.totp_secret, str(payload.get("code") or "")):
        raise HTTPException(status_code=422, detail="That code is not valid")
    user.totp_enabled = True
    _audit(db, "auth.2fa.enabled", user.email)
    db.commit()
    return {"enabled": True}


@app.post("/auth/2fa/disable")
async def two_factor_disable(payload: dict = Body(...), db: Session = Depends(get_db)):
    identity = current_user()
    user = db.query(models.User).filter(models.User.id == identity.get("id")).first()
    if not user:
        raise HTTPException(status_code=401, detail="Session is no longer valid")
    if user.totp_enabled and not _verify_totp(user.totp_secret, str(payload.get("code") or "")):
        raise HTTPException(status_code=422, detail="That code is not valid")
    user.totp_enabled = False
    user.totp_secret = None
    _audit(db, "auth.2fa.disabled", user.email)
    db.commit()
    return {"enabled": False}


@app.get("/auth/me")
async def auth_me(db: Session = Depends(get_db)):
    identity = current_user()
    user = db.query(models.User).filter(models.User.id == identity.get("id")).first()
    if not user:
        raise HTTPException(status_code=401, detail="Session is no longer valid")
    return _user_payload(user)


@app.get("/auth/users")
async def list_users(db: Session = Depends(get_db)):
    _ensure_default_users(db)
    return [_user_payload(user) for user in db.query(models.User).order_by(models.User.id).all()]

@app.post("/auth/users", status_code=201)
async def create_user(payload: dict = Body(...), db: Session = Depends(get_db)):
    _ensure_default_users(db)
    email = str(payload.get("email") or "").strip().lower()
    password = str(payload.get("password") or "")
    if not email or not password or not payload.get("name"):
        raise HTTPException(status_code=422, detail="Name, email and password are required")
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=422, detail="Enter a valid email address")
    try:
        package_gb = float(payload.get("packageGb") or 0)
        used_gb = float(payload.get("usedGb") or 0)
        account_balance = float(
            payload.get("accountBalance") if payload.get("accountBalance") is not None else 248.5
        )
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail="packageGb, usedGb and accountBalance must be numbers") from error
    if db.query(models.User).filter(models.User.email == email).first():
        raise HTTPException(status_code=409, detail="A user with this email already exists")
    user = models.User(
        name=str(payload["name"]).strip(), email=email, password_hash=_password_hash(password),
        role=payload.get("role", "User"), account_role=payload.get("accountRole"),
        permissions=_validated_permissions(payload.get("permissions")) or [],
        status=payload.get("status", "Active"),
        customer_name=payload.get("customerName"), last_login="Never",
        package_gb=package_gb,
        used_gb=used_gb,
        account_balance=account_balance,
    )
    db.add(user)
    _audit(db, "user.create", email, f"role={user.role}")
    db.commit()
    db.refresh(user)
    return _user_payload(user)

@app.patch("/auth/users/{user_id}")
async def update_user(user_id: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if "email" in payload:
        email = str(payload["email"]).strip().lower()
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise HTTPException(status_code=422, detail="Enter a valid email address")
        duplicate = db.query(models.User).filter(models.User.email == email, models.User.id != user_id).first()
        if duplicate:
            raise HTTPException(status_code=409, detail="A user with this email already exists")
        user.email = email
    if "permissions" in payload:
        payload["permissions"] = _validated_permissions(payload["permissions"])
    for key in ("packageGb", "usedGb", "accountBalance"):
        if key in payload:
            try:
                payload[key] = float(payload[key] or 0)
            except (TypeError, ValueError) as error:
                raise HTTPException(status_code=422, detail=f"{key} must be a number") from error
    for key, field in {"name": "name", "role": "role", "accountRole": "account_role",
                       "permissions": "permissions", "status": "status", "customerName": "customer_name",
                       "packageGb": "package_gb", "usedGb": "used_gb", "accountBalance": "account_balance"}.items():
        if key in payload:
            setattr(user, field, payload[key])
    if payload.get("password"):
        user.password_hash = _password_hash(str(payload["password"]))
    _audit(db, "user.update", user.email, ",".join(sorted(payload.keys())))
    db.commit()
    db.refresh(user)
    return _user_payload(user)

@app.delete("/auth/users/{user_id}", status_code=204)
async def delete_user(user_id: int, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    _trash_record(db, "user", user.email, _snapshot(user, [
        "name", "email", "password_hash", "role", "account_role", "permissions",
        "status", "customer_name", "package_gb", "used_gb", "account_balance",
    ]))
    _audit(db, "user.delete", user.email, f"role={user.role}")
    db.delete(user)
    db.commit()


@app.get("/customers")
async def list_customers(db: Session = Depends(get_db)):
    """Registered customer accounts with live fleet, usage and lifecycle totals."""
    devices = db.query(Device).all()
    customers = db.query(Customer).order_by(Customer.name).all()
    return [_customer_payload(customer, devices) for customer in customers]


@app.post("/customers", status_code=201)
async def create_customer(payload: dict = Body(...), db: Session = Depends(get_db)):
    """Register a new customer account so it can own SIMs in the studio."""
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Customer name is required")
    if len(name) > 80:
        raise HTTPException(status_code=422, detail="Customer name must be 80 characters or fewer")
    if db.query(Customer).filter(func.lower(Customer.name) == name.lower()).first():
        raise HTTPException(status_code=409, detail="A customer with this name already exists")
    email = str(payload.get("contactEmail") or "").strip() or None
    if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise HTTPException(status_code=422, detail="contactEmail must be a valid email address")
    status_value = str(payload.get("status") or "Active").strip()
    if status_value not in {"Active", "Suspended"}:
        raise HTTPException(status_code=422, detail="status must be Active or Suspended")
    customer = Customer(
        name=name,
        contact_name=str(payload.get("contactName") or "").strip() or None,
        contact_email=email,
        phone=str(payload.get("phone") or "").strip() or None,
        status=status_value,
        notes=str(payload.get("notes") or "").strip() or None,
    )
    db.add(customer)
    db.commit()
    db.refresh(customer)
    return _customer_payload(customer, [])


@app.patch("/customers/{customer_id}")
async def update_customer(customer_id: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    """Edit a customer account; a rename also moves every SIM it already owns."""
    customer = db.query(Customer).filter(Customer.id == customer_id).first()
    if not customer:
        raise HTTPException(status_code=404, detail="Customer not found")
    previous = customer.name
    if "name" in payload:
        name = str(payload["name"] or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="Customer name cannot be empty")
        if len(name) > 80:
            raise HTTPException(status_code=422, detail="Customer name must be 80 characters or fewer")
        duplicate = db.query(Customer).filter(
            func.lower(Customer.name) == name.lower(), Customer.id != customer_id
        ).first()
        if duplicate:
            raise HTTPException(status_code=409, detail="A customer with this name already exists")
        customer.name = name
    if "contactName" in payload:
        customer.contact_name = str(payload["contactName"] or "").strip() or None
    if "contactEmail" in payload:
        email = str(payload["contactEmail"] or "").strip() or None
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            raise HTTPException(status_code=422, detail="contactEmail must be a valid email address")
        customer.contact_email = email
    if "phone" in payload:
        customer.phone = str(payload["phone"] or "").strip() or None
    if "notes" in payload:
        customer.notes = str(payload["notes"] or "").strip() or None
    if "status" in payload:
        status_value = str(payload["status"] or "").strip()
        if status_value not in {"Active", "Suspended"}:
            raise HTTPException(status_code=422, detail="status must be Active or Suspended")
        customer.status = status_value

    devices = db.query(Device).all()
    if customer.name != previous:
        for device in devices:
            if device.customer == previous:
                device.customer = customer.name
        for user in db.query(models.User).all():
            if user.customer_name == previous:
                user.customer_name = customer.name
    db.commit()
    db.refresh(customer)
    return _customer_payload(customer, devices)


@app.get("/plans")
async def list_plans(db: Session = Depends(get_db)):
    """Sellable data plans with the allowance, price and overage rate per plan."""
    return [_plan_payload(plan) for plan in db.query(Plan).order_by(Plan.data_mb).all()]


@app.patch("/plans/{plan_id}")
async def update_plan(plan_id: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    """Edit a plan (allowance, price, overage, availability) without a redeploy."""
    plan = db.query(Plan).filter(Plan.id == plan_id).first()
    if not plan:
        raise HTTPException(status_code=404, detail="Plan not found")
    if "name" in payload:
        name = str(payload["name"] or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="Plan name cannot be empty")
        duplicate = db.query(Plan).filter(func.lower(Plan.name) == name.lower(), Plan.id != plan_id).first()
        if duplicate:
            raise HTTPException(status_code=409, detail="A plan with this name already exists")
        plan.name = name
    for key, field in {"dataMb": "data_mb", "priceUsd": "price_usd", "overagePerGb": "overage_per_gb"}.items():
        if key in payload:
            try:
                value = float(payload[key] or 0)
            except (TypeError, ValueError) as error:
                raise HTTPException(status_code=422, detail=f"{key} must be a number") from error
            if value < 0:
                raise HTTPException(status_code=422, detail=f"{key} cannot be negative")
            setattr(plan, field, value)
    if "description" in payload:
        plan.description = str(payload["description"] or "").strip() or None
    if "active" in payload:
        plan.is_active = bool(payload["active"])
    db.commit()
    db.refresh(plan)
    return _plan_payload(plan)

PLAN_LIMITS_MB = {
    "500 MB / month": 500,
    "1 GB / month": 1024,
    "5 GB / month": 5120,
    "Pooled data": 0,
}

# Commercial catalogue seeded into the plans table on first boot. The studio reads
# allowance and price from the database, so a plan edit only needs a row update.
PLAN_CATALOG_SEED = [
    {
        "name": "500 MB / month",
        "data_mb": 500,
        "price_usd": 1.50,
        "overage_per_gb": 2.50,
        "description": "Entry plan for low-traffic sensors and meters",
    },
    {
        "name": "1 GB / month",
        "data_mb": 1024,
        "price_usd": 2.90,
        "overage_per_gb": 2.00,
        "description": "Standard plan for trackers, valves and remote monitors",
    },
    {
        "name": "5 GB / month",
        "data_mb": 5120,
        "price_usd": 8.50,
        "overage_per_gb": 1.40,
        "description": "Higher throughput for gateways, cameras and telemetry hubs",
    },
    {
        "name": "Pooled data",
        "data_mb": 0,
        "price_usd": 0.60,
        "overage_per_gb": 0.00,
        "description": "Shared pool metered per MB against the account package",
    },
]


def _plan_payload(plan: Plan) -> dict:
    """Public shape of a plan row used by the studio catalogue and review step."""
    return {
        "id": plan.id,
        "name": plan.name,
        "dataMb": float(plan.data_mb or 0),
        "priceUsd": float(plan.price_usd or 0),
        "overagePerGb": float(plan.overage_per_gb or 0),
        "description": plan.description,
        "active": bool(plan.is_active),
    }


def _plan_map(db: Session) -> dict:
    """Map of plan name -> payload, so SIM payloads can carry real pricing."""
    return {plan.name: _plan_payload(plan) for plan in db.query(Plan).all()}


def _plan_row(db: Session, name: str) -> Plan | None:
    return db.query(Plan).filter(Plan.name == name).first()


def _plan_names(db: Session) -> list:
    return [row.name for row in db.query(Plan).order_by(Plan.data_mb).all()]


def _plan_allowance(db: Session, name: str, fallback: float = 500) -> float:
    """Data allowance for a plan name: the catalogued row wins, then the legacy table."""
    plan = _plan_row(db, name)
    if plan:
        return float(plan.data_mb or 0)
    return float(PLAN_LIMITS_MB.get(name, fallback))


def _customer_payload(customer: Customer, devices: list) -> dict:
    """Customer registry row with the live fleet numbers attached."""
    owned = [device for device in devices if (device.customer or "") == customer.name]
    return {
        "id": customer.id,
        "name": customer.name,
        "contactName": customer.contact_name,
        "contactEmail": customer.contact_email,
        "phone": customer.phone,
        "status": customer.status,
        "notes": customer.notes,
        "sims": len(owned),
        "active": sum(1 for device in owned if str(device.status or "").lower() == "active"),
        "suspended": sum(1 for device in owned if str(device.status or "").lower() == "suspended"),
        "usageMb": round(sum(float(device.usage_mb or 0) for device in owned), 1),
        "dataLimitMb": round(sum(float(device.data_limit_mb or 0) for device in owned), 1),
        "createdAt": customer.created_at.isoformat() if customer.created_at else None,
    }


def _ensure_sim_catalog(db: Session) -> None:
    """Seed the plan catalogue and the customer registry from real fleet data.

    The studio may only offer customers that are registered accounts, so the
    registry is built from the SIM owners already stored in the database plus the
    customer portal users. Seeding runs once, then the tables are the source of
    truth and can be edited without touching code.
    """
    if not db.query(Plan).count():
        for seed in PLAN_CATALOG_SEED:
            db.add(Plan(
                name=seed["name"],
                data_mb=seed["data_mb"],
                price_usd=seed["price_usd"],
                overage_per_gb=seed["overage_per_gb"],
                description=seed["description"],
                is_active=True,
            ))
        db.commit()

    if not db.query(Customer).count():
        portal_users = db.query(models.User).filter(models.User.role == "Customer").all()
        names = {
            str(name).strip()
            for (name,) in db.query(Device.customer).distinct().all()
            if name and str(name).strip() and str(name).strip() != "Unassigned"
        }
        for user in portal_users:
            if user.customer_name and user.customer_name.strip():
                names.add(user.customer_name.strip())
        for name in sorted(names):
            owner = next((user for user in portal_users if (user.customer_name or "").strip() == name), None)
            db.add(Customer(
                name=name,
                contact_name=owner.name if owner else None,
                contact_email=owner.email if owner else None,
                status="Active",
                notes="Registered from existing SIM ownership",
            ))
        db.commit()


# One switch per key: the dashboard renders these names and PATCHes the same
# names back. A key missing here is silently dropped, so every switch the UI
# draws has to exist in this map.
ROAMING_POLICY_DEFAULTS = {
    "International roaming": True,
    "Automatic network selection": True,
    "Network failover": True,
    "Data cap protection": False,
    "Data roaming": True,
}

# Older builds stored the failover switch as "Automatic failover". Keep reading
# that row so an upgrade cannot silently re-enable failover.
ROAMING_POLICY_ALIASES = {"Automatic failover": "Network failover"}


def _roaming_policy(db: Session, key: str) -> bool:
    key = ROAMING_POLICY_ALIASES.get(key, key)
    policy = db.query(RoamingPolicy).filter(RoamingPolicy.policy_key == key).first()
    if policy is not None:
        return bool(policy.enabled)
    for legacy_key, canonical in ROAMING_POLICY_ALIASES.items():
        if canonical != key:
            continue
        legacy = db.query(RoamingPolicy).filter(RoamingPolicy.policy_key == legacy_key).first()
        if legacy is not None:
            return bool(legacy.enabled)
    return ROAMING_POLICY_DEFAULTS.get(key, True)


def _effective_roaming_enabled(db: Session, requested: bool) -> bool:
    return bool(requested) and _roaming_policy(db, "International roaming") and _roaming_policy(db, "Data roaming")


@app.get("/roaming/policies")
async def get_roaming_policies(db: Session = Depends(get_db)):
    saved = {policy.policy_key: policy.enabled for policy in db.query(RoamingPolicy).all()}
    return {key: saved.get(key, default) for key, default in ROAMING_POLICY_DEFAULTS.items()}


@app.patch("/roaming/policies")
async def update_roaming_policies(payload: dict = Body(...), db: Session = Depends(get_db)):
    if not payload:
        raise HTTPException(status_code=422, detail="At least one roaming policy is required")
    known = set(ROAMING_POLICY_DEFAULTS) | set(ROAMING_POLICY_ALIASES)
    unknown = [key for key in payload if key not in known]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown roaming policy: {', '.join(sorted(unknown))}",
        )
    for raw_key, value in payload.items():
        key = ROAMING_POLICY_ALIASES.get(raw_key, raw_key)
        if not isinstance(value, bool):
            raise HTTPException(status_code=422, detail=f"{raw_key} must be a boolean")
        policy = db.query(RoamingPolicy).filter(RoamingPolicy.policy_key == key).first()
        if not policy:
            policy = RoamingPolicy(policy_key=key, enabled=value)
            db.add(policy)
        else:
            policy.enabled = value
    if payload.get("International roaming") is False or payload.get("Data roaming") is False:
        db.query(Device).update({Device.roaming_enabled: False}, synchronize_session=False)
    db.commit()
    return await get_roaming_policies(db)

def _device_payload(device: Device) -> dict:
    return {
        "id": device.id,
        "iccid": device.iccid,
        "device": device.device_name,
        "type": device.sim_type,
        "networkTech": device.network_tech,
        "customer": device.customer,
        "location": device.region,
        "usageMb": device.usage_mb,
        "dataPlan": device.data_plan,
        "dataLimitMb": device.data_limit_mb,
        "roamingEnabled": device.roaming_enabled,
        "status": device.status,
        "network": device.telecom_network.name if device.telecom_network else (device.carrier.name if device.carrier else "Auto select"),
        "telecomNetworkId": device.telecom_network_id,
        "createdAt": device.created_at.isoformat() if device.created_at else None,
    }

@app.get("/inventory")
async def list_inventory(db: Session = Depends(get_db)):
    """Return the SIM inventory, scoped to the caller's customer when relevant."""
    query = db.query(Device)
    if is_customer():
        query = query.filter(func.lower(Device.customer) == customer_scope().lower())
    return [_device_payload(device) for device in query.order_by(Device.created_at.desc()).all()]


@app.get("/system/health")
async def system_health(db: Session = Depends(get_db)):
    """Return safe backend and database runtime metrics for the admin dashboard."""
    checked_at = datetime.utcnow()
    try:
        db.execute(text("SELECT 1"))
        database_status = "online"
    except SQLAlchemyError as error:
        database_status = "error"
        raise HTTPException(status_code=503, detail=f"Database health check failed: {error}") from error
    latest_events = (
        db.query(NetworkEvent, Device.iccid, Device.device_name)
        .join(Device, NetworkEvent.device_id == Device.id)
        .order_by(NetworkEvent.timestamp.desc())
        .limit(10)
        .all()
    )
    return {
        "status": "online",
        "checkedAt": checked_at.isoformat(),
        "database": {"status": database_status, "engine": config.DATABASE_URL.split(":", 1)[0]},
        "counts": {
            "devices": db.query(Device).count(),
            "activeDevices": db.query(Device).filter(Device.status == "active").count(),
            "telecomNetworks": db.query(TelecomNetwork).count(),
            "rspProviders": db.query(RspProvider).count(),
            "euiccs": db.query(Euicc).count(),
            "rspOperations": db.query(RspOperation).count(),
        },
        "recentEvents": [{
            "timestamp": event.timestamp.isoformat() if event.timestamp else None,
            "iccid": iccid,
            "device": device_name,
            "network": event.network_name,
            "status": event.status,
            "latencyMs": event.latency_ms,
            "bandwidthMbps": event.bandwidth_mbps,
        } for event, iccid, device_name in latest_events],
    }

@app.post("/inventory", status_code=201)
async def provision_inventory(payload: dict = Body(...), db: Session = Depends(get_db)):
    """Create a SIM inventory record and optionally an eSIM profile."""
    iccid = str(payload.get("iccid") or "").strip()
    if not iccid or not iccid.isdigit() or not 15 <= len(iccid) <= 22:
        raise HTTPException(status_code=422, detail="ICCID must contain 15 to 22 digits")
    if db.query(Device).filter(Device.iccid == iccid).first():
        raise HTTPException(status_code=409, detail="ICCID already exists")
    sim_type = payload.get("type", "IoT SIM")
    plan = str(payload.get("dataPlan", "500 MB / month") or "").strip()
    if not _plan_row(db, plan):
        raise HTTPException(status_code=422, detail=f"Unknown data plan '{plan}'. Choose one of: {', '.join(_plan_names(db))}")
    device = Device(
        iccid=iccid,
        device_name=str(payload.get("device") or "Unnamed device").strip(),
        sim_type=sim_type,
        network_tech=payload.get("networkTech", "4G"),
        customer=payload.get("customer", "Unassigned"),
        region=payload.get("region", "Global"),
        data_plan=plan,
        data_limit_mb=_plan_allowance(db, plan),
        roaming_enabled=_effective_roaming_enabled(db, bool(payload.get("roamingEnabled", True))),
        status="active",
    )
    db.add(device)
    db.commit()
    db.refresh(device)
    return _device_payload(device)


@app.post("/inventory/bulk", status_code=207)
async def bulk_provision_inventory(payload: dict = Body(...), db: Session = Depends(get_db)):
    """Provision up to 500 SIM records and return a result for every input row."""
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=422, detail="items must be a non-empty array")
    if len(items) > 500:
        raise HTTPException(status_code=422, detail="A maximum of 500 SIMs can be provisioned per batch")
    results = []
    seen = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            results.append({"row": index, "status": "failed", "error": "Each row must be an object"})
            continue
        iccid = str(item.get("iccid") or "").strip()
        if not iccid or not iccid.isdigit() or not 15 <= len(iccid) <= 22:
            results.append({"row": index, "iccid": iccid, "status": "failed", "error": "ICCID must contain 15 to 22 digits"})
            continue
        if iccid in seen or db.query(Device).filter(Device.iccid == iccid).first():
            results.append({"row": index, "iccid": iccid, "status": "failed", "error": "ICCID already exists"})
            continue
        seen.add(iccid)
        plan = str(item.get("dataPlan", "500 MB / month") or "").strip()
        if not _plan_row(db, plan):
            results.append({"row": index, "iccid": iccid, "status": "failed", "error": f"Unknown data plan '{plan}'"})
            continue
        device = Device(
            iccid=iccid,
            device_name=str(item.get("device") or "Unnamed device").strip(),
            sim_type=item.get("type", "IoT SIM"),
            network_tech=item.get("networkTech", "4G"),
            customer=item.get("customer", "Unassigned"),
            region=item.get("region", "Global"),
            data_plan=plan,
            data_limit_mb=_plan_allowance(db, plan),
            roaming_enabled=_effective_roaming_enabled(db, bool(item.get("roamingEnabled", True))),
            status="active",
        )
        db.add(device)
        db.flush()
        results.append({"row": index, "iccid": iccid, "status": "created", "device": _device_payload(device)})
    db.commit()
    created = sum(result["status"] == "created" for result in results)
    return {"total": len(items), "created": created, "failed": len(items) - created, "results": results}

@app.patch("/inventory/{iccid}")
async def update_inventory(iccid: str, payload: dict = Body(...), db: Session = Depends(get_db)):
    """Update lifecycle, assignment, roaming, plan, or usage fields."""
    device = db.query(Device).filter(Device.iccid == iccid).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    allowed = {
        "device": "device_name", "type": "sim_type", "networkTech": "network_tech",
        "customer": "customer", "region": "region", "status": "status",
        "roamingEnabled": "roaming_enabled", "usageMb": "usage_mb",
        "dataLimitMb": "data_limit_mb",
    }
    for key, field in allowed.items():
        if key in payload:
            setattr(device, field, payload[key])
    if "roamingEnabled" in payload:
        device.roaming_enabled = _effective_roaming_enabled(db, bool(payload["roamingEnabled"]))
    if "dataPlan" in payload:
        plan_name = str(payload["dataPlan"] or "").strip()
        if not _plan_row(db, plan_name):
            raise HTTPException(status_code=422, detail=f"Unknown data plan '{plan_name}'")
        device.data_plan = plan_name
        device.data_limit_mb = _plan_allowance(db, plan_name, device.data_limit_mb)
    db.commit()
    db.refresh(device)
    return _device_payload(device)

@app.post("/inventory/{iccid}/usage")
async def record_usage(iccid: str, payload: dict = Body(...), db: Session = Depends(get_db)):
    """Add usage in MB and return the updated allowance state."""
    device = db.query(Device).filter(Device.iccid == iccid).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    amount = payload.get("megabytes")
    if not isinstance(amount, (int, float)) or amount < 0:
        raise HTTPException(status_code=422, detail="megabytes must be a non-negative number")
    device.usage_mb += amount
    db.commit()
    return {"iccid": iccid, "usageMb": device.usage_mb, "dataLimitMb": device.data_limit_mb,
            "overLimit": bool(device.data_limit_mb and device.usage_mb > device.data_limit_mb)}

@app.post("/devices/", response_model=dict)
async def create_device(iccid: str, db: Session = Depends(get_db)):
    """Provision a new SIM device."""
    iccid = str(iccid or "").strip()
    if not iccid.isdigit() or not 15 <= len(iccid) <= 22:
        raise HTTPException(status_code=422, detail="ICCID must contain 15 to 22 digits")
    # Check if device exists
    existing = db.query(models.Device).filter(models.Device.iccid == iccid).first()
    if existing:
        raise HTTPException(status_code=400, detail="Device ICCID already exists")
    # Associate with a default carrier (e.g., carrier_a) if not specified
    carrier = db.query(models.Carrier).filter(models.Carrier.name == "carrier_a").first()
    if not carrier:
        carrier = Carrier(name="carrier_a", api_endpoint="https://api.carrier-a.com", api_key="key_a", bandwidth_mbps=50, reliability_score=0.95)
        db.add(carrier)
        db.commit()
        db.refresh(carrier)
    device = Device(iccid=iccid, carrier_id=carrier.id, status="provisioned")
    db.add(device)
    db.commit()
    db.refresh(device)
    return {"device_id": device.id, "iccid": device.iccid, "carrier": carrier.name}

@app.get("/devices/{iccid}/status")
async def device_status(iccid: str, db: Session = Depends(get_db)):
    """Get current network status for a device."""
    device = db.query(models.Device).filter(models.Device.iccid == iccid).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    # Fetch latest network event
    event = db.query(models.NetworkEvent).filter(models.NetworkEvent.device_id == device.id).order_by(models.NetworkEvent.timestamp.desc()).first()
    return {
        "iccid": device.iccid,
        "current_carrier": device.carrier.name if device.carrier else None,
        "status": device.status,
        "last_network_event": {
            "network_name": event.network_name if event else None,
            "bandwidth_mbps": event.bandwidth_mbps if event else None,
            "latency_ms": event.latency_ms if event else None,
            "timestamp": event.timestamp.isoformat() if event else None,
            "status": event.status if event else None,
        } if event else None,
    }

@app.post("/devices/{iccid}/switch")
async def trigger_switch(iccid: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """Trigger automated network switch for device."""
    if not _roaming_policy(db, "Network failover"):
        raise HTTPException(status_code=409, detail="Network failover is disabled by the active roaming policy")
    device = db.query(models.Device).filter(models.Device.iccid == iccid).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    # Select best carrier based on current status
    # For simplicity, just pick the current carrier's fallback
    best = await switching.select_best_carrier(device, None)  # placeholder
    if not best:
        raise HTTPException(status_code=400, detail="No suitable carrier found")
    # --- eSIM profile handling ---
    # Check if a profile already exists for this device and the new carrier
    existing_profile = db.query(models.EsimProfile).filter(models.EsimProfile.device_id == device.id).first()
    if not existing_profile or existing_profile.carrier_name != best.name:
        # generate a stub profile (replace with real GSMA RSP profile generation if needed)
        profile_dict = {"id": str(uuid.uuid4()), "carrier_name": best.name, "msisdn": "+15555555555"}
        profile_json = json.dumps(profile_dict)
        new_profile = models.EsimProfile(
            device_id=device.id,
            carrier_name=best.name,
            profile_xml=profile_json,
            activated_at=datetime.utcnow(),
            status="active",
        )
        db.add(new_profile)
        db.commit()
        db.refresh(new_profile)
    # ---------------------------------
    # Background task to perform switch
    background_tasks.add_task(switching.switch_network, device.id, best, db)
    return {"message": f"Network switch initiated to {best.name}", "device_iccid": iccid}

def _collect_alerts(db: Session, customer: str | None = None) -> list:
    """Operational alerts derived from live data (usage, invoices, payments).

    Customers pass their own name so they only see their fleet and invoices.
    """
    now = datetime.utcnow()
    alerts = []

    def add(kind, severity, title, detail, view, key=None):
        alerts.append({
            "id": f"{kind}:{key if key is not None else len(alerts)}",
            "type": kind, "severity": severity, "title": title,
            "detail": detail, "view": view, "key": key,
        })

    devices = db.query(Device)
    if customer:
        devices = devices.filter(func.lower(Device.customer) == customer.lower())
    for device in devices.order_by(Device.id).all():
        status = str(device.status or "").lower()
        if status == "terminated":
            continue
        limit = float(device.data_limit_mb or 0)
        used = float(device.usage_mb or 0)
        name = device.device_name or device.iccid
        if limit > 0 and used >= limit:
            add("usage", "critical", f"{name} hit its data cap",
                f"{used:.0f} of {limit:.0f} MB used", "sims", device.iccid)
        elif limit > 0 and used >= limit * 0.8:
            add("usage", "warning", f"{name} is near its data cap",
                f"{used:.0f} of {limit:.0f} MB used", "sims", device.iccid)
        if status == "suspended":
            add("status", "info", f"{name} is suspended",
                "Review the SIM to restore service", "sims", device.iccid)

    invoices = db.query(models.Invoice).filter(models.Invoice.status.in_(("issued", "part_paid")))
    if customer:
        invoices = invoices.filter(func.lower(models.Invoice.customer) == customer.lower())
    for invoice in invoices.all():
        due = invoice.due_at
        if due is None:
            continue
        if due.tzinfo is not None:
            due = due.replace(tzinfo=None)
        days = (due - now).total_seconds() / 86400
        balance = float(invoice.total or 0) - float(invoice.paid_total or 0)
        if days < 0:
            add("invoice", "critical", f"Invoice {invoice.number} is overdue",
                f"{invoice.customer} owes {balance:.2f} {invoice.currency}", "billing", invoice.id)
        elif days <= 2:
            add("invoice", "warning", f"Invoice {invoice.number} is due soon",
                f"Due {due.date().isoformat()} - {balance:.2f} {invoice.currency}", "billing", invoice.id)

    payments = db.query(models.Payment).filter(models.Payment.status == "pending")
    if customer:
        payments = payments.filter(func.lower(models.Payment.customer) == customer.lower())
    for payment in payments.all():
        created = payment.created_at
        if created is None:
            continue
        if created.tzinfo is not None:
            created = created.replace(tzinfo=None)
        if (now - created).total_seconds() > 86400:
            add("payment", "warning", f"Payment {payment.reference} is still pending",
                "Check with the payer or cancel the checkout", "billing", payment.id)

    order = {"critical": 0, "warning": 1, "info": 2}
    alerts.sort(key=lambda item: order.get(item["severity"], 3))
    return alerts


@app.get("/alerts")
async def list_alerts(customer: str | None = Query(default=None), db: Session = Depends(get_db)):
    if is_customer():
        customer = customer_scope()
    alerts = _collect_alerts(db, customer)
    return {"count": len(alerts), "generatedAt": datetime.utcnow().isoformat(),
            "alerts": alerts[:60]}


@app.get("/marketplace")
async def list_marketplace(db: Session = Depends(get_db)):
    """
    Returns the network marketplace: countries mapped to the carriers (provisions)
    the platform has signed with.
    """
    # Distinct over the country column only. Query.distinct(<column>) keeps the
    # whole row distinct, so a country with two carriers was listed twice.
    country_names = [
        row[0]
        for row in db.query(models.Carrier.country)
        .distinct()
        .order_by(models.Carrier.country)
        .all()
    ]

    marketplace = []
    for name in country_names:
        name = name or "Unknown"
        carriers = db.query(models.Carrier).filter(
            models.Carrier.country == name,
            models.Carrier.is_active == True,
        ).order_by(models.Carrier.bandwidth_mbps.desc()).all()

        marketplace.append({
            "country": name,
            "total_carriers": len(carriers),
            "active_carriers": sum(1 for c in carriers if c.is_active),
            "carriers": [{
                "id": c.id,
                "name": c.name,
                "bandwidth_mbps": c.bandwidth_mbps,
                "reliability_score": c.reliability_score,
                "is_active": c.is_active,
            } for c in carriers],
        })

    return {
        "total_countries": len(marketplace),
        "total_provisioned_carriers": db.query(models.Carrier).filter(models.Carrier.is_active == True).count(),
        "snapshot": datetime.utcnow().isoformat(),
        "marketplace": marketplace,
    }


@app.post("/marketplace", status_code=201)
async def create_marketplace_carrier(payload: dict = Body(...), db: Session = Depends(get_db)):
    """
    Register a new carrier in the marketplace.
    Body: { name, country, api_endpoint, api_key, bandwidth_mbps, reliability_score, is_active }
    """
    name = str(payload.get("name") or "").strip()
    country = str(payload.get("country") or "Unknown").strip()
    # The dashboard speaks camelCase; older API clients sent snake_case. Accept both.
    api_endpoint = str(payload.get("apiEndpoint") or payload.get("api_endpoint") or "").strip()
    api_key = str(payload.get("apiKey") or payload.get("api_key") or "").strip()

    if not name or not country or not api_endpoint or not api_key:
        raise HTTPException(status_code=422, detail="name, country, apiEndpoint and apiKey are required")

    if not api_endpoint.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="apiEndpoint must use http:// or https://")

    if db.query(models.Carrier).filter(models.Carrier.name == name).first():
        raise HTTPException(status_code=409, detail=f"Carrier '{name}' already exists")

    try:
        bandwidth = float(payload.get("bandwidthMbps", payload.get("bandwidth_mbps")) or 0)
        reliability = float(payload.get("reliabilityScore", payload.get("reliability_score")) or 1.0)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail="bandwidthMbps and reliabilityScore must be numbers") from error
    if bandwidth < 0:
        raise HTTPException(status_code=422, detail="bandwidthMbps cannot be negative")
    if not 0 <= reliability <= 1:
        raise HTTPException(status_code=422, detail="reliabilityScore must be between 0 and 1")

    carrier = models.Carrier(
        name=name,
        country=country,
        api_endpoint=api_endpoint,
        api_key=api_key,
        bandwidth_mbps=bandwidth,
        reliability_score=reliability,
        is_active=bool(payload.get("isActive", payload.get("is_active", True))),
    )
    links = _carrier_network_links_from_payload(db, payload)
    if links is not None:
        carrier.networks = links
        carrier.telecom_network_id = links[0].id if links else None
    db.add(carrier)
    _audit(db, "carrier.create", name, country)
    db.commit()
    db.refresh(carrier)

    return _carrier_payload(carrier)

# -------------------------------------------------------------------#
# Webhook endpoint for carrier events (e.g., from gateway hardware)
# -------------------------------------------------------------------#

@app.post("/webhooks/carrier-event")
async def carrier_event_hook(
    payload: dict,
    db: Session = Depends(get_db),
):
    """Receive carrier-side events (status change, bandwidth update, etc.)."""
    device_iccid = payload.get("iccid")
    if not device_iccid:
        raise HTTPException(status_code=400, detail="Missing 'iccid' in payload")

    device = db.query(models.Device).filter(models.Device.iccid == device_iccid).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    # Create a network event log entry
    event = models.NetworkEvent(
        device_id=device.id,
        network_name=payload.get("network_name", device.carrier.name if device.carrier else "unknown"),
        bandwidth_mbps=payload.get("bandwidth_mbps", 0.0),
        latency_ms=payload.get("latency_ms", 0.0),
        rssi=payload.get("rssi"),
        status=payload.get("status", "connected"),
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    return {"status": "ok", "event_id": event.id}


# ----------------------------------------------------------------------
# eSIM / GSMA Remote SIM Provisioning endpoints
# ----------------------------------------------------------------------


def _network_payload(network: TelecomNetwork) -> dict:
    return {
        "id": network.id,
        "name": network.name,
        "country": network.country,
        "apiBaseUrl": network.api_base_url,
        "authType": network.auth_type,
        "isActive": network.is_active,
        "scope": network.scope or "local",
        "coverage": network.coverage,
        "hasApiKey": bool(network.api_key),
        "createdAt": network.created_at.isoformat() if network.created_at else None,
    }


PROVIDER_SCOPES = {"local", "international"}


def _provider_scope(payload: dict) -> str:
    scope = str(payload.get("scope") or "local").strip().lower()
    if scope not in PROVIDER_SCOPES:
        raise HTTPException(status_code=422, detail="scope must be 'local' or 'international'")
    return scope


def _provider_coverage(payload: dict) -> str | None:
    """International providers describe their footprint; local ones do not."""
    scope = _provider_scope(payload)
    coverage = str(payload.get("coverage") or "").strip()
    if scope == "international":
        return coverage or "Global"
    return coverage or None


@app.get("/telecom/networks")
async def list_telecom_networks(db: Session = Depends(get_db)):
    """List configured local telecom/RSP providers without exposing credentials."""
    return [_network_payload(network) for network in db.query(TelecomNetwork).order_by(TelecomNetwork.name).all()]


@app.post("/telecom/networks", status_code=201)
async def create_telecom_network(payload: dict = Body(...), db: Session = Depends(get_db)):
    name = str(payload.get("name") or "").strip()
    endpoint = str(payload.get("apiBaseUrl") or "").strip().rstrip("/")
    api_key = str(payload.get("apiKey") or "").strip()
    if not name or not endpoint or not api_key:
        raise HTTPException(status_code=422, detail="Name, API base URL and API key are required")
    if not endpoint.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="API base URL must use http:// or https://")
    if db.query(TelecomNetwork).filter(TelecomNetwork.name == name).first():
        raise HTTPException(status_code=409, detail="A telecom network with this name already exists")
    network = TelecomNetwork(
        name=name, country=str(payload.get("country") or "Unknown").strip(),
        api_base_url=endpoint, api_key=api_key,
        auth_type=str(payload.get("authType") or "bearer").lower(),
        is_active=bool(payload.get("isActive", True)),
        scope=_provider_scope(payload),
        coverage=_provider_coverage(payload),
    )
    db.add(network)
    _audit(db, "network.create", name, str(payload.get("country") or ""))
    db.commit()
    db.refresh(network)
    return _network_payload(network)


@app.patch("/telecom/networks/{network_id}")
async def update_telecom_network(network_id: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    network = db.query(TelecomNetwork).filter(TelecomNetwork.id == network_id).first()
    if not network:
        raise HTTPException(status_code=404, detail="Telecom network not found")
    if "name" in payload:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="name cannot be empty")
        duplicate = (
            db.query(TelecomNetwork)
            .filter(func.lower(TelecomNetwork.name) == name.lower(), TelecomNetwork.id != network_id)
            .first()
        )
        if duplicate:
            raise HTTPException(status_code=409, detail="A telecom network with this name already exists")
        network.name = name
    if "country" in payload:
        network.country = str(payload.get("country") or "Unknown").strip() or "Unknown"
    if "authType" in payload:
        network.auth_type = str(payload.get("authType") or "bearer").lower()
    if "isActive" in payload:
        network.is_active = bool(payload["isActive"])
    if "scope" in payload or "coverage" in payload:
        merged = {
            "scope": payload.get("scope", network.scope),
            "coverage": payload.get("coverage", network.coverage),
        }
        network.scope = _provider_scope(merged)
        network.coverage = _provider_coverage(merged)
    if payload.get("apiBaseUrl"):
        url = str(payload["apiBaseUrl"]).strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=422, detail="API base URL must use http:// or https://")
        network.api_base_url = url
    if payload.get("apiKey"):
        network.api_key = str(payload["apiKey"]).strip()
    _audit(db, "network.update", network.name, ",".join(sorted(payload.keys())))
    db.commit()
    db.refresh(network)
    return _network_payload(network)


@app.delete("/telecom/networks/{network_id}")
async def delete_telecom_network(network_id: int, db: Session = Depends(get_db)):
    """Remove a provider; the snapshot stays in Trash for restore."""
    network = db.query(TelecomNetwork).filter(TelecomNetwork.id == network_id).first()
    if not network:
        raise HTTPException(status_code=404, detail="Telecom network not found")
    linked_carriers = [
        row[0] for row in db.query(models.carrier_networks.c.carrier_id)
        .filter(models.carrier_networks.c.network_id == network_id).all()
    ]
    _trash_record(db, "network", network.name, _snapshot(network, [
        "name", "country", "api_base_url", "api_key", "auth_type",
        "is_active", "scope", "coverage",
    ]) | {"carrier_ids": linked_carriers})
    db.execute(models.carrier_networks.delete().where(
        models.carrier_networks.c.network_id == network_id
    ))
    _audit(db, "network.delete", network.name)
    db.delete(network)
    db.commit()
    return {"id": network_id, "deleted": True, "trashed": True}


@app.post("/telecom/networks/{network_id}/test")
async def test_telecom_network(network_id: int, db: Session = Depends(get_db)):
    network = db.query(TelecomNetwork).filter(TelecomNetwork.id == network_id).first()
    if not network:
        raise HTTPException(status_code=404, detail="Telecom network not found")
    headers = {"Accept": "application/json"}
    if network.auth_type == "bearer":
        headers["Authorization"] = f"Bearer {network.api_key}"
    else:
        headers["X-API-Key"] = network.api_key
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{network.api_base_url}/health", headers=headers) as response:
                body = await response.text()
                if response.status >= 400:
                    raise HTTPException(status_code=502, detail=f"Provider health check returned HTTP {response.status}")
                return {"networkId": network.id, "status": "online", "providerStatus": body[:500]}
    except HTTPException:
        raise
    except (aiohttp.ClientError, TimeoutError) as error:
        raise HTTPException(status_code=502, detail=f"Unable to reach telecom network: {error}") from error


@app.post("/telecom/networks/{network_id}/attach", status_code=202)
async def attach_sim_to_network(network_id: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    """Attach a physical SIM/iSIM to a provider network through its activation API."""
    iccid = str(payload.get("iccid") or "").strip()
    device = db.query(Device).filter(Device.iccid == iccid).first()
    network = db.query(TelecomNetwork).filter(TelecomNetwork.id == network_id, TelecomNetwork.is_active.is_(True)).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device ICCID not found")
    if not network:
        raise HTTPException(status_code=404, detail="Active telecom network not found")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers["Authorization" if network.auth_type == "bearer" else "X-API-Key"] = (
        f"Bearer {network.api_key}" if network.auth_type == "bearer" else network.api_key
    )
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{network.api_base_url}/sim/attach", headers=headers,
                                    json={"iccid": iccid, "networkTech": device.network_tech}) as response:
                if response.status >= 400:
                    raise HTTPException(status_code=502, detail=f"Provider attach request returned HTTP {response.status}")
        device.telecom_network_id = network.id
        device.status = "active"
        db.add(NetworkEvent(device_id=device.id, network_name=network.name,
                             bandwidth_mbps=0, latency_ms=0, status="connected"))
        db.commit()
        return {"iccid": iccid, "networkId": network.id, "network": network.name, "status": "attached"}
    except HTTPException:
        raise
    except (aiohttp.ClientError, TimeoutError) as error:
        raise HTTPException(status_code=502, detail=f"SIM attach request failed: {error}") from error


@app.post("/telecom/networks/{network_id}/bind", status_code=201)
async def bind_sim_to_network(network_id: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    """Persist a SIM-to-network association without requiring a provider API call."""
    iccid = str(payload.get("iccid") or "").strip()
    network = db.query(TelecomNetwork).filter(
        TelecomNetwork.id == network_id, TelecomNetwork.is_active.is_(True)
    ).first()
    if not network:
        raise HTTPException(status_code=404, detail="Active telecom network not found; configure a network first")
    device = db.query(Device).filter(Device.iccid == iccid).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device ICCID not found; select a registered SIM")
    device.telecom_network_id = network.id
    db.add(NetworkEvent(
        device_id=device.id, network_name=network.name,
        bandwidth_mbps=0, latency_ms=0, status="connected",
    ))
    db.commit()
    db.refresh(device)
    return _device_payload(device)


# =============================================================================
# Network marketplace - country & carrier discovery
# =============================================================================

def _carrier_payload(carrier: models.Carrier) -> dict:
    networks = list(carrier.networks or [])
    return {
        "id": carrier.id,
        "name": carrier.name,
        "country": carrier.country,
        "apiEndpoint": carrier.api_endpoint,
        "bandwidthMbps": carrier.bandwidth_mbps,
        "reliabilityScore": carrier.reliability_score,
        "isActive": carrier.is_active,
        "telecomNetworkId": networks[0].id if networks else None,
        "telecomNetworkIds": [network.id for network in networks],
        "networks": [
            {
                "id": network.id,
                "name": network.name,
                "scope": network.scope or "local",
                "coverage": network.coverage,
            }
            for network in networks
        ],
    }


def _carrier_network_links_from_payload(db: Session, payload: dict):
    """Resolve the provider links a carrier payload asks for.

    Accepts telecomNetworkIds (list), the legacy single telecomNetworkId, and
    the snake_case spellings. Returns None when the payload does not mention
    providers at all, so PATCH can leave existing links untouched.
    """
    if "telecomNetworkIds" in payload:
        raw = payload.get("telecomNetworkIds")
    elif "telecom_network_ids" in payload:
        raw = payload.get("telecom_network_ids")
    elif "telecomNetworkId" in payload:
        raw = [payload.get("telecomNetworkId")]
    elif "telecom_network_id" in payload:
        raw = [payload.get("telecom_network_id")]
    else:
        return None

    if raw is None:
        return []
    if not isinstance(raw, (list, tuple, set)):
        raw = [raw]

    resolved = []
    seen = set()
    for value in raw:
        if value in (None, "", 0, "0"):
            continue
        try:
            network_id = int(value)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail="telecomNetworkIds must be integers") from error
        if network_id in seen:
            continue
        network = db.query(TelecomNetwork).filter(TelecomNetwork.id == network_id).first()
        if not network:
            raise HTTPException(status_code=422, detail=f"Unknown telecom network id {network_id}")
        seen.add(network_id)
        resolved.append(network)
    return resolved


@app.get("/marketplace/countries")
async def list_marketplace_countries(db: Session = Depends(get_db)):
    """List all countries that have at least one signed carrier, with counts."""
    rows = (
        db.query(
            models.Carrier.country,
            func.count(models.Carrier.id).label("carrier_count"),
            func.count(
                case((models.Carrier.is_active == True, 1), else_=None)
            ).label("active_count"),
        )
        .group_by(models.Carrier.country)
        .order_by(models.Carrier.country.asc())
        .all()
    )
    return [
        {
            "country": r.country,
            "carrierCount": r.carrier_count,
            "activeCount": r.active_count,
        }
        for r in rows
    ]


@app.get("/marketplace/country/{country}/carriers")
async def list_country_carriers(country: str, db: Session = Depends(get_db)):
    """List all carriers signed in a specific country."""
    carriers = (
        db.query(models.Carrier)
        .filter(models.Carrier.country == country)
        .order_by(models.Carrier.name.asc())
        .all()
    )
    return [_carrier_payload(c) for c in carriers]


@app.get("/marketplace/carriers")
async def list_all_marketplace_carriers(db: Session = Depends(get_db)):
    """List every signed carrier, grouped by country."""
    carriers = (
        db.query(models.Carrier)
        .order_by(models.Carrier.country.asc(), models.Carrier.name.asc())
        .all()
    )
    return [_carrier_payload(c) for c in carriers]


@app.patch("/marketplace/{carrier_id}")
async def update_marketplace_carrier(
    carrier_id: int, payload: dict = Body(...), db: Session = Depends(get_db)
):
    """Update a signed carrier. Fields left out of the body keep their value."""
    carrier = db.query(models.Carrier).filter(models.Carrier.id == carrier_id).first()
    if not carrier:
        raise HTTPException(status_code=404, detail="Carrier not found")

    if "name" in payload:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="name cannot be empty")
        clash = (
            db.query(models.Carrier)
            .filter(models.Carrier.name == name, models.Carrier.id != carrier.id)
            .first()
        )
        if clash:
            raise HTTPException(status_code=409, detail=f"Carrier '{name}' already exists")
        carrier.name = name

    if "country" in payload:
        carrier.country = str(payload.get("country") or "Unknown").strip() or "Unknown"

    for key in ("apiEndpoint", "api_endpoint"):
        if key in payload:
            endpoint = str(payload.get(key) or "").strip()
            if not endpoint.startswith(("http://", "https://")):
                raise HTTPException(status_code=422, detail="apiEndpoint must use http:// or https://")
            carrier.api_endpoint = endpoint

    # An empty key means "keep the stored credential", so the dashboard can edit
    # the rest of the carrier without ever holding the secret.
    for key in ("apiKey", "api_key"):
        if key in payload and str(payload.get(key) or "").strip():
            carrier.api_key = str(payload[key]).strip()

    if "bandwidthMbps" in payload or "bandwidth_mbps" in payload:
        raw = payload.get("bandwidthMbps", payload.get("bandwidth_mbps"))
        try:
            carrier.bandwidth_mbps = max(0.0, float(raw))
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail="bandwidthMbps must be a number") from error

    if "reliabilityScore" in payload or "reliability_score" in payload:
        raw = payload.get("reliabilityScore", payload.get("reliability_score"))
        try:
            score = float(raw)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail="reliabilityScore must be a number") from error
        if not 0 <= score <= 1:
            raise HTTPException(status_code=422, detail="reliabilityScore must be between 0 and 1")
        carrier.reliability_score = score

    links = _carrier_network_links_from_payload(db, payload)
    if links is not None:
        carrier.networks = links
        carrier.telecom_network_id = links[0].id if links else None

    _audit(db, "carrier.update", carrier.name, ",".join(sorted(payload.keys())))
    if "isActive" in payload:
        carrier.is_active = bool(payload["isActive"])

    db.commit()
    db.refresh(carrier)
    return _carrier_payload(carrier)


@app.delete("/marketplace/{carrier_id}")
async def delete_marketplace_carrier(carrier_id: int, db: Session = Depends(get_db)):
    """Remove a carrier, first detaching any SIM that still points at it."""
    carrier = db.query(models.Carrier).filter(models.Carrier.id == carrier_id).first()
    if not carrier:
        raise HTTPException(status_code=404, detail="Carrier not found")
    _trash_record(db, "carrier", carrier.name, _snapshot(carrier, [
        "name", "country", "api_endpoint", "api_key", "bandwidth_mbps",
        "reliability_score", "is_active",
    ]) | {"network_ids": [link.id for link in (carrier.networks or [])]})
    db.query(Device).filter(Device.carrier_id == carrier.id).update(
        {Device.carrier_id: None}, synchronize_session=False
    )
    _audit(db, "carrier.delete", carrier.name)
    db.delete(carrier)
    db.commit()
    # 200 with a body (not 204): the dashboard's authRequest() always parses JSON.
    return {"id": carrier_id, "deleted": True, "trashed": True}


def _euicc_payload(euicc: Euicc) -> dict:
    return {
        "id": euicc.id, "eid": euicc.eid, "iccid": euicc.iccid,
        "smdpAddress": euicc.smdp_address, "matchingId": euicc.matching_id,
        "status": euicc.status, "createdAt": euicc.created_at.isoformat() if euicc.created_at else None,
    }


@app.get("/euicc")
async def list_euiccs(db: Session = Depends(get_db)):
    return [_euicc_payload(item) for item in db.query(Euicc).order_by(Euicc.created_at.desc()).all()]


@app.post("/euicc", status_code=201)
async def register_euicc(payload: dict = Body(...), db: Session = Depends(get_db)):
    eid = str(payload.get("eid") or "").strip()
    if not eid or not eid.isdigit() or len(eid) not in (32, 33):
        raise HTTPException(status_code=422, detail="EID must contain 32 or 33 digits")
    if db.query(Euicc).filter(Euicc.eid == eid).first():
        raise HTTPException(status_code=409, detail="EID already registered")
    euicc = Euicc(
        eid=eid, iccid=str(payload.get("iccid") or "").strip() or None,
        smdp_address=str(payload.get("smdpAddress") or "").strip() or None,
        matching_id=str(payload.get("matchingId") or "").strip() or None,
        status=str(payload.get("status") or "ready"),
    )
    db.add(euicc)
    db.commit()
    db.refresh(euicc)
    return _euicc_payload(euicc)


@app.get("/rsp/operations")
async def list_rsp_operations(db: Session = Depends(get_db)):
    rows = db.query(RspOperation).order_by(RspOperation.created_at.desc()).limit(100).all()
    return [{
        "id": row.id, "operation": row.operation, "eid": row.eid,
        "profileIccid": row.profile_iccid, "networkId": row.network_id,
        "status": row.status, "error": row.error_message,
        "createdAt": row.created_at.isoformat() if row.created_at else None,
        "completedAt": row.completed_at.isoformat() if row.completed_at else None,
    } for row in rows]


@app.post("/rsp/download", status_code=202)
async def rsp_download(payload: dict = Body(...), db: Session = Depends(get_db)):
    """Request a profile download through a configured provider's RSP API."""
    eid = str(payload.get("eid") or "").strip()
    network_id = payload.get("networkId")
    if not eid or network_id is None:
        raise HTTPException(status_code=422, detail="EID and networkId are required")
    euicc = db.query(Euicc).filter(Euicc.eid == eid).first()
    network = db.query(TelecomNetwork).filter(TelecomNetwork.id == network_id, TelecomNetwork.is_active.is_(True)).first()
    if not euicc:
        raise HTTPException(status_code=404, detail="eUICC is not registered")
    if not network:
        raise HTTPException(status_code=404, detail="Active telecom network not found")
    operation = RspOperation(
        id=str(uuid.uuid4()), operation="download", eid=eid,
        profile_iccid=str(payload.get("profileIccid") or "").strip() or None,
        network_id=network.id, status="queued",
    )
    db.add(operation)
    db.commit()
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    headers["Authorization" if network.auth_type == "bearer" else "X-API-Key"] = (
        f"Bearer {network.api_key}" if network.auth_type == "bearer" else network.api_key
    )
    request_payload = {"eid": eid, "profileIccid": operation.profile_iccid, "smdpAddress": euicc.smdp_address,
                       "matchingId": euicc.matching_id}
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(f"{network.api_base_url}/rsp/profiles/download",
                                    headers=headers, json=request_payload) as response:
                if response.status >= 400:
                    operation.status = "failed"
                    operation.error_message = f"Provider returned HTTP {response.status}"
                    db.commit()
                    raise HTTPException(status_code=502, detail=operation.error_message)
                operation.status = "submitted"
                operation.completed_at = datetime.utcnow()
                db.commit()
    except HTTPException:
        raise
    except (aiohttp.ClientError, TimeoutError) as error:
        operation.status = "failed"
        operation.error_message = str(error)[:500]
        db.commit()
        raise HTTPException(status_code=502, detail=f"RSP provider request failed: {error}") from error
    return {"operationId": operation.id, "status": operation.status, "eid": eid, "networkId": network.id}


def _rsp_provider_payload(provider: RspProvider) -> dict:
    return {
        "id": provider.id, "name": provider.name, "protocol": provider.protocol,
        "adapter": provider.adapter, "apiBaseUrl": provider.api_base_url,
        "healthPath": provider.health_path, "downloadPath": provider.download_path,
        "authType": provider.auth_type, "credentialStorage": provider.credential_storage,
        "credentialRef": provider.credential_ref, "documentationUrl": provider.documentation_url,
        "isActive": provider.is_active,
        "createdAt": provider.created_at.isoformat() if provider.created_at else None,
    }


@app.get("/rsp/providers")
async def list_rsp_providers(db: Session = Depends(get_db)):
    """List RSP adapter configurations; secret values are never returned."""
    return [_rsp_provider_payload(provider) for provider in db.query(RspProvider).order_by(RspProvider.name).all()]


@app.post("/rsp/providers", status_code=201)
async def create_rsp_provider(payload: dict = Body(...), db: Session = Depends(get_db)):
    name = str(payload.get("name") or "").strip()
    base_url = str(payload.get("apiBaseUrl") or "").strip().rstrip("/")
    protocol = str(payload.get("protocol") or "").lower().strip()
    storage = str(payload.get("credentialStorage") or "environment").lower().strip()
    credential_ref = str(payload.get("credentialRef") or "").strip()
    if not name or not base_url or not credential_ref:
        raise HTTPException(status_code=422, detail="Name, API base URL and credential reference are required")
    if protocol not in {"sgp22", "sgp32", "both"}:
        raise HTTPException(status_code=422, detail="Protocol must be sgp22, sgp32, or both")
    if storage not in {"environment", "vault", "database_encrypted"}:
        raise HTTPException(status_code=422, detail="Unsupported credential storage")
    if not base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=422, detail="API base URL must use http:// or https://")
    if db.query(RspProvider).filter(RspProvider.name == name).first():
        raise HTTPException(status_code=409, detail="An RSP provider with this name already exists")
    provider = RspProvider(
        name=name, protocol=protocol, adapter=str(payload.get("adapter") or "provider_http"),
        api_base_url=base_url, health_path=str(payload.get("healthPath") or "/health"),
        download_path=str(payload.get("downloadPath") or "/rsp/profiles/download"),
        auth_type=str(payload.get("authType") or "bearer").lower(),
        credential_storage=storage, credential_ref=credential_ref,
        documentation_url=str(payload.get("documentationUrl") or "").strip() or None,
        is_active=bool(payload.get("isActive", True)),
    )
    db.add(provider)
    _audit(db, "provider.create", name, protocol)
    db.commit()
    db.refresh(provider)
    return _rsp_provider_payload(provider)


@app.delete("/rsp/providers/{provider_id}")
async def delete_rsp_provider(provider_id: int, db: Session = Depends(get_db)):
    """Remove an RSP provider; the snapshot stays in Trash for restore."""
    provider = db.query(RspProvider).filter(RspProvider.id == provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="RSP provider not found")
    _trash_record(db, "provider", provider.name, _snapshot(provider, [
        "name", "protocol", "adapter", "api_base_url", "health_path",
        "download_path", "auth_type", "credential_storage", "credential_ref",
        "documentation_url", "is_active",
    ]))
    _audit(db, "provider.delete", provider.name)
    db.delete(provider)
    db.commit()
    return {"id": provider_id, "deleted": True, "trashed": True}


@app.patch("/rsp/providers/{provider_id}")
async def update_rsp_provider(provider_id: int, payload: dict = Body(...), db: Session = Depends(get_db)):
    provider = db.query(RspProvider).filter(RspProvider.id == provider_id).first()
    if not provider:
        raise HTTPException(status_code=404, detail="RSP provider not found")
    if "name" in payload:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=422, detail="name cannot be empty")
        duplicate = (
            db.query(RspProvider)
            .filter(func.lower(RspProvider.name) == name.lower(), RspProvider.id != provider_id)
            .first()
        )
        if duplicate:
            raise HTTPException(status_code=409, detail="An RSP provider with this name already exists")
    fields = {
        "name": "name", "protocol": "protocol", "adapter": "adapter",
        "healthPath": "health_path", "downloadPath": "download_path",
        "authType": "auth_type", "credentialStorage": "credential_storage",
        "credentialRef": "credential_ref", "documentationUrl": "documentation_url",
        "isActive": "is_active",
    }
    for key, field in fields.items():
        if key in payload:
            setattr(provider, field, payload[key])
    if payload.get("apiBaseUrl"):
        provider.api_base_url = str(payload["apiBaseUrl"]).strip().rstrip("/")
    _audit(db, "provider.update", provider.name, ",".join(sorted(payload.keys())))
    db.commit()
    db.refresh(provider)
    return _rsp_provider_payload(provider)


def _generate_profile_xml(carrier_name: str) -> dict:
    """
    Minimal stub that returns a GSMA‑RSP profile dict.
    In production replace with a call to the carrier’s RSP service or a library like `pyesim`.
    """
    import json  # already imported at top, but keep for clarity
    profile = {
        "id": str(uuid.uuid4()),
        "eid": "00:00:00:00:00:00:00:00:00",          # placeholder eSIM ID
        "ki": "dummy_Ki_value",                      # master key (dummy)
        "op": "dummy_op_value",                      # operator key (dummy)
        "imsi": "001010000000000" + carrier_name[-1],  # simple IMSI
        "msisdn": "+1555" + carrier_name[-1] * 7,     # dummy MSISDN
        "profile_op": "EMPTY",                       # encryption mode
        "support_gba": False,
    }
    return profile


@app.post("/esim/provision")
async def esim_provision(payload: dict | str = Body(...), db: Session = Depends(get_db)):
    """Create / retrieve a GSMA‑RSP profile for the given ICCID.

    Accepts either a JSON object ({"iccid": "..."}) or a bare JSON string, the
    two shapes clients have historically sent.
    """
    iccid = str(
        payload.get("iccid") if isinstance(payload, dict) else payload or ""
    ).strip()
    if not iccid:
        raise HTTPException(status_code=422, detail="iccid is required")
    device = db.query(Device).filter(Device.iccid == iccid).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    existing = db.query(EsimProfile).filter(EsimProfile.device_id == device.id).first()
    if existing:
        stored_profile = existing.profile_xml
        if isinstance(stored_profile, str):
            try:
                stored_profile = json.loads(stored_profile)
            except (TypeError, ValueError):
                stored_profile = {"raw": stored_profile}
        return {
            "iccid": iccid,
            "carrier_name": existing.carrier_name,
            "profile_xml": stored_profile,
            "activated_at": existing.activated_at,
            "status": existing.status,
        }

    # generate a stub profile
    profile_dict = _generate_profile_xml(device.carrier.name if device.carrier else "carrier_a")

    new_profile = EsimProfile(
        device_id=device.id,
        carrier_name=device.carrier.name if device.carrier else "carrier_a",
        profile_xml=profile_dict,
        activated_at=datetime.utcnow(),
        status="active",
    )
    db.add(new_profile)
    db.commit()
    db.refresh(new_profile)

    return {
        "iccid": iccid,
        "carrier_name": new_profile.carrier_name,
        "profile_xml": profile_dict,
        "activated_at": new_profile.activated_at,
        "status": new_profile.status,
    }


@app.get("/esim/profiles")
async def list_esim_profiles(db: Session = Depends(get_db)):
    """List all stored eSIM profiles."""
    rows = db.query(EsimProfile).all()
    out = []
    for r in rows:
        dev_iccid = db.query(Device).filter(Device.id == r.device_id).first().iccid
        out.append(
            {
                "iccid": dev_iccid,
                "carrier_name": r.carrier_name,
                "activated_at": r.activated_at,
                "deactivated_at": r.deactivated_at,
                "status": r.status,
            }
        )
    return out


@app.post("/esim/deactivate")
async def esim_deactivate(payload: dict = Body(...), db: Session = Depends(get_db)):
    """Mark a device’s eSIM profile as deactivated."""
    iccid = payload.get("iccid")
    if not iccid:
        raise HTTPException(status_code=400, detail="Missing 'iccid'")

    device = db.query(Device).filter(Device.iccid == iccid).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    profile = db.query(EsimProfile).filter(EsimProfile.device_id == device.id).first()
    if not profile:
        raise HTTPException(status_code=404, detail="No eSIM profile for this device")

    profile.status = "deactivated"
    profile.deactivated_at = datetime.utcnow()
    db.commit()
    return {"iccid": iccid, "status": profile.status, "deactivated_at": profile.deactivated_at}

# ---------------------------------------------------------------------------
# SIM studio - IoT SIM / i-SIM / eSIM provisioning, activation and lifecycle
# ---------------------------------------------------------------------------

SIM_STUDIO_DEFAULT_PLAN = "500 MB / month"
SIM_STUDIO_SMDP = "rsp.raw-switch.io"
SIM_STUDIO_MAX_BATCH = 25

# Numbering plan used for every SIM the studio issues. Keeping the ranges in one
# place means a new issuer range only needs a config change, not a code change.
SIM_STUDIO_ICCID_PREFIX = "8944100"   # 20 digit ICCID: issuer prefix + serial + Luhn
SIM_STUDIO_EID_PREFIX = "89049032"    # 32 digit eUICC EID: GSMA prefix + serial + Luhn
SIM_STUDIO_MCC = "901"                # ITU test / private mobile network MCC
SIM_STUDIO_MNC = "70"
SIM_STUDIO_MSISDN_PREFIX = "+88216"   # ITU international network (+882 16)

SIM_STUDIO_TYPES = [
    {
        "key": "iot-sim",
        "label": "IoT SIM",
        "format": "IoT SIM",
        "tagline": "Physical plug-in card for any IoT device",
        "description": "An industrial UICC card that drops into any modem, tracker or gateway.",
        "highlights": [
            "Works in 2G, 3G, 4G, LTE-M and NB-IoT modems",
            "PIN and PUK issued instantly",
            "Move the card between devices at any time",
        ],
        "delivery": "Physical card - activation details issued immediately",
        "icon": "credit-card",
        "profileState": "ready",
        "options": ["formFactor"],
    },
    {
        "key": "i-sim",
        "label": "i-SIM",
        "format": "iSIM",
        "tagline": "SIM soldered inside the device chipset",
        "description": "An integrated iUICC that ships inside the device SoC - nothing to insert.",
        "highlights": [
            "No card slot, no handling, no theft risk",
            "Profile bound to the device pairing token",
            "Ideal for sealed sensors and long-life trackers",
        ],
        "delivery": "Embedded - pair the device with its activation token",
        "icon": "cpu",
        "profileState": "paired",
        "options": ["eid"],
    },
    {
        "key": "e-sim",
        "label": "eSIM",
        "format": "eSIM (SGP.32)",
        "tagline": "Downloadable profile with a scan-to-install QR code",
        "description": "A remote SIM profile delivered over GSMA RSP - install it by scanning one QR code.",
        "highlights": [
            "Scan the QR code to install the profile",
            "SGP.22 consumer and SGP.32 IoT download",
            "Re-issue the activation code whenever you need to",
        ],
        "delivery": "Digital - scan the QR code or type the activation code",
        "icon": "smartphone",
        "profileState": "released",
        "options": ["protocol", "eid"],
    },
]

SIM_STUDIO_MAP = {entry["key"]: entry for entry in SIM_STUDIO_TYPES}

SIM_STUDIO_ALIASES = {
    "iot": "iot-sim", "iot sim": "iot-sim", "iot-sim": "iot-sim", "iotsim": "iot-sim",
    "physical": "iot-sim", "uicc": "iot-sim", "compatible iot sim": "iot-sim",
    "i-sim": "i-sim", "i sim": "i-sim", "isim": "i-sim", "i-sim card": "i-sim",
    "embedded": "i-sim", "softsim": "i-sim",
    "e-sim": "e-sim", "e sim": "e-sim", "esim": "e-sim", "e-sim card": "e-sim",
    "digital": "e-sim", "esim (sgp.22)": "e-sim", "esim (sgp.32)": "e-sim",
}

SIM_STUDIO_APN = {
    "iot-sim": "iot.raw-switch.io",
    "i-sim": "isim.raw-switch.io",
    "e-sim": "esim.raw-switch.io",
}

SIM_LIFECYCLE_ACTIONS = {
    "activate": {"allowed": {"provisioned", "inactive", "suspended", "failed"}, "result": "active"},
    "suspend": {"allowed": {"active"}, "result": "suspended"},
    "resume": {"allowed": {"suspended"}, "result": "active"},
    "terminate": {"allowed": {"provisioned", "inactive", "active", "suspended", "failed"}, "result": "terminated"},
}


def _studio_entry(raw_value) -> dict:
    """Resolve any SIM type spelling used by the dashboard to a studio catalogue entry."""
    key = SIM_STUDIO_ALIASES.get(str(raw_value or "").strip().lower())
    if not key:
        raise HTTPException(status_code=422, detail="simType must be one of iot-sim, i-sim or e-sim")
    return SIM_STUDIO_MAP[key]


def _luhn_check_digit(base: str) -> str:
    """Return the Luhn check digit that validates an ICCID-style number."""
    total = 0
    for index, character in enumerate(reversed(base)):
        digit = int(character)
        if index % 2 == 0:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return str((10 - total % 10) % 10)


def _random_digits(length: int) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(length))


def _allocate_iccid(db: Session) -> str:
    """Generate a unique 20 digit ICCID (8944100 issuer prefix + Luhn check digit)."""
    for _ in range(12):
        base = (SIM_STUDIO_ICCID_PREFIX + _random_digits(19 - len(SIM_STUDIO_ICCID_PREFIX)))[:19]
        iccid = base + _luhn_check_digit(base)
        if not db.query(Device).filter(Device.iccid == iccid).first():
            return iccid
    raise HTTPException(status_code=503, detail="Unable to allocate a free ICCID, please retry")


def _allocate_eid() -> str:
    """Generate a 32 digit GSMA eUICC EID: 89 + 29 random digits + Luhn check digit."""
    base = SIM_STUDIO_EID_PREFIX + _random_digits(31 - len(SIM_STUDIO_EID_PREFIX))
    return base + _luhn_check_digit(base)


def _sim_identifiers(entry: dict, eid_hint: str = "", form_factor: str = "") -> dict:
    """Build the identity, activation and credential kit for one SIM of the requested type."""
    key = entry["key"]
    kit = {
        "imsi": SIM_STUDIO_MCC + SIM_STUDIO_MNC + _random_digits(15 - len(SIM_STUDIO_MCC + SIM_STUDIO_MNC)),
        "msisdn": SIM_STUDIO_MSISDN_PREFIX + _random_digits(8),
        "apn": SIM_STUDIO_APN[key],
        "eid": None,
        "smdp_address": None,
        "matching_id": None,
        "activation_code": None,
        "activation_token": None,
        "form_factor": None,
        "pin1": None,
        "puk1": None,
        "profile_state": entry["profileState"],
    }
    if key == "iot-sim":
        kit["form_factor"] = form_factor or "4FF (nano)"
        kit["pin1"] = _random_digits(4)
        kit["puk1"] = _random_digits(8)
    elif key == "i-sim":
        kit["eid"] = (eid_hint or _allocate_eid()).replace(":", "")
        # An iSIM is soldered into the device, so it always ships in an
        # embedded package. It used to be stored as NULL.
        kit["form_factor"] = form_factor or "MFF2 (solderable)"
        kit["activation_token"] = secrets.token_hex(16).upper()
    else:
        kit["eid"] = (eid_hint or _allocate_eid()).replace(":", "")
        kit["smdp_address"] = SIM_STUDIO_SMDP
        kit["matching_id"] = secrets.token_hex(16).upper()
        kit["activation_code"] = f"LPA:1${kit['smdp_address']}${kit['matching_id']}"
    return kit


def _sim_detail_payload(device: Device, plans: dict | None = None) -> dict:
    """Inventory payload plus every activation detail needed to install the SIM."""
    payload = _device_payload(device)
    sim_type = str(device.sim_type or "").lower()
    plan = (plans or {}).get(device.data_plan)
    allowance = float(device.data_limit_mb or 0)
    used = float(device.usage_mb or 0)
    payload.update({
        "simTypeKey": "e-sim" if sim_type.startswith("esim")
        else "i-sim" if sim_type in {"isim", "softsim"} else "iot-sim",
        "imsi": device.imsi,
        "msisdn": device.msisdn,
        "apn": device.apn,
        "formFactor": device.form_factor,
        "pin1": device.pin1,
        "puk1": device.puk1,
        "eid": device.eid,
        "smdpAddress": device.smdp_address,
        "matchingId": device.matching_id,
        "activationCode": device.activation_code,
        "activationToken": device.activation_token,
        "profileState": device.profile_state,
        "provisionedBy": device.provisioned_by,
        "activatedAt": device.activated_at.isoformat() if device.activated_at else None,
        "terminatedAt": device.terminated_at.isoformat() if device.terminated_at else None,
        "qrUrl": f"/sims/{device.iccid}/qr.svg" if device.activation_code else None,
        "isEsim": sim_type.startswith("esim"),
        "isSim": sim_type in {"isim", "softsim"},
        "planPriceUsd": plan["priceUsd"] if plan else None,
        "planOveragePerGb": plan["overagePerGb"] if plan else None,
        "planDataMb": plan["dataMb"] if plan else None,
        "planDescription": plan["description"] if plan else None,
        "usagePercent": round(used / allowance * 100, 1) if allowance else None,
        "overLimit": bool(allowance and used > allowance),
    })
    return payload



@app.get("/sims/catalog")
async def sim_catalog(db: Session = Depends(get_db)):
    """Describe the three cards the studio can provision, plus the live catalogue."""
    plans = db.query(Plan).filter(Plan.is_active.is_(True)).order_by(Plan.data_mb).all()
    customers = db.query(Customer).order_by(Customer.name).all()
    return {
        "types": SIM_STUDIO_TYPES,
        "plans": [_plan_payload(plan) for plan in plans],
        "customers": [
            {
                "name": customer.name,
                "contactName": customer.contact_name,
                "contactEmail": customer.contact_email,
                "status": customer.status,
            }
            for customer in customers
        ],
        "networkTechs": ["2G", "3G", "4G", "LTE-M", "NB-IoT"],
        "regions": ["Europe", "North America", "Asia Pacific", "Africa", "Global"],
        "formFactors": ["2FF (mini)", "3FF (micro)", "4FF (nano)", "MFF2 (solderable)"],
        "protocols": ["SGP.32", "SGP.22"],
        "maxBatch": SIM_STUDIO_MAX_BATCH,
        "smdpAddress": SIM_STUDIO_SMDP,
    }


@app.get("/sims/summary")
async def sim_summary(db: Session = Depends(get_db)):
    """Counts used by the SIM studio header: one tile per card family plus lifecycle states."""
    device_query = db.query(Device)
    if is_customer():
        device_query = device_query.filter(func.lower(Device.customer) == customer_scope().lower())
    devices = device_query.all()

    def family(device: Device) -> str:
        sim_type = str(device.sim_type or "").lower()
        return "e-sim" if sim_type.startswith("esim") else "i-sim" if sim_type in {"isim", "softsim"} else "iot-sim"

    def state(device: Device) -> str:
        return str(device.status or "").lower()

    billed = [
        device for device in devices
        if float(device.data_limit_mb or 0) > 0 and str(device.status or "").lower() != "terminated"
    ]
    plans = _plan_map(db)
    monthly_usd = sum(float((plans.get(device.data_plan) or {}).get("priceUsd") or 0) for device in billed)

    return {
        "total": len(devices),
        "families": {key: sum(1 for device in devices if family(device) == key) for key in SIM_STUDIO_MAP},
        "states": {
            "active": sum(1 for device in devices if state(device) == "active"),
            "suspended": sum(1 for device in devices if state(device) == "suspended"),
            "terminated": sum(1 for device in devices if state(device) == "terminated"),
        },
        "usageMb": round(sum(float(device.usage_mb or 0) for device in devices), 1),
        "dataLimitMb": round(sum(float(device.data_limit_mb or 0) for device in devices), 1),
        "overLimit": sum(
            1 for device in devices
            if float(device.data_limit_mb or 0) and float(device.usage_mb or 0) > float(device.data_limit_mb or 0)
        ),
        "customers": db.query(Customer).count(),
        "plans": db.query(Plan).count(),
        "monthlyRecurringUsd": round(monthly_usd, 2),
    }


@app.get("/sims/recent")
async def sim_recent(limit: int = 8, db: Session = Depends(get_db)):
    """Return the most recently provisioned SIMs with their activation details."""
    capped = max(1, min(int(limit or 8), 50))
    device_query = db.query(Device)
    if is_customer():
        device_query = device_query.filter(func.lower(Device.customer) == customer_scope().lower())
    devices = device_query.order_by(Device.created_at.desc()).limit(capped).all()
    plans = _plan_map(db)
    return [_sim_detail_payload(device, plans) for device in devices]



@app.post("/sims/provision", status_code=201)
async def sim_provision(payload: dict = Body(...), db: Session = Depends(get_db)):
    """Provision one or more IoT SIM, i-SIM or eSIM cards and return activation details."""
    entry = _studio_entry(payload.get("simType") or payload.get("type"))
    quantity = payload.get("quantity", 1)
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= SIM_STUDIO_MAX_BATCH:
        raise HTTPException(
            status_code=422,
            detail=f"quantity must be a whole number between 1 and {SIM_STUDIO_MAX_BATCH}",
        )

    device_name = str(payload.get("device") or "").strip() or "Unnamed device"
    customer = str(payload.get("customer") or "").strip() or "Unassigned"
    region = str(payload.get("region") or "Global").strip()
    network_tech = str(payload.get("networkTech") or "4G").strip()
    plan = str(payload.get("dataPlan") or payload.get("plan") or SIM_STUDIO_DEFAULT_PLAN).strip()
    form_factor = str(payload.get("formFactor") or "").strip()
    eid_hint = str(payload.get("eid") or "").strip().replace(" ", "")
    msisdn_hint = str(payload.get("msisdn") or "").strip()
    requested_iccid = str(payload.get("iccid") or "").strip()
    supplied_protocol = str(payload.get("protocol") or "").strip().upper()

    if len(device_name) > 80:
        raise HTTPException(status_code=422, detail="Device name must be 80 characters or fewer")
    if msisdn_hint and not re.fullmatch(r"\+?[0-9]{6,15}", msisdn_hint):
        raise HTTPException(status_code=422, detail="MSISDN must be 6 to 15 digits, optionally starting with +")
    if eid_hint and not re.fullmatch(r"[0-9]{28,32}", eid_hint):
        raise HTTPException(status_code=422, detail="eUICC EID must be 28 to 32 digits")
    if supplied_protocol and supplied_protocol not in {"SGP.22", "SGP.32"}:
        raise HTTPException(status_code=422, detail="protocol must be SGP.22 or SGP.32")
    if requested_iccid:
        if quantity > 1:
            raise HTTPException(status_code=422, detail="A manual ICCID can only be used when quantity is 1")
        if not requested_iccid.isdigit() or not 15 <= len(requested_iccid) <= 22:
            raise HTTPException(status_code=422, detail="ICCID must contain 15 to 22 digits")
        if db.query(Device).filter(Device.iccid == requested_iccid).first():
            raise HTTPException(status_code=409, detail="ICCID already exists")

    # The studio may only bill SIMs to a registered customer on a catalogued plan.
    plan_row = _plan_row(db, plan)
    if not plan_row or not plan_row.is_active:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown data plan '{plan}'. Choose one of: {', '.join(_plan_names(db))}",
        )
    customer_row = db.query(Customer).filter(func.lower(Customer.name) == customer.lower()).first()
    if customer != "Unassigned" and not customer_row:
        registered = [row.name for row in db.query(Customer).order_by(Customer.name).all()]
        raise HTTPException(
            status_code=422,
            detail=f"Unknown customer '{customer}'. Register the customer first. Known customers: {', '.join(registered)}",
        )
    if customer_row and str(customer_row.status or "").lower() == "suspended":
        raise HTTPException(status_code=409, detail=f"Customer '{customer_row.name}' is suspended and cannot be issued new SIMs")
    if customer_row:
        customer = customer_row.name

    sim_format = entry["format"]
    if entry["key"] == "e-sim" and supplied_protocol:
        sim_format = f"eSIM ({supplied_protocol})"

    created = []
    for index in range(quantity):
        iccid = requested_iccid if requested_iccid else _allocate_iccid(db)
        kit = _sim_identifiers(entry, eid_hint=eid_hint, form_factor=form_factor)
        device = Device(
            iccid=iccid,
            device_name=device_name if quantity == 1 else f"{device_name} {index + 1:02d}",
            sim_type=sim_format,
            network_tech=network_tech,
            customer=customer,
            region=region,
            data_plan=plan,
            data_limit_mb=float(plan_row.data_mb or 0),
            roaming_enabled=_effective_roaming_enabled(db, bool(payload.get("roamingEnabled", True))),
            status="active",
            imsi=kit["imsi"],
            msisdn=msisdn_hint.split("+")[-1] if msisdn_hint else kit["msisdn"],
            apn=kit["apn"],
            form_factor=kit["form_factor"],
            pin1=kit["pin1"],
            puk1=kit["puk1"],
            eid=kit["eid"],
            smdp_address=kit["smdp_address"],
            matching_id=kit["matching_id"],
            activation_code=kit["activation_code"],
            activation_token=kit["activation_token"],
            profile_state=kit["profile_state"],
            provisioned_by=str(payload.get("provisionedBy") or "dashboard")[:80],
            activated_at=datetime.utcnow(),
        )
        db.add(device)
        db.flush()
        created.append(device)

    _audit(db, "sim.provision", device_name, f"{entry['key']} x{quantity} customer={customer}")
    try:
        db.commit()
    except SQLAlchemyError as error:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"SIM could not be saved: {error}") from error

    for device in created:
        db.refresh(device)
    return {
        "batchId": uuid.uuid4().hex[:8].upper(),
        "simType": entry["key"],
        "label": entry["label"],
        "format": sim_format,
        "created": len(created),
        "sims": [_sim_detail_payload(device, plans=_plan_map(db)) for device in created],
    }



@app.get("/sims/{iccid}")
async def sim_detail(iccid: str, db: Session = Depends(get_db)):
    """Return one SIM with every identifier, credential and activation field."""
    device = db.query(Device).filter(Device.iccid == iccid).first()
    if not device:
        raise HTTPException(status_code=404, detail="SIM not found")
    if is_customer() and str(device.customer or "").lower() != customer_scope().lower():
        raise HTTPException(status_code=404, detail="SIM not found")
    return _sim_detail_payload(device, plans=_plan_map(db))


@app.post("/sims/{iccid}/lifecycle")
async def sim_lifecycle(iccid: str, payload: dict = Body(...), db: Session = Depends(get_db)):
    """Move a SIM through its lifecycle: activate, suspend, resume or terminate."""
    action = str(payload.get("action") or "").strip().lower()
    rule = SIM_LIFECYCLE_ACTIONS.get(action)
    if not rule:
        raise HTTPException(status_code=422, detail="action must be activate, suspend, resume or terminate")
    device = db.query(Device).filter(Device.iccid == iccid).first()
    if not device:
        raise HTTPException(status_code=404, detail="SIM not found")
    current = str(device.status or "").lower()
    if current not in rule["allowed"]:
        raise HTTPException(status_code=409, detail=f"Cannot {action} a SIM that is currently {current}")
    device.status = rule["result"]
    _audit(db, f"sim.{action}", iccid, f"-> {rule['result']}")
    now = datetime.utcnow()
    if rule["result"] == "active":
        device.activated_at = device.activated_at or now
        device.terminated_at = None
        device.profile_state = "released" if device.activation_code else device.profile_state
    if rule["result"] == "suspended":
        device.profile_state = "suspended"
    if rule["result"] == "terminated":
        device.terminated_at = now
        device.profile_state = "revoked"
        device.activation_code = None
    db.commit()
    db.refresh(device)
    return {
        "iccid": iccid,
        "action": action,
        "status": device.status,
        "profileState": device.profile_state,
        "sim": _sim_detail_payload(device, plans=_plan_map(db)),
    }


@app.post("/sims/{iccid}/usage")
async def sim_record_usage(iccid: str, payload: dict = Body(...), db: Session = Depends(get_db)):
    """Meter real data usage (MB) against the plan allowance and report overage."""
    device = db.query(Device).filter(Device.iccid == iccid).first()
    if not device:
        raise HTTPException(status_code=404, detail="SIM not found")
    if str(device.status or "").lower() == "terminated":
        raise HTTPException(status_code=409, detail="A terminated SIM cannot report usage")
    raw = payload.get("megabytes", payload.get("mb"))
    try:
        megabytes = float(raw)
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail="megabytes must be a number") from error
    if megabytes < 0:
        raise HTTPException(status_code=422, detail="megabytes cannot be negative")
    device.usage_mb = round(float(device.usage_mb or 0) + megabytes, 1)
    db.commit()
    db.refresh(device)
    allowance = float(device.data_limit_mb or 0)
    used = float(device.usage_mb or 0)
    plan = _plan_map(db).get(device.data_plan) or {}
    overage_mb = max(0.0, used - allowance) if allowance else 0.0
    return {
        "iccid": iccid,
        "usageMb": used,
        "dataLimitMb": allowance,
        "overLimit": bool(allowance and used > allowance),
        "overageMb": round(overage_mb, 1),
        "overageChargeUsd": round(overage_mb / 1024 * float(plan.get("overagePerGb") or 0), 2),
        "plan": plan or None,
        "sim": _sim_detail_payload(device, plans=_plan_map(db)),
    }


@app.post("/sims/{iccid}/activation")
async def sim_reissue_activation(iccid: str, db: Session = Depends(get_db)):
    """Re-issue an eSIM activation code, or rotate an iSIM pairing token."""
    device = db.query(Device).filter(Device.iccid == iccid).first()
    if not device:
        raise HTTPException(status_code=404, detail="SIM not found")
    sim_type = str(device.sim_type or "").lower()
    if str(device.status or "").lower() == "terminated":
        raise HTTPException(status_code=409, detail="A terminated SIM cannot be activated again")
    if sim_type.startswith("esim"):
        device.matching_id = secrets.token_hex(16).upper()
        device.smdp_address = device.smdp_address or SIM_STUDIO_SMDP
        device.activation_code = f"LPA:1${device.smdp_address}${device.matching_id}"
        device.profile_state = "released"
    elif sim_type in {"isim", "softsim"}:
        device.activation_token = secrets.token_hex(16).upper()
        device.profile_state = "paired"
    else:
        device.pin1 = device.pin1 or _random_digits(4)
        device.puk1 = device.puk1 or _random_digits(8)
    db.commit()
    db.refresh(device)
    return _sim_detail_payload(device, plans=_plan_map(db))


@app.get("/sims/{iccid}/qr.svg")
async def sim_activation_qr(iccid: str, db: Session = Depends(get_db)):
    """Render the GSMA activation code as a scannable QR code (SVG)."""
    device = db.query(Device).filter(Device.iccid == iccid).first()
    if not device:
        raise HTTPException(status_code=404, detail="SIM not found")
    if is_customer() and str(device.customer or "").lower() != customer_scope().lower():
        raise HTTPException(status_code=404, detail="SIM not found")
    if not device.activation_code:
        raise HTTPException(status_code=404, detail="This SIM has no eSIM activation code")
    try:
        import io

        import qrcode
        import qrcode.image.svg
    except ImportError as error:
        raise HTTPException(status_code=503, detail="QR renderer is not installed on this server") from error
    image = qrcode.make(
        device.activation_code,
        image_factory=qrcode.image.svg.SvgPathImage,
        box_size=12,
        border=2,
    )
    buffer = io.BytesIO()
    image.save(buffer)
    return Response(
        content=buffer.getvalue(),
        media_type="image/svg+xml",
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# Dashboard hosting
# ---------------------------------------------------------------------------
# The RAW-Switch dashboard is a static bundle. Serving it from the API process
# means one URL to open (http://127.0.0.1:8000/) instead of a dashboard on one
# port and an API on another, and it keeps the browser on a single origin.
# Every API route above is registered first, so this mount only handles the URLs
# the API does not claim. Set UI_DIR to point at a different bundle, or to an
# empty string to switch the mount off.

def _resolve_ui_dir() -> str:
    """Return the dashboard folder to host, or '' when there is nothing to host."""
    override = os.getenv("UI_DIR")
    if override is not None:
        candidate = Path(override.strip()).expanduser() if override.strip() else None
        return str(candidate) if candidate and (candidate / "index.html").is_file() else ""
    for candidate in (
        Path(__file__).resolve().parent.parent / "nexora-iot",  # workspace layout
        Path("/var/www/raw-switch-iot"),                        # VPS layout
    ):
        if (candidate / "index.html").is_file():
            return str(candidate)
    return ""


UI_DIR = _resolve_ui_dir()
if UI_DIR:
    app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="dashboard")

