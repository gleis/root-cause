"""
CoordinatorAgent: the orchestrator. Receives an incident, does scope
detection, fans out to the specialist agents, aggregates their Evidence,
tracks investigation status transitions (investigating -> analyzing ->
resolved), then hands off to HypothesisAgent and ReportingAgent.

Implemented as a hand-rolled explicit async state machine rather than
LangGraph -- for an MVP with a fixed, non-branching agent sequence, a plain
function with clear stages is easier to read, debug, and keep reliable than
introducing a graph-execution framework dependency. See README for the
tradeoff discussion.
"""
import logging
from typing import List

from sqlalchemy.orm import Session

from .. import models
from . import metrics_agent, log_agent, change_agent, infra_agent, dependency_agent, hypothesis_agent, reporting_agent

logger = logging.getLogger("rootcause.agents.coordinator")


def detect_scope(incident: models.Incident) -> dict:
    """Scope detection: service/env/namespace from incident metadata +
    affected_components (which, in a real k8s deployment, would carry
    namespace/label info from the alert that opened the incident)."""
    namespace = None
    for c in (incident.affected_components or []):
        if isinstance(c, str) and c:
            namespace = c
            break
    return {
        "service": incident.service,
        "environment": incident.environment,
        "namespace": namespace or incident.service,
    }


async def run_investigation(incident_id: str, db: Session) -> models.Incident:
    incident = db.get(models.Incident, incident_id)
    if incident is None:
        raise ValueError(f"Incident {incident_id} not found")

    incident.status = "investigating"
    db.commit()

    scope = detect_scope(incident)
    logger.info("Investigating incident %s with scope=%s", incident_id, scope)

    # --- Fan out to specialist agents. Each returns a list of evidence dicts
    # (not yet persisted -- no evidence_id until we add+flush them).
    raw_evidence: List[dict] = []
    raw_evidence += _safe_run(metrics_agent.run, incident)
    raw_evidence += _safe_run(log_agent.run, incident)
    raw_evidence += _safe_run(change_agent.run, incident)
    raw_evidence += _safe_run(infra_agent.run, incident)
    raw_evidence += _safe_run(dependency_agent.run, incident, raw_evidence)

    evidence_rows: List[models.Evidence] = []
    for item in raw_evidence:
        row = models.Evidence(
            incident_id=incident.incident_id,
            source=item["source"],
            type=item["type"],
            timestamp=item["timestamp"],
            summary=item["summary"],
            raw_reference=item.get("raw_reference", {}),
            relevance_score=item.get("relevance_score", 0.5),
        )
        db.add(row)
        evidence_rows.append(row)
    db.flush()  # assign evidence_id / defaults without committing yet

    incident.status = "analyzing"
    db.commit()

    # --- Hypothesis generation (deterministic scoring + LLM prose only)
    hyp_dicts = hypothesis_agent.run(incident, evidence_rows)
    hypothesis_rows: List[models.Hypothesis] = []
    for h in hyp_dicts:
        row = models.Hypothesis(
            incident_id=incident.incident_id,
            statement=h["statement"],
            confidence=h["confidence"],
            supporting_evidence=h["supporting_evidence"],
            contradicting_evidence=h["contradicting_evidence"],
            status=h["status"],
            rationale=h.get("rationale", ""),
            score_breakdown=h.get("score_breakdown", {}),
        )
        db.add(row)
        hypothesis_rows.append(row)
    db.flush()

    incident.insufficient_evidence = (
        len(hypothesis_rows) == 1 and hypothesis_rows[0].status == "insufficient_evidence"
    )

    # --- Recommended (text-only, never executed) corrective action based on
    # the top hypothesis, so the approve-action endpoint has something to act on.
    if hypothesis_rows and not incident.insufficient_evidence:
        top = max(hypothesis_rows, key=lambda h: h.confidence)
        action = models.RecommendedAction(
            incident_id=incident.incident_id,
            description=f"Investigate/roll back the change most correlated with the leading hypothesis: {top.statement}",
            command_text="kubectl rollout undo deployment/<affected-deployment>  # RECOMMENDATION ONLY -- not executed by this system",
            status="pending",
        )
        db.add(action)
        db.flush()

    # Flip to resolved before building reports so the Slack summary /
    # postmortem reflect the final status rather than the mid-pipeline
    # "analyzing" state.
    incident.status = "resolved"

    # --- Reporting: timeline, slack summary, postmortem markdown
    timeline = reporting_agent.build_timeline(evidence_rows)
    slack_text = reporting_agent.build_slack_summary(incident, hypothesis_rows, timeline)
    actions = db.query(models.RecommendedAction).filter_by(incident_id=incident.incident_id).all()
    postmortem_md = reporting_agent.build_postmortem(incident, hypothesis_rows, evidence_rows, timeline, actions)

    incident.slack_summary = slack_text
    incident.postmortem_markdown = postmortem_md
    db.commit()
    db.refresh(incident)

    logger.info("Investigation complete for incident %s: %d evidence items, %d hypotheses",
                incident_id, len(evidence_rows), len(hypothesis_rows))
    return incident


def _safe_run(fn, *args):
    """Runs a specialist agent and never lets one agent's failure kill the
    whole investigation -- logs and returns an explicit evidence item noting
    the failure instead, so the gap is visible rather than silent."""
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Agent %s failed", getattr(fn, "__module__", fn))
        incident = args[0]
        return [{
            "source": "system",
            "type": "agent_error",
            "timestamp": incident.started_at,
            "summary": f"Agent {getattr(fn, '__module__', str(fn))} failed: {exc}",
            "raw_reference": {"error": str(exc)},
            "relevance_score": 0.0,
        }]
