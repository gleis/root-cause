#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-demo}"

if ! kubectl -n "${NAMESPACE}" get deployment postgres-demo >/dev/null 2>&1; then
  echo "==> postgres-demo not found, applying it first ..."
  kubectl apply -f "$(dirname "${BASH_SOURCE[0]}")/postgres.yaml"
  kubectl -n "${NAMESPACE}" wait --for=condition=Ready pod -l app=postgres-demo --timeout=90s
fi

echo "==> Launching connection-exhaustion Job (max_connections=15, opening ~25 idle connections) ..."
kubectl -n "${NAMESPACE}" delete job db-conn-exhauster --ignore-not-found >/dev/null 2>&1 || true
cat <<'EOF' | kubectl -n "${NAMESPACE}" apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: db-conn-exhauster
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: exhauster
          image: rootcause-postgres:16-secure
          imagePullPolicy: Never
          env:
            - name: PGPASSWORD
              value: demo
          command:
            - /bin/sh
            - -c
            - |
              for i in $(seq 1 25); do
                psql -h postgres-demo -U demo -d demo -c "select pg_sleep(300);" >/tmp/conn_$i.log 2>&1 &
              done
              echo "opened 25 background connections, sleeping 280s to hold them"
              sleep 280
EOF

echo
echo "Watch: kubectl -n ${NAMESPACE} exec deploy/postgres-demo -- psql -U demo -c 'select count(*) from pg_stat_activity;'"
echo "Resolve with: infra/k3d/scenarios/db-connection-exhaustion/resolve.sh"
