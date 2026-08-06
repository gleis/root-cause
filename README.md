# AI Root Cause Investigator

An SRE incident-investigation tool that correlates metrics, logs, deployments, and infrastructure events into a reconstructed timeline, ranked root-cause hypotheses backed by cited evidence, and a draft postmortem — powered entirely by a locally-run LLM (Ollama), with no hosted or cloud model in the loop.

When an incident fires, an engineer typically has to manually stitch together dashboards, logs, deploy history, and `kubectl` output to figure out what happened. This tool automates that gathering and correlation, and drafts a first-pass hypothesis and postmortem for a human to review. It never takes destructive action, and it never asserts a cause it can't back with evidence.

## Features

- **Evidence-driven investigation** — an 8-agent pipeline gathers metrics (Prometheus), logs (Loki), deployment history (GitLab), and infrastructure events (Kubernetes), then correlates them into a single timeline.
- **Ranked hypotheses with citations** — each hypothesis lists the specific evidence IDs supporting it and any contradicting evidence, with a deterministic, weighted confidence score (never presented as certainty).
- **Local LLM only** — every model call goes through a gateway that redacts secrets/PII before the prompt leaves the process and routes to a locally-hosted model (Ollama). There is no hosted-model fallback anywhere in the codebase.
- **Read-only by default** — no diagnostic or remediation command is ever executed automatically. Recommended actions are always text for a human to run.
- **Auto-intake and notifications** — an Alertmanager webhook can open an incident automatically, and a Slack `/investigate` command and posted summaries integrate with incident channels.
- **Auth, audit, and secrets management** — OIDC + API key authentication with role-based access control, HashiCorp Vault-backed secrets, and an audit log of every mutating action.

## Architecture

```
                         ┌─────────────────────┐
  POST /incidents  ───▶  │   CoordinatorAgent   │  scope detection,
  Slack /investigate     │  (async state machine)│  fan-out, status tracking
                         └──────────┬───────────┘
                                    │ fans out to
        ┌───────────────┬──────────┼──────────┬────────────────┐
        ▼                ▼         ▼           ▼                ▼
  MetricsAgent      LogAgent  ChangeAgent  InfraAgent   DependencyAgent
  (Prometheus)      (Loki)    (GitLab)     (Kubernetes)  (heuristics + LLM)
        │                │         │           │                │
        └────────────────┴─────────┴───────────┴────────────────┘
                                    │  Evidence[] (structured, persisted)
                                    ▼
                         ┌─────────────────────┐
                         │   HypothesisAgent    │  deterministic weighted
                         │ (scoring in Python;  │  scoring + local LLM for
                         │  LLM writes prose)   │  statement/rationale only
                         └──────────┬───────────┘
                                    ▼
                         ┌─────────────────────┐
                         │   ReportingAgent     │  timeline merge, Slack
                         │                       │  summary, postmortem
                         └──────────┬───────────┘
                                    ▼
                    REST API (FastAPI) + web frontend
                                    │
                                    ▼
                    Local Ollama (http://localhost:11434)
                    -- the only LLM this system ever calls --
```

A few notable design choices:

- **Durable background execution.** Investigations run as a Celery task backed by Redis, so an in-flight investigation survives an API process restart. If no broker is reachable, it falls back to an in-process background task — useful for local development, and the standalone demo script never needs Redis at all.
- **A plain orchestrator, not a graph framework.** The pipeline is a fixed sequence — fan out to five specialist agents, then hypothesis scoring, then reporting — so a straightforward async function is easier to debug than a general-purpose graph executor.
- **Confidence scoring is deterministic.** The weighted formula (temporal correlation, metric correlation, log evidence, change correlation, recovery-after-rollback, historical match) runs in plain Python. The LLM is only ever used to phrase the natural-language rationale — never to produce the score.

## Quick start

### 1. Install a local LLM

```bash
ollama serve &
ollama pull qwen2.5
```

Any locally-pulled model works. Set `OLLAMA_MODEL_CLASSIFICATION`, `OLLAMA_MODEL_HYPOTHESIS`, `OLLAMA_MODEL_POSTMORTEM`, `OLLAMA_MODEL_SUMMARY`, and `OLLAMA_MODEL_REDACTION` in `.env` to whatever `ollama list` shows (e.g. `gemma3:latest`, `llama3.1:8b`) — each task can use a different model.

