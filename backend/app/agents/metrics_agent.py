"""
MetricsAgent: queries Prometheus for CPU/memory/error-rate/latency around the
incident window, compares each sample to the baseline window, and flags
anomalies as Evidence. Purely deterministic/statistical -- no LLM call here.
"""
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any

from ..integrations.prometheus import PrometheusClient

logger = logging.getLogger("rootcause.agents.metrics")

# Simple anomaly thresholds: flag a sample as anomalous if it deviates from
# baseline by more than this many percentage points / relative multiplier.
_MEMORY_DELTA_PCT_THRESHOLD = 15
_CPU_DELTA_PCT_THRESHOLD = 15
_ERROR_RATE_MULTIPLIER_THRESHOLD = 5
_LATENCY_MULTIPLIER_THRESHOLD = 2


def run(incident) -> List[Dict[str, Any]]:
    client = PrometheusClient(environment=incident.environment)
    data = client.get_incident_window_metrics(incident.service, incident.started_at, incident.ended_at)

    evidence: List[Dict[str, Any]] = []

    if data.get("mode") == "demo":
        baseline = data["baseline"]
        baseline_comparison = data.get("baseline_comparison", {})
        for point in data["series"]:
            ts = datetime.fromisoformat(point["timestamp"])
            anomalies = []

            mem_delta = point["memory_utilization_pct"] - baseline["memory_utilization_pct"]
            if mem_delta >= _MEMORY_DELTA_PCT_THRESHOLD:
                anomalies.append(f"memory {point['memory_utilization_pct']}% (baseline {baseline['memory_utilization_pct']}%, +{mem_delta:.0f}pp)")

            cpu_delta = point["cpu_utilization_pct"] - baseline["cpu_utilization_pct"]
            if cpu_delta >= _CPU_DELTA_PCT_THRESHOLD:
                anomalies.append(f"cpu {point['cpu_utilization_pct']}% (baseline {baseline['cpu_utilization_pct']}%, +{cpu_delta:.0f}pp)")

            if baseline["error_rate_pct"] > 0 and point["error_rate_pct"] >= baseline["error_rate_pct"] * _ERROR_RATE_MULTIPLIER_THRESHOLD:
                anomalies.append(f"error rate {point['error_rate_pct']}% (baseline {baseline['error_rate_pct']}%)")

            if baseline["p99_latency_ms"] > 0 and point["p99_latency_ms"] >= baseline["p99_latency_ms"] * _LATENCY_MULTIPLIER_THRESHOLD:
                anomalies.append(f"p99 latency {point['p99_latency_ms']}ms (baseline {baseline['p99_latency_ms']}ms)")

            if anomalies:
                severity = "high" if len(anomalies) >= 2 else "medium"
                relevance = 0.9 if len(anomalies) >= 2 else 0.6
                evidence.append({
                    "source": "metrics",
                    "type": "metric_anomaly",
                    "timestamp": ts,
                    "summary": f"Anomalous metrics detected ({severity}): " + "; ".join(anomalies) + (f" [{point['note']}]" if "note" in point else ""),
                    "raw_reference": {
                        "sample": point,
                        "baseline": baseline,
                        # Structured deviation-from-baseline data (previous
                        # hour / previous day / previous week), computed by
                        # PrometheusClient via PromQL offset modifiers in
                        # live mode. scoring.py's metric_correlation factor
                        # can weight on this instead of/in addition to the
                        # raw immediate-baseline delta above.
                        "baseline_comparison": baseline_comparison,
                    },
                    "relevance_score": relevance,
                })

        if not evidence:
            evidence.append({
                "source": "metrics",
                "type": "no_anomaly",
                "timestamp": incident.started_at,
                "summary": "No metric anomalies detected relative to the 1-hour baseline window.",
                "raw_reference": {"baseline": baseline, "baseline_comparison": baseline_comparison},
                "relevance_score": 0.1,
            })
        else:
            evidence.append(_baseline_comparison_evidence(incident, baseline_comparison))
        return evidence

    # Live mode: Prometheus range-vector series come back in raw API shape.
    # A full anomaly detector against arbitrary PromQL matrices is future
    # work; for now we surface a single evidence item noting the query was
    # attempted, so LogAgent/ChangeAgent/InfraAgent evidence isn't silently
    # missing a metrics counterpart when running against real infra.
    baseline_comparison = data.get("baseline_comparison", {})
    evidence.append({
        "source": "metrics",
        "type": "metric_query_result",
        "timestamp": incident.started_at,
        "summary": f"Queried live Prometheus at {incident.started_at.isoformat()} for service={incident.service}; "
                   f"see raw_reference for series data. Automated anomaly scoring against live series is limited in this MVP.",
        "raw_reference": {"series_keys": list(data.get("series", {}).keys()), "baseline_comparison": baseline_comparison},
        "relevance_score": 0.3,
    })
    if baseline_comparison:
        evidence.append(_baseline_comparison_evidence(incident, baseline_comparison))
    return evidence


def _baseline_comparison_evidence(incident, baseline_comparison: Dict[str, Any]) -> Dict[str, Any]:
    """
    Builds a single summary Evidence item describing % deviation from each
    of the three baseline windows (previous hour / previous day / previous
    week), across whichever metrics deviate meaningfully (>=15pp/15%) from
    at least one baseline. Kept separate from the anomaly-threshold evidence
    above so it always appears once per run (giving the report/hypothesis
    layer a single place to cite "vs. historical baseline" evidence) rather
    than being duplicated per anomalous sample.
    """
    notable = []
    for metric_name, comp in baseline_comparison.items():
        for window_name, b in comp.get("baselines", {}).items():
            dev = b.get("pct_deviation")
            if dev is not None and abs(dev) >= 15:
                notable.append(f"{metric_name} vs {window_name.replace('_', ' ')}: {dev:+.1f}%")

    if notable:
        summary = "Deviation from historical baselines: " + "; ".join(notable)
        relevance = 0.7
    else:
        summary = "Metrics are within +/-15% of previous-hour/previous-day/previous-week baselines."
        relevance = 0.2

    return {
        "source": "metrics",
        "type": "baseline_comparison",
        "timestamp": incident.started_at,
        "summary": summary,
        "raw_reference": {"baseline_comparison": baseline_comparison},
        "relevance_score": relevance,
    }
