#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-demo}"

echo "==> Deleting DNS-failure log generator Job ..."
kubectl -n "${NAMESPACE}" delete job dns-failure-gen --ignore-not-found --force --grace-period=0

echo "==> Restoring DOWNSTREAM_DB_HOST=postgres-demo ..."
kubectl -n "${NAMESPACE}" set env deployment/customer-api DOWNSTREAM_DB_HOST=postgres-demo
kubectl -n "${NAMESPACE}" rollout status deployment/customer-api --timeout=120s

echo "Recovered."