### 2. Run the end-to-end demo (no other infrastructure required)

```bash
cd backend
pip install -r requirements.txt
DEMO_MODE=true python scripts/run_demo.py
```

This creates a sample incident and runs the full agent pipeline against fixture data reproducing a canonical scenario — a deployment that raises worker concurrency, causing memory pressure, OOMKilled pods, and a rollback that restores service. It prints the timeline, ranked hypotheses with evidence citations, and a Slack-formatted summary, then writes `backend/demo_postmortem.md`. A throwaway local SQLite database is used, so no Postgres or Docker is required.

### 3. Run the full API and frontend

```bash
docker-compose up --build
```

This starts Postgres, Redis, and the FastAPI backend (`DEMO_MODE=true` by default, still returning fixture evidence but through the real REST API). Open `frontend/index.html` in a browser, or visit `http://localhost:8000/`, which serves it directly.

Create an incident and watch it investigate itself:

```bash
curl -X POST http://localhost:8000/api/v1/incidents \
  -H "Content-Type: application/json" \
  -d '{"title": "Checkout errors spiking", "service": "checkout-api", "severity": "sev1"}'
```

Copy the returned `incident_id` into the frontend and load it — the investigation runs in the background.

### 4. Point it at real infrastructure

Copy `.env.example` to `.env` at the repo root (configuration is also read from `backend/.env` if present, but the repo root is the simplest single source of truth, since `docker-compose.yml` reads from there too). Set `DEMO_MODE=false` and fill in:

| Variable | Purpose |
|---|---|
| `PROMETHEUS_URL` | Prometheus endpoint for metric queries |
| `LOKI_URL` | Loki endpoint for log queries |
| `KUBECONFIG`, `K8S_NAMESPACE` | Cluster access for read-only Kubernetes inspection |
| `GITLAB_URL`, `GITLAB_TOKEN`, `GITLAB_PROJECT_ID` | GitLab project for commit/MR/pipeline/deployment history |
| `SLACK_BOT_TOKEN`, `SLACK_CHANNEL` | Slack workspace for posted summaries and the `/investigate` command |
| `OLLAMA_BASE_URL`, `OLLAMA_MODEL_*` | Local Ollama host/port and per-task model selection |

Every integration client has a real implementation gated by `DEMO_MODE`, so flipping it to `false` switches every agent from fixtures to live calls.

## Live demo on a local Kubernetes cluster

The quick demo above can be extended to a real, disposable Kubernetes cluster so you can see the full loop end to end: an alert fires, Alertmanager posts a webhook, an incident opens automatically, the pipeline gathers real evidence, and a hypothesis, Slack message, and postmortem come out the other side.

All of this lives under `infra/`.

### Prerequisites

