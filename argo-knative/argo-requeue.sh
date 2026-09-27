#!/usr/bin/env bash
# Set how often the Argo workflow controller re-checks a workflow and how often
# the HTTP agent reports finished steps (ARGO_AGENT_PATCH_RATE defaults to the
# same value). Argo's default is 10s; Argo's own code comments suggest ~2s for
# workflows made of many short steps, with 1s as the practical minimum.
#
#   ./argo-requeue.sh 2s        # tuned
#   ./argo-requeue.sh default   # back to Argo's default (10s)
#
# Record which setting each Experiment 1 run used - it dominates Torch on Argo.
set -euo pipefail
value=${1:?usage: $0 <duration like 2s | default>}
if [ "$value" = "default" ]; then
  kubectl set env deploy/workflow-controller -n argo DEFAULT_REQUEUE_TIME-
else
  kubectl set env deploy/workflow-controller -n argo DEFAULT_REQUEUE_TIME="$value"
fi
kubectl rollout status deploy/workflow-controller -n argo --timeout=180s
kubectl get deploy workflow-controller -n argo \
  -o jsonpath='{.spec.template.spec.containers[0].env}{"\n"}'
