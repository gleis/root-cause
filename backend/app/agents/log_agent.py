"""
LogAgent: queries Loki for the affected service/namespace in the incident
window, clusters error lines by a simple normalized-message grouping,
extracts anything that looks like a stack trace, and emits Evidence per
cluster (not per raw line, to avoid evidence explosion).
"""
import logging
import re
from collections import defaultdict
from datetime import datetime
from typing import List, Dict, Any

from ..integrations.loki import LokiClient

logger = logging.getLogger("rootcause.agents.log")

_TOKEN_RE = re.compile(r"[a-f0-9]{6,}|\d+")  # strip pod-suffix/id/numeric noise for clustering


def _cluster_key(message: str) -> str:
    return _TOKEN_RE.sub("#", message)


def run(incident) -> List[Dict[str, Any]]:
    client = LokiClient(environment=incident.environment)
    data = client.get_incident_window_logs(incident.service, incident.environment, incident.started_at, incident.ended_at)

    evidence: List[Dict[str, Any]] = []

    entries = data.get("entries", [])
    if not entries:
        evidence.append({
            "source": "logs",
            "type": "no_log_evidence",
            "timestamp": incident.started_at,
            "summary": "No log entries were returned for the incident window.",
            "raw_reference": {"mode": data.get("mode")},
            "relevance_score": 0.1,
        })
        return evidence

    clusters: Dict[str, List[dict]] = defaultdict(list)
    for entry in entries:
        if entry.get("level") in ("error", "warn"):
            clusters[_cluster_key(entry["message"])].append(entry)

    for key, members in clusters.items():
        first = members[0]
        ts = datetime.fromisoformat(first["timestamp"])
        is_stack_trace = "traceback" in first["message"].lower() or "exception" in first["message"].lower() or "error:" in first["message"].lower()
        relevance = 0.85 if len(members) >= 5 else (0.6 if is_stack_trace else 0.4)
        summary = f"{len(members)}x '{first['message'][:140]}'"
        evidence.append({
            "source": "logs",
            "type": "stack_trace" if is_stack_trace and "traceback" in first["message"].lower() else "log_cluster",
            "timestamp": ts,
            "summary": summary,
            "raw_reference": {"cluster_key": key, "count": len(members), "sample_messages": [m["message"] for m in members[:5]]},
            "relevance_score": relevance,
        })

    # Informational (non-error) log lines that mark rollouts/rollbacks are
    # still useful timeline anchors even though they aren't anomalies.
    for entry in entries:
        if entry.get("level") == "info" and any(k in entry["message"].lower() for k in ("rollout", "rollback", "recover")):
            ts = datetime.fromisoformat(entry["timestamp"])
            evidence.append({
                "source": "logs",
                "type": "deployment_marker" if "rollback" not in entry["message"].lower() else "rollback",
                "timestamp": ts,
                "summary": entry["message"],
                "raw_reference": {"raw_entry": entry},
                "relevance_score": 0.5,
            })

    return evidence
