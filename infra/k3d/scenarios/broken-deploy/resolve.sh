#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-demo}"
DEPLOY="customer-api"

echo "==> Removing the bad command (restores the image's default CMD) ..."
kubectl -n "${NAMESPACE}" patch deployment "${DEPLOY}" --type=json \
  -p='[{"op":"remove","path":"/spec/template/spec/containers/0/command"}]' 2>/dev/null || \
kubectl -n "${NAMESPACE}" rollout undo deployment/"${DEPLOY}"

echo "==> Waiting for rollout ..."
kubectl -n "${NAMESPACE}" rollout status deployment/"${DEPLOY}" --timeout=120s

echo "Recovered."
