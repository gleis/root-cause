#!/usr/bin/env bash
# Creates the k3d cluster used for the live demo of AI Root Cause
# Investigator. Idempotent: safe to re-run if the cluster already exists.
#
# Prerequisites (NOT installed by this script): docker, k3d, kubectl.
#   macOS:  brew install k3d kubectl
#
# Usage:
#   infra/k3d/setup-cluster.sh
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-rootcause-demo}"

for bin in docker k3d kubectl; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "ERROR: '$bin' is required but not found on PATH. Install it first (e.g. 'brew install k3d kubectl', Docker Desktop for docker)." >&2
    exit 1
  fi
done

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker daemon is not running. Start Docker Desktop and re-run this script." >&2
  exit 1
fi

if k3d cluster list 2>/dev/null | awk '{print $1}' | grep -qx "${CLUSTER_NAME}"; then
  echo "k3d cluster '${CLUSTER_NAME}' already exists -- skipping creation."
else
  echo "Creating k3d cluster '${CLUSTER_NAME}' ..."
  # - 1 server + 1 agent node is enough to show real pod scheduling/eviction
  #   behavior without needing much host resource.
  # - Port 8081 -> 80 on the loadbalancer so an Ingress could be added later
  #   without reconfiguring the cluster; not required for the current demo
  #   which uses kubectl port-forward instead.
  k3d cluster create "${CLUSTER_NAME}" \
    --servers 1 \
    --agents 1 \
    --port "8081:80@loadbalancer" \
    --wait
fi

echo "Waiting for cluster nodes to be Ready ..."
kubectl --context "k3d-${CLUSTER_NAME}" wait --for=condition=Ready nodes --all --timeout=180s

echo "Waiting for core system pods (kube-system) to be Ready ..."
kubectl --context "k3d-${CLUSTER_NAME}" -n kube-system wait --for=condition=Ready pods --all --timeout=180s || {
  echo "WARNING: not all kube-system pods reported Ready in time; check 'kubectl -n kube-system get pods'." >&2
}

kubectl config use-context "k3d-${CLUSTER_NAME}"

echo
echo "Cluster '${CLUSTER_NAME}' is up. kubectl context set to k3d-${CLUSTER_NAME}."
echo "Next: infra/k3d/install-observability.sh"
