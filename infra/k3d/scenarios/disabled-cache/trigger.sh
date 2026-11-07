#!/usr/bin/env bash
set -euo pipefail
source "$(CDPATH= cd -- "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scenario-images.env"
NAMESPACE="${NAMESPACE:-demo}"

echo "==> Disabling cache (CACHE_ENABLED=false) ..."
kubectl -n "${NAMESPACE}" set env deployment/customer-api CACHE_ENABLED=false
kubectl -n "${NAMESPACE}" rollout status deployment/customer-api --timeout=120s || true

echo "==> Starting background load generator ..."
kubectl -n "${NAMESPACE}" delete pod disabled-cache-load-gen --ignore-not-found --force --grace-period=0 >/dev/null 2>&1 || true
kubectl -n "${NAMESPACE}" run disabled-cache-load-gen --restart=Never --image="${DEMO_SHELL_IMAGE}" -- \
  /bin/sh -c 'i=0; while [ $i -lt 100000 ]; do wget -q -O /dev/null http://customer-api/work || true; i=$((i+1)); done'

echo
echo "Watch: Prometheus p99 latency for path=/work, or kubectl -n ${NAMESPACE} logs -f customer-api-<pod>"
echo "Resolve with: infra/k3d/scenarios/disabled-cache/resolve.sh"
