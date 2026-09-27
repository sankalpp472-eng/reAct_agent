# Stack B: Argo Workflows + Knative Serving

The same agent loop as `conductor-agentic-loop/` + faasd, on the heavier stack:

| | Stack A | Stack B |
|---|---|---|
| Orchestrator | Conductor (`pae_agentic_loop_v2.json`) | Argo Workflows (`argo-workflowtemplate.yaml`) |
| FaaS | faasd | Knative Serving + Kourier (`knative-services.yaml`) |
| Function images | built by `faas-cli build` | **the same images** |
| Request path | gateway → watchdog → handler | Kourier (Envoy) → queue-proxy → watchdog → handler |

Both stacks use the same function code and container images. They also share the same mock
LLM settings, the same loop (planner → actor → evaluator, up to 8 turns) and the same
metric definitions (`experiments/dataplane.py`). Only the orchestrator and the FaaS
platform differ.

## Files

| File | What it is |
|---|---|
| `setup.sh` | Installs Knative Serving v1.19 + Kourier and Argo Workflows v3.6.19 into the current cluster |
| `knative-services.yaml` | The 5 functions as Knative Services, with the same env as `stack.yaml` |
| `argo-rbac.yaml` | Service account for the Argo HTTP agent, plus its token Secret |
| `argo-workflowtemplate.yaml` | The loop as an Argo WorkflowTemplate |
| `deploy.sh` | Applies the three YAML files (sets your registry/tag in the Services) |
| `argo-requeue.sh` | Sets the Argo controller's requeue time (see below) |

## How the Argo workflow maps to the Conductor one

| Conductor | Argo |
|---|---|
| `DO_WHILE` loop | template `turn` that calls itself until `verdict == done` or 8 turns |
| `HTTP` task | `http` template. All of a workflow's HTTP steps run in **one agent pod**, created when the workflow starts, not one pod per step |
| `JSON_JQ_TRANSFORM` / `SET_VARIABLE` | expression templates `{{=...}}` evaluated by the workflow controller; history is passed between turns as a parameter |

## Settings that matter for the comparison

- **One warm replica per function, like faasd.** Each Knative Service is set to
  `min-scale: 1` / `max-scale: 1`, so it never scales to zero.
- **Activator out of the warm path.** `target-burst-capacity: 0` means Knative's
  activator is only used when scaling up from zero. Warm requests go
  Kourier → queue-proxy → container, which is the path shown in the deck.
- **The actor reaches the tool functions** through their cluster-local Knative address
  (`TOOLS_URL_TEMPLATE=http://{name}.default.svc.cluster.local`). That goes through
  Kourier's internal gateway, so tool calls take the same Knative path as the
  orchestrator's calls.
- **Argo's requeue time dominates Torch.** The controller re-checks a workflow, and the
  HTTP agent reports finished steps, on a `DEFAULT_REQUEUE_TIME` cycle (default **10 s**).
  Every step can therefore wait up to that long before the next one starts.
  `argo-requeue.sh 2s` / `argo-requeue.sh default` switch it. Argo's own code suggests
  ~2 s for workflows of many short steps, with 1 s as the floor. The driver records the
  value in each run's `summary.json`. Run Experiment 1 at the default *and* at a tuned
  value, and say which is which.
- **Everything else uses the Argo and Knative defaults**, the same as Conductor.

## Install and run

On the same machine as stack A. **Stop the other stack while measuring**, so the two don't
compete for CPU:
- stop faasd with `sudo systemctl stop faasd faasd-provider`
- stop Conductor the way you started it, e.g. `docker compose stop`

### 1. Kubernetes: k3s ≥ 1.32

Knative v1.19 refuses to start on older Kubernetes.

```bash
curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION=v1.33.1+k3s1 sh -s - \
    --disable=traefik --write-kubeconfig-mode=644
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml     # add to ~/.bashrc
kubectl get nodes                               # STATUS Ready
```

`--disable=traefik` frees port 80 for Kourier, which k3s's built-in load balancer
exposes on the node's port 80. Nothing else on the host may use port 80.

### 2. Knative + Kourier + Argo

```bash
./argo-knative/setup.sh
```

### 3. Images

These are the same images as faasd. Rebuild and push them if any function code changed
since your last push. For example, the actor changed for this stack (`TOOLS_URL_TEMPLATE`).

```bash
faas-cli build -f stack.yaml && faas-cli push -f stack.yaml
```

### 4. Deploy the functions and the workflow

```bash
REGISTRY=sankalps2003 ./argo-knative/deploy.sh    # your Docker Hub user; TAG defaults to latest
```

Re-run it after every image push. Each deploy creates a new Knative revision, so fresh
`:latest` images are picked up.

### 5. Smoke test: one run

```bash
pip install kubernetes
python experiments/exp1_argo.py --ingress knative://127.0.0.1:80/default.example.com \
    --workloads workloads/retail-44.json --runs 1 --warmup 0
```

`127.0.0.1:80` assumes the driver runs on the k3s node. From another machine, use the
node's IP (`kubectl get svc kourier -n kourier-system` shows it as EXTERNAL-IP). Expect
`Succeeded verdict=done reward=1.0`.

With Argo's default 10 s requeue, one retail-44 run takes a few minutes. That's normal:
see "Argo's requeue time" above.

### 6. Experiment 1

```bash
./argo-knative/argo-requeue.sh default
python experiments/exp1_argo.py --ingress knative://127.0.0.1:80/default.example.com --runs 10 --warmup 1

./argo-knative/argo-requeue.sh 2s
python experiments/exp1_argo.py --ingress knative://127.0.0.1:80/default.example.com --runs 10 --warmup 1
```

### Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Knative pods crash-loop with "Version check failed" | Kubernetes older than 1.32. Upgrade k3s |
| Workflow stuck, agent pod in `Init` with `secret "pae-agent.service-account-token" not found` | `argo-rbac.yaml` not applied. Re-run `deploy.sh` |
| Controller log shows `cannot patch resource "workflowtasksets/status"` and the workflow stalls after a step | Same: `argo-rbac.yaml` grants this |
| Knative revision "Unable to fetch image" | Wrong `REGISTRY`/`TAG`, or the image isn't pushed / is private |
| `WARNING: some HTTP nodes had no _timing` | The deployed images predate the telemetry code. Rebuild, push and re-deploy |
