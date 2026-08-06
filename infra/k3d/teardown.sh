#!/usr/bin/env bash
# Deletes the k3d demo cluster and everything in it (all namespaces,
# volumes local to the cluster's containers). Nothing on the host is
# touched beyond the k3d-managed docker containers/network/volumes.
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-rootcause-demo}"

if ! command -v k3d >/dev/null 2>&1; then
  echo "ERROR: k3d not found on PATH." >&2
  exit 1
fi

if k3d cluster list 2>/dev/null | awk '{print $1}' | grep -qx "${CLUSTER_NAME}"; then
  echo "Deleting k3d cluster '${CLUSTER_NAME}' ..."
  k3d cluster delete "${CLUSTER_NAME}"
  echo "Done."
else
  echo "k3d cluster '${CLUSTER_NAME}' does not exist -- nothing to do."
fi
