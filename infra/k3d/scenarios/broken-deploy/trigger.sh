#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-demo}"
DEPLOY="customer-api"

echo "==> Patching ${DEPLOY} with a guaranteed-crash command (simulated bad deploy) ..."
kubectl -n "${NAMESPACE}" patch deployment "${DEPLOY}" --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/command","value":["/bin/sh","-c","echo BAD_DEPLOY simulated failure; exit 1"]}]'

echo "==> Waiting briefly for CrashLoopBackOff to appear ..."
sleep 15
kubectl -n "${NAMESPACE}" get pods -l app="${DEPLOY}"

echo
echo "Watch: kubectl -n ${NAMESPACE} get pods -w"
echo "Resolve with: infra/k3d/scenarios/broken-deploy/resolve.sh"
