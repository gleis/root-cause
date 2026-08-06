"""
End-to-end test of the 8-agent investigation pipeline against DEMO_MODE
fixtures -- the same scenario backend/scripts/run_demo.py exercises
(api-v2.14.3 worker-concurrency increase -> memory pressure -> pods
OOMKilled -> rollback -> recovery). Asserts the pipeline produces evidence,
a ranked hypothesis list, and that the top hypothesis is the expected
OOMKilled/memory-pressure root cause with reasonable confidence.
"""
import asyncio
from datetime import datetime, timedelta, timezone

from app import models
from app.agents.coordinator import run_investigation


def test_oomkilled_scenario_produces_expected_top_hypothesis(db_session):
    incident = models.Incident(
        title="Checkout API elevated error rate / pod restarts",
        severity="sev1",
        environment="production",
        service="checkout-api",
        started_at=datetime.now(timezone.utc) - timedelta(minutes=40),
        affected_components=["checkout-prod"],
        status="investigating",
    )
    db_session.add(incident)
    db_session.commit()
    db_session.refresh(incident)

    result = asyncio.run(run_investigation(incident.incident_id, db_session))

    assert result.status == "resolved"

    evidence_rows = db_session.query(models.Evidence).filter_by(incident_id=incident.incident_id).all()
    assert len(evidence_rows) > 0
    sources = {e.source for e in evidence_rows}
    # Fixtures should produce evidence from at least metrics/logs/kubernetes/gitlab.
    assert {"metrics", "logs", "kubernetes", "gitlab"}.issubset(sources)

    hypotheses = (
        db_session.query(models.Hypothesis)
        .filter_by(incident_id=incident.incident_id)
        .order_by(models.Hypothesis.confidence.desc())
        .all()
    )
    assert len(hypotheses) >= 1
    assert result.insufficient_evidence is False

    top = hypotheses[0]
    statement_lower = top.statement.lower()
    assert "oom" in statement_lower or "memory" in statement_lower
    assert top.confidence > 0.4
    assert top.supporting_evidence  # cites at least one piece of evidence

    # A recommended (text-only, never-executed) action should exist for the
    # top hypothesis.
    actions = db_session.query(models.RecommendedAction).filter_by(incident_id=incident.incident_id).all()
    assert len(actions) >= 1
    assert "not executed" in (actions[0].command_text or "").lower() or "recommendation only" in (actions[0].command_text or "").lower()

    # Postmortem + Slack summary should have been generated.
    assert result.postmortem_markdown
    assert result.slack_summary
