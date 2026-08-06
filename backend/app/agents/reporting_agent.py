"""
ReportingAgent: builds the chronological timeline, a Slack-formatted summary
message, and a structured postmortem markdown document from the incident's
evidence + hypotheses. Uses one local-LLM call to write the prose Summary /
Contributing Factors sections of the postmortem, strictly instructed to only
describe what is in the provided evidence/hypotheses -- with a deterministic
template fallback if Ollama is unavailable so the demo always produces a
complete postmortem.
"""
import logging
from datetime import datetime
from typing import List, Dict, Any

from ..llm_gateway import get_llm_gateway, LLMTask, OllamaUnavailableError

logger = logging.getLogger("rootcause.agents.reporting")


def build_timeline(evidence_rows: List) -> List[dict]:
    ordered = sorted(evidence_rows, key=lambda e: e.timestamp)
    return [
        {
            "timestamp": e.timestamp,
            "source": e.source,
            "type": e.type,
            "summary": e.summary,
            "evidence_id": e.evidence_id,
        }
        for e in ordered
    ]


def build_slack_summary(incident, hypotheses: List, timeline: List[dict]) -> str:
    top = hypotheses[0] if hypotheses else None
    lines = [
        f"*Incident Investigation: {incident.title}*",
        f"Service: `{incident.service}`  |  Env: `{incident.environment}`  |  Severity: `{incident.severity}`  |  Status: `{incident.status}`",
        "",
    ]
    if top and top.status != "insufficient_evidence":
        lines.append(f"*Most likely cause* ({top.confidence * 100:.0f}% confidence): {top.statement}")
        if top.supporting_evidence:
            lines.append(f"Supporting evidence: {', '.join(top.supporting_evidence[:5])}")
        if top.contradicting_evidence:
            lines.append(f"Contradicting evidence: {', '.join(top.contradicting_evidence[:5])}")
    else:
        lines.append("*Insufficient evidence to determine a most-likely cause.* Manual investigation recommended.")

    lines.append("")
    lines.append(f"*Timeline* ({len(timeline)} events):")
    for entry in timeline[:8]:
        lines.append(f"  - `{entry['timestamp']}` [{entry['source']}] {entry['summary'][:120]}")
    if len(timeline) > 8:
        lines.append(f"  ... and {len(timeline) - 8} more events")

    if len(hypotheses) > 1:
        lines.append("")
        lines.append("*Other hypotheses considered:*")
        for h in hypotheses[1:4]:
            lines.append(f"  - ({h.confidence * 100:.0f}%) {h.statement}")

    return "\n".join(lines)


_POSTMORTEM_SYSTEM_PROMPT = """You are an SRE assistant writing the prose sections of a postmortem
document. You will be given the incident metadata, the ranked hypotheses (with their evidence
citations and confidence scores), and the timeline. Write ONLY the "Summary" and
"Contributing Factors" prose (2-4 sentences each). Do not invent any fact, evidence, metric, or
event that is not present in what you were given. If the top hypothesis is marked
insufficient_evidence, say plainly that a root cause could not be determined and that confidence
scores here are ranking heuristics only, not certainty. Respond ONLY with JSON:
{"summary": "...", "contributing_factors": "..."}
"""


def _llm_prose(incident, hypotheses: List, timeline: List[dict]) -> Dict[str, str]:
    top = hypotheses[0] if hypotheses else None
    try:
        gateway = get_llm_gateway()
        hyp_text = "\n".join(
            f"- ({h.confidence*100:.0f}% confidence) {h.statement} "
            f"[supports: {', '.join(h.supporting_evidence) or 'none'}; contradicts: {', '.join(h.contradicting_evidence) or 'none'}]"
            for h in hypotheses
        )
        user_prompt = (
            f"Incident: {incident.title} | service={incident.service} | env={incident.environment} | "
            f"severity={incident.severity} | started_at={incident.started_at.isoformat()}\n\n"
            f"Hypotheses:\n{hyp_text}\n\nTimeline has {len(timeline)} events spanning "
            f"{timeline[0]['timestamp'] if timeline else 'n/a'} to {timeline[-1]['timestamp'] if timeline else 'n/a'}."
        )
        result = gateway.generate_json(LLMTask.POSTMORTEM, _POSTMORTEM_SYSTEM_PROMPT, user_prompt)
        if result.get("summary"):
            return result
    except OllamaUnavailableError:
        logger.warning("ReportingAgent: Ollama unavailable, using templated postmortem prose.")

    if top and top.status != "insufficient_evidence":
        summary = (
            f"On {incident.started_at.isoformat()}, {incident.service} experienced a {incident.severity} incident "
            f"({incident.title}). The leading hypothesis, with a heuristic confidence ranking of "
            f"{top.confidence*100:.0f}%, is: {top.statement}"
        )
        contributing = (
            "See supporting and contradicting evidence IDs on each hypothesis below for the basis of this ranking. "
            "Confidence values are an evidence-weighting heuristic, not a certainty claim."
        )
    else:
        summary = (
            f"On {incident.started_at.isoformat()}, {incident.service} experienced a {incident.severity} incident "
            f"({incident.title}). Automated investigation did not find sufficient evidence to determine a root cause."
        )
        contributing = "No contributing factors could be confidently attributed given the available evidence."

    return {"summary": summary, "contributing_factors": contributing}


