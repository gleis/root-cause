#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-demo}"

echo "==> Deleting connection-exhaustion Job (frees all held connections) ..."
kubectl -n "${NAMESPACE}" delete job db-conn-exhauster --ignore-not-found --force --grace-period=0

echo "Recovered. Connections should drop to baseline within a few seconds:"
echo "  kubectl -n ${NAMESPACE} exec deploy/postgres-demo -- psql -U demo -c 'select count(*) from pg_stat_activity;'"
