#!/usr/bin/env bash
# Install Knative Serving (+ Kourier ingress) and Argo Workflows on the current
# kubectl context. Run once per cluster. Safe to re-run.
set -euo pipefail

KNATIVE_VERSION=${KNATIVE_VERSION:-v1.19.0}
ARGO_VERSION=${ARGO_VERSION:-v3.6.19}
KN=https://github.com/knative/serving/releases/download/knative-${KNATIVE_VERSION}
KOURIER=https://github.com/knative/net-kourier/releases/download/knative-${KNATIVE_VERSION}
ARGO=https://github.com/argoproj/argo-workflows/releases/download/${ARGO_VERSION}

# Download the manifests with curl and apply local copies: curl honours any
# proxy in the environment (e.g. the cluster VM's socks5h tunnel); kubectl may not.
MANIFESTS=$(mktemp -d); trap 'rm -rf "$MANIFESTS"' EXIT
fetch() { curl -fsSL --retry 3 -o "$MANIFESTS/$(basename "$1")" "$1" && echo "$MANIFESTS/$(basename "$1")"; }

# Knative v1.19 needs Kubernetes >= 1.32 (its pods crash-loop on "Version check failed" otherwise)
minor=$(kubectl version -o json | python3 -c 'import json,sys,re; print(re.sub(r"\D", "", json.load(sys.stdin)["serverVersion"]["minor"]))')
if [ "${minor}" -lt 32 ]; then
  echo "Kubernetes 1.${minor} is too old for Knative ${KNATIVE_VERSION}: need >= 1.32" >&2
  exit 1
fi

echo "== Knative Serving ${KNATIVE_VERSION}"
kubectl apply -f "$(fetch "${KN}/serving-crds.yaml")"
kubectl wait --for=condition=Established crd -l knative.dev/crd-install=true --timeout=120s
kubectl apply -f "$(fetch "${KN}/serving-core.yaml")"

echo "== Kourier ingress"
kubectl apply -f "$(fetch "${KOURIER}/kourier.yaml")"
kubectl patch configmap/config-network -n knative-serving --type merge \
  -p '{"data":{"ingress-class":"kourier.ingress.networking.knative.dev"}}'
# Services get URLs <name>.<namespace>.example.com; the driver reaches them
# through Kourier with a Host header, so no DNS is needed.
kubectl patch configmap/config-domain -n knative-serving --type merge \
  -p '{"data":{"example.com":""}}'

echo "== Argo Workflows ${ARGO_VERSION}"
kubectl create namespace argo --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n argo -f "$(fetch "${ARGO}/install.yaml")"

echo "== Waiting for everything to be ready"
kubectl wait deploy --all -n knative-serving --for=condition=Available --timeout=600s
kubectl wait deploy --all -n kourier-system --for=condition=Available --timeout=600s
kubectl wait deploy --all -n argo --for=condition=Available --timeout=600s

echo
echo "Kourier ingress service (EXTERNAL-IP / port 80 is what the driver needs):"
kubectl get svc kourier -n kourier-system
