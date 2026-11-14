from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, ForeignKey, JSON, Table, Text
from sqlalchemy.sql import func
from sqlalchemy.orm import relationship

from .config import Base

# A carrier may be reachable through several provider APIs: a national one plus
# any number of international/roaming providers. The legacy
# carriers.telecom_network_id column is kept in sync with the first link.
carrier_networks = Table(
    "carrier_networks",
    Base.metadata,
    Column("carrier_id", Integer, ForeignKey("carriers.id", ondelete="CASCADE"), primary_key=True),
    Column("network_id", Integer, ForeignKey("telecom_networks.id", ondelete="CASCADE"), primary_key=True),
)


class Carrier(Base):
    __tablename__ = "carriers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    country = Column(String, nullable=False, default="Unknown")  # Country this carrier operates in
    api_endpoint = Column(String, nullable=False)
    api_key = Column(String, nullable=False)
    bandwidth_mbps = Column(Float, nullable=False, default=0)
    reliability_score = Column(Float, nullable=False, default=1.0)
    is_active = Column(Boolean, default=True)
    telecom_network_id = Column(Integer, ForeignKey("telecom_networks.id"), nullable=True)

    devices = relationship("Device", back_populates="carrier")
    networks = relationship("TelecomNetwork", secondary=carrier_networks, lazy="selectin")


class Device(Base):
    __tablename__ = "devices"

    id = Column(Integer, primary_key=True, index=True)
    iccid = Column(String, unique=True, index=True, nullable=False)  # SIM ICCID
    device_name = Column(String, nullable=False, default="Unnamed device")
    sim_type = Column(String, nullable=False, default="IoT SIM")
    network_tech = Column(String, nullable=False, default="4G")
    customer = Column(String, nullable=False, default="Unassigned")
    region = Column(String, nullable=False, default="Global")
    data_plan = Column(String, nullable=False, default="500 MB / month")
    usage_mb = Column(Float, nullable=False, default=0)
    data_limit_mb = Column(Float, nullable=False, default=500)
    roaming_enabled = Column(Boolean, nullable=False, default=True)
    carrier_id = Column(Integer, ForeignKey("carriers.id"), nullable=True)
    telecom_network_id = Column(Integer, ForeignKey("telecom_networks.id"), nullable=True)
    status = Column(String, default="provisioned")  # provisioned, active, suspended, terminated, failed, roaming
    last_switch_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # SIM studio identity, activation and lifecycle metadata (IoT SIM / iSIM / eSIM)
    imsi = Column(String, nullable=True)
    msisdn = Column(String, nullable=True)
    apn = Column(String, nullable=True)
    form_factor = Column(String, nullable=True)  # physical IoT SIM: 2FF / 3FF / 4FF / MFF2
    pin1 = Column(String, nullable=True)
    puk1 = Column(String, nullable=True)
    eid = Column(String, nullable=True)  # eUICC / iUICC identifier
    smdp_address = Column(String, nullable=True)  # SM-DP+ FQDN for eSIM downloads
    matching_id = Column(String, nullable=True)  # GSMA matchingID of the profile
    activation_code = Column(String, nullable=True)  # LPA:1$<smdp>$<matchingID>
    activation_token = Column(String, nullable=True)  # iSIM device pairing token
    profile_state = Column(String, nullable=True)  # ready / paired / released / installed / revoked
    provisioned_by = Column(String, nullable=True)
    activated_at = Column(DateTime(timezone=True), nullable=True)
    terminated_at = Column(DateTime(timezone=True), nullable=True)

    carrier = relationship("Carrier", back_populates="devices")
    telecom_network = relationship("TelecomNetwork")
    network_events = relationship("NetworkEvent", back_populates="device")
    esim_profiles = relationship("EsimProfile", back_populates="device")


class NetworkEvent(Base):
    __tablename__ = "network_events"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=False)
    timestamp = Column(DateTime(timezone=True), server_default=func.now())
    network_name = Column(String, nullable=False)
    bandwidth_mbps = Column(Float, nullable=False)
    latency_ms = Column(Float, nullable=False)
    rssi = Column(Integer, nullable=True)  # signal strength
    status = Column(String, nullable=False)  # connected, disconnected, failed

    device = relationship("Device", back_populates="network_events")


