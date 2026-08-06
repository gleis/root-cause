#!/usr/bin/env bash
set -euo pipefail
NAMESPACE="${NAMESPACE:-demo}"
BAD_HOST="postgres-demo.nonexistent-namespace.svc.cluster.local"

echo "==> Pointing DOWNSTREAM_DB_HOST at a nonexistent service ..."
kubectl -n "${NAMESPACE}" set env deployment/customer-api DOWNSTREAM_DB_HOST="${BAD_HOST}"
kubectl -n "${NAMESPACE}" rollout status deployment/customer-api --timeout=120s || true

echo "==> Launching DNS-failure log generator Job ..."
kubectl -n "${NAMESPACE}" delete job dns-failure-gen --ignore-not-found >/dev/null 2>&1 || true
cat <<EOF | kubectl -n "${NAMESPACE}" apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: dns-failure-gen
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: dns-fail
          image: busybox:1.36
          command:
            - /bin/sh
            - -c
            - |
              for i in \$(seq 1 60); do
                echo "\$(date -Iseconds) ERROR could not translate host name \"${BAD_HOST}\" to address: Name or service not known"
                nslookup ${BAD_HOST} || true
                sleep 5
              done
EOF

echo
echo "Watch: kubectl -n ${NAMESPACE} logs -f job/dns-failure-gen"
echo "Resolve with: infra/k3d/scenarios/dns-misconfig/resolve.sh"
