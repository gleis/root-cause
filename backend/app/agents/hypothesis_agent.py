"""
HypothesisAgent: given ALL gathered evidence + the merged timeline, produces
3-5 ranked root-cause hypotheses.

Guardrails (spec section on AI guardrails -- enforced in code, not just by
asking nicely in the prompt):
  1. Every hypothesis must cite specific evidence_ids as supporting evidence.
     Any evidence_id the LLM invents that doesn't exist in the actual
     evidence set is silently dropped before persistence.
  2. Contradicting evidence is always attached and shown alongside supporting
     evidence -- never hidden. Hypotheses formed with zero real supporting
     evidence after id-validation are discarded entirely (rather than kept
     with fabricated backing).
  3. If there is no real evidence to work with (only "no_*_evidence"
     placeholder items from the specialist agents), the system returns a
     single explicit "insufficient evidence" hypothesis instead of ever
     declaring a cause.
  4. Confidence is NEVER produced by the LLM. It is always computed
     deterministically in agents/scoring.py from the structured Evidence
     objects. The LLM is only used for the natural-language statement and
     rationale, and is explicitly instructed that confidence is a
     ranking/prioritization heuristic, not a certainty claim.
"""
import logging
from typing import List, Dict, Any

from .scoring import EvidenceLike, score_hypothesis
from ..llm_gateway import get_llm_gateway, LLMTask, OllamaUnavailableError

logger = logging.getLogger("rootcause.agents.hypothesis")

_PLACEHOLDER_TYPES = {
    "no_anomaly", "no_log_evidence", "no_change_evidence", "no_infra_evidence",
}

_SYSTEM_PROMPT = """You are an SRE root-cause-analysis assistant. You will be given a
list of structured Evidence items (each with an evidence_id, source, type, timestamp,
and summary) gathered from metrics, logs, GitLab change history, Kubernetes, and
dependency checks for a single incident.

Rules you MUST follow:
1. Only reason over the evidence items given to you. Never invent, assume, or reference
   any metric, log line, deployment, or event that is not explicitly present in the
   evidence list.
2. Every hypothesis must cite the specific evidence_id(s) that support it in
   "supporting_evidence_ids", using only IDs that appear in the evidence list.
3. Every hypothesis must also list any evidence_id(s) that argue AGAINST it (evidence
   inconsistent with, or not explained by, this hypothesis) in "contradicting_evidence_ids".
   If there is genuinely no contradicting evidence, return an empty list -- do not omit
   the field, and do not hide contradicting evidence to make a hypothesis look stronger.
4. Do NOT output a numeric confidence or probability. Confidence is computed separately
   by a deterministic scoring system from the evidence -- you only produce the natural
   language statement and a short rationale.
5. Produce between 3 and 5 distinct hypotheses ordered from most to least plausible given
   the evidence, unless there are fewer than 3 plausible independent explanations, in
   which case return fewer rather than padding with implausible ones.
6. If the evidence provided is too sparse or contradictory to support ANY plausible
   hypothesis, return an empty "hypotheses" list rather than forcing a guess.

Respond ONLY with JSON of the shape:
{"hypotheses": [
  {"statement": "<one sentence root-cause hypothesis>",
   "supporting_evidence_ids": ["..."],
   "contradicting_evidence_ids": ["..."],
   "rationale": "<2-3 sentences citing evidence, hedged appropriately, no certainty language>"}
]}
"""