def build_postmortem(incident, hypotheses: List, evidence_rows: List, timeline: List[dict], actions: List) -> str:
    prose = _llm_prose(incident, hypotheses, timeline)
    top = hypotheses[0] if hypotheses else None
    insufficient = (not top) or top.status == "insufficient_evidence"

    lines = [
        f"# Postmortem: {incident.title}",
        "",
        f"- **Incident ID:** {incident.incident_id}",
        f"- **Service:** {incident.service}",
        f"- **Environment:** {incident.environment}",
        f"- **Severity:** {incident.severity}",
        f"- **Status:** {incident.status}",
        f"- **Started:** {incident.started_at.isoformat()}",
        f"- **Ended:** {incident.ended_at.isoformat() if incident.ended_at else 'ongoing / unknown'}",
        "",
        "## Summary",
        prose.get("summary", ""),
        "",
        "## Impact",
        f"Affected components: {', '.join(incident.affected_components) if incident.affected_components else 'not specified'}. "
        f"See Timeline and Evidence sections for observed error-rate/latency impact.",
        "",
        "## Timeline",
    ]
    for entry in timeline:
        lines.append(f"- `{entry['timestamp']}` **[{entry['source']}/{entry['type']}]** {entry['summary']}")

    lines += ["", "## Root Cause"]
    if insufficient:
        lines.append(
            "**Insufficient evidence to declare a definitive root cause.** "
            "The investigation pipeline explicitly does not guess when evidence does not support a conclusion. "
            "See Evidence section for what was collected."
        )
    else:
        lines.append(f"**Leading hypothesis** (confidence heuristic {top.confidence*100:.0f}%): {top.statement}")
        lines.append("")
        lines.append(f"Rationale: {top.rationale}")
        lines.append("")
        lines.append(f"Supporting evidence: {', '.join(top.supporting_evidence) if top.supporting_evidence else 'none'}")
        lines.append(f"Contradicting evidence: {', '.join(top.contradicting_evidence) if top.contradicting_evidence else 'none'}")

    lines += ["", "## Contributing Factors", prose.get("contributing_factors", "")]

    if len(hypotheses) > 1:
        lines += ["", "### Other hypotheses considered"]
        for h in hypotheses[1:]:
            lines.append(f"- ({h.confidence*100:.0f}%) {h.statement} "
                         f"[supports: {', '.join(h.supporting_evidence) or 'none'}; "
                         f"contradicts: {', '.join(h.contradicting_evidence) or 'none'}]")

    lines += [
        "",
        "## Detection",
        "Detected via automated metric/log anomaly evidence collected by the investigation pipeline "
        "(see Evidence section, source=metrics/logs/kubernetes).",
        "",
        "## Resolution",
    ]
    rollback_evidence = [e for e in evidence_rows if e.type == "rollback"]
    if rollback_evidence:
        lines.append(f"Resolved via rollback: {rollback_evidence[0].summary}")
    else:
        lines.append("Resolution steps not yet recorded. See Recommended Corrective Actions below.")

    lines += ["", "## Corrective Actions", "", "| # | Action | Owner | Status |", "|---|--------|-------|--------|"]
    if actions:
        for i, a in enumerate(actions, 1):
            lines.append(f"| {i} | {a.description} | TBD | {a.status} |")
    else:
        lines.append("| 1 | No corrective actions recorded yet. | TBD | pending |")

    lines += [
        "",
        "---",
        "_This postmortem was drafted automatically by the AI Root Cause Investigator MVP. "
        "All confidence scores are evidence-ranking heuristics, not certainty. Review before sharing._",
    ]

    return "\n".join(lines)
