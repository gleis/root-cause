"""
Tests for app/agents/scoring.py -- the deterministic confidence-scoring
math. No DB, no LLM, no network -- pure function tests.
"""
from datetime import datetime, timedelta, timezone

from app.agents.scoring import EvidenceLike, WEIGHTS, score_hypothesis


def _ev(source, type_, timestamp, relevance=0.9, evidence_id="e1"):
    return EvidenceLike(evidence_id=evidence_id, source=source, type=type_, timestamp=timestamp, relevance_score=relevance)


def test_weights_sum_to_one():
    assert abs(sum(WEIGHTS.values()) - 1.0) < 1e-9


def test_no_supporting_evidence_yields_zero_score():
    started = datetime.now(timezone.utc)
    breakdown = score_hypothesis([], [], started, [])
    assert breakdown.weighted_total() == 0.0


def test_strong_evidence_close_to_incident_scores_high():
    started = datetime.now(timezone.utc)
    supporting = [
        _ev("metrics", "metric_anomaly", started + timedelta(minutes=2), relevance=0.95, evidence_id="m1"),
        _ev("logs", "log_cluster", started + timedelta(minutes=3), relevance=0.9, evidence_id="l1"),
        _ev("kubernetes", "oom_killed", started + timedelta(minutes=5), relevance=0.95, evidence_id="k1"),
        _ev("gitlab", "deployment", started - timedelta(hours=1), relevance=0.9, evidence_id="g1"),
    ]
    breakdown = score_hypothesis(supporting, [], started, supporting)
    total = breakdown.weighted_total()
    assert 0.5 < total <= 1.0
    assert breakdown.temporal_correlation > 0.6
    assert breakdown.metric_correlation > 0.0
    assert breakdown.log_evidence > 0.0
    assert breakdown.change_correlation > 0.0


def test_evidence_far_from_incident_scores_low_temporal_correlation():
    started = datetime.now(timezone.utc)
    supporting = [_ev("metrics", "metric_anomaly", started + timedelta(hours=5), relevance=0.9, evidence_id="m1")]
    breakdown = score_hypothesis(supporting, [], started, supporting)
    assert breakdown.temporal_correlation == 0.0


def test_rollback_recovery_boosts_score_when_in_supporting_set():
    started = datetime.now(timezone.utc)
    rollback = _ev("gitlab", "rollback", started + timedelta(minutes=20), relevance=0.9, evidence_id="r1")
    supporting = [rollback]
    breakdown = score_hypothesis(supporting, [], started, [rollback])
    assert breakdown.recovery_after_rollback == 1.0


def test_score_is_deterministic_across_calls():
    started = datetime.now(timezone.utc)
    supporting = [_ev("metrics", "metric_anomaly", started + timedelta(minutes=1), evidence_id="m1")]
    b1 = score_hypothesis(supporting, [], started, supporting)
    b2 = score_hypothesis(supporting, [], started, supporting)
    assert b1.weighted_total() == b2.weighted_total()
    assert b1.as_dict() == b2.as_dict()


def test_weighted_total_is_clamped_between_zero_and_one():
    started = datetime.now(timezone.utc)
    # Pile on many high-relevance supporting items across every source to
    # try to push the raw weighted sum past 1.0 -- weighted_total() must
    # still clamp to [0, 1].
    supporting = [
        _ev("metrics", "metric_anomaly", started, relevance=1.0, evidence_id=f"m{i}") for i in range(5)
    ] + [
        _ev("logs", "log_cluster", started, relevance=1.0, evidence_id=f"l{i}") for i in range(5)
    ]
    breakdown = score_hypothesis(supporting, [], started, supporting, historical_matches=10)
    total = breakdown.weighted_total()
    assert 0.0 <= total <= 1.0
