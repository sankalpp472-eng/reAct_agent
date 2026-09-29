#!/usr/bin/env bash
# Inside the VM: run one stack at a time, so neither competes with the other
# during measurements.
#
#   bash cluster/vm/switch_stack.sh b     # Argo + Knative on k3s (first time: installs them)
#   bash cluster/vm/switch_stack.sh a     # Conductor + faasd
#   DEPLOY=1 bash cluster/vm/switch_stack.sh b   # also redeploy the functions (e.g. new images)
#
# Stack B's first start downloads Knative, Kourier, Argo and the function images,
# so the tunnel (cluster/vm/tunnel.sh on the node) must be up for it.
set -euo pipefail
cd "$(dirname "$0")/../.."
REGISTRY=${REGISTRY:-sankalps2003}
SOCKS=${SOCKS:-socks5://10.0.2.2:1080}
NO_PROXY_LIST=localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,.svc,.cluster.local,.local
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

wait_for() {  # wait_for <description> <command...>
  local what=$1; shift
  echo -n "waiting for $what "
  for _ in $(seq 1 120); do "$@" >/dev/null 2>&1 && { echo up; return 0; }; echo -n .; sleep 5; done
  echo; echo "ERROR: $what not up after 10 min" >&2; return 1
}

case "${1:-}" in
b)
  echo "== stopping Stack A"
  docker stop conductor >/dev/null 2>&1 || true
  sudo systemctl stop faasd faasd-provider
  echo "== starting k3s"
  sudo systemctl start k3s
  wait_for "the k3s node" sh -c "kubectl get nodes 2>/dev/null | grep -q ' Ready'"
  mkdir -p ~/.kube && ln -sf "$KUBECONFIG" ~/.kube/config   # for the drivers' kubernetes client

  if ! kubectl get ns knative-serving >/dev/null 2>&1; then
    echo "== first start: installing Knative + Kourier + Argo"
    bash argo-knative/setup.sh
    # Knative's controller resolves image tags to digests on Docker Hub itself,
    # so it needs the tunnel too (pods reach the node's tunnel at 10.0.2.2)
    kubectl -n knative-serving set env deploy/controller \
      HTTPS_PROXY="$SOCKS" HTTP_PROXY="$SOCKS" NO_PROXY="$NO_PROXY_LIST"
    kubectl -n knative-serving rollout status deploy/controller --timeout=300s
  fi
  # Each deploy makes new Knative revisions, which look their images up on Docker
  # Hub (tunnel needed). So only deploy the first time, or with DEPLOY=1.
  if [ "${DEPLOY:-0}" = 1 ] || ! kubectl -n argo get workflowtemplate pae-agentic-loop >/dev/null 2>&1; then
    echo "== deploying the functions (Knative) and the WorkflowTemplate (Argo)"
    REGISTRY="$REGISTRY" bash argo-knative/deploy.sh
  else
    wait_for "the Knative services" kubectl wait ksvc --all -n default --for=condition=Ready --timeout=5s
  fi
  echo "Argo requeue: $(kubectl -n argo get deploy workflow-controller \
    -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="DEFAULT_REQUEUE_TIME")].value}' || true)"\
"  (empty = Argo's default, 10s; change with argo-knative/argo-requeue.sh)"
  KOURIER=$(kubectl -n kourier-system get svc kourier -o jsonpath='{.spec.clusterIP}')
  echo
  echo "Stack B ready. Driver ingress: --ingress knative://$KOURIER:80/default.example.com"
  ;;
a)
  echo "== stopping k3s"
  sudo systemctl stop k3s || true
  sudo /usr/local/bin/k3s-killall.sh >/dev/null 2>&1 || true   # its pods and network rules
  echo "== starting Stack A"
  sudo systemctl start faasd-provider faasd
  docker start conductor >/dev/null
  wait_for "the faasd gateway" curl -sf --noproxy '*' http://127.0.0.1:8080/healthz
  wait_for "Conductor" curl -sf --noproxy '*' http://127.0.0.1:8082/health
  echo "Stack A ready: --conductor http://127.0.0.1:8082/api --gateway http://127.0.0.1:8080"
  ;;
*)
  echo "usage: $0 a|b" >&2; exit 2 ;;
esac
