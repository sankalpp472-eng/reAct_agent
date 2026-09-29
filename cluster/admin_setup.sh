#!/usr/bin/env bash
# One-time ROOT setup of a compute node for the Conductor+faasd vs Argo+Knative
# study. To be run by the cluster admin on the node itself (e.g. node13):
#
#   sudo TARGET_USER=sankalps bash admin_setup.sh             # everything
#   sudo TARGET_USER=sankalps PROXY=http://<proxy>:<port> bash admin_setup.sh
#   sudo TARGET_USER=sankalps STEPS="docker sudoers" bash admin_setup.sh   # only some steps
#   sudo bash admin_setup.sh check                            # preflight report only
#
# Steps (all idempotent; run again safely):
#   docker   add TARGET_USER to the docker group (root-equivalent, admin's call)
#   kernel   load overlay/br_netfilter, enable IP forwarding (needed by both stacks)
#   faasd    install faasd (OpenFaaS on containerd) as systemd services
#   k3s      install k3s as a systemd service, INSTALLED BUT STOPPED
#   sudoers  let TARGET_USER start/stop only these services and kill faasd
#            function tasks (for the cold-start experiment); nothing else
#
# The node needs internet access while this runs (it downloads faasd, CNI
# plugins, k3s, and faasd's own container images). If it only has access
# through a proxy, pass PROXY=...; it is also configured for the services, so
# they can pull images later.
#
# What it changes: /usr/local/bin/{faasd,faas-cli}, /opt/cni/bin, /var/lib/faasd,
# systemd units faasd, faasd-provider, k3s, /etc/sysctl.d/99-pae.conf,
# /etc/modules-load.d/pae.conf, /etc/sudoers.d/pae-experiments, and (only if
# the installed one is older than 1.5 and UPGRADE_CONTAINERD=yes) the
# containerd.io package that Docker also uses.
set -euo pipefail

TARGET_USER=${TARGET_USER:-}
STEPS=${STEPS:-"docker kernel faasd k3s sudoers"}
FAASD_VERSION=${FAASD_VERSION:-0.19.6}
CNI_VERSION=${CNI_VERSION:-v1.5.1}
FAAS_CLI_VERSION=${FAAS_CLI_VERSION:-0.17.1}
K3S_VERSION=${K3S_VERSION:-v1.33.1+k3s1}
UPGRADE_CONTAINERD=${UPGRADE_CONTAINERD:-no}
PROXY=${PROXY:-}

