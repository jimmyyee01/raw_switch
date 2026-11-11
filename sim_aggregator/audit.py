"""Shared activity-log helper.

Kept in its own module so both the API (main.py) and billing.py can record
events without importing each other.
"""

from contextvars import ContextVar

from . import models

AUDIT_ACTOR: ContextVar[str] = ContextVar("audit_actor", default="system")


def audit(db, action: str, target: str = "", detail: str = "") -> None:
    """Queue one audit row. Never let logging break the request."""
    try:
        db.add(models.AuditEvent(
            actor=(AUDIT_ACTOR.get() or "system")[:120],
            action=str(action)[:80],
            target=str(target or "")[:160],
            detail=str(detail or "")[:1000],
        ))
    except Exception:
        pass
