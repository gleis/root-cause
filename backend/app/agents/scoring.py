"""
Deterministic hypothesis confidence scoring (spec section 6).

The LLM (HypothesisAgent) is only ever used to produce the natural-language
statement/rationale for a candidate hypothesis. The numeric confidence score
is always computed here, in plain Python, from the structured Evidence
objects -- never left to the model to "estimate". This keeps confidence
auditable and reproducible.

Weighted formula:
    temporal_correlation   25%
    metric_correlation     20%
    log_evidence            20%
    change_correlation      15%
    recovery_after_rollback 10%
    historical_match        10%

Each factor is computed as a 0..1 sub-score from the evidence set associated
with a hypothesis; the final confidence is the weighted sum, also 0..1.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, List, Optional

WEIGHTS = {
    "temporal_correlation": 0.25,
    "metric_correlation": 0.20,
    "log_evidence": 0.20,
    "change_correlation": 0.15,
    "recovery_after_rollback": 0.10,
    "historical_match": 0.10,
}

assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


@dataclass
class EvidenceLike:
    """Minimal structural shape scoring needs -- works against both ORM rows
    and the lightweight in-pipeline evidence dataclass."""
    evidence_id: str
    source: str
    type: str
    timestamp: datetime
    relevance_score: float


@dataclass
class ScoreBreakdown:
    temporal_correlation: float = 0.0
    metric_correlation: float = 0.0
    log_evidence: float = 0.0
    change_correlation: float = 0.0
    recovery_after_rollback: float = 0.0
    historical_match: float = 0.0

    def weighted_total(self) -> float:
        total = (
            self.temporal_correlation * WEIGHTS["temporal_correlation"]
            + self.metric_correlation * WEIGHTS["metric_correlation"]
            + self.log_evidence * WEIGHTS["log_evidence"]
            + self.change_correlation * WEIGHTS["change_correlation"]
            + self.recovery_after_rollback * WEIGHTS["recovery_after_rollback"]
            + self.historical_match * WEIGHTS["historical_match"]
        )
        return round(min(max(total, 0.0), 1.0), 4)

    def as_dict(self) -> dict:
        d = {
            "temporal_correlation": round(self.temporal_correlation, 4),
            "metric_correlation": round(self.metric_correlation, 4),
            "log_evidence": round(self.log_evidence, 4),
            "change_correlation": round(self.change_correlation, 4),
            "recovery_after_rollback": round(self.recovery_after_rollback, 4),
            "historical_match": round(self.historical_match, 4),
            "weights": WEIGHTS,
        }
        d["weighted_total"] = self.weighted_total()
        return d


def _closeness(delta_seconds: float, tolerance_seconds: float) -> float:
    """1.0 when delta is 0, decaying linearly to 0.0 at tolerance_seconds."""
    if tolerance_seconds <= 0:
        return 0.0
    score = 1.0 - (abs(delta_seconds) / tolerance_seconds)
    return max(0.0, min(1.0, score))


def score_hypothesis(
    supporting_evidence: List[EvidenceLike],
    contradicting_evidence: List[EvidenceLike],
    incident_started_at: datetime,
    all_evidence: List[EvidenceLike],
    historical_matches: int = 0,
) -> ScoreBreakdown:
    """
    Computes each weighted-formula sub-score from 0..1 based purely on the
    structured evidence -- no LLM involvement.
    """
    breakdown = ScoreBreakdown()
    if not supporting_evidence:
        return breakdown  # no evidence -> all-zero score, forces "insufficient evidence" path upstream

    # --- temporal_correlation: how close, on average, supporting evidence is
    # to the incident start (within a 30 minute tolerance window).
    deltas = [
        (e.timestamp - incident_started_at).total_seconds() for e in supporting_evidence
    ]
    closeness_scores = [_closeness(d, tolerance_seconds=30 * 60) for d in deltas]
    breakdown.temporal_correlation = sum(closeness_scores) / len(closeness_scores)

    # --- metric_correlation: fraction of supporting evidence sourced from
    # metrics, weighted by their own relevance_score.
    metric_evi = [e for e in supporting_evidence if e.source == "metrics"]
    if metric_evi:
        breakdown.metric_correlation = sum(e.relevance_score for e in metric_evi) / len(metric_evi)

    # --- log_evidence: fraction of supporting evidence sourced from logs,
    # weighted by their own relevance_score.
    log_evi = [e for e in supporting_evidence if e.source == "logs"]
    if log_evi:
        breakdown.log_evidence = sum(e.relevance_score for e in log_evi) / len(log_evi)

    # --- change_correlation: any supporting evidence from gitlab (deploy /
    # commit / MR) shortly before the incident boosts this strongly.
    change_evi = [e for e in supporting_evidence if e.source == "gitlab"]
    if change_evi:
        change_deltas = [(incident_started_at - e.timestamp).total_seconds() for e in change_evi]
        # a change in the 6 hours before the incident scores highly
        change_scores = [_closeness(d - 0, tolerance_seconds=6 * 3600) for d in change_deltas if d >= 0]
        breakdown.change_correlation = max(change_scores) if change_scores else 0.3

    # --- recovery_after_rollback: did a rollback-type deployment evidence
    # appear, followed by metric/log evidence indicating recovery, anywhere
    # in the full evidence set (not just this hypothesis's supporting set)?
    rollback_events = [
        e for e in all_evidence
        if e.source == "gitlab" and "rollback" in (getattr(e, "type", "") or "").lower()
    ]
    if not rollback_events:
        # fall back to duck-typed summary sniffing handled by caller via type field
        rollback_events = [e for e in all_evidence if getattr(e, "type", "") == "rollback"]
    if rollback_events:
        breakdown.recovery_after_rollback = 1.0 if any(
            e in supporting_evidence for e in rollback_events
        ) else 0.5

    # --- historical_match: heuristic count of similar past incidents (MVP
    # has no historical incident DB / vector search yet -- this is a
    # deliberate stub, see README "out of scope"). 0 unless explicitly passed.
    breakdown.historical_match = min(historical_matches / 3.0, 1.0)

    return breakdown
