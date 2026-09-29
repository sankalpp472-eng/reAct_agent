#!/usr/bin/env bash
# Shut the VM down cleanly. Its disk in /tmp is kept, so the next start_vm.sh resumes it.
VM_DIR=${VM_DIR:-/tmp/$(id -un)-pae-vm}
bash "$(dirname "$0")/ssh.sh" sudo poweroff 2>/dev/null || true
for _ in $(seq 1 30); do
  kill -0 "$(cat "$VM_DIR/qemu.pid" 2>/dev/null)" 2>/dev/null || { echo stopped; exit 0; }; sleep 2
done
kill "$(cat "$VM_DIR/qemu.pid")" && echo killed
