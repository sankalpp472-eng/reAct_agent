#!/usr/bin/env bash
# Inside the VM: redeploy the running stack's functions with a real LLM (Groq by
# default), or back to the mock. Run switch_stack.sh for that stack first.
#
#   LLM_MODEL=openai/gpt-oss-120b bash cluster/vm/use_llm.sh a    # Stack A (faasd)
#   LLM_MODEL=openai/gpt-oss-120b bash cluster/vm/use_llm.sh b    # Stack B (Knative)
#   LLM_BACKEND=mock bash cluster/vm/use_llm.sh a|b                # back to the mock
#
# The API key is read from ~/.llm-api-key (never from the repo). Create it once:
#   (umask 077; read -rs k; printf %s "$k" > ~/.llm-api-key)     # paste the key, Enter
#
# The VM's only way out is the node's SSH SOCKS tunnel (cluster/vm/tunnel.sh), so
# the functions send their model calls through it (LLM_PROXY); calls between the
# functions stay inside the VM. Other settings (env): LLM_BASE_URL (default Groq),
# LLM_TOOL_MODE (default native), REGISTRY (the images' Docker Hub user).
set -euo pipefail
cd "$(dirname "$0")/../.."
REGISTRY=${REGISTRY:-sankalps2003}
KEY_FILE=${KEY_FILE:-$HOME/.llm-api-key}
GATEWAY=http://127.0.0.1:8080
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
die() { echo "ERROR: $*" >&2; exit 1; }

STACK=${1:-}
[ "$STACK" = a ] || [ "$STACK" = b ] || { echo "usage: $0 a|b" >&2; exit 2; }

export LLM_BACKEND=${LLM_BACKEND:-openai}
if [ "$LLM_BACKEND" = openai ]; then
  export LLM_BASE_URL=${LLM_BASE_URL:-https://api.groq.com/openai/v1}
  export LLM_TOOL_MODE=${LLM_TOOL_MODE:-native}
  export LLM_PROXY=${LLM_PROXY:-socks5h://10.0.2.2:1080}
  : "${LLM_MODEL:?set LLM_MODEL, e.g. LLM_MODEL=openai/gpt-oss-120b}"
  export LLM_MODEL
  [ -s "$KEY_FILE" ] || die "no API key in $KEY_FILE; create it: (umask 077; read -rs k; printf %s \"\$k\" > $KEY_FILE)"
  echo "== checking $LLM_BASE_URL through the tunnel ($LLM_PROXY)"
  models=$(curl -sf -x "$LLM_PROXY" "$LLM_BASE_URL/models" \
             -H @<(printf 'Authorization: Bearer %s' "$(cat "$KEY_FILE")") | jq -r '.data[].id') \
    || die "the API did not answer. Is the tunnel up on the node (cluster/vm/tunnel.sh)? Is the key right?"
  grep -qxF "$LLM_MODEL" <<<"$models" \
    || die "model $LLM_MODEL is not available to this key. Available:"$'\n'"$models"
  echo "model $LLM_MODEL: ok"
  SECRET_FILE=$KEY_FILE
else
  export LLM_BACKEND=mock LLM_PROXY=
  SECRET_FILE=$(mktemp); printf unused > "$SECRET_FILE"; trap 'rm -f "$SECRET_FILE"' EXIT
fi

case "$STACK" in
a)
  curl -sf --noproxy '*' "$GATEWAY/healthz" >/dev/null || die "faasd is not up: bash cluster/vm/switch_stack.sh a"
  # faasd's gateway cuts every call off after 60 s; with rate-limit waits an actor
  # step can take longer. The functions and the workflow allow 300 s.
  COMPOSE=/var/lib/faasd/docker-compose.yaml
  if sudo grep -qE '(read|write)_timeout=60s|upstream_timeout=65s' "$COMPOSE"; then
    echo "== raising faasd's gateway timeouts to 300 s"
    sudo sed -i -E 's/(read_timeout|write_timeout)=60s/\1=300s/; s/upstream_timeout=65s/upstream_timeout=305s/' "$COMPOSE"
    sudo systemctl restart faasd
    for _ in $(seq 1 60); do curl -sf --noproxy '*' "$GATEWAY/healthz" >/dev/null && break; sleep 2; done
  fi
  faas-cli login -g "$GATEWAY" -u admin --password-stdin < ~/.faasd-password >/dev/null
  echo "== secret llm-api-key"
  faas-cli secret create llm-api-key -g "$GATEWAY" --from-file "$SECRET_FILE" >/dev/null 2>&1 \
    || faas-cli secret update llm-api-key -g "$GATEWAY" --from-file "$SECRET_FILE" >/dev/null
  echo "== deploying the functions (LLM_BACKEND=$LLM_BACKEND)"
  faas-cli deploy -f stack.yaml -g "$GATEWAY"
  echo "== registering the workflow (300 s HTTP timeouts)"
  jq -s '.' conductor-agentic-loop/pae_agentic_loop_v2.json \
    | curl -sf --noproxy '*' -X PUT http://127.0.0.1:8082/api/metadata/workflow \
        -H 'Content-Type: application/json' -d @- >/dev/null
  for fn in planner actor evaluator; do
    for _ in $(seq 1 60); do
      [ "$(faas-cli describe "$fn" -g "$GATEWAY" 2>/dev/null | awk '/^Status:/{print $2}')" = Ready ] && break
      sleep 2
    done
  done
  ;;
b)
  kubectl get ns knative-serving >/dev/null 2>&1 || die "Stack B is not up: bash cluster/vm/switch_stack.sh b"
  echo "== secret llm-api-key"
  kubectl create secret generic llm-api-key --from-file=value="$SECRET_FILE" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  echo "== deploying the functions (LLM_BACKEND=$LLM_BACKEND)"
  REGISTRY="$REGISTRY" bash argo-knative/deploy.sh
  ;;
esac
echo
echo "Done: Stack ${STACK^^} uses LLM_BACKEND=$LLM_BACKEND${LLM_MODEL:+ ($LLM_MODEL)}."