log() { printf '\n==> %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
ver_ge() { [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]; }  # $1 >= $2

[ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
if [ -n "$PROXY" ]; then
  export HTTP_PROXY=$PROXY HTTPS_PROXY=$PROXY http_proxy=$PROXY https_proxy=$PROXY
  export NO_PROXY=localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,.svc,.cluster.local
  export no_proxy=$NO_PROXY
fi
proxy_dropin() {  # give a systemd service the proxy settings
  [ -n "$PROXY" ] || return 0
  mkdir -p "/etc/systemd/system/$1.service.d"
  cat > "/etc/systemd/system/$1.service.d/proxy.conf" <<EOF
[Service]
Environment="HTTP_PROXY=$PROXY" "HTTPS_PROXY=$PROXY" "NO_PROXY=$NO_PROXY"
EOF
}

check() {
  log "Preflight"
  . /etc/os-release 2>/dev/null || true
  echo "os: ${PRETTY_NAME:-?}  kernel: $(uname -r)  cgroup: $([ -f /sys/fs/cgroup/cgroup.controllers ] && echo v2 || echo v1)"
  echo "cpus: $(nproc)  mem: $(free -g | awk '/Mem:/{print $2}') GB  /var free: $(df -h /var | awk 'NR==2{print $4}')"
  echo "docker: $(docker --version 2>/dev/null || echo none)"
  echo "containerd: $(containerd --version 2>/dev/null | awk '{print $3}' || echo none)"
  echo "selinux: $(getenforce 2>/dev/null || echo n/a)"
  for u in https://github.com https://ghcr.io/v2/ https://registry-1.docker.io/v2/; do
    echo "reach $u -> $(curl -s -o /dev/null -m 10 -w '%{http_code}' "$u" || echo fail)"
  done
  kmaj=$(uname -r | cut -d. -f1); kmin=$(uname -r | cut -d. -f2)
  if [ "$kmaj" -lt 4 ] || { [ "$kmaj" -eq 4 ] && [ "$kmin" -lt 18 ]; }; then
    warn "kernel $(uname -r) is very old. k3s 1.33 / Kubernetes 1.33 and Knative expect a 4.18+ kernel"
    warn "(RHEL/Rocky/Alma 8+, Ubuntu 20.04+). faasd will probably work; k3s may not. A node with"
    warn "a newer OS is strongly preferred for the Kubernetes stack."
  fi
}

step_docker() {
  [ -n "$TARGET_USER" ] || die "set TARGET_USER"
  log "docker group for $TARGET_USER"
  getent group docker >/dev/null || groupadd docker
  usermod -aG docker "$TARGET_USER"
  getent group systemd-journal >/dev/null && usermod -aG systemd-journal "$TARGET_USER" || true
  echo "added (takes effect at $TARGET_USER's next login)"
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
}

step_faasd() {
  log "faasd $FAASD_VERSION"
  # faasd talks to the system containerd on /run/containerd/containerd.sock.
  # On a Docker host that is Docker's containerd.io, which must be >= 1.5.
  if command -v containerd >/dev/null; then
    cv=$(containerd --version | awk '{print $3}' | sed 's/^v//')
    if ! ver_ge "$cv" 1.5.0; then
      if [ "$UPGRADE_CONTAINERD" = yes ]; then
        log "upgrading containerd.io $cv (shared with Docker) from the Docker CE repo"
        command -v yum-config-manager >/dev/null || yum install -y yum-utils
        [ -f /etc/yum.repos.d/docker-ce.repo ] || \
          yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
        yum install -y containerd.io
        systemctl restart containerd docker
      else
        die "containerd $cv is too old for faasd (needs >= 1.5). It is Docker's containerd.io; rerun with
UPGRADE_CONTAINERD=yes to upgrade it (restarts Docker: running containers on this node stop)."
      fi
    fi
  else
    log "no containerd: installing containerd.io from the Docker CE repo"
    command -v yum >/dev/null || die "no yum; install containerd >= 1.5 first"
    command -v yum-config-manager >/dev/null || yum install -y yum-utils
    [ -f /etc/yum.repos.d/docker-ce.repo ] || \
      yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo
    yum install -y containerd.io
  fi
  # containerd.io ships with the CRI plugin disabled; faasd doesn't need CRI, keep it as is
  proxy_dropin containerd
  systemctl daemon-reload; systemctl enable --now containerd

  mkdir -p /opt/cni/bin
  if [ ! -x /opt/cni/bin/bridge ]; then
    curl -fsSL "https://github.com/containernetworking/plugins/releases/download/${CNI_VERSION}/cni-plugins-linux-amd64-${CNI_VERSION}.tgz" \
      | tar -xz -C /opt/cni/bin
  fi
  curl -fsSL -o /usr/local/bin/faasd "https://github.com/openfaas/faasd/releases/download/${FAASD_VERSION}/faasd"
  curl -fsSL -o /usr/local/bin/faas-cli "https://github.com/openfaas/faas-cli/releases/download/${FAAS_CLI_VERSION}/faas-cli"
  chmod +x /usr/local/bin/faasd /usr/local/bin/faas-cli

  # `faasd install` reads docker-compose.yaml, prometheus.yml, resolv.conf from
  # the current directory, copies them to /var/lib/faasd and creates the
  # faasd + faasd-provider services.
  src=$(mktemp -d); mkdir -p "$src/hack"
  for f in docker-compose.yaml prometheus.yml resolv.conf hack/faasd.service hack/faasd-provider.service; do
    curl -fsSL -o "$src/$f" "https://raw.githubusercontent.com/openfaas/faasd/${FAASD_VERSION}/$f"
  done
  proxy_dropin faasd; proxy_dropin faasd-provider
  (cd "$src" && /usr/local/bin/faasd install)
  rm -rf "$src"

  echo -n "waiting for the faasd gateway "
  for _ in $(seq 1 60); do
    curl -s -o /dev/null http://127.0.0.1:8080/healthz && { echo "up"; break; }
    echo -n "."; sleep 5
  done
  if [ -n "$TARGET_USER" ]; then  # gateway password for faas-cli login, readable by the user only
    home=$(getent passwd "$TARGET_USER" | cut -d: -f6)
    install -m 600 -o "$TARGET_USER" /var/lib/faasd/secrets/basic-auth-password "$home/.faasd-password"
    echo "gateway password copied to $home/.faasd-password"
  fi
}

step_k3s() {
  log "k3s $K3S_VERSION (installed, left stopped)"
  if ! command -v k3s >/dev/null || ! k3s --version | grep -q "${K3S_VERSION}"; then
    curl -fsSL https://get.k3s.io | INSTALL_K3S_VERSION="$K3S_VERSION" INSTALL_K3S_SKIP_START=true \
      INSTALL_K3S_SKIP_ENABLE=true INSTALL_K3S_SELINUX_WARN=true INSTALL_K3S_EXEC="server --disable traefik --write-kubeconfig-mode 644" sh -
  fi
  # the install script copies HTTP(S)_PROXY into /etc/systemd/system/k3s.service.env
  echo "start/stop with: sudo systemctl start k3s / sudo systemctl stop k3s (and k3s-killall.sh)"
}

step_sudoers() {
  [ -n "$TARGET_USER" ] || die "set TARGET_USER"
  log "sudoers rule for $TARGET_USER (these commands only)"
  sc=$(command -v systemctl); ctr=$(command -v ctr || echo /usr/bin/ctr); jc=$(command -v journalctl)
  f=/etc/sudoers.d/pae-experiments
  cat > "$f.tmp" <<EOF
# Conductor+faasd vs Argo+Knative experiments: switch between the two stacks
# (only one runs at a time) and stop a faasd function (cold-start experiment).
Cmnd_Alias PAE_SVC = $sc start faasd, $sc stop faasd, $sc restart faasd, \\
                     $sc start faasd-provider, $sc stop faasd-provider, $sc restart faasd-provider, \\
                     $sc start k3s, $sc stop k3s, $sc restart k3s, /usr/local/bin/k3s-killall.sh
Cmnd_Alias PAE_CTR = $ctr -n openfaas-fn task ls, $ctr -n openfaas-fn task kill *
Cmnd_Alias PAE_LOG = $jc -u faasd*, $jc -u k3s*, $jc -t openfaas-fn\\:*
$TARGET_USER ALL=(root) NOPASSWD: PAE_SVC, PAE_CTR, PAE_LOG
EOF
  visudo -cf "$f.tmp" >/dev/null || { rm -f "$f.tmp"; die "sudoers syntax check failed"; }
  install -m 440 "$f.tmp" "$f"; rm -f "$f.tmp"
  echo "installed $f"
}

check
[ "${1:-}" = check ] && exit 0
for s in $STEPS; do "step_$s"; done

log "Done"
cat <<EOF
For $TARGET_USER (log out and in again first, for the docker group):
  docker ps                                  # Docker works without sudo
  faas-cli login -g http://127.0.0.1:8080 -u admin --password-stdin < ~/.faasd-password
  sudo systemctl stop faasd; sudo systemctl stop faasd-provider   # before using the Kubernetes stack
  sudo systemctl start k3s; export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; kubectl get nodes
EOF