class EsimProfile(Base):
    __tablename__ = "esim_profiles"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=False)
    carrier_name = Column(String, nullable=False)
    profile_xml = Column(JSON, nullable=False)
    activated_at = Column(DateTime(timezone=True), nullable=True)
    deactivated_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(String, default="provisioned")  # provisioned, active, deactivated, failed

    device = relationship("Device", back_populates="esim_profiles")


class TelecomNetwork(Base):
    __tablename__ = "telecom_networks"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    country = Column(String, nullable=False, default="Unknown")
    api_base_url = Column(String, nullable=False)
    api_key = Column(String, nullable=False)
    auth_type = Column(String, nullable=False, default="bearer")
    is_active = Column(Boolean, nullable=False, default=True)
    # "local" (one national operator) or "international" (roaming / multi-country
    # aggregator). International providers describe their footprint in coverage.
    scope = Column(String, nullable=False, default="local")
    coverage = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Euicc(Base):
    __tablename__ = "euiccs"

    id = Column(Integer, primary_key=True, index=True)
    eid = Column(String, unique=True, index=True, nullable=False)
    iccid = Column(String, nullable=True, index=True)
    smdp_address = Column(String, nullable=True)
    matching_id = Column(String, nullable=True)
    status = Column(String, nullable=False, default="ready")
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class RspOperation(Base):
    __tablename__ = "rsp_operations"

    id = Column(String, primary_key=True, index=True)
    operation = Column(String, nullable=False)
    eid = Column(String, nullable=False, index=True)
    profile_iccid = Column(String, nullable=True)
    network_id = Column(Integer, ForeignKey("telecom_networks.id"), nullable=False)
    status = Column(String, nullable=False, default="queued")
    error_message = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)

    network = relationship("TelecomNetwork")


