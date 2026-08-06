# Scenario: High latency

Sets `LATENCY_MS` (an artificial `time.sleep()` the app does on every
`/work` request) from its default of 20ms up to 2000ms, so p99 latency
spikes dramatically without any errors or crashes -- a pure latency
incident, useful to contrast against the OOMKilled/broken-deploy scenarios
which also produce error-rate signal.

## What `trigger.sh` does
`kubectl set env deployment/customer-api LATENCY_MS=2000` (rolling restart)
plus a background load generator hitting `/work`.

## What to watch
```bash
# Prometheus:
#   histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{path="/work"}[5m])) by (le))
```
p99 should jump from ~20-50ms to ~2000ms within a couple of scrape
intervals.

## Recovery
`resolve.sh` stops the load generator and reverts `LATENCY_MS=20`.
