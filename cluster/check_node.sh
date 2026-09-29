#!/usr/bin/env bash
# What can we run on this node without sudo? Read-only: changes nothing.
#   bash cluster/check_node.sh 2>&1 | tee node-report.txt
h() { printf '\n== %s\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1 && echo "yes: $(command -v "$1") $("$1" --version 2>/dev/null | head -1)" || echo "no"; }

h "Machine"
echo "user: $(id -un)  groups: $(id -Gn)"
. /etc/os-release 2>/dev/null; echo "os: ${PRETTY_NAME:-?}  kernel: $(uname -r)  arch: $(uname -m)"
echo "cpus: $(nproc)  mem: $(free -g 2>/dev/null | awk '/Mem:/{print $2" GB total, "$7" GB available"}')"
echo "home: $HOME  free: $(df -h "$HOME" | awk 'NR==2{print $4}')   /tmp free: $(df -h /tmp | awk 'NR==2{print $4}')"
echo "sudo without password: $(sudo -n true 2>/dev/null && echo yes || echo no)"

h "Container runtimes"
for c in docker podman nerdctl apptainer singularity; do echo "$c: $(have $c)"; done
docker info >/dev/null 2>&1 && echo "docker daemon usable by you: yes" || echo "docker daemon usable by you: no"
podman info >/dev/null 2>&1 && echo "podman works rootless: yes" || echo "podman works rootless: no"

h "Kubernetes / job scheduler"
for c in kubectl k3s kind helm sbatch srun; do echo "$c: $(have $c)"; done
kubectl auth can-i create customresourcedefinitions >/dev/null 2>&1 \
  && echo "existing k8s cluster, can create CRDs (cluster-admin-ish): yes" \
  || echo "existing k8s cluster with CRD rights: no"

h "Rootless prerequisites (k3s --rootless, rootless podman/containerd)"
echo "cgroup version: $([ -f /sys/fs/cgroup/cgroup.controllers ] && echo v2 || echo v1)"
echo "cgroup controllers delegated to you: $(cat /sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/cgroup.controllers 2>/dev/null || echo none/unknown)"
echo "subuid entry: $(grep "^$(id -un):" /etc/subuid 2>/dev/null || echo none)"
echo "subgid entry: $(grep "^$(id -un):" /etc/subgid 2>/dev/null || echo none)"
echo "newuidmap: $(have newuidmap)   slirp4netns: $(have slirp4netns)   rootlesskit: $(have rootlesskit)"
echo "unprivileged user namespaces: $(unshare --user --map-root-user true 2>/dev/null && echo yes || echo no)"
echo "systemd --user running: $(systemctl --user is-system-running 2>/dev/null || echo no)"
echo "linger enabled (services survive logout): $(loginctl show-user "$(id -un)" -p Linger 2>/dev/null | cut -d= -f2)"
echo "/dev/fuse: $([ -e /dev/fuse ] && echo yes || echo no)   /dev/kvm: $([ -r /dev/kvm ] && echo yes || echo no)"

h "Tools"
for c in python3 pip3 java git curl jq go; do echo "$c: $(have $c)"; done

h "Network"
for u in https://registry-1.docker.io/v2/ https://ghcr.io https://github.com https://pypi.org/simple/; do
  echo "$u -> $(curl -s -o /dev/null -m 8 -w '%{http_code}' "$u" 2>/dev/null || echo fail)"
done
echo "proxy env: http_proxy=${http_proxy:-} https_proxy=${https_proxy:-}"
