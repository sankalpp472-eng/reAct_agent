#!/usr/bin/env bash
# Stage 3 - run on the compute node (node13). Boots the VM in the background with KVM.
# Its disk lives on the node's local /tmp; the base image is copied there once.
#   CPUS=12 MEM_GB=16 DISK_GB=80 bash cluster/vm/start_vm.sh
#   log in: bash cluster/vm/ssh.sh      stop: bash cluster/vm/stop_vm.sh
set -euo pipefail
VM_HOME=${VM_HOME:-$HOME/pae-vm}
VM_DIR=${VM_DIR:-/tmp/$(id -un)-pae-vm}
CPUS=${CPUS:-12}
MEM_GB=${MEM_GB:-16}
DISK_GB=${DISK_GB:-80}
SSH_PORT=${SSH_PORT:-2222}
QEMU=${QEMU:-/usr/libexec/qemu-kvm}
QEMU_IMG=${QEMU_IMG:-$(command -v qemu-img || true)}
[ -n "$QEMU_IMG" ] || { echo "qemu-img not found; set QEMU_IMG=/path/to/qemu-img" >&2; exit 1; }
mkdir -p "$VM_DIR"; chmod 700 "$VM_DIR"

if [ -f "$VM_DIR/qemu.pid" ] && kill -0 "$(cat "$VM_DIR/qemu.pid")" 2>/dev/null; then
  echo "VM already running (pid $(cat "$VM_DIR/qemu.pid"))"; exit 0
fi
# base image on local disk (not NFS home), read-only; the VM writes to an overlay
[ -f "$VM_DIR/base.img" ] || cp "$VM_HOME/noble.img" "$VM_DIR/base.img"
if [ ! -f "$VM_DIR/disk.qcow2" ]; then
  "$QEMU_IMG" create -f qcow2 -o backing_file="$VM_DIR/base.img",backing_fmt=qcow2 \
    "$VM_DIR/disk.qcow2" "${DISK_GB}G"
fi

"$QEMU" -name pae-vm -machine accel=kvm -cpu host -smp "$CPUS" -m "$((MEM_GB * 1024))" \
  -drive file="$VM_DIR/disk.qcow2",if=virtio,cache=writeback \
  -drive file="$VM_HOME/seed.iso",media=cdrom \
  -netdev user,id=n0,hostfwd=tcp:127.0.0.1:"$SSH_PORT"-:22 -device virtio-net-pci,netdev=n0 \
  -display none -serial file:"$VM_DIR/console.log" \
  -daemonize -pidfile "$VM_DIR/qemu.pid"

echo "VM started (pid $(cat "$VM_DIR/qemu.pid")), console log: $VM_DIR/console.log"
echo -n "waiting for SSH on 127.0.0.1:$SSH_PORT "
for _ in $(seq 1 90); do
  if ssh -q -i "$HOME/.ssh/pae_vm" -p "$SSH_PORT" -o BatchMode=yes -o ConnectTimeout=3 \
       -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null pae@127.0.0.1 true 2>/dev/null; then
    echo " up"; exit 0
  fi
  echo -n "."; sleep 5
done
echo; echo "no SSH after 7.5 min; check: tail -50 $VM_DIR/console.log"; exit 1
