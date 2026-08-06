#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-demo}"

echo "==> Tearing down TLS front-end and failure generator ..."
kubectl -n "${NAMESPACE}" delete job tls-handshake-fail --ignore-not-found --force --grace-period=0
kubectl -n "${NAMESPACE}" delete deployment customer-api-tls --ignore-not-found
kubectl -n "${NAMESPACE}" delete service customer-api-tls --ignore-not-found
kubectl -n "${NAMESPACE}" delete configmap customer-api-tls-nginx-conf --ignore-not-found
kubectl -n "${NAMESPACE}" delete secret customer-api-tls-cert --ignore-not-found

echo "Recovered (TLS front-end fully removed; customer-api itself was never affected)."
