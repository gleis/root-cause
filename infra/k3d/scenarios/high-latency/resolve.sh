#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-demo}"

echo "==> Stopping load generator ..."
kubectl -n "${NAMESPACE}" delete pod high-latency-load-gen --ignore-not-found --force --grace-period=0

echo "==> Reverting LATENCY_MS=20 ..."
kubectl -n "${NAMESPACE}" set env deployment/customer-api LATENCY_MS=20
kubectl -n "${NAMESPACE}" rollout status deployment/customer-api --timeout=120s

echo "Recovered."
