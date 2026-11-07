#!/usr/bin/env bash
set -euo pipefail
source "$(CDPATH= cd -- "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/scenario-images.env"
NAMESPACE="${NAMESPACE:-demo}"
TMPDIR="$(mktemp -d)"
trap 'rm -rf "${TMPDIR}"' EXIT

if ! command -v openssl >/dev/null 2>&1; then
  echo "ERROR: openssl is required (should be preinstalled on macOS)." >&2
  exit 1
fi

echo "==> Generating an already-expired self-signed certificate ..."
YESTERDAY="$(date -u -v-1d +%y%m%d%H%M%SZ 2>/dev/null || date -u -d 'yesterday' +%y%m%d%H%M%SZ)"
TWO_DAYS_AGO="$(date -u -v-2d +%y%m%d%H%M%SZ 2>/dev/null || date -u -d '2 days ago' +%y%m%d%H%M%SZ)"

openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "${TMPDIR}/tls.key" -out "${TMPDIR}/tls.crt" \
  -subj "/CN=customer-api-tls.demo.svc.cluster.local" \
  -addext "subjectAltName=DNS:customer-api-tls,DNS:customer-api-tls.demo.svc.cluster.local" \
  -not_before "${TWO_DAYS_AGO}" -not_after "${YESTERDAY}"

echo "==> Creating TLS secret ..."
kubectl -n "${NAMESPACE}" delete secret customer-api-tls-cert --ignore-not-found >/dev/null 2>&1 || true
kubectl -n "${NAMESPACE}" create secret tls customer-api-tls-cert \
  --cert="${TMPDIR}/tls.crt" --key="${TMPDIR}/tls.key"

echo "==> Deploying nginx TLS front-end (customer-api-tls) ..."
cat <<EOF | kubectl -n demo apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: customer-api-tls-nginx-conf
data:
  default.conf: |
    server {
      listen 443 ssl;
      ssl_certificate     /etc/tls/tls.crt;
      ssl_certificate_key /etc/tls/tls.key;
      location / {
        proxy_pass http://customer-api:80;
      }
    }
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: customer-api-tls
spec:
  replicas: 1
  selector:
    matchLabels: {app: customer-api-tls}
  template:
    metadata:
      labels: {app: customer-api-tls}
    spec:
      containers:
        - name: nginx
          image: ${DEMO_NGINX_IMAGE}
          ports: [{containerPort: 443}]
          volumeMounts:
            - {name: tls, mountPath: /etc/tls, readOnly: true}
            - {name: conf, mountPath: /etc/nginx/conf.d}
      volumes:
        - name: tls
          secret: {secretName: customer-api-tls-cert}
        - name: conf
          configMap: {name: customer-api-tls-nginx-conf}
---
apiVersion: v1
kind: Service
metadata:
  name: customer-api-tls
spec:
  selector: {app: customer-api-tls}
  ports: [{port: 443, targetPort: 443}]
EOF

kubectl -n "${NAMESPACE}" rollout status deployment/customer-api-tls --timeout=90s

echo "==> Launching TLS-handshake-failure log generator Job ..."
kubectl -n "${NAMESPACE}" delete job tls-handshake-fail --ignore-not-found >/dev/null 2>&1 || true
cat <<EOF | kubectl -n demo apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: tls-handshake-fail
spec:
  backoffLimit: 0
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: tls-check
          image: ${DEMO_CURL_IMAGE}
          command: ["/bin/sh", "-c", "for i in \$(seq 1 20); do curl -v https://customer-api-tls 2>&1 | grep -i -E 'expire|SSL certificate problem' || true; sleep 5; done"]
EOF

echo
echo "Watch: kubectl -n ${NAMESPACE} logs -f job/tls-handshake-fail"
echo "Resolve with: infra/k3d/scenarios/expired-cert/resolve.sh"
