#!/usr/bin/env bash
# Stage 1 - run ONCE on the head node (polaris, has internet). No sudo, and
# nothing installed: it only downloads the Ubuntu image and writes two small files.
#   ~/pae-vm/noble.img        Ubuntu 24.04 cloud image (~600 MB)
#   ~/pae-vm/seed.iso         cloud-init config: user "pae" with your VM SSH key, proxy settings
#   ~/.ssh/pae_vm(.pub)       SSH key for logging into the VM
# The seed disk is made with genisoimage/mkisofs/xorriso if the node has one;
# otherwise with pycdlib (pure Python, ~2 MB), kept in ~/pae-vm/.pycdlib.
set -euo pipefail
VM_HOME=${VM_HOME:-$HOME/pae-vm}
SOCKS_PORT=${SOCKS_PORT:-1080}   # the node's ssh -D port; the VM reaches it at 10.0.2.2
mkdir -p "$VM_HOME" "$HOME/.ssh"

if [ ! -f "$VM_HOME/noble.img" ]; then
  echo "== Ubuntu 24.04 cloud image"
  curl -fL --retry 3 -o "$VM_HOME/noble.img.part" \
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
# NoCloud seed: an ISO labelled "cidata" holding user-data and meta-data
ISO_TOOL=$(command -v genisoimage || command -v mkisofs || true)
if [ -n "$ISO_TOOL" ]; then
  "$ISO_TOOL" -quiet -output "$VM_HOME/seed.iso" -volid cidata -joliet -rock "$TMP/user-data" "$TMP/meta-data"
elif command -v xorriso >/dev/null; then
  xorriso -as mkisofs -quiet -output "$VM_HOME/seed.iso" -volid cidata -joliet -rock "$TMP/user-data" "$TMP/meta-data"
else
  python3 -c "import sys; sys.path.insert(0, '$VM_HOME/.pycdlib'); import pycdlib" 2>/dev/null || \
    python3 -m pip install -q --only-binary=:all: --target "$VM_HOME/.pycdlib" pycdlib
  PYTHONPATH="$VM_HOME/.pycdlib" python3 - "$VM_HOME/seed.iso" "$TMP/user-data" "$TMP/meta-data" <<'PY'
import io, sys, pycdlib
out, *files = sys.argv[1:]
iso = pycdlib.PyCdlib()
iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident="cidata")
for path in files:
    name = path.rsplit("/", 1)[-1]
    data = open(path, "rb").read()
    iso.add_fp(io.BytesIO(data), len(data), "/" + name.replace("-", "").upper() + ".;1",
               rr_name=name, joliet_path="/" + name)
iso.write(out)
iso.close()
PY
fi
rm -rf "$TMP"
echo "done:"; ls -lh "$VM_HOME"
