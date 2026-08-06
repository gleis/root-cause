"""
ChangeAgent: queries GitLab (commits, merge requests, pipelines,
deployments) for the affected project in the time window before the
incident, emitting Evidence for deployments/config changes. Deployments
that occur shortly before incident start are the strongest change-related
signal; a later deployment matching a "rollback"-style ref is tagged with
type=rollback so scoring.py's recovery_after_rollback factor can find it.
"""
import logging
from datetime import datetime
from typing import List, Dict, Any

from ..integrations.gitlab_client import GitLabClient

logger = logging.getLogger("rootcause.agents.change")


def run(incident) -> List[Dict[str, Any]]:
    client = GitLabClient(environment=incident.environment)
    data = client.get_incident_window_changes(None, incident.started_at, incident.ended_at)

    evidence: List[Dict[str, Any]] = []

    for c in data.get("commits", []):
        ts = datetime.fromisoformat(c["timestamp"])
        evidence.append({
            "source": "gitlab",
            "type": "commit",
            "timestamp": ts,
            "summary": f"Commit {c.get('sha', '')[:8]} by {c.get('author', 'unknown')}: {c.get('title', '')}",
            "raw_reference": c,
            "relevance_score": 0.6,
        })

    for mr in data.get("merge_requests", []):
        ts = datetime.fromisoformat(mr["timestamp"])
        evidence.append({
            "source": "gitlab",
            "type": "merge_request",
            "timestamp": ts,
            "summary": f"MR !{mr.get('iid')} merged: {mr.get('title', '')}",
            "raw_reference": mr,
            "relevance_score": 0.5,
        })

    for p in data.get("pipelines", []):
        ts = datetime.fromisoformat(p["timestamp"])
        evidence.append({
            "source": "gitlab",
            "type": "pipeline",
            "timestamp": ts,
            "summary": f"Pipeline #{p.get('pipeline_id', p.get('id'))} status={p.get('status')}",
            "raw_reference": p,
            "relevance_score": 0.4,
        })

    for d in data.get("deployments", []):
        ts = datetime.fromisoformat(d["timestamp"])
        description = d.get("description", f"Deployment of {d.get('ref')} to {d.get('environment')}")
        is_rollback = "rollback" in description.lower()
        evidence.append({
            "source": "gitlab",
            "type": "rollback" if is_rollback else "deployment",
            "timestamp": ts,
            "summary": description,
            "raw_reference": d,
            "relevance_score": 0.95 if is_rollback else 0.85,
        })

    if not evidence:
        evidence.append({
            "source": "gitlab",
            "type": "no_change_evidence",
            "timestamp": incident.started_at,
            "summary": "No GitLab commits, merge requests, pipelines, or deployments found in the window preceding the incident.",
            "raw_reference": {"mode": data.get("mode")},
            "relevance_score": 0.1,
        })

    return evidence
