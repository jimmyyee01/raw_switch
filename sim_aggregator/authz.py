"""Per-request identity shared by the API and billing modules."""

from contextvars import ContextVar

CURRENT_USER: ContextVar[dict] = ContextVar("current_user", default={})


def current_user() -> dict:
    return CURRENT_USER.get() or {}


def is_customer() -> bool:
    return current_user().get("role") == "Customer"


def customer_scope() -> str:
    return str(current_user().get("customerName") or "")


def can(permission: str) -> bool:
    user = current_user()
    if user.get("role") == "Administrator":
        return True
    return permission in (user.get("permissions") or [])
