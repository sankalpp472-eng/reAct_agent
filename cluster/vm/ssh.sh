#!/usr/bin/env bash
# SSH into the VM from the compute node. Extra args are passed to ssh (e.g. a command).
exec ssh -i "$HOME/.ssh/pae_vm" -p "${SSH_PORT:-2222}" -o StrictHostKeyChecking=no \
  -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR pae@127.0.0.1 "$@"
