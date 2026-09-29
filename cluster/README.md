# Running the experiments on a cluster node

The study runs two stacks on one node, one at a time: Conductor + faasd, and Argo
Workflows + Knative on k3s. Both need root to install. After a one-time setup by the
admin, everything else runs as a normal user.

## For the admin: `admin_setup.sh`

Run on the compute node itself (e.g. node13), as root:

```bash
sudo bash cluster/admin_setup.sh check                                # preflight report, changes nothing
sudo TARGET_USER=sankalps bash cluster/admin_setup.sh                 # full setup
sudo TARGET_USER=sankalps PROXY=http://<proxy>:<port> bash cluster/admin_setup.sh   # if the node needs a proxy
```

It does five things, each idempotent and each selectable with `STEPS="..."`:

| Step | What it does | Notes |
|---|---|---|
| `docker` | adds the user to the `docker` group (and `systemd-journal`) | the `docker` group is root-equivalent on that node |
| `kernel` | loads `overlay` and `br_netfilter`, enables IP forwarding | needed by both faasd and k3s |
| `faasd` | installs faasd 0.19.6 + CNI plugins + faas-cli; faasd runs on the system containerd | if Docker's `containerd.io` is older than 1.5, it stops and asks for `UPGRADE_CONTAINERD=yes`, because upgrading restarts Docker |
| `k3s` | installs k3s v1.33.1 as a systemd service, **installed but not started or enabled**, kubeconfig readable (644), traefik off | kernel 3.10 (CentOS 7) is older than Kubernetes 1.33 / Knative expect; see below |
| `sudoers` | lets the user start/stop `faasd`, `faasd-provider`, `k3s`, list/kill faasd function tasks, and read their logs, nothing else | `/etc/sudoers.d/pae-experiments`, checked with `visudo` |

**Internet access.** The node needs internet access while the script runs, to download
faasd, the CNI plugins, k3s, and container images. Later, the services need it too, to
pull images for Knative, Argo and the functions. If the cluster has an HTTP proxy, pass
it as `PROXY=`; the script also configures it for containerd, faasd and k3s.

**OS.** node13 runs CentOS 7 (kernel 3.10, EOL). faasd should work there. k3s 1.33 and
Knative 1.19 are built for 4.18+ kernels (RHEL/Rocky/Alma 8+, Ubuntu 20.04+), so if a
node with a newer OS is available, use that one.

## For the user, after the setup

Log out and back in once, so the `docker` group applies. Then:

```bash
# Stack A: Conductor + faasd
faas-cli login -g http://127.0.0.1:8080 -u admin --password-stdin < ~/.faasd-password
# Conductor runs in Docker; the functions are deployed with faas-cli (see the root README)

# switch to Stack B: Argo + Knative on k3s
sudo systemctl stop faasd; sudo systemctl stop faasd-provider
sudo systemctl start k3s
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml; kubectl get nodes
bash argo-knative/setup.sh          # Knative + Argo, no root needed from here on

# and back
sudo systemctl stop k3s; sudo /usr/local/bin/k3s-killall.sh
sudo systemctl start faasd-provider; sudo systemctl start faasd
```

The cold-start experiment already calls `sudo ctr` (its `--ctr` default), which the
sudoers rule allows.

`check_node.sh` reports what a node offers without root. Run it to see what a node has
before asking for the setup.
