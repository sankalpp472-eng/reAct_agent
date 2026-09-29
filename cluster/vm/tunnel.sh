#!/usr/bin/env bash
# Stage 2 - run on the compute node (node13): a SOCKS proxy to the internet
# through the head node, for the VM. Needs passwordless SSH from the node to the
# head node (see README). Stop: kill $(cat ~/pae-vm/tunnel-$(hostname -s).pid)
set -euo pipefail
HEAD=${HEAD:-polaris}
SOCKS_PORT=${SOCKS_PORT:-1080}
PIDFILE=$HOME/pae-vm/tunnel-$(hostname -s).pid
mkdir -p "$HOME/pae-vm"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "tunnel already running (pid $(cat "$PIDFILE"))"
else
  ssh -f -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o BatchMode=yes \
      -D 127.0.0.1:"$SOCKS_PORT" "$HEAD"
  pgrep -u "$(id -u)" -n -f "ssh -f -N .*-D 127.0.0.1:$SOCKS_PORT $HEAD" > "$PIDFILE"
  echo "tunnel up: socks5 127.0.0.1:$SOCKS_PORT via $HEAD (pid $(cat "$PIDFILE"))"
fi
curl -s -o /dev/null -m 15 -w "github through the tunnel: HTTP %{http_code}\n" \
  -x socks5h://127.0.0.1:"$SOCKS_PORT" https://github.com
