#!/usr/bin/env bash
# Deploy the five functions as Knative Services and the Argo WorkflowTemplate.
# Re-run after rebuilding/pushing the images or changing the YAML.
#   REGISTRY  image registry/user the faas-cli images were pushed to
#   TAG       image tag
set -euo pipefail
cd "$(dirname "$0")"
REGISTRY=${REGISTRY:-sankalps2003}
TAG=${TAG:-latest}

sed -e "s|REGISTRY/|${REGISTRY}/|" -e "s|:TAG\$|:${TAG}|" -e "s|DEPLOYED_AT|$(date +%s)|" \
  knative-services.yaml | kubectl apply -f -
kubectl wait ksvc --all -n default --for=condition=Ready --timeout=300s

kubectl apply -f argo-rbac.yaml
kubectl apply -f argo-workflowtemplate.yaml

kubectl get ksvc -n default
kubectl get workflowtemplate -n argo
