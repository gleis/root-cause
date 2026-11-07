#!/usr/bin/env bash
set -euo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scenario-images.env"
NAMESPACE="${NAMESPACE:-demo}"

echo "==> Launching disk-fill Job (repeatedly calls /fill-disk against customer-api's 100Mi scratch volume) ..."
kubectl -n "${NAMESPACE}" delete job disk-fill-gen --ignore-not-found >/dev/null 2>&1 || true
cat <<EOF | kubectl -n demo apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: disk-fill-gen
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: filler
          image: ${DEMO_CURL_IMAGE}
          command:
            - /bin/sh
            - -c
            - |
              for i in \$(seq 1 8); do
                echo "fill-disk call \$i:"
                curl -s -X POST http://customer-api/fill-disk
                echo
                sleep 3
              done
EOF

kubectl -n "${NAMESPACE}" wait --for=condition=complete job/disk-fill-gen --timeout=90s || true
echo
echo "Watch: kubectl -n ${NAMESPACE} logs job/disk-fill-gen"
echo "       kubectl -n ${NAMESPACE} exec deploy/customer-api -- df -h /scratch"
echo "Resolve with: infra/k3d/scenarios/disk-full/resolve.sh"
