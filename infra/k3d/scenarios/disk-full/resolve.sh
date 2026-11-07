#!/usr/bin/env bash
set -euo pipefail
source "$(CDPATH= cd -- "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scenario-images.env"
NAMESPACE="${NAMESPACE:-demo}"

echo "==> Calling /reset-disk on customer-api to remove junk files ..."
kubectl -n "${NAMESPACE}" run disk-reset --rm -i --restart=Never --image="${DEMO_CURL_IMAGE}" -- \
  curl -s -X POST http://customer-api/reset-disk

echo "==> Cleaning up load Job ..."
kubectl -n "${NAMESPACE}" delete job disk-fill-gen --ignore-not-found --force --grace-period=0

echo "Recovered."
