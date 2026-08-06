"""
Alertmanager webhook intake: POST /api/v1/alerts/webhook.

Accepts a standard Alertmanager `webhook_config` payload
(https://prometheus.io/docs/alerting/latest/configuration/#webhook_config),
maps the first *firing* alert to a new Incident (service/environment from
labels, title from annotations), and kicks off the same investigation
pipeline POST /api/v1/incidents uses -- as a background task, so the
response comes back fast (Alertmanager expects a prompt 200; a slow webhook
receiver gets marked unhealthy and Alertmanager stops delivering to it).

See infra/k3d/alertmanager-config.yaml for the Alertmanager-side routing
config that points at this endpoint, and
infra/k3d/prometheusrule-error-rate.yaml for a sample alert that will
trigger it end-to-end against a live k3d cluster.
"""
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy.orm import Session

from .. import models, schemas
from ..database import get_db
from ..auth import Actor, require_role
from ..audit import audit

logger = logging.getLogger("rootcause.routers.alerts")

router = APIRouter(prefix="/api/v1/alerts", tags=["alerts"])

# Alertmanager's alert-level `severity` label -> this system's Incident
# `severity` field. Anything not in this map falls back to "sev3".
_SEVERITY_MAP = {
    "critical": "sev1",
    "page": "sev1",
    "error": "sev2",
    "warning": "sev3",
    "info": "sev4",
}


def _map_severity(label_value: str) -> str:
    return _SEVERITY_MAP.get((label_value or "").lower(), "sev3")


@router.post("/webhook", response_model=schemas.AlertWebhookResponse)
def alertmanager_webhook(
    payload: schemas.AlertmanagerWebhookPayload,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    # Machine caller: Alertmanager authenticates with an API key (minted via
    # backend/scripts/create_api_key.py, role >= "engineer"), configured as
    # a bearer token / X-API-Key header in Alertmanager's webhook_config.
    # This reuses the same require_role dependency as the human-facing
    # routes (an OIDC JWT would also satisfy it, but there's no real-world
    # reason Alertmanager would ever present one).
    actor: Actor = Depends(require_role("engineer")),
):
    firing = [a for a in payload.alerts if (a.status or "firing") == "firing"]
    if not firing:
        # Nothing to investigate (all alerts already resolved, or an empty
        # batch) -- still 200 so Alertmanager doesn't retry/backoff on us.
        return schemas.AlertWebhookResponse(accepted=False, detail="No firing alerts in payload; nothing created.")

    alert = firing[0]
    labels = alert.labels or {}
    annotations = alert.annotations or {}

    service = labels.get("service") or labels.get("job") or labels.get("deployment") or "unknown-service"
    environment = labels.get("environment") or labels.get("env") or "production"
    severity = _map_severity(labels.get("severity", ""))
    alertname = labels.get("alertname", "Alert")
    title = annotations.get("summary") or annotations.get("description") or f"Alertmanager alert: {alertname}"
    started_at = alert.startsAt or datetime.now(timezone.utc)

    affected_components = [labels[k] for k in ("namespace", "pod", "container") if labels.get(k)]
    if not affected_components:
        affected_components = [service]

    incident = models.Incident(
        title=title,
        severity=severity,
        environment=environment,
        service=service,
        started_at=started_at,
        affected_components=affected_components,
        status="investigating",
    )
    db.add(incident)
    db.commit()
    db.refresh(incident)

    logger.info(
        "Alertmanager webhook: opened incident %s for alert '%s' (service=%s, severity=%s)",
        incident.incident_id, alertname, service, severity,
    )

    # Imported here (not at module scope) to avoid a circular import with
    # main.py, which also defines this same dispatch helper for the manual
    # POST /api/v1/incidents path.
    from ..main import dispatch_investigation

    dispatch_investigation(incident.incident_id, background_tasks)
    audit(db, request, actor, "incident.create", "incident", incident.incident_id, detail="via alertmanager webhook")

    return schemas.AlertWebhookResponse(
        accepted=True,
        incident_id=incident.incident_id,
        detail=f"Incident {incident.incident_id} created for alert '{alertname}'; investigation running in background.",
    )
