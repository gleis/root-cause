"""
DependencyAgent: lightweight -- does NOT query any new data source. It scans
the evidence already gathered by LogAgent/MetricsAgent/InfrastructureAgent
for DB/queue/cache-related signals (connection errors, timeouts to known
dependency names) via simple heuristics, and uses exactly one local-LLM call
to classify the overall picture as more likely "internal" (caused by our own
deploy/config/code) vs "external" (caused by a downstream dependency), or
"unclear" if evidence doesn't support either. That classification is emitted
as its own Evidence item (source=dependency) so HypothesisAgent can weigh it
like any other evidence -- it is explicitly NOT allowed to invent a root
cause; it only labels a tendency and must cite which evidence it looked at.
"""
import logging
from datetime import datetime
from typing import List, Dict, Any

from ..llm_gateway import get_llm_gateway, LLMTask, OllamaUnavailableError

logger = logging.getLogger("rootcause.agents.dependency")

_DEPENDENCY_KEYWORDS = [
    "connection pool exhausted", "connection refused", "timeout", "timed out",
    "upstream", "gateway", "queue", "redis", "postgres", "mysql", "kafka",
    "rabbitmq", "dns", "certificate", "tls handshake",
]


def run(incident, existing_evidence: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    hits = []
    for ev in existing_evidence:
        summary_lower = (ev.get("summary") or "").lower()
        if any(kw in summary_lower for kw in _DEPENDENCY_KEYWORDS):
            hits.append(ev)

    evidence: List[Dict[str, Any]] = []

    if not hits:
        evidence.append({
            "source": "dependency",
            "type": "dependency_check",
            "timestamp": incident.started_at,
            "summary": "No dependency-related signals (DB/queue/cache connection errors, upstream timeouts) found in gathered evidence.",
            "raw_reference": {"matched_evidence_ids": []},
            "relevance_score": 0.2,
        })
        return evidence

    # Heuristic default in case the LLM is unavailable: presence of an
    # own-service deployment/change close in time to dependency errors leans
    # "internal" (our change likely caused the downstream symptom), absence
    # of any recent own-service change leans "external".
    has_recent_change = any(e.get("source") == "gitlab" and e.get("type") in ("deployment", "commit") for e in existing_evidence)
    heuristic_label = "internal" if has_recent_change else "unclear"

    label = heuristic_label
    rationale = "Heuristic only (LLM unavailable): based on presence/absence of a recent own-service change alongside dependency-related error signals."

    try:
        gateway = get_llm_gateway()
        evidence_text = "\n".join(
            f"- [{e.get('evidence_id', 'pending')}] ({e.get('source')}/{e.get('type')} @ {e.get('timestamp')}): {e.get('summary')}"
            for e in hits
        )
        system_prompt = (
            "You are an SRE assistant classifying whether an incident's dependency-related "
            "symptoms look INTERNAL (caused by this service's own recent deploy/code/config change) "
            "or EXTERNAL (caused by a downstream dependency failing independently), or UNCLEAR if the "
            "evidence does not clearly support either. Only use the evidence provided below -- do not "
            "invent any facts, metrics, or log lines not present in it. Respond ONLY with JSON: "
            '{"label": "internal"|"external"|"unclear", "rationale": "<one or two sentences citing evidence>"}'
        )
        user_prompt = f"Dependency-related evidence:\n{evidence_text}\n\nHas a recent own-service deployment/commit: {has_recent_change}"
        result = gateway.generate_json(LLMTask.CLASSIFICATION, system_prompt, user_prompt)
        if result.get("label") in ("internal", "external", "unclear"):
            label = result["label"]
            rationale = result.get("rationale", rationale)
    except OllamaUnavailableError:
        logger.info("DependencyAgent: Ollama unavailable, using heuristic classification only.")

    evidence.append({
        "source": "dependency",
        "type": "dependency_classification",
        "timestamp": incident.started_at,
        "summary": f"Dependency signal classification: {label}. {rationale}",
        "raw_reference": {"matched_evidence_summaries": [h.get("summary") for h in hits], "label": label},
        "relevance_score": 0.5,
    })
    return evidence
