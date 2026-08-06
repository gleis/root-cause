"""
Audit logging: one AuditLog row per mutating endpoint call or investigation
trigger. Deliberately a plain helper function (not middleware) so each
call site can supply a meaningful resource_type/resource_id/action rather
than trying to infer them generically from the request path.
"""
import logging

from fastapi import Request
from sqlalchemy.orm import Session

from . import models
from .auth import Actor

logger = logging.getLogger("rootcause.audit")


def audit(
    db: Session,
    request: Request,
    actor: Actor,
    action: str,
    resource_type: str | None = None,
    resource_id: str | None = None,
    result: str = "success",
    detail: str | None = None,
) -> None:
    """Writes an audit_log row. Never raises -- an audit-logging failure
    must not take down the request it's auditing; it's logged instead."""
    try:
        client_ip = request.client.host if request and request.client else None
        row = models.AuditLog(
            actor_id=actor.id,
            actor_type=actor.type,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            ip_address=client_ip,
            result=result,
            detail=detail,
        )
        db.add(row)
        db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("Failed to write audit log entry for action=%s", action)
