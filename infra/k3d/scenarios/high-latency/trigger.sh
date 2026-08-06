#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-demo}"

echo "==> Injecting artificial latency (LATENCY_MS=2000) ..."
kubectl -n "${NAMESPACE}" set env deployment/customer-api LATENCY_MS=2000
kubectl -n "${NAMESPACE}" rollout status deployment/customer-api --timeout=120s || true

echo "==> Starting background load generator ..."
kubectl -n "${NAMESPACE}" delete pod high-latency-load-gen --ignore-not-found --force --grace-period=0 >/dev/null 2>&1 || true
kubectl -n "${NAMESPACE}" run high-latency-load-gen --restart=Never --image=busybox:1.36 -- \
  /bin/sh -c 'i=0; while [ $i -lt 100000 ]; do wget -q -T 5 -O /dev/null http://customer-api/work || true; i=$((i+1)); done'

echo
echo "Watch: Prometheus p99 latency for path=/work"
echo "Resolve with: infra/k3d/scenarios/high-latency/resolve.sh"