class RspProvider(Base):
    __tablename__ = "rsp_providers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    protocol = Column(String, nullable=False, default="sgp32")
    adapter = Column(String, nullable=False, default="provider_http")
    api_base_url = Column(String, nullable=False)
    health_path = Column(String, nullable=False, default="/health")
    download_path = Column(String, nullable=False, default="/rsp/profiles/download")
    auth_type = Column(String, nullable=False, default="bearer")
    credential_storage = Column(String, nullable=False, default="environment")
    credential_ref = Column(String, nullable=False)
    documentation_url = Column(String, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class RoamingPolicy(Base):
    __tablename__ = "roaming_policies"

    id = Column(Integer, primary_key=True, index=True)
    policy_key = Column(String, unique=True, index=True, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class Customer(Base):
    """Customer account that really owns SIMs.

    The SIM studio used to offer free-text customer names (and pulled whatever
    string was typed into a portal profile). The studio now reads its customer
    dropdown from this registry so every option is a real, auditable account.
    """

    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    contact_name = Column(String, nullable=True)
    contact_email = Column(String, nullable=True, index=True)
    phone = Column(String, nullable=True)
    status = Column(String, nullable=False, default="Active")
    notes = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class Plan(Base):
    """Sellable data plan: hard allowance, monthly price and overage rate."""

    __tablename__ = "plans"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    data_mb = Column(Float, nullable=False, default=500)
    price_usd = Column(Float, nullable=False, default=0)
    overage_per_gb = Column(Float, nullable=False, default=0)
    description = Column(String, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class DataRate(Base):
    """Pay-per-data rate card.

    Billing on this platform is metered: every megabyte a SIM moves is priced
    against a rate card row. A row may be bound to a sellable plan name, or left
    unbound (``plan_name`` NULL) to act as the default rate for any SIM whose
    plan has no dedicated tariff.
    """

    __tablename__ = "data_rates"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, index=True, nullable=False)
    currency = Column(String, nullable=False, default="USD")
    price_per_mb = Column(Float, nullable=False, default=0.0025)
    block_mb = Column(Float, nullable=False, default=0.01)  # metering step, charge rounds up
    min_charge = Column(Float, nullable=False, default=0.01)  # floor for any non-zero session
    plan_name = Column(String, nullable=True, index=True)  # optional binding to a sellable plan
    description = Column(String, nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class UsageRecord(Base):
    """One metered data event, priced at the moment it is recorded."""

    __tablename__ = "usage_records"

    id = Column(Integer, primary_key=True, index=True)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=True)
    iccid = Column(String, nullable=False, index=True)
    customer = Column(String, nullable=False, default="Unassigned", index=True)
    device_name = Column(String, nullable=True)
    network = Column(String, nullable=True)
    megabytes = Column(Float, nullable=False, default=0)
    billable_mb = Column(Float, nullable=False, default=0)
    rate_id = Column(Integer, ForeignKey("data_rates.id"), nullable=True)
    rate_name = Column(String, nullable=True)
    price_per_mb = Column(Float, nullable=False, default=0)
    amount = Column(Float, nullable=False, default=0)
    currency = Column(String, nullable=False, default="USD")
    source = Column(String, nullable=False, default="api")  # api, sim, cdr, simulated
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=True, index=True)
    recorded_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class Invoice(Base):
    """Pay-per-data invoice for one customer and one billing period."""

    __tablename__ = "invoices"

    id = Column(Integer, primary_key=True, index=True)
    number = Column(String, unique=True, index=True, nullable=False)
    customer = Column(String, nullable=False, index=True)
    period_start = Column(String, nullable=True)
    period_end = Column(String, nullable=True)
    usage_mb = Column(Float, nullable=False, default=0)
    subtotal = Column(Float, nullable=False, default=0)
    # Optional commercial charges applied at invoicing time.
    platform_fee_pct = Column(Float, nullable=False, default=0)
    platform_fee = Column(Float, nullable=False, default=0)
    tax_pct = Column(Float, nullable=False, default=0)
    tax = Column(Float, nullable=False, default=0)
    total = Column(Float, nullable=False, default=0)
    paid_total = Column(Float, nullable=False, default=0)
    currency = Column(String, nullable=False, default="USD")
    status = Column(String, nullable=False, default="issued")  # issued, part_paid, paid, void
    notes = Column(String, nullable=True)
    issued_at = Column(DateTime(timezone=True), server_default=func.now())
    due_at = Column(DateTime(timezone=True), nullable=True)
    paid_at = Column(DateTime(timezone=True), nullable=True)


class InvoiceLine(Base):
    """One priced rate bucket inside an invoice."""

    __tablename__ = "invoice_lines"

    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False, index=True)
    description = Column(String, nullable=False)
    rate_name = Column(String, nullable=True)
    quantity_mb = Column(Float, nullable=False, default=0)
    price_per_mb = Column(Float, nullable=False, default=0)
    amount = Column(Float, nullable=False, default=0)


class Payment(Base):
    """Pesapal (or simulated) payment attempt against an invoice or a top-up."""

    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    reference = Column(String, unique=True, index=True, nullable=False)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=True, index=True)
    customer = Column(String, nullable=False, default="Unassigned", index=True)
    purpose = Column(String, nullable=False, default="invoice")  # invoice, top_up
    amount = Column(Float, nullable=False, default=0)  # invoice currency (USD)
    amount_local = Column(Float, nullable=False, default=0)  # amount actually charged
    currency = Column(String, nullable=False, default="USD")
    provider = Column(String, nullable=False, default="pesapal")
    mode = Column(String, nullable=False, default="simulated")  # simulated, sandbox, live
    status = Column(String, nullable=False, default="pending")  # pending, completed, failed, cancelled
    payer_name = Column(String, nullable=True)
    payer_email = Column(String, nullable=True)
    payer_phone = Column(String, nullable=True)
    description = Column(String, nullable=True)
    order_tracking_id = Column(String, nullable=True, index=True)
    merchant_reference = Column(String, nullable=True)
    redirect_url = Column(String, nullable=True)
    confirmation_code = Column(String, nullable=True)
    payment_method = Column(String, nullable=True)
    status_message = Column(String, nullable=True)
    raw = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)


