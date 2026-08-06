# Vault setup

`backend/app/secrets.py` (`VaultClient`) reads:

- Static secrets (GitLab token, Slack bot token, Slack signing secret) from
  a **KV v2** mount.
- Optionally, short-lived dynamic Postgres credentials from Vault's
  **database secrets engine**, instead of a static password in
  `DATABASE_URL`.

If `VAULT_ADDR` is unset, every one of these falls back to the plain env
var (`GITLAB_TOKEN`, `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`,
`DATABASE_URL`) and logs a `WARNING` on first use -- this is what keeps
local dev / DEMO_MODE / `docker-compose up` working with zero Vault setup,
but it also means: **if you see that warning in a production log, Vault
isn't actually wired up and secrets are coming from plain environment
variables** (still fine security-wise if those env vars themselves come
from a k8s Secret backed by something sane, just not what was asked for).

## 1. KV v2 mount for static secrets

```bash
vault secrets enable -path=secret kv-v2   # often already enabled by default as "secret/"

vault kv put secret/rootcause \
  gitlab_token="glpat-xxxxxxxxxxxxxxxxxxxx" \
  slack_bot_token="xoxb-xxxxxxxxxxxx-xxxxxxxxxxxx-xxxxxxxxxxxxxxxxxxxxxxxx" \
  slack_signing_secret="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
```

App config:
```
VAULT_ADDR=https://vault.example.com:8200
VAULT_KV_MOUNT=secret
VAULT_KV_PATH=rootcause
```

## 2. Policy scoping read access to just that path

```hcl
# rootcause-app-policy.hcl
path "secret/data/rootcause" {
  capabilities = ["read"]
}
path "secret/metadata/rootcause" {
  capabilities = ["read"]
}
```

```bash
vault policy write rootcause-app rootcause-app-policy.hcl
```

## 3. AppRole auth (preferred over a static root/admin token for a service)

```bash
vault auth enable approle

vault write auth/approle/role/rootcause-app \
  token_policies="rootcause-app" \
  token_ttl=1h \
  token_max_ttl=4h

vault read auth/approle/role/rootcause-app/role-id
vault write -f auth/approle/role/rootcause-app/secret-id
```

App config (instead of `VAULT_TOKEN`):
```
VAULT_ROLE_ID=<role_id from above>
VAULT_SECRET_ID=<secret_id from above>
```

`secret_id` is itself a secret -- deliver it to the running app via your
platform's usual mechanism (k8s Secret injected as an env var, Vault Agent
Injector, CSI secrets store driver, etc), not by hardcoding it. A static
`VAULT_TOKEN` is only meant for local Vault dev servers (`vault server -dev`).

## 4. Database secrets engine for dynamic Postgres credentials (optional)

Lets Vault mint a short-lived Postgres role+password per app instance
instead of a static password baked into `DATABASE_URL`.

```bash
vault secrets enable -path=database database

vault write database/config/rootcause-postgres \
  plugin_name=postgresql-database-plugin \
  connection_url="postgresql://{{username}}:{{password}}@postgres:5432/rootcause?sslmode=disable" \
  allowed_roles="rootcause-app" \
  username="vault_admin" \
  password="<vault_admin's own postgres password>"

vault write database/roles/rootcause-app \
  db_name=rootcause-postgres \
  creation_statements="CREATE ROLE \"{{name}}\" WITH LOGIN PASSWORD '{{password}}' VALID UNTIL '{{expiration}}'; \
    GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO \"{{name}}\";" \
  default_ttl="1h" \
  max_ttl="24h"
```

App config:
```
VAULT_DATABASE_MOUNT=database
VAULT_DATABASE_ROLE=rootcause-app
```

`VaultClient.get_database_url()` calls
`database/creds/rootcause-app`, gets back a `{username, password}` pair with
a lease, and merges it into the host/port/dbname parsed from the static
`DATABASE_URL` template. It refetches shortly before the lease expires.
`vault_admin`'s own postgres user needs `CREATEROLE` (so it can create the
short-lived roles) -- provision that separately with your normal Postgres
admin tooling, it's outside Vault's scope.

**Caveat**: Alembic migrations (`alembic upgrade head`, see
`backend/alembic/`) run as a separate one-shot step (docker-compose
`migrate` service / Helm migration Job) and use the static `DATABASE_URL`
directly today, not dynamic credentials -- wiring Alembic itself through
Vault dynamic creds is a reasonable follow-up but adds complexity (the
migration role would need `CREATE TABLE`/`ALTER TABLE`, broader than the
app's own runtime role should have) that wasn't justified for this phase.

## What's NOT been tested here

No real Vault server was started and exercised end-to-end in this
sandbox (no network egress / no Vault binary available). `app/secrets.py`
was verified to: (a) import cleanly, (b) fall back to plain env vars and
log the expected warning when `VAULT_ADDR` is unset (this is exactly the
path `backend/scripts/run_demo.py` and the test suite exercise). The AppRole
login, KV v2 read, and database dynamic-credential paths are implemented
against the documented `hvac` API but have only been reviewed, not run
against a live Vault -- validate them against a real `vault server -dev`
instance before depending on them in production.
