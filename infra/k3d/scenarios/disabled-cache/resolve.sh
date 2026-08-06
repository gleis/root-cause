#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-demo}"

echo "==> Stopping load generator ..."
kubectl -n "${NAMESPACE}" delete pod disabled-cache-load-gen --ignore-not-found --force --grace-period=0

echo "==> Re-enabling cache (CACHE_ENABLED=true) ..."
kubectl -n "${NAMESPACE}" set env deployment/customer-api CACHE_ENABLED=true
kubectl -n "${NAMESPACE}" rollout status deployment/customer-api --timeout=120s

echo "Recovered."
