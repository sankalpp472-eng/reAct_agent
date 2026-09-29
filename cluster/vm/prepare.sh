#!/usr/bin/env bash
# Stage 1 - run ONCE on the head node (polaris, has internet). No sudo needed.
# Everything goes to your home directory, which the compute nodes share:
#   ~/.local/bin/micromamba   package manager (conda-forge), no root
#   ~/pae-env                 Python 3.11 + xorriso (builds the VM's cloud-init disk) + driver packages
#   ~/pae-vm/noble.img        Ubuntu 24.04 cloud image (~600 MB)
#   ~/pae-vm/seed.iso         cloud-init config: user "pae" with your VM SSH key, proxy settings
#   ~/.ssh/pae_vm(.pub)       SSH key for logging into the VM
set -euo pipefail
VM_HOME=${VM_HOME:-$HOME/pae-vm}
ENV=${ENV:-$HOME/pae-env}
SOCKS_PORT=${SOCKS_PORT:-1080}   # the node's ssh -D port; the VM reaches it at 10.0.2.2
mkdir -p "$VM_HOME" "$HOME/.local/bin" "$HOME/.ssh"

if [ ! -x "$HOME/.local/bin/micromamba" ]; then
  echo "== micromamba"
  curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest | tar -xj -C "$HOME/.local" bin/micromamba
fi
MM="$HOME/.local/bin/micromamba"

if [ ! -x "$ENV/bin/python" ]; then
  echo "== Python env $ENV (python 3.11, xorriso, driver packages)"
  "$MM" create -y -p "$ENV" -c conda-forge python=3.11 xorriso pip
  "$ENV/bin/pip" install -q requests kubernetes matplotlib pyyaml
fi

if [ ! -f "$VM_HOME/noble.img" ]; then
  echo "== Ubuntu 24.04 cloud image"
  curl -L -o "$VM_HOME/noble.img.part" \
    https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img
  mv "$VM_HOME/noble.img.part" "$VM_HOME/noble.img"
fi

[ -f "$HOME/.ssh/pae_vm" ] || ssh-keygen -q -t ed25519 -N "" -f "$HOME/.ssh/pae_vm" -C pae-vm

echo "== cloud-init seed"
TMP=$(mktemp -d)
cat > "$TMP/meta-data" <<META
instance-id: pae-vm-1
local-hostname: pae-vm
META
cat > "$TMP/user-data" <<USER
#cloud-config
users:
  - name: pae
    sudo: ALL=(ALL) NOPASSWD:ALL
    shell: /bin/bash
    ssh_authorized_keys:
      - $(cat "$HOME/.ssh/pae_vm.pub")
ssh_pwauth: false
growpart: {mode: auto, devices: ["/"]}
# The VM's only way out is the node's SSH SOCKS tunnel to the head node
# (10.0.2.2 is the node itself, as seen from QEMU's user-mode network).
write_files:
  - path: /etc/apt/apt.conf.d/95pae-proxy
    content: |
      Acquire::http::Proxy "socks5h://10.0.2.2:${SOCKS_PORT}";
      Acquire::https::Proxy "socks5h://10.0.2.2:${SOCKS_PORT}";
  - path: /etc/profile.d/pae-proxy.sh
    content: |
      export ALL_PROXY=socks5h://10.0.2.2:${SOCKS_PORT} HTTPS_PROXY=socks5h://10.0.2.2:${SOCKS_PORT} HTTP_PROXY=socks5h://10.0.2.2:${SOCKS_PORT}
      export NO_PROXY=localhost,127.0.0.1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,.svc,.cluster.local,.local
      export all_proxy=\$ALL_PROXY https_proxy=\$HTTPS_PROXY http_proxy=\$HTTP_PROXY no_proxy=\$NO_PROXY
USER
"$ENV/bin/xorriso" -as mkisofs -quiet -output "$VM_HOME/seed.iso" -volid cidata -joliet -rock \
  "$TMP/user-data" "$TMP/meta-data"
rm -rf "$TMP"
echo "done:"; ls -lh "$VM_HOME"