- Docker Desktop running, plus `k3d`, `kubectl`, and `helm` on your PATH (`brew install k3d kubectl helm` on macOS).
- Optional, for the GitLab and Slack legs: a GitLab personal access token with `read_api` scope, and a Slack app (see `infra/slack/README.md` for setup — Slack's OAuth flow has to be completed manually in your own workspace).

### Steps

```bash
# 1. Create the cluster
infra/k3d/setup-cluster.sh

# 2. Install Prometheus, Alertmanager, Grafana, and Loki, plus a sample
#    PrometheusRule (error rate > 10%)
infra/k3d/install-observability.sh

# 3. Build and deploy the sample workload
docker build -t customer-api:demo infra/k3d/sample-app
k3d image import customer-api:demo -c rootcause-demo
kubectl apply -f infra/k3d/sample-app/k8s/namespace.yaml
kubectl apply -f infra/k3d/sample-app/k8s/configmap.yaml
kubectl apply -f infra/k3d/sample-app/k8s/deployment.yaml
kubectl apply -f infra/k3d/sample-app/k8s/service.yaml
kubectl apply -f infra/k3d/sample-app/k8s/servicemonitor.yaml
kubectl -n demo rollout status deployment/customer-api

# 4. Port-forward Prometheus/Loki/Alertmanager/Grafana (run in its own
#    terminal -- it blocks)
infra/k3d/port-forward.sh

# 5. In another terminal, configure and start the backend against the
#    live cluster
cp .env.example .env
#   edit .env: DEMO_MODE=false, K8S_NAMESPACE=demo,
#   GITLAB_TOKEN=<your PAT>, GITLAB_PROJECT_ID=<your project>,
#   SLACK_BOT_TOKEN=<xoxb-...>, SLACK_SIGNING_SECRET=<...> (optional)
cd backend
pip install -r ../requirements.txt
uvicorn app.main:app --reload

# 6. Trigger a failure scenario
infra/k3d/scenarios/oomkilled/trigger.sh

# 7. Watch the alert fire (a couple of minutes)
kubectl -n demo get pods -w
#   http://localhost:9090/alerts

# 8. Alertmanager posts to /api/v1/alerts/webhook, which auto-creates an
#    incident and starts the investigation
curl -s http://localhost:8000/api/v1/incidents/<incident_id>/hypotheses | jq
#   or open frontend/index.html and paste in the incident_id

# 9. If Slack is configured, the summary posts to SLACK_CHANNEL once the
#    investigation finishes

# 10. Recover
infra/k3d/scenarios/oomkilled/resolve.sh

# When finished
infra/k3d/teardown.sh
```

### Other failure scenarios

Each of `infra/k3d/scenarios/{oomkilled, broken-deploy, db-connection-exhaustion, dns-misconfig, expired-cert, disk-full, disabled-cache, high-latency}/` has its own `README.md`, `trigger.sh`, and `resolve.sh`, following the same pattern.

### GitLab and Slack setup

- **GitLab**: create a personal access token at `https://gitlab.com/-/user_settings/personal_access_tokens` with `read_api` scope, and set `GITLAB_TOKEN`/`GITLAB_PROJECT_ID` (numeric ID or `namespace/project` path) in `.env`. The Change Agent queries this project's commits, merge requests, pipelines, and deployments within the incident window.
- **Slack**: see `infra/slack/README.md` for the full walkthrough — create the app from `infra/slack/app-manifest.yaml`, install it to your workspace, and expose the backend via a tunnel (e.g. `ngrok`) for the `/investigate` slash command.

## Safety

- **Read-only diagnostics.** `backend/app/safety.py` enforces an allowlist (`kubectl get/describe/logs`, `journalctl`, `systemctl status`, `df`, `free`, `uptime`, `ps`, `ss`) and a hard denylist of mutating verbs (`delete`, `rollout`, `apply`, `patch`, `exec`, `restart`, `rm`, `reboot`, `shutdown`, and more), checked against every token to catch shell-injection attempts (e.g. `kubectl get pods; rm -rf /` is rejected). The Kubernetes agent talks to the API's read-only endpoints directly rather than shelling out, but this module is the required gate for any future command-execution feature.
- **Recommended actions are text only.** Approving a recommended action flips a status flag for tracking; nothing is ever executed.
- **Guardrails enforced in code, not just prompt wording:**
  - Every hypothesis cites evidence IDs that exist in the actual evidence set — any ID an LLM invents is dropped, and a hypothesis left with no valid evidence is discarded entirely.
  - Contradicting evidence is always attached and rendered alongside supporting evidence, never hidden.
  - If the evidence set is insufficient, the system returns an explicit "insufficient evidence" result rather than guessing.
  - Confidence is always computed deterministically from structured evidence — the LLM never outputs a number, and prompts state explicitly that confidence is a ranking heuristic, not a certainty claim.
  - Every LLM prompt is redacted for API keys, tokens, passwords, emails, IP addresses, internal hostnames, and database connection strings before it leaves the process.

## API

```
POST   /api/v1/incidents
GET    /api/v1/incidents/{id}
GET    /api/v1/incidents/{id}/hypotheses
GET    /api/v1/incidents/{id}/timeline
GET    /api/v1/incidents/{id}/evidence
POST   /api/v1/incidents/{id}/evidence            (engineer note)
POST   /api/v1/incidents/{id}/actions/{action_id}/approve
GET    /api/v1/incidents/{id}/postmortem
POST   /api/v1/slack/investigate                  (Slack slash command: /investigate <service>)
POST   /api/v1/alerts/webhook                     (Alertmanager webhook intake)
GET    /api/v1/api-keys                           (admin only)
POST   /api/v1/api-keys/{id}/revoke               (admin only)
GET    /api/v1/audit-log                          (admin only)
```

Every endpoint except the two machine-caller routes (Slack signature verification, Alertmanager via API key) requires a valid OIDC token or API key with sufficient role — see Authentication below.

## Authentication, secrets, and operations

- **Auth + RBAC.** Two credential types are accepted, each mapped to a `viewer` / `engineer` / `admin` role: OIDC JWTs for human users (signature verified against a real identity provider's JWKS, issuer/audience checked — see `docs/auth-setup.md` for a Keycloak walkthrough with notes for Okta and Azure AD), and API keys for service-to-service calls such as the Alertmanager webhook (SHA-256 hashed, minted via `backend/scripts/create_api_key.py`, revocable via the API). Every endpoint fails closed: missing or invalid credentials return 401, insufficient role returns 403. A double-gated anonymous fallback exists only for local demo use (`DEMO_MODE=true` and `AUTH_ALLOW_ANONYMOUS_IN_DEMO=true`).
- **Secrets management.** `backend/app/secrets.py` reads static secrets (GitLab token, Slack tokens) and optionally dynamic, short-lived Postgres credentials from HashiCorp Vault. If `VAULT_ADDR` is unset, it falls back to plain environment variables with a warning — see `docs/vault-setup.md` for the required Vault mounts and policies.
- **Durable task queue.** Investigations run as Celery tasks backed by Redis, with automatic fallback to in-process execution if the broker is unreachable.
- **Migrations.** Schema changes are managed with Alembic (`backend/alembic/`); `alembic upgrade head` is the supported path for any real Postgres deployment.
- **Audit log.** Every mutating endpoint and every investigation trigger writes an entry (actor, action, resource, result) to an audit log, queryable via `GET /api/v1/audit-log`.
- **Data retention.** `EVIDENCE_RETENTION_DAYS` controls how long evidence is kept; `backend/scripts/cleanup_expired_evidence.py` (with `--dry-run`) removes expired rows and is intended to run on a schedule.
- **Environment isolation.** Integration clients resolve their target URL, kubeconfig, namespace, and project ID per incident environment, so a staging investigation cannot query production infrastructure.
- **Deployment.** A generic Helm chart is provided under `deploy/helm/rootcause/` (API and worker deployments, a migration job, and secret templates compatible with the External Secrets Operator or plain Kubernetes secrets) — see `docs/deployment.md`.

## Testing

```bash
find backend -name "*.py" -exec python3 -m py_compile {} +
cd backend && pytest
```

The test suite covers the confidence-scoring math, the command allowlist/denylist (including injection attempts), the full agent pipeline against fixture data, and authentication/authorization enforcement.

## Scope and roadmap

**Implemented:** manual and API-triggered incident intake, Alertmanager webhook auto-intake, the full evidence-gathering and hypothesis-scoring pipeline, Slack and postmortem output, read-only integrations with Prometheus/Loki/Kubernetes/GitLab/Slack (real or fixture-backed), a local-LLM-only design with redaction guardrails, authentication/RBAC, Vault-backed secrets, durable task execution, audit logging, and a deployable Helm chart.

**Not yet implemented:**
- PagerDuty/Opsgenie-style paging intake (Alertmanager webhook intake is implemented; other paging systems are not).
- Historical incident matching — the scoring factor exists but there is no incident embedding store or similarity search yet.
- Guided remediation approval workflows beyond a simple audit flag — no multi-step approval chain or change-management integration.
- Any automated remediation. This system never executes a mutating command against anything it investigates; all remediation is human-read and human-run.
- A frontend login flow — the current UI does not perform an OIDC sign-in or attach a token to API requests, so it will not work against an API with auth enforced until that's added.
- A health check endpoint suitable for Kubernetes liveness/readiness probes that verifies downstream dependencies (database, Redis, Vault).

Before relying on this in a real environment, validate the pieces that depend on infrastructure not available during development: authenticate against your actual identity provider, exercise Vault's AppRole login and dynamic credentials against a real Vault server, run `helm lint`/`helm template`/a real install against your target cluster, and load-test the Celery/Redis path under concurrent investigations.
