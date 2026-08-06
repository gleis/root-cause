"""
Loki client for LogAgent. Real mode issues LogQL queries over HTTP; DEMO_MODE
returns the canned OOMKilled-scenario log fixture.
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from ..config import get_settings

logger = logging.getLogger("rootcause.integrations.loki")

_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "logs.json")


class LokiClient:
    def __init__(self, environment: Optional[str] = None):
        """`environment` (e.g. "production"/"staging") selects the
        environment-scoped LOKI_URL_<ENV> override if one is configured
        (see Settings.loki_url_for), so a staging investigation can never
        accidentally query production Loki. Falls back to the global
        LOKI_URL when no environment is given or no override exists for it."""
        self.settings = get_settings()
        self.environment = environment
        self.loki_url = self.settings.loki_url_for(environment) if environment else self.settings.loki_url

    def query_range(self, logql: str, start: datetime, end: datetime, limit: int = 1000) -> dict:
        params = {
            "query": logql,
            "start": int(start.timestamp() * 1e9),
            "end": int(end.timestamp() * 1e9),
            "limit": limit,
        }
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(f"{self.loki_url}/loki/api/v1/query_range", params=params)
            resp.raise_for_status()
            return resp.json()

    def get_incident_window_logs(self, service: str, namespace: str, start: datetime, end: Optional[datetime]) -> dict:
        """Returns normalized log entries for the incident window, live or fixture."""
        if self.settings.demo_mode:
            return self._demo_logs(start)

        end = end or (start + timedelta(hours=1))
        logql = f'{{service="{service}", namespace="{namespace}"}} |= "error"'
        try:
            raw = self.query_range(logql, start - timedelta(minutes=15), end)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Loki query failed: %s", exc)
            raw = {}
        return {"mode": "live", "service": service, "namespace": namespace, "raw": raw}

    @staticmethod
    def _demo_logs(incident_start: datetime) -> dict:
        with open(_FIXTURE_PATH) as f:
            fixture = json.load(f)

        entries = []
        for e in fixture["entries"]:
            ts = incident_start + timedelta(minutes=e["offset_minutes"])
            entries.append({**e, "timestamp": ts.isoformat()})

        return {
            "mode": "demo",
            "scenario": fixture["scenario"],
            "entries": entries,
        }
