# Deployment

This app is built to be deployable to any Kubernetes cluster (self-managed
kubeadm/k3s/RKE, or a managed offering like EKS/GKE/AKS) -- nothing in the
application code or the Helm chart (`deploy/helm/rootcause/`) is
cloud-specific. This doc covers what you provide vs what the chart provides,
and the handful of things that genuinely differ between self-managed and
managed clusters.

## Components

| Component | What it is | Where |
|---|---|---|
| `api` Deployment | FastAPI app (uvicorn) | `deploy/helm/rootcause/templates/deployment-api.yaml` |
| `worker` Deployment | Celery worker running the investigation pipeline | `templates/deployment-worker.yaml` |
| `migrate` Job | Runs `alembic upgrade head`, Helm pre-install/pre-upgrade hook | `templates/job-migrate.yaml` |
| Postgres | Not included in the chart -- bring your own (managed RDS/Cloud SQL/etc, or a separate Postgres chart like Bitnami's) | -- |
| Redis | Not included in the chart -- bring your own (managed ElastiCache/Memorystore/etc, or a separate Redis chart) | -- |
| Vault | Not included -- bring your own Vault cluster; see `docs/vault-setup.md` | -- |
| OIDC IdP | Not included -- bring your own; see `docs/auth-setup.md` | -- |
| Ollama | Not included -- either run it as its own Deployment+PVC (GPU node pool if available) or point `OLLAMA_BASE_URL` at an existing one | -- |

This chart intentionally does NOT bundle Postgres/Redis/Vault/Ollama as
subcharts -- those are exactly the pieces that vary the most by deployment
target (managed service vs self-hosted vs shared-cluster), so bundling them
would make the chart cloud/environment-opinionated in a way the rest of this
project deliberately avoids.

## Secrets: External Secrets Operator vs plain Secret

`values.yaml`'s `externalSecrets.enabled` toggles between two ways to get
Vault-sourced secret material into the cluster:

- **`true`** (recommended if you already run [External Secrets
  Operator](https://external-secrets.io/)): renders an `ExternalSecret`
  that syncs from a `ClusterSecretStore`/`SecretStore` pointed at Vault
  into a native k8s `Secret` named `secretName`. Install ESO and configure
  the Vault-backed SecretStore separately (see ESO's Vault provider docs);
  this chart only emits the `ExternalSecret` resource that consumes it.
- **`false`**: you create the `Secret` named `secretName` yourself, by
  whatever mechanism your org uses (`kubectl create secret generic`, Vault
  Agent Injector's templated files translated into a Secret via an
  init-container, SOPS + a GitOps controller, etc). The chart's
  Deployments/Job just reference `secretKeyRef`s against that name -- they
  don't care how it got there.

Either way, **no secret value is ever hardcoded in the chart** -- `helm
template`/`helm install` never sees plaintext GitLab tokens, Slack tokens,
or database passwords.

## Self-managed Kubernetes (kubeadm, k3s, RKE, etc)

- You own the Ingress controller -- install one (nginx-ingress, Traefik,
  etc) and set `ingress.className` accordingly.
- You own cert issuance for `ingress.tls` -- typically cert-manager +
  Let's Encrypt (HTTP-01 or DNS-01 depending on whether the cluster is
  publicly reachable).
- StorageClass for Postgres (if self-hosting it in-cluster rather than
  using a managed DB) is whatever your CSI driver provides (local-path,
  Longhorn, Ceph/Rook, NFS, ...) -- not something this chart configures.
- No IAM-based pod identity available by default -- Vault auth from pods
  should use AppRole (see `docs/vault-setup.md`) with the `secret_id`
  delivered via a k8s Secret, or Vault's Kubernetes auth method (pods
  authenticate to Vault using their own ServiceAccount token -- set that up
  separately if you want it; the chart creates a ServiceAccount
  (`serviceAccount.create`) that a Vault Kubernetes auth role could bind to).

## Managed Kubernetes (EKS/GKE/AKS/etc)

- Prefer the cloud's managed Postgres (RDS/Cloud SQL/Azure Database for
  Postgres) and Redis (ElastiCache/Memorystore/Azure Cache) over
  self-hosting them in-cluster -- set `DATABASE_URL`/`REDIS_URL` (via the
  Secret/ConfigMap) to point at those instead of in-cluster services.
- Prefer the cloud's managed Ingress/LoadBalancer integration -- set
  `service.type: LoadBalancer` or use the cloud's native Ingress
  controller with `ingress.className` set accordingly, and any
  cloud-specific annotations belong in your own values override file, not
  in this chart.
- IAM-based pod identity (IRSA on EKS, Workload Identity on GKE, Managed
  Identity on AKS) can replace static Vault AppRole credentials if your
  Vault is also integrated with that cloud's IAM -- outside this chart's
  scope, but compatible with it (the ServiceAccount this chart creates is
  exactly what you'd annotate for IRSA/Workload Identity).

## Deploying

```bash
# 1. Provision Postgres, Redis, Vault, and (optionally) Ollama however
#    your environment calls for -- managed services or your own charts.

# 2. Populate secrets (either path):
#    (a) install External Secrets Operator + a Vault-backed SecretStore,
#        set externalSecrets.enabled=true, OR
#    (b) kubectl create secret generic rootcause-secrets \
#          --from-literal=database-url=postgresql+psycopg2://... \
#          --from-literal=gitlab-token=glpat-... \
#          --from-literal=slack-bot-token=xoxb-... \
#          --from-literal=slack-signing-secret=...

# 3. Build and push the backend image (Dockerfile already exists at
#    backend/Dockerfile from the docker-compose setup):
docker build -t ghcr.io/yourorg/rootcause-backend:0.1.0 backend/
docker push ghcr.io/yourorg/rootcause-backend:0.1.0

# 4. Install:
helm install rootcause deploy/helm/rootcause \
  --set image.repository=ghcr.io/yourorg/rootcause-backend \
  --set image.tag=0.1.0 \
  --set config.oidcIssuerUrl=https://your-idp/realms/rootcause \
  --set config.vaultAddr=https://vault.example.com:8200 \
  -f your-environment-values.yaml
```

## What's NOT been tested here

This chart has been reviewed for internal consistency (balanced Go
template `{{ }}`/`{{- if -}}`/`{{- end -}}` pairs, valid YAML for the
non-templated files) but **has not been rendered with `helm template`/
`helm lint` or installed against a real cluster** -- no Docker/Kubernetes
was available in the sandbox this was built in. Before relying on it:
run `helm lint deploy/helm/rootcause`, `helm template deploy/helm/rootcause`
and diff the output against expectations, then `helm install --dry-run
--debug` against a real cluster (kind/k3d/minikube is enough) before a
production rollout.
