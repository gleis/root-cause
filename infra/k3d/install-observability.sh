#!/usr/bin/env bash
# Installs the observability stack the AI Root Cause Investigator queries
# against in DEMO_MODE=false:
#   - metrics-server (only if not already present -- some k3d setups ship
#     one, some don't; kube-prometheus-stack doesn't need it, but `kubectl
#     top` and HPAs do, and it's cheap to have for the demo)
#   - kube-prometheus-stack (Prometheus + Alertmanager + Grafana + operator
#     CRDs incl. ServiceMonitor/PrometheusRule), namespace: monitoring
#   - loki-stack (Loki + Promtail), namespace: monitoring
#
# Prerequisites: a running k3d cluster (run setup-cluster.sh first), helm.
#   macOS: brew install helm
#
# Usage:
#   infra/k3d/install-observability.sh
set -euo pipefail

CLUSTER_NAME="${CLUSTER_NAME:-rootcause-demo}"
NAMESPACE="${NAMESPACE:-monitoring}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for bin in kubectl helm; do
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo "ERROR: '$bin' is required but not found on PATH (e.g. 'brew install helm')." >&2
    exit 1
  fi
done

kubectl config use-context "k3d-${CLUSTER_NAME}" >/dev/null

echo "==> Checking for metrics-server ..."
if kubectl get deployment metrics-server -n kube-system >/dev/null 2>&1; then
  echo "metrics-server already present -- skipping."
else
  echo "Installing metrics-server (with --kubelet-insecure-tls, needed inside k3d's self-signed kubelet certs) ..."
  kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
  kubectl patch deployment metrics-server -n kube-system --type=json \
    -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]' || true
fi

echo "==> Adding helm repos ..."
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
helm repo add grafana https://grafana.github.io/helm-charts >/dev/null
helm repo update >/dev/null

kubectl create namespace "${NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

echo "==> Installing kube-prometheus-stack (Prometheus + Alertmanager + Grafana) into namespace '${NAMESPACE}' ..."
# serviceMonitorSelectorNilUsesHelmValues=false (and podMonitor/ruleSelector
# equivalents) so Prometheus picks up ServiceMonitors/PrometheusRules from
# ANY namespace, not just ones labeled to match the chart's own release --
# this is what lets infra/k3d/sample-app/k8s/servicemonitor.yaml (namespace
# 'demo') and the PrometheusRule in this directory get discovered without
# extra label wiring.
helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
  --namespace "${NAMESPACE}" \
  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.podMonitorSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.ruleSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.retention=6h \
  --set alertmanager.alertmanagerSpec.configSecret=alertmanager-rootcause-config \
  --set grafana.adminPassword=admin \
  --wait --timeout 10m

echo "==> Creating Alertmanager config secret (routes alerts to the rootcause webhook) ..."
# kube-prometheus-stack expects the Alertmanager config as a Secret named
# "alertmanager-<config-name>" with key "alertmanager.yaml" when
# alertmanagerSpec.configSecret is set (see above) instead of the chart's
# own values-driven config -- this keeps the webhook URL editable without a
# helm upgrade.
kubectl create secret generic alertmanager-rootcause-config \
  --namespace "${NAMESPACE}" \
  --from-file=alertmanager.yaml="${SCRIPT_DIR}/alertmanager-config.yaml" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl rollout restart statefulset alertmanager-prometheus-kube-prometheus-alertmanager -n "${NAMESPACE}" 2>/dev/null || true

echo "==> Installing loki-stack (Loki + Promtail) into namespace '${NAMESPACE}' ..."
helm upgrade --install loki grafana/loki-stack \
  --namespace "${NAMESPACE}" \
  --set grafana.enabled=false \
  --set prometheus.enabled=false \
  --set loki.persistence.enabled=false \
  --set promtail.enabled=true \
  --wait --timeout 10m

echo
echo "==> Applying the sample error-rate PrometheusRule ..."
kubectl apply -f "${SCRIPT_DIR}/prometheusrule-error-rate.yaml"

echo
echo "Observability stack installed. In-cluster DNS names:"
echo "  Prometheus:   http://prometheus-kube-prometheus-prometheus.${NAMESPACE}:9090"
echo "  Alertmanager: http://prometheus-kube-prometheus-alertmanager.${NAMESPACE}:9093"
echo "  Loki:         http://loki.${NAMESPACE}:3100"
echo "  Grafana:      http://prometheus-grafana.${NAMESPACE} (admin/admin)"
echo
echo "Next: deploy the sample app, then infra/k3d/port-forward.sh for local (non-in-cluster) backend runs."
