#!/usr/bin/env bash
# Deploy the five functions as Knative Services and the Argo WorkflowTemplate.
# Re-run after rebuilding/pushing the images or changing the YAML.
#   REGISTRY  image registry/user the faas-cli images were pushed to
#   TAG       image tag
#   LLM_BACKEND / LLM_BASE_URL / LLM_MODEL   see the root README, "Using a real LLM"
set -euo pipefail
cd "$(dirname "$0")"
REGISTRY=${REGISTRY:-sankalps2003}
TAG=${TAG:-latest}
# LLM for planner/actor/evaluator: "mock", or "openai" + an OpenAI-compatible URL and model
LLM_BACKEND=${LLM_BACKEND:-mock}
LLM_BASE_URL=${LLM_BASE_URL:-http://172.17.0.1:11434/v1}
LLM_MODEL=${LLM_MODEL:-qwen2.5:3b}

sed -e "s|REGISTRY/|${REGISTRY}/|" -e "s|:TAG\$|:${TAG}|" -e "s|DEPLOYED_AT|$(date +%s)|" \
  -e "s|LLM_BACKEND_VALUE|${LLM_BACKEND}|" -e "s|LLM_BASE_URL_VALUE|${LLM_BASE_URL}|" -e "s|LLM_MODEL_VALUE|${LLM_MODEL}|" \
  knative-services.yaml | kubectl apply -f -
kubectl wait ksvc --all -n default --for=condition=Ready --timeout=300s

kubectl apply -f argo-rbac.yaml
kubectl apply -f argo-workflowtemplate.yaml

kubectl get ksvc -n default
kubectl get workflowtemplate -n argo
