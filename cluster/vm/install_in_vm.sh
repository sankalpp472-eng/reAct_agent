#!/usr/bin/env bash
# Stage 4 - run INSIDE the VM (Ubuntu 24.04), once:
#   sudo bash cluster/vm/install_in_vm.sh
#   sudo STEPS="conductor" bash cluster/vm/install_in_vm.sh     # only some steps
#
# Installs only what the experiments need:
#   base       Docker (+ containerd, runc from Ubuntu), git, jq, and the drivers'
#              Python packages from apt (python3-requests, -kubernetes, -yaml)
#   proxy      the VM's internet goes through the node's SSH SOCKS tunnel; give
#              containerd/Docker/faasd/k3s that proxy so they can pull images
#   kernel     overlay + br_netfilter, IP forwarding
#   faasd      faasd 0.19.6 on the same containerd Docker uses (no second
#              containerd), CNI plugins, faas-cli; gateway on :8080
#   conductor  Conductor OSS in Docker (conductoross/conductor, built-in SQLite),
#              API on :8082, UI on :5000
#   k3s        k3s v1.33.1, installed but NOT started (one stack at a time)
# The tunnel (cluster/vm/tunnel.sh on the node) must be running.
set -euo pipefail

STEPS=${STEPS:-"base proxy kernel faasd conductor k3s"}
VM_USER=${VM_USER:-pae}
SOCKS=${SOCKS:-socks5://10.0.2.2:1080}
FAASD_VERSION=${FAASD_VERSION:-0.19.6}
CNI_VERSION=${CNI_VERSION:-v1.5.1}
FAAS_CLI_VERSION=${FAAS_CLI_VERSION:-0.17.1}
CONDUCTOR_IMAGE=${CONDUCTOR_IMAGE:-conductoross/conductor:3.32.5}
K3S_VERSION=${K3S_VERSION:-v1.33.1+k3s1}
# never proxy local, cluster-internal or VM-internal traffic
NO_PROXY_LIST=localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,.svc,.cluster.local,.local,.openfaas

log() { printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || die "run with sudo"

# this script's own downloads (curl, git) go through the tunnel too
export ALL_PROXY=${SOCKS/socks5:/socks5h:} HTTPS_PROXY=${SOCKS/socks5:/socks5h:} HTTP_PROXY=${SOCKS/socks5:/socks5h:}
export NO_PROXY=$NO_PROXY_LIST all_proxy=$ALL_PROXY https_proxy=$HTTPS_PROXY http_proxy=$HTTP_PROXY no_proxy=$NO_PROXY
curl -s -o /dev/null -m 15 https://github.com \
  || die "no internet through the tunnel ($ALL_PROXY). On the node, run: bash cluster/vm/tunnel.sh"

proxy_dropin() {  # Go programs (containerd, dockerd, faasd, k3s) accept socks5:// proxies
  mkdir -p "/etc/systemd/system/$1.service.d"
  cat > "/etc/systemd/system/$1.service.d/proxy.conf" <<EOF
[Service]
Environment="HTTP_PROXY=$SOCKS" "HTTPS_PROXY=$SOCKS" "NO_PROXY=$NO_PROXY_LIST"
EOF
}

step_base() {
  log "base packages"
  apt-get update -q
  DEBIAN_FRONTEND=noninteractive apt-get install -y -q docker.io git jq iptables bridge-utils \
    python3-requests python3-kubernetes python3-yaml
  usermod -aG docker "$VM_USER"
  echo "containerd $(containerd --version | awk '{print $3}'), $(docker --version)"
}

step_proxy() {
  log "proxy for containerd and Docker (image pulls)"
  proxy_dropin containerd; proxy_dropin docker
  systemctl daemon-reload; systemctl restart containerd docker
}

step_kernel() {
  log "kernel modules and sysctls"
  printf 'overlay\nbr_netfilter\n' > /etc/modules-load.d/pae.conf
  modprobe overlay; modprobe br_netfilter
  cat > /etc/sysctl.d/99-pae.conf <<EOF
net.ipv4.ip_forward = 1
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
EOF
  sysctl -q --system
  # Docker sets the FORWARD policy to DROP. Conductor (on docker0) and the
  # functions (on faasd's openfaas0) reach the faasd gateway through
  # 172.17.0.1:8080, which is DNATed and forwarded, so let forwarded traffic
  # through via Docker's DOCKER-USER chain. Re-applied at every boot.
  cat > /etc/systemd/system/pae-forward.service <<'EOF'
[Unit]
Description=Allow forwarding between Docker, faasd and k3s networks
After=docker.service
Requires=docker.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'iptables -C DOCKER-USER -j ACCEPT 2>/dev/null || iptables -I DOCKER-USER -j ACCEPT'
[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload; systemctl enable --now pae-forward.service
}

step_faasd() {
  log "faasd $FAASD_VERSION on the system containerd"
  mkdir -p /opt/cni/bin
  [ -x /opt/cni/bin/bridge ] || curl -fsSL \
    "https://github.com/containernetworking/plugins/releases/download/${CNI_VERSION}/cni-plugins-linux-amd64-${CNI_VERSION}.tgz" \
    | tar -xz -C /opt/cni/bin
  curl -fsSL -o /usr/local/bin/faasd "https://github.com/openfaas/faasd/releases/download/${FAASD_VERSION}/faasd"
  curl -fsSL -o /usr/local/bin/faas-cli "https://github.com/openfaas/faas-cli/releases/download/${FAAS_CLI_VERSION}/faas-cli"
  chmod +x /usr/local/bin/faasd /usr/local/bin/faas-cli
  # `faasd install` copies these from the current directory to /var/lib/faasd
  # and creates the faasd + faasd-provider services from hack/*.service
  src=$(mktemp -d); mkdir -p "$src/hack"
  for f in docker-compose.yaml prometheus.yml resolv.conf hack/faasd.service hack/faasd-provider.service; do
    curl -fsSL -o "$src/$f" "https://raw.githubusercontent.com/openfaas/faasd/${FAASD_VERSION}/$f"
  done
  proxy_dropin faasd; proxy_dropin faasd-provider   # faasd pulls images itself
  (cd "$src" && /usr/local/bin/faasd install)
  rm -rf "$src"
  echo -n "waiting for the gateway on :8080 "
  for _ in $(seq 1 60); do
    curl -s -o /dev/null --noproxy '*' http://127.0.0.1:8080/healthz && { echo up; break; }
    echo -n .; sleep 5
  done
  home=$(getent passwd "$VM_USER" | cut -d: -f6)
  install -m 600 -o "$VM_USER" /var/lib/faasd/secrets/basic-auth-password "$home/.faasd-password"
}

step_conductor() {
  log "Conductor ($CONDUCTOR_IMAGE) in Docker"
  docker pull -q "$CONDUCTOR_IMAGE"
  docker rm -f conductor >/dev/null 2>&1 || true
  # No CONFIG_PROP: the image's built-in defaults (SQLite, no Redis/Elasticsearch).
  # Its HTTP tasks call the functions via the Docker bridge, 172.17.0.1:8080.
  docker run -d --name conductor --restart unless-stopped \
    -p 8082:8080 -p 5000:5000 "$CONDUCTOR_IMAGE" >/dev/null
  echo -n "waiting for Conductor on :8082 "
  for _ in $(seq 1 60); do
    curl -s -o /dev/null --noproxy '*' http://127.0.0.1:8082/health && { echo up; break; }
    echo -n .; sleep 5
  done
}

step_k3s() {
  log "k3s $K3S_VERSION (installed, left stopped)"
  if ! command -v k3s >/dev/null || ! k3s --version | grep -qF "$K3S_VERSION"; then
    # our curl resolves names through the tunnel (socks5h); the proxy the
    # installer saves for k3s's own image pulls (k3s.service.env) is socks5://
    curl -fsSL -o /usr/local/bin/k3s \
      "https://github.com/k3s-io/k3s/releases/download/${K3S_VERSION//+/%2B}/k3s"
    chmod +x /usr/local/bin/k3s
    curl -fsSL -o /tmp/k3s-install.sh https://get.k3s.io
    HTTP_PROXY=$SOCKS HTTPS_PROXY=$SOCKS NO_PROXY="$NO_PROXY_LIST,10.42.0.0/16,10.43.0.0/16" \
      INSTALL_K3S_SKIP_DOWNLOAD=true INSTALL_K3S_SKIP_START=true INSTALL_K3S_SKIP_ENABLE=true \
      INSTALL_K3S_EXEC="server --disable traefik --write-kubeconfig-mode 644" sh /tmp/k3s-install.sh
    rm -f /tmp/k3s-install.sh
  fi
  grep -E '^(HTTPS?_PROXY|NO_PROXY)=' /etc/systemd/system/k3s.service.env || echo "(no proxy saved for k3s)"
}

for s in $STEPS; do "step_$s"; done

log "Done"
cat <<EOF
Log out of the VM and back in (for the docker group), then:
  docker ps                                   # conductor running
  curl -s http://127.0.0.1:8080/healthz; echo # faasd gateway: OK
  curl -s http://127.0.0.1:8082/health; echo  # Conductor: {"healthy":true,...}
Stack B later: docker stop conductor; sudo systemctl stop faasd faasd-provider; sudo systemctl start k3s
EOF
