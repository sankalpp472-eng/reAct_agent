# Experiments

## Experiment 1: single-agent baseline (Conductor + faasd)

Runs each workload sequentially (C=1) with warm functions and collects the timestamp
pipeline from the deck:

```
[t0] Dispatch -> [t1] Orch Start -> [t_llm] LLM Call -> [t2] Gateway Ping
  -> [t3] Container Enter -> [t4] Container Exit -> [t5] FaaS Response
  -> [t6] Orch Step End -> [t7] Final Answer Received
```

### Where each timestamp comes from

| Timestamp | Source |
|---|---|
| t0 | Driver's clock, just before `POST /api/workflow/pae_agentic_loop` |
| t7 | Driver's clock, at the first status poll that sees the workflow finished (default poll every 50 ms) |
| t1 | Conductor: `scheduledTime` of the first task in a loop turn (`build_planner_context`) |
| t6 | Conductor: `endTime` of the last task in the loop turn (`save_feedback`) |
| t2, t5 | **Conductor → planner/actor/evaluator**: the HTTP task's `startTime` / `endTime`.<br>**Actor → retail-tools/airline-tools**: the actor's clock just before sending / right after receiving |
| t3, t4 | The called function's handler, on entry / just before returning. Reported in the `_timing` object of every response body |
| T_LLM | Time each function spends in LLM calls (`_timing.llm_ms`). With the mock this is the simulated sleeps |

Every metric is a difference of two timestamps from the **same** clock:
- Twarm uses only the function's clock.
- t5 − t2 uses only the caller's clock.
- Tcycle uses only Conductor's clock.
- Te2e uses only the driver's clock.

So the driver, Conductor and faasd can run on different machines.

### Metrics

| Metric | Formula | Computed per |
|---|---|---|
| Te2e | t7 − t0 | run |
| Tcycle | t6 − t1 | loop turn |
| Twarm | t4 − t3 | function call (tool calls reported separately from planner/actor/evaluator) |
| Troute | (t5 − t2) − Twarm | function call |
| Torch | Tcycle − (T_LLM + T_FaaS_HTTP), where T_LLM + T_FaaS_HTTP = Σ(t5 − t2) over the turn's 3 HTTP tasks | loop turn |
| Rfriction | (ΣTorch + ΣTroute) / ΣT_LLM, where ΣTroute counts both Conductor→function and actor→tool hops | run |

The deck's other two Experiment 1 metrics are not measured yet:
- **Sworkflow:** not measured for now.
- **Pass%:** not meaningful with only two tasks.

Each run is still scored with `workloads/score.py` (`reward` column). Only runs that
finish `COMPLETED` with reward 1 go into the stats, because a failed run's timings
aren't comparable.

### What counts as what (worth stating in the write-up)

- **Twarm** is measured inside the function's handler. The OpenFaaS watchdog and the
  template's Flask/waitress layer sit between the gateway and the handler, so their time
  lands in **Troute**, together with the gateway and the network. This is the same place
  Knative's queue-proxy will land for the Argo + Knative stack.
- **Troute** for Conductor→function hops also includes Conductor's own HTTP client work,
  because t2/t5 come from the task's start/end times (ms resolution).
- **Torch** covers everything in a turn that isn't one of the 3 HTTP calls:
  - the JQ and SET_VARIABLE tasks
  - queueing and scheduling gaps between tasks
  - persisting task state

  Time between turns (the DO_WHILE condition check) and before or after the loop is
  not part of any Tcycle. It is still included in Te2e.
- **Warm tools:** each workload gets `--warmup` discarded runs first (default 1). faasd
  keeps one replica always running, so after that every call is warm.

### Before running

The functions and the workflow changed for this experiment:
- every function now returns `_timing`
- the workflow's `append_history` strips `_timing` from the actor's result before
  adding it to the history

So redeploy everything and re-register the workflow:

```bash
faas-cli up -f stack.yaml
jq -s '.' conductor-agentic-loop/pae_agentic_loop_v2.json \
  | curl -s -X PUT "$CONDUCTOR/metadata/workflow" -H 'Content-Type: application/json' -d @-
```

### Run

```bash
python experiments/exp1_conductor.py \
    --conductor http://<conductor-host>:<port>/api \
    --gateway   http://<faasd-host>:8080 \
    --runs 10 --warmup 1
```

This prints a line per run and a summary table. It writes to
`experiments/results/exp1-conductor-<timestamp>/`:

| File | Contents |
|---|---|
| `runs.csv` | one row per run: Te2e, ΣT_LLM, ΣTorch, ΣTroute, Rfriction, mean tool Twarm/Troute, reward |
| `cycles.csv` | one row per loop turn: Tcycle, T_LLM, T_FaaS_HTTP, Torch, Troute |
| `calls.csv` | one row per function call (planner/actor/evaluator and each tool call): t5−t2, Twarm, Troute, LLM time |
| `summary.json` | n / mean / p50 / p95 / std / min / max for each metric, overall and per workload |
| `raw/*.json` | the full Conductor execution JSON of each run, so metrics can be recomputed later |
| `dataplane_*.csv`, `summary.json` → `dataplane` | the stack-independent metrics below, for comparing with Argo + Knative |

## Comparing the two stacks: data-plane metrics

Argo records node start/finish times only to the **second**, so the Conductor task
timestamps used above have no ms-precise Argo equivalent. To compare the stacks fairly,
both drivers also compute the same metrics from timestamps that exist on both, in
`dataplane.py`:
- the driver's t0/t7
- the functions' own t3/t4 and LLM time
- the actor's t2/t5 for tool calls

**Use the `dataplane` numbers when comparing the stacks.** They are the same
definitions and code on both sides.

| Metric | Data-plane definition |
|---|---|
| Te2e | t7 − t0 (unchanged) |
| Twarm, Troute (tool calls) | unchanged: measured the same way on both stacks |
| Troute (orchestrator → function) | Conductor: measured, (t5 − t2) − Twarm. Argo: **estimated** as the run's median tool-call Troute, which takes the same Kourier → queue-proxy → container path |
| Tcycle | planner-to-planner: t3(planner, turn k+1) − t3(planner, turn k). A full turn *including* the loop-back, so a bit larger than the Conductor-task Tcycle. The last turn has none |
| Torch (per turn) | Tcycle − Σ(Twarm + Troute) of that turn's 3 calls |
| Torch_run | Te2e − Σ(Twarm + Troute) over all orchestrator → function calls: everything the orchestrator does outside the calls, including dispatch and completion |
| Rfriction | (Torch_run + ΣTroute orchestrator hops + ΣTroute tool hops) / ΣT_LLM |

Tcycle compares t3 values from *different* functions, so all functions must share a
clock. That holds on a single faasd host and on a single-node k3s cluster.

## Experiment 1 on Argo + Knative

Set up the stack first (see [`argo-knative/README.md`](../argo-knative/README.md)), then:

```bash
pip install kubernetes
python experiments/exp1_argo.py --ingress knative://<node-ip>:80/default.example.com --runs 10 --warmup 1
```

- **How it works:** it submits Workflows through the Kubernetes API (your kubeconfig),
  and a watch on the Workflow gives t7.
- **Reset and scoring:** these go through Kourier with a `Host:` header, so no DNS is needed.
- **Output:** `experiments/results/exp1-argo-<timestamp>/`, with the same `dataplane_*.csv`
  files and `summary.json` → `dataplane` layout as the Conductor run.
