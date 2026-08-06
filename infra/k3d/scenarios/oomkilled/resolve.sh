#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-demo}"
DEPLOY="customer-api"

echo "==> Stopping load generator pod (if running) ..."
kubectl -n "${NAMESPACE}" delete pod oomkilled-load-gen --ignore-not-found --force --grace-period=0 >/dev/null 2>&1 || true
rm -f /tmp/rootcause-scenario-oomkilled-load.pid

echo "==> Reverting WORKER_CONCURRENCY to 10 ..."
kubectl -n "${NAMESPACE}" set env deployment/"${DEPLOY}" WORKER_CONCURRENCY=10

echo "==> Restoring memory limit to 256Mi ..."
kubectl -n "${NAMESPACE}" patch deployment "${DEPLOY}" --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"256Mi"}]'

echo "==> Waiting for rollout ..."
kubectl -n "${NAMESPACE}" rollout status deployment/"${DEPLOY}" --timeout=120s

echo "Recovered. Pods should stabilize within a minute; memory/error-rate metrics will taper back to baseline."
