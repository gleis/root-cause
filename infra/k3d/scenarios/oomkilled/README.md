# Scenario: OOMKilled (worker concurrency spike)

Mirrors the spec's canonical example: worker concurrency 10 -> 40, memory
climbs 62% -> 98%, pods get OOMKilled, error rate spikes, then a rollback
(reverting concurrency) recovers.

## What `trigger.sh` does
1. Lowers the `customer-api` Deployment's memory limit to 128Mi (from 256Mi).
2. Raises `WORKER_CONCURRENCY` from 10 to 40 via `kubectl set env`, which
   triggers a rolling restart of the pods (env change = new pod spec).
3. Starts a background `hey`/`curl`-loop load generator (falls back to a
   plain bash curl loop if `hey` isn't installed) hitting `/work` on the
   customer-api Service so the higher per-request memory usage is actually
   exercised and OOMKilled events occur.

Each held "worker slot" retains `MEM_PER_WORKER_MB` (default 4MB) of memory
for the life of the pod process, so 40 concurrent workers ~= 160MB, comfortably
over the 128Mi limit once baseline (20MB) is added -- pods will be OOMKilled
within roughly a minute of load starting.

## What to watch
```bash
kubectl -n demo get pods -w
kubectl -n demo describe pod <pod> | grep -A5 "Last State"
```
You should see `OOMKilled` in `Last State` and climbing `RESTARTS`. The
`CustomerApiOOMKilled` and `CustomerApiHighErrorRate` PrometheusRules
(infra/k3d/prometheusrule-error-rate.yaml) should fire within a couple of
minutes, and (if Alertmanager's webhook is wired up, see the main README's
"Live Demo (k3d)" section) an incident should auto-open via
`POST /api/v1/alerts/webhook`.

## Recovery
Run `resolve.sh`, which:
1. Stops the background load generator.
2. Reverts `WORKER_CONCURRENCY` to 10.
3. Restores the memory limit to 256Mi.
4. Waits for the rollout to finish and pods to be Ready again.
