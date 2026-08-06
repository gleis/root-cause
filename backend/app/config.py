"""
Central configuration, loaded from environment variables (.env supported via
pydantic-settings). See ../../.env.example for the full list of variables.

Design note on background tasks: the spec allows either Celery+Redis or a
lightweight asyncio background-task approach. This MVP uses FastAPI's
built-in BackgroundTasks / asyncio.create_task for running the agent
pipeline, because it removes an operational dependency (a running Celery
worker) that isn't needed for a single-incident-at-a-time MVP demo. Redis and
Celery are still wired up in docker-compose.yml and requirements.txt so the
system can be trivially upgraded to a real task queue for production/
multi-tenant use -- swapping `asyncio.create_task(...)` for `task.delay(...)`
in coordinator.py is the only change required.
"""
import os
from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings

# .env can live at the repo root (convenient alongside docker-compose.yml,
# which reads repo-root .env for variable substitution) or in backend/
# (convenient when running `python scripts/run_demo.py` or `uvicorn` from
# inside backend/). pydantic-settings' env_file is resolved relative to the
# process's current working directory by default, which caused real
# confusion (a .env at repo root was silently ignored when running from
# backend/). Resolve both candidate locations explicitly, relative to this
# file, so it works regardless of cwd. Later paths override earlier ones for
# any keys they both set.
_APP_DIR = Path(__file__).resolve().parent      # backend/app
_BACKEND_DIR = _APP_DIR.parent                  # backend
_REPO_ROOT = _BACKEND_DIR.parent                # repo root
_ENV_FILES = tuple(
    str(p) for p in (_REPO_ROOT / ".env", _BACKEND_DIR / ".env") if p.exists()
) or (str(_BACKEND_DIR / ".env"),)  # keep a default even if neither exists yet


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg2://rootcause:rootcause@localhost:5432/rootcause"
    redis_url: str = "redis://localhost:6379/0"

    ollama_base_url: str = "http://localhost:11434"
    ollama_model_summary: str = "qwen2.5"
    ollama_model_redaction: str = "qwen2.5"
    ollama_model_classification: str = "qwen2.5"
    ollama_model_hypothesis: str = "qwen2.5"
    ollama_model_postmortem: str = "qwen2.5"

    demo_mode: bool = True

    gitlab_url: str = "https://gitlab.com"
    gitlab_token: str = "changeme"
    gitlab_project_id: str = "changeme"

    prometheus_url: str = "http://localhost:9090"
    loki_url: str = "http://localhost:3100"

    # ---- Per-environment integration URLs (spec item 7: environment
    # restriction enforcement) ----
    # An Incident carries an `environment` field (e.g. "production",
    # "staging"). Integration clients should be constructed per-request from
    # the URL scoped to *that* incident's environment, not one global URL,
    # so a staging investigation can never accidentally query production
    # Prometheus/Loki/GitLab/K8s (or vice versa). These are optional
    # overrides keyed by environment name; `prometheus_url`/`loki_url` above
    # remain the fallback for any environment without a specific override
    # (and for DEMO_MODE, where nothing is actually called).
    prometheus_url_production: str | None = None
    prometheus_url_staging: str | None = None
    loki_url_production: str | None = None
    loki_url_staging: str | None = None
    kubeconfig_production: str | None = None
    kubeconfig_staging: str | None = None
    k8s_namespace_production: str | None = None
    k8s_namespace_staging: str | None = None
    gitlab_project_id_production: str | None = None
    gitlab_project_id_staging: str | None = None

    def prometheus_url_for(self, environment: str) -> str:
        return getattr(self, f"prometheus_url_{(environment or '').lower()}", None) or self.prometheus_url

    def loki_url_for(self, environment: str) -> str:
        return getattr(self, f"loki_url_{(environment or '').lower()}", None) or self.loki_url

    def kubeconfig_for(self, environment: str) -> str:
        return getattr(self, f"kubeconfig_{(environment or '').lower()}", None) or self.kubeconfig

    def k8s_namespace_for(self, environment: str) -> str:
        return getattr(self, f"k8s_namespace_{(environment or '').lower()}", None) or self.k8s_namespace

    def gitlab_project_id_for(self, environment: str) -> str:
        return getattr(self, f"gitlab_project_id_{(environment or '').lower()}", None) or self.gitlab_project_id

    # KUBECONFIG respects the standard env var of the same name if set (e.g.
    # `KUBECONFIG=/path/to/config` in the shell/`.env`); this field is the
    # pydantic-settings-managed default/override for it. k8s_namespace
    # defaults to "demo" to match infra/k3d/sample-app/k8s/namespace.yaml --
    # override via K8S_NAMESPACE if your real cluster uses something else.
    kubeconfig: str = "~/.kube/config"
    k8s_namespace: str = "demo"

    slack_bot_token: str = "xoxb-changeme"
    slack_channel: str = "#incidents"
    slack_signing_secret: str = "changeme"

    # Alertmanager webhook intake (POST /api/v1/alerts/webhook, see
    # app/routers/alerts.py). No outbound calls are made using this --
    # it's here purely for documentation/reference of the expected source.
    alertmanager_url: str = "http://localhost:9093"

    # ---- Auth: OIDC (human/UI users) ----
    # Validated per-request against the IdP's JWKS; no fake/local IdP is
    # bundled -- see docs/auth-setup.md for registering a real IdP (worked
    # example: Keycloak; notes for Okta/Azure AD).
    oidc_issuer_url: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""  # if blank, derived as f"{oidc_issuer_url}/.well-known/jwks.json" style discovery is NOT auto-attempted -- set explicitly
    oidc_roles_claim: str = "roles"
    # JWKS keys are cached for this many seconds between refetches.
    oidc_jwks_cache_seconds: int = 3600

    # ---- Auth: API keys (service-to-service: Alertmanager webhook, CI) ----
    api_key_header: str = "X-API-Key"  # also accepted via Authorization: Bearer <key>

    # ---- Auth: dev/demo escape hatch ----
    # When true (DEMO_MODE's default posture), unauthenticated requests are
    # treated as an implicit admin actor so the existing demo/docker-compose
    # flows keep working without standing up an IdP or minting API keys.
    # MUST be false in any real deployment -- this is what "fails closed in
    # prod" depends on. auth.py refuses to honor this flag unless
    # demo_mode is also true, so flipping this alone in a prod .env without
    # also setting DEMO_MODE=true does nothing.
    auth_allow_anonymous_in_demo: bool = True

    # ---- Vault (secrets management) ----
    # If VAULT_ADDR is unset, secrets.py falls back to the plain env vars
    # above (gitlab_token, slack_bot_token, etc) and logs a warning -- see
    # docs/vault-setup.md.
    vault_addr: str = ""
    vault_token: str = ""          # static token auth (dev) -- prefer AppRole/K8s auth in prod, see docs/vault-setup.md
    vault_role_id: str = ""
    vault_secret_id: str = ""
    vault_kv_mount: str = "secret"
    vault_kv_path: str = "rootcause"
    vault_database_mount: str = "database"
    vault_database_role: str = "rootcause-app"
    vault_secret_cache_seconds: int = 300

    # ---- Data retention ----
    evidence_retention_days: int = 90

    class Config:
        env_file = _ENV_FILES
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()
