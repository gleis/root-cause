#!/usr/bin/env bash
# Port-forwards Prometheus, Loki, Alertmanager and Grafana to localhost so
# the FastAPI backend can run OUTSIDE the cluster (DEMO_MODE=false, normal
# `uvicorn` on your Mac) and still reach them via the *_URL settings in
# .env (which default to localhost).
#
# Run this in its own terminal (it blocks); Ctrl-C to stop all forwards.
#
# Usage:
#   infra/k3d/port-forward.sh
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-rootcause-demo}"
NAMESPACE="${NAMESPACE:-monitoring}"

kubectl config use-context "k3d-${CLUSTER_NAME}" >/dev/null

PIDS=()
cleanup() {
  echo
  echo "Stopping port-forwards ..."
  for pid in "${PIDS[@]:-}"; do
    kill "$pid" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT INT TERM

echo "Forwarding prometheus-kube-prometheus-prometheus -> localhost:9090"
kubectl -n "${NAMESPACE}" port-forward svc/prometheus-kube-prometheus-prometheus 9090:9090 >/tmp/rootcause-pf-prometheus.log 2>&1 &
PIDS+=($!)

echo "Forwarding loki -> localhost:3100"
kubectl -n "${NAMESPACE}" port-forward svc/loki 3100:3100 >/tmp/rootcause-pf-loki.log 2>&1 &
PIDS+=($!)

echo "Forwarding prometheus-kube-prometheus-alertmanager -> localhost:9093"
kubectl -n "${NAMESPACE}" port-forward svc/prometheus-kube-prometheus-alertmanager 9093:9093 >/tmp/rootcause-pf-alertmanager.log 2>&1 &
PIDS+=($!)

echo "Forwarding prometheus-grafana -> localhost:3000"
kubectl -n "${NAMESPACE}" port-forward svc/prometheus-grafana 3000:80 >/tmp/rootcause-pf-grafana.log 2>&1 &
PIDS+=($!)

sleep 2
echo
echo "Port-forwards running (logs in /tmp/rootcause-pf-*.log):"
echo "  Prometheus:   http://localhost:9090"
echo "  Loki:         http://localhost:3100"
echo "  Alertmanager: http://localhost:9093"
echo "  Grafana:      http://localhost:3000 (admin/admin)"
echo
echo "NOTE: the FastAPI backend and Alertmanager's webhook target still need"
echo "to be reachable from IN-CLUSTER pods, not just localhost -- see the"
echo "README 'Live Demo (k3d)' section for how the webhook path is routed"
echo "when the backend runs outside the cluster (a second port-forward /"
echo "tunnel in the other direction, e.g. via 'kubectl port-forward' is not"
echo "possible for inbound traffic; use ngrok or run the backend as an"
echo "in-cluster Deployment for the full closed-loop demo)."
echo
echo "Press Ctrl-C to stop."
wait
