"""
InfrastructureAgent: queries the Kubernetes API for pod events, restarts,
OOMKilled events, and node conditions in the namespace/time window relevant
to the incident, emitting Evidence. Read-only (see integrations/kubernetes_client.py).
"""
import logging
from datetime import datetime
from typing import List, Dict, Any

from ..integrations.kubernetes_client import KubernetesClient

logger = logging.getLogger("rootcause.agents.infra")


def run(incident) -> List[Dict[str, Any]]:
    client = KubernetesClient(environment=incident.environment)
    namespace = _namespace_for(incident)
    data = client.get_incident_window_events(namespace, incident.started_at, incident.ended_at)

    evidence: List[Dict[str, Any]] = []

    pod_events = data.get("pod_events", [])
    oom_events = [e for e in pod_events if (e.get("reason") or "").upper() == "OOMKILLED"]
    other_events = [e for e in pod_events if (e.get("reason") or "").upper() != "OOMKILLED"]

    if oom_events:
        first_ts = datetime.fromisoformat(oom_events[0]["timestamp"])
        last_ts = datetime.fromisoformat(oom_events[-1]["timestamp"])
        pod_names = [e["pod"] for e in oom_events]
        evidence.append({
            "source": "kubernetes",
            "type": "oom_killed",
            "timestamp": first_ts,
            "summary": f"{len(oom_events)} pods OOMKilled in namespace {namespace} between {first_ts.time()} and {last_ts.time()}: "
                       + ", ".join(pod_names[:6]) + ("..." if len(pod_names) > 6 else ""),
            "raw_reference": {"pods": pod_names, "events": oom_events},
            "relevance_score": 0.95,
        })

    for e in other_events:
        ts = datetime.fromisoformat(e["timestamp"])
        evidence.append({
            "source": "kubernetes",
            "type": "pod_event",
            "timestamp": ts,
            "summary": f"Pod {e.get('pod')} event: {e.get('reason')}",
            "raw_reference": e,
            "relevance_score": 0.4,
        })

    for nc in data.get("node_conditions", []):
        ts = datetime.fromisoformat(nc["timestamp"])
        if nc.get("status") in ("True", True):
            evidence.append({
                "source": "kubernetes",
                "type": "node_condition",
                "timestamp": ts,
                "summary": f"Node {nc.get('node')} reported condition {nc.get('condition')}=True",
                "raw_reference": nc,
                "relevance_score": 0.7,
            })

    for r in data.get("restarts", []):
        ts = datetime.fromisoformat(r["timestamp"])
        evidence.append({
            "source": "kubernetes",
            "type": "rollback" if "rollback" in (r.get("reason") or "").lower() else "restart",
            "timestamp": ts,
            "summary": f"Deployment {r.get('deployment')} action: {r.get('reason')} ({r.get('replicas')} replicas)",
            "raw_reference": r,
            "relevance_score": 0.6,
        })

    if not evidence:
        evidence.append({
            "source": "kubernetes",
            "type": "no_infra_evidence",
            "timestamp": incident.started_at,
            "summary": f"No relevant Kubernetes pod events or node condition changes found in namespace {namespace}.",
            "raw_reference": {"mode": data.get("mode"), "namespace": namespace},
            "relevance_score": 0.1,
        })

    return evidence


def _namespace_for(incident) -> str:
    """Scope-detection heuristic: prefer an explicit affected_components
    entry that looks like a namespace, else fall back to service name."""
    for c in (incident.affected_components or []):
        if isinstance(c, str) and c:
            return c
    return incident.service