class PaymentGateway(Base):
    """Pesapal merchant configuration. One row per provider."""

    __tablename__ = "payment_gateways"

    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String, unique=True, index=True, nullable=False, default="pesapal")
    environment = Column(String, nullable=False, default="simulated")  # simulated, sandbox, live
    consumer_key = Column(String, nullable=True)
    consumer_secret = Column(String, nullable=True)
    ipn_id = Column(String, nullable=True)
    ipn_url = Column(String, nullable=True)
    callback_url = Column(String, nullable=True)
    currency = Column(String, nullable=False, default="USD")
    fx_rate = Column(Float, nullable=False, default=1.0)  # gateway currency per 1 USD
    is_active = Column(Boolean, nullable=False, default=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    role = Column(String, nullable=False, default="User")
    account_role = Column(String, nullable=True)
    permissions = Column(JSON, nullable=False, default=list)
    status = Column(String, nullable=False, default="Active")
    customer_name = Column(String, nullable=True, index=True)
    package_gb = Column(Float, nullable=False, default=0)
    used_gb = Column(Float, nullable=False, default=0)
    account_balance = Column(Float, nullable=False, default=248.5)
    last_login = Column(String, nullable=True)
    totp_secret = Column(String, nullable=True)
    totp_enabled = Column(Boolean, nullable=False, default=False)

class AuditEvent(Base):
    """One line in the admin activity log."""

    __tablename__ = "audit_events"

    id = Column(Integer, primary_key=True, index=True)
    actor = Column(String, nullable=False, default="system", index=True)
    action = Column(String, nullable=False, index=True)
    target = Column(String, nullable=True)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class DeletedItem(Base):
    """A snapshot of something removed from the console, kept for restore."""

    __tablename__ = "deleted_items"

    id = Column(Integer, primary_key=True, index=True)
    kind = Column(String, nullable=False, index=True)       # carrier, network, provider, user
    name = Column(String, nullable=False)
    payload = Column(Text, nullable=False)                   # JSON snapshot
    deleted_by = Column(String, nullable=True)
    deleted_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class EmailSettings(Base):
    """SMTP configuration for alert emails (single row)."""

    __tablename__ = "email_settings"

    id = Column(Integer, primary_key=True, index=True)
    enabled = Column(Boolean, nullable=False, default=False)
    mode = Column(String, nullable=False, default="simulated")   # simulated | smtp
    host = Column(String, nullable=True)
    port = Column(Integer, nullable=False, default=587)
    username = Column(String, nullable=True)
    password = Column(String, nullable=True)
    from_address = Column(String, nullable=False, default="alerts@raw-switch.io")
    use_tls = Column(Boolean, nullable=False, default=True)
    admin_recipients = Column(String, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class EmailMessage(Base):
    """Outbox: every alert email the system produced (sent or simulated)."""

    __tablename__ = "email_messages"

    id = Column(Integer, primary_key=True, index=True)
    to_address = Column(String, nullable=False)
    subject = Column(String, nullable=False)
    body = Column(Text, nullable=False)
    status = Column(String, nullable=False, default="simulated")  # sent | simulated | failed
    error = Column(Text, nullable=True)
    alert_key = Column(String, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class AuthSession(Base):
    """Bearer session issued at login; only the hash of the token is stored."""

    __tablename__ = "auth_sessions"

    id = Column(Integer, primary_key=True, index=True)
    token_hash = Column(String, unique=True, index=True, nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)


class LoginAttempt(Base):
    """One login attempt, used for server-side throttling."""

    __tablename__ = "login_attempts"

    id = Column(Integer, primary_key=True, index=True)
    identifier = Column(String, nullable=False, index=True)
    ip = Column(String, nullable=True, index=True)
    success = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)


class PasswordResetToken(Base):
    """Single-use password-reset token (stored hashed)."""

    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String, unique=True, index=True, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)


class LoginChallenge(Base):
    """Short-lived human-verification challenge used instead of locking accounts."""

    __tablename__ = "login_challenges"

    id = Column(Integer, primary_key=True, index=True)
    answer_hash = Column(String, nullable=False)
    ip = Column(String, nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used_at = Column(DateTime(timezone=True), nullable=True)



class WorkspaceBranding(Base):
    """Company branding (name, logo, colours) served to every client."""

    __tablename__ = "workspace_branding"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=True)
    logo = Column(Text, nullable=True)
    primary = Column(String, nullable=True)
    accent = Column(String, nullable=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
