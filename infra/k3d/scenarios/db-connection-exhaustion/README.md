Build and import the patched PostgreSQL image before starting this scenario:

```sh
docker build -f deploy/postgres/Dockerfile -t rootcause-postgres:16-secure .
k3d image import rootcause-postgres:16-secure -c "${CLUSTER_NAME:-rootcause-demo}"
```

The image preserves PostgreSQL 16 initialization and storage layout, replacing only gosu with a build using patched Go dependencies. CI scans the final image and checks fresh-volume initialization and persisted data across restart. Run `deploy/postgres/smoke.sh` for the same disposable local check.

# Scenario: DB connection pool exhaustion

Deploys a tiny Postgres (`max_connections=15`) and opens many idle
connections against it from a load-generator Job, so customer-api-style
traffic (or anything else sharing the DB) starts failing with
`FATAL: sorry, too many clients already` / connection-timeout errors.

## Prerequisites
Apply the Postgres deployment once (not part of the base sample-app, since
not every scenario needs it):
```bash
kubectl apply -f infra/k3d/scenarios/db-connection-exhaustion/postgres.yaml
kubectl -n demo wait --for=condition=Ready pod -l app=postgres-demo --timeout=90s
```

## What `trigger.sh` does
Runs a Kubernetes Job (`db-conn-exhauster`) using the `postgres:16-alpine`
image's `psql` client in a loop that opens ~25 simultaneous long-lived
connections (`SELECT pg_sleep(300)`) against `postgres-demo`, comfortably
exceeding `max_connections=15`. New connection attempts (including from
customer-api if you point `DOWNSTREAM_DB_HOST` at it) start failing.

## What to watch
```bash
kubectl -n demo logs job/db-conn-exhauster
kubectl -n demo exec deploy/postgres-demo -- psql -U demo -c "select count(*) from pg_stat_activity;"
```

## Recovery
`resolve.sh` deletes the exhauster Job, which kills its connections; Postgres
frees the connection slots within a few seconds (`pg_sleep(300)` connections
just get dropped when the job pod is deleted).
