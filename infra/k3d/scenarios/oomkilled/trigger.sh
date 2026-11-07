#!/usr/bin/env bash
set -euo pipefail
source "$(CDPATH= cd -- "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scenario-images.env"
NAMESPACE="${NAMESPACE:-demo}"
DEPLOY="customer-api"
LOAD_PID_FILE="/tmp/rootcause-scenario-oomkilled-load.pid"

echo "==> Lowering memory limit to 128Mi ..."
kubectl -n "${NAMESPACE}" patch deployment "${DEPLOY}" --type=json \
  -p='[{"op":"replace","path":"/spec/template/spec/containers/0/resources/limits/memory","value":"128Mi"}]'

echo "==> Raising WORKER_CONCURRENCY: 10 -> 40 ..."
kubectl -n "${NAMESPACE}" set env deployment/"${DEPLOY}" WORKER_CONCURRENCY=40

echo "==> Waiting for rollout ..."
kubectl -n "${NAMESPACE}" rollout status deployment/"${DEPLOY}" --timeout=120s || true

echo "==> Starting background load generator against customer-api/work ..."
(
  kubectl -n "${NAMESPACE}" run oomkilled-load-gen --rm -i --restart=Never --image="${DEMO_SHELL_IMAGE}" -- \
    /bin/sh -c 'i=0; while [ $i -lt 100000 ]; do wget -q -O /dev/null http://customer-api/work || true; i=$((i+1)); done' \
    >/tmp/rootcause-scenario-oomkilled-load.log 2>&1 &
  echo $! > "${LOAD_PID_FILE}"
)
sleep 1
echo "Load generator started (local wrapper pid in ${LOAD_PID_FILE}; in-cluster pod: oomkilled-load-gen)."
echo
echo "Watch: kubectl -n ${NAMESPACE} get pods -w"
echo "Resolve with: infra/k3d/scenarios/oomkilled/resolve.sh"
