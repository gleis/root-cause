"""
SQLAlchemy ORM models. Field names/shapes are chosen to match the Pydantic
schemas in schemas.py (and, in turn, the JSON shapes required by the product
spec) as closely as possible.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, String, DateTime, Float, Text, ForeignKey, JSON, Boolean
)
from sqlalchemy.orm import relationship

from .database import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Incident(Base):
    __tablename__ = "incidents"

    incident_id = Column(String, primary_key=True, default=_uuid)
    title = Column(String, nullable=False)
    status = Column(String, nullable=False, default="investigating")
    # investigating -> analyzing -> resolved
    severity = Column(String, nullable=False, default="sev3")
    environment = Column(String, nullable=False, default="production")
    service = Column(String, nullable=False)
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    affected_components = Column(JSON, default=list)

    # Cached pipeline outputs (regenerated each time the investigation runs;
    # kept on the incident row so GET /postmortem and the Slack summary don't
    # need to recompute from scratch on every request).
    postmortem_markdown = Column(Text, nullable=True)
    slack_summary = Column(Text, nullable=True)
    insufficient_evidence = Column(Boolean, default=False)

    created_at = Column(DateTime(timezone=True), default=_now)
    updated_at = Column(DateTime(timezone=True), default=_now, onupdate=_now)

    evidence = relationship("Evidence", back_populates="incident", cascade="all, delete-orphan")
    hypotheses = relationship("Hypothesis", back_populates="incident", cascade="all, delete-orphan")
    actions = relationship("RecommendedAction", back_populates="incident", cascade="all, delete-orphan")


class Evidence(Base):
    __tablename__ = "evidence"

    evidence_id = Column(String, primary_key=True, default=_uuid)
    incident_id = Column(String, ForeignKey("incidents.incident_id"), nullable=False)
    source = Column(String, nullable=False)  # metrics | logs | gitlab | kubernetes | dependency | engineer_note
    type = Column(String, nullable=False)    # e.g. metric_anomaly, log_cluster, deployment, pod_event, note
    timestamp = Column(DateTime(timezone=True), nullable=False)
    summary = Column(Text, nullable=False)
    raw_reference = Column(JSON, default=dict)  # pointer back to raw data (query, log lines, commit sha, etc.)
    relevance_score = Column(Float, default=0.5)

    incident = relationship("Incident", back_populates="evidence")


class Hypothesis(Base):
    __tablename__ = "hypotheses"

    hypothesis_id = Column(String, primary_key=True, default=_uuid)
    incident_id = Column(String, ForeignKey("incidents.incident_id"), nullable=False)
    statement = Column(Text, nullable=False)
    confidence = Column(Float, nullable=False, default=0.0)
    supporting_evidence = Column(JSON, default=list)     # list[evidence_id]
    contradicting_evidence = Column(JSON, default=list)  # list[evidence_id]
    status = Column(String, default="proposed")  # proposed | confirmed | rejected
    rationale = Column(Text, default="")
    score_breakdown = Column(JSON, default=dict)  # per-factor weighted scoring, see agents/scoring.py

    incident = relationship("Incident", back_populates="hypotheses")


class RecommendedAction(Base):
    """
    Purely advisory / textual. Nothing in this table is ever executed by the
    system -- approving an action just flips a status flag for audit /
    tracking purposes. See app/safety.py for the hard read-only enforcement
    on the diagnostic-command side.
    """
    __tablename__ = "recommended_actions"

    action_id = Column(String, primary_key=True, default=_uuid)
    incident_id = Column(String, ForeignKey("incidents.incident_id"), nullable=False)
    description = Column(Text, nullable=False)
    command_text = Column(Text, nullable=True)  # e.g. "kubectl rollout undo deployment/api-v2" -- TEXT ONLY, never run
    status = Column(String, default="pending")  # pending | approved | rejected
    approved_by = Column(String, nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)

    incident = relationship("Incident", back_populates="actions")


class ApiKey(Base):
    """
    Service-to-service auth (Alertmanager webhook, CI, scripts). The
    plaintext key is shown to the admin exactly once at creation time
    (backend/scripts/create_api_key.py) and never stored -- only a SHA-256
    hash of it is persisted, alongside a short, non-secret `prefix` (first 8
    chars of the plaintext key) so admins/logs can identify *which* key was
    used without being able to reconstruct it.
    """
    __tablename__ = "api_keys"

    id = Column(String, primary_key=True, default=_uuid)
    key_hash = Column(String, nullable=False, unique=True, index=True)  # sha256 hex digest
    prefix = Column(String, nullable=False, index=True)  # first 8 chars of the plaintext key, for identification
    role = Column(String, nullable=False, default="viewer")  # viewer | engineer | admin
    description = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_now)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)


class AuditLog(Base):
    """
    Append-only record of every mutating/authenticated action. Written by
    the `audit()` helper in app/audit.py, invoked from every mutating
    endpoint and every investigation trigger. Never updated or deleted by
    the application itself (retention/pruning, if ever needed, is a DBA
    operation outside this codebase's scope).
    """
    __tablename__ = "audit_log"

    id = Column(String, primary_key=True, default=_uuid)
    timestamp = Column(DateTime(timezone=True), default=_now, nullable=False)
    actor_id = Column(String, nullable=False)     # OIDC `sub` claim, or api_keys.id
    actor_type = Column(String, nullable=False)   # "oidc_user" | "api_key" | "system"
    action = Column(String, nullable=False)       # e.g. "incident.create", "evidence.add"
    resource_type = Column(String, nullable=True)
    resource_id = Column(String, nullable=True)
    ip_address = Column(String, nullable=True)
    result = Column(String, nullable=False, default="success")  # success | denied | error
    detail = Column(Text, nullable=True)
