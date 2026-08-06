"""
FastAPI application: REST API (spec section 11) + static frontend hosting.

Background execution note: the investigation pipeline runs via FastAPI's
BackgroundTasks (asyncio), not Celery, for this MVP -- see config.py for the
rationale. Redis/Celery remain wired in docker-compose.yml / requirements.txt
for a straightforward upgrade path.
"""
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, Depends, HTTPException, BackgroundTasks, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from . import models, schemas
from .config import get_settings
from .database import get_db, init_db, SessionLocal
from .integrations.slack_client import SlackClient, verify_slack_signature
from .routers.alerts import router as alerts_router
from .auth import Actor, require_role, hash_api_key
from .audit import audit
from .tasks import run_investigation_sync

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("rootcause.main")

app = FastAPI(title="AI Root Cause Investigator", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(alerts_router)


@app.on_event("startup")
def on_startup():
    try:
        init_db()
    except Exception as exc:  # noqa: BLE001
        # Don't crash the whole app if postgres isn't reachable yet (e.g.
        # during docker-compose startup ordering) -- endpoints that need the
        # DB will surface a clear error instead.
        logger.warning("init_db failed at startup (DB may not be reachable yet): %s", exc)


# Kept as a module-level alias so routers/alerts.py's existing
# `from ..main import _run_investigation_sync` import (and any other
# external caller) keeps working -- the actual implementation now lives in
# app/tasks.py so it's importable without going through main.py.
_run_investigation_sync = run_investigation_sync


def dispatch_investigation(incident_id: str, background_tasks: BackgroundTasks) -> None:
    """
    Durable dispatch: hands the investigation off to a Celery task (backed
    by Redis) so it survives an API process restart. If the broker is
    unreachable -- e.g. Redis isn't running, which is common in
    tests/DEMO_MODE/local dev without `docker-compose up` -- falls back to
    FastAPI's in-process BackgroundTasks so the endpoint still works, and
    logs a clear warning that durability isn't actually in effect for this
    call.
    """
    try:
        from .tasks import run_investigation_task
        run_investigation_task.delay(incident_id)
    except Exception as exc:  # noqa: BLE001 -- broker down, misconfigured, etc.
        logger.warning(
            "Celery broker unavailable (%s); falling back to in-process BackgroundTasks for "
            "incident %s. This investigation will NOT survive an API process restart.",
            exc, incident_id,
        )
        background_tasks.add_task(run_investigation_sync, incident_id)


# ---------------------------------------------------------------------------
# Incidents
# ---------------------------------------------------------------------------

@app.post("/api/v1/incidents", response_model=schemas.IncidentOut)
def create_incident(
    payload: schemas.IncidentCreate,
    background_tasks: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    actor: Actor = Depends(require_role("engineer")),
):
    incident = models.Incident(
        title=payload.title,
        severity=payload.severity,
        environment=payload.environment,
        service=payload.service,
        started_at=payload.started_at or datetime.now(timezone.utc),
        affected_components=payload.affected_components,
        status="investigating",
    )
    db.add(incident)
    db.commit()
    db.refresh(incident)

    dispatch_investigation(incident.incident_id, background_tasks)
    audit(db, request, actor, "incident.create", "incident", incident.incident_id)
    return incident


@app.get("/api/v1/incidents/{incident_id}", response_model=schemas.IncidentOut)
def get_incident(incident_id: str, db: Session = Depends(get_db), actor: Actor = Depends(require_role("viewer"))):
    incident = db.get(models.Incident, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


@app.get("/api/v1/incidents/{incident_id}/hypotheses", response_model=list[schemas.HypothesisOut])
def get_hypotheses(incident_id: str, db: Session = Depends(get_db), actor: Actor = Depends(require_role("viewer"))):
    _require_incident(db, incident_id)
    rows = (
        db.query(models.Hypothesis)
        .filter_by(incident_id=incident_id)
        .order_by(models.Hypothesis.confidence.desc())
        .all()
    )
    return rows


@app.get("/api/v1/incidents/{incident_id}/timeline", response_model=schemas.TimelineOut)
def get_timeline(incident_id: str, db: Session = Depends(get_db), actor: Actor = Depends(require_role("viewer"))):
    _require_incident(db, incident_id)
    rows = (
        db.query(models.Evidence)
        .filter_by(incident_id=incident_id)
        .order_by(models.Evidence.timestamp.asc())
        .all()
    )
    entries = [
        schemas.TimelineEntry(
            timestamp=r.timestamp, source=r.source, type=r.type, summary=r.summary, evidence_id=r.evidence_id
        )
        for r in rows
    ]
    return schemas.TimelineOut(incident_id=incident_id, entries=entries)


@app.get("/api/v1/incidents/{incident_id}/evidence", response_model=list[schemas.EvidenceOut])
def get_evidence(incident_id: str, source: Optional[str] = None, db: Session = Depends(get_db), actor: Actor = Depends(require_role("viewer"))):
    _require_incident(db, incident_id)
    q = db.query(models.Evidence).filter_by(incident_id=incident_id)
    if source:
        q = q.filter_by(source=source)
    return q.order_by(models.Evidence.timestamp.asc()).all()


@app.post("/api/v1/incidents/{incident_id}/evidence", response_model=schemas.EvidenceOut)
def add_evidence(
    incident_id: str,
    payload: schemas.EvidenceCreate,
    request: Request,
    db: Session = Depends(get_db),
    actor: Actor = Depends(require_role("engineer")),
):
    """Lets an engineer attach a manual note as Evidence (source=engineer_note)."""
    _require_incident(db, incident_id)
    row = models.Evidence(
        incident_id=incident_id,
        source="engineer_note",
        type=payload.type,
        timestamp=payload.timestamp or datetime.now(timezone.utc),
        summary=payload.summary,
        raw_reference=payload.raw_reference,
        relevance_score=payload.relevance_score,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    audit(db, request, actor, "evidence.add", "evidence", row.evidence_id, detail=f"incident={incident_id}")
    return row


@app.post("/api/v1/incidents/{incident_id}/actions/{action_id}/approve", response_model=schemas.ActionOut)
def approve_action(
    incident_id: str,
    action_id: str,
    payload: schemas.ActionApproveRequest,
    request: Request,
    db: Session = Depends(get_db),
    actor: Actor = Depends(require_role("admin")),
):
    """
    Marks a recommended (text-only) action as approved for audit/tracking.
    IMPORTANT: this endpoint never executes anything. RecommendedAction rows
    only ever hold descriptive text / a command string that is displayed,
    never run -- see app/safety.py and app/models.py for the enforcement
    and documentation of that boundary. Admin-only: approving a corrective
    action is a consequential decision even though nothing auto-executes.
    """
    action = db.get(models.RecommendedAction, action_id)
    if not action or action.incident_id != incident_id:
        raise HTTPException(status_code=404, detail="Action not found")
    action.status = "approved"
    action.approved_by = payload.approved_by
    action.approved_at = datetime.now(timezone.utc)
    db.commit()
    audit(db, request, actor, "action.approve", "recommended_action", action_id, detail=f"incident={incident_id}")
    db.refresh(action)
    return action


@app.get("/api/v1/incidents/{incident_id}/postmortem", response_model=schemas.PostmortemOut)
def get_postmortem(incident_id: str, db: Session = Depends(get_db), actor: Actor = Depends(require_role("viewer"))):
    incident = _require_incident(db, incident_id)
    if not incident.postmortem_markdown:
        raise HTTPException(status_code=409, detail="Postmortem not yet generated; investigation may still be running.")
    return schemas.PostmortemOut(
        incident_id=incident_id,
        markdown=incident.postmortem_markdown,
        generated_at=incident.updated_at or datetime.now(timezone.utc),
        insufficient_evidence=bool(incident.insufficient_evidence),
    )


# ---------------------------------------------------------------------------
# Slack slash-command style endpoint
# ---------------------------------------------------------------------------

def _run_slack_investigation_and_notify(incident_id: str, channel: Optional[str]):
    """Background task for the Slack path: runs the investigation (same as
    _run_investigation_sync) and then posts a follow-up chat.postMessage
    with the outcome -- this is Slack's documented "immediate ack + deferred
    response" pattern for slash commands that can't finish within the 3s
    ack window."""
    _run_investigation_sync(incident_id)
    db = SessionLocal()
    try:
        incident = db.get(models.Incident, incident_id)
        if incident is None:
            return
        text = incident.slack_summary or (
            f":warning: Investigation for incident `{incident_id}` finished with status "
            f"`{incident.status}` but produced no summary."
        )
        SlackClient().post_message(text, channel=channel)
    except Exception:
        logger.exception("Failed to post Slack follow-up for incident %s", incident_id)
    finally:
        db.close()


@app.post("/api/v1/slack/investigate", response_model=schemas.SlackInvestigateResponse)
async def slack_investigate(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    """
    Handles a Slack `/investigate <service>` slash command.

    Slack slash commands POST `application/x-www-form-urlencoded` bodies
    (not JSON) and sign every request with X-Slack-Signature /
    X-Slack-Request-Timestamp (HMAC-SHA256 over "v0:{timestamp}:{raw_body}"
    keyed by SLACK_SIGNING_SECRET) -- see
    https://api.slack.com/authentication/verifying-requests-from-slack.
    We verify that signature before trusting the payload (skipped only in
    DEMO_MODE, where there's no real Slack app to sign requests).

    Slack requires a response within 3 seconds, so this endpoint immediately
    acks with a "started" message and runs the investigation + a follow-up
    chat.postMessage in a background task.
    """
    settings = get_settings()
    raw_body = await request.body()

    if not settings.demo_mode:
        from .secrets import get_vault_client
        signing_secret = get_vault_client().get_slack_signing_secret()
        timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
        signature = request.headers.get("X-Slack-Signature", "")
        if not verify_slack_signature(signing_secret, timestamp, raw_body, signature):
            raise HTTPException(status_code=401, detail="Invalid Slack request signature")

    form = await request.form()
    text = form.get("text", "")
    channel_name = form.get("channel_name") or form.get("channel_id")

    service = (text or "").strip() or "unknown-service"
    incident = models.Incident(
        title=f"Investigation requested via Slack for {service}",
        severity="sev3",
        environment="production",
        service=service,
        started_at=datetime.now(timezone.utc),
        affected_components=[service],
        status="investigating",
    )
    db.add(incident)
    db.commit()
    db.refresh(incident)

    background_tasks.add_task(_run_slack_investigation_and_notify, incident.incident_id, channel_name)

    response_text = (
        f":mag: Investigation started for *{service}* (incident `{incident.incident_id}`). "
        f"I'll post an update in this channel once evidence gathering and hypothesis scoring finish."
    )
    return schemas.SlackInvestigateResponse(incident_id=incident.incident_id, response_text=response_text)


# ---------------------------------------------------------------------------
# API key management (admin-only)
# ---------------------------------------------------------------------------

@app.get("/api/v1/api-keys", response_model=list[schemas.ApiKeyOut])
def list_api_keys(db: Session = Depends(get_db), actor: Actor = Depends(require_role("admin"))):
    return db.query(models.ApiKey).order_by(models.ApiKey.created_at.desc()).all()


@app.post("/api/v1/api-keys/{key_id}/revoke", response_model=schemas.ApiKeyOut)
def revoke_api_key(key_id: str, request: Request, db: Session = Depends(get_db), actor: Actor = Depends(require_role("admin"))):
    """Revokes an API key (does not delete it, so it stays visible in audit
    history). Minting new keys is done out-of-band via
    backend/scripts/create_api_key.py, which requires shell/DB access, not
    an API call -- this endpoint only covers revocation."""
    row = db.get(models.ApiKey, key_id)
    if not row:
        raise HTTPException(status_code=404, detail="API key not found")
    row.revoked_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(row)
    audit(db, request, actor, "api_key.revoke", "api_key", key_id)
    return row


# ---------------------------------------------------------------------------
# Audit log (admin-only)
# ---------------------------------------------------------------------------

@app.get("/api/v1/audit-log", response_model=list[schemas.AuditLogOut])
def get_audit_log(
    limit: int = 100,
    actor_id: Optional[str] = None,
    action: Optional[str] = None,
    db: Session = Depends(get_db),
    actor: Actor = Depends(require_role("admin")),
):
    q = db.query(models.AuditLog)
    if actor_id:
        q = q.filter_by(actor_id=actor_id)
    if action:
        q = q.filter_by(action=action)
    rows = q.order_by(models.AuditLog.timestamp.desc()).limit(min(limit, 1000)).all()
    return rows


def _require_incident(db: Session, incident_id: str) -> models.Incident:
    incident = db.get(models.Incident, incident_id)
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    return incident


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

_FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "frontend")
if os.path.isdir(_FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
