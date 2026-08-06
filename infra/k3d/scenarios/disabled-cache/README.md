# Scenario: Disabled cache -> latency/DB-load increase

Toggles `CACHE_ENABLED=false` on customer-api. In the app, this simulates
the app falling through to a "DB roundtrip" on every request (an extra
~50ms sleep per `/work` call standing in for a real cache-miss-hits-Postgres
path), so p99 latency and Postgres load both increase without anything
crashing outright -- a "silent" degradation, useful to contrast against the
crash-y scenarios.

## What `trigger.sh` does
Sets `CACHE_ENABLED=false` via `kubectl set env` (rolling restart) and
starts a load generator hitting `/work` so the latency increase is visible
in Prometheus (`http_request_duration_seconds`).

## What to watch
```bash
kubectl -n demo top pods         # (needs metrics-server, installed by install-observability.sh)
# p99 latency via Prometheus:
#   histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{path="/work"}[5m])) by (le))
```

## Recovery
`resolve.sh` stops the load generator and sets `CACHE_ENABLED=true`.
