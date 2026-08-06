# AI Root Cause Investigator (MVP)

An SRE incident-investigation tool that correlates metrics, logs,
deployments, and infrastructure events to produce a **timeline**, **ranked
root-cause hypotheses with cited evidence**, and a **draft postmortem** --
using only a **locally-run LLM (Ollama)**, never a hosted/cloud model.

## Purpose

When an incident fires, an SRE has to manually stitch together Prometheus
graphs, Loki logs, the GitLab deploy history, and `kubectl` output to figure
out what happened. This tool automates the *gathering and correlating* of
that evidence and drafts a first-pass hypothesis + postmortem for a human to
review -- it never takes destructive action and never asserts a cause it
can't back with evidence.

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
  (Prometheus)      (Loki)    (GitLab)     (Kubernetes)  (heuristics + 1 LLM call)
        │                │         │           │                │
        └────────────────┴─────────┴───────────┴────────────────┘
                                    │  Evidence[] (structured, persisted)
                                    ▼
                         ┌─────────────────────┐
                         │   HypothesisAgent    │  deterministic weighted
                         │ (scoring in Python;  │  scoring (scoring.py) +
                         │  LLM writes prose)   │  local LLM for statement
                         └──────────┬───────────┘  /rationale only
                                    ▼
                         ┌─────────────────────┐
                         │   ReportingAgent     │  timeline merge, Slack
                         │                       │  summary, postmortem.md
                         └──────────┬───────────┘
                                    ▼
                    REST API (FastAPI) + frontend/index.html
                                    │
                                    ▼
                    Local Ollama (http://localhost:11434, qwen2.5)
                    -- the ONLY LLM this system ever calls --
```

All LLM calls go through `backend/app/llm_gateway.py`, which redacts
secrets/PII from every prompt before it's sent to Ollama (spec section 17)
and routes different tasks (summary/classification/hypothesis/postmortem) to
per-task-configurable *local* models -- there is no hosted-model fallback
anywhere in the codebase.

## Design choices worth calling out

- **Celery + Redis for durable background execution, with an in-process
  fallback.** The investigation pipeline is dispatched as a Celery task
  (`app/tasks.py::run_investigation_task`, broker/backend config in
  `app/celery_app.py`) so an in-flight investigation survives an API
  process restart -- see `docker-compose.yml`'s `worker` service (`celery
  -A app.celery_app worker`). `app/main.py::dispatch_investigation()` tries
  `.delay()` first and falls back to FastAPI `BackgroundTasks` (logging a
  clear warning) if the broker is unreachable, so local dev / tests still
  work without Redis running. `backend/scripts/run_demo.py` bypasses Celery
  entirely and calls the pipeline directly via `asyncio.run(...)`, so the
  zero-infra demo never needs Redis either.
- **Hand-rolled orchestrator, not LangGraph.** The agent pipeline is a fixed
  sequence (fan-out to 5 specialist agents → hypothesis → reporting) with no
  branching/looping, so a plain async function (`app/agents/coordinator.py`)
  is more debuggable than introducing a graph-execution framework for its
  own sake.
- **Confidence scoring is 100% deterministic Python** (`app/agents/scoring.py`),
  implementing the weighted formula from spec section 6. The LLM is *only*
  ever used to phrase the natural-language hypothesis statement/rationale --
  never to produce the number.

## Setup

### 1. Local LLM (required for full LLM-assisted output; the pipeline still
   runs and produces templated output without it)

```bash
# install Ollama: https://ollama.com
ollama serve &
ollama pull qwen2.5
```

Any locally-pulled model works, not just `qwen2.5` -- set
`OLLAMA_MODEL_CLASSIFICATION` / `OLLAMA_MODEL_HYPOTHESIS` /
`OLLAMA_MODEL_POSTMORTEM` / `OLLAMA_MODEL_SUMMARY` / `OLLAMA_MODEL_REDACTION`
in `.env` to whatever `ollama list` shows (e.g. `gemma3:latest`,
`llama3.1:8b`). Each task can point at a different model if you want a
smaller/faster one for classification and a larger one for hypothesis
generation.

### 2. Fastest path: run the end-to-end demo with no other infra at all

```bash
cd backend
pip install -r requirements.txt
DEMO_MODE=true python scripts/run_demo.py
```

This creates a sample incident, runs the full agent pipeline against the
canned "api-v2.14.3 worker-concurrency increase → memory pressure → 12 pods
OOMKilled → rollback → recovery" fixture data (in
`backend/app/integrations/fixtures/`), and prints the timeline, ranked
hypotheses (with evidence citations + confidence), and Slack summary to
stdout, then writes `backend/demo_postmortem.md`. It uses a throwaway local
SQLite file (`backend/demo.db`) so no Postgres/Docker is required just to
see it work.

### 3. Run the real API + frontend

```bash
docker-compose up --build
```

This starts Postgres, Redis, and the FastAPI backend (`DEMO_MODE=true` by
default in `docker-compose.yml`, still returning fixture evidence but
through the real REST API this time). Then either:

- open `frontend/index.html` directly in a browser (it calls
  `http://localhost:8000` by default, editable in the top bar), or
- it's also served by the backend itself at `http://localhost:8000/`.

Create an incident and watch it investigate itself:

```bash
curl -X POST http://localhost:8000/api/v1/incidents \
  -H "Content-Type: application/json" \
  -d '{"title": "Checkout errors spiking", "service": "checkout-api", "severity": "sev1"}'
```

Copy the returned `incident_id` into the frontend's incident_id box and
click Load (investigation runs in the background, usually a few seconds in
DEMO_MODE, longer if a local LLM call is involved).

### 4. Point it at real infrastructure

Copy `.env.example` to `.env` **at the repo root** (`config.py` looks for it
both at the repo root and in `backend/`, so either location works regardless
of which directory you run `uvicorn`/`run_demo.py` from -- but repo root is
the simplest single source of truth since `docker-compose.yml` also reads it
from there). Set `DEMO_MODE=false`, and fill in:

| Variable | Purpose |
|---|---|
| `PROMETHEUS_URL` | Real Prometheus for MetricsAgent PromQL queries |
| `LOKI_URL` | Real Loki for LogAgent LogQL queries |
| `KUBECONFIG`, `K8S_NAMESPACE` | Real cluster for InfrastructureAgent (read-only k8s API calls) |
| `GITLAB_URL`, `GITLAB_TOKEN`, `GITLAB_PROJECT_ID` | Real GitLab project for ChangeAgent (commits/MRs/pipelines/deployments) |
| `SLACK_BOT_TOKEN`, `SLACK_CHANNEL` | Real Slack workspace for ReportingAgent's posted summary and the `/investigate` acknowledgment |
| `OLLAMA_BASE_URL`, `OLLAMA_MODEL_*` | Point at a different local Ollama host/port or different locally-pulled models per task |

Every integration client in `backend/app/integrations/` has a real
implementation guarded by `if not settings.demo_mode`, so flipping
`DEMO_MODE=false` alone switches every agent from fixtures to live calls.

## Live Demo (k3d) -- real Prometheus/Loki/Kubernetes/GitLab/Slack

This is the "no infra needed" quick demo (sections 2/3 above) extended to a
real, disposable Kubernetes cluster on your own machine, so you can see the
full loop: **an alert fires -> Alertmanager posts a webhook -> an incident
auto-opens -> the pipeline gathers real evidence -> a hypothesis + Slack
message + postmortem come out the other side.**

All of this material lives under `infra/`. Nothing in this section requires
Docker/k3d/kubectl on the machine that authored these scripts -- they were
written and syntax-checked, but **not run against a live cluster**, so
treat the first run as a shakedown; see "Known residual risk" at the end.

### Prerequisites
- Docker Desktop running, plus `k3d`, `kubectl`, `helm` on PATH
  (macOS: `brew install k3d kubectl helm`).
- (Optional, for the GitLab/Slack legs) a GitLab personal access token
  (`api`/`read_api` scope) for a real project, and a Slack app -- see
  `infra/slack/README.md` for the click-through setup (OAuth can't be done
  for you programmatically).

### Ordered command sequence

```bash
# 1. Create the cluster
infra/k3d/setup-cluster.sh

# 2. Install Prometheus+Alertmanager+Grafana (kube-prometheus-stack) and
#    Loki+Promtail, plus the sample PrometheusRule (error rate > 10%)
infra/k3d/install-observability.sh

# 3. Build and deploy the sample workload ("customer-api")
docker build -t customer-api:demo infra/k3d/sample-app
k3d image import customer-api:demo -c rootcause-demo
kubectl apply -f infra/k3d/sample-app/k8s/namespace.yaml
kubectl apply -f infra/k3d/sample-app/k8s/configmap.yaml
kubectl apply -f infra/k3d/sample-app/k8s/deployment.yaml
kubectl apply -f infra/k3d/sample-app/k8s/service.yaml
kubectl apply -f infra/k3d/sample-app/k8s/servicemonitor.yaml
kubectl -n demo rollout status deployment/customer-api

# 4. Port-forward Prometheus/Loki/Alertmanager/Grafana to localhost
#    (run this in its own terminal -- it blocks)
infra/k3d/port-forward.sh

# 5. In another terminal: configure and start the backend against the
#    live cluster instead of fixtures (repo root, not backend/ --
#    config.py checks both, but keep one copy to avoid confusion)
cp .env.example .env
#   edit .env: DEMO_MODE=false, K8S_NAMESPACE=demo,
#   GITLAB_TOKEN=<your PAT>, GITLAB_PROJECT_ID=<your project>,
#   SLACK_BOT_TOKEN=<xoxb-...>, SLACK_SIGNING_SECRET=<...> (see
#   infra/slack/README.md if you want the Slack leg -- optional)
cd backend
pip install -r ../requirements.txt
uvicorn app.main:app --reload

# 6. Trigger a failure scenario (each has trigger.sh / resolve.sh / README.md
#    under infra/k3d/scenarios/) -- start with the canonical OOMKilled one:
infra/k3d/scenarios/oomkilled/trigger.sh

# 7. Watch the PrometheusRule fire (couple of minutes):
kubectl -n demo get pods -w
#   http://localhost:9090/alerts  (via the port-forward from step 4)

# 8. Alertmanager posts to POST /api/v1/alerts/webhook (routed per
#    infra/k3d/alertmanager-config.yaml, applied automatically by
#    install-observability.sh) -- this auto-creates an incident and starts
#    the investigation in the background. Check it via the API or frontend:
curl -s http://localhost:8000/api/v1/incidents/<incident_id>/hypotheses | jq
#   or open frontend/index.html and paste in the incident_id.

# 9. If you configured Slack (step 5), the incident's summary is also
#    posted to SLACK_CHANNEL once the investigation finishes.

# 10. Recover
infra/k3d/scenarios/oomkilled/resolve.sh

# When you're done with the whole demo:
infra/k3d/teardown.sh
```

### Other scenarios

Each of `infra/k3d/scenarios/{oomkilled,broken-deploy,
db-connection-exhaustion,dns-misconfig,expired-cert,disk-full,
disabled-cache,high-latency}/` has its own `README.md`, `trigger.sh`, and
`resolve.sh`, all following the same pattern as step 6/10 above.

### Prerequisites detail: GitLab PAT and Slack app

- **GitLab**: create a personal access token at
  `https://gitlab.com/-/user_settings/personal_access_tokens` with `read_api`
  scope, set `GITLAB_TOKEN` and `GITLAB_PROJECT_ID` (numeric project ID or
  `namespace/project` path) in `.env`. `ChangeAgent` queries this project's
  commits/MRs/pipelines/deployments in the incident window.
- **Slack**: see `infra/slack/README.md` for the full walkthrough (create
  app from `infra/slack/app-manifest.yaml`, install to workspace, get the
  bot token + signing secret, expose the backend via a tunnel like `ngrok`
  for the `/investigate` slash command).

### Known residual risk (honest caveat)

This infra was authored and statically validated (YAML parses, shell
scripts read through carefully, Python syntax-checked) in a sandbox with
**no Docker/k3d/kubectl available**, so none of the k3d/helm/kubectl
commands above have actually been executed end-to-end. Specific things
that could need a tweak on first real run:
- Exact `kube-prometheus-stack` / `loki-stack` chart values (service names
  like `prometheus-kube-prometheus-prometheus`, the Alertmanager
  `configSecret` mechanism) are based on the current published chart
  defaults as of this writing and may drift with chart version.
- `host.k3d.internal` (used in `infra/k3d/alertmanager-config.yaml` so
  in-cluster Alertmanager can reach the backend running on your Mac) is a
  k3d feature that should work out of the box, but hasn't been confirmed
  against a live cluster.
- Timing/thresholds in the scenario scripts (how long until OOMKilled,
  how much load is needed) are estimates based on the app's simulated
  memory-per-worker math, not measured against a real container runtime.

## Safety notes

- **Read-only diagnostics.** `backend/app/safety.py` implements
  `is_command_allowed(cmd)` — an allowlist of `kubectl get/describe/logs`,
  `journalctl`, `systemctl status`, `df`, `free`, `uptime`, `ps`, `ss`, with a
  hard denylist of mutating verbs (`delete`, `rollout`, `apply`, `patch`,
  `exec`, `restart`, `rm`, `reboot`, `shutdown`, etc.) that is checked
  against *every token*, not just the subcommand slot, specifically to catch
  shell-injection/string-trick smuggling (`kubectl get pods; rm -rf /` is
  rejected outright due to the `;`). Nothing in this codebase currently
  shells out at all -- `InfrastructureAgent` talks to the Kubernetes Python
  client's read-only list APIs directly -- but this module is the gate any
  future "run a diagnostic command" feature must go through.
- **Recommended actions are text only.** `RecommendedAction` rows (and the
  `POST /actions/{id}/approve` endpoint) never execute anything. Approving
  an action just flips a status flag for audit/tracking; the `command_text`
  field is always prefixed with a comment noting it's a recommendation, not
  something the system runs.
- **AI guardrails**, enforced in code (not just prompt wording), in
  `app/agents/hypothesis_agent.py`:
  - Every hypothesis must cite real `evidence_id`s that exist in the actual
    Evidence set; any evidence_id the LLM invents is silently dropped, and a
    hypothesis with zero valid supporting evidence after that filtering is
    discarded entirely.
  - Contradicting evidence is always attached to a hypothesis and rendered
    alongside supporting evidence -- never hidden.
  - If the evidence set is empty/insufficient, the system returns an
    explicit "insufficient evidence" result rather than guessing.
  - Confidence is always computed deterministically in
    `app/agents/scoring.py` from structured Evidence -- the LLM never
    outputs a number, and prompts explicitly state confidence is a ranking
    heuristic, not a certainty claim.
  - Every LLM prompt is redacted (`app/llm_gateway.py::redact`) for API
    keys/tokens, passwords, emails, IPs, internal hostnames, and DB
    connection strings before it leaves the process.

## Endpoints (spec section 11)

```
POST   /api/v1/incidents
GET    /api/v1/incidents/{id}
GET    /api/v1/incidents/{id}/hypotheses
GET    /api/v1/incidents/{id}/timeline
GET    /api/v1/incidents/{id}/evidence
POST   /api/v1/incidents/{id}/evidence            (engineer_note)
POST   /api/v1/incidents/{id}/actions/{action_id}/approve
GET    /api/v1/incidents/{id}/postmortem
POST   /api/v1/slack/investigate                  (Slack slash-command style: /investigate <service>)
POST   /api/v1/alerts/webhook                     (Alertmanager webhook intake -- auto-opens an incident from a firing alert)
GET    /api/v1/api-keys                            (admin-only)
POST   /api/v1/api-keys/{id}/revoke                 (admin-only)
GET    /api/v1/audit-log                            (admin-only)
```

Every endpoint above except the two machine-caller ones (Slack signature
verification, Alertmanager via API key) is gated by
`Depends(require_role(...))` -- see "Production Readiness" below.

## What's in scope for this MVP vs. the full roadmap

**In scope (this MVP):**
- Manual/API-triggered incident intake (Phase 1: investigation assistant).
- Full 5-agent evidence-gathering fan-out + deterministic hypothesis
  scoring + Slack summary + postmortem draft (pulls in some Phase 2
  capabilities: basic hypothesis scoring, Slack/postmortem output).
- Read-only integrations with real-or-fixture Prometheus/Loki/Kubernetes/
  GitLab/Slack.
- Local-LLM-only design end to end, with redaction guardrails.

**Explicitly OUT of scope / future work:**
- **PagerDuty webhooks** (or other paging systems). Alertmanager webhook
  auto-intake IS implemented (`POST /api/v1/alerts/webhook`, see the "Live
  Demo (k3d)" section above) -- PagerDuty/OpsGenie-style intake is not.
- **Historical incident matching / vector DB.** The `historical_match`
  scoring factor in `scoring.py` is a stub (always 0 unless explicitly
  passed a count) -- there is no incident embedding store or similarity
  search yet.
- **Guided remediation approval workflows.** `POST /actions/{id}/approve`
  only flips a status flag for audit purposes; there is no multi-step
  approval chain, no execution, no integration with a change-management
  system.
- **Any automation / auto-remediation.** This system never executes a
  mutating command against any system it investigates -- see Safety notes
  above. All remediation is human-read, human-run.

## Sanity-checking this codebase

```bash
find backend -name "*.py" -exec python3 -m py_compile {} +
python3 -c "import yaml; yaml.safe_load(open('docker-compose.yml'))"
cd backend && pytest    # 61 tests: scoring, safety, the demo pipeline, auth enforcement
```

## Production Readiness

This section is an honest inventory of what moved from "demo/portfolio"
to "genuinely production-capable" in this phase, and what's still not
proven against real infrastructure.

### Implemented

- **Auth + RBAC** (`app/auth.py`, `app/audit.py`). Two credential types,
  either satisfies every protected endpoint via one dependency,
  `Depends(require_role(min_role))`:
  - **OIDC JWTs** for human/UI users -- signature verified against a real
    IdP's JWKS (RS256), issuer/audience checked, a configurable claim
    (`OIDC_ROLES_CLAIM`) mapped to `viewer`/`engineer`/`admin`. No fake IdP
    is bundled -- see `docs/auth-setup.md` (Keycloak worked example, Okta/
    Azure AD notes).
  - **API keys** for service-to-service calls (Alertmanager webhook, CI,
    scripts) -- SHA-256 hashed, never stored in plaintext, minted via
    `backend/scripts/create_api_key.py`, revocable via
    `POST /api/v1/api-keys/{id}/revoke`.
  - Fails closed throughout: missing/invalid/expired/forged credentials ->
    401; valid credential with insufficient role -> 403. The only
    exception is an explicit, double-gated anonymous-admin fallback
    (`DEMO_MODE=true` AND `AUTH_ALLOW_ANONYMOUS_IN_DEMO=true`, both true by
    default only in the demo/docker-compose config) that keeps the
    zero-setup demo working.
- **Vault integration** (`app/secrets.py`, `hvac`). Static secrets (GitLab
  token, Slack bot token/signing secret) read from a KV v2 mount; optional
  short-lived dynamic Postgres credentials from Vault's database secrets
  engine instead of a static `DATABASE_URL` password. Falls back to plain
  env vars (with a loud warning) when `VAULT_ADDR` is unset. See
  `docs/vault-setup.md` for the mounts/policies/AppRole setup.
- **Durable task queue.** The investigation pipeline runs as a Celery task
  (`app/celery_app.py`, `app/tasks.py`) backed by Redis, dispatched via
  `dispatch_investigation()` in `app/main.py`, with an automatic fallback
  to in-process `BackgroundTasks` if the broker is unreachable. `docker-
  compose.yml` gained a `worker` service. `run_demo.py` still runs the
  pipeline synchronously in-process and never needs Redis.
- **Alembic migrations** (`backend/alembic/`). Initial migration generated
  from the current models (including the new `api_keys`/`audit_log`
  tables), verified end-to-end against SQLite (`alembic upgrade head`
  creates all 6 tables). `database.py::init_db()` now only auto-creates
  tables for DEMO_MODE/SQLite -- a real Postgres deployment is expected to
  run `alembic upgrade head` (see the `migrate` service in `docker-
  compose.yml` and the migration Job in the Helm chart) instead.
- **Audit logging** (`app/audit.py`, `audit_log` table). Every mutating
  endpoint (incident creation, evidence add, action approval, API key
  revocation) and every investigation trigger (manual, Slack, Alertmanager)
  writes an entry with actor/action/resource/IP/result. Queryable via
  `GET /api/v1/audit-log` (admin-only).
- **Data retention.** `EVIDENCE_RETENTION_DAYS` config +
  `backend/scripts/cleanup_expired_evidence.py` (with `--dry-run`), meant
  to run as a cron job / k8s CronJob, deletes Evidence rows (and any
  locally-stored raw blobs) past the retention window.
- **Environment restriction enforcement.** `PrometheusClient`,
  `LokiClient`, `KubernetesClient`, and `GitLabClient` now accept an
  `environment` argument and resolve their target URL/kubeconfig/namespace/
  project-id from an environment-scoped override (`Settings.*_for()`) when
  one is configured, falling back to the global value otherwise. Every
  specialist agent (`metrics_agent`, `log_agent`, `infra_agent`,
  `change_agent`) now constructs its client with `environment=incident.
  environment`, so a staging investigation can't accidentally query
  production infrastructure.
- **Automated tests** (`backend/tests/`, pytest). 61 tests covering
  `scoring.py`'s deterministic math, `safety.py`'s allowlist/denylist
  (including shell-injection/smuggling attempts), the full 8-agent
  pipeline end-to-end against DEMO_MODE fixtures (asserts the OOMKilled
  scenario produces the expected top hypothesis), and auth enforcement
  (valid/invalid/missing API keys, valid/expired/forged/wrong-audience
  JWTs against a self-issued test JWKS, role checks) via FastAPI's
  `TestClient`. All 61 pass.
- **Helm chart** (`deploy/helm/rootcause/`). API Deployment, worker
  Deployment, migration Job (pre-install/pre-upgrade hook running `alembic
  upgrade head`), Service, ConfigMap, and either an External Secrets
  Operator `ExternalSecret` or a plain k8s `Secret` for Vault-sourced
  values -- no secret value is ever hardcoded in the chart. Not
  cloud-specific; see `docs/deployment.md` for self-managed vs managed k8s
  notes.

### Explicitly NOT done / not validated live

Be honest with yourself before a real rollout about what here has only
been reviewed vs actually run:

- **No real OIDC IdP was stood up.** `test_auth.py` verifies the JWT
  verification *logic* (signature/issuer/audience/role-claim checks,
  rejection of expired/forged/wrong-audience tokens) against a
  locally-generated RSA keypair standing in for a JWKS -- it does not prove
  a specific Keycloak/Okta/Azure AD tenant's real token shape matches
  what's documented. Mint one real token from your chosen IdP and confirm
  before going live.
- **No real Vault server was exercised.** `app/secrets.py`'s env-var
  fallback path is tested (and is what `run_demo.py`/the test suite
  actually exercise); the AppRole login, KV v2 read, and dynamic database
  credential paths are implemented against `hvac`'s documented API but
  unverified against a live Vault instance.
- **No real Kubernetes cluster.** The Helm chart has not been through
  `helm lint`/`helm template`/`helm install --dry-run`, let alone a real
  install -- no Docker/Kubernetes was available in the environment this was
  built in. Validate with `helm lint` and a local kind/k3d/minikube cluster
  before trusting it in production. (Separately, the pre-existing
  `infra/k3d/` live-cluster material was also never executed, for the same
  reason -- see the top of this README.)
- **No load-tested Celery/Redis.** The durable-queue wiring is
  functionally correct (dispatch, fallback, task retry config) but hasn't
  been exercised under concurrent multi-incident load, worker crash/restart
  scenarios, or Redis failover.
- **No `/healthz` endpoint yet.** The Helm chart's readiness probe hits
  `/` (served by the static frontend) as a placeholder, which proves the
  process is up but not that the DB/Redis/Vault dependencies it needs are
  reachable -- a real liveness/readiness endpoint that checks those is a
  reasonable near-term follow-up.
- **No frontend auth.** `frontend/index.html` is still the plain,
  unauthenticated static page from the original MVP -- it does not perform
  an OIDC login flow or attach a bearer token to its API calls. Wiring
  that up (e.g. `oidc-client-ts` + attaching `Authorization: Bearer
  <token>` to every fetch) is necessary before real human users hit the
  now-auth-gated API through the browser.
