#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-demo}"

echo "==> Calling /reset-disk on customer-api to remove junk files ..."
kubectl -n "${NAMESPACE}" run disk-reset --rm -i --restart=Never --image=curlimages/curl:8.8.0 -- \
  curl -s -X POST http://customer-api/reset-disk

echo "==> Cleaning up load Job ..."
kubectl -n "${NAMESPACE}" delete job disk-fill-gen --ignore-not-found --force --grace-period=0

echo "Recovered."
