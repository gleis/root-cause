"""
Kubernetes client for InfrastructureAgent. Real mode uses the official
`kubernetes` python client (reads events, pod statuses, node conditions).
DEMO_MODE returns the canned OOMKilled fixture (12 pods OOMKilled, node
MemoryPressure).

IMPORTANT: this client is READ-ONLY. It only ever calls list_namespaced_*/
list_*_for_all_namespaces style read APIs. See app/safety.py for the
allowlist that governs any future shell-command diagnostics; this module
doesn't execute shell commands at all, it goes through the k8s API directly.
"""
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from ..config import get_settings

logger = logging.getLogger("rootcause.integrations.kubernetes_client")

_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "k8s_events.json")


class KubernetesClient:
    def __init__(self, environment: Optional[str] = None):
        """`environment` selects the environment-scoped KUBECONFIG_<ENV> /
        K8S_NAMESPACE_<ENV> overrides if configured (Settings.kubeconfig_for
        / k8s_namespace_for), so this client talks to the cluster/namespace
        matching the incident's environment rather than one global
        cluster/namespace."""
        self.settings = get_settings()
        self.environment = environment
        self.kubeconfig = self.settings.kubeconfig_for(environment) if environment else self.settings.kubeconfig
        self._api = None

    def _get_api(self):
        """Lazily loads the kubernetes client + kubeconfig only when needed,
        so importing this module never requires a kubeconfig to be present
        (important for DEMO_MODE / syntax checks)."""
        if self._api is not None:
            return self._api
        from kubernetes import client, config  # local import: optional dependency at import time

        try:
            config.load_kube_config(config_file=os.path.expanduser(self.kubeconfig))
        except Exception:
            config.load_incluster_config()
        self._api = client.CoreV1Api()
        return self._api

    def get_incident_window_events(self, namespace: str, start: datetime, end: Optional[datetime]) -> dict:
        if self.settings.demo_mode:
            return self._demo_events(start)

        end = end or (start + timedelta(hours=1))
        api = self._get_api()
        try:
            events = api.list_namespaced_event(namespace=namespace).items
            pods = api.list_namespaced_pod(namespace=namespace).items
            nodes = api.list_node().items
        except Exception as exc:  # noqa: BLE001
            logger.warning("Kubernetes API call failed: %s", exc)
            return {"mode": "live", "namespace": namespace, "pod_events": [], "node_conditions": [], "error": str(exc)}

        pod_events = [
            {
                "timestamp": e.last_timestamp.isoformat() if e.last_timestamp else None,
                "pod": e.involved_object.name,
                "reason": e.reason,
                "message": e.message,
            }
            for e in events
            if start <= (e.last_timestamp or start) <= end
        ]
        node_conditions = [
            {"node": n.metadata.name, "condition": c.type, "status": c.status}
            for n in nodes
            for c in (n.status.conditions or [])
            if c.type in ("MemoryPressure", "DiskPressure", "PIDPressure", "Ready")
        ]
        return {"mode": "live", "namespace": namespace, "pod_events": pod_events, "node_conditions": node_conditions}

    @staticmethod
    def _demo_events(incident_start: datetime) -> dict:
        with open(_FIXTURE_PATH) as f:
            fixture = json.load(f)

        def _shift(items):
            out = []
            for item in items:
                ts = incident_start + timedelta(minutes=item["offset_minutes"])
                out.append({**item, "timestamp": ts.isoformat()})
            return out

        return {
            "mode": "demo",
            "scenario": fixture["scenario"],
            "namespace": fixture["namespace"],
            "pod_events": _shift(fixture["pod_events"]),
            "node_conditions": _shift(fixture["node_conditions"]),
            "restarts": _shift(fixture["restarts"]),
        }
