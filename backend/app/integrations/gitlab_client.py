"""
GitLab client for ChangeAgent (spec requires GitLab, not GitHub). Real mode
uses python-gitlab to pull commits / merge requests / pipelines / deployments
for the configured project in the incident time window. DEMO_MODE returns the
canned fixture showing the api-v2.14.3 worker-concurrency change.
"""
import json
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from ..config import get_settings

logger = logging.getLogger("rootcause.integrations.gitlab_client")

_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "gitlab_events.json")


class GitLabClient:
    def __init__(self, environment: Optional[str] = None):
        """`environment` selects the environment-scoped
        GITLAB_PROJECT_ID_<ENV> override if configured
        (Settings.gitlab_project_id_for), so a staging investigation looks
        at the staging project/branch history rather than assuming
        production's project id."""
        self.settings = get_settings()
        self.environment = environment
        self._gl = None

    def _get_client(self):
        if self._gl is not None:
            return self._gl
        import gitlab  # local import: optional dependency at import time

        from ..secrets import get_vault_client
        token = get_vault_client().get_gitlab_token()
        self._gl = gitlab.Gitlab(self.settings.gitlab_url, private_token=token)
        return self._gl

    def get_incident_window_changes(self, project_id: Optional[str], start: datetime, end: Optional[datetime]) -> dict:
        """
        Returns commits, merge requests, pipelines, and deployments for the
        project in the window leading up to (and slightly after, to catch a
        rollback) the incident start.
        """
        if self.settings.demo_mode:
            return self._demo_changes(start)

        end = end or (start + timedelta(hours=1))
        window_start = start - timedelta(hours=6)
        project_id = project_id or (
            self.settings.gitlab_project_id_for(self.environment) if self.environment else self.settings.gitlab_project_id
        )
        gl = self._get_client()
        try:
            project = gl.projects.get(project_id)
            commits = project.commits.list(since=window_start.isoformat(), until=end.isoformat(), all=True)
            mrs = project.mergerequests.list(updated_after=window_start.isoformat(), state="merged", all=True)
            pipelines = project.pipelines.list(updated_after=window_start.isoformat(), all=True)
            deployments = project.deployments.list(all=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("GitLab API call failed: %s", exc)
            return {"mode": "live", "error": str(exc), "commits": [], "merge_requests": [], "pipelines": [], "deployments": []}

        return {
            "mode": "live",
            "commits": [{"sha": c.short_id, "title": c.title, "created_at": c.created_at, "author": c.author_name} for c in commits],
            "merge_requests": [{"iid": m.iid, "title": m.title, "merged_at": getattr(m, "merged_at", None)} for m in mrs],
            "pipelines": [{"id": p.id, "status": p.status, "created_at": p.created_at} for p in pipelines],
            "deployments": [{"id": d.id, "ref": d.ref, "status": d.status, "created_at": d.created_at} for d in deployments],
        }

    @staticmethod
    def _demo_changes(incident_start: datetime) -> dict:
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
            "commits": _shift(fixture["commits"]),
            "merge_requests": _shift(fixture["merge_requests"]),
            "pipelines": _shift(fixture["pipelines"]),
            "deployments": _shift(fixture["deployments"]),
        }
