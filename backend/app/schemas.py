"""
Pydantic schemas for the REST API. Shapes intentionally mirror models.py /
the product spec's JSON examples.
"""
from datetime import datetime
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, ConfigDict


# ---------------------------------------------------------------------------
# Incident
# ---------------------------------------------------------------------------

class IncidentCreate(BaseModel):
    title: str
    severity: str = "sev3"
    environment: str = "production"
    service: str
    started_at: Optional[datetime] = None
    affected_components: List[str] = Field(default_factory=list)


class IncidentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    incident_id: str
    title: str
    status: str
    severity: str
    environment: str
    service: str
    started_at: datetime
    ended_at: Optional[datetime] = None
    affected_components: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Evidence
# ---------------------------------------------------------------------------

class EvidenceCreate(BaseModel):
    """Used by POST /incidents/{id}/evidence for manual engineer_note evidence."""
    summary: str
    type: str = "engineer_note"
    timestamp: Optional[datetime] = None
    raw_reference: Dict[str, Any] = Field(default_factory=dict)
    relevance_score: float = 0.5


class EvidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    evidence_id: str
    incident_id: str
    source: str
    type: str
    timestamp: datetime
    summary: str
    raw_reference: Dict[str, Any] = Field(default_factory=dict)
    relevance_score: float


# ---------------------------------------------------------------------------
# Hypothesis
# ---------------------------------------------------------------------------

class HypothesisOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    hypothesis_id: str
    incident_id: str
    statement: str
    confidence: float
    supporting_evidence: List[str] = Field(default_factory=list)
    contradicting_evidence: List[str] = Field(default_factory=list)
    status: str
    rationale: str = ""
    score_breakdown: Dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Timeline
# ---------------------------------------------------------------------------

class TimelineEntry(BaseModel):
    timestamp: datetime
    source: str
    type: str
    summary: str
    evidence_id: str


class TimelineOut(BaseModel):
    incident_id: str
    entries: List[TimelineEntry]


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

class ActionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    action_id: str
    incident_id: str
    description: str
    command_text: Optional[str] = None
    status: str
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None


class ActionApproveRequest(BaseModel):
    approved_by: str = "unknown"


# ---------------------------------------------------------------------------
# Postmortem
# ---------------------------------------------------------------------------

class PostmortemOut(BaseModel):
    incident_id: str
    markdown: str
    generated_at: datetime
    insufficient_evidence: bool = False


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

class SlackInvestigateRequest(BaseModel):
    text: str = ""            # slash-command body, e.g. "checkout-service"
    user_name: str = "unknown"
    channel_name: Optional[str] = None


class SlackInvestigateResponse(BaseModel):
    incident_id: str
    response_text: str


# ---------------------------------------------------------------------------
# Alertmanager webhook intake (POST /api/v1/alerts/webhook)
# ---------------------------------------------------------------------------
# Shapes mirror Alertmanager's standard webhook_config payload:
# https://prometheus.io/docs/alerting/latest/configuration/#webhook_config

class AlertmanagerAlert(BaseModel):
    status: str = "firing"
    labels: Dict[str, str] = Field(default_factory=dict)
    annotations: Dict[str, str] = Field(default_factory=dict)
    startsAt: Optional[datetime] = None
    endsAt: Optional[datetime] = None
    generatorURL: Optional[str] = None
    fingerprint: Optional[str] = None


class AlertmanagerWebhookPayload(BaseModel):
    version: Optional[str] = None
    groupKey: Optional[str] = None
    status: Optional[str] = None
    receiver: Optional[str] = None
    groupLabels: Dict[str, str] = Field(default_factory=dict)
    commonLabels: Dict[str, str] = Field(default_factory=dict)
    commonAnnotations: Dict[str, str] = Field(default_factory=dict)
    externalURL: Optional[str] = None
    alerts: List[AlertmanagerAlert] = Field(default_factory=list)


class AlertWebhookResponse(BaseModel):
    accepted: bool
    incident_id: Optional[str] = None
    detail: str


# ---------------------------------------------------------------------------
# Auth: API keys + audit log
# ---------------------------------------------------------------------------

class ApiKeyOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    prefix: str
    role: str
    description: Optional[str] = None
    created_at: datetime
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    timestamp: datetime
    actor_id: str
    actor_type: str
    action: str
    resource_type: Optional[str] = None
    resource_id: Optional[str] = None
    ip_address: Optional[str] = None
    result: str
    detail: Optional[str] = None