def run(incident, evidence_rows: List) -> List[Dict[str, Any]]:
    """
    evidence_rows: list of models.Evidence ORM rows (already persisted, so
    evidence_id is populated).
    Returns a list of dicts ready to construct models.Hypothesis rows.
    """
    real_evidence = [e for e in evidence_rows if e.type not in _PLACEHOLDER_TYPES]

    if not real_evidence:
        return [{
            "statement": "Insufficient evidence to determine a root cause. No metric, log, "
                         "change, or infrastructure evidence was found for this incident window.",
            "confidence": 0.0,
            "supporting_evidence": [],
            "contradicting_evidence": [e.evidence_id for e in evidence_rows],
            "status": "insufficient_evidence",
            "rationale": "All specialist agents reported no anomalies or relevant events in the "
                         "incident window. Declaring a root cause here would not be supported by evidence.",
            "score_breakdown": {},
        }]

    evidence_by_id = {e.evidence_id: e for e in evidence_rows}
    evidence_text = "\n".join(
        f"- id={e.evidence_id} source={e.source} type={e.type} timestamp={e.timestamp.isoformat()} :: {e.summary}"
        for e in evidence_rows
    )

    llm_hypotheses: List[dict] = []
    try:
        gateway = get_llm_gateway()
        user_prompt = f"Incident: {incident.title} (service={incident.service}, started_at={incident.started_at.isoformat()})\n\nEvidence:\n{evidence_text}"
        result = gateway.generate_json(LLMTask.HYPOTHESIS, _SYSTEM_PROMPT, user_prompt)
        llm_hypotheses = result.get("hypotheses", []) if isinstance(result, dict) else []
    except OllamaUnavailableError:
        logger.warning("HypothesisAgent: Ollama unavailable, falling back to heuristic hypothesis generation.")

    if not llm_hypotheses:
        llm_hypotheses = _heuristic_hypotheses(evidence_rows)

    hypotheses_out: List[Dict[str, Any]] = []
    for h in llm_hypotheses[:5]:
        statement = (h.get("statement") or "").strip()
        if not statement:
            continue

        # Guardrail: drop any cited evidence_id that doesn't actually exist.
        supporting_ids = [eid for eid in h.get("supporting_evidence_ids", []) if eid in evidence_by_id]
        contradicting_ids = [eid for eid in h.get("contradicting_evidence_ids", []) if eid in evidence_by_id]

        # Guardrail: a hypothesis with zero real supporting evidence after
        # validation is discarded -- we never keep a hypothesis whose only
        # backing was a fabricated evidence_id.
        if not supporting_ids:
            logger.info("Dropping hypothesis with no valid supporting evidence: %r", statement)
            continue

        supporting_items = [
            EvidenceLike(e.evidence_id, e.source, e.type, e.timestamp, e.relevance_score)
            for e in (evidence_by_id[i] for i in supporting_ids)
        ]
        contradicting_items = [
            EvidenceLike(e.evidence_id, e.source, e.type, e.timestamp, e.relevance_score)
            for e in (evidence_by_id[i] for i in contradicting_ids)
        ]
        all_items = [
            EvidenceLike(e.evidence_id, e.source, e.type, e.timestamp, e.relevance_score)
            for e in evidence_rows
        ]

        breakdown = score_hypothesis(
            supporting_evidence=supporting_items,
            contradicting_evidence=contradicting_items,
            incident_started_at=incident.started_at,
            all_evidence=all_items,
        )

        hypotheses_out.append({
            "statement": statement,
            "confidence": breakdown.weighted_total(),
            "supporting_evidence": supporting_ids,
            "contradicting_evidence": contradicting_ids,
            "status": "proposed",
            "rationale": h.get("rationale", ""),
            "score_breakdown": breakdown.as_dict(),
        })

    hypotheses_out.sort(key=lambda h: h["confidence"], reverse=True)

    if not hypotheses_out:
        return [{
            "statement": "Evidence was gathered but no hypothesis could be formed with valid, "
                         "cited supporting evidence. Manual investigation is recommended.",
            "confidence": 0.0,
            "supporting_evidence": [],
            "contradicting_evidence": [e.evidence_id for e in real_evidence],
            "status": "insufficient_evidence",
            "rationale": "Automated hypothesis generation did not produce any hypothesis backed by "
                         "verifiable evidence_ids.",
            "score_breakdown": {},
        }]

    return hypotheses_out


def _heuristic_hypotheses(evidence_rows: List) -> List[dict]:
    """
    Fallback used only when Ollama is unreachable: builds template-based
    hypotheses straight from evidence types/sources, still respecting the
    "cite real evidence_ids" contract. This keeps the demo runnable even
    with no local LLM installed, while making it obvious (via rationale
    text) that this is a non-LLM fallback path.
    """
    by_type: Dict[str, List] = {}
    for e in evidence_rows:
        by_type.setdefault(e.type, []).append(e)

    hyps = []

    if "oom_killed" in by_type and ("deployment" in by_type or "commit" in by_type):
        deploy_ids = [e.evidence_id for e in by_type.get("deployment", [])] + [e.evidence_id for e in by_type.get("commit", [])]
        oom_ids = [e.evidence_id for e in by_type["oom_killed"]]
        contradicting = [e.evidence_id for e in by_type.get("no_change_evidence", [])]
        hyps.append({
            "statement": "A recent deployment increased resource usage (e.g. worker concurrency), "
                         "causing pods to exceed their memory limit and be OOMKilled.",
            "supporting_evidence_ids": deploy_ids + oom_ids + [e.evidence_id for e in by_type.get("metric_anomaly", [])],
            "contradicting_evidence_ids": contradicting,
            "rationale": "[heuristic fallback, Ollama unavailable] Deployment evidence and OOMKilled pod events "
                         "occur close together in the timeline, with memory metric anomalies in between.",
        })

    if "metric_anomaly" in by_type:
        ids = [e.evidence_id for e in by_type["metric_anomaly"]]
        hyps.append({
            "statement": "Resource saturation (memory and/or CPU) beyond baseline directly preceded the "
                         "error-rate and latency spike.",
            "supporting_evidence_ids": ids,
            "contradicting_evidence_ids": [],
            "rationale": "[heuristic fallback, Ollama unavailable] Multiple metric samples deviate sharply "
                         "from the pre-incident baseline window.",
        })

    if "rollback" in by_type:
        ids = [e.evidence_id for e in by_type["rollback"]]
        hyps.append({
            "statement": "The change that was rolled back was the root cause, since system health "
                         "recovered after the rollback.",
            "supporting_evidence_ids": ids + [e.evidence_id for e in by_type.get("deployment", [])],
            "contradicting_evidence_ids": [],
            "rationale": "[heuristic fallback, Ollama unavailable] Rollback event evidence is present in the timeline.",
        })

    if "dependency_classification" in by_type:
        ids = [e.evidence_id for e in by_type["dependency_classification"]]
        hyps.append({
            "statement": "A downstream dependency (database, cache, queue, or upstream service) "
                         "contributed to or caused the incident.",
            "supporting_evidence_ids": ids,
            "contradicting_evidence_ids": [],
            "rationale": "[heuristic fallback, Ollama unavailable] Dependency-related error signals were "
                         "detected in gathered evidence.",
        })

    return hyps
