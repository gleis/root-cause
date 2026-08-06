"""
Prometheus client for MetricsAgent. Real mode issues PromQL range queries
over HTTP; DEMO_MODE returns the canned OOMKilled-scenario fixture so the
whole pipeline can run without a live Prometheus.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any

import httpx

from ..config import get_settings

logger = logging.getLogger("rootcause.integrations.prometheus")

_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "metrics.json")


class PrometheusClient:
    # PromQL base templates for the 4 metrics MetricsAgent cares about. Each
    # has a `{rate_window}` slot where an `offset` modifier is injected to
    # query the same metric shifted back in time (spec section on baseline
    # comparison: previous hour / previous day / previous week, same
    # time-of-day) -- e.g. `rate(x[5m] offset 1h)` computes the 1h-ago rate
    # over the same 5m window width, so it's directly comparable to the
    # unshifted "current" value.
    _METRIC_TEMPLATES = {
        "cpu_utilization_pct": 'avg(rate(container_cpu_usage_seconds_total{{service="{service}"}}[5m]{offset})) * 100',
        "memory_utilization_pct": (
            'avg(container_memory_working_set_bytes{{service="{service}"}}{offset}) '
            '/ avg(container_spec_memory_limit_bytes{{service="{service}"}}{offset}) * 100'
        ),
        "error_rate_pct": (
            'sum(rate(http_requests_total{{service="{service}",status=~"5.."}}[5m]{offset})) '
            '/ sum(rate(http_requests_total{{service="{service}"}}[5m]{offset})) * 100'
        ),
        "p99_latency_ms": (
            'histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{{service="{service}"}}[5m]{offset})) '
            'by (le)) * 1000'
        ),
    }

    # Baseline windows compared against the incident window: PromQL offset
    # modifier per window, keyed by the name used throughout Evidence /
    # scoring output.
    BASELINE_OFFSETS = {
        "previous_hour": "1h",
        "previous_day": "1d",
        "previous_week": "7d",
    }

    def __init__(self, environment: Optional[str] = None):
        """`environment` selects the environment-scoped PROMETHEUS_URL_<ENV>
        override if configured (Settings.prometheus_url_for), so a
        staging/production investigation is scoped to the matching
        Prometheus instance rather than one global URL."""
        self.settings = get_settings()
        self.environment = environment
        self.prometheus_url = self.settings.prometheus_url_for(environment) if environment else self.settings.prometheus_url

    def query_range(self, query: str, start: datetime, end: datetime, step: str = "60s") -> dict:
        """Real Prometheus range query. Returns raw Prometheus API JSON."""
        params = {
            "query": query,
            "start": start.timestamp(),
            "end": end.timestamp(),
            "step": step,
        }
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(f"{self.prometheus_url}/api/v1/query_range", params=params)
            resp.raise_for_status()
            return resp.json()

    def query_instant(self, query: str, time: Optional[datetime] = None) -> dict:
        """Real Prometheus instant query (single point in time)."""
        params = {"query": query}
        if time is not None:
            params["time"] = time.timestamp()
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(f"{self.prometheus_url}/api/v1/query", params=params)
            resp.raise_for_status()
            return resp.json()

    @staticmethod
    def _extract_scalar(result_json: dict) -> Optional[float]:
        """Pulls a single float out of a Prometheus instant-query response's
        vector result (our aggregate PromQL templates always resolve to a
        single-series vector), or None if the series had no data."""
        try:
            result = result_json["data"]["result"]
            if not result:
                return None
            return float(result[0]["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            return None

    def get_incident_window_metrics(self, service: str, start: datetime, end: Optional[datetime]) -> dict:
        """
        High-level helper used by MetricsAgent: returns CPU / memory /
        error-rate / p99-latency samples for `service` across the incident
        window, PLUS baseline comparisons against the previous hour,
        previous day (same time), and previous week (same time), computed
        via PromQL `offset` modifiers -- in a normalized shape (independent
        of whether we hit real Prometheus or the fixture).
        """
        if self.settings.demo_mode:
            return self._demo_metrics(start)

        end = end or (start + timedelta(hours=1))
        baseline_start = start - timedelta(hours=1)
        window_metrics = {}
        for metric_name, template in self._METRIC_TEMPLATES.items():
            promql = template.format(service=service, offset="")
            try:
                window_metrics[metric_name] = self.query_range(promql, baseline_start, end)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Prometheus query failed for %s: %s", metric_name, exc)
                window_metrics[metric_name] = None

        baseline_comparison = self._get_baseline_comparison_live(service, start, end)

        return {
            "mode": "live",
            "service": service,
            "series": window_metrics,
            "baseline_comparison": baseline_comparison,
        }

    def _get_baseline_comparison_live(self, service: str, start: datetime, end: datetime) -> dict:
        """
        For each metric, queries the current incident-window average
        alongside the same metric `offset 1h` / `offset 1d` / `offset 7d`
        (evaluated at `end`, so each offset query lands on the equivalent
        point in the prior window), and computes % deviation from each
        baseline. Structured for direct use by scoring.py's
        metric_correlation / temporal_correlation factors.
        """
        comparison: Dict[str, Any] = {}
        for metric_name, template in self._METRIC_TEMPLATES.items():
            current_q = template.format(service=service, offset="")
            try:
                current_val = self._extract_scalar(self.query_instant(current_q, end))
            except Exception as exc:  # noqa: BLE001
                logger.warning("Baseline current-value query failed for %s: %s", metric_name, exc)
                current_val = None

            baselines = {}
            for window_name, offset in self.BASELINE_OFFSETS.items():
                offset_q = template.format(service=service, offset=f" offset {offset}")
                try:
                    baseline_val = self._extract_scalar(self.query_instant(offset_q, end))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Baseline %s query failed for %s: %s", window_name, metric_name, exc)
                    baseline_val = None
                baselines[window_name] = {
                    "value": baseline_val,
                    "pct_deviation": _pct_deviation(current_val, baseline_val),
                }

            comparison[metric_name] = {"current": current_val, "baselines": baselines}
        return comparison

    @staticmethod
    def _demo_metrics(incident_start: datetime) -> dict:
        with open(_FIXTURE_PATH) as f:
            fixture = json.load(f)

        baseline = fixture["baseline_window"]
        baseline_windows = fixture.get("baseline_windows", {})
        series = []
        for point in fixture["series"]:
            ts = incident_start + timedelta(minutes=point["offset_minutes"])
            series.append({**point, "timestamp": ts.isoformat()})

        # Structured baseline_comparison in the same shape live mode
        # produces, computed against the peak-anomaly sample (matches what
        # an SRE would actually want to see: "how far off is the worst
        # point from history"), so DEMO_MODE can exercise/display the same
        # baseline-comparison code path as live mode.
        peak = max(series, key=lambda p: p.get("memory_utilization_pct", 0))
        baseline_comparison = {}
        for metric_name in ("cpu_utilization_pct", "memory_utilization_pct", "error_rate_pct", "p99_latency_ms"):
            current_val = peak.get(metric_name)
            baselines = {}
            for window_name, window_data in baseline_windows.items():
                baseline_val = window_data.get(metric_name)
                baselines[window_name] = {
                    "value": baseline_val,
                    "pct_deviation": _pct_deviation(current_val, baseline_val),
                }
            baseline_comparison[metric_name] = {"current": current_val, "baselines": baselines}

        return {
            "mode": "demo",
            "scenario": fixture["scenario"],
            "baseline": baseline,
            "baseline_windows": baseline_windows,
            "baseline_comparison": baseline_comparison,
            "series": series,
        }


def _pct_deviation(current: Optional[float], baseline: Optional[float]) -> Optional[float]:
    """% deviation of `current` from `baseline`; None if either is missing
    or baseline is 0 (undefined/uninformative)."""
    if current is None or baseline is None or baseline == 0:
        return None
    return round((current - baseline) / baseline * 100, 1)
